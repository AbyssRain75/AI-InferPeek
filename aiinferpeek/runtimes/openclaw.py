"""OpenClaw（开源的个人 AI 助手，网关把它接到各种聊天软件上）。

- 是否在运行：网关进程。用 npm 装的是 node 跑 openclaw.mjs（启动参数里有 openclaw 的路径），也认 openclaw 程序
- 会话：每个 agent 一个 SQLite 库，~/.openclaw/agents/<agent id>/agent/openclaw-agent.sqlite：
  - session_nodes：每个会话键（会话）一行：当前的会话 id、标题、最后更新时间；entry_json 里取模型和上下文
  - session_windows：会话 id 对应的聊天渠道和模型
  - transcript_events：会话记录，按 seq 追加。只取消息的角色、时间、回复的 stopReason、模型和 token 用量（json_extract），
    消息内容不取出来；太大而被压缩（zstd）的回复要整个解压才能取到用量，取完就丢掉
- 最后一条消息是用户消息、工具结果或者要调用工具的回复，就是这一轮还在进行；回复的 stopReason 说明这一轮怎么结束的。
  工具调用等你确认时库里没有记录，显示的是工作中
- 子会话（subagent、spawn 出来的）不单独列出，归档的不列
- 网关一个进程服务所有会话，对不上进程和会话，所以列出最近有活动的会话
- 网关跑在 WSL 里时数据在 Linux 那边，这里看不到
"""

import glob
import json
import os
import time

from . import sqlite
from .base import prune
from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, Rate, newest_version, outcome, session, summarize, track, usage

try:
    from compression import zstd  # Python 3.14 起自带
except ImportError:
    zstd = None

DB_NAME = "openclaw-agent.sqlite"
CHUNK = 2000  # 每次最多读这么多条会话记录，很长的会话分几轮读完，不卡住轮询
# 回复的 stopReason：toolUse 是要调用工具，这一轮还没结束；没见过的值算完成
STOPS = {"error": "failed", "aborted": "interrupted"}
STOP_DETAIL = {"length": "回复超出长度上限"}
ROLES = ("user", "assistant", "toolResult")  # 看这几种消息判断状态；别的（插件插进来的消息等）不算
WORKING_ROLES = ("user", "toolResult")  # 最后一条是这些时模型还没回复

NODES_SQL = """
SELECT n.session_key, n.current_session_id, coalesce(n.label, n.display_name, json_extract(n.entry_json, '$.subject')),
       max(n.updated_at, coalesce(w.updated_at, 0), coalesce(w.transcript_updated_at, 0)),
       coalesce(json_extract(n.entry_json, '$.model'), w.model), w.channel,
       json_extract(n.entry_json, '$.totalTokens'), json_extract(n.entry_json, '$.totalTokensFresh'),
       json_extract(n.entry_json, '$.contextTokens')
FROM session_nodes n LEFT JOIN session_windows w ON w.session_id = n.current_session_id
WHERE n.archived_at IS NULL AND n.parent_session_key IS NULL AND n.spawned_by IS NULL
  AND max(n.updated_at, coalesce(w.updated_at, 0), coalesce(w.transcript_updated_at, 0)) >= ?
ORDER BY 4 DESC
LIMIT ?
"""
EVENTS_SQL = """
SELECT seq, created_at, message_role, json_extract(event_json, '$.message.stopReason'),
       json_extract(event_json, '$.message.usage.input'), json_extract(event_json, '$.message.usage.cacheRead'),
       json_extract(event_json, '$.message.usage.cacheWrite'), json_extract(event_json, '$.message.usage.output'),
       json_extract(event_json, '$.message.model'), json_extract(event_json, '$.supersedesEntryId'),
       CASE WHEN event_json IS NULL AND message_role = 'assistant' THEN event_zstd END
FROM transcript_events
WHERE session_id = ? AND seq > ? AND message_role IS NOT NULL
ORDER BY seq
LIMIT ?
"""


