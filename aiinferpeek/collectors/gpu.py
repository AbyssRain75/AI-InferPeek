"""显卡：NVIDIA 用 NVML（数据最全），其余（AMD、Intel 的独显和核显，NVML 读不了的 NVIDIA）用通用方式（wddm.py）。

笔记本显卡常常不支持读取风扇转速等项目，读不到的字段返回 None。
"""

import pynvml

from ..i18n import tr
from .dxgi import list_adapters
from .wddm import WddmGpus, match_nvml


def _try(fn, *args):
    try:
        return fn(*args)
    except pynvml.NVMLError:
        return None


class NvidiaGpus:
    """用 NVML 读的 NVIDIA 显卡。不是独立的采集器，由 GpuCollector 合进 gpus 列表。"""

    def __init__(self):
        self._handles = []
        try:
            pynvml.nvmlInit()
            self._handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(pynvml.nvmlDeviceGetCount())]
        except pynvml.NVMLError:
            pass
        # 每块的 PCI (总线, 设备号)，用来对上 DXGI 里的 LUID
        pci = [_try(pynvml.nvmlDeviceGetPciInfo, h) for h in self._handles]
        self.pci = [(p.bus, p.device) if p else None for p in pci]

    def __len__(self):
        return len(self._handles)

    def info(self):
        driver = _try(pynvml.nvmlSystemGetDriverVersion) if self._handles else None
        gpus = []
        for i, h in enumerate(self._handles):
            mem = _try(pynvml.nvmlDeviceGetMemoryInfo, h)
            limit = _try(pynvml.nvmlDeviceGetEnforcedPowerLimit, h)
            gpus.append({
                "name": _try(pynvml.nvmlDeviceGetName, h) or f"GPU {i}",
                "vendor": "NVIDIA",
                "integrated": False,
                "driver": driver,
                "mem_total": mem.total if mem else None,
                "power_limit": round(limit / 1000, 1) if limit else None,
                "clock_max": _try(pynvml.nvmlDeviceGetMaxClockInfo, h, pynvml.NVML_CLOCK_GRAPHICS),
            })
        return gpus

    def sample(self):
        out = []
        for h in self._handles:
            util = _try(pynvml.nvmlDeviceGetUtilizationRates, h)
            mem = _try(pynvml.nvmlDeviceGetMemoryInfo, h)
            power = _try(pynvml.nvmlDeviceGetPowerUsage, h)
            pstate = _try(pynvml.nvmlDeviceGetPerformanceState, h)
            out.append({
                "util": util.gpu if util else None,
                "mem_used": mem.used if mem else None,
                "mem_total": mem.total if mem else None,
                "temp": _try(pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU),
                "power": round(power / 1000, 1) if power is not None else None,
                "clock_gr": _try(pynvml.nvmlDeviceGetClockInfo, h, pynvml.NVML_CLOCK_GRAPHICS),
                "clock_mem": _try(pynvml.nvmlDeviceGetClockInfo, h, pynvml.NVML_CLOCK_MEM),
                "fan": _try(pynvml.nvmlDeviceGetFanSpeed, h),
                "pstate": f"P{pstate}" if pstate is not None else None,
            })
        return out

    def close(self):
        if self._handles:
            _try(pynvml.nvmlShutdown)


class GpuCollector:
    """所有硬件显卡，独显在前、核显在后（NVML 读的 NVIDIA 显卡排最前）。

    每块显卡的字段：util（%）、mem_used / mem_total（字节；核显是专用 + 共享，见 wddm.memory_view）、
    temp（°C）、power（W）、clock_gr / clock_mem（MHz）、fan（%）、fan_rpm（转/分，通用方式才有）、pstate。
    main_luids 是“主显卡”的 LUID（有独显时是所有独显，否则是核显），交给按进程的采集用；
    luid_index 是 {LUID: 这块显卡在列表里的序号}，按进程的采集用它标出模型服务的进程用的是哪块显卡。
    """

    name = "gpus"

    def __init__(self):
        self._nvidia = NvidiaGpus()
        self._wddm = WddmGpus(list_adapters(), skip_nvidia=len(self._nvidia) > 0)
        self.main_luids = self._wddm.main_luids
        luids = match_nvml(self._nvidia.pci, self._wddm.adapters) + self._wddm.luids
        self.luid_index = {luid: i for i, luid in enumerate(luids) if luid}

    def info(self):
        devices = self._nvidia.info() + self._wddm.info()
        for i, d in enumerate(devices):
            d["index"] = i
        return {"devices": devices, "error": None if devices else tr("未检测到显卡")}

    def main_mem_total(self):
        """主显卡的显存总量合计（字节）：有独显时是所有独显，否则是核显的专用 + 共享。
        按进程的采集用它识别计数器的异常读数（GpuProcessCollector 的 vram_limit）；读不到时是 None。"""
        indexes = {i for luid, i in self.luid_index.items() if luid in self.main_luids}
        totals = [d["mem_total"] for d in self.info()["devices"] if d["index"] in indexes and d["mem_total"]]
        return sum(totals) if totals else None

    def sample(self):
        return self._nvidia.sample() + self._wddm.sample()

    def close(self):
        self._nvidia.close()
        self._wddm.close()
