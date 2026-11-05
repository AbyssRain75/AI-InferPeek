"""悬浮窗、看板窗口（完整看板和设置页面）和托盘菜单。"""

import json
import logging
import math
import threading
import time
import webbrowser
from ctypes import wintypes
from urllib.parse import urlsplit

import pystray
import webview
from webview.window import FixPoint

from .. import __version__
from ..config import FROZEN
from ..icon import make_icon_image
from .settings import COMPACT_WIDTH, DOCKS, OPACITIES, THEMES, WIDTHS
from .system import APP_NAME, IDYES, MB_ICONWARNING, MB_YESNO, autostart_enabled, message_box, set_autostart, system_dark
from .win32 import (
    SWP_NOACTIVATE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    apply_window_style,
    bring_to_front,
    hide_from_taskbar,
    list_work_areas,
    round_corners,
    set_dpi_aware,
    set_menu_theme,
    system_scale,
    user32,
    window_rect,
    work_area,
)

WINDOW_TITLE = "AI InferPeek 悬浮窗"
MAIN_SIZE = (1280, 860)  # 看板窗口的默认大小（逻辑像素），屏幕放不下时缩小
MINI_MIN_SIZE = (200, 80)  # 悬浮窗最小的大小（逻辑像素）
MINI_MARGIN = 24  # 悬浮窗默认放在主屏右上角，离右边和上边的距离（逻辑像素）

log = logging.getLogger("aiinferpeek")


def find_window():
    return user32.FindWindowW(None, WINDOW_TITLE)


def main_window_rect():
    """看板窗口的位置和大小 (x, y, 宽, 高)，逻辑像素：默认 MAIN_SIZE，放在主屏去掉任务栏后的正中间，放不下时缩小，四周留点边。

    pywebview 不给位置时用的 CenterScreen 不起作用（设置时窗口已经建好了），所以自己算。
    """
    work_left, work_top, work_right, work_bottom = list_work_areas()[0]
    scale = system_scale()
    work_width, work_height = (work_right - work_left) / scale, (work_bottom - work_top) / scale
    width = max(min(MAIN_SIZE[0], int(work_width) - 40), 640)
    height = max(min(MAIN_SIZE[1], int(work_height) - 40), 480)
    x = int(work_left / scale + (work_width - width) / 2)
    y = int(work_top / scale + (work_height - height) / 2)
    return x, y, width, height


# 悬浮窗的位置：区域都是 (左, 上, 右, 下)，窗口用左上角 (x, y) 和宽、高表示；下面几个函数只做计算，单位一致就行


def logical_area(area, scale):
    """物理像素的区域换成逻辑像素（除以缩放比例）。往里取整：放在里面的窗口换回物理像素后也不会超出边。"""
    left, top, right, bottom = area
    return math.ceil(left / scale), math.ceil(top / scale), math.floor(right / scale), math.floor(bottom / scale)


def overlap(x, y, width, height, area):
    """窗口和区域重叠的面积。"""
    left, top, right, bottom = area
    return max(min(x + width, right) - max(x, left), 0) * max(min(y + height, bottom) - max(y, top), 0)


def clamp_pos(x, y, width, height, area):
    """把窗口整个拉进区域，返回新的左上角；比区域还大时贴左边、上边（标题栏留在屏幕里）。"""
    left, top, right, bottom = area
    return max(min(x, right - width), left), max(min(y, bottom - height), top)


def dock_pos(x, y, height, area, dock):
    """吸附时把窗口贴到区域的顶边或底边，返回新的左上角；左右不动，不吸附时原样返回。"""
    if dock == "top":
        y = area[1]
    elif dock == "bottom":
        y = area[3] - height
    return x, y


