"""增量读取一直在追加的 JSONL 文件（Claude Code、Codex 的会话记录）。

会话记录里有对话原文。这里先按字节匹配标记，只解析含有标记的行，调用方也只取用量和状态字段，其余内容直接丢弃。
每次打开读完就关，不占着文件。
"""

import json
import os
from datetime import datetime

CHUNK = 16 * 1024 * 1024  # 每次最多读这么多，很大的文件分几次读完，不卡住轮询


def parse_ts(value):
    """ISO 时间（结尾可能是 Z）转成毫秒，解析不了返回 None。"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except (AttributeError, ValueError):
        return None


def version_key(v):
    """"2.1.308" 这样的版本号转成可以比较大小的元组。"""
    return tuple(int(x) if x.isdigit() else 0 for x in str(v or "").split("-")[0].split("."))


class JsonlTail:
    def __init__(self, path, markers):
        self.path = path
        self.markers = tuple(markers)
        self.ident = None
        self.pos = 0
        self.rest = b""

    def read(self):
        """返回 (是否从头重读, 新增的对象列表)。文件被截断或替换时从头重读，调用方应清空之前的统计。"""
        try:
            st = os.stat(self.path)
        except OSError:
            return False, []
        # 同名文件被替换（删掉重建、改名覆盖）后 (st_dev, st_ino) 会变，新文件比旧的大也能发现
        ident, size = (st.st_dev, st.st_ino), st.st_size
        restarted = (self.ident is not None and ident != self.ident) or size < self.pos
        self.ident = ident
        if restarted:
            self.pos, self.rest = 0, b""
        if size == self.pos:
            return restarted, []
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                if (st.st_dev, st.st_ino) != ident:
                    return restarted, []  # stat 之后刚好又被替换，下一轮再从头读
                f.seek(self.pos)
                chunk = f.read(min(size - self.pos, CHUNK))
        except OSError:
            return restarted, []
        self.pos += len(chunk)
        lines = (self.rest + chunk).split(b"\n")
        self.rest = lines.pop()
        out = []
        for raw in lines:
            if not any(m in raw for m in self.markers):
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return restarted, out
