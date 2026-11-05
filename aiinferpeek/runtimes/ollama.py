"""Ollama。

- 已加载的模型：/api/ps
- 模型对应哪个 llama-server 进程：/api/show 里 FROM 的文件路径 = 进程的 --model 参数
- 实时生成速度：每秒读 llama-server 的 /slots，用已生成 token 数的增量计算
- 每次请求的统计（提示词速度、生成速度、token 数）：解析 server.log 里的计时行。
  日志里还有提示词原文，这里只匹配计时相关的行，其余内容直接丢弃。
"""

import os
import re
import time
from collections import deque
from datetime import datetime

import psutil

from .base import Adapter, HttpStatusError, prune, request_json

SPEED_WINDOW = 3.0  # 秒，实时速度取最近这段时间的平均
TASK_MEMORY = 120.0  # 秒，记住每个进程最近处理过的 task 编号，用来把日志记录对应到模型
# 最多记住几个还没等到 total 行的请求。一次请求的三行计时是一起打出来的，缺了 total 行的（日志被截断、格式不同）不会再拼上
PENDING_MAX = 32
GEN_ENDPOINTS = ("/api/generate", "/api/chat", "/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/messages")


def default_log_path():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return os.path.join(base, "Ollama", "server.log") if base else None
    path = os.path.expanduser("~/.ollama/logs/server.log")
    return path if os.path.exists(path) else None


def short_name(name):
    for prefix in ("registry.ollama.ai/library/", "registry.ollama.ai/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _norm_path(path):
    return os.path.normcase(os.path.normpath(path.strip().strip('"')))


def _parse_time(value):
    """Ollama 的时间带 7 位小数和时区，截到微秒再解析；keep_alive=-1 时是很远的将来，返回 None。"""
    m = re.match(r"(.+T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", value or "")
    if not m:
        return None
    tz = m.group(3) or ""
    tz = "+00:00" if tz == "Z" else tz
    try:
        ts = datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + tz).timestamp()
    except ValueError:
        return None
    return None if ts > time.time() + 10 * 365 * 86400 else ts * 1000


def _arg(cmdline, *flags):
    for i, a in enumerate(cmdline[:-1]):
        if a in flags:
            return cmdline[i + 1]
    return None


