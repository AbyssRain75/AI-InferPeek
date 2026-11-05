"""D3DKMT（gdi32.dll 导出的显卡内核接口）：适配器类型，以及温度、频率、风扇。

任务管理器“性能”页里显卡的温度就来自这里（KMTQAITYPE_ADAPTERPERFDATA）。
各项由驱动自己填，不填的是 0：Intel 核显一项都没有，NVIDIA 有温度和频率。读不到的返回 None。
"""

import ctypes
from ctypes import wintypes

# KMTQUERYADAPTERINFOTYPE
_ADAPTERADDRESS, _ADAPTERTYPE, _NODEPERFDATA, _ADAPTERPERFDATA, _ADAPTERPERFDATA_CAPS = 6, 15, 61, 62, 63
# D3DKMT_ADAPTERTYPE 的第 0 位：能渲染。虚拟显示器之类只管显示的适配器没有这一位
_RENDER_SUPPORTED = 1
MHZ = 1000 * 1000


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _OpenAdapterFromLuid(ctypes.Structure):
    _fields_ = [("AdapterLuid", _LUID), ("hAdapter", wintypes.UINT)]


class _CloseAdapter(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT)]


class _QueryAdapterInfo(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", ctypes.c_int), ("pPrivateDriverData", ctypes.c_void_p), ("PrivateDriverDataSize", wintypes.UINT)]


class _AdapterAddress(ctypes.Structure):
    _fields_ = [("BusNumber", wintypes.UINT), ("DeviceNumber", wintypes.UINT), ("FunctionNumber", wintypes.UINT)]


class _AdapterPerfData(ctypes.Structure):
    _fields_ = [
        ("PhysicalAdapterIndex", ctypes.c_uint32),
        ("MemoryFrequency", ctypes.c_uint64),  # Hz
        ("MaxMemoryFrequency", ctypes.c_uint64),
        ("MaxMemoryFrequencyOC", ctypes.c_uint64),
        ("MemoryBandwidth", ctypes.c_uint64),
        ("PCIEBandwidth", ctypes.c_uint64),
        ("FanRPM", wintypes.ULONG),
        ("Power", wintypes.ULONG),  # 千分之几（占额定功率的比例），不是瓦数
        ("Temperature", wintypes.ULONG),  # 0.1 °C
        ("PowerStateOverride", ctypes.c_ubyte),
    ]


class _AdapterPerfDataCaps(ctypes.Structure):
    _fields_ = [
        ("PhysicalAdapterIndex", ctypes.c_uint32),
        ("MaxMemoryBandwidth", ctypes.c_uint64),
        ("MaxPCIEBandwidth", ctypes.c_uint64),
        ("MaxFanRPM", wintypes.ULONG),
        ("TemperatureMax", wintypes.ULONG),
        ("TemperatureWarning", wintypes.ULONG),
    ]


class _NodePerfData(ctypes.Structure):
    _fields_ = [
        ("NodeOrdinal", ctypes.c_uint32),
        ("PhysicalAdapterIndex", ctypes.c_uint32),
        ("Frequency", ctypes.c_uint64),  # Hz
        ("MaxFrequency", ctypes.c_uint64),
        ("MaxFrequencyOC", ctypes.c_uint64),
        ("Voltage", wintypes.ULONG),
        ("VoltageMax", wintypes.ULONG),
        ("VoltageMaxOC", wintypes.ULONG),
        ("MaxTransitionLatency", ctypes.c_uint64),
    ]


_gdi = None


def _lib():
    global _gdi
    if _gdi is None:
        _gdi = ctypes.WinDLL("gdi32.dll")
    return _gdi


def _mhz(hz):
    return round(hz / MHZ) if hz else None


class Adapter:
    """按 LUID 打开的适配器，用完 close()。"""

    def __init__(self, handle):
        self._handle = handle
        self._has_fan = None

    @classmethod
    def open(cls, luid):
        """luid 是 dxgi.luid_key() 的写法；打不开（不是 Windows、适配器没了）返回 None。"""
        try:
            lib = _lib()
            high, low = (int(x, 16) for x in luid.split("_"))
            req = _OpenAdapterFromLuid(_LUID(low, ctypes.c_long(high).value))
            if lib.D3DKMTOpenAdapterFromLuid(ctypes.byref(req)) != 0:
                return None
        except (OSError, AttributeError, ValueError):
            return None
        return cls(req.hAdapter)

    def _query(self, kind, data):
        if not self._handle:
            return False
        req = _QueryAdapterInfo(self._handle, kind, ctypes.cast(ctypes.byref(data), ctypes.c_void_p), ctypes.sizeof(data))
        return _lib().D3DKMTQueryAdapterInfo(ctypes.byref(req)) == 0

    def can_render(self):
        """能不能渲染；查不到返回 None。"""
        kind = wintypes.UINT()
        return bool(kind.value & _RENDER_SUPPORTED) if self._query(_ADAPTERTYPE, kind) else None

    def address(self):
        """PCI 位置 (总线, 设备, 功能)；虚拟适配器（全是 0xFFFFFFFF）、查不到时返回 None。"""
        data = _AdapterAddress()
        if not self._query(_ADAPTERADDRESS, data) or data.BusNumber == 0xFFFFFFFF:
            return None
        return data.BusNumber, data.DeviceNumber, data.FunctionNumber

    def perf(self):
        """{temp（°C）, clock_mem（MHz）, fan_rpm}，驱动没填的项是 None。"""
        if self._has_fan is None:
            # 风扇停转（很多显卡空闲时停）也是 0：驱动报了最高转速才把 0 当真
            caps = _AdapterPerfDataCaps()
            self._has_fan = self._query(_ADAPTERPERFDATA_CAPS, caps) and caps.MaxFanRPM > 0
        data = _AdapterPerfData()
        if not self._query(_ADAPTERPERFDATA, data):
            return {"temp": None, "clock_mem": None, "fan_rpm": None}
        return {
            "temp": round(data.Temperature / 10) if data.Temperature else None,
            "clock_mem": _mhz(data.MemoryFrequency),
            "fan_rpm": data.FanRPM if data.FanRPM or self._has_fan else None,
        }

    def clock(self, node=0):
        """引擎（节点 0 一般是 3D）的 (当前, 最高) 频率（MHz）。"""
        data = _NodePerfData(node, 0)
        if not self._query(_NODEPERFDATA, data):
            return None, None
        return _mhz(data.Frequency), _mhz(data.MaxFrequency)

    def close(self):
        if self._handle:
            _lib().D3DKMTCloseAdapter(ctypes.byref(_CloseAdapter(self._handle)))
            self._handle = None
