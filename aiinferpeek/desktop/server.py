"""桌面版内置的看板服务：在后台线程里跑，只监听本机。"""

import socket
import threading
import time

import uvicorn

from ..app import create_app, port_answering
from ..config import DEFAULT_CONFIG
from ..i18n import tr


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    # 绑得上不等于没人用：别的程序用 0.0.0.0、[::] 监听着这个端口时照样绑得上（见 port_answering），再连一下看有没有人在接
    return not port_answering("127.0.0.1", port)


def pick_port(preferred):
    # 往后试 19 个，不超过 65535：再大 bind 抛的是 OverflowError，不是端口被占的 OSError
    for port in [preferred, *range(preferred + 1, min(preferred + 20, 65536))]:
        if port_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, port, config, updates=None):
        """updates：托盘用的 UpdateChecker，看板顶栏的“检查更新”也用它。"""
        cfg = uvicorn.Config(
            create_app(config=config, config_path=DEFAULT_CONFIG, updates=updates),
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
                raise RuntimeError(tr("服务线程意外退出"))
            if time.monotonic() > deadline:
                raise RuntimeError(tr("{n} 秒内未完成启动", n=timeout))
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
