"""桌面版内置的看板服务：在后台线程里跑，只监听本机。"""

import socket
import threading
import time

import uvicorn

from ..app import create_app
from ..config import DEFAULT_CONFIG


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred):
    for port in [preferred, *range(preferred + 1, preferred + 20)]:
        if port_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, port, config):
        cfg = uvicorn.Config(
            create_app(config=config, config_path=DEFAULT_CONFIG),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=1,
            log_config=None,
        )
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="server", daemon=True)

    def start(self, timeout=20):
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("服务线程意外退出")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{timeout} 秒内没有启动完成")
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
