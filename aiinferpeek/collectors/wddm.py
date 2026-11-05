"""不靠厂商 SDK 的通用显卡采集：AMD、Intel 的独显和核显，以及 NVML 读不了的 NVIDIA。

- 名字、专用显存总量、是不是核显、驱动版本：DXGI / DXCore（dxgi.py）
- 显存已用、共享显存：性能计数器 \\GPU Adapter Memory(*)，按 LUID
- 利用率：性能计数器 \\GPU Engine(*)，按 LUID 汇总，口径和任务管理器一致
- 温度、核心频率、显存频率、风扇转速：D3DKMT（d3dkmt.py），驱动不填的项是 None
- 功耗：D3DKMT 只给占额定功率的比例、不给瓦数，是 None
"""

import re
from collections import defaultdict

from .d3dkmt import Adapter
from .dxgi import NVIDIA_VENDOR_ID, VENDORS
from .pdh import PdhError, PdhQuery

MB = 1024 * 1024
# 读不到 DXCore 时按专用显存判断：不到 1 GB 算核显（Intel 核显是 128 MB）
INTEGRATED_MAX_DEDICATED = 1024 * MB

# \GPU Adapter Memory 的实例名：luid_0x00000000_0x00018C24_phys_0（十六进制是大写）
_ADAPTER_INSTANCE = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+$", re.I)
# \GPU Engine 的实例名：pid_1234_luid_0x…_0x…_phys_0_eng_3_engtype_Copy
_ENGINE_INSTANCE = re.compile(r"_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_", re.I)


def per_luid(values):
    """\\GPU Adapter Memory 的 {实例名: 字节} → {luid: 字节}，多块物理芯片（phys_0、phys_1…）相加。"""
    out = defaultdict(float)
    for inst, v in values.items():
        m = _ADAPTER_INSTANCE.match(inst)
        if m:
            out[m.group(1).lower()] += v
    return dict(out)


def engine_util(values):
    """\\GPU Engine 的 {实例名: %} → {luid: 利用率}。

    同一个引擎的各进程相加，再取这块显卡各引擎里最高的，和任务管理器一致；超过 100 的按 100。
    """
    engines = defaultdict(float)
    for inst, v in values.items():
        m = _ENGINE_INSTANCE.search(inst)
        if m:
            engines[(m.group(1).lower(), m.group(2), m.group(3))] += v
    out = {}
    for (luid, _, _), v in engines.items():
        out[luid] = max(out.get(luid, 0.0), min(v, 100.0))
    return out


def is_integrated(adapter):
    """是不是核显：优先用 DXCore 的判断，读不到时看专用显存大小。"""
    if adapter.get("integrated") is not None:
        return adapter["integrated"]
    return adapter["dedicated"] < INTEGRATED_MAX_DEDICATED


def pick_adapters(adapters, counter_luids):
    """DXGI 列出的适配器里挑出真正的硬件显卡，独显在前、核显在后。

    DXGI 会把虚拟显示器（间接显示适配器）也列出来，名字、设备 ID、专用显存都和它借用的显卡一样，
    同一块核显可能出现好几次。去掉不能渲染的（render 是 False）；性能计数器可用时（counter_luids 不为空），
    再去掉计数器里没有的 LUID，读不到数据的适配器列出来也没用。
    """
    out = [a for a in adapters if a.get("render") is not False and (not counter_luids or a["luid"] in counter_luids)]
    return sorted(out, key=is_integrated)


def main_luids(adapters):
    """“主显卡”的 LUID：有独显时是所有独显，否则是核显。按进程的显存只统计它们，不混进核显上浏览器、桌面占的。"""
    discrete = [a for a in adapters if not is_integrated(a)]
    return {a["luid"] for a in discrete or adapters}


def match_nvml(pci, adapters):
    """NVML 读的每块显卡对应哪个 LUID（对不上的是 None）。pci：每块的 (总线, 设备号)，读不到是 None；
    adapters：挑出来的适配器，带 D3DKMT 查到的 address。按 PCI 位置对；对不上时，只有一块 NVIDIA 显卡就是它。"""
    nvidia = [a for a in adapters if a["vendor_id"] == NVIDIA_VENDOR_ID]
    by_address = {a["address"][:2]: a["luid"] for a in nvidia if a.get("address")}
    out = [by_address.get(p) if p else None for p in pci]
    if len(out) == 1 and len(nvidia) == 1 and out[0] is None:
        out[0] = nvidia[0]["luid"]
    return out


