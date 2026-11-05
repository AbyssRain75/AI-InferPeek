"""自动发现本机的部署系统，并在后台线程里每秒轮询一次。发现时顺便记下的检测情况由 diagnostics.py 整理。"""

import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import Future

import psutil

from ..logs import ErrorThrottle
from . import diagnostics
from .base import Instance

log = logging.getLogger("aiinferpeek")

DONE_KEEP = 300.0  # 秒，“xxx 已完成”的提示保留这么久
DONE_MAX = 20
POLL_BUDGET = 0.6  # 一轮轮询最多用掉采样间隔的这个比例，超出的实例这一轮沿用上次的数据
BACKOFF = (0, 5, 10, 30, 60)  # 秒，服务连续第 N 次连不上后隔这么久再试
DISCOVER_BUDGET = 2.0  # 秒，发现阶段每次最多花这么久探测新地址
PROBE_RETRY = 10.0  # 秒，探测失败的地址隔这么久再试


def _process_tree():
    """{父进程号: [子进程号]}。每轮只枚举一次进程表；psutil 的 children() 每调一次都要枚举一次，进程一多就很慢。"""
    tree = defaultdict(list)
    try:
        if hasattr(psutil, "_ppid_map"):
            parents = psutil._ppid_map()
        else:
            parents = {p.pid: p.info["ppid"] for p in psutil.process_iter(["ppid"])}
    except (psutil.Error, OSError):
        log.debug("读取进程表失败", exc_info=True)
        return tree
    for pid, ppid in parents.items():
        if pid != ppid:
            tree[ppid].append(pid)
    return tree


def _descendants(root, tree):
    """root 和它的全部子孙进程，root 已经退出时返回空列表。

    和 psutil 的 children() 一样，比 root 启动得早的不算：那是父进程号被复用前留下的进程。
    """
    try:
        started = psutil.Process(root).create_time()
    except psutil.NoSuchProcess:
        return []
    except psutil.Error:
        return [root]  # 没有权限看它的子进程时只算它自己
    out, seen, stack = [root], {root}, list(tree.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            if psutil.Process(pid).create_time() < started:
                continue
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            pass  # 读不到启动时间时只按父进程号算
        out.append(pid)
        stack.extend(tree.get(pid, ()))
    return out


def _launchers(matched):
    """matched（{进程号: 适配器}）里的启动器：子进程也被同一个适配器认出的进程。
    Windows 上 venv 里的 python.exe 只是个启动器，带着同样的启动参数再开一个真正的 python 子进程，接口在子进程上，只算子进程。"""
    if len(matched) < 2:
        return set()
    out = set()
    for pid, adapter in matched.items():
        try:
            ppid = psutil.Process(pid).ppid()
        except psutil.Error:
            continue
        if ppid != pid and matched.get(ppid) is adapter:
            out.add(ppid)
    return out


def _listen_ports():
    """({进程号: {(ip, 端口)}}, 读不到时的错误)"""
    ports = defaultdict(set)
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError) as exc:
        return ports, str(exc) or type(exc).__name__
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.pid:
            ports[c.pid].add((c.laddr.ip, c.laddr.port))
    return ports, None


def _url(ip, port):
    if ip in ("0.0.0.0", "::", ""):
        ip = "127.0.0.1"
    elif ":" in ip:
        ip = f"[{ip}]"
    return f"http://{ip}:{port}"


def _probe_order(adapter, addrs):
    # 默认端口优先，其余按端口号
    return sorted(addrs, key=lambda a: (a[1] not in adapter.default_ports, a[1]))


class RuntimeManager:
    """adapters、instances 只在后台线程里读写（构造时除外）；别的线程要换适配器和手动添加的地址用 reconfigure()。"""

    def __init__(self, adapters, endpoints=(), interval=1.0, scan_every=5.0):
        self.adapters = list(adapters)
        self.interval = interval
        self.scan_every = scan_every
        self.instances = {}
        self._set_endpoints(endpoints)
        self._latest = []
        self._owners = {}
        self._diag = None  # 最近一次发现时各工具的检测情况，发现时整个换掉、不再修改
        self._probe_failed = {}  # (进程号, ip, 端口) -> (什么时候可以再探测, 错误)
        self._pending = None  # reconfigure() 交过来、还没换上的 (适配器, 地址)
        self._waiters = []  # 等 reconfigure() 生效的线程
        self._errors = ErrorThrottle()  # 每秒轮询、每 5 秒发现，同样的错误不每次都写日志
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()  # 停止或换配置时叫醒后台线程，不用等这一轮的间隔
        self._thread = threading.Thread(target=self._run, name="runtimes", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)

    def reconfigure(self, adapters, endpoints=(), timeout=None):
        """换成新的适配器和手动添加的地址，可以在别的线程里调用。

        后台线程下一轮开始时（马上被叫醒）换上，接着重新发现一次；timeout 不为 None 时等到这次发现做完，
        返回是否已经生效（超时返回 False，之后照样会生效）。连着调用几次时只换上最后一次的。
        和原来是同一个对象的适配器保留它的实例和状态（读到一半的日志、已完成的提示），其余的实例丢掉重新发现。
        """
        done = threading.Event()
        with self._lock:
            self._pending = (list(adapters), list(endpoints))
            self._waiters.append(done)
        self._wake.set()
        return done.wait(timeout) if timeout is not None else False

    def _apply_pending(self):
        """在后台线程里换上 reconfigure() 交过来的配置，返回要通知的等待者；没有新配置时返回空列表。
        正在轮询的线程不用管：它轮询的是丢掉的实例时，结果没人取，线程做完自己退出。"""
        with self._lock:
            pending, self._pending = self._pending, None
            waiters, self._waiters = self._waiters, []
        if pending is None:
            return waiters
        self.adapters, endpoints = pending
        keep = {id(a) for a in self.adapters}
        for key, inst in list(self.instances.items()):
            if id(inst.adapter) not in keep:
                del self.instances[key]
        self._set_endpoints(endpoints)
        return waiters

    def _set_endpoints(self, endpoints):
        """手动添加的地址：去掉不再需要的，补上新加的；没变的保留（连同它的状态）。
        和本机发现的实例地址相同时以本机发现的为准（能看到进程资源）。"""
        by_kind = {a.kind: a for a in self.adapters}
        wanted = {}
        for ep in endpoints:
            adapter = by_kind.get(ep.get("kind"))
            if adapter and ep.get("url"):
                inst = Instance(adapter, ep["url"].rstrip("/"), configured=True)
                wanted.setdefault(inst.id, inst)
        for key, inst in list(self.instances.items()):
            if inst.configured and key not in wanted:
                del self.instances[key]
        for key, inst in wanted.items():
            self.instances.setdefault(key, inst)

    def snapshot(self):
        with self._lock:
            return list(self._latest)

    def owners(self):
        with self._lock:
            return dict(self._owners)

    def diagnostics(self):
        """最近一次发现时各工具的检测情况；已发现的实例再看最近一轮轮询连不连得上。可以在别的线程里调用。"""
        with self._lock:
            diag = self._diag
            latest = {p["id"]: p for p in self._latest}
        return diagnostics.merge(diag, latest, self.scan_every)

    # ---------- 发现 ----------

    def _discover(self):
        names = set().union(*(a.process_names for a in self.adapters))
        candidates = []
        for p in psutil.process_iter(["pid", "name"]):
            name = p.info["name"] or ""
            if name.lower() in names:
                try:
                    candidates.append((p.info["pid"], name, p.cmdline()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        matched = {}  # 进程号 -> 认出它的模型服务适配器
        for pid, name, cmdline in candidates:
            adapter = next((a for a in self.adapters if a.category == "service" and a.match_pid(pid, name, cmdline)), None)
            if adapter:
                matched[pid] = adapter
        for pid in _launchers(matched):
            del matched[pid]

        alive = set()
        ports = ports_error = None
        waiting = {}  # 进程号 -> 还没连上接口时卡在哪一步（诊断用）
        deadline = time.monotonic() + DISCOVER_BUDGET
        for pid, adapter in matched.items():
            alive.add(pid)
            existing = next((i for i in self.instances.values() if i.main_pid == pid), None)
            if existing and existing.url:
                continue
            if ports is None:
                ports, ports_error = _listen_ports()
            addrs = ports.get(pid, ())
            inst = self._probe_process(adapter, pid, addrs, deadline)
            if not inst.url:
                waiting[pid] = diagnostics.port_check(pid, ports_error, self._tried(adapter, pid, addrs))
            if existing:
                del self.instances[existing.id]
            self.instances[inst.id] = inst

        for key, inst in list(self.instances.items()):
            if inst.main_pid and inst.main_pid not in alive:
                del self.instances[key]
        # 探测失败的记录留到进程退出：页面上要显示每个地址最后一次的错误
        self._probe_failed = {k: v for k, v in self._probe_failed.items() if k[0] in alive}

        # 命令行工具：本机所有进程合成一个实例，没有进程时卡片消失
        find_errors = {}
        for adapter in self.adapters:
            if adapter.category != "cli":
                continue
            key = f"{adapter.kind}@local"
            try:
                roots = adapter.find_processes(candidates)
            except Exception as exc:
                self._errors.exception(("find", adapter.kind), "查找 %s 进程失败", adapter.label)
                find_errors[adapter.kind] = str(exc) or type(exc).__name__
                roots = []
            if not roots:
                self.instances.pop(key, None)
                continue
            inst = self.instances.get(key) or Instance(adapter, None)
            inst.roots = roots
            self.instances[key] = inst

        diag = diagnostics.collect(self.adapters, self.instances.values(), waiting, find_errors, self.scan_every)
        with self._lock:
            self._diag = diag

    def _probe_process(self, adapter, pid, addrs, deadline):
        # 最近探测失败的地址先跳过，这次扫描的时间用完了就等下次
        for ip, port in _probe_order(adapter, addrs):
            now = time.monotonic()
            if now > deadline or self._probe_failed.get((pid, ip, port), (0, None))[0] > now:
                continue
            url = _url(ip, port)
            try:
                meta = adapter.probe(url)
                error = None if meta is not None else f"返回的内容不是 {adapter.label} 的接口"
            except ValueError:
                meta, error = None, f"返回的不是 JSON，不是 {adapter.label} 的接口"
            except Exception as exc:
                meta, error = None, str(exc) or type(exc).__name__
            if meta is not None:
                inst = Instance(adapter, url, main_pid=pid)
                inst.meta = meta
                return inst
            self._probe_failed[(pid, ip, port)] = (time.monotonic() + PROBE_RETRY, error)
        # 进程在但接口还没起来（例如正在启动）
        return Instance(adapter, None, main_pid=pid)

    def _tried(self, adapter, pid, addrs):
        """进程监听的每个地址（按探测顺序）和最近一次探测失败的原因，诊断用。"""
        return [
            {"url": _url(ip, port), "error": self._probe_failed.get((pid, ip, port), (0, None))[1]}
            for ip, port in _probe_order(adapter, addrs)
        ]

    # ---------- 轮询 ----------

    def _refresh_pids(self, inst, tree):
        pids = []
        for root in inst.roots:
            pids += _descendants(root, tree)
        inst.pids = list(dict.fromkeys(pids))
        known = set(inst.pids)
        inst.children = {pid: kids for pid in inst.pids if (kids := [c for c in tree.get(pid, ()) if c in known])}

    @staticmethod
    def _base(inst):
        # polled：最后一次成功轮询的时间（毫秒），页面拿它判断这个实例的数据是不是停在了旧的一帧
        return {
            "id": inst.id,
            "kind": inst.adapter.kind,
            "label": inst.adapter.label,
            "category": inst.adapter.category,
            "url": inst.url,
            "local": inst.local,
            "pids": inst.pids,
            "polled": inst.state.get("polled"),
        }

    def _poll_safe(self, inst, tree):
        """一个实例出错不影响其他实例；同样的错误不每次都写日志，免得每秒刷屏。"""
        try:
            return self._poll(inst, tree)
        except Exception as exc:
            self._errors.exception(("poll", inst.id), "轮询 %s 出错", inst.id)
            return {**self._base(inst), "status": "error", "error": str(exc), "done": inst.done}

    def _poll(self, inst, tree):
        st = inst.state
        if inst.local:
            self._refresh_pids(inst, tree)
        if inst.adapter.category == "service" and not inst.url:
            data = {"status": "starting"}
            st["polled"] = int(time.time() * 1000)
        elif time.monotonic() < st.get("retry_at", 0):
            data = st["offline"]  # 刚连不上，等一会儿再试，免得每秒都卡在超时上
        else:
            try:
                if inst.configured and not inst.meta:
                    inst.meta = inst.adapter.probe(inst.url) or {}
                data = inst.adapter.poll(inst)
                if not isinstance(data, dict):
                    raise TypeError(f"poll() 返回了 {type(data).__name__}，应该返回 dict")
            except Exception as exc:
                log.debug("轮询 %s 失败：%s", inst.id, exc)
                data = {"status": "offline" if isinstance(exc, OSError) else "error", "error": str(exc)}
                if isinstance(exc, OSError) and inst.adapter.category == "service":
                    failures = st.get("failures", 0) + 1
                    delay = BACKOFF[min(failures, len(BACKOFF)) - 1]
                    st.update(failures=failures, retry_at=time.monotonic() + delay, offline=data)
            else:
                st.pop("failures", None)
                st.pop("retry_at", None)
                st["polled"] = int(time.time() * 1000)
                self._finished(inst, data)
        return {
            **self._base(inst),
            **(inst.meta if isinstance(inst.meta, dict) else {}),
            **{k: v for k, v in data.items() if not k.startswith("_")},  # _ 开头的只给 finished() 用
            "done": inst.done,
        }

    def _finished(self, inst, data):
        """和上一次成功读到的数据比，记下新完成的任务；第一次读到的是现状，不算。"""
        prev = inst.state.get("prev_data")
        inst.state["prev_data"] = data
        events = []
        if prev is not None:
            try:
                events = list(inst.adapter.finished(prev, data))
                if not all(isinstance(e, dict) and isinstance(e.get("t"), (int, float)) for e in events):
                    raise TypeError('finished() 应该返回 [{..., "t": 毫秒时间}] 这样的列表')
            except Exception:
                events = []
                self._errors.exception(("finished", inst.id), "比较 %s 的任务失败", inst.id)
        cutoff = time.time() * 1000 - DONE_KEEP * 1000
        inst.done = [e for e in inst.done + events if e["t"] >= cutoff][-DONE_MAX:]

    def _start_poll(self, inst, tree):
        """在单独的线程里轮询一个实例。线程设成 daemon：卡在阻塞调用里时也不会让程序退不出去。"""
        job = Future()
        threading.Thread(target=lambda: job.set_result(self._poll_safe(inst, tree)), name="poll", daemon=True).start()
        return job

    def _tick(self):
        deadline = time.monotonic() + self.interval * POLL_BUDGET
        # 最久没轮到的先轮；时间预算用完后，后面的实例这一轮沿用上次的数据，下一轮排在最前
        insts = sorted(self.instances.values(), key=lambda i: i.state.get("polled_at", 0.0))
        tree = _process_tree() if any(i.local for i in insts) else {}
        payloads = []
        for inst in insts:
            st = inst.state
            cached = st.get("payload")
            job = st.get("job")
            if job is None:
                if cached is not None and time.monotonic() > deadline:
                    payloads.append(cached)
                    continue
                job = st["job"] = self._start_poll(inst, tree)
                st["polled_at"] = time.monotonic()
                # 等到预算用完为止；第一次轮询没有旧数据可用，多等一个间隔
                wait = deadline - time.monotonic() + (0 if cached is not None else self.interval)
            else:
                wait = 0  # 上一轮没轮询完的只看完成了没有，不再等
            try:
                payload = job.result(timeout=max(0.0, wait))
            except TimeoutError:
                # 很慢或卡在阻塞调用里：这个实例沿用上次的数据（polled 不变，页面会标出过期），不挡住后面的实例。
                # 第一次轮询就没完成的先显示“启动中”，polled 记成开始等的时间，等太久同样标出过期
                if cached is None:
                    since = st.setdefault("waiting_since", int(time.time() * 1000))
                    cached = {**self._base(inst), "status": "starting", "done": inst.done, "polled": since}
                payloads.append(cached)
                continue
            del st["job"]
            st["payload"] = payload
            payloads.append(payload)
        # 模型服务排在命令行工具前面；同一个进程同时算在两边时（比如在 Claude Code 里启动了 ComfyUI）归模型服务
        payloads.sort(key=lambda p: (p["category"] != "service", p["kind"], p["id"]))
        owners = {}
        for p in payloads:
            for pid in p["pids"]:
                owners.setdefault(pid, {"id": p["id"], "label": p["label"]})
        with self._lock:
            self._latest = payloads
            self._owners = owners

    def _run(self):
        next_scan = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            waiters = self._apply_pending()
            if started >= next_scan or waiters:
                try:
                    self._discover()
                except Exception:
                    self._errors.exception("discover", "扫描部署系统失败")
                next_scan = started + self.scan_every
            # 兜底：这一轮出了没料到的错也不能让线程退出，不然所有卡片都会停在最后一帧
            try:
                self._tick()
            except Exception:
                self._errors.exception("tick", "轮询部署系统出错")
            # 换完配置、发现并轮询过一轮才算生效：新加的地址连不连得上这时已经知道了
            for done in waiters:
                done.set()
            self._wake.wait(max(0.0, self.interval - (time.monotonic() - started)))
            self._wake.clear()
