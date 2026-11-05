"""命令行工具（Claude Code、Codex、DeepSeek Harness、Grok Build、OpenCode、OpenClaw、Antigravity）共用的部分：会话一行的字段、输出速度、卡片汇总、完成提示。"""

import json
import os
import time
from collections import deque

from .base import Adapter, prune
from .jsonl import version_key

RATE_WINDOW = 60.0  # 秒，输出速度取最近这段时间的平均（tokens/s）
RECENT = 3 * 3600  # 秒，对不上进程和会话的工具，列出最近这段时间里有活动的会话
STALE = 30 * 60  # 秒，回合开始后这么久没有任何新记录，当作已经中断（进程被结束时不会写回合结束）
SCAN_EVERY = 10.0  # 秒，列最近有活动的会话的工具多久重新找一次会话文件
MAX_SESSIONS = 10
STATUS_ORDER = {"waiting": 0, "busy": 1, "idle": 2}
# since 是现在这个状态从什么时候开始的（页面上显示“工作中 · 多久”）；turn_start 是这一轮从什么时候开始工作的，
# 状态在工作中和等你操作之间切换时 since 会变而它不变，完成提示的用时从它算起，没有时用 since。
# started 是会话从什么时候开始的。remote 为 True 的会话（ChatGPT 里选在本机执行的任务）对话在服务器上，
# 本机只看得到开始时间和这一轮是不是还在进行，其余字段是看不到，不是没有
FIELDS = (
    "id", "pid", "name", "cwd", "entry", "status", "waiting_for", "since", "turn_start", "started", "model", "context", "context_window",
    "tokens", "rate", "last", "outcome", "remote",
)


def outcome(result, detail=None):
    """上一个回合怎么结束的：ok 完成、failed 失败、interrupted 被中断、lost 很久没有新记录（可能已经中断）。"""
    return {"result": result, "detail": detail}


LOST = outcome("lost", f"{STALE // 60} 分钟没有新记录")


def folder_name(path):
    return os.path.basename((path or "").rstrip("\\/"))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def newest_version(versions):
    """几个版本号里最新的（空的不算），一个都没有时返回 None。"""
    versions = [v for v in versions if v]
    return max(versions, key=version_key) if versions else None


def track(cache, keys, factory):
    """cache 只跟踪 keys（比如最近有活动的会话文件）：不在里面的删掉，新出现的用 factory(key) 新建。
    按 keys 的顺序返回跟踪的对象。"""
    prune(cache, keys)
    for key in keys:
        if key not in cache:
            cache[key] = factory(key)
    return [cache[key] for key in keys]


def session(**fields):
    """一行会话。没给的字段是 None，前端显示“—”；没有标题时用目录名。"""
    s = dict.fromkeys(FIELDS)
    s.update(status="idle", rate=0)
    s.update(fields)
    s["name"] = s["name"] or folder_name(s["cwd"]) or (s["id"] or "")[:8]
    return s


def usage(input=0, cached=0, output=0):
    """input 包含缓存命中的部分，cached 是其中缓存命中的部分。"""
    return {"input": input or 0, "cached": cached or 0, "output": output or 0}


def summarize(sessions, limit=None, **extra):
    """卡片数据：有会话等你操作时状态是 waiting，其次 busy；输出速度是各会话之和。
    页面上最多列 limit 个会话；_all 是全部会话，给 finished() 用：刚结束的会话变成空闲后可能被挤出列表，也要提示完成。"""
    ranked = sorted(sessions, key=lambda s: (STATUS_ORDER[s["status"]], -(s["last"] or 0)))
    sessions = ranked[:limit]
    statuses = {s["status"] for s in sessions}
    return {
        "status": "waiting" if "waiting" in statuses else "busy" if "busy" in statuses else "idle",
        "sessions": sessions,
        "speed": round(sum(s["rate"] for s in sessions), 1),
        "_all": ranked,
        **extra,
    }


class CliAdapter(Adapter):
    """命令行工具的公共基类：poll() 返回 summarize() 的结果。

    对不上进程和会话、列最近有活动的会话的工具（Codex、DeepSeek Harness、Grok Build、OpenClaw）实现 _scan_sessions()，
    poll() 里用 _session_paths() 取会话文件，不用每秒都遍历一遍文件夹。"""

    category = "cli"

    def _scan_sessions(self, st, now):
        """最近有活动的会话文件（或文件夹），从新到旧。版本这类不常变的信息也在这时读，记在 st（inst.state）里。now 是秒。"""
        raise NotImplementedError

    def _session_paths(self, st, now):
        """_scan_sessions() 找到的会话文件：每 SCAN_EVERY 秒重新找一次，其余时候用上次找到的。"""
        if now - st.get("scan_t", 0) >= SCAN_EVERY:
            st["paths"] = self._scan_sessions(st, now)
            st["scan_t"] = now
        return st["paths"]

    def finished(self, prev, cur):
        """会话从工作中（或等你操作）变成空闲，算这一轮结束；结果看会话的 outcome，没有时算完成。
        用时从这一轮开始工作算起（会话行的 turn_start，没有时用 since），很久没有新记录的算到最后一条记录为止。
        remote 的会话不提示：看不到这一轮是完成、出错还是被中断。比较的是全部会话（_all），不只是页面上列出的。"""
        now = time.time() * 1000
        before = {s["id"]: s for s in prev.get("_all", prev.get("sessions")) or []}
        out = []
        for s in cur.get("_all", cur.get("sessions")) or []:
            p = before.get(s["id"])
            if p and not s.get("remote") and p["status"] != "idle" and s["status"] == "idle":
                end = s.get("outcome") or outcome("ok")
                since = p.get("turn_start") or p.get("since")
                stop = s["last"] if end["result"] == "lost" and s["last"] else now
                out.append({
                    "key": s["id"],
                    "name": s["name"],
                    "result": end["result"],
                    "t": now,
                    "duration": stop - since if since else None,
                    "detail": end["detail"],
                })
        return out


class Rate:
    """最近 RATE_WINDOW 秒里平均每秒新增的输出 tokens。"""

    def __init__(self):
        self._items = deque()

    def clear(self):
        self._items.clear()

    def add(self, t, n):
        if t and n > 0:
            self._items.append((t, n))

    def per_second(self, now):
        while self._items and self._items[0][0] < now - RATE_WINDOW * 1000:
            self._items.popleft()
        return round(sum(n for _, n in self._items) / RATE_WINDOW, 1)
