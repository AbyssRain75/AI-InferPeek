"""Antigravity（Google 的 AI 编程工具，2.x 版本）。

- 是否在运行：Antigravity.exe 主进程（界面子进程 --type=... 算在它的子进程里），或者启动参数里带 antigravity 的 language_server
  （Antigravity 带的后台进程，agent 在它里面跑；这个进程名别的工具也用，所以看启动参数）。版本号来自 language_server 的启动参数
- 会话：数据目录 ~/.gemini/antigravity 里的 SQLite 库。库里大多是 protobuf，字段号和枚举值查自 2.28 版 language_server 里带的定义
  - conversation_summaries.db：每个对话一行，有标题、目录、运行状态、最后一次发消息的时间；raw_summary 列里还有上一轮怎么结束的
  - conversations/<对话 id>.db：对话的每一步（steps 表）。只取模型回复那几步的 metadata（时间和 token 用量，不含对话内容）
    和在等你操作的步骤；gen_metadata 表是每次调用模型的记录，只读做完后精简过的那些（正在生成的那条带着完整的提示词，
    有几百 KB，不读），从里面取模型的显示名和上下文上限
- 有步骤的状态是 WAITING：在等你确认（执行命令、访问工作区外的文件等）或回答问题
- 子 agent 的对话（parent_conversation_id 不为空）不单独列出
- 一个 language_server 进程服务所有对话，对不上进程和对话，所以列出最近有活动的对话
- 额度（Beta）：不写到磁盘上，只在 language_server 的内存里。它在本机开了一个 HTTPS 接口（端口随机，自签证书），
  要带启动参数里的 --csrf_token 才能调；每分钟调一次没公开的 GetUserStatus，只取每个模型的额度（剩余比例、重置时间）。
  令牌只在内存里用，不记日志、不发给别处
"""

import json
import os
import re
import ssl
import time
import urllib.request

import psutil

from ..i18n import N_
from . import protobuf, sqlite
from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, Rate, outcome, session, summarize, track, uri_path, usage
from .jsonl import parse_ts

# conversation_summaries 的 status 列（CascadeRunStatus）：运行中、正在取消、忙都算在工作，IDLE 是空闲
RUNNING = frozenset({"CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_CANCELING", "CASCADE_RUN_STATUS_BUSY"})
# raw_summary 列（CascadeTrajectorySummary）：25 interrupted，28 latest_executor_termination_reason
SUMMARY_INTERRUPTED, SUMMARY_END = 25, 28
# 上一轮怎么结束的（ExecutorTerminationReason）。没列的（4 模型不再调用工具、8 走到了结束的步骤等）是正常做完
ENDS = {
    1: outcome("failed"),
    2: outcome("interrupted"),
    3: outcome("failed", N_("达到工具调用次数上限")),
    6: outcome("failed", N_("达到工具调用次数上限")),
    11: outcome("failed", N_("超出 token 预算")),
}
# steps 表的 step_type（CortexStepType）和 status（CortexStepStatus）
PLANNER_RESPONSE = 15  # 模型的一次回复，metadata 里有这次的 token 用量
WAITING = 9
WAITING_FOR = {138: "question", 82: "review"}  # 在等的步骤是 ASK_QUESTION、NOTIFY_USER；其余是等你确认权限
# metadata 列（CortexStepMetadata）：1 created_at，9 model_usage，26 internal_metadata → 1 status_transitions（1 状态、2 时间）
# model_usage（ModelUsageStats）：2 输入（不含缓存）、3 输出（含思考）、4 写入缓存、5 缓存命中
STEPS_SQL = "SELECT step_type, status, metadata FROM steps WHERE step_type = ? OR status = ? ORDER BY idx"
# gen_metadata 的 data 列（CortexStepGeneratorMetadata）：1 chat_model（ChatModelMetadata）→ 21 显示名、19 模型 id、
# 9 chat_start_metadata → 10 context_window_metadata → 4 上下文上限
GEN_SQL = "SELECT data FROM gen_metadata WHERE length(data) < ? ORDER BY idx DESC LIMIT 1"
GEN_MAX = 64 * 1024  # 字节，精简过的一条一两 KB
SUMMARIES_SQL = """
SELECT conversation_id, title, workspace_uris, status, killed, last_modified_time, last_user_input_time, raw_summary
FROM conversation_summaries
WHERE parent_conversation_id = ''
ORDER BY last_modified_time DESC
LIMIT ?
"""
STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
QUOTA_EVERY = 60.0  # 秒，多久读一次额度
QUOTA_RETRY = 10.0  # 秒，读失败后隔这么久再试
QUOTA_KEEP = 300.0  # 秒，读失败时上次读到的额度还显示多久
QUOTA_TIMEOUT = 2.0
LEVEL = re.compile(r"\s*\([^()]*\)$")  # 模型名后面的档位：Gemini 3.8 Flash (High)

