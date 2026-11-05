"""Codex（命令行版和桌面版共用的 codex 程序）。

- 是否在运行：codex 进程（桌面版的界面进程不算）
- 会话：~/.codex/sessions/年/月/日/rollout-*.jsonl。只解析事件行：
  task_started / task_complete / turn_aborted（是否在工作；回合是完成、出错还是被中断）、token_count（用量、上下文、额度）、
  turn_context（模型）、session_meta（目录、版本）。对话原文所在的行不解析。
- 标题：~/.codex/session_index.jsonl
- 桌面版一个 codex 进程服务所有会话，对不上进程和会话，所以列出最近有活动的会话
- ChatGPT 里选在本机执行的任务（Codex 应用首页的“工作”模式）：对话和模型在 ChatGPT 服务器上，本机只有一个
  `codex exec-server --remote` 进程替它执行命令，不写会话文件。你每发一条消息，exec-server 就为这一轮启动几个工具进程，
  做完就关掉；其中一个的启动参数带着 --interaction-client-id local-chatgpt:<对话 id>。这组进程开着就是这一轮在进行，
  开始时间是它们的启动时间。关掉后这个对话照样列出，显示空闲。标题、目录、模型、用量和这一轮怎么结束的都看不到
"""

import os
import time

import psutil

from .base import prune
from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, Rate, newest_version, outcome, session, summarize, track, usage
from .jsonl import JsonlTail, parse_ts

TASK_FLAG = "--interaction-client-id"
TASK_PREFIX = "local-chatgpt:"
CONSOLE = "conhost.exe"  # Windows 给命令行程序配的控制台进程
ERRORS = {"usage_limit_exceeded": "额度已用完"}  # task_complete 里 error.codex_error_info 的说明
MARKERS = tuple(
    f'"type":"{t}"'.encode()
    for t in ("session_meta", "turn_context", "thread_settings_applied", "task_started", "task_complete", "turn_aborted", "token_count")
)


def default_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def _limit(w, t):
    """额度窗口：已用百分比、窗口长度（分钟）、重置时间（毫秒）。老版本只给“多少秒后重置”。"""
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    resets = w.get("resets_at")
    if resets is None and w.get("resets_in_seconds") is not None and t:
        resets = t / 1000 + w["resets_in_seconds"]
    return {"used": w["used_percent"], "window": w.get("window_minutes"), "resets": resets * 1000 if resets else None}


def _cmdline(pid):
    try:
        return psutil.Process(pid).cmdline()
    except psutil.Error:
        return []


def _task_id(cmdline):
    """ChatGPT 任务的工具进程带着 --interaction-client-id local-chatgpt:<对话 id>，返回这个 id，别的进程返回 None。"""
    for i, a in enumerate(cmdline[:-1]):
        if a == TASK_FLAG and cmdline[i + 1].startswith(TASK_PREFIX):
            return cmdline[i + 1]
    return None


def _child(pid, wrappers):
    """exec-server 的一个子进程：(启动时间（毫秒）, ChatGPT 对话 id)，不是工具进程或读不到时返回 None。
    控制台进程和套在命令外面的 codex.exe 不会带对话 id，不读启动参数（读一次要几毫秒）。"""
    try:
        p = psutil.Process(pid)
        name, created = p.name().lower(), p.create_time() * 1000
    except psutil.Error:
        return None
    task = None if name == CONSOLE or name in wrappers else _task_id(_cmdline(pid))
    return (created, task) if task else None