def mini_window_pos(x, y, width, height, areas, scale):
    """悬浮窗打开时的位置 (x, y)，逻辑像素（pywebview 会再乘缩放比例换成物理像素）。

    x, y：上次保存的位置（逻辑像素，pywebview 移动事件给的），没保存过是 None；width、height：窗口大小（逻辑像素）；
    areas：各显示器去掉任务栏后的区域（物理像素，主屏在最前，见 list_work_areas()）；scale：缩放比例，见 system_scale()。
    保存的位置还和某块屏幕重叠，就放回去，整个拉进重叠最多的那块；和哪块都不重叠（拔掉了副屏、换了分辨率）
    或者没保存过，放在主屏右上角。
    """
    areas = [logical_area(area, scale) for area in areas]
    if x is not None and y is not None:
        area = max(areas, key=lambda a: overlap(x, y, width, height, a))
        if overlap(x, y, width, height, area):
            return clamp_pos(x, y, width, height, area)
    right, top = areas[0][2], areas[0][1]
    return clamp_pos(right - width - MINI_MARGIN, top + MINI_MARGIN, width, height, areas[0])


class JsApi:
    """悬浮窗页面里通过 window.pywebview.api 调用。属性名都以下划线开头，避免被暴露给页面。"""

    def __init__(self, app):
        self._app = app

    def fit(self, height):
        self._app.fit(int(height))

    def hide(self):
        self._app.set_visible(False)

    def open_dashboard(self):
        self._app.open_dashboard()

    def set_compact(self, compact):
        self._app.set_compact(bool(compact))

    def set_device_name(self, name):
        self._app.set_device_name(name if isinstance(name, str) else None)


class MainApi:
    """看板窗口的页面里通过 window.pywebview.api 调用。"""

    def __init__(self, app):
        self._app = app

    def set_unsaved(self, unsaved):
        # 设置页面有没保存的修改：关窗口、退出前先问一下（浏览器里关标签页会问，这里窗口关了页面不知道）
        self._app.unsaved = bool(unsaved)