# language_server 用自签证书，只连本机的 127.0.0.1，不验证证书；不走系统代理
_tls = ssl.create_default_context()
_tls.check_hostname = False
_tls.verify_mode = ssl.CERT_NONE
_https = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_tls))


def user_status(port, token, timeout=QUOTA_TIMEOUT):
    """调 language_server 的 GetUserStatus，返回解析好的 JSON。连不上、出错时抛 OSError 或 ValueError。"""
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}{STATUS_PATH}",
        data=b"{}",
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1", "X-Codeium-Csrf-Token": token},
    )
    with _https.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def listen_ports(pid):
    """进程在本机监听的端口，从小到大。"""
    try:
        conns = psutil.Process(pid).net_connections(kind="tcp")
    except psutil.Error:
        return []
    # 没绑定地址的连接 laddr 是空的 ()，先排除掉再取 ip
    local = ("127.0.0.1", "::1", "0.0.0.0", "::")
    return sorted({c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.ip in local})


def quota_pools(status):
    """GetUserStatus 的返回 → 额度分组 [{"names", "models", "used", "resets"}]，按返回的顺序。

    每个模型带 quotaInfo（剩余比例 remainingFraction、重置时间 resetTime），几个模型共用一份额度时这两个值一样，合成一组；
    names 是去掉档位（Low、High…）后的模型名，models 是完整的名字。剩余 0 时 remainingFraction 不出现（转 JSON 时省掉了 0 值）。"""
    user = status.get("userStatus") if isinstance(status, dict) else None
    data = user.get("cascadeModelConfigData") if isinstance(user, dict) else None
    configs = data.get("clientModelConfigs") if isinstance(data, dict) else None
    groups = {}
    for c in configs if isinstance(configs, list) else []:
        quota = c.get("quotaInfo") if isinstance(c, dict) else None
        label = isinstance(quota, dict) and (c.get("label") or c.get("modelId"))
        left = quota.get("remainingFraction", 0) if label else None
        if not isinstance(left, (int, float)) or not isinstance(label, str):
            continue
        resets = parse_ts(quota.get("resetTime"))
        g = groups.setdefault((left, resets), {"names": [], "models": [], "used": round((1 - left) * 100, 1), "resets": resets})
        g["models"].append(label)
        name = LEVEL.sub("", label)
        if name not in g["names"]:
            g["names"].append(name)
    return list(groups.values())


class Quota:
    """额度：每 QUOTA_EVERY 秒调一次 language_server。记着上次能用的端口，换了进程或调不通时再按它监听的端口挨个试。"""

    def __init__(self, fetch=None, ports=None):
        self.fetch, self.ports = fetch or user_status, ports or listen_ports
        self.next = 0.0  # 下次什么时候读（秒）
        self.port = {}  # 进程号 -> 能用的端口
        self.limits = None

    def _candidates(self, pid):
        """要试的端口：先是上次能用的，调通了就不用再找；之后才列它监听的端口（要枚举整张 TCP 表，能省就省）。"""
        known = self.port.get(pid)
        if known:
            yield known
        for port in self.ports(pid):
            if port != known:
                yield port

    def update(self, servers, now):
        """servers：[(进程号, csrf 令牌)]；now 是秒。"""
        if now < self.next:
            return
        for pid, token in servers:
            for port in self._candidates(pid):
                try:
                    status = self.fetch(port, token)
                except (OSError, ValueError):
                    continue
                self.port = {pid: port}
                pools = quota_pools(status)
                self.limits = {"source": "antigravity", "t": int(now * 1000), "pools": pools} if pools else None
                self.next = now + QUOTA_EVERY
                return
        # 都调不通（刚启动、正忙、关掉了）：过一会儿再试，上次读到的再显示一阵
        self.port = {}
        self.next = now + QUOTA_RETRY
        if self.limits and now * 1000 - self.limits["t"] >= QUOTA_KEEP * 1000:
            self.limits = None


