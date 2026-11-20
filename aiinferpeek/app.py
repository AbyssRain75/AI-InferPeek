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
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, i18n
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
from .i18n import tr
from .runtimes import default_adapters
from .sampler import Sampler
from .update import RELEASES_PAGE, UpdateChecker, UpdateError

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


def port_answering(host, port, timeout=0.3):
    """要监听 host:port 之前，看这个端口上现在是不是已经有程序在接连接（网页版和桌面版挑端口时都用）。
    光看绑不绑得上不够：Windows 上具体地址和通配地址不算冲突，别的程序用 0.0.0.0 或 [::]（双栈）监听着某个端口时，
    127.0.0.1 的同一个端口照样绑得上（反过来也一样），结果两个程序同时在一个端口上，本机用 127.0.0.1 访问到的是绑具体地址的那个。
    host 是具体的地址就连它，是 0.0.0.0、::、localhost 这类就连 127.0.0.1；连得上就是有人占着。
    Windows 上连本机没人监听的端口不会马上被拒绝，要重试 2 秒左右才报错，所以超时设得很短，超时和被拒绝都算没人占；
    没人占时要白等这一次超时，所以自己绑端口的地方先绑一下，绑不上就不用再问了。"""
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
        target = "127.0.0.1" if addr.is_unspecified else str(addr)
    except ValueError:
        target = "127.0.0.1"
    try:
        socket.create_connection((target, port), timeout=timeout).close()
    except OSError:  # 超时（TimeoutError）、被拒绝都是 OSError
        return False
    return True


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


def lan_names():
    """开放到局域网时认的本机名字：计算机名和完整计算机名（带 DNS 后缀），以及各自加 .local 的写法。
    Windows 上本机的名字在本地解析，很快；在 create_app() 里（启动时）算好交给 LocalGuard，不在事件循环线程里算。"""
    names = set()
    for name in (socket.gethostname(), socket.getfqdn()):
        names |= {name.lower(), f"{name.lower()}.local"}
    return names


class LocalGuard:
    """ASGI 中间件：按上面的规则拒绝请求。host 是监听地址；端口取这次连接的本地端口（scope["server"]）。
    names 是开放到局域网时认的本机名字（见 lan_names()），不传时自己算。"""

    def __init__(self, app, host="127.0.0.1", names=None):
        self.app = app
        self.local_only = is_loopback(host)
        # 本机能用的名字；监听的是别的回环地址（比如 127.0.0.2）时也认它
        self.names = set(LOOPBACK_NAMES)
        if self.local_only and host.lower() != "localhost":
            self.names.add(host.strip("[]").lower())
        # 开放到局域网时别的电脑用 IP 或本机的计算机名访问；域名一律不认，DNS 重绑定用的是域名
        self.lan_names = set()
        if not self.local_only:
            self.lan_names = set(names) if names is not None else lan_names()

    def host_ok(self, value, port):
        name, p = _split_host(value)
        if name is None or p != port:
            return False
        return name in self.names or (not self.local_only and (_is_ip(name) or name in self.lan_names))

    def write_problem(self, headers, client, port):
        """会改动东西的请求不能放行的原因，可以放行返回 None。"""
        if not self.local_only:
            return tr("看板已开放到局域网（--host 不是本机地址），无法在网页上修改设置")
        if not client or not is_loopback(client[0]):
            return tr("仅接受来自本机的修改")
        origin = headers.get("origin", "")
        if not origin:
            return tr("请求未携带 Origin，仅接受来自本页面的修改")
        name, p = _split_host(origin[len("http://"):]) if origin.lower().startswith("http://") else (None, None)
        if name not in self.names or p != port:
            return tr("请求来自其他网页（{origin}），已拒绝", origin=origin)
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        hosts = [v.decode("latin-1") for k, v in scope["headers"] if k == b"host"]
        port = (scope.get("server") or (None, None))[1]
        if len(hosts) != 1 or port is None or not self.host_ok(hosts[0], port):
            text = tr("Host 不正确：请使用 http://127.0.0.1:端口 或 http://localhost:端口 打开") if self.local_only else tr("Host 不正确：请使用 IP 地址或计算机名打开")
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
        "language": data.get("language"),
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
        return tr("写入 {path} 失败：{reason}", path=path, reason=reason)
    where = "" if FROZEN else tr("，也可用 --config 指定其他位置的配置文件")
    text = tr(
        "无权限写入 {path}（{reason}）。文件可能为只读，或程序所在的文件夹不允许写入（例如位于 Program Files 中）："
        "可取消文件的只读属性，或将程序文件夹移至其他位置",
        path=path,
        reason=reason,
    )
    return text + where


