"""Windows 性能计数器（PDH）的最小封装。

用英文计数器路径（PdhAddEnglishCounterW），在中文等本地化系统上也能用。
任务管理器里的 CPU 占用、CPU 速度、每个进程的显存都来自这些计数器。
"""

import ctypes
import sys
from ctypes import wintypes

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x00000000
PDH_CSTATUS_NEW_DATA = 0x00000001


class PdhError(OSError):
    pass


class _FmtValue(ctypes.Structure):
    # PDH_FMT_COUNTERVALUE：DWORD + 联合体，这里只用 double 成员
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _FmtValueItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FmtValue)]


_pdh = None


def _lib():
    global _pdh
    if _pdh is None:
        if sys.platform != "win32":
            raise PdhError("PDH 只在 Windows 上可用")
        lib = ctypes.WinDLL("pdh.dll")
        for name in (
            "PdhOpenQueryW",
            "PdhAddEnglishCounterW",
            "PdhCollectQueryData",
            "PdhGetFormattedCounterValue",
            "PdhGetFormattedCounterArrayW",
            "PdhCloseQuery",
        ):
            getattr(lib, name).restype = wintypes.DWORD
        _pdh = lib
    return _pdh


def _check(status, what):
    if status != 0:
        raise PdhError(f"{what} 失败，PDH 状态码 0x{status:08X}")


class PdhQuery:
    """一组计数器，每次 collect() 后读取。速率类计数器需要两次 collect 才有值。"""

    def __init__(self):
        self._lib = _lib()
        self._handle = wintypes.HANDLE()
        _check(self._lib.PdhOpenQueryW(None, 0, ctypes.byref(self._handle)), "PdhOpenQuery")
        self._counters = {}

    def add(self, key, path):
        counter = wintypes.HANDLE()
        _check(
            self._lib.PdhAddEnglishCounterW(self._handle, path, 0, ctypes.byref(counter)),
            f"添加计数器 {path}",
        )
        self._counters[key] = counter

    def collect(self):
        _check(self._lib.PdhCollectQueryData(self._handle), "PdhCollectQueryData")

    def value(self, key, nocap=False):
        """单实例计数器的当前值；没有有效数据时返回 None。"""
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap else 0)
        out = _FmtValue()
        status = self._lib.PdhGetFormattedCounterValue(self._counters[key], fmt, None, ctypes.byref(out))
        if status != 0 or out.CStatus not in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
            return None
        return out.doubleValue

    def values(self, key, nocap=False):
        """通配符计数器（路径里带 *）的 {实例名: 值}，跳过无效实例。"""
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap else 0)
        counter = self._counters[key]
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        status = self._lib.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if status != PDH_MORE_DATA:
            return {}
        buf = (ctypes.c_byte * size.value)()
        status = self._lib.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if status != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_FmtValueItem))
        result = {}
        for i in range(count.value):
            item = items[i]
            if item.FmtValue.CStatus in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
                result[item.szName] = item.FmtValue.doubleValue
        return result

    def close(self):
        if self._handle:
            self._lib.PdhCloseQuery(self._handle)
            self._handle = wintypes.HANDLE()
