"""opencode（开源的命令行编程工具；opencode 桌面版也是调用它）。

- 是否在运行：opencode 进程（命令行 opencode、后台服务，桌面版 OpenCode 和它带的 opencode-cli）。
  桌面版的界面子进程（--type=...）算在主进程的子进程里
- 会话：数据目录（~/.local/share/opencode）里的 opencode.db。2.0 起会话都存在这个 SQLite 库里：
  - session_v2：每个会话一行，有标题、目录、模型、token 合计，以及上一轮什么时候结束（time_idle）、怎么结束的（idle_outcome）
  - session_message：会话里的每条消息。只取用户消息的时间（这一轮什么时候开始）、最后一条消息的更新时间（还在生成的回复
    会一直更新它）和最后一条回复的 token 用量（上下文），用 json_extract 取这几个字段，消息内容不取出来
- 最后一条用户消息比上一轮结束得晚，就是这一轮还在进行。工具调用等你确认、等你回答问题时库里没有记录，显示的是工作中
- 子会话（subagent）也有自己的一行（parent_id 不为空），不单独列出
- 后台服务一个进程服务所有会话（命令行、桌面版、网页都连它），对不上进程和会话，所以列出最近有活动的会话
"""

import os
import time

from . import sqlite
from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, Rate, newest_version, outcome, session, summarize, track, usage

# idle_outcome：这一轮怎么结束的。opencode 退出时被打断的回合不写结束（库里记着要在下次启动时接着跑），
# 这里也算没有结束，30 分钟没有新记录后提示“可能已中断”
OUTCOMES = {"succeeded": "ok", "failed": "failed", "interrupted": "interrupted"}

SESSIONS_SQL = """
SELECT s.id, s.title, s.directory, s.version, json_extract(s.model, '$.id'),
       s.tokens_input, s.tokens_output, s.tokens_reasoning, s.tokens_cache_read, s.tokens_cache_write,
       s.time_updated, s.time_idle, s.idle_outcome,
       (SELECT max(m.time_created) FROM session_message m WHERE m.session_id = s.id AND m.type = 'user'),
       (SELECT m.time_updated FROM session_message m WHERE m.session_id = s.id ORDER BY m.seq DESC LIMIT 1)
FROM session_v2 s
WHERE s.parent_id IS NULL AND s.time_archived IS NULL AND max(s.time_updated, coalesce(s.time_idle, 0)) >= ?
ORDER BY max(s.time_updated, coalesce(s.time_idle, 0)) DESC
LIMIT ?
"""
# 最后一条带用量的回复：上下文是它发给模型的 tokens
REPLY_SQL = """
SELECT json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.cache.read'), json_extract(data, '$.tokens.cache.write'),
       json_extract(data, '$.model.id')
FROM session_message
WHERE session_id = ? AND type = 'assistant' AND json_extract(data, '$.tokens') IS NOT NULL
ORDER BY seq DESC
LIMIT 1
"""


def default_home():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "opencode")


class OpencodeSession:
    """一个会话最近读到的一行；输出速度用两次读取之间 token 合计的增量算。"""

    def __init__(self, sid):
        self.id = sid
        self.data = None
        self.reply = None
        self.prev_output = None
        self.rate = Rate()

    def update(self, data, reply, now):
        self.data, self.reply = data, reply
        out = (data[6] or 0) + (data[7] or 0)  # 推理的 tokens 也是模型生成的，算在输出里
        if self.prev_output is not None:
            self.rate.add(now, out - self.prev_output)  # 第一次读到的是历史，不算
        self.prev_output = out

    def row(self, now):
        _, title, cwd, _, model, t_in, t_out, t_reason, t_read, t_write, updated, idle, ended, prompt, message_at = self.data
        ctx_in, ctx_read, ctx_write, reply_model = self.reply or (None,) * 4
        last = max(filter(None, (updated, idle, prompt, message_at)))
        open_turn = prompt is not None and prompt > (idle or 0)
        busy = open_turn and now - last < STALE * 1000
        context = (ctx_in or 0) + (ctx_read or 0) + (ctx_write or 0) if self.reply else None
        tokens = (t_in or 0) + (t_read or 0) + (t_write or 0), t_read, (t_out or 0) + (t_reason or 0)
        if open_turn:
            end = None if busy else LOST
        else:
            end = outcome(OUTCOMES.get(ended, "failed")) if ended else None
        return session(
            id=self.id,
            name=title,
            cwd=cwd,
            status="busy" if busy else "idle",
            since=prompt if busy else last,
            model=model or reply_model,
            context=context or None,
            tokens=usage(*tokens) if any(tokens) else None,
            rate=self.rate.per_second(now),
            last=last,
            outcome=end,
        )


class OpencodeAdapter(CliAdapter):
    kind = "opencode"
    label = "OpenCode"
    process_names = frozenset({"opencode.exe", "opencode", "opencode-cli.exe", "opencode-cli"})
    config_key = "opencode_dir"

    def __init__(self, home=None):
        self.home = home or default_home()
        self.db = os.path.join(self.home, "opencode.db")

    def data_paths(self):
        return [("home", self.home), ("db", self.db)]

    def match_process(self, name, cmdline):
        # 桌面版的界面子进程（--type=renderer 等）算在主进程的子进程里
        return name.lower() in self.process_names and not any(a.startswith("--type=") for a in cmdline[1:])

    def _read(self, now):
        """最近有活动的会话 [(会话那一行, 最后一条回复的用量)]。"""
        with sqlite.connect(self.db) as db:
            rows = db.execute(SESSIONS_SQL, (now - RECENT * 1000, MAX_SESSIONS * 2)).fetchall()
            return [(r, db.execute(REPLY_SQL, (r[0],)).fetchone()) for r in rows if r[13] is not None]  # 还没发过消息的不列

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
        sessions = []
        for s, (data, reply) in zip(track(st.setdefault("sessions", {}), [r[0][0] for r in rows], OpencodeSession), rows):
            if s.data != data or s.reply != reply:
                s.update(data, reply, now)
            row = s.row(now)
            if row["status"] != "idle" or now - row["last"] < RECENT * 1000:
                sessions.append(row)
        version = newest_version(data[3] for data, _ in rows)
        return summarize(sessions, limit=MAX_SESSIONS, version=version, home=self.home, recent=RECENT)
