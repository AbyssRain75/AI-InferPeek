"""Claude Code。

- 正在运行的会话：Claude Code 在 ~/.claude/sessions/<进程号>.json 里登记每个会话
  （标题、目录、状态 busy / idle、在等用户做什么），退出时删除。进程号会被重新分配，所以还要核对进程的启动时间。
- 模型、上下文、token 用量：~/.claude/projects/<目录>/<会话 id>.jsonl 里每条回复的 usage。
  会话记录里有对话原文，这里只解析回复所在的行，只取模型和用量。
- 回合怎么结束的：同一个文件里回复的 stop_reason（完成）、程序插入的接口出错消息（失败），
  以及用户按 Esc 时程序写入的 "[Request interrupted by user]"（被中断）。中断那一行只看是不是这个固定开头，不取其余内容。
- 额度（Beta）：Claude Code 只在接口的响应头里拿到额度，不写进本地文件。这里读 Claude 桌面版记下的套餐用量
  （数据目录里的 plan-usage-history.json），只用和 Claude Code 登录的是同一个组织的记录（组织 id 从全局配置
  .claude.json 的 oauthAccount 里读，只读这一个字段）。桌面版启动时记一次，之后只有 24 小时内点开过托盘里的用量面板，
  才每 15 分钟记一次，所以常常是几小时前的数，也没有重置时间，并不准确。
"""

import glob
import os
import re
import time

import psutil

from .base import prune
from .cli import CliAdapter, Rate, newest_version, outcome, read_json, session, summarize, usage
from .jsonl import JsonlTail, parse_ts

FIND_RETRY = 5.0  # 秒，新会话第一条消息之前还没有记录文件，隔这么久再找
FILETIME_EPOCH = 11644473600  # 1601-01-01 到 1970-01-01 的秒数
SETTLE = 3000  # 毫秒，登记里变成空闲后，会话记录里的结束标记可能晚一点才写进去，最多等这么久
INTERRUPTED = "[Request interrupted by user"  # 后面可能还有 " for tool use]"
API_ERRORS = {"rate_limit": "触发频率限制", "server_error": "服务端出错"}  # 接口出错消息的 error 字段
PLAN_FILE = "plan-usage-history.json"
PLAN_CHECK = 30.0  # 秒，多久重新读一次桌面版的用量记录（它最快 15 分钟记一次）
# 用量记录里的字段 -> 页面上的额度窗口（和 Codex 的一样，primary 是 5 小时、secondary 是每周），窗口长度（分钟）
PLAN_WINDOWS = (("primary", "fh", 300), ("secondary", "sd", 10080))


def default_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def config_path(home):
    """Claude Code 的全局配置：数据目录是默认的 ~/.claude 时在 ~/.claude.json，改了（CLAUDE_CONFIG_DIR）时在数据目录里。"""
    user = os.path.expanduser("~")
    if os.path.normcase(os.path.abspath(home)) == os.path.normcase(os.path.join(user, ".claude")):
        return os.path.join(user, ".claude.json")
    return os.path.join(home, ".claude.json")


def desktop_dirs():
    """Claude 桌面版可能的数据目录：微软商店版在 %LOCALAPPDATA%/Claude-Data/claude_<包名>，安装包版在 %APPDATA%/Claude，
    macOS 在 ~/Library/Application Support/Claude，Linux 在 ~/.config/Claude。"""
    user = os.path.expanduser("~")
    out = []
    if os.environ.get("LOCALAPPDATA"):
        out += sorted(glob.glob(os.path.join(glob.escape(os.path.join(os.environ["LOCALAPPDATA"], "Claude-Data")), "*")))
    if os.environ.get("APPDATA"):
        out.append(os.path.join(os.environ["APPDATA"], "Claude"))
    return out + [os.path.join(user, "Library", "Application Support", "Claude"), os.path.join(user, ".config", "Claude")]


