"""Cursor（AI 编程编辑器，3.x 版本）。

- 是否在运行：Cursor.exe 主进程。界面、GPU、扩展宿主这些子进程（启动参数带 --type=...）都是主进程开的，所以开着这种子进程的
  Cursor 进程就是主进程。借 Cursor.exe 跑的 Node 脚本（语言服务、gitWorker.js 这些，扩展宿主开的，扩展再开的也一样）、
  命令行的 cursor、Cursor 关掉后没退出的语言服务都没有这种子进程，不算；不看启动参数里是不是 .js（用 Cursor 打开 app.js、
  three.js 这类文件、文件夹时，主进程的启动参数里也带着它）。主进程的父进程常常已经退出（命令行的 cursor、更新程序拉起来的），
  子进程记着的父进程号可能被复用，所以比这个进程启动得早的子进程不算。只看 Cursor 自己的进程，别的 Electron 程序的子进程不管。
  macOS 上子进程叫 Cursor Helper (Renderer) 这些（Electron 的命名，没在 Mac 上实测过）。
  版本号来自程序目录里的 resources/app/package.json（macOS 上是 Cursor.app/Contents/Resources/app/package.json），
  Cursor 在原来的目录里升级，这个文件变了就重新读
- 对话（Cursor 里叫 composer）：数据目录（%APPDATA%\\Cursor）里的 User/globalStorage/state.vscdb（SQLite），字段名查自 3.21 和 3.29 版，
  也在这两个版本上实测过：
  - composerHeaders 表：每个对话一行，有创建和最后更新的时间、是不是子 agent、有没有归档；value 列（JSON）里有标题、
    是不是草稿、有没有要你处理的事（hasBlockingPendingActions：等你确认执行命令这类操作，或者回答它问的问题）
  - cursorDiskKV 表里键为 composerData:<对话 id> 的那一行（JSON）：状态（completed 做完、aborted 没做完就停了）、
    这一轮还没做完时它开始的时间（unfinishedRunAt）、模型、上下文（contextTokensUsed / contextTokenLimit）、最后一条消息的 id 和时间、工作区
  - 消息头（每条消息的 id、时间）有时在 composerData 里，多数时候分块存在 composerConvHeaders:<对话 id>:<6 位块号>，
    composerData 的 headerLog 记着有几块；最后一条消息在最后一块的末尾
  - 有要你处理的事时，再看最后一条消息（键为 bubbleId:<对话 id>:<消息 id>）调用的工具：ASK_QUESTION 是在问你问题，其余是等你确认
  都用 json_extract 只取这几个字段，对话内容不取出来
- 正在做的对话存盘时状态写的是 aborted（万一崩溃，下次打开就是被中断），要看有没有 unfinishedRunAt，这一轮做完或按了停止后就不再记；
  只有在云端跑的 agent 照样写 generating
- Cursor 存盘时不写中断原因（abortReason）和 token 用量（turnTokenUsage）：这一轮出错和被你按停止分不出来，都算被中断；
  也没有输入、输出和输出速度，只有上下文
- Cursor 被关掉时库里还留着 unfinishedRunAt，下次打开那个对话时它才清掉。所以这一轮比现在的 Cursor 进程启动得早、
  之后也没有新消息的，算被中断，重新打开后显示空闲。关掉后卡片在下一次发现进程时（5 秒内）消失，重新打开后不弹“已中断”的
  提示；关掉后几秒内又打开（比如更新后重启）、卡片还没消失的，照常弹。重新打开后这一轮又有新消息的，算接着在做，
  用时从进程启动算起。云端 agent 不跟着本机的进程走，不这样算；进程一直开着、30 分钟没有新消息的，提示可能已中断
- 子 agent 的对话、归档了的对话不列出
- 一个 Cursor 进程（可以开好几个窗口）服务所有对话，对不上进程和对话，所以列出最近有活动的对话
"""

import json
import os
import sys
import time
from collections import defaultdict

import psutil

from ..i18n import N_, tr
from . import sqlite
from .base import ppid_map
from .cli import LOST, CliAdapter, MAX_SESSIONS, RECENT, STALE, local_path, outcome, read_json, session, summarize, uri_path
from .jsonl import parse_ts

