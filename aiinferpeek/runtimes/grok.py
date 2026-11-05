"""Grok Build（xAI 的命令行编程工具；Grok 桌面版也是调用它）。

- 是否在运行：~/.grok 下的程序（bin/agent.exe 等），或别处 bin 目录里的 grok 命令。
  同名的 Grok 桌面版（Grok.exe）只是界面，不算，它启动的 agent 进程才算。
- 会话：~/.grok/sessions/<编码后的目录>/<会话 id>/
  - summary.json：标题、目录、模型、最后活动时间
  - signals.json：上下文用了多少、上限
  - events.jsonl：回合开始 / 结束（是否在工作；回合是完成还是被取消）、权限请求 / 处理（是否在等你确认）
  - updates.jsonl：每个回合结束时的 token 用量。这个文件里有对话原文，只解析 turn_completed 所在的行
- 子会话在会话目录的 subagents/ 下，不单独列出
- 一个进程可以服务多个会话，对不上进程和会话，所以列出最近有活动的会话
"""

import os
import time
from urllib.parse import unquote

import psutil

from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, Rate, outcome, read_json, session, summarize, track, usage
from .jsonl import JsonlTail, parse_ts

ASK_DELAY = 2000  # 毫秒，自动批准的工具也会记一对权限请求 / 处理，请求挂着超过这么久才算在等你确认
EVENTS = tuple(f'"type":"{t}"'.encode() for t in ("turn_started", "turn_ended", "permission_requested", "permission_resolved"))
# turn_ended 的 outcome；没有这个字段（老版本）算完成，其余没见过的值算失败
OUTCOMES = {"completed": "ok", "cancelled": "interrupted", "interrupted": "interrupted"}


class GrokSession:
    def __init__(self, path):
        self.path = path
        self.events = JsonlTail(os.path.join(path, "events.jsonl"), EVENTS)
        self.updates = JsonlTail(os.path.join(path, "updates.jsonl"), (b'"sessionUpdate":"turn_completed"',))
        self.files = {}  # summary.json / signals.json：文件名 -> (修改时间, 内容)
        self.turn = None  # 进行中的回合开始时间（毫秒）
        self.asking = None  # 挂着的权限请求时间（毫秒）
        self.ended = None  # 上一个回合怎么结束的
        self.model = None
        self.last = None
        self.totals = [0, 0, 0]  # 输入（含缓存）、缓存命中、输出
        self.rate = Rate()

    def _json(self, name):
        path = os.path.join(self.path, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return {}
        hit = self.files.get(name)
        if not hit or hit[0] != mtime:
            data = read_json(path)
            if data is None:
                # 读不到（Grok 正写到一半、文件被占着）：不记下来，下一轮再读；先用上次读到的
                return hit[1] if hit else {}
            hit = self.files[name] = (mtime, data or {})
        return hit[1]

    def update(self):
        restarted, objs = self.events.read()
        if restarted:
            self.turn = self.asking = self.ended = None
        for o in objs:
            t = parse_ts(o.get("ts"))
            if t:
                self.last = max(self.last or 0, t)
            kind = o.get("type")
            if kind == "turn_started":
                self.turn, self.asking, self.ended = t, None, None
                self.model = o.get("model_id") or self.model
            elif kind == "turn_ended":
                self.turn = self.asking = None
                result = o.get("outcome")
                self.ended = outcome(OUTCOMES.get(result, "failed") if result else "ok")
            elif kind == "permission_requested":
                self.asking = t
            elif kind == "permission_resolved":
                self.asking = None

        restarted, objs = self.updates.read()
        if restarted:
            self.totals = [0, 0, 0]
            self.rate.clear()
        for o in objs:
            u = ((o.get("params") or {}).get("update") or {}).get("usage")
            if not isinstance(u, dict):
                continue
            row = (u.get("inputTokens") or 0, u.get("cachedReadTokens") or 0, u.get("outputTokens") or 0)
            for i in range(3):
                self.totals[i] += row[i]
            ts = o.get("timestamp")
            self.rate.add(ts * 1000 if isinstance(ts, (int, float)) else None, row[2])

    def row(self, now):
        summary = self._json("summary.json")
        signals = self._json("signals.json")
        info = summary.get("info") or {}
        last = max(filter(None, (self.last, parse_ts(summary.get("last_active_at")))), default=None)
        busy = self.turn is not None and last is not None and now - last < STALE * 1000
        waiting = busy and self.asking is not None and now - self.asking >= ASK_DELAY
        return session(
            id=info.get("id") or os.path.basename(self.path),
            name=summary.get("generated_title") or summary.get("session_summary"),
            cwd=info.get("cwd") or unquote(os.path.basename(os.path.dirname(self.path))),
            status="waiting" if waiting else "busy" if busy else "idle",
            waiting_for="permission" if waiting else None,
            since=self.turn if busy else last,
            model=self.model or summary.get("current_model_id"),
            context=signals.get("contextTokensUsed"),
            context_window=signals.get("contextWindowTokens") or summary.get("context_window"),
            tokens=usage(*self.totals) if any(self.totals) else None,
            rate=self.rate.per_second(now),
            last=last,
            outcome=LOST if self.turn is not None and not busy else self.ended,
        )


class GrokBuildAdapter(CliAdapter):
    kind = "grok-build"
    label = "Grok Build"
    process_names = frozenset({"grok.exe", "grok", "agent.exe", "agent"})
    match_hint = "程序要在数据目录里，或者是 bin 文件夹里的 grok"
    config_key = "grok_dir"

    def __init__(self, home=None):
        self.home = home or os.path.join(os.path.expanduser("~"), ".grok")

    def find_processes(self, candidates):
        home = os.path.normcase(os.path.abspath(self.home)) + os.sep
        out = []
        for pid, name, cmdline in candidates:
            if name.lower() not in self.process_names:
                continue
            try:
                exe = os.path.normcase(psutil.Process(pid).exe())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if exe.startswith(home) or (name.lower().startswith("grok") and os.path.basename(os.path.dirname(exe)) == "bin"):
                out.append(pid)
        return out

    def _scan_sessions(self, st, now):
        """最近有活动的会话目录。"""
        st["version"] = (read_json(os.path.join(self.home, "version.json")) or {}).get("version")
        found = []
        try:
            folders = [de.path for de in os.scandir(os.path.join(self.home, "sessions")) if de.is_dir()]
        except OSError:
            return []
        for folder in folders:
            try:
                entries = [de for de in os.scandir(folder) if de.is_dir()]
            except OSError:
                continue
            for de in entries:
                mtimes = []
                for name in ("events.jsonl", "summary.json"):
                    try:
                        mtimes.append(os.stat(os.path.join(de.path, name)).st_mtime)
                    except OSError:
                        pass
                if mtimes and now - max(mtimes) < RECENT:
                    found.append((max(mtimes), de.path))
        found.sort(reverse=True)
        return [path for _, path in found[: MAX_SESSIONS * 2]]

    def poll(self, inst):
        st = inst.state
        now_s = time.time()
        now = now_s * 1000
        sessions = []
        for s in track(st.setdefault("sessions", {}), self._session_paths(st, now_s), GrokSession):
            s.update()
            row = s.row(now)
            if row["tokens"] or row["status"] != "idle":  # 新建后还没完成过一个回合的会话不列出
                sessions.append(row)
        return summarize(sessions, limit=MAX_SESSIONS, version=st.get("version"), home=self.home, recent=RECENT)