class PlanUsage:
    """桌面版记下的套餐用量里，Claude Code 登录的这个组织最新的一条。每 PLAN_CHECK 秒检查一次，文件没变不重新读；
    读失败（比如正好在写）时沿用上次的。samples 里每条是 {"t": 毫秒, "org": 组织 id, "u": {"fh": 5 小时已用 %, "sd": 每周已用 %}}。"""

    def __init__(self, config, dirs=None):
        self.config = config
        self.dirs = dirs  # None 时用 desktop_dirs()
        self.checked = None
        self.org = None
        self.files = {}  # 用量记录文件 -> ((修改时间, 大小), 里面的 samples)
        self.sample = None

    def update(self, now):
        """now 是秒。"""
        if self.checked is not None and now - self.checked < PLAN_CHECK:
            return
        self.checked = now
        data = read_json(self.config)
        if isinstance(data, dict):
            account = data.get("oauthAccount")
            self.org = account.get("organizationUuid") if isinstance(account, dict) else None
        if not self.org:  # 没登录账号（用 API key 等），套餐额度和它无关
            self.files.clear()
            self.sample = None
            return
        seen = set()
        for d in self.dirs if self.dirs is not None else desktop_dirs():
            path = os.path.join(d, PLAN_FILE)
            try:
                info = os.stat(path)
            except FileNotFoundError:
                continue  # 没有这个文件，或者删掉了：不再用它的
            except OSError:
                seen.add(path)  # 一时读不了（被占用、没权限）：沿用上次读到的
                continue
            seen.add(path)
            key = (info.st_mtime, info.st_size)
            if self.files.get(path, (None,))[0] != key:
                data = read_json(path)
                samples = data.get("samples") if isinstance(data, dict) else None
                if isinstance(samples, list):
                    self.files[path] = (key, samples)
        prune(self.files, seen)
        mine = [
            s for _, samples in self.files.values() for s in samples
            if isinstance(s, dict) and s.get("org") == self.org and isinstance(s.get("t"), (int, float)) and isinstance(s.get("u"), dict)
        ]
        self.sample = max(mine, key=lambda s: s["t"], default=None)

    def limits(self, now):
        """页面上的额度，格式和 Codex 的一样。没有重置时间；记录比窗口还长（早就重置过了）的窗口不给。now 是秒。"""
        s = self.sample
        if not s:
            return None
        out = {"t": s["t"], "source": "claude-desktop"}
        for key, field, minutes in PLAN_WINDOWS:
            used = s["u"].get(field)
            fresh = now * 1000 - s["t"] < minutes * 60_000
            out[key] = {"used": used, "window": minutes, "resets": None} if isinstance(used, (int, float)) and fresh else None
        return out if out["primary"] or out["secondary"] else None


def _same_process(entry):
    """登记的进程还在，而且是同一个进程（不是退出后进程号被别的程序用了）。"""
    try:
        created = psutil.Process(entry["pid"]).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    ft = str(entry.get("procStart") or "")
    if os.name == "nt" and ft.isdigit():
        return abs(int(ft) / 1e7 - FILETIME_EPOCH - created) < 2
    started = entry.get("startedAt")
    return not isinstance(started, (int, float)) or created <= started / 1000 + 2


def _turn_end(o, msg):
    """这一行是回合结束的标记时返回结果，不是返回 None。子任务的行不算。"""
    if o.get("isSidechain"):
        return None
    if o.get("type") == "assistant":
        if o.get("isApiErrorMessage"):
            return outcome("failed", API_ERRORS.get(o.get("error")))
        if msg.get("stop_reason") in ("end_turn", "stop_sequence") and not str(msg.get("model") or "").startswith("<"):
            return outcome("ok")
    elif o.get("type") == "user":
        # 工具返回的内容里也可能出现这串字，只认程序直接写入的文本消息
        content = msg.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        else:
            texts = []
        if any(isinstance(x, str) and x.startswith(INTERRUPTED) for x in texts):
            return outcome("interrupted")
    return None


class Transcript:
    """一个会话的记录文件：按回复 id 去重累计用量，记下最新的模型、上下文大小和回合结束标记。"""

    def __init__(self, path):
        self.tail = JsonlTail(path, (b'"type":"assistant"', INTERRUPTED.encode()))
        self._reset()

    def _reset(self):
        self.usage = {}  # 回复 id -> (输入, 缓存读取, 缓存写入, 输出)；一条回复会拆成几行写，用量相同
        self.totals = [0, 0, 0, 0]
        self.model = None
        self.context = None
        self.last = None
        self.end = None  # 最近一个回合结束标记：(结果, 毫秒时间)
        self.rate = Rate()

    def update(self):
        restarted, objs = self.tail.read()
        if restarted:
            self._reset()
        for o in objs:
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            t = parse_ts(o.get("timestamp"))
            end = _turn_end(o, msg)
            if end and t:
                self.end = (end, t)
            if o.get("type") != "assistant":
                continue
            u, mid = msg.get("usage"), msg.get("id")
            if not isinstance(u, dict) or not mid:
                continue
            row = tuple(
                u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
            )
            old = self.usage.get(mid, (0, 0, 0, 0))
            self.usage[mid] = row
            for i in range(4):
                self.totals[i] += row[i] - old[i]
            if t:
                self.last = max(self.last or 0, t)
                self.rate.add(t, row[3] - old[3])
            model = msg.get("model")
            if model and not model.startswith("<"):  # "<synthetic>" 是程序自己插入的消息，用量为 0
                self.model = model
                if not o.get("isSidechain"):
                    self.context = row[0] + row[1] + row[2]