ASK_QUESTION = 51  # ClientSideToolV2：在问你问题（其余要你处理的是确认执行命令、切换模式这类操作）
AUTO = "default"  # 模型选的是 Auto 时 modelName 是 default
# macOS 上 Electron 的子进程是单独的程序，名字不一样；Windows、Linux 上子进程也叫 Cursor
MAC_HELPERS = frozenset({"cursor helper", "cursor helper (gpu)", "cursor helper (plugin)", "cursor helper (renderer)"})
# composerData 里要的字段，一次 json_extract 取出来（返回 JSON 数组）
LAST_ID, LAST_AT = "fullConversationHeadersOnly[#-1].bubbleId", "fullConversationHeadersOnly[#-1].createdAt"
DATA_FIELDS = (
    "status", "unfinishedRunAt", "lastUpdatedAt", "conversationCheckpointLastUpdatedAt", "modelConfig.modelName", "modelConfig.maxMode",
    "contextTokensUsed", "contextTokenLimit", LAST_ID, LAST_AT, "headerLog.chunkCount", "workspaceIdentifier",
)


def _extract(*fields):
    """只取 value 列（JSON）里的这些字段：一个字段时返回它的值，几个字段时返回 JSON 数组。
    不是有效 JSON 的行返回 NULL，不让一行坏数据拖得整个查询出错。"""
    paths = ", ".join(f"'$.{f}'" for f in fields)
    return f"CASE WHEN json_valid(CAST(value AS TEXT)) THEN json_extract(CAST(value AS TEXT), {paths}) END"


# 子 agent 有自己的一行（isSubagent，老一些的是 task- 开头的 id），不单独列出
HEADERS_SQL = f"""
SELECT composerId, workspaceId, createdAt, lastUpdatedAt, checkpointAt, {_extract("name", "isDraft", "hasBlockingPendingActions")}
FROM composerHeaders
WHERE coalesce(isSubagent, 0) = 0 AND coalesce(isArchived, 0) = 0 AND composerId NOT LIKE 'task-%'
ORDER BY recency DESC
LIMIT ?
"""
DATA_SQL = f"SELECT key, {_extract(*DATA_FIELDS)} FROM cursorDiskKV WHERE key IN ({{}})"
TOOL_SQL = f"SELECT {_extract('toolFormerData.tool')} FROM cursorDiskKV WHERE key = ?"
# 消息头分块存的对话（composerData 里有 headerLog）：键是 composerConvHeaders:<对话 id>:<6 位块号>，每块是消息头的 JSON 数组，
# 最后一条消息在最后一块的末尾
CHUNK_KEY = "composerConvHeaders:{}:{:06d}"
CHUNK_SQL = (
    "SELECT CASE WHEN json_valid(CAST(value AS TEXT)) THEN json_extract(CAST(value AS TEXT), '$[#-1].bubbleId', '$[#-1].createdAt') END "
    "FROM cursorDiskKV WHERE key = ?"
)


def default_home():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Cursor")


def _is_child(cmdline):
    """界面、GPU、扩展宿主这些主进程开的子进程：启动参数带 --type=。"""
    return any(a.startswith("--type=") for a in cmdline[1:])


def _created(pid):
    """进程的启动时间（秒），读不到时返回 None。"""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _loads(text):
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _ms(value):
    """库里的毫秒时间；没有或者不是数字时返回 None。"""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def workspace_path(ident):
    """composerData 的 workspaceIdentifier → 工作区的路径：打开的是文件夹时是 uri，是 .code-workspace 文件时是 configPath。
    都是 VS Code 存下来的 URI 对象（fsPath 是本机路径，有时没存，就用 path 拼）；空窗口只有 id，返回 None。"""
    uri = (ident.get("uri") or ident.get("configPath")) if isinstance(ident, dict) else None
    if isinstance(uri, str):
        return uri_path(uri)
    if not isinstance(uri, dict):
        return None
    if isinstance(uri.get("fsPath"), str):
        return uri["fsPath"]
    if isinstance(uri.get("external"), str):
        return uri_path(uri["external"])
    path, scheme, host = uri.get("path"), uri.get("scheme"), uri.get("authority") or ""
    if not isinstance(path, str) or not path:
        return None
    if scheme != "file":  # 远程（SSH、WSL、容器）的工作区，原样写出地址
        return f"{scheme}://{host}{path}"
    return local_path(host, path)  # 和 file:// 地址一样还原成本机路径


def model_name(name, max_mode):
    if not isinstance(name, str) or not name:
        return None
    name = "Auto" if name == AUTO else name
    return f"{name} (MAX)" if max_mode is True else name