def _ts(value):
    """库里的时间（2026-10-31 06:56:31.6258013+00:00）转成毫秒；没有时是 0001 年，返回 None。"""
    t = parse_ts(value)
    return t if t and t > 0 else None


def _flag(cmdline, name):
    for i, a in enumerate(cmdline):
        if a == name and i + 1 < len(cmdline):
            return cmdline[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


def _workspace(uris):
    try:
        uris = json.loads(uris or "[]")
    except ValueError:
        return None
    return uri_path(uris[0]) if isinstance(uris, list) and uris and isinstance(uris[0], str) else None


class Conversation:
    """一个对话的库，变了才重新查：模型回复的 token 用量、在等你操作的步骤、最近一次调用的模型和上下文上限。"""

    def __init__(self, path):
        self.watch = sqlite.Watch(path)
        self.changed = None  # 库最后一次修改的时间（毫秒）
        self.totals = [0, 0, 0]  # 输入（含缓存）、缓存命中、输出
        self.context = None
        self.model = self.window = None
        self.waiting = None  # (步骤类型, 从什么时候开始等)
        self.prev_output = None
        self.rate = Rate()

    def update(self, now):
        stamp = self.watch.due(now / 1000)
        if not stamp:
            return  # 库还没建好，或者没变
        try:
            with sqlite.connect(self.watch.path) as db:
                steps = db.execute(STEPS_SQL, (PLANNER_RESPONSE, WAITING)).fetchall()
                gen = db.execute(GEN_SQL, (GEN_MAX,)).fetchone()
        except sqlite.Error as exc:
            if not sqlite.locked(exc):
                raise
            return  # 库被锁住：沿用上次读到的，下一轮再读
        self.watch.done(stamp, now / 1000)
        self.changed = max(s[0] for s in stamp if s) / 1e6
        totals = [0, 0, 0]
        self.context = self.waiting = None
        # 库里的数据解不开（ValueError，比如新版本改了字段的编码）时跳过那一条，不让它拖得整张卡片报错
        for kind, status, meta in steps:
            try:
                meta = protobuf.Message(meta)
                if status == WAITING:
                    moved = [t.time(2) for t in meta.message(26).messages(1) if t.int(1) == WAITING]
                    self.waiting = (kind, (moved and moved[-1]) or meta.time(1))
                if kind == PLANNER_RESPONSE and meta.get(9) is not None:  # 还在生成的回复没有用量
                    u = meta.message(9)
                    sent = (u.int(2) or 0) + (u.int(4) or 0) + (u.int(5) or 0)
                    totals[0] += sent
                    totals[1] += u.int(5) or 0
                    totals[2] += u.int(3) or 0
                    self.context = sent
            except ValueError:
                continue
        if self.prev_output is not None:
            self.rate.add(now, totals[2] - self.prev_output)  # 第一次读到的是历史，不算
        self.prev_output = totals[2]
        self.totals = totals
        if gen:
            try:
                chat = protobuf.Message(gen[0]).message(1)
                model, window = chat.text(21) or chat.text(19), chat.message(9).message(10).int(4)
            except ValueError:
                model = window = None  # 沿用上次读到的
            self.model = model or self.model
            self.window = window or self.window

    def row(self, summary, now):
        cid, title, uris, status, killed, modified, prompt, raw = summary
        try:
            raw = protobuf.Message(raw)
        except ValueError:
            raw = protobuf.Message()  # 解不开时当作没有记录怎么结束的，按正常做完算
        prompt = _ts(prompt)
        last = max(filter(None, (_ts(modified), prompt, self.changed)), default=None)
        running = status in RUNNING and not killed
        busy = running and last is not None and now - last < STALE * 1000
        waiting = busy and self.waiting is not None
        if running:
            end = None if busy else LOST
        elif killed or raw.int(SUMMARY_INTERRUPTED):
            end = outcome("interrupted")
        else:
            end = ENDS.get(raw.int(SUMMARY_END), outcome("ok"))
        return session(
            id=cid,
            name=title,
            cwd=_workspace(uris),
            status="waiting" if waiting else "busy" if busy else "idle",
            waiting_for=WAITING_FOR.get(self.waiting[0], "permission") if waiting else None,
            since=(self.waiting[1] or prompt or last) if waiting else (prompt or last) if busy else last,
            turn_start=prompt if busy else None,
            model=self.model,
            context=self.context,
            context_window=self.window,
            tokens=usage(*self.totals) if any(self.totals) else None,
            rate=self.rate.per_second(now),
            last=last,
            outcome=end,
        )


class AntigravityAdapter(CliAdapter):
    kind = "antigravity"
    label = "Antigravity"
    process_names = frozenset({"antigravity.exe", "antigravity", "language_server.exe", "language_server"})
    match_hint = N_("language_server 的启动参数中需包含 antigravity")
    config_key = "antigravity_dir"

    def __init__(self, home=None):
        self.home = home or os.path.join(os.path.expanduser("~"), ".gemini", "antigravity")
        self.db = os.path.join(self.home, "conversation_summaries.db")
        self.version = None
        self.servers = []  # [(language_server 的进程号, csrf 令牌)]，读额度用

    def data_paths(self):
        return [("home", self.home), ("db", self.db)]

    def match_process(self, name, cmdline):
        name = name.lower()
        if name.startswith("antigravity"):
            return not any(a.startswith("--type=") for a in cmdline[1:])
        return name in self.process_names and any("antigravity" in a.lower() for a in cmdline)

    def find_processes(self, candidates):
        found = [(pid, cmdline) for pid, name, cmdline in candidates if self.match_process(name, cmdline)]
        # 版本号在 language_server 的启动参数里，Antigravity.exe 的没有
        self.version = next(filter(None, (_flag(c, "--override_ide_version") for _, c in found)), self.version)
        self.servers = [(pid, token) for pid, c in found if (token := _flag(c, "--csrf_token"))]
        return [pid for pid, _ in found]

    def _read(self, now):
        """最近有活动的对话（conversation_summaries 的行）。"""
        with sqlite.connect(self.db) as db:
            rows = db.execute(SUMMARIES_SQL, (MAX_SESSIONS * 2,)).fetchall()
        return [r for r in rows if now - max(filter(None, (_ts(r[5]), _ts(r[6]))), default=0) < RECENT * 1000]

    def poll(self, inst):
        st = inst.state
        now_s = time.time()
        now = now_s * 1000
        watch = st.get("watch") or st.setdefault("watch", sqlite.Watch(self.db))
        stamp = watch.due(now_s)
        if stamp is None:
            st["rows"] = []
        elif stamp:
            try:
                st["rows"] = self._read(now)
                watch.done(stamp, now_s)
            except sqlite.Error as exc:
                if not sqlite.locked(exc):
                    raise
        rows = st.setdefault("rows", [])  # 库被锁住时沿用上次读到的，下一轮再读
        folder = os.path.join(self.home, "conversations")
        conversations = track(st.setdefault("sessions", {}), [r[0] for r in rows], lambda cid: Conversation(os.path.join(folder, f"{cid}.db")))
        sessions = []
        for c, r in zip(conversations, rows):
            c.update(now)
            row = c.row(r, now)
            if row["tokens"] or row["status"] != "idle":  # 还没回复过的对话不列出
                sessions.append(row)
        quota = st.get("quota") or st.setdefault("quota", Quota())
        quota.update(self.servers, now_s)
        return summarize(sessions, limit=MAX_SESSIONS, version=self.version, home=self.home, recent=RECENT, limits=quota.limits)
