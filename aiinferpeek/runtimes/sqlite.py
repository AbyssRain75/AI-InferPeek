"""只读打开别的程序的 SQLite 数据库（opencode、OpenClaw、Antigravity 的会话库，Cursor 的状态库）。

库里有对话原文。查询时用 json_extract 只取标题、状态、模型和 token 用量这些字段，消息内容不取出来。
用 mode=ro 打开，不会写入；每次查完就关，不一直占着读事务（WAL 模式下长时间不结束的读事务会让对方的日志越积越大）。
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

TIMEOUT = 0.5  # 秒，对方正在写、库被锁住时最多等这么久
# 秒，文件看起来没变也隔这么久重新查一次：修改时间是按系统时钟的刻度（十几毫秒）记的，一个刻度里写两次、
# WAL 日志合并后从头复用时，修改时间和大小可能都和上次一样
REREAD = 5.0

Error = sqlite3.Error


def connect(path):
    """只读连接，用 with 包起来，出了 with 就关掉。"""
    uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    return closing(sqlite3.connect(uri, uri=True, timeout=TIMEOUT))


def stamp(path):
    """数据库文件和 WAL 日志的 (修改时间, 大小)，和上次一样就不用重新查。数据库文件不在时返回 None。"""
    out = []
    for p in (path, path + "-wal"):
        try:
            st = os.stat(p)
        except OSError:
            out.append(None)
        else:
            out.append((st.st_mtime_ns, st.st_size))
    return tuple(out) if out[0] else None


class Watch:
    """数据库变了没有：和上次查成功时比 stamp()，没变的话隔 REREAD 秒也重新查一次。"""

    def __init__(self, path):
        self.path = path
        self.seen = None
        self.at = 0.0

    def due(self, now):
        """now 是秒。数据库文件不在时返回 None；要重新查时返回这次的 stamp，查成功后交给 done()；不用查时返回 False。"""
        s = stamp(self.path)
        if s is None:
            return None
        return s if s != self.seen or now - self.at >= REREAD else False

    def done(self, s, now):
        self.seen, self.at = s, now


def locked(exc):
    """对方正在写、库被锁住了：这一轮沿用上次读到的，下一轮再读。"""
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)