class Rollout:
    """一个会话文件。"""

    def __init__(self, path):
        self.tail = JsonlTail(path, MARKERS)
        self._reset()

    def _reset(self):
        self.id = self.cwd = self.originator = self.version = self.model = None
        self.turns = {}  # 进行中的回合 id -> 开始时间（毫秒）
        self.dropped = set()  # 没等到结束事件就被新回合顶掉的回合 id
        self.ended = None  # 上一个回合怎么结束的
        self.totals = None
        self.context = self.window = None
        self.limits = None
        self.limits_t = None
        self.last = None
        self.prev_output = None  # 上一条 token_count 的累计输出；还没读到过时是 None
        self.rate = Rate()

    def update(self):
        restarted, objs = self.tail.read()
        if restarted:
            self._reset()
        for o in objs:
            p = o.get("payload")
            if not isinstance(p, dict):
                continue
            t = parse_ts(o.get("timestamp"))
            if t:
                self.last = max(self.last or 0, t)
            kind = o.get("type")
            if kind == "session_meta":
                self.id = p.get("id") or self.id
                self.cwd = p.get("cwd") or self.cwd
                self.originator = p.get("originator")
                self.version = p.get("cli_version")
            elif kind == "turn_context":
                self.model = p.get("model") or self.model
            elif kind == "event_msg":
                self._event(p, t)

    def _event(self, p, t):
        kind = p.get("type")
        if kind == "task_started":
            # 一个会话同时只跑一个回合，Codex 开始新回合前会先结束旧的。还没结束的旧回合是进程在回合中途被结束留下的
            # （之后恢复了这个会话，接着写同一个文件），丢掉，万一之后又收到它的结束事件也不理会
            self.dropped.update(k for k in self.turns if k is not None)
            started = p.get("started_at")
            self.turns = {p.get("turn_id"): started * 1000 if isinstance(started, (int, float)) else t}
            self.window = p.get("model_context_window") or self.window
            self.ended = None
        elif kind in ("task_complete", "turn_aborted"):
            turn_id = p.get("turn_id")
            if turn_id in self.dropped:
                self.dropped.discard(turn_id)
                return
            if self.turns.pop(turn_id, None) is None:
                self.turns.clear()
            error = p.get("error")
            if kind == "turn_aborted":
                self.ended = outcome("interrupted")
            elif error:
                info = error.get("codex_error_info") if isinstance(error, dict) else None
                self.ended = outcome("failed", ERRORS.get(info) if isinstance(info, str) else None)
            else:
                self.ended = outcome("ok")
        elif kind == "thread_settings_applied":
            self.model = (p.get("thread_settings") or {}).get("model") or self.model
        elif kind == "token_count":
            info = p.get("info")
            if isinstance(info, dict):
                total = info.get("total_token_usage") or {}
                out = total.get("output_tokens")
                if isinstance(out, int):
                    if self.prev_output is None:
                        # 文件里的第一条：累计值里可能有这个文件之前的历史（比如接着旧会话写的新文件），
                        # 只算最近一次请求的输出，不然速度一下子冲到几百上千 tokens/s
                        last = (info.get("last_token_usage") or {}).get("output_tokens")
                        self.rate.add(t, min(out, last) if isinstance(last, int) else 0)
                    else:
                        self.rate.add(t, out - self.prev_output)
                    self.prev_output = out
                self.totals = total
                self.context = (info.get("last_token_usage") or {}).get("total_tokens")
                self.window = info.get("model_context_window") or self.window
            limits = p.get("rate_limits")
            if isinstance(limits, dict) and t:
                self.limits, self.limits_t = limits, t

    def busy(self, now):
        return bool(self.turns) and self.last is not None and now - self.last < STALE * 1000


