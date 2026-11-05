"""用 DXGI 列出显卡适配器及其 LUID，用 DXCore 补上是不是核显、驱动版本。

性能计数器里的显存、GPU 引擎实例名都带 luid_0x…_0x…，
靠它才能区分独显和核显上的占用。
"""

import ctypes
from ctypes import wintypes

NVIDIA_VENDOR_ID = 0x10DE
VENDORS = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x1022: "AMD", 0x8086: "Intel", 0x5143: "Qualcomm"}
_ADAPTER_FLAG_SOFTWARE = 2


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _AdapterDesc1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(d1, d2, d3, *d4):
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


_IID_IDXGIFactory1 = _guid(0x770AAE78, 0xF26F, 0x4DBA, 0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
_IID_IDXCoreAdapterFactory = _guid(0x78EE5945, 0xC36E, 0x4B13, 0xA6, 0x69, 0x00, 0x5D, 0xD1, 0x1C, 0x0F, 0x06)
_IID_IDXCoreAdapter = _guid(0xF0DB4C7F, 0xFE5A, 0x42A2, 0xBD, 0x62, 0xF2, 0xA6, 0xCF, 0x6F, 0xC8, 0x3E)

# COM 虚函数表序号：IDXGIFactory1::EnumAdapters1 = 12，IDXGIAdapter1::GetDesc1 = 10，IUnknown::Release = 2
_ENUM_ADAPTERS1, _GET_DESC1, _RELEASE = 12, 10, 2
# IDXCoreAdapterFactory::GetAdapterByLuid = 4，IDXCoreAdapter::GetProperty = 6；DXCoreAdapterProperty 的 DriverVersion、IsIntegrated
_GET_ADAPTER_BY_LUID, _GET_PROPERTY = 4, 6
_PROP_DRIVER_VERSION, _PROP_IS_INTEGRATED = 1, 12


def _method(obj, index, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def luid_key(high, low):
    """和性能计数器实例名里的写法一致：0x00000000_0x00018962（小写）。"""
    return f"0x{high & 0xFFFFFFFF:08x}_0x{low & 0xFFFFFFFF:08x}"


def list_adapters():
    """返回 [{name, vendor_id, device_id, luid, dedicated, shared, integrated, driver}]；不是 Windows 或调用失败时返回空列表。

    shared 是能借用的内存上限。integrated（是不是核显）和 driver（驱动版本）来自 DXCore，读不到时是 None。
    """
    try:
        dxgi = ctypes.WinDLL("dxgi.dll")
    except (OSError, AttributeError):
        return []
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
        return []
    adapters, luids = [], []
    try:
        enum = _method(factory, _ENUM_ADAPTERS1, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum(factory, i, ctypes.byref(adapter)) != 0:
                break
            try:
                desc = _AdapterDesc1()
                get_desc = _method(adapter, _GET_DESC1, ctypes.c_long, ctypes.POINTER(_AdapterDesc1))
                if get_desc(adapter, ctypes.byref(desc)) == 0 and not desc.Flags & _ADAPTER_FLAG_SOFTWARE:
                    adapters.append({
                        "name": desc.Description,
                        "vendor_id": desc.VendorId,
                        "device_id": desc.DeviceId,
                        "luid": luid_key(desc.AdapterLuid.HighPart, desc.AdapterLuid.LowPart),
                        "dedicated": desc.DedicatedVideoMemory,
                        "shared": desc.SharedSystemMemory,
                    })
                    luids.append(_LUID(desc.AdapterLuid.LowPart, desc.AdapterLuid.HighPart))
            finally:
                _method(adapter, _RELEASE, wintypes.ULONG)(adapter)
            i += 1
    finally:
        _method(factory, _RELEASE, wintypes.ULONG)(factory)
    traits = _dxcore_traits(luids)
    for a, t in zip(adapters, traits):
        a["integrated"], a["driver"] = t
    return adapters


def _dxcore_traits(luids):
    """每个 LUID 的 (是不是核显, 驱动版本)。DXCore 从 Windows 10 2004 起才有；
    它不认的适配器（比如虚拟显示器借用显卡名字列出来的那些）两项都是 None。"""
    out = [(None, None)] * len(luids)
    try:
        dxcore = ctypes.WinDLL("dxcore.dll")
    except (OSError, AttributeError):
        return out
    factory = ctypes.c_void_p()
    if dxcore.DXCoreCreateAdapterFactory(ctypes.byref(_IID_IDXCoreAdapterFactory), ctypes.byref(factory)) != 0:
        return out
    try:
        get_adapter = _method(
            factory, _GET_ADAPTER_BY_LUID, ctypes.c_long, ctypes.POINTER(_LUID), ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
        )
        for i, luid in enumerate(luids):
            adapter = ctypes.c_void_p()
            if get_adapter(factory, ctypes.byref(luid), ctypes.byref(_IID_IDXCoreAdapter), ctypes.byref(adapter)) != 0:
                continue
            try:
                get_prop = _method(adapter, _GET_PROPERTY, ctypes.c_long, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p)
                integrated, version = ctypes.c_bool(), ctypes.c_uint64()
                ok = get_prop(adapter, _PROP_IS_INTEGRATED, ctypes.sizeof(integrated), ctypes.byref(integrated)) == 0
                has_version = get_prop(adapter, _PROP_DRIVER_VERSION, ctypes.sizeof(version), ctypes.byref(version)) == 0
                # 驱动版本是 4 个 16 位数：32.0.101.6913
                driver = ".".join(str(version.value >> s & 0xFFFF) for s in (48, 32, 16, 0)) if has_version and version.value else None
                out[i] = (integrated.value if ok else None, driver)
            finally:
                _method(adapter, _RELEASE, wintypes.ULONG)(adapter)
    finally:
        _method(factory, _RELEASE, wintypes.ULONG)(factory)
    return out
