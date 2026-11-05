"""CPU 与内存。

CPU 占用和速度优先用性能计数器，和任务管理器显示的口径一致；
计数器不可用时退回 psutil。
CPU 功耗用性能计数器 Energy Meter 里的 RAPL 封装功耗（不用管理员权限），没有这个计数器时用 LibreHardwareMonitor 的；
CPU 温度 Windows 没有接口，从 LibreHardwareMonitor 读（见 lhm.py）。
"""

import platform
import socket

import psutil

from .lhm import LhmReader
from .pdh import PdhError, PdhQuery


def _cpu_name():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except OSError:
        return platform.processor() or "未知 CPU"


def rapl_power(values):
    """Energy Meter 各实例的功率（毫瓦）里整颗 CPU 的（_PKG，几颗 CPU 相加），单位瓦；没有时是 None。"""
    pkg = [v for k, v in values.items() if k.upper().endswith("_PKG")]
    return round(sum(pkg) / 1000, 1) if pkg else None


def _core_sort_key(instance):
    # 实例名形如 "0,13"（处理器组,序号）
    group, _, index = instance.partition(",")
    return int(group), int(index)


class CpuCollector:
    name = "cpu"

    def __init__(self, lhm=None):
        self._query = None
        self._rapl = False
        try:
            query = PdhQuery()
            query.add("util", r"\Processor Information(*)\% Processor Utility")
            query.add("perf", r"\Processor Information(_Total)\% Processor Performance")
            query.add("base", r"\Processor Information(_Total)\Processor Frequency")
            try:
                # 实例是 RAPL_Package0_PKG（整颗 CPU）、_PP0（核心）、_PP1（核显）、_DRAM，单位毫瓦
                query.add("power", r"\Energy Meter(*)\Power")
                self._rapl = True
            except PdhError:
                pass  # 没有能量计（老系统、部分 AMD），功耗用 LibreHardwareMonitor 的
            query.collect()
            self._query = query
        except PdhError:
            psutil.cpu_percent(percpu=True)
        self._lhm = lhm if lhm is not None else LhmReader()

    def info(self):
        return {
            "name": _cpu_name(),
            "cores": psutil.cpu_count(logical=False),
            "threads": psutil.cpu_count(logical=True),
            "host": socket.gethostname(),
        }

    def sample(self):
        data = self._sample_pdh() if self._query is not None else self._sample_psutil()
        temp, power, note = self._lhm.latest()
        if data.get("power") is None and power is not None:
            data["power"] = round(power, 1)
        data["temp"] = round(temp, 1) if temp is not None else None
        data["temp_note"] = note  # 读不到温度的原因，读到了是 None
        return data

    def slim(self, data):
        # 原因只在最新一条里用，历史里不存
        return {k: v for k, v in data.items() if k != "temp_note"}

    def _sample_psutil(self):
        per_core = psutil.cpu_percent(percpu=True)
        freq = psutil.cpu_freq()
        return {
            "percent": round(sum(per_core) / len(per_core), 1),
            "per_core": [round(v, 1) for v in per_core],
            "freq_mhz": round(freq.current) if freq else None,
        }

    def _sample_pdh(self):
        q = self._query
        q.collect()
        util = q.values("util", nocap=True)
        cores = sorted((k for k in util if "_Total" not in k), key=_core_sort_key)
        per_core = [round(min(util[k], 100.0), 1) for k in cores]
        total = util.get("_Total")
        if total is None and per_core:
            total = sum(per_core) / len(per_core)
        perf, base = q.value("perf", nocap=True), q.value("base")
        return {
            "percent": round(min(total, 100.0), 1) if total is not None else None,
            "per_core": per_core,
            "freq_mhz": round(base * perf / 100) if perf and base else None,
            "power": rapl_power(q.values("power")) if self._rapl else None,
        }

    def close(self):
        self._lhm.close()
        if self._query is not None:
            self._query.close()


class MemoryCollector:
    name = "mem"

    def info(self):
        return {"total": psutil.virtual_memory().total}

    def sample(self):
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "total": vm.total,
            "used": vm.total - vm.available,
            "available": vm.available,
            "percent": vm.percent,
            "swap_total": swap.total,
            "swap_used": swap.used,
        }

    def close(self):
        pass
