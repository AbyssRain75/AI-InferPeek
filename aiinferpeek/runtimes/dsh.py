"""DeepSeek Harness（命令行 dsh 和桌面版是同一个程序）。

- 是否在运行：DeepSeek Harness 进程（dsh 命令也是用它以 Node 方式运行）
- 会话：~/.dsh/storages/session_projcache/sessions/<会话 id>.json。这是 DeepSeek Harness 从会话记录
  （sessions/ 下压缩的 session.v4.jsonl.zstd）算好的汇总，和会话记录同时写入：标题、目录、模型、
  token 合计、上下文、是否有进行中的回合、是否在等用户回答问题。文件里有提示词原文，这里只取上面这些字段。
- 子任务（subagent）也有自己的会话文件，不单独列出
- 一个进程服务所有会话，对不上进程和会话，所以列出最近有活动的会话
- 余额：DeepSeek Harness 自己不记余额。装了余额小鲸鱼插件（dsh-whale-widget）时，它查到 DeepSeek 余额后记在账本
  ~/.dsh/.dshw-usage.json（网页端是 profiles/web/ 下的同名文件）里：lastBalance 是余额，accounting 里当前账户
  （active）的账本有币种、最后一次查到余额的时间，按北京时间每天一行的 debitUnits 是当天花掉的钱（单位是 1e-8 元）。
  这里只读这个账本，不读 API key、不联网；DeepSeek Harness 的界面开着时插件大约每 25 秒查一次
"""

import os
import time
from datetime import datetime, timedelta, timezone

from ..i18n import N_
from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, Rate, read_json, session, summarize, track, usage

LEDGERS = (".dshw-usage.json", os.path.join("profiles", "web", ".dshw-usage.json"))
LEDGER_CHECK = 5.0  # 秒，多久看一次账本有没有变
UNITS = 1e8  # 账本里的金额单位：1 元是 1e8
BEIJING = timezone(timedelta(hours=8))  # 账本按北京时间分天


def beijing_day(ms):
    return datetime.fromtimestamp(ms / 1000, BEIJING).strftime("%Y-%m-%d")