class ClaudeCodeAdapter(CliAdapter):
    kind = "claude-code"
    label = "Claude Code"
    match_hint = "按 Claude Code 在数据目录的 sessions 文件夹里登记的进程找"
    config_key = "claude_dir"

    def __init__(self, home=None, desktop=None):
        """desktop：Claude 桌面版的数据目录列表，测试时指定；None 时按 desktop_dirs() 找。"""
        self.home = home or default_home()
        self.desktop = desktop

    def _registry(self):
        """{进程号: 登记内容}"""
        out = {}
        try:
            entries = list(os.scandir(os.path.join(self.home, "sessions")))
        except OSError:
            return out
        for de in entries:
            entry = read_json(de.path) if de.name.endswith(".json") else None
            if isinstance(entry, dict) and isinstance(entry.get("pid"), int):
                out[entry["pid"]] = entry
        return out

    def find_processes(self, candidates):
        return [pid for pid, entry in self._registry().items() if _same_process(entry)]

    def _transcript_path(self, cwd, sid):
        # 目录名是工作目录里非字母数字的字符都换成 "-"；很长的路径会被截短加哈希，找不到时按会话 id 搜
        projects = os.path.join(self.home, "projects")
        path = os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", cwd), f"{sid}.jsonl")
        if os.path.isfile(path):
            return path
        found = glob.glob(os.path.join(glob.escape(projects), "*", f"{glob.escape(sid)}.jsonl"))
        return found[0] if found else None

    def poll(self, inst):
        now = time.time() * 1000
        roots = set(inst.roots)
        files = inst.state.setdefault("transcripts", {})
        misses = inst.state.setdefault("misses", {})
        shown = inst.state.setdefault("shown", {})  # 会话 id -> 上一次显示的 (状态, 开始时间, 在等什么)
        # 会话 id -> 这一轮开始工作的时间。登记里的 statusUpdatedAt 在工作中和等你操作之间切换时也会变，
        # 完成提示的用时要从空闲变成工作中（或等你操作）的那一刻算起
        turns = inst.state.setdefault("turns", {})
        sessions = []
        versions = []
        for pid, e in self._registry().items():
            if pid not in roots:
                continue
            sid, cwd = e.get("sessionId"), e.get("cwd") or ""
            tr = files.get(sid)
            if tr is None and sid and now - misses.get(sid, 0) >= FIND_RETRY * 1000:
                path = self._transcript_path(cwd, sid)
                if path:
                    tr = files[sid] = Transcript(path)
                else:
                    misses[sid] = now
            if tr:
                tr.update()
            versions.append(e.get("version"))
            waiting = e.get("waitingFor")
            status = "waiting" if waiting else "busy" if e.get("status") == "busy" else "idle"
            since = e.get("statusUpdatedAt") or e.get("startedAt")
            ended = None
            prev = shown.get(sid)
            if status == "idle" and prev and prev[0] != "idle" and tr:
                # 这一轮刚结束：会话记录里有这一轮的结束标记就用它，还没有就先按原来的状态显示，等一会儿
                if tr.end and tr.end[1] >= (prev[1] or 0):
                    ended = tr.end[0]
                elif since and now - since < SETTLE:
                    status, since, waiting = prev
            shown[sid] = (status, since, waiting)
            if status == "idle":
                turns.pop(sid, None)
            else:
                turns.setdefault(sid, since or now)
            sessions.append(session(
                id=sid,
                pid=pid,
                name=e.get("name"),
                cwd=cwd,
                entry=e.get("entrypoint"),
                status=status,
                waiting_for=waiting,
                since=since,
                turn_start=turns.get(sid),
                outcome=ended,
                model=tr and tr.model,
                context=tr and tr.context,
                tokens=tr and usage(sum(tr.totals[:3]), tr.totals[1], tr.totals[3]),
                rate=tr.rate.per_second(now) if tr else 0,
                last=max(filter(None, (tr and tr.last, e.get("statusUpdatedAt"), e.get("startedAt"))), default=None),
            ))

        active = {s["id"] for s in sessions}
        for cache in (files, misses, shown, turns):
            prune(cache, active)
        plan = inst.state.get("plan")
        if plan is None:
            plan = inst.state["plan"] = PlanUsage(config_path(self.home), self.desktop)
        plan.update(now / 1000)
        return summarize(sessions, version=newest_version(versions), home=self.home, limits=plan.limits(now / 1000))