def create_app(interval=1.0, history_seconds=600, config=None, host="127.0.0.1", config_path=None, updates=None):
    """host 是监听地址：不是本机地址（开放到局域网）时只读，写接口一律拒绝。config_path 是设置页面读写的配置文件。
    updates 是检查新版本用的 UpdateChecker（看板顶栏的“检查更新”）：桌面版传托盘用的那个，不传时新建一个只在页面上点了才查的。"""
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG
    i18n.set_language((config or {}).get("language"))
    sampler = Sampler(default_collectors(config, interval), interval=interval, history_seconds=history_seconds)

    @asynccontextmanager
    async def lifespan(app):
        await sampler.start()
        yield
        await sampler.stop()

    app = FastAPI(title="AI InferPeek", lifespan=lifespan, docs_url=None, redoc_url=None)

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

    local = is_loopback(host)

    def readonly():
        if local:
            return None
        return tr("看板已开放到局域网（--host 不是本机地址），此处仅可查看，无法保存。如需修改设置，请直接编辑配置文件，或使用本机地址启动")

    save_lock = threading.Lock()  # 同时只做一次保存：读文件、写文件、换配置要连在一起

    # ---------- 版本号和检查更新（看板顶栏） ----------
    # 桌面版用托盘的那个 UpdateChecker，自动检查查到的新版本这里也看得到，在这里查到的托盘菜单里也有；
    # 命令行版自己建一个，不自动检查，只在页面上点了“检查更新”才联网
    updates = updates or UpdateChecker(lambda: False, lambda version, url: None)

    def update_state(error=None):
        latest = updates.latest
        return {
            "version": __version__,
            "latest": {"version": latest[0], "url": latest[1]} if latest else None,  # 查到过的比现在新的版本
            "releases": RELEASES_PAGE,
            "can_check": local,  # 开放到局域网时不让网页叫它联网（写接口一律拒绝，见 LocalGuard）
            "error": error,  # 这次没查成的原因
        }

    @app.get("/api/update")
    def get_update():
        return JSONResponse(update_state())

    @app.post("/api/update/check")
    def check_update():
        # 普通函数：最多等 GitHub 十几秒，FastAPI 放到线程池里跑。别的网页、局域网发来的已经被 LocalGuard 挡掉
        try:
            updates.check(quiet=True)
        except UpdateError as exc:
            return JSONResponse(update_state(str(exc)))
        return JSONResponse(update_state())

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
            "readonly": readonly(),
            "tools": _tools(),
            "service_kinds": list(SERVICE_KINDS),
            "host": socket.gethostname(),  # 没给这台电脑起名时用的名字（输入框里的提示）
            "device_name_max": DEVICE_NAME_MAX,
            "system_language": i18n.system_language(),  # 跟随系统时用哪种语言（选项后面写上）
        })

    def save(settings, version):
        with save_lock:
            try:
                data, version = update_config_file(config_path, {k: settings.get(k) for k in CONFIG_KEYS}, version)
            except ConfigInvalid as exc:
                return JSONResponse({"error": tr("有 {n} 处填写有误，未保存", n=len(exc.problems)), "problems": exc.problems}, status_code=400)
            except ConfigConflict as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            except OSError as exc:
                return JSONResponse({"error": _write_error(config_path, exc)}, status_code=500)
            i18n.set_language(data.get("language"))  # 页面收到结果后自己刷新，换成新的语言
            if device:
                device.reconfigure(data)
            applied = runtimes.reconfigure(data, timeout=APPLY_TIMEOUT) if runtimes else False
            if runtimes:
                # 能识别哪些工具（看板空着时列出来的）在连接时推给页面的静态信息里，关掉或打开了工具要跟着换，
                # 之后连上来的页面拿到的才是新的。整个换成新的字典，不改正在推给别的页面的那个
                sampler.info = {**sampler.info, "runtimes": runtimes.info()}
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
            return JSONResponse({"error": tr("仅接受 application/json")}, status_code=415)
        try:
            body = json.loads(await request.body())
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("config"), dict):
            return JSONResponse({"error": tr('内容应为 {"config": {...}, "version": "..."}')}, status_code=400)
        version = body.get("version") if isinstance(body.get("version"), str) else ""
        # 写文件、等新配置生效都是阻塞的，放到线程池里做
        return await run_in_threadpool(save, body["config"], version)

    @app.get("/api/i18n.js")
    def i18n_script():
        # 页面 <head> 里同步加载：现在的界面语言和译文放进 window.I18N，web/i18n.js 直接用。
        # 不是中文时先把页面藏起来，web/i18n.js 换好静态文字再显示，免得闪一下中文
        lang = i18n.current()
        data = {"lang": lang, "messages": {} if lang == i18n.SOURCE else i18n.messages()}
        text = f"window.I18N = {json.dumps(data, ensure_ascii=False)};\n"
        if lang != i18n.SOURCE:
            text += "document.documentElement.classList.add('i18n-pending');\n"
        return Response(text, media_type="text/javascript; charset=utf-8", headers={"Cache-Control": "no-cache"})

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
    # 中间件要到第一个请求才构造，局域网模式认的本机名字在这里（启动时）先算好，不在事件循环线程里查
    app.add_middleware(LocalGuard, host=host, names=None if local else lan_names())
    return app