def _number(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def parse_ledger(data):
    """小鲸鱼的账本 → {"amount", "currency", "t", "spent": {北京时间的日期: 花掉的钱}}；没有余额时是 None。"""
    if not isinstance(data, dict) or _number(data.get("lastBalance")) is None:
        return None
    acc = data.get("accounting") if isinstance(data.get("accounting"), dict) else {}
    books = acc.get("books") if isinstance(acc.get("books"), dict) else {}
    book = books.get(acc.get("active"))
    book = book if isinstance(book, dict) else {}
    days = book.get("days") if isinstance(book.get("days"), dict) else {}
    spent = {
        day: row["debitUnits"] / UNITS
        for day, row in days.items()
        if isinstance(row, dict) and _number(row.get("debitUnits")) is not None
    }
    currency = book.get("currency")
    return {"amount": data["lastBalance"], "currency": currency if isinstance(currency, str) else None, "t": _number(book.get("lastAt")), "spent": spent}


class Balance:
    """余额：每 LEDGER_CHECK 秒看一次两个账本，变了才重新读；读坏了（正在写）沿用上次的。用最近查到余额的那个。"""

    def __init__(self, home):
        self.paths = [os.path.join(home, p) for p in LEDGERS]
        self.checked = None
        self.files = {}  # 账本 -> ((修改时间, 大小), parse_ledger() 的结果)
        self.value = None

    def update(self, now):
        """now 是秒。"""
        if self.checked is not None and now - self.checked < LEDGER_CHECK:
            return
        self.checked = now
        found = []
        for path in self.paths:
            try:
                info = os.stat(path)
            except FileNotFoundError:
                self.files.pop(path, None)  # 没有这个账本，或者删掉了：不再用它的
                continue
            except OSError:
                pass  # 一时读不了（被占用、没权限）：沿用上次读到的
            else:
                key = (info.st_mtime, info.st_size)
                if self.files.get(path, (None,))[0] != key:
                    parsed = parse_ledger(read_json(path))
                    if parsed is not None:
                        if parsed["t"] is None:
                            parsed["t"] = info.st_mtime * 1000  # 老账本没有记查询时间，用文件的修改时间
                        self.files[path] = (key, parsed)
            if path in self.files:
                found.append(self.files[path][1])
        self.value = max(found, key=lambda b: b["t"], default=None)

    def payload(self, now, warn):
        """页面上的余额：今天（北京时间）花掉的钱、预警线（设置里写的，没写是 None）。now 是秒。"""
        b = self.value
        if not b:
            return None
        return {"amount": b["amount"], "currency": b["currency"], "t": b["t"], "today": b["spent"].get(beijing_day(now * 1000)), "warn": warn}


class DshSession:
    """一个会话的汇总文件，变了才重新读。"""

    def __init__(self, path):
        self.path = path
        self.id = os.path.basename(path)[: -len(".json")]
        self.mtime = None
        self.rows = {}
        self.identity = {}
        self.prev_output = None
        self.rate = Rate()

    def update(self, now):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return
        if mtime == self.mtime:
            return
        record = (read_json(self.path) or {}).get("record")
        if not isinstance(record, dict):
            return
        self.mtime = mtime
        self.rows = record.get("rows") or {}
        self.identity = record.get("identity") or {}
        # 文件里只有合计，输出速度用两次读取之间的增量算；第一次读到的是历史，不算
        out = self.totals().get("outputTokens") or 0
        if self.prev_output is not None:
            self.rate.add(now, out - self.prev_output)
        self.prev_output = out

    def val(self, key):
        row = self.rows.get(key)
        return row.get("val") if isinstance(row, dict) else None

    def totals(self):
        return (self.val("tokenUsage") or {}).get("totals") or {}

    def listed(self):
        """子任务和新建后还没用过的会话不列出。"""
        blank = (self.val("sessionListMetadata") or {}).get("blank")
        return not self.val("subagent") and not (blank and (self.val("turnBoundary") or {}).get("openTurnStartSeq") is None)

    def row(self, now):
        last = (self.val("timeContext") or {}).get("lastMessageTime") or self.mtime * 1000
        open_turn = (self.val("turnBoundary") or {}).get("openTurnStartSeq") is not None
        busy = open_turn and now - self.mtime * 1000 < STALE * 1000
        questions = ((self.val("userQuestions") or {}).get("questions") or {}).get("active")
        ctx = self.val("contextPressure") or {}
        tot = self.totals()
        cached = tot.get("cacheReadTokens") or 0
        return session(
            id=self.id,
            name=self.val("title"),
            cwd=self.val("workingDirectory") or self.identity.get("cwd"),
            status="waiting" if busy and questions else "busy" if busy else "idle",
            waiting_for="question" if busy and questions else None,
            since=((self.val("sessionListMetadata") or {}).get("lastPromptAt") or last) if busy else last,
            model=((self.val("modelSelection") or {}).get("lastUsed") or {}).get("model"),
            context=ctx.get("pressureTokens") or ctx.get("surfaceTokens"),
            context_window=ctx.get("contextWindow"),
            tokens=usage((tot.get("uncachedInputTokens") or 0) + cached + (tot.get("cacheWriteTokens") or 0), cached, tot.get("outputTokens"))
            if tot
            else None,
            rate=self.rate.per_second(now),
            last=last,
            # 汇总里只记了回合开始和结束，没有结束的原因，分不出完成和被中断
            outcome=LOST if open_turn and not busy else None,
        )


class DshAdapter(CliAdapter):
    kind = "dsh"  # 内部标识，disabled_tools、balance_warning 里写的都是它，不要改
    label = "DeepSeek Harness"
    process_names = frozenset({"deepseek harness.exe", "deepseek harness", "deepseek-harness", "node.exe", "node"})
    match_hint = N_("node 进程的启动参数中需包含 @deepseek-ai")
    config_key = "dsh_dir"
    has_balance = True
    settings = ("balance_warn",)

    def __init__(self, home=None, balance_warn=None):
        """balance_warn：余额低于多少时提醒（aiinferpeek.json 的 balance_warning.dsh），None 是不提醒。"""
        self.home = home or os.path.join(os.path.expanduser("~"), ".dsh")
        self.balance_warn = balance_warn

    def data_paths(self):
        return [("home", self.home), ("data", os.path.join(self.home, "storages", "session_projcache", "sessions"))]

    def match_process(self, name, cmdline):
        name = name.lower()
        if name.startswith("deepseek"):
            # 桌面版的主进程和 dsh 命令；界面子进程（--type=renderer 等）算在主进程的子进程里
            return not any(a.startswith("--type=") for a in cmdline[1:])
        return name in ("node.exe", "node") and any("@deepseek-ai" in a.replace("\\", "/") for a in cmdline[1:])

    def _scan_sessions(self, st, now):
        """最近有活动的会话汇总文件。"""
        runtime = read_json(os.path.join(self.home, "dsh-runtimes", "dsh-primary-runtime", "runtime.json")) or {}
        st["version"] = runtime.get("desktopVersion")
        found = []
        try:
            entries = list(os.scandir(os.path.join(self.home, "storages", "session_projcache", "sessions")))
        except OSError:
            return []
        for de in entries:
            try:
                if de.name.endswith(".json") and now - de.stat().st_mtime < RECENT:
                    found.append((de.stat().st_mtime, de.path))
            except OSError:
                continue
        found.sort(reverse=True)
        return [path for _, path in found[: MAX_SESSIONS * 3]]

    def poll(self, inst):
        st = inst.state
        now_s = time.time()
        now = now_s * 1000
        sessions = []
        for s in track(st.setdefault("sessions", {}), self._session_paths(st, now_s), DshSession):
            s.update(now)
            if s.mtime is not None and s.listed():
                sessions.append(s.row(now))
        balance = st.get("balance") or st.setdefault("balance", Balance(self.home))
        balance.update(now_s)
        return summarize(
            sessions,
            limit=MAX_SESSIONS,
            version=st.get("version"),
            home=self.home,
            recent=RECENT,
            balance=balance.payload(now_s, self.balance_warn),
        )