def composer_row(raw, now, started):
    """_read() 读到的一个对话 → 会话行。started 是现在这个 Cursor 进程什么时候启动的（毫秒），读不到时是 None。"""
    d = raw["data"]
    status = d.get("status")
    bubble_at = parse_ts(d.get(LAST_AT))
    submitted = _ms(d.get("lastUpdatedAt"))  # 每一轮开始时 Cursor 把它改成当时的时间
    unfinished = _ms(d.get("unfinishedRunAt"))  # 这一轮还没做完（存盘时状态写的是 aborted）
    running = status == "generating" or unfinished is not None
    last = max(
        filter(None, (raw["created"], raw["updated"], raw["checkpoint"], submitted, _ms(d.get("conversationCheckpointLastUpdatedAt")), bubble_at)),
        default=None,
    )
    end = None
    # 本地跑的这一轮（有 unfinishedRunAt）在现在的 Cursor 进程启动前就开始了。它跟着进程走，之后也没有新消息的已经停了。
    # 比的是这一轮开始的时间，不是最后的活动：重新打开后对话列表、checkpoint 的时间可能被改过，unfinishedRunAt 却还留着。
    # 云端 agent 没有 unfinishedRunAt，不跟着本机的进程走，关掉 Cursor 时它照样在跑，只按很久没有新消息算可能已中断
    before = bool(started and unfinished and unfinished < started)
    if running:
        if before and not (bubble_at and bubble_at >= started):
            state, end = "idle", outcome("interrupted", N_("Cursor 关闭时本轮尚未完成"))
        elif raw["blocking"]:
            state = "waiting"
        elif last and now - last < STALE * 1000:
            state = "busy"
        else:
            state, end = "idle", LOST
    elif raw["blocking"] and status != "aborted":
        state = "waiting"  # 它问了你问题，这一轮先停下来等你回答
    else:
        state = "idle"
        if status == "aborted":
            end = outcome("interrupted")  # 按了停止，或者出错（库里不记原因，分不出来）
    turn_start = None
    if running and state != "idle":
        # 进程启动前开始、重新打开后又接着做的，从进程启动算起，关着的那段时间不算
        turn_start = started if before else (unfinished or submitted or last)
    return session(
        id=raw["id"],
        name=raw["name"],
        cwd=raw["cwd"],
        status=state,
        waiting_for=("question" if raw["asking"] else "permission") if state == "waiting" else None,
        since=(bubble_at or last) if state == "waiting" else turn_start if state == "busy" else last,
        turn_start=turn_start,
        model=model_name(d.get("modelConfig.modelName"), d.get("modelConfig.maxMode")),
        context=_count(d.get("contextTokensUsed")) or None,
        context_window=_count(d.get("contextTokenLimit")) or None,
        last=last,
        outcome=end,
    )


