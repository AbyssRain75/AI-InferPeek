"""网页服务：静态页面 + 实时数据推送（Server-Sent Events）。"""

import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .collectors import default_collectors
from .config import (
    CONFIG_KEYS,
    DEFAULT_CONFIG,
    DEVICE_NAME_MAX,
    FROZEN,
    PATH_KEYS,
    SERVICE_KINDS,
    WEB_DIR,
    ConfigConflict,
    ConfigInvalid,
    config_problems,
    read_config_file,
    update_config_file,
)
from .runtimes import default_adapters
from .sampler import Sampler

APPLY_TIMEOUT = 5.0  # 秒，保存设置后最多等这么久，让新配置发现、轮询过一轮再返回


def _finite(data):
    """NaN、无穷大换成 None（页面上显示“—”）：JSON 里没有这几个值，Python 照样输出 NaN，浏览器的 JSON.parse 会出错，
    整条数据都用不了。采集器读到的异常值（比如计数器算出 0/0）都在这里统一处理。"""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {k: _finite(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_finite(v) for v in data]
    return data


def _event(name, data):
    # allow_nan=False：上面万一漏掉了就直接报错，不发出浏览器解析不了的数据
    text = json.dumps(_finite(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return f"event: {name}\ndata: {text}\n\n"


# ---------- 请求来源校验 ----------
# 1. 所有请求都校验 Host 头：本机浏览器里的恶意网页可以用 DNS 重绑定把自己的域名指到 127.0.0.1 来读本地接口，
#    这时 Host 是它的域名。监听回环地址时只认 127.0.0.1 / localhost / [::1] 加本服务的端口
# 2. 会改动东西的请求（GET、HEAD、OPTIONS 以外）还要：服务只监听回环地址、客户端是本机、Origin 是本服务自己的地址。
#    浏览器给所有非 GET 请求都带 Origin（同源的也带），网页脚本改不了它；没有 Origin 的不是网页发的，
#    是本机程序，它本来就能直接改配置文件，拒绝它不损失什么，也省得另外发令牌

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_HOST_RE = re.compile(r"(?:\[([0-9a-f:.]+)\]|([^:\[\]/@\s]+))(?::(\d{1,5}))?")


def is_loopback(host):
    """监听地址是不是只有本机连得上：localhost、127.x.x.x、::1。0.0.0.0 / :: 是所有网卡，不算。"""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _split_host(value):
    """Host 头（或 Origin 去掉 http:// 的部分）→ (小写主机名, 端口)，没写端口按 80；格式不对返回 (None, None)。"""
    m = _HOST_RE.fullmatch(value.lower())
    if not m:
        return None, None
    return m.group(1) or m.group(2), int(m.group(3) or 80)


def _is_ip(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


class LocalGuard:
    """ASGI 中间件：按上面的规则拒绝请求。host 是监听地址；端口取这次连接的本地端口（scope["server"]）。"""

    def __init__(self, app, host="127.0.0.1"):
        self.app = app
        self.local_only = is_loopback(host)
        # 本机能用的名字；监听的是别的回环地址（比如 127.0.0.2）时也认它
        self.names = set(LOOPBACK_NAMES)
        if self.local_only and host.lower() != "localhost":
            self.names.add(host.strip("[]").lower())
        # 开放到局域网时别的电脑用 IP 或本机的计算机名访问；域名一律不认，DNS 重绑定用的是域名
        self.lan_names = set()
        if not self.local_only:
            for name in (socket.gethostname(), socket.getfqdn()):
                self.lan_names |= {name.lower(), f"{name.lower()}.local"}

    def host_ok(self, value, port):
        name, p = _split_host(value)
        if name is None or p != port:
            return False
        return name in self.names or (not self.local_only and (_is_ip(name) or name in self.lan_names))

    def write_problem(self, headers, client, port):
        """会改动东西的请求不能放行的原因，可以放行返回 None。"""
        if not self.local_only:
            return "看板开放到了局域网（--host 不是本机地址），不能在网页上修改设置"
        if not client or not is_loopback(client[0]):
            return "只接受本机发来的修改"
        origin = headers.get("origin", "")
        if not origin:
            return "请求没有带 Origin，只接受本页面发来的修改"
        name, p = _split_host(origin[len("http://"):]) if origin.lower().startswith("http://") else (None, None)
        if name not in self.names or p != port:
            return f"请求来自别的网页（{origin}），不接受"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        hosts = [v.decode("latin-1") for k, v in scope["headers"] if k == b"host"]
        port = (scope.get("server") or (None, None))[1]
        if len(hosts) != 1 or port is None or not self.host_ok(hosts[0], port):
            text = "Host 不对：请用 http://127.0.0.1:端口 或 http://localhost:端口 打开" if self.local_only else "Host 不对：请用 IP 地址或计算机名打开"
            return await self._reject(scope, receive, send, 400, text)
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            problem = self.write_problem(headers, scope.get("client"), port)
            if problem:
                return await self._reject(scope, receive, send, 403, problem)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, status, text):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await JSONResponse({"error": text}, status_code=status)(scope, receive, send)


class WebFiles(StaticFiles):
    """页面文件每次都向服务端确认有没有更新，重新打包后悬浮窗不会还用缓存里的旧文件。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# ---------- 设置页面 ----------


def _managed(data):
    """配置里设置页面管理的部分，没写的项给空值。"""
    return {
        "device_name": data.get("device_name"),
        "endpoints": data.get("endpoints") if data.get("endpoints") is not None else [],
        "disabled_tools": data.get("disabled_tools") if data.get("disabled_tools") is not None else [],
        **{k: data.get(k) for k in PATH_KEYS},
        "balance_warning": data.get("balance_warning"),
    }


def _tools():
    """所有能监控的工具（包括关掉的）：名字、类别、改数据位置的键，以及不改时用的位置（输入框里的提示）。"""
    out = []
    for a in default_adapters({}):
        paths = a.data_paths() if a.config_key else []
        default = os.path.normpath(paths[0][1]) if paths else None
        out.append({"kind": a.kind, "label": a.label, "category": a.category, "key": a.config_key, "default": default, "balance": a.has_balance})
    return out


def _write_error(path, exc):
    reason = exc.strerror or str(exc)
    if not isinstance(exc, PermissionError):
        return f"写入 {path} 失败：{reason}"
    where = "" if FROZEN else "，也可以用 --config 指定别处的配置文件"
    return (
        f"没有权限写入 {path}（{reason}）。文件可能是只读的，或者程序所在的文件夹不允许写入（比如放在 Program Files 里）："
        f"可以去掉文件的只读属性，或者把程序文件夹移到别处{where}"
    )


def create_app(interval=1.0, history_seconds=600, config=None, host="127.0.0.1", config_path=None):
    """host 是监听地址：不是本机地址（开放到局域网）时只读，写接口一律拒绝。config_path 是设置页面读写的配置文件。"""
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG
    sampler = Sampler(default_collectors(config), interval=interval, history_seconds=history_seconds)

    @asynccontextmanager
    async def lifespan(app):
        await sampler.start()
        yield
        await sampler.stop()

    app = FastAPI(title="AI InferPeek", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/api/snapshot")
    async def snapshot():
        return JSONResponse(_finite({"info": sampler.info, "history": sampler.snapshot()}))

    @app.get("/api/latest")
    async def latest():
        # 只要最新一条完整样本（还没采样时是 null）：设置页每 5 秒看一下 CPU 温度读到没有，不用拉整段历史
        sample = sampler.latest
        return JSONResponse(_finite({k: v for k, v in sample.items() if k != "slim"} if sample else None))

    runtimes = next((c for c in sampler.collectors if getattr(c, "name", None) == "runtimes"), None)
    device = next((c for c in sampler.collectors if getattr(c, "name", None) == "device"), None)

    @app.get("/api/diagnostics")
    def diagnostics():
        # 为什么没有发现某个服务或工具：后台每次发现时按工具记下的检测情况，只有进程名、地址、路径和错误信息。
        # 普通函数：FastAPI 放到线程池里跑，等后台线程的锁时不挡住推送
        return JSONResponse(runtimes.diagnostics() if runtimes else {"checked": None, "tools": []})

    readonly = None if is_loopback(host) else "看板开放到了局域网（--host 不是本机地址），这里只能查看、不能保存。要改设置请直接编辑配置文件，或者用本机地址启动"
    save_lock = threading.Lock()  # 同时只做一次保存：读文件、写文件、换配置要连在一起

    @app.get("/api/config")
    def get_config():
        # 读的是文件里现在的内容；文件坏了（启动后被手动改坏）时显示正在用的配置，并说明不能保存
        try:
            data, version = read_config_file(config_path)
            error = None
        except ConfigConflict as exc:
            data, version, error = (runtimes.config if runtimes else {}), None, str(exc)
        return JSONResponse({
            "path": str(config_path),
            "exists": config_path.is_file(),
            "version": version,
            "config": _managed(data),
            "problems": config_problems(data),
            "extra": [k for k in data if k not in CONFIG_KEYS],
            "error": error,
            "readonly": readonly,
            "tools": _tools(),
            "service_kinds": list(SERVICE_KINDS),
            "host": socket.gethostname(),  # 没给这台电脑起名时用的名字（输入框里的提示）
            "device_name_max": DEVICE_NAME_MAX,
        })

    def save(settings, version):
        with save_lock:
            try:
                data, version = update_config_file(config_path, {k: settings.get(k) for k in CONFIG_KEYS}, version)
            except ConfigInvalid as exc:
                return JSONResponse({"error": f"有 {len(exc.problems)} 处写得不对，没有保存", "problems": exc.problems}, status_code=400)
            except ConfigConflict as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            except OSError as exc:
                return JSONResponse({"error": _write_error(config_path, exc)}, status_code=500)
            if device:
                device.reconfigure(data)
            applied = runtimes.reconfigure(data, timeout=APPLY_TIMEOUT) if runtimes else False
        return JSONResponse({
            "version": version,
            "exists": True,
            "config": _managed(data),
            "applied": applied,
            "diagnostics": runtimes.diagnostics() if runtimes else None,
        })

    @app.put("/api/config")
    async def put_config(request: Request):
        # 只收 JSON：别的网页跨站发 JSON 要先过 CORS 预检，又多一道挡（来源已经由 LocalGuard 校验过）
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "只接受 application/json"}, status_code=415)
        try:
            body = json.loads(await request.body())
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("config"), dict):
            return JSONResponse({"error": '内容应该是 {"config": {...}, "version": "..."}'}, status_code=400)
        version = body.get("version") if isinstance(body.get("version"), str) else ""
        # 写文件、等新配置生效都是阻塞的，放到线程池里做
        return await run_in_threadpool(save, body["config"], version)

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events():
            # 先发静态信息和全部历史，之后每次采样推一条。now 是服务端当前时间，
            # 页面用它算最新一条样本已经多久了：连上时采样已经停住，马上就能提示
            seq = sampler.seq
            yield _event("init", {"info": sampler.info, "history": sampler.snapshot(), "now": int(time.time() * 1000)})
            while not await request.is_disconnected():
                seq, sample = await sampler.wait_next(seq, timeout=5)
                if sample is None:
                    yield ": keepalive\n\n"
                else:
                    yield _event("sample", sample)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    app.mount("/", WebFiles(directory=WEB_DIR, html=True), name="web")
    app.add_middleware(LocalGuard, host=host)
    return app