def memory_view(adapter, dedicated, shared):
    """(已用, 总量) 字节。独显是专用显存；核显没有像样的专用显存（Intel 只有 128 MB），
    算专用 + 共享，和任务管理器里的“GPU 内存”一致。读不到已用时是 None。"""
    if not is_integrated(adapter):
        return (round(dedicated) if dedicated is not None else None), adapter["dedicated"] or None
    used = round(dedicated + shared) if dedicated is not None and shared is not None else None
    return used, (adapter["dedicated"] + adapter["shared"]) or None


class WddmGpus:
    """一组用通用方式采集的显卡。不是独立的采集器，由 gpu.GpuCollector 合进 gpus 列表。"""

    def __init__(self, adapters, skip_nvidia=False):
        """adapters：dxgi.list_adapters() 的结果；skip_nvidia：NVIDIA 显卡已经由 NVML 读了，这里跳过。"""
        self._query = None
        counter_luids = set()
        try:
            q = PdhQuery()
            q.add("dedicated", r"\GPU Adapter Memory(*)\Dedicated Usage")
            q.add("shared", r"\GPU Adapter Memory(*)\Shared Usage")
            q.add("engine", r"\GPU Engine(*)\Utilization Percentage")
            q.collect()  # 利用率是速率类计数器，第一次只建立基准
            counter_luids = set(per_luid(q.values("dedicated")))
            self._query = q
        except PdhError:
            pass
        handles = {}
        for a in adapters:
            h = Adapter.open(a["luid"])
            a["render"] = h.can_render() if h else None
            a["address"] = h.address() if h else None
            handles[a["luid"]] = h
        picked = pick_adapters(adapters, counter_luids)
        # 挑出来的全部硬件显卡（包括交给 NVML 读的），用来给 NVML 的显卡找 LUID
        self.adapters = picked
        self.main_luids = main_luids(picked)
        self._gpus = []
        for a in picked:
            if not (skip_nvidia and a["vendor_id"] == NVIDIA_VENDOR_ID):
                self._gpus.append((a, handles.pop(a["luid"])))
        for h in handles.values():
            if h:
                h.close()
        self.luids = [a["luid"] for a, _ in self._gpus]

    def info(self):
        out = []
        for a, h in self._gpus:
            _, clock_max = h.clock() if h else (None, None)
            out.append({
                "name": a["name"],
                "vendor": VENDORS.get(a["vendor_id"]),
                "integrated": is_integrated(a),
                "driver": a.get("driver"),
                "mem_total": memory_view(a, None, None)[1],
                "power_limit": None,
                "clock_max": clock_max,
            })
        return out

    def sample(self):
        if not self._gpus:
            return []
        dedicated = shared = util = None
        if self._query is not None:
            q = self._query
            try:
                q.collect()
                dedicated, shared, util = per_luid(q.values("dedicated")), per_luid(q.values("shared")), engine_util(q.values("engine"))
            except PdhError:
                pass
        out = []
        for a, h in self._gpus:
            luid = a["luid"]
            mem_used, mem_total = memory_view(a, dedicated.get(luid) if dedicated else None, shared.get(luid) if shared else None)
            perf = h.perf() if h else {}
            clock, _ = h.clock() if h else (None, None)
            out.append({
                # 没有进程用过的引擎没有计数器实例，这时利用率是 0
                "util": round(util.get(luid, 0.0)) if util is not None else None,
                "mem_used": mem_used,
                "mem_total": mem_total,
                "temp": perf.get("temp"),
                "power": None,
                "clock_gr": clock,
                "clock_mem": perf.get("clock_mem"),
                "fan": None,
                "fan_rpm": perf.get("fan_rpm"),
                "pstate": None,
            })
        return out

    def close(self):
        for _, h in self._gpus:
            if h:
                h.close()
        if self._query is not None:
            self._query.close()
