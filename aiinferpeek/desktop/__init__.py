"""桌面版：后台运行看板服务，显示置顶悬浮窗，右下角托盘图标负责控制。

托盘菜单：显示/隐藏悬浮窗、打开完整看板、设置（改 aiinferpeek.json）、小窗模式、置顶、吸附到顶部/底部、鼠标穿透、透明度、宽度、主题、开机自动启动、版本号、退出。
完整看板和设置页面在程序自己的窗口（看板窗口）里打开，不用浏览器；两个页面共用这一个窗口。
悬浮窗的位置、宽度、透明度、小窗模式、吸附等设置保存在 %APPDATA%\\AIInferPeek\\settings.json。

app.py 是悬浮窗、看板窗口和托盘；settings.py 是悬浮窗的设置；server.py 是内置的看板服务；
system.py 是数据位置、只运行一个、WebView2 检测、开机自动启动等和系统打交道的部分；win32.py 是窗口样式、显示器区域。
"""

import logging
import sys
import threading
import webbrowser

import webview

from .. import __version__
from ..config import FROZEN, ConfigError, load_config
from ..logs import LogFile
from .app import DesktopApp
from .server import ServerThread, pick_port
from .settings import Settings
from .system import (
    DATA_DIR,
    IDYES,
    LOG_FILE,
    LOG_MAX,
    MB_ICONWARNING,
    MB_YESNO,
    WEBVIEW2_DOWNLOAD,
    message_box,
    migrate_old_name,
    old_version_running,
    single_instance,
    webview2_version,
)

log = logging.getLogger("aiinferpeek")


def _setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if FROZEN:
        # 无控制台的 exe 没有 stdout/stderr，统一写到日志文件；写满 LOG_MAX 换一份，见 LogFile
        sys.stdout = sys.stderr = LogFile(LOG_FILE, LOG_MAX)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def main():
    _setup_logging()
    mutex = single_instance()
    if mutex is None:
        message_box("AI InferPeek 已经在运行了，请在屏幕右下角的托盘图标里操作。")
        return
    if old_version_running():
        message_box("旧版的 AI Board 还在运行，请先在屏幕右下角的托盘图标里退出它，再打开 AI InferPeek。")
        return
    log.info("AI InferPeek %s 启动", __version__)
    migrate_old_name()
    try:
        config = load_config()
    except ConfigError as exc:
        message_box(str(exc))
        return

    settings = Settings()
    port = pick_port(settings["port"])
    try:
        server = ServerThread(port, config)
        server.start()
    except Exception as exc:
        log.exception("启动看板服务失败")
        message_box(f"看板服务启动失败：{exc}\n\n详情见日志：{LOG_FILE}", MB_ICONWARNING)
        return
    log.info("看板服务已启动：http://127.0.0.1:%s", port)

    app = DesktopApp(settings, f"http://127.0.0.1:{port}")
    try:
        version = webview2_version()
        if version:
            log.info("WebView2 版本：%s", version)
            app.create_window()
            app.build_tray()
            threading.Thread(target=app.icon.run, name="tray", daemon=True).start()
            # 不用隐私模式：看板窗口里的主题、时间范围、关掉的“已完成”提示要像浏览器里一样，下次打开还在
            webview.start(gui="edgechromium", storage_path=str(DATA_DIR / "webview"), private_mode=False)
        else:
            log.warning("没有找到 WebView2 运行时，不显示悬浮窗")
            answer = message_box(
                "系统未检测到 Microsoft Edge WebView2 运行时，无法加载桌面悬浮窗（Windows 11 自带，部分 Windows 10 需要另外安装）。\n\n"
                "• 点“是”：前往官网下载并退出程序，安装完成后重新打开 AI InferPeek。\n"
                "• 点“否”：本次先在浏览器中打开完整看板，系统托盘图标仍正常可用。",
                MB_YESNO | MB_ICONWARNING,
            )
            if answer == IDYES:
                webbrowser.open(WEBVIEW2_DOWNLOAD)
            else:
                app.build_tray()
                app.open_dashboard()
                app.icon.run()  # 托盘菜单“退出”后返回
    except Exception:
        log.exception("桌面版运行出错")
        message_box(f"AI InferPeek 出错了，即将退出。\n\n详情见日志：{LOG_FILE}", MB_ICONWARNING)
    finally:
        app.stop_tray()
        server.stop()
        settings._write()