def default_home():
    return os.environ.get("OPENCLAW_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".openclaw")


def _unzip(blob):
    """压缩的回复：解压后只取 stopReason、用量和模型，其余丢掉。解不开时返回空的。"""
    if zstd is None:
        return {}
    try:
        event = json.loads(zstd.decompress(blob))
    except (zstd.ZstdError, ValueError):
        return {}
    msg = (event.get("message") if isinstance(event, dict) else None) or {}
    u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    return {
        "stop": msg.get("stopReason"),
        "usage": (u.get("input"), u.get("cacheRead"), u.get("cacheWrite"), u.get("output")),
        "model": msg.get("model"),
        "supersedes": event.get("supersedesEntryId"),
    }


class OpenclawSession:
    """一个会话键当前的会话记录：按 seq 往后读，累计用量，记下最后一条消息。"""

    def __init__(self, key):
        self.key = key
        self.node = None
        self._reset(None)

    def _reset(self, session_id):
        self.session_id = session_id
        self.seq = 0
        self.totals = [0, 0, 0]  # 输入（含缓存）、缓存命中、输出
        self.role = None  # 最后一条消息的角色
        self.stop = None  # 最后一条回复的 stopReason
        self.prompt = None  # 最后一条用户消息的时间（毫秒），这一轮从这时开始
        self.last = None
        self.model = None
        self.rate = Rate()
        # 还没读完（很长的会话分几轮读）：读到一半时最后一条可能是用户消息，接着读到回复会被当成一轮刚结束，
        # 所以读完之前不列出这个会话，也就不会误报完成
        self.behind = False

    def update(self, db, node):
        """读这个会话新追加的记录，返回是不是还没读完。"""
        self.node = node
        if node[1] != self.session_id:
            # 换了会话 id（/new、每天重置、空闲太久重开）：是新的一段会话记录，从头读
            self._reset(node[1])
        rows = db.execute(EVENTS_SQL, (self.session_id, self.seq, CHUNK)).fetchall()
        for seq, t, role, stop, u_in, u_read, u_write, u_out, model, supersedes, blob in rows:
            self.seq = seq
            self.last = max(self.last or 0, t or 0) or None
            if role not in ROLES:
                continue
            self.role = role
            if role == "user":
                self.prompt = t
            if role != "assistant":
                continue
            if blob is not None:
                z = _unzip(blob)
                stop, model, supersedes = z.get("stop"), z.get("model"), z.get("supersedes")
                u_in, u_read, u_write, u_out = z.get("usage") or (None,) * 4
            self.stop = stop
            self.model = model or self.model
            if supersedes:
                continue  # 改写会话记录时重新追加的旧回复，用量已经算过
            row = (u_in or 0) + (u_read or 0) + (u_write or 0), u_read or 0, u_out or 0
            for i in range(3):
                self.totals[i] += row[i]
            self.rate.add(t, row[2])
        self.behind = len(rows) == CHUNK
        return self.behind

    def row(self, agent, now):
        key, _, title, updated, model, channel, context, fresh, window = self.node
        last = max(filter(None, (updated, self.last)))
        open_turn = self.role in WORKING_ROLES or (self.role == "assistant" and self.stop == "toolUse")
        busy = open_turn and now - last < STALE * 1000
        if open_turn:
            end = None if busy else LOST
        elif self.role == "assistant":
            end = outcome(STOPS.get(self.stop, "ok"), STOP_DETAIL.get(self.stop))
        else:
            end = None
        prefix = f"agent:{agent}:"
        return session(
            id=f"{agent}/{key}",
            name=title or (key[len(prefix):] if key.startswith(prefix) else key),
            entry=channel,
            status="busy" if busy else "idle",
            since=self.prompt if busy and self.prompt else last,
            model=model or self.model,
            context=context if fresh != 0 else None,  # totalTokensFresh 为 false 时这个数不是最新的
            context_window=window,
            tokens=usage(*self.totals) if any(self.totals) else None,
            rate=self.rate.per_second(now),
            last=last,
            outcome=end,
        )


class OpenclawAgent:
    """一个 agent 的库，变了才重新查。"""

    def __init__(self, path):
        self.path = path
        self.agent = os.path.basename(os.path.dirname(os.path.dirname(path)))
        self.watch = sqlite.Watch(path)
        self.more = False  # 上次有会话记录没读完，下一轮库没变也要接着读
        self.version = None
        self.sessions = {}  # 会话键 -> OpenclawSession

    def update(self, now):
        stamp = self.watch.due(now / 1000)
        if stamp is None:
            self.sessions.clear()
            return
        if stamp is False and not self.more:
            return
        stamp = stamp or sqlite.stamp(self.path)
        try:
            with sqlite.connect(self.path) as db:
                if self.version is None:
                    self.version = newest_version(r[0] for r in db.execute("SELECT app_version FROM schema_meta"))
                nodes = db.execute(NODES_SQL, (now - RECENT * 1000, MAX_SESSIONS * 2)).fetchall()
                prune(self.sessions, {n[0] for n in nodes})
                more = False
                for node in nodes:
                    s = self.sessions.get(node[0]) or self.sessions.setdefault(node[0], OpenclawSession(node[0]))
                    more = s.update(db, node) or more
        except sqlite.Error as exc:
            if not sqlite.locked(exc):
                raise
            return  # 库被锁住：沿用上次读到的，下一轮再读
        self.watch.done(stamp, now / 1000)
        self.more = more

    def rows(self, now):
        out = []
        for s in self.sessions.values():
            if s.role is None or s.behind:
                continue  # 还没有消息的、会话记录还没读完的不列
            row = s.row(self.agent, now)
            if row["status"] != "idle" or now - row["last"] < RECENT * 1000:
                out.append(row)
        return out


class OpenclawAdapter(CliAdapter):
    kind = "openclaw"
    label = "OpenClaw"
    process_names = frozenset({"openclaw.exe", "openclaw", "node.exe", "node"})
    match_hint = "node 进程要在启动参数里带 openclaw"
    config_key = "openclaw_dir"

    def __init__(self, home=None):
        self.home = home or default_home()

    def data_paths(self):
        return [("home", self.home), ("data", os.path.join(self.home, "agents"))]

    def match_process(self, name, cmdline):
        name = name.lower()
        if name.startswith("openclaw"):
            return True
        if name not in ("node.exe", "node"):
            return False
        # npm 装的：node .../node_modules/openclaw/openclaw.mjs gateway
        for a in cmdline[1:]:
            a = a.replace("\\", "/").lower()
            if "/openclaw/" in a or a.endswith(("/openclaw", "openclaw.mjs")):
                return True
        return False

    def _scan_sessions(self, st, now):
        """各 agent 的库。"""
        return sorted(glob.glob(os.path.join(glob.escape(self.home), "agents", "*", "agent", DB_NAME)))

    def poll(self, inst):
        st = inst.state
        now = time.time() * 1000
        sessions = []
        agents = track(st.setdefault("agents", {}), self._session_paths(st, now / 1000), OpenclawAgent)
        for a in agents:
            a.update(now)
            sessions += a.rows(now)
        version = newest_version(a.version for a in agents)
        return summarize(sessions, limit=MAX_SESSIONS, version=version, home=self.home, recent=RECENT)