class CodexAdapter(CliAdapter):
    kind = "codex"
    label = "Codex"
    process_names = frozenset({"codex.exe", "codex"})
    config_key = "codex_dir"

    def __init__(self, home=None):
        self.home = home or default_home()

    def _scan_sessions(self, st, now):
        """最近有活动的会话文件；最新的一个总是保留，额度信息从它读。

        桌面版往会话文件里追加时，文件的修改时间不一定跟着变（实测能落后十几个小时），所以还看文件大小：
        和上次扫描时不一样，就算这次扫描时有活动。刚启动时还没有上次的大小，只能先看修改时间。
        """
        sizes = {}
        grown = st.get("grown", {})  # 会话文件 -> 最近一次发现大小变了的时间（秒）
        found = []
        stack = [os.path.join(self.home, "sessions")]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for de in entries:
                try:
                    if de.is_dir():
                        stack.append(de.path)
                    elif de.name.startswith("rollout-") and de.name.endswith(".jsonl"):
                        info = de.stat()
                        sizes[de.path] = info.st_size
                        if st.get("sizes", {}).get(de.path, info.st_size) != info.st_size:
                            grown[de.path] = now
                        found.append((max(info.st_mtime, grown.get(de.path, 0)), de.path))
                except OSError:
                    continue
        st["sizes"] = sizes
        st["grown"] = {path: t for path, t in grown.items() if path in sizes and now - t < RECENT}
        found.sort(reverse=True)
        keep = [path for mtime, path in found[: MAX_SESSIONS * 2] if now - mtime < RECENT]
        if found and not keep:
            keep = [found[0][1]]
        return keep

    def _titles(self, st):
        tail = st.get("index")
        if tail is None:
            tail = st["index"] = JsonlTail(os.path.join(self.home, "session_index.jsonl"), (b'"thread_name"',))
        titles = st.setdefault("titles", {})
        restarted, objs = tail.read()
        if restarted:
            titles.clear()
        for o in objs:
            if o.get("id") and o.get("thread_name"):
                titles[o["id"]] = o["thread_name"]
        return titles

    def _exec_servers(self, inst):
        """替 ChatGPT 在本机执行命令的 codex 进程（启动参数里有 exec-server）。每个进程只读一次启动参数。"""
        roles = inst.state.setdefault("exec_servers", {})  # 进程号 -> 是不是 exec-server
        prune(roles, inst.roots)
        for pid in inst.roots:
            if pid not in roles:
                roles[pid] = "exec-server" in _cmdline(pid)
        return [pid for pid in inst.roots if roles[pid]]

    def _chatgpt_tasks(self, inst, now):
        """ChatGPT 里选在本机执行的任务，一个对话一行：这一轮的工具进程开着就是工作中，关掉后显示空闲。"""
        st = inst.state
        kids = st.setdefault("exec_children", {})  # 子进程号 -> _child() 的结果
        # 对话 id -> [第一次看到它的开始时间, 最后一次看到它的工具进程的时间]（毫秒）。只在内存里，程序重启后从下一轮算起
        seen = st.setdefault("chatgpt", {})
        children = [c for pid in self._exec_servers(inst) for c in inst.children.get(pid, ())]
        prune(kids, children)
        for c in children:
            if c not in kids:
                kids[c] = _child(c, self.process_names)
        turns = {}  # 对话 id -> 进行中的这一轮从什么时候开始
        for created, task in filter(None, (kids[c] for c in children)):
            turns[task] = min(created, turns.get(task, created))
        for task, start in turns.items():
            record = seen.setdefault(task, [start, now])
            record[0], record[1] = min(record[0], start), now

        rows = []
        for task, (started, last) in list(seen.items()):
            busy = task in turns
            if not busy and now - last >= RECENT * 1000:
                del seen[task]
                continue
            rows.append(session(
                id=task,
                name="ChatGPT 任务",
                entry="chatgpt",
                status="busy" if busy else "idle",
                since=turns[task] if busy else last,
                turn_start=turns.get(task),
                started=started,
                last=last,
                remote=True,
            ))
        return rows

    def poll(self, inst):
        st = inst.state
        now_s = time.time()
        now = now_s * 1000
        rollouts = st.setdefault("rollouts", {})
        for r in track(rollouts, self._session_paths(st, now_s), Rollout):
            r.update()
        titles = self._titles(st)

        sessions = []
        for r in rollouts.values():
            if not r.id or r.last is None or now - r.last >= RECENT * 1000:
                continue
            busy = r.busy(now)
            tot = r.totals
            sessions.append(session(
                id=r.id,
                name=titles.get(r.id),
                cwd=r.cwd,
                entry=r.originator,
                status="busy" if busy else "idle",
                since=min(filter(None, r.turns.values()), default=r.last) if busy else r.last,
                model=r.model,
                context=r.context,
                context_window=r.window,
                tokens=usage(tot.get("input_tokens"), tot.get("cached_input_tokens"), tot.get("output_tokens")) if tot else None,
                rate=r.rate.per_second(now),
                last=r.last,
                outcome=LOST if r.turns and not busy else r.ended,
            ))
        sessions += self._chatgpt_tasks(inst, now)

        newest = max((r for r in rollouts.values() if r.limits_t), key=lambda r: r.limits_t, default=None)
        limits = None
        if newest:
            limits = {
                "primary": _limit(newest.limits.get("primary"), newest.limits_t),
                "secondary": _limit(newest.limits.get("secondary"), newest.limits_t),
                "t": newest.limits_t,
            }
        return summarize(
            sessions,
            limit=MAX_SESSIONS,
            version=newest_version(r.version for r in rollouts.values()),
            home=self.home,
            recent=RECENT,
            limits=limits,
        )
