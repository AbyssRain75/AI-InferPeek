"""按进程统计主显卡上的显存、共享显存、GPU 利用率，以及进程的内存和 CPU。

主显卡：有独显时是所有独显，否则是核显（见 wddm.main_luids）。
模型服务的进程用的是哪几块显卡另外标出（devices），这个看所有显卡，模型跑在核显上也看得出来。
有几块主显卡（比如两块独显）时，每行还标出它在哪块主显卡上，共享显存也按主显卡分开合计（shared）。
数据来自 Windows 性能计数器（和任务管理器“详细信息”页同源），不分厂商。
nvidia-smi 在 Windows 上读不到单个进程的显存，所以不用 NVML。
"""

import re
from collections import defaultdict

import psutil

from .pdh import PdhError, PdhQuery

MB = 1024 * 1024
# 显存 + 共享显存低于这个值、又不属于任何模型服务的进程不列出
MIN_BYTES = 64 * MB
MAX_ROWS = 12
# 模型服务的进程在一块显卡上的显存 + 共享显存不少于这个值，才算在用这块显卡（只建了个上下文的不算）
GPU_MIN_BYTES = 32 * MB

# 实例名：pid_1234_luid_0x…_0x…_phys_0，\GPU Engine 的后面还有 _eng_3_engtype_Copy（Intel 有些引擎没有类型名）
_INSTANCE = re.compile(r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)(?:_eng_(\d+)_engtype_(.*))?$", re.I)


def used_devices(per_device):
    """{显卡序号: 字节} → 用到的显卡序号，占得多的在前；不到 GPU_MIN_BYTES 的不算。"""
    ranked = sorted(per_device.items(), key=lambda kv: kv[1], reverse=True)
    return [index for index, v in ranked if v >= GPU_MIN_BYTES]