class OllamaLog:
    """增量读取 server.log。每次打开读完就关，避免占着文件导致 Ollama 重启时无法轮换日志。"""

    PROMPT = re.compile(r"task (\d+) \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
    EVAL = re.compile(r"task (\d+) \|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
    TOTAL = re.compile(r"task (\d+) \|\s+total time =\s*([\d.]+) ms /\s*(\d+) tokens")
    GIN = re.compile(r'^\[GIN\] (\d{4}/\d\d/\d\d - \d\d:\d\d:\d\d) \|\s*(\d+) \|[^|]*\|[^|]*\|\s*(\w+)\s+"([^"]+)"')
    TEMPLATE = re.compile(r'msg="template selection" model=(\S+)')
    RUNNERS = re.compile(r'msg="loaded runners" count=(\d+)')

    def __init__(self, path, backlog_bytes=512 * 1024):
        self.path = path
        self.backlog_bytes = backlog_bytes
        self.ident = None
        self.pos = 0
        self.rest = b""
        self.timing = {}  # task -> 正在拼的记录
        self.untimed = []  # 历史部分里等待 [GIN] 行给出时间的记录
        self.last_model = None
        self.runner_count = None

    def read(self):
        """返回新完成的请求记录。第一次调用会顺带解析文件末尾一段历史。"""
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        ident = (st.st_dev, st.st_ino)
        backlog = False
        if ident != self.ident or st.st_size < self.pos:
            backlog = self.ident is None
            start = max(0, st.st_size - self.backlog_bytes) if backlog else 0
            self.ident, self.pos, self.rest = ident, start, b""
            self.timing.clear()
        if st.st_size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read(st.st_size - self.pos)
        except OSError:
            return []
        first_partial = backlog and self.pos > 0
        self.pos += len(chunk)
        data = self.rest + chunk
        lines = data.split(b"\n")
        self.rest = lines.pop()
        if first_partial and lines:
            lines.pop(0)

        out = []
        now = time.time() * 1000
        for raw in lines:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            rec = self._line(line, now if not backlog else None)
            if rec:
                out.append(rec)
        if backlog:
            out = [r for r in out if r["t"] is not None]
            for r in out:
                r["backlog"] = True
            self.untimed.clear()
        return out

    def _pending(self, task):
        """task 正在拼的记录；超过 PENDING_MAX 个时丢掉最早的。"""
        if task not in self.timing:
            while len(self.timing) >= PENDING_MAX:
                del self.timing[next(iter(self.timing))]
            self.timing[task] = {}
        return self.timing[task]

    def _line(self, line, now):
        if line.startswith("slot print_timing:"):
            if m := self.PROMPT.search(line):
                self._pending(int(m.group(1)))["prompt"] = (float(m.group(2)), int(m.group(3)))
            elif m := self.EVAL.search(line):
                self._pending(int(m.group(1)))["eval"] = (float(m.group(2)), int(m.group(3)))
            elif m := self.TOTAL.search(line):
                task = int(m.group(1))
                parts = self.timing.pop(task, {})
                p_ms, p_n = parts.get("prompt", (None, None))
                g_ms, g_n = parts.get("eval", (None, None))
                rec = {
                    "task": task,
                    "t": now,
                    "prompt_tokens": p_n,
                    "prompt_ms": p_ms,
                    "gen_tokens": g_n,
                    "gen_ms": g_ms,
                    "total_ms": float(m.group(2)),
                    "model_hint": self.last_model if self.runner_count == 1 else None,
                }
                if now is None:
                    self.untimed.append(rec)
                return rec
        elif line.startswith("[GIN]"):
            m = self.GIN.match(line)
            if m and self.untimed and m.group(4).split("?")[0] in GEN_ENDPOINTS:
                ts = datetime.strptime(m.group(1), "%Y/%m/%d - %H:%M:%S").timestamp() * 1000
                for rec in self.untimed:
                    rec["t"] = ts
                self.untimed.clear()
        elif line.startswith("time="):
            if m := self.TEMPLATE.search(line):
                self.last_model = short_name(m.group(1))
            elif m := self.RUNNERS.search(line):
                self.runner_count = int(m.group(1))
        return None


class OllamaAdapter(Adapter):
    kind = "ollama"
    label = "Ollama"
    process_names = frozenset({"ollama.exe", "ollama"})
    default_ports = (11434,)
    match_hint = "启动参数里要有 serve（Ollama 的服务进程）"
    config_key = "ollama_log"

    def __init__(self, log_path=None):
        self.log_path = log_path or default_log_path()

    def data_paths(self):
        return [("log", self.log_path)] if self.log_path else []

    def match_process(self, name, cmdline):
        return name.lower() in self.process_names and "serve" in cmdline[1:]

    def probe(self, url):
        data = request_json(url + "/api/version", timeout=1.0)
        if isinstance(data, dict) and "version" in data:
            return {"version": data["version"]}
        return None

    # ---------- 进程与模型的对应 ----------

    def _runners(self, inst):
        cache = inst.state.setdefault("runner_cmd", {})
        runners = []
        for pid in inst.pids[1:]:
            if pid not in cache:
                try:
                    cache[pid] = psutil.Process(pid).cmdline()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    cache[pid] = []
            cmd = cache[pid]
            model, port = _arg(cmd, "--model", "-m"), _arg(cmd, "--port")
            if model and port:
                runners.append({"pid": pid, "port": int(port), "path": _norm_path(model)})
        prune(cache, inst.pids)
        return runners

    def _model_paths(self, inst, name, digest):
        cache = inst.state.setdefault("paths", {})
        key = (name, digest)
        hit = cache.get(key)
        if hit and (hit[1] or time.time() - hit[0] < 30):
            return hit[1]
        paths = set()
        try:
            show = inst.post("/api/show", {"model": name})
            for line in (show.get("modelfile") or "").splitlines():
                if line.startswith("FROM "):
                    paths.add(_norm_path(line[5:]))
        except (OSError, ValueError):
            pass
        cache[key] = (time.time(), paths)
        return paths

    # ---------- 实时状态 ----------

    def _live(self, inst, runner, now):
        lives = inst.state.setdefault("live", {})
        ls = lives.setdefault(runner["pid"], {"window": deque(), "busy": None, "tasks": {}})
        try:
            slots = request_json(f"http://127.0.0.1:{runner['port']}/slots", timeout=0.5)
        except HttpStatusError as exc:
            return {"state": "loading"} if exc.status == 503 else {"state": "unknown"}
        except (OSError, ValueError):
            return {"state": "unknown"}

        for t_id, seen in list(ls["tasks"].items()):
            if now - seen > TASK_MEMORY:
                del ls["tasks"][t_id]
        busy = [s for s in slots if isinstance(s, dict) and s.get("is_processing")]
        if not busy:
            ls["window"].clear()
            ls["busy"] = None
            return {"state": "idle"}

        tasks = tuple(sorted(s.get("id_task", -1) for s in busy))
        for t_id in tasks:
            ls["tasks"][t_id] = now
        decoded = 0
        for s in busy:
            nt = s.get("next_token")
            nt = nt[0] if isinstance(nt, list) and nt else nt if isinstance(nt, dict) else {}
            decoded += nt.get("n_decoded", 0) or 0
        if tasks != ls["busy"]:
            ls["window"].clear()
            ls["busy"] = tasks
        w = ls["window"]
        w.append((now, decoded))
        while len(w) > 2 and now - w[0][0] > SPEED_WINDOW:
            w.popleft()
        speed = None
        if len(w) >= 2 and w[-1][0] - w[0][0] >= 0.8 and w[-1][1] >= w[0][1]:
            speed = round((w[-1][1] - w[0][1]) / (w[-1][0] - w[0][0]), 2)
        return {"state": "generating" if decoded else "prompt", "speed": speed, "n_decoded": decoded}

    def poll(self, inst):
        now = time.time()
        ps = inst.get("/api/ps").get("models") or []
        runners = self._runners(inst) if inst.local else []
        lives = inst.state.setdefault("live", {})
        prune(lives, {r["pid"] for r in runners})

        models = []
        runner_model = {}
        for m in ps:
            paths = self._model_paths(inst, m["name"], m.get("digest")) if runners else set()
            runner = next((r for r in runners if r["path"] in paths), None)
            if runner is None and len(ps) == 1 and len(runners) == 1:
                runner = runners[0]
            live = self._live(inst, runner, now) if runner else {}
            if runner:
                runner_model[runner["pid"]] = short_name(m["name"])
            details = m.get("details") or {}
            quant = details.get("quantization_level")
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            models.append({
                "name": short_name(m["name"]),
                "family": details.get("family"),
                "params": details.get("parameter_size"),
                "quant": quant if quant and quant.lower() != "unknown" else None,
                "size": size,
                "size_vram": vram,
                "gpu_ratio": round(vram / size, 4) if size else None,
                "context": m.get("context_length"),
                "expires": _parse_time(m.get("expires_at")),
                "pid": runner["pid"] if runner else None,
                "state": live.get("state", "idle"),
                "speed": live.get("speed"),
                "n_decoded": live.get("n_decoded"),
            })

        # 正在加载的模型还不在 /api/ps 里，但它的 llama-server 进程已经起来了（/health 返回 503）
        for runner in runners:
            if runner["pid"] in runner_model:
                continue
            try:
                request_json(f"http://127.0.0.1:{runner['port']}/health", timeout=0.5)
                continue
            except HttpStatusError as exc:
                if exc.status != 503:
                    continue
            except (OSError, ValueError):
                continue
            known = next((short_name(n) for (n, _), (_, paths) in inst.state.get("paths", {}).items() if runner["path"] in paths), None)
            models.append({
                "name": known or "（正在加载的模型）",
                "pid": runner["pid"],
                "state": "loading",
                "speed": None,
            })

        requests = inst.state.setdefault("requests", deque(maxlen=30))
        log = inst.state.get("log")
        if log is None and inst.local and self.log_path:
            log = inst.state["log"] = OllamaLog(self.log_path)
        if log is not None:
            for rec in log.read():
                rec["model"] = self._attribute(rec, lives, runner_model, models)
                requests.appendleft(rec)

        states = {m["state"] for m in models}
        status = "busy" if states & {"generating", "prompt"} else "loading" if "loading" in states else "idle"
        speeds = [m["speed"] for m in models if m["speed"] is not None]
        return {
            "status": status,
            "models": models,
            "speed": round(sum(speeds), 2) if speeds else None,
            "requests": [{k: v for k, v in r.items() if k not in ("model_hint", "backlog")} for r in list(requests)[:12]],
            "log": log is not None,
        }

    def finished(self, prev, cur):
        """日志里新出现的请求记录；同一个模型的请求页面上合成一条。"""
        last = max((r["t"] or 0 for r in prev.get("requests") or []), default=0)
        return [
            {
                "key": r.get("model") or "request",
                "name": r.get("model") or "一次请求",
                "result": "ok",
                "t": r["t"],
                "duration": r.get("total_ms"),
                "detail": f"生成 {r['gen_tokens']} tokens" if r.get("gen_tokens") else None,
            }
            for r in cur.get("requests") or []
            if r.get("t") and r["t"] > last
        ]

    @staticmethod
    def _attribute(rec, lives, runner_model, models):
        if rec.get("backlog"):
            return rec["model_hint"]  # 启动时读到的历史记录，只能按当时加载的模型推断
        owners = [pid for pid, ls in lives.items() if rec["task"] in ls["tasks"]]
        if len(owners) == 1 and owners[0] in runner_model:
            return runner_model[owners[0]]
        if len(models) == 1:
            return models[0]["name"]
        return rec["model_hint"]
