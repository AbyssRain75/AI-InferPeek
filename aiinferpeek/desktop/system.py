"""和 Windows 系统打交道的部分：数据放在哪、只运行一个、检测 WebView2、深色模式、消息框、开机自动启动、从改名前的版本升级。"""

import ctypes
import logging
import os
import platform
import shutil
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

from ..config import FROZEN
from .win32 import user32

APP_NAME = "AI InferPeek"
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "AIInferPeek"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = Path(os.environ.get("LOCALAPPDATA") or DATA_DIR) / "AIInferPeek" / "aiinferpeek.log"
LOG_MAX = 2 * 1024 * 1024  # 日志超过这么大时旧的改名成 aiinferpeek.old.log，见 logs.LogFile
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AIInferPeek"
# Windows 设置里“启动应用”（任务管理器的“启动应用”页也一样）的开关记在这里：关掉时不删 Run 里的值，只在这里记一笔
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
# 改名前（AI Board）用的名字，升级时把旧的设置搬过来，见 migrate_old_name()
OLD_DATA_DIR = DATA_DIR.with_name("AIBoard")
OLD_MUTEX = "Local\\AIBoardDesktop"
OLD_RUN_VALUE = "AIBoard"

log = logging.getLogger("aiinferpeek")


def system_dark():
    """Windows 的“应用模式”是不是深色。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


# WebView2 运行时在注册表里的登记：正式版、Beta、Dev、Canary
WEBVIEW2_CLIENTS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",
)
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"


def webview2_version():
    """WebView2 运行时的版本，没装返回 None。

    判断方法和 pywebview 一样（.NET 4.6.2 以上、注册表里有 86 以上的版本）：它判断没有时会悄悄改用 IE 内核，
    悬浮窗页面在 IE 里跑不起来，只会白屏。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            if winreg.QueryValueEx(key, "Release")[0] < 394802:
                return None
    except OSError:
        return None
    machine_path = r"SOFTWARE\Microsoft\EdgeUpdate\Clients" if platform.machine() == "x86" else r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    places = [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"), (winreg.HKEY_LOCAL_MACHINE, machine_path)]
    for client in WEBVIEW2_CLIENTS:
        for root, path in places:
            try:
                with winreg.OpenKey(root, rf"{path}\{client}") as key:
                    version = str(winreg.QueryValueEx(key, "pv")[0])
            except OSError:
                continue
            try:
                if int(version.split(".")[0]) >= 86:
                    return version
            except ValueError:
                continue
    return None


MB_YESNO, MB_ICONWARNING, MB_ICONINFORMATION, IDYES = 0x4, 0x30, 0x40, 6


def message_box(text, flags=MB_ICONINFORMATION, owner=None):
    return user32.MessageBoxW(owner, text, APP_NAME, flags)


def single_instance():
    """用命名互斥量保证只运行一个；返回 None 表示已经有一个在运行。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\AIInferPeekDesktop")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def old_version_running():
    """改名前的 AI Board 是否还在运行：它占着旧的数据目录，设置搬不过来。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel32.OpenMutexW(0x00100000, False, OLD_MUTEX)  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED：互斥量在，只是打不开


def migrate_old_name():
    """从改名前的 AI Board 升级：把 %APPDATA%\\AIBoard 里的设置和页面数据、开机自动启动搬到新名字下。"""
    if OLD_DATA_DIR.is_dir() and not DATA_DIR.exists():
        try:
            OLD_DATA_DIR.rename(DATA_DIR)
            log.info("已把旧的设置从 %s 搬到 %s", OLD_DATA_DIR, DATA_DIR)
        except OSError:
            # 有文件还被占着（比如旧版刚退出，看板窗口的进程还没关完），能复制的先复制过来
            log.warning("搬旧的设置失败，改为复制", exc_info=True)
            try:
                shutil.copytree(OLD_DATA_DIR, DATA_DIR, dirs_exist_ok=True)
            except OSError:
                log.warning("复制旧的设置时有文件没复制成功", exc_info=True)
    if not FROZEN:
        return  # 开机自动启动只在 exe 里提供
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            winreg.QueryValueEx(key, OLD_RUN_VALUE)
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{sys.executable}"')
            winreg.DeleteValue(key, OLD_RUN_VALUE)
        log.info("开机自动启动已改为启动 %s", sys.executable)
    except FileNotFoundError:
        return  # 旧版没开开机自动启动
    except OSError:
        log.warning("搬开机自动启动失败", exc_info=True)
        return
    # 在 Windows 设置里关掉过的，换了名字也要关着
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            data, kind = winreg.QueryValueEx(key, OLD_RUN_VALUE)
            winreg.SetValueEx(key, RUN_VALUE, 0, kind, data)
            winreg.DeleteValue(key, OLD_RUN_VALUE)
    except FileNotFoundError:
        pass  # 没关过
    except OSError:
        log.warning("搬“启动应用”里的开关失败", exc_info=True)


# ---------- 开机自动启动（只在打包后的 exe 里提供） ----------


def autostart_enabled():
    """开机会不会启动正在运行的这个 exe：Run 里登记的是它（exe 换了文件夹，登记的旧位置不算），并且没在“启动应用”里关掉。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command = winreg.QueryValueEx(key, RUN_VALUE)[0]
    except OSError:
        return False
    return runs_this_exe(command) and not startup_disabled(RUN_VALUE)


def runs_this_exe(command):
    """Run 里的一条命令启动的是不是正在运行的这个 exe。命令带引号时取引号里的路径，不带的整条当路径。"""
    command = str(command).strip()
    path = command[1:].split('"', 1)[0] if command.startswith('"') else command
    try:
        return os.path.samefile(path, sys.executable)  # 大小写、短文件名写法不同也认得出来
    except OSError:
        return False  # 登记的位置已经没有这个文件了


def startup_disabled(name):
    """是不是在“启动应用”里关掉了。值是 12 个字节，第一个字节是单数表示关掉（02 开着、03 关着），没有记录就是开着。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return False
    return isinstance(data, bytes) and bool(data) and data[0] & 1 == 1


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
    # “启动应用”里的开关记录一起删掉：打开时，之前在 Windows 设置里关掉的那笔还留着的话，写了 Run 也不会启动
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass
