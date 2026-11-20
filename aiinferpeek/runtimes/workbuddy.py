"""WorkBuddy（腾讯的 AI 办公助手桌面版，内核是 CodeBuddy 的命令行程序）。

字段名查自 5.11.3 版（内核 2.176.0）自带的程序代码，会话记录的写法对照过这个版本做完的会话；
被停止、出错时写的标记只查了代码，没碰上过实际的记录。

- 是否在运行：WorkBuddy.exe。桌面版的界面、后台服务和每个会话的内核都是这个程序、都叫这个名字，只算最上面的
  （父进程不是 WorkBuddy 的）那个，其余都是它的子进程。
- 会话：数据目录（~/.workbuddy）里的 projects/<目录>/<会话 id>.jsonl，一个进程对不上所有会话，所以列出最近有活动的会话。
  只解析下面这几种行，对话原文所在的其余行不解析：
  - 你发的消息（providerData.startsNewUserRequest 为 true）：这一轮开始
  - 带用量的行（message.usage）：模型每回复一次，最后一项带着这次请求的用量、模型。最后一项是工具调用（function_call）
    时这一轮还要继续，是助手的消息（message）时这一轮做完了。一次回复里有几个工具调用时每个都带同样的用量，按 messageId 只算一次
  - 程序自己插入的助手消息（providerData.skipRun 为 true）：status 是 incomplete 时这一轮没做完，内容是固定的
    "Interrupted by user" 时算被你停止，其余算出错（只比是不是这句话，不取内容）；流超时的那种（error.isStreamTimeout）
    之后会自己重试，不算结束
  - 标题：ai-title（自动起的）、custom-title（你改的）
  子 agent 的记录在 <会话 id>/subagents 下，不列出。
- 这一轮还在不在做：WorkBuddy 给每个会话单独开一个内核进程，登记在 sessions/<进程号>.json（会话 id、进程的启动时间），
  这一轮做完几秒后就把它关掉。会话记录里这一轮还没结束、内核进程也还在才算工作中；进程没了而这一轮没结束，算被中断
  （关的时候不删登记，进程号会被重新分配，所以核对启动时间）。登记里没有“在等你确认”，等你回答问题时也显示工作中。
- 额度（Beta）：服务器随请求返回的额度写在那一行的 providerData.rawUsage.quota 里（每轮的第一次请求才带，其余是 null）：
  model 是算额度的模型，percent 是已用的百分比，reset_at 是重置时间（秒，写成字符串），shared_models 是和它共用这份额度的模型。
  程序代码里没有用到这几个字段，含义是照本机的记录推断的（同一个模型先是 0、用过之后是 2，重置时间在第一次用的 24 小时后），
  所以标成 Beta。每份额度取各会话里最近一次返回的；最近 3 小时没有会话时也留着最新的那个会话文件，额度从它读。
- 上下文上限：桌面版记的 storage/<账号>/scoped/<id>/conversationContextUsage.json（每个会话的 size），没有就只显示已用的。
- 版本号：last-launch.json（桌面版每次启动时写）。
"""

import glob
import math
import os
import time

import psutil

from ..i18n import N_
from .base import ppid_map, prune
from .cli import CliAdapter, MAX_SESSIONS, RECENT, Rate, outcome, read_json, session, summarize, track, usage
from .jsonl import JsonlTail, parse_ts

INTERRUPTED = "Interrupted by user"  # 你按停止时程序插入的助手消息，内容固定是这句
EXITED = outcome("interrupted", N_("会话进程退出时本轮尚未完成"))
RESCAN = 2.0  # 秒，有内核进程在跑、它的会话还没列出来时，隔这么久就重新找一次会话文件（平时是 SCAN_EVERY）
CONTEXT_FILE = "conversationContextUsage.json"
MARKERS = (b'"startsNewUserRequest":true', b'"message":{"usage"', b'"skipRun":true', b'"aiTitle"', b'"customTitle"', b'"quota":{')


def default_home():
    return os.path.join(os.path.expanduser("~"), ".workbuddy")