class DesktopApp:
    def __init__(self, settings, url):
        self.settings = settings
        self.url = url
        self.window = None
        self.icon = None
        self.hwnd = None
        self.height = 360
        self.quitting = False
        self.main = None  # 看板窗口（完整看板和设置页面），关掉后是 None，下次打开再建
        self.main_page = None  # 看板窗口里现在是哪个页面："" 是看板，"settings.html" 是设置
        self.main_lock = threading.Lock()
        self.unsaved = False

    # 窗口

    def mini_url(self):
        theme = self.settings["theme"]
        query = [f"theme={theme}" if theme in ("dark", "light") else None, "compact=1" if self.settings["compact"] else None]
        query = "&".join(q for q in query if q)
        return f"{self.url}/mini.html" + (f"?{query}" if query else "")

    def window_width(self):
        return COMPACT_WIDTH if self.settings["compact"] else self.settings["width"]

    def create_window(self):
        st = self.settings
        set_dpi_aware()  # 先设好，下面读到的显示器区域才和 pywebview 用的坐标对得上
        width = self.window_width()
        # 高度要等页面加载完按内容定（fit），这里先保证顶上一截（标题栏）在屏幕里，fit 时再按实际高度整个拉进来
        x, y = mini_window_pos(st["x"], st["y"], width, MINI_MIN_SIZE[1], list_work_areas(), system_scale())
        self.window = webview.create_window(
            WINDOW_TITLE,
            self.mini_url(),
            js_api=JsApi(self),
            width=width,
            height=self.height,
            x=x,
            y=y,
            frameless=True,
            easy_drag=False,
            on_top=st["on_top"],
            resizable=False,
            hidden=not st["visible"],
            background_color="#1a1a19",
            min_size=MINI_MIN_SIZE,
        )
        # 拖动标题栏时 pywebview 每次鼠标移动都调用 window.move（逻辑像素）；吸附时改成贴着边移动，窗口只能沿着边左右滑
        move = self.window.move

        def drag_move(x, y):
            scale = user32.GetDpiForWindow(wintypes.HWND(self.hwnd)) / 96 if self.hwnd else 1
            if not self.snap(round(x * scale), round(y * scale)):
                move(x, y)

        self.window.move = drag_move
        self.window.events.shown += self._on_shown
        self.window.events.loaded += self._on_loaded
        self.window.events.moved += self._on_moved
        self.window.events.closing += self._on_closing

    def snap(self, x=None, y=None):
        """吸附时把窗口贴到所在屏幕的顶边或底边（任务栏让开），返回是否处理了。

        x, y 是想去的位置（物理像素，拖动时跟着鼠标），默认原地。只贴边，左右拖到哪是哪，不往屏幕里拉。
        """
        if self.settings["dock"] == "none" or not self.hwnd:
            return False
        self._place(x, y, keep_inside=False)
        return True

    def keep_on_screen(self):
        """把窗口整个拉进所在屏幕去掉任务栏后的区域，吸附时贴到顶边或底边。

        改宽度、切换小窗模式、页面高度变了、换吸附方式后调用；拖动时不管屏幕边，见 snap()。
        """
        if self.hwnd:
            self._place()

    def _place(self, x=None, y=None, keep_inside=True):
        # 物理像素，按窗口的实际大小算：刚创建的窗口高度和页面要的不一定一样
        left, top, right, bottom = window_rect(self.hwnd)
        x = left if x is None else x
        y = top if y is None else y
        width, height = right - left, bottom - top
        area = work_area(x + width // 2, y + height // 2)
        x, y = dock_pos(x, y, height, area, self.settings["dock"])
        if keep_inside:
            x, y = clamp_pos(x, y, width, height, area)
        if (x, y) != (left, top):
            user32.SetWindowPos(wintypes.HWND(self.hwnd), None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def resize(self, width, height, keep_right=False):
        # 吸附在底部时底边不动，其余时候顶边不动；keep_right：右边不动。改完超出屏幕的话再往里挪
        fix = FixPoint.SOUTH if self.settings["dock"] == "bottom" else FixPoint.NORTH
        self.window.resize(width, height, fix_point=fix | (FixPoint.EAST if keep_right else FixPoint.WEST))
        self.keep_on_screen()

    def _on_shown(self):
        log.info("悬浮窗已显示，渲染引擎：%s", webview.renderer)
        for _ in range(50):
            self.hwnd = find_window()
            if self.hwnd:
                break
            time.sleep(0.05)
        if not self.hwnd:
            log.warning("找不到悬浮窗句柄，透明度和鼠标穿透不可用")
            return
        hide_from_taskbar(self.hwnd)
        round_corners(self.hwnd)
        self.apply_style()
        self.snap()

    def _on_moved(self, x, y):
        self.settings["x"], self.settings["y"] = int(x), int(y)
        self.settings.save()

    def _on_closing(self):
        # Alt+F4 之类只隐藏，真正退出走托盘菜单。这个事件在界面线程里同步执行，隐藏要交给别的线程：
        # set_visible() 用 evaluate_js 通知页面，会等界面线程送回结果，在界面线程上等就卡死了
        if self.quitting:
            return True
        threading.Thread(target=self.set_visible, args=(False,), name="hide", daemon=True).start()
        return False

    def apply_style(self):
        if self.hwnd:
            apply_window_style(self.hwnd, self.settings["opacity"], self.settings["click_through"])

    def fit(self, height):
        # 宽度以设置为准，高度跟着页面内容走
        if self.window and height > 0:
            self.height = height
            self.resize(self.window_width(), height)

    def set_visible(self, visible):
        self.settings["visible"] = visible
        if visible:
            self._tell_page_visible(True)  # 先在隐藏着的时候画好，窗口一出来就是最新的
            self.window.show()
            if self.hwnd:
                hide_from_taskbar(self.hwnd)
                self.apply_style()
        else:
            self.window.hide()
            self._tell_page_visible(False)
        self.refresh_menu()

    def _call_page(self, name, value, what):
        """调用悬浮窗页面上的 window.<name>(value)，页面还没准备好这个函数时不调；what 是写日志用的说明。"""
        try:
            self.window.evaluate_js(f"window.{name} && window.{name}({json.dumps(value)})")
        except Exception:
            log.exception("通知悬浮窗%s失败", what)

    def _tell_page_visible(self, visible):
        # pywebview 的 hide 只隐藏窗体，WebView2 里的页面仍算可见（document.hidden 一直是 false），
        # 要自己告诉页面：隐藏时只收数据不渲染，显示时马上刷新一次
        self._call_page("setWindowVisible", visible, "显示状态")

    def _on_loaded(self):
        # 隐藏时在托盘里换主题会重新加载页面，新页面要再告诉一次
        if not self.settings["visible"]:
            self._tell_page_visible(False)

    # 看板窗口：普通的程序窗口（有标题栏、任务栏图标，能最大化），完整看板和设置页面都在这里打开

    def open_dashboard(self):
        self.open_page("")

    def open_settings(self):
        # 要填路径、地址，悬浮窗太小，放在看板窗口里
        self.open_page("settings.html")

    def open_page(self, page):
        """在看板窗口里打开 page：窗口没开就新建，开着就换到这个页面，并切到最前面。"""
        url = f"{self.url}/{page}"
        if not self.window:
            webbrowser.open(url)  # 没有 WebView2，只能用浏览器
            return
        with self.main_lock:
            main = self.main
            if main is None:
                self.main, self.main_page = self.create_main_window(url), page
                return
        try:
            if self.main_page != page:
                # 让页面自己跳转：设置页面有没保存的修改时会先问要不要离开，选“留下”就不换
                main.evaluate_js(f"location.assign({json.dumps(url)})")
            bring_to_front(main.native.Handle.ToInt64())
        except Exception:
            log.exception("切换看板窗口失败")

    def create_main_window(self, url):
        x, y, width, height = main_window_rect()
        main = webview.create_window(
            APP_NAME,
            url,
            js_api=MainApi(self),
            width=width,
            height=height,
            x=x,
            y=y,
            min_size=(640, 480),
            text_select=True,  # 会话标题、目录、设置里的路径要能选中复制
            background_color="#0d0d0d" if system_dark() else "#f9f9f7",  # 和页面底色一样，打开时不闪白
        )
        main.events.loaded += self._on_main_loaded
        main.events.closing += self._on_main_closing
        return main

    @staticmethod
    def page_of(url):
        page = urlsplit(url or "").path.lstrip("/")
        return "" if page == "index.html" else page

    def _on_main_loaded(self, window):
        # 换了页面：之前页面上没保存的修改已经不在了；窗口标题跟着页面标题（“AI InferPeek”“AI InferPeek 设置”）
        if window is not self.main:
            return  # 已经关掉的窗口
        self.unsaved = False
        try:
            self.main_page = self.page_of(window.get_current_url())
            window.set_title(window.evaluate_js("document.title") or APP_NAME)
        except Exception:
            log.exception("读取看板窗口的页面失败")

    def _on_main_closing(self, window):
        # 在界面线程里同步调用，返回 False 取消关闭；退出时 quit() 已经问过了
        if self.unsaved and not self.quitting and not self.confirm_discard(window, "关闭窗口"):
            return False
        if self.main is window:
            self.main, self.main_page, self.unsaved = None, None, False
        return True

    def confirm_discard(self, window, action):
        try:
            owner = window.native.Handle.ToInt64()
        except Exception:
            owner = None
        text = f"设置页面上有未保存的修改，{action}后这些修改将会丢失。\n\n确定要{action}吗？"
        return message_box(text, MB_YESNO | MB_ICONWARNING, owner) == IDYES

    # 托盘菜单动作

    def toggle_visible(self, *_):
        self.set_visible(not self.settings["visible"])

    def toggle_on_top(self, *_):
        self.settings["on_top"] = not self.settings["on_top"]
        self.window.on_top = self.settings["on_top"]
        self.settings.save()

    def set_dock(self, value):
        def action(*_):
            self.settings["dock"] = value
            self.keep_on_screen()  # 马上贴过去
            self.settings.save()

        return action

    def toggle_click_through(self, *_):
        self.settings["click_through"] = not self.settings["click_through"]
        self.apply_style()
        self.settings.save()

    def set_opacity(self, value):
        def action(*_):
            self.settings["opacity"] = value
            self.apply_style()
            self.settings.save()

        return action

    def set_width(self, value):
        def action(*_):
            self.settings["width"] = value
            if not self.settings["compact"]:
                self.resize(value, self.height)
            self.settings.save()

        return action

    def set_compact(self, compact):
        # 右上角不动：标题栏右边的切换按钮还在鼠标底下，靠右放的窗口也不会离开屏幕边；变宽后左边出了屏幕才往里挪
        self.settings["compact"] = compact
        self.resize(self.window_width(), self.height, keep_right=True)
        self.settings.save()
        self.refresh_menu()

    def toggle_compact(self, *_):
        compact = not self.settings["compact"]
        self.set_compact(compact)
        # 页面自己换布局，换完会按新内容调整高度
        self._call_page("setCompact", compact, "切换小窗模式")

    def set_theme(self, value):
        def action(*_):
            self.settings["theme"] = value
            set_menu_theme(value)
            self.window.load_url(self.mini_url())
            self.settings.save()

        return action

    def toggle_autostart(self, *_):
        try:
            set_autostart(not autostart_enabled())
        except OSError:
            log.exception("修改开机启动失败")
            message_box("修改开机自动启动失败，详情见日志。")

    def quit(self, *_):
        main = self.main
        if main and self.unsaved and not self.confirm_discard(main, "退出"):
            return
        self.quitting = True
        self.stop_tray()
        if main:
            main.destroy()  # 先关看板窗口：最后一个窗口关掉时界面线程才结束
        if self.window:
            self.window.destroy()

    def set_device_name(self, name):
        # 托盘图标的提示带上设置里给这台电脑起的名字；悬浮窗页面收到名字（或名字变了）时调用
        title = f"{APP_NAME} · {name}" if name else APP_NAME
        if self.icon and self.icon.title != title:
            self.icon.title = title

    def stop_tray(self):
        icon, self.icon = self.icon, None
        if icon:
            try:
                icon.stop()
            except Exception:
                pass

    def refresh_menu(self):
        if self.icon:
            self.icon.update_menu()

    def build_tray(self):
        st = self.settings
        set_menu_theme(st["theme"])
        Item, Menu = pystray.MenuItem, pystray.Menu
        if self.window:
            items = [
                Item("显示悬浮窗", self.toggle_visible, checked=lambda _: st["visible"], default=True),
                Item("打开完整看板", lambda *_: self.open_dashboard()),
                Item("设置...", lambda *_: self.open_settings()),
                Item("小窗模式", self.toggle_compact, checked=lambda _: st["compact"]),
                Menu.SEPARATOR,
                Item("总在最前", self.toggle_on_top, checked=lambda _: st["on_top"]),
                Item(
                    "吸附",
                    Menu(*(Item(name, self.set_dock(k), checked=lambda _, k=k: st["dock"] == k, radio=True) for k, name in DOCKS.items())),
                ),
                Item("鼠标穿透", self.toggle_click_through, checked=lambda _: st["click_through"]),
                Item(
                    "透明度",
                    Menu(*(Item(f"{v}%", self.set_opacity(v), checked=lambda _, v=v: st["opacity"] == v, radio=True) for v in OPACITIES)),
                ),
                Item(
                    "宽度",
                    Menu(*(Item(name, self.set_width(v), checked=lambda _, v=v: st["width"] == v, radio=True) for name, v in WIDTHS.items())),
                    enabled=lambda _: not st["compact"],  # 小窗模式宽度固定
                ),
                Item(
                    "主题",
                    Menu(*(Item(name, self.set_theme(k), checked=lambda _, k=k: st["theme"] == k, radio=True) for k, name in THEMES.items())),
                ),
            ]
        else:
            # 没有 WebView2 时没有悬浮窗，只留看板入口
            items = [
                Item("打开完整看板", lambda *_: self.open_dashboard(), default=True),
                Item("设置...", lambda *_: self.open_settings()),
            ]
        if FROZEN:
            items += [Menu.SEPARATOR, Item("开机自动启动", self.toggle_autostart, checked=lambda _: autostart_enabled())]
        items += [Menu.SEPARATOR, Item(f"版本 {__version__}", None, enabled=False), Item("退出", self.quit)]
        self.icon = pystray.Icon("AIInferPeek", make_icon_image(), APP_NAME, menu=Menu(*items))
