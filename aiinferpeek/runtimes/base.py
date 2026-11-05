"""部署系统适配器的公共部分。

每种部署系统（Ollama、ComfyUI…）写一个 Adapter 子类：
- process_names / match_process()：从进程名和启动参数认出它；还要看启动目录等其他信息的改写 match_pid()
- default_ports：优先探测的端口
- probe(url)：确认这个地址确实是它，返回版本等信息，不是则返回 None
- poll(inst)：每秒调用一次，返回要显示的数据（dict）；_ 开头的键不发给页面，只留给 finished() 比较
- finished(prev, cur)（可选）：比较前后两次 poll() 的结果，找出刚完成的任务，页面上提示“xxx 已完成”

命令行工具（Claude Code、Codex…）没有 HTTP 接口，category 设为 "cli"：
- find_processes(candidates)：返回本机正在运行的进程号，本机所有进程合成一个实例
- poll(inst)：读本地的会话文件

页面上“为什么没有发现”用到的（可选）：match_hint、config_key、data_paths()
"""

import json
import os
import urllib.error
import urllib.request

# 不走系统代理：本机服务经过代理会失败或变慢
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HttpStatusError(OSError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def prune(cache, keep):
    """dict 里只留键在 keep 里的项，其余删掉。适配器在 inst.state 里按进程、会话、任务记的缓存用它清理。"""
    for key in [k for k in cache if k not in keep]:
        del cache[key]


def request_json(url, data=None, timeout=1.0):
    body = None if data is None else json.dumps(data).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code) from None


class Instance:
    """一个正在运行（或配置文件里指定）的部署系统实例。"""

    def __init__(self, adapter, url, main_pid=None, configured=False):
        self.adapter = adapter
        self.url = url
        self.main_pid = main_pid
        self.configured = configured
        self.roots = [main_pid] if main_pid else []  # 本机进程；pids 是它们加上全部子进程
        self.pids = list(self.roots)
        self.children = {}  # pids 里的进程号 -> 它的直接子进程号（也都在 pids 里），和 pids 一起每轮更新；没有子进程的不列
        self.meta = {}  # probe() 返回的信息
        self.state = {}  # 适配器自己的状态
        self.done = []  # 最近完成的任务（finished() 找出来的）

    @property
    def id(self):
        if self.url:
            host = self.url.split("://", 1)[-1].rstrip("/")
            return f"{self.adapter.kind}@{host}"
        if self.main_pid:
            return f"{self.adapter.kind}@pid{self.main_pid}"
        return f"{self.adapter.kind}@local"

    @property
    def local(self):
        return bool(self.roots)

    def get(self, path, timeout=1.0):
        return request_json(self.url + path, timeout=timeout)

    def post(self, path, data, timeout=2.0):
        return request_json(self.url + path, data=data, timeout=timeout)


class Adapter:
    kind = ""
    label = ""
    category = "service"  # "service"：模型服务，读 HTTP 接口；"cli"：命令行工具，读本地会话文件
    process_names = frozenset()
    default_ports = ()
    match_hint = ""  # 除了进程名还要满足什么条件，没有找到进程时页面上说明
    config_key = None  # aiinferpeek.json 里改数据目录（或日志位置）的键
    has_balance = False  # 读得到账户余额（按量付费的工具），设置页面上给它填余额预警
    # 设置保存后可以直接换上的属性（不用重建适配器、不丢状态）：数据位置没变时把新建的适配器上的这些值抄到原来的上
    settings = ()

    def data_paths(self):
        """要读的本地数据 [(用途, 路径)]，从外到里排；用途是 home（数据目录）、data（会话文件夹）、db（会话数据库文件）或 log（日志文件）。
        诊断时只看在不在、能不能打开，不读内容。命令行工具默认是数据目录（home）和里面的 sessions 文件夹，不一样的自己改。"""
        home = getattr(self, "home", None)
        if self.category == "cli" and home:
            return [("home", home), ("data", os.path.join(home, "sessions"))]
        return []

    def match_process(self, name, cmdline):
        return name.lower() in self.process_names

    def match_pid(self, pid, name, cmdline):
        """模型服务发现进程时用的，默认只看进程名和启动参数。"""
        return self.match_process(name, cmdline)

    def find_processes(self, candidates):
        """命令行工具用：candidates 是 [(pid, 进程名, 启动参数)]（进程名在 process_names 里的），返回属于它的进程号。"""
        return [pid for pid, name, cmdline in candidates if self.match_process(name, cmdline)]

    def probe(self, url):
        raise NotImplementedError

    def poll(self, inst):
        raise NotImplementedError

    def finished(self, prev, cur):
        """比较前后两次 poll() 的结果，返回这一秒里新结束的任务：
        [{"key", "name", "result", "t", "duration", "detail"}]。result 是 ok（完成）、failed（失败）、
        interrupted（被中断）或 lost（很久没有新记录，可能已经中断）。key 相同的（同一个会话、同一类任务）页面上合成一条。"""
        return []