def _number(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _created(pid):
    """进程的启动时间（秒），读不到时返回 None。"""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _same_process(entry):
    """登记的内核进程还在，而且是同一个进程（不是退出后进程号被别的程序用了）。"""
    created = _created(entry["pid"])
    if created is None:
        return False
    identity = entry.get("processIdentity")
    started = parse_ts(identity.get("procStart")) if isinstance(identity, dict) else None
    if started is not None:
        return abs(started / 1000 - created) < 2
    # 老版本没记进程的启动时间：登记是进程启动后才写的，进程比登记还晚启动就不是它
    registered = _number(entry.get("startedAt"))
    return registered is None or created <= registered / 1000 + 2


def _interrupted(content):
    """助手消息的内容是不是只有那句固定的 "Interrupted by user"。"""
    if isinstance(content, str):
        return content == INTERRUPTED
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "output_text" and b.get("text") == INTERRUPTED for b in content)


class Transcript:
    """一个会话的记录文件：累计用量，记下标题、模型、上下文，这一轮什么时候开始、怎么结束的。"""

    def __init__(self, path):
        self.id = os.path.basename(path)[: -len(".jsonl")]
        self.tail = JsonlTail(path, MARKERS)
        self._reset()

    def _reset(self):
        self.cwd = self.title = self.custom = self.model = None
        self.turn = None  # 进行中的这一轮从什么时候开始（毫秒），不在做时是 None
        self.ended = None  # 上一轮怎么结束的
        self.totals = [0, 0, 0]  # 输入（含缓存命中的）、其中缓存命中的、输出
        self.reply = (None, (0, 0, 0))  # 最近一次回复的 messageId 和用量，同一次回复的几行只算一次
        self.context = None
        self.last = None
        self.rate = Rate()
        self.quotas = {}  # 共用一份额度的模型（排好序）-> 最近一次返回的额度，见 _quota()
        self.shown = None  # 上一轮列出的会话行，见 WorkBuddyAdapter.poll()

    @property
    def name(self):
        return self.custom or self.title

    def update(self):
        restarted, objs = self.tail.read()
        if restarted:
            self._reset()
        for o in objs:
            kind = o.get("type")
            if kind == "ai-title":
                self.title = self._text(o.get("aiTitle")) or self.title
                continue
            if kind == "custom-title":
                self.custom = self._text(o.get("customTitle")) or self.custom
                continue
            t = _number(o.get("timestamp"))
            data = o.get("providerData")
            data = data if isinstance(data, dict) else {}
            if isinstance(o.get("cwd"), str):
                self.cwd = o["cwd"] or self.cwd
            role = o.get("role") if kind == "message" else None
            if t is None or kind not in ("message", "function_call"):
                continue
            self._quota(data.get("rawUsage"), t)
            if role == "user":
                if data.get("startsNewUserRequest") is True:
                    self.turn, self.ended = t, None
                    self.last = max(self.last or 0, t)
                continue
            if role == "assistant" and data.get("skipRun") is True:
                error = data.get("error")
                if isinstance(error, dict) and error.get("isStreamTimeout"):
                    continue
                if o.get("status") != "incomplete":
                    self._end(outcome("ok"), t)
                else:
                    self._end(outcome("interrupted" if _interrupted(o.get("content")) else "failed"), t)
                continue
            if self._usage(o, data, t):
                if role == "assistant":
                    self._end(outcome("ok"), t)
                elif self.turn is None:  # 不是你发的消息开的头（出错后自己接着做这类），从这次回复算起
                    self.turn, self.ended = t, None

    @staticmethod
    def _text(value):
        return value.strip() if isinstance(value, str) else None

    def _end(self, result, t):
        self.turn, self.ended = None, result
        self.last = max(self.last or 0, t)

    def _quota(self, raw, t):
        """记下这一行里服务器返回的额度（见模块说明），没有或写得不对时不管。"""
        q = raw.get("quota") if isinstance(raw, dict) else None
        if not isinstance(q, dict):
            return
        model, used = q.get("model"), _number(q.get("percent"))
        if not isinstance(model, str) or not model or used is None:
            return
        try:
            resets = float(q.get("reset_at")) * 1000
        except (TypeError, ValueError):
            resets = None
        shared = q.get("shared_models")
        models = [model] + [m for m in (shared if isinstance(shared, list) else ()) if isinstance(m, str) and m and m != model]
        pool = {"names": models, "models": models, "used": used, "resets": resets if resets and math.isfinite(resets) else None, "t": t}
        self.quotas[tuple(sorted(models))] = pool

    def _usage(self, o, data, t):
        """记上这一行的用量，没有用量时返回 False。"""
        message = o.get("message")
        u = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(u, dict):
            return False
        row = tuple(_number(u.get(k)) or 0 for k in ("input_tokens", "cache_read_input_tokens", "output_tokens"))
        mid, old = self.reply
        reply = data.get("messageId")
        if reply is None or reply != mid:
            old = (0, 0, 0)
        self.reply = (reply, row)
        for i in range(3):
            self.totals[i] += row[i] - old[i]
        self.rate.add(t, row[2] - old[2])
        self.last = max(self.last or 0, t)
        self.context = row[0]
        model = data.get("requestModelName") or data.get("model")
        if isinstance(model, str):
            self.model = model
        return True


