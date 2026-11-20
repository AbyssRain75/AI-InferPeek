"""悬浮窗和托盘的设置：位置、宽度、透明度、小窗模式、吸附、自动检查更新等，保存在 %APPDATA%\\AIInferPeek\\settings.json。"""

import json
import logging
import threading

from ..config import write_atomic
from ..i18n import N_
from .system import DATA_DIR, SETTINGS_FILE

log = logging.getLogger("aiinferpeek")

DEFAULTS = {
    "port": 8765,
    "x": None,
    "y": None,
    "width": 300,
    "opacity": 100,
    "on_top": True,
    "click_through": False,
    "theme": "system",
    "visible": True,
    "compact": False,
    "dock": "none",
    "check_update": True,  # 自动检查新版本（启动后和每天一次），见 update.py
    "update_notified": None,  # 已经弹过通知的新版本号：同一个版本只提醒一次，重启后也不再弹
}
# 托盘菜单里的名字用 N_() 标出来，建菜单时按当前语言翻译
WIDTHS = {N_("窄"): 260, N_("中"): 300, N_("宽"): 360}
COMPACT_WIDTH = 240  # 小窗模式：不画进度条和折线，只显示数字
OPACITIES = (100, 90, 80, 70, 60)
THEMES = {"system": N_("跟随系统"), "dark": N_("深色"), "light": N_("浅色")}
DOCKS = {"none": N_("不吸附"), "top": N_("顶部"), "bottom": N_("底部")}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# 每项设置允许的值；settings.json 里写坏的项回到默认值
SETTING_CHECKS = {
    "port": lambda v: _is_int(v) and 1 <= v <= 65535,
    "x": lambda v: v is None or _is_int(v),
    "y": lambda v: v is None or _is_int(v),
    "width": lambda v: _is_int(v) and v in WIDTHS.values(),
    "opacity": lambda v: _is_int(v) and v in OPACITIES,
    "on_top": lambda v: isinstance(v, bool),
    "click_through": lambda v: isinstance(v, bool),
    "theme": lambda v: isinstance(v, str) and v in THEMES,
    "visible": lambda v: isinstance(v, bool),
    "compact": lambda v: isinstance(v, bool),
    "dock": lambda v: isinstance(v, str) and v in DOCKS,
    "check_update": lambda v: isinstance(v, bool),
    "update_notified": lambda v: v is None or (isinstance(v, str) and len(v) <= 32),
}


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        saved = {}
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("读取设置文件失败，使用默认设置", exc_info=True)
        if not isinstance(saved, dict):
            log.warning("设置文件的内容不是 { } 对象，使用默认设置")
            saved = {}
        for key, check in SETTING_CHECKS.items():
            if key not in saved:
                continue
            if check(saved[key]):
                self[key] = saved[key]
            else:
                log.warning("设置 %s 的值 %r 不对，改用默认值 %r", key, saved[key], DEFAULTS[key])
        self["visible"] = True  # 每次启动都显示悬浮窗，不然看起来像没打开
        self._timer = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def save(self, delay=0.5):
        # 拖动窗口时会连续触发，稍等一下再写盘
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._write)
            self._timer.daemon = True
            self._timer.start()

    def flush(self):
        """马上写盘，不等 save() 的定时器：退出、关机时用。"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self._write()

    def _write(self):
        # 写到一半崩溃也不会留下坏文件（见 write_atomic）；定时器线程和退出时的主线程可能同时写，所以持锁
        with self._write_lock:
            text = json.dumps(dict(self), ensure_ascii=False, indent=2)
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                write_atomic(SETTINGS_FILE, text)
            except OSError:
                log.exception("保存设置失败")