class CursorAdapter(CliAdapter):
    kind = "cursor"
    label = "Cursor"
    process_names = frozenset({"cursor.exe", "cursor"}) | MAC_HELPERS
    match_hint = N_("仅识别主进程：界面、扩展等子进程（启动参数含 --type=）以及借助它运行的 Node 脚本不计入")
    config_key = "cursor_dir"

    def __init__(self, home=None):
        self.home = home or default_home()
        self.db = os.path.join(self.home, "User", "globalStorage", "state.vscdb")
        self.version = None
        self.started = None  # 现在的 Cursor 主进程什么时候启动的（毫秒）
        self._versions = {}  # 程序路径 -> (package.json 的修改时间, 版本号)

    def data_paths(self):
        return [("home", self.home), ("db", self.db)]

    def _version(self, exe):
        """程序目录里 package.json 的版本号。Cursor 在原来的目录里升级、路径不变，所以记着文件的修改时间，变了就重新读。"""
        folder = os.path.dirname(exe)
        if os.path.basename(folder) == "MacOS":  # macOS：程序在 Cursor.app/Contents/MacOS 里，package.json 在同级的 Contents/Resources/app 下
            path = os.path.join(os.path.dirname(folder), "Resources", "app", "package.json")
        else:
            path = os.path.join(folder, "resources", "app", "package.json")
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = None
        if exe not in self._versions or self._versions[exe][0] != stamp:
            pkg = read_json(path) if stamp is not None else None
            version = pkg.get("version") if isinstance(pkg, dict) else None
            self._versions[exe] = (stamp, version if isinstance(version, str) else None)
        return self._versions[exe][1]

    def find_processes(self, candidates):
        """主进程：开着 --type= 子进程的 Cursor 进程，见模块说明。刚启动、还没开子进程的那一下认不出，下一次发现时就有了。"""
        procs = [(pid, cmdline) for pid, name, cmdline in candidates if name.lower() in self.process_names]
        kids = {pid for pid, cmdline in procs if _is_child(cmdline)}
        owners = defaultdict(list)  # 进程号 -> 它开的 --type= 子进程
        if kids:  # Cursor 没开着时不用枚举进程表
            parents = ppid_map()
            for pid in kids:
                owners[parents.get(pid)].append(pid)
        found, started = [], []
        for pid, cmdline in procs:
            if pid in kids or pid not in owners:
                continue
            created = _created(pid)
            # 比它启动得早的子进程是父进程号被复用前留下的，不是它开的（同 manager._descendants）；读不到启动时间时只按父进程号算
            if created is not None and not any(t is None or t >= created for t in map(_created, owners[pid])):
                continue
            found.append((pid, cmdline))
            if created is not None:
                started.append(created * 1000)
        self.started = min(started) if started else None
        self.version = next(filter(None, (self._version(c[0]) for _, c in found if c)), self.version)
        return [pid for pid, _ in found]

    def _read(self):
        """最近有活动的对话：[{"id", "name", "cwd" ..., "data": composerData 里取的字段}]，从新到旧。"""
        with sqlite.connect(self.db) as db:
            try:
                heads = db.execute(HEADERS_SQL, (MAX_SESSIONS * 2,)).fetchall()
            except sqlite.Error as exc:
                if "no such table" in str(exc).lower():
                    raise RuntimeError(tr("Cursor 的数据库中无 composerHeaders 表，需 3.x 版本的 Cursor")) from exc
                raise
            keys = [f"composerData:{h[0]}" for h in heads]
            found = dict(db.execute(DATA_SQL.format(", ".join("?" * len(keys))), keys).fetchall()) if keys else {}
            rows = []
            for cid, workspace, created, updated, checkpoint, extra in heads:
                name, draft, blocking = ((_loads(extra) or []) + [None] * 3)[:3]
                d = dict(zip(DATA_FIELDS, _loads(found.get(f"composerData:{cid}")) or ()))
                chunks = _count(d.get("headerLog.chunkCount"))
                if not d.get(LAST_ID) and chunks:
                    got = db.execute(CHUNK_SQL, (CHUNK_KEY.format(cid, chunks - 1),)).fetchone()
                    d[LAST_ID], d[LAST_AT] = ((_loads(got[0]) if got else None) or [None, None])[:2]
                bubble = d.get(LAST_ID)
                if draft is True or not d or not (bubble or d.get("status") == "generating" or d.get("unfinishedRunAt")):
                    continue  # 草稿、还没发过消息的对话不列
                asking = False
                if blocking is True and isinstance(bubble, str):
                    tool = db.execute(TOOL_SQL, (f"bubbleId:{cid}:{bubble}",)).fetchone()
                    asking = bool(tool) and tool[0] == ASK_QUESTION
                rows.append({
                    "id": cid,
                    "workspace": workspace,
                    "name": name if isinstance(name, str) else None,
                    "created": _ms(created),
                    "updated": _ms(updated),
                    "checkpoint": _ms(checkpoint),
                    "blocking": blocking is True,
                    "asking": asking,
                    "data": d,
                })
            return rows

    def _folder(self, st, workspace):
        """工作区 id → 打开的文件夹：workspaceStorage/<id>/workspace.json 里记着（composerData 里没存路径时用）。"""
        folders = st.setdefault("folders", {})
        if workspace not in folders:
            info = read_json(os.path.join(self.home, "User", "workspaceStorage", workspace, "workspace.json")) if workspace else None
            uri = (info.get("folder") or info.get("workspace")) if isinstance(info, dict) else None
            folders[workspace] = uri_path(uri) if isinstance(uri, str) else None
        return folders[workspace]

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
                rows = self._read()
            except sqlite.Error as exc:
                if not sqlite.locked(exc):
                    raise
            else:
                for r in rows:
                    r["cwd"] = workspace_path(r["data"].get("workspaceIdentifier")) or self._folder(st, r["workspace"])
                st["rows"] = rows
                watch.done(stamp, now_s)
        rows = st.setdefault("rows", [])  # 库被锁住时沿用上次读到的，下一轮再读
        sessions = [composer_row(r, now, self.started) for r in rows]
        # 等你回答的问题可以一直放着（这一轮已经做完，Cursor 一直标着要你处理），所以等你操作的也只列最近有活动的
        sessions = [s for s in sessions if s["last"] and now - s["last"] < RECENT * 1000]
        return summarize(sessions, limit=MAX_SESSIONS, version=self.version, home=self.home, recent=RECENT)