class WorkBuddyAdapter(CliAdapter):
    kind = "workbuddy"
    label = "WorkBuddy"
    process_names = frozenset({"workbuddy.exe", "workbuddy"})
    config_key = "workbuddy_dir"

    def __init__(self, home=None):
        self.home = home or default_home()

    def data_paths(self):
        return [("home", self.home), ("data", os.path.join(self.home, "projects"))]

    def find_processes(self, candidates):
        """最上面的 WorkBuddy 进程，见模块说明。父进程也叫 WorkBuddy、但比它启动得晚的，是父进程号被复用了，不是它的父进程。"""
        mine = sorted(pid for pid, name, _ in candidates if name.lower() in self.process_names)
        if not mine:
            return []  # 没开着时不用枚举进程表
        parents = ppid_map()
        created = {pid: _created(pid) for pid in mine}
        roots = []
        for pid in mine:
            parent = parents.get(pid)
            if parent in created and parent != pid:
                a, b = created[parent], created[pid]
                if a is None or b is None or a <= b:
                    continue
            roots.append(pid)
        return roots

    def _kernels(self, st):
        """{会话 id: 进程号}：登记在 sessions 文件夹里、进程还在的会话内核。登记文件每 30 秒重写一次，
        正好在写、读不出来时沿用上次读到的，不然这一轮会被当成进程没了。"""
        cache = st.setdefault("registry", {})  # 文件名 -> ((修改时间, 大小), 登记内容)
        seen = set()
        try:
            entries = list(os.scandir(os.path.join(self.home, "sessions")))
        except OSError:
            entries = []
        for de in entries:
            if not de.name.endswith(".json"):
                continue
            try:
                info = de.stat()
            except OSError:
                continue
            seen.add(de.name)
            key = (info.st_mtime_ns, info.st_size)
            if cache.get(de.name, (None,))[0] != key:
                entry = read_json(de.path)
                if isinstance(entry, dict):
                    cache[de.name] = (key, entry)
        prune(cache, seen)
        out = {}
        for _, e in cache.values():
            pid, sid = e.get("pid"), e.get("sessionId")
            # 只要会话的内核；后台服务（daemon）、预热的进程（prewarm）也登记在这里
            if isinstance(pid, int) and pid > 0 and isinstance(sid, str) and e.get("kind", "interactive") == "interactive" and _same_process(e):
                out[sid] = pid
        return out

    def _windows(self):
        """{会话 id: 上下文上限}，桌面版记的，见模块说明。"""
        out = {}
        for path in glob.glob(os.path.join(glob.escape(os.path.join(self.home, "storage")), "*", "scoped", "*", CONTEXT_FILE)):
            data = read_json(path)
            for sid, row in (data.items() if isinstance(data, dict) else ()):
                size = _number(row.get("size")) if isinstance(row, dict) else None
                if size:
                    out[sid] = size
        return out

    def _scan_sessions(self, st, now):
        """最近有活动的会话文件；一个都没有时留着最新的那个，只从它读额度。版本号和各会话的上下文上限也在这时读。

        内核进程还在的会话（st["kernels"]）不看修改时间、排在最前：进程开着文件往里追加时，文件夹里记的修改时间不一定跟着变
        （Codex 桌面版就是这样），隔了很久接着聊的旧会话不能因此漏掉。
        """
        launch = read_json(os.path.join(self.home, "last-launch.json"))
        version = launch.get("version") if isinstance(launch, dict) else None
        st["version"] = version if isinstance(version, str) else None
        st["windows"] = self._windows()
        running = st.get("kernels", {})
        found = []
        newest = None  # 最近没有活动的会话文件里最新的一个：(修改时间, 路径)
        try:
            folders = [de.path for de in os.scandir(os.path.join(self.home, "projects")) if de.is_dir()]
        except OSError:
            return []
        for folder in folders:
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for de in entries:
                try:
                    if not de.name.endswith(".jsonl") or not de.is_file():
                        continue
                    mtime = de.stat().st_mtime
                    if de.name[: -len(".jsonl")] in running:
                        found.append((now, de.path))
                    elif now - mtime < RECENT:
                        found.append((mtime, de.path))
                    elif newest is None or mtime > newest[0]:
                        newest = (mtime, de.path)
                except OSError:
                    continue
        if not found and newest:
            return [newest[1]]  # 不列出（太久没有活动），只从它读额度
        found.sort(reverse=True)
        return [path for _, path in found[: MAX_SESSIONS * 2]]

    def poll(self, inst):
        st = inst.state
        now_s = time.time()
        now = now_s * 1000
        kernels = st["kernels"] = self._kernels(st)
        listed = {tr.id for tr in st.get("transcripts", {}).values()}
        # 刚发出第一条消息的新会话：不等下一次定时找，免得很短的一轮整个落在两次之间
        if not kernels.keys() <= listed and now_s - st.get("scan_t", 0) >= RESCAN:
            st["scan_t"] = 0
        transcripts = track(st.setdefault("transcripts", {}), self._session_paths(st, now_s), Transcript)

        sessions = []
        quotas = {}  # 共用一份额度的模型 -> 各会话里最近一次返回的额度
        for tr in transcripts:
            tr.update()
            # 很大的会话文件要分几轮才读完，没读完时算出的状态不是现状：第一次读时不列出，已经列出过的照样列上一轮的样子（同 Codex）
            if tr.tail.behind:
                if tr.shown:
                    sessions.append(tr.shown)
                continue
            for key, q in tr.quotas.items():
                if key not in quotas or q["t"] > quotas[key]["t"]:
                    quotas[key] = q
            if tr.last is None or now - tr.last >= RECENT * 1000:
                tr.shown = None
                continue
            pid = kernels.get(tr.id)
            busy = tr.turn is not None and pid is not None
            tot = tr.totals
            tr.shown = session(
                id=tr.id,
                pid=pid,
                name=tr.name,
                cwd=tr.cwd,
                status="busy" if busy else "idle",
                since=tr.turn if busy else tr.last,
                model=tr.model,
                context=tr.context,
                context_window=st["windows"].get(tr.id),
                tokens=usage(*tot) if any(tot) else None,
                rate=tr.rate.per_second(now),
                last=tr.last,
                outcome=EXITED if tr.turn is not None and not busy else tr.ended,
            )
            sessions.append(tr.shown)
        limits = None
        if quotas:
            pools = [{k: q[k] for k in ("names", "models", "used", "resets")} for _, q in sorted(quotas.items())]
            limits = {"source": "workbuddy", "t": max(q["t"] for q in quotas.values()), "pools": pools}
        return summarize(sessions, limit=MAX_SESSIONS, version=st.get("version"), home=self.home, recent=RECENT, limits=limits)
