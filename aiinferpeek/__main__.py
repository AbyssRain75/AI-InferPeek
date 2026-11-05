import argparse
import sys
import threading
import webbrowser

import uvicorn

from . import __version__
from .app import create_app, is_loopback
from .config import DEFAULT_CONFIG, ConfigError, load_config

LAN_WARNING = """
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! 警告：看板开放到了局域网（--host {host}），没有密码保护
!! 同一网络里的任何人都能打开它，看到会话标题、项目目录、模型、
!! token 用量和进程列表。只在信任的网络里这样用；只给本机看时去掉 --host
!! 这种模式下设置页面只能查看，网页上的修改一律拒绝
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""


def main():
    parser = argparse.ArgumentParser(prog="aiinferpeek", description="本地 AI 运行状态看板")
    parser.add_argument("--version", action="version", version=f"AI InferPeek {__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只允许本机访问；改成 0.0.0.0 会开放到局域网，见 README")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔（秒）")
    parser.add_argument("--history", type=int, default=600, help="保留多少秒的历史")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--config", help=f"配置文件路径，默认读取 {DEFAULT_CONFIG.name}（存在时）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 应该在 1 到 65535 之间")
    if args.interval <= 0:
        parser.error("--interval 应该大于 0")
    if args.history < args.interval:
        parser.error("--history 不能小于 --interval")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.exit(str(exc))

    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{f'[{host}]' if ':' in host else host}:{args.port}"
    if not is_loopback(args.host):
        print(LAN_WARNING.format(host=args.host), file=sys.stderr)
    print(f"AI InferPeek 已启动：{url}  （按 Ctrl+C 退出）")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    uvicorn.run(
        create_app(
            interval=args.interval,
            history_seconds=args.history,
            config=config,
            host=args.host,
            config_path=args.config or DEFAULT_CONFIG,
        ),
        host=args.host,
        port=args.port,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