class GpuProcessCollector:
    name = "procs"

    def __init__(self, owners=None, luids=(), devices=None, vram_limit=None):
        """owners：可调用对象，返回 {pid: {"id", "label"}}，用来标出进程属于哪个模型服务。
        luids：只统计这些显卡（dxgi.luid_key() 的写法），一般是 GpuCollector.main_luids。
        devices：{LUID: 显卡序号}，一般是 GpuCollector.luid_index，用来标出模型服务的进程用的是哪块显卡。
        vram_limit：主显卡的显存总量合计（字节），一般是 GpuCollector.main_mem_total()。性能计数器偶尔给某个进程报出比它还大的
        显存占用（本机见过 800 多 GB），超过它的读数照实显示但标成异常（vram_bad），不计入排序、合计和在哪块显卡上的判断；None 时不检查。"""
        self._owners = owners or (lambda: {})
        self._procs = {}
        self._ncpu = psutil.cpu_count() or 1
        self._luids = {luid.lower() for luid in luids}
        self._devices = {luid.lower(): i for luid, i in (devices or {}).items()}
        self._vram_limit = vram_limit
        self._query = None
        try:
            q = PdhQuery()
            q.add("dedicated", r"\GPU Process Memory(*)\Dedicated Usage")
            q.add("shared", r"\GPU Process Memory(*)\Shared Usage")
            q.add("engine", r"\GPU Engine(*)\Utilization Percentage")
            q.collect()
            self._query = q
        except PdhError:
            pass

    def info(self):
        return {"available": self._query is not None and bool(self._luids)}

    def _per_pid(self, values):
        out = defaultdict(float)
        for inst, v in values.items():
            m = _INSTANCE.match(inst)
            if m and m.group(2).lower() in self._luids:
                out[int(m.group(1))] += v
        return out

    def _gpu_bytes(self, values, pids):
        """{pid: {显卡序号: 字节}}：这些进程在每块显卡上的显存 + 共享显存，所有显卡都算。values 是 (实例名, 值) 的序列。"""
        out = defaultdict(lambda: defaultdict(float))
        for inst, v in values:
            m = _INSTANCE.match(inst)
            if m and int(m.group(1)) in pids and m.group(2).lower() in self._devices:
                out[int(m.group(1))][self._devices[m.group(2).lower()]] += v
        return out

    def _engine_util(self, values):
        """{pid: (利用率, 引擎类型)}：每个引擎（哪块显卡、哪块物理芯片、几号引擎）单独算，取最忙的那个，
        和任务管理器的算法一致。同类型的几个引擎（比如两块显卡上的 3D 引擎）不相加。"""
        engines = defaultdict(float)
        kinds = {}
        for inst, v in values.items():
            m = _INSTANCE.match(inst)
            if m and m.group(4) is not None and m.group(2).lower() in self._luids:
                key = (int(m.group(1)), m.group(2).lower(), m.group(3), m.group(4))
                engines[key] += v
                kinds[key] = m.group(5).lower() or None
        best = {}
        for key, v in engines.items():
            if v > best.get(key[0], (0.0, None))[0]:
                best[key[0]] = (v, kinds[key])
        return best

    def _process(self, pid):
        p = self._procs.get(pid)
        if p is None or not p.is_running():
            p = psutil.Process(pid)
            p.cpu_percent()  # 第一次调用只建立基准
            self._procs[pid] = p
        return p

    def sample(self):
        if self._query is None or not self._luids:
            return None
        q = self._query
        q.collect()
        raw_dedicated, raw_shared = q.values("dedicated"), q.values("shared")
        # 比显卡总显存还大的读数是计数器的异常值：排序、合计、门槛、在哪块卡上都当作没有，表里照实显示但标出来
        limit = self._vram_limit
        clean = {inst: v for inst, v in raw_dedicated.items() if not (limit and v > limit)}
        dedicated = self._per_pid(clean)
        shown = self._per_pid(raw_dedicated)  # 表里显示的数（含异常读数）
        bad = set(self._per_pid({inst: v for inst, v in raw_dedicated.items() if inst not in clean}))  # 主显卡上有异常读数的进程
        shared = self._per_pid(raw_shared)
        engines = self._engine_util(q.values("engine"))
        owners = self._owners()

        pids = {pid for pid in set(dedicated) | set(shared) if dedicated.get(pid, 0) + shared.get(pid, 0) >= MIN_BYTES}
        pids |= set(owners)
        gpu_bytes = self._gpu_bytes([*clean.items(), *raw_shared.items()], pids)
        shared_bytes = self._gpu_bytes(raw_shared.items(), pids)
        main = {self._devices[luid] for luid in self._luids if luid in self._devices}
        rows = []
        totals = {}
        for pid in pids:
            try:
                p = self._process(pid)
                with p.oneshot():
                    name = p.name()
                    mem = p.memory_info().rss
                    cpu = p.cpu_percent() / self._ncpu
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            util, engine = engines.get(pid, (0.0, None))
            owner = owners.get(pid)
            vram = round(dedicated.get(pid, 0))  # 排序、合计用的（异常读数按 0 算）
            row = {
                "pid": pid,
                "name": name,
                "owner": owner["id"] if owner else None,
                "owner_label": owner["label"] if owner else None,
                "vram": round(shown.get(pid, 0)),
                **({"vram_bad": True} if pid in bad else {}),
                "shared": round(shared.get(pid, 0)),
                "gpu": round(min(util, 100.0), 1),
                "engine": engine,
                "mem": mem,
                "cpu": round(cpu, 1),
                # 在哪块主显卡上（显存、共享显存只算主显卡，这里也只标主显卡）
                "devices": used_devices({i: v for i, v in gpu_bytes.get(pid, {}).items() if i in main}),
            }
            if owner:
                # 每个模型服务的合计包含它的全部子进程，即使没用显卡
                t = totals.setdefault(owner["id"], {"vram": 0, "shared": 0, "gpu": 0.0, "mem": 0, "cpu": 0.0})
                for k in t:
                    t[k] += vram if k == "vram" else row[k]
            if vram + row["shared"] >= MB:
                rows.append(row)
        for pid in list(self._procs):
            if pid not in pids:
                del self._procs[pid]
        rows.sort(key=lambda r: dedicated.get(r["pid"], 0) + r["shared"], reverse=True)
        for t in totals.values():
            t["gpu"] = round(min(t["gpu"], 100.0), 1)
            t["cpu"] = round(t["cpu"], 1)
        # 模型服务用的显卡：按服务把各进程合起来，和单个进程的（Ollama 每个模型一个进程，可能在不同的显卡上）
        by_owner = defaultdict(lambda: defaultdict(float))
        for pid in owners:
            for index, v in gpu_bytes.get(pid, {}).items():
                by_owner[owners[pid]["id"]][index] += v
        for oid, t in totals.items():
            t["devices"] = used_devices(by_owner.get(oid, {}))
        devices = {pid: used_devices(gpu_bytes.get(pid, {})) for pid in owners}
        listed = rows[:MAX_ROWS] + [r for r in rows[MAX_ROWS:] if r["owner"]]
        # 列出的进程在每块主显卡上的共享显存合计：悬浮窗按显卡提示显存不够、借用了内存
        spill = defaultdict(float)
        for r in listed:
            for index, v in shared_bytes.get(r["pid"], {}).items():
                if index in main:
                    spill[index] += v
        return {
            "rows": listed,
            "owners": totals,
            "devices": {pid: d for pid, d in devices.items() if d},
            "shared": {index: round(v) for index, v in spill.items()},
        }

    def slim(self, data):
        return None

    def close(self):
        if self._query is not None:
            self._query.close()
