"""悬浮窗和看板窗口用到的 Win32 调用：显示器区域、窗口样式、圆角、托盘菜单颜色。"""

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger("aiinferpeek")
user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x2
SW_HIDE, SW_SHOWNOACTIVATE, SW_RESTORE = 0, 4, 9
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, 2
# 关机、重启、注销时系统先发 WM_QUERYENDSESSION 问每个窗口能不能结束（回 0 就是“不行”，会拦住关机），
# 都同意后再发 WM_ENDSESSION（wParam 非 0 表示真的要结束，0 表示被取消了），处理完这条进程随时会被结束
WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016

user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


MONITORINFOF_PRIMARY = 1
MONITOR_DEFAULTTONEAREST = 2
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
# GetWindowRect、SetWindowPos、GetDpiForWindow 不设 argtypes：pywebview 也用 windll.user32 调它们，传的参数类型不一样


def set_dpi_aware():
    """和 pywebview 一样把进程设成系统级 DPI 感知，提前到读显示器区域之前。

    pywebview 在 webview.start() 里才设；在那之前进程不感知 DPI，系统给的显示器区域是按 96 DPI 换算过的，
    缩放比例不一样的副屏还会换算得各不相同，和 pywebview 之后用的坐标对不上。设好以后，这里读到的区域、
    窗口位置和 pywebview 用的是同一套（各屏缩放比例一样时就是物理像素），除以 system_scale() 就是
    pywebview 的逻辑像素。已经设过时调用没有影响。
    """
    user32.SetProcessDPIAware()


def system_scale():
    """缩放比例：系统 DPI / 96。pywebview 的逻辑像素乘以它是物理像素（系统级 DPI 感知时，每个窗口都按系统 DPI 算）。"""
    try:
        return user32.GetDpiForSystem() / 96
    except AttributeError:
        return 1  # Windows 10 1607 之前没有


def list_work_areas():
    """所有显示器去掉任务栏后的区域 (左, 上, 右, 下)，主屏在最前。

    不用 webview.screens：它会提前初始化 pywebview 的界面后端，之后 webview.start(gui=...) 就不起作用了。
    """
    monitors = []

    def callback(hmon, hdc, rect, data):
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcWork
            monitors.append(((r.left, r.top, r.right, r.bottom), bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    if not monitors:
        monitors.append(((0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)), True))
    monitors.sort(key=lambda m: not m[1])
    return [area for area, _ in monitors]


def work_area(x, y):
    """点 (x, y) 所在（不在任何屏幕上就取最近）的显示器去掉任务栏后的区域：(左, 上, 右, 下)。"""
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    hmon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return list_work_areas()[0]


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def set_ex_style(hwnd, add=0, remove=0):
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    new = (style | add) & ~remove
    if new != style:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new)


def apply_window_style(hwnd, opacity, click_through):
    """透明度和鼠标穿透都需要分层窗口；两者都关掉时去掉分层样式，避免影响渲染。"""
    layered = opacity < 100 or click_through
    set_ex_style(
        hwnd,
        add=(WS_EX_LAYERED if layered else 0) | (WS_EX_TRANSPARENT if click_through else 0),
        remove=(0 if layered else WS_EX_LAYERED) | (0 if click_through else WS_EX_TRANSPARENT),
    )
    if layered:
        user32.SetLayeredWindowAttributes(hwnd, 0, round(255 * opacity / 100), LWA_ALPHA)


def hide_from_taskbar(hwnd):
    # 工具窗口不出现在任务栏和 Alt+Tab 里；样式要在窗口重新显示后才生效
    visible = user32.IsWindowVisible(hwnd)
    set_ex_style(hwnd, add=WS_EX_TOOLWINDOW, remove=WS_EX_APPWINDOW)
    if visible:
        user32.ShowWindow(hwnd, SW_HIDE)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def bring_to_front(hwnd):
    # 最小化了先还原（最小化前是最大化的，还原成最大化）；托盘菜单、悬浮窗按钮刚被点过，系统允许把窗口切到前面
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def round_corners(hwnd):
    try:
        value = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), 4)
    except OSError:
        pass  # Windows 10 没有圆角设置


# 托盘菜单是系统原生菜单，默认总是浅色。uxtheme 里没公开的 SetPreferredAppMode（序号 135）能换颜色，
# Windows 10 1903 起才有；1：跟随系统，2：深色，3：浅色
MENU_MODES = {"system": 1, "dark": 2, "light": 3}


def set_menu_theme(theme):
    if sys.getwindowsversion().build < 18362:
        return
    try:
        uxtheme = ctypes.windll.uxtheme
        uxtheme[135](MENU_MODES[theme])
        uxtheme[136]()  # FlushMenuThemes：之后弹出的菜单马上用新颜色
    except (AttributeError, OSError):
        log.warning("设置托盘菜单颜色失败", exc_info=True)
