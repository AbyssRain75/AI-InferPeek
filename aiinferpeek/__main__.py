import argparse
import os
import socket
import sys
import threading
import webbrowser

import uvicorn

from . import __version__
from .app import create_app, is_loopback, port_answering
from .config import DEFAULT_CONFIG, ConfigError, load_config

LAN_WARNING = """
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! 警告：看板开放到了局域网（--host {host}），没有密码保护
!! 同一网络里的任何人都能打开它，看到会话标题、项目目录、模型、
!! token 用量和进程列表。只在信任的网络里这样用；只给本机看时去掉 --host
!! 这种模式下设置页面只能查看，网页上的修改一律拒绝
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""

START_FAILED = "AI InferPeek 没有启动成功：端口 {port} 可能被别的程序占用了（桌面版默认也用 8765）。可以先退出占用它的程序，或者用 --port 换一个端口；具体的错误见上面"


def dual_stack_socket(port):
    """--host :: 用的监听套接字，IPv6 和 IPv4 都收。只把 :: 交给 uvicorn 时，asyncio 会给 IPv6 套接字设上 IPV6_V6ONLY，
    只收 IPv6：本机打开的 http://127.0.0.1:端口 连不上，局域网里用 IPv4 的电脑也连不上。"""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    except OSError:
        sock.close()
        raise
    return sock


def announce_when_started(server, done, announce, poll=0.05):
    """等服务真的启动成功（端口绑上了）再调用 announce()，返回有没有调用。在另一个线程里跑，done 是 server.run() 结束后设上的 Event。
    没启动成功就退出了（比如端口被占用）不调用；还没启动好就按了 Ctrl+C（启动完马上就退）也不调用。"""
    while not server.started:
        if done.wait(poll):
            return False
    if server.should_exit:
        return False
    announce()
    return True


def main():
    parser = argparse.ArgumentParser(
        prog="aiinferpeek",
        description="本地 AI 运行状态看板：在浏览器里实时查看本机硬件（CPU、内存、显卡）的占用，以及本地模型服务（Ollama、ComfyUI）和 AI 编程工具（Claude Code、Codex、Cursor 等）的运行情况",
    )
    parser.add_argument("--version", action="version", version=f"AI InferPeek {__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只允许本机访问；改成 0.0.0.0（只收 IPv4）、::（IPv4 和 IPv6 都收）会开放到局域网，见 README")
    parser.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔（秒），默认 1")
    parser.add_argument("--history", type=int, default=600, help="保留多少秒的历史，默认 600")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--config", help=f"配置文件路径，默认读取程序旁边的 {DEFAULT_CONFIG.name}；文件还不存在时用默认设置，设置页面保存时创建")
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
    if args.config and not os.path.exists(args.config):
        # 和默认路径一样先用默认设置启动；说一声，免得路径打错了没人发现
        print(f"配置文件 {args.config} 不存在，先用默认设置；在设置页面保存时会创建它", file=sys.stderr)

    sockets = None
    if args.host == "::":
        try:
            sockets = [dual_stack_socket(args.port)]
        except OSError as exc:
            # 和默认地址端口被占用时一样：先说系统给的错误（那边是 uvicorn 打的），再补一句怎么办，退出码 3
            print(f"监听 [::]:{args.port} 失败：{exc}", file=sys.stderr)
            print(START_FAILED.format(port=args.port), file=sys.stderr)
            sys.exit(3)
    # 绑得上不等于端口空着：别的程序用通配地址监听着这个端口时照样绑得上（见 port_answering），启动后本机访问到的不一定是谁。
    # 所以启动前先连一下，已经有人在接连接就按端口被占用处理。--host :: 上面已经绑好了；其余地址由 uvicorn 去绑，
    # 真被同一个地址占着时这里马上连得上，不用等它绑了才知道
    if port_answering(args.host, args.port):
        for sock in sockets or ():
            sock.close()
        print(f"端口 {args.port} 上已经有别的程序在监听", file=sys.stderr)
        print(START_FAILED.format(port=args.port), file=sys.stderr)
        sys.exit(3)

    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{f'[{host}]' if ':' in host else host}:{args.port}"
    if not is_loopback(args.host):
        print(LAN_WARNING.format(host=args.host), file=sys.stderr)

    server = uvicorn.Server(
        uvicorn.Config(
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
    )

    def announce():
        print(f"AI InferPeek 已启动：{url}  （按 Ctrl+C 退出）")
        if not args.no_browser:
            webbrowser.open(url)

    # 端口绑上之后才说“已启动”、才打开浏览器：端口被占用时（比如桌面版正开着）先说已启动再报错，
    # 浏览器打开的还是占着端口的另一个程序
    done = threading.Event()
    threading.Thread(target=announce_when_started, args=(server, done, announce), daemon=True).start()
    # 和 uvicorn.run() 一样：Ctrl+C 安静地退出，没启动成功时退出码是 3（uvicorn 的 STARTUP_FAILURE）
    interrupted = False
    try:
        server.run(sockets=sockets)
    except KeyboardInterrupt:
        interrupted = True
    except SystemExit:
        # 没启动成功时 uvicorn 自己退出（sys.exit），原因它已经打在上面了；这里接住，补一句怎么办再退
        if server.started:
            raise
    finally:
        done.set()
    if not server.started:
        if not interrupted:
            print(START_FAILED.format(port=args.port), file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
