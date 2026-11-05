"""ComfyUI。

- 版本、设备：/system_stats
- 正在执行、排队、最近完成的任务：/api/jobs（新版本），老版本退回 /queue + /history
- 用到的模型：当前任务（空闲时是上一个完成的任务）的节点参数里，以模型扩展名结尾的文件名。
  任务里还有提示词原文，这里只取文件名，其余内容直接丢弃
- 逐步进度（第几步、每秒几步）ComfyUI 只发给提交任务的那个网页，外部读不到
"""

import os
import time

import psutil

from .base import Adapter, HttpStatusError, prune, request_json

STATS_EVERY = 5.0
RECENT = 8
# 任务状态 → 完成提示的结果。被中断（interrupted）只有老版本的 /history 分得出来
RESULTS = {"completed": "ok", "failed": "failed", "interrupted": "interrupted"}
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")
# 参数名 → 模型类别，按顺序匹配；主模型（checkpoint、UNet、扩散模型）是 "model"
MODEL_KINDS = (
    ("lora", "lora"),
    ("vae", "vae"),
    ("control", "controlnet"),
    ("upscale", "upscale"),
    ("clip", "clip"),
    ("text_encoder", "clip"),
    ("t5", "clip"),
    ("style", "other"),
    ("ckpt", "model"),
    ("unet", "model"),
    ("model", "model"),
)


def _model_kind(key, class_type):
    if "vision" in class_type:
        return "other"
    if "upscale" in class_type:
        return "upscale"
    return next((kind for word, kind in MODEL_KINDS if word in key), "other")


def _models(prompt):
    """任务（API 格式的节点表）用到的模型文件，去重并保持节点顺序。"""
    out = []
    if not isinstance(prompt, dict):
        return out
    for node in prompt.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        class_type = str(node.get("class_type") or "").lower()
        for key, value in inputs.items():
            if isinstance(value, str) and len(value) < 260 and value.lower().endswith(MODEL_EXT):
                item = {"name": value, "kind": _model_kind(key.lower(), class_type)}
                if item not in out:
                    out.append(item)
    return out


def _job(j):
    start, end = j.get("execution_start_time"), j.get("execution_end_time")
    preview = j.get("preview_output") or {}
    meta = preview.get("metadata") or {}
    return {
        "id": j.get("id"),
        "status": j.get("status"),
        "created": j.get("create_time"),
        "start": start,
        "end": end,
        "duration": end - start if start and end else None,
        "outputs": j.get("outputs_count"),
        "media": meta.get("kind") or preview.get("mediaType"),
        "width": meta.get("width"),
        "height": meta.get("height"),
    }


def _legacy_job(prompt_id, item):
    status = item.get("status") or {}
    times = {name: data.get("timestamp") for name, data in status.get("messages") or [] if isinstance(data, dict)}
    start = times.get("execution_start")
    end = times.get("execution_success") or times.get("execution_error") or times.get("execution_interrupted")
    ok = status.get("status_str") == "success"
    return {
        "id": prompt_id,
        "status": "completed" if ok else "interrupted" if "execution_interrupted" in times else "failed",
        "created": start,
        "start": start,
        "end": end,
        "duration": end - start if start and end else None,
        "outputs": sum(len(v.get("images", [])) for v in (item.get("outputs") or {}).values()) or None,
        "media": None,
        "width": None,
        "height": None,
    }


class ComfyUIAdapter(Adapter):
    kind = "comfyui"
    label = "ComfyUI"
    process_names = frozenset({"python.exe", "pythonw.exe", "python", "python3"})
    default_ports = (8188, 8000)
    match_hint = "启动参数里要有 main.py，并且启动参数或启动目录带 comfy 字样（ComfyUI 的目录名）"

    def match_process(self, name, cmdline):
        if name.lower() not in self.process_names:
            return False
        args = [a.replace("\\", "/").lower() for a in cmdline[1:]]
        return any(a.endswith("main.py") for a in args) and any("comfy" in a for a in args)

    def match_pid(self, pid, name, cmdline):
        if self.match_process(name, cmdline):
            return True
        # 在 ComfyUI 目录里直接 python main.py 启动的（比如 comfy-cli 的 comfy launch），启动参数里没有目录名，看启动目录
        args = [a.replace("\\", "/").lower() for a in cmdline[1:]]
        if name.lower() not in self.process_names or not any(a.endswith("main.py") and not os.path.isabs(a) for a in args):
            return False
        try:
            return "comfy" in psutil.Process(pid).cwd().lower()
        except psutil.Error:
            return False

    def probe(self, url):
        data = request_json(url + "/system_stats", timeout=1.0)
        system = data.get("system") if isinstance(data, dict) else None
        if not isinstance(system, dict) or "devices" not in data:
            return None
        return {
            "version": system.get("comfyui_version"),
            "python": (system.get("python_version") or "").split(" ")[0] or None,
            "pytorch": system.get("pytorch_version"),
        }

    def _jobs(self, inst):
        st = inst.state
        if not st.get("legacy"):
            try:
                active = inst.get("/api/jobs?status=in_progress,pending&limit=100").get("jobs") or []
                done = inst.get(f"/api/jobs?status=completed,failed&limit={RECENT}").get("jobs") or []
                return [_job(j) for j in active], [_job(j) for j in done]
            except HttpStatusError as exc:
                if exc.status not in (404, 405):
                    raise
                st["legacy"] = True
        queue = inst.get("/queue", timeout=2.0)
        now = time.time() * 1000
        seen = st.setdefault("seen_running", {})
        models = st.setdefault("job_models", {})
        active = []
        for status, items in (("in_progress", queue.get("queue_running") or []), ("pending", queue.get("queue_pending") or [])):
            for item in items:
                pid = item[1] if len(item) > 1 else None
                if status == "in_progress":
                    seen.setdefault(pid, now)
                if pid not in models:
                    models[pid] = _models(item[2] if len(item) > 2 else None)
                active.append({"id": pid, "status": status, "created": None, "start": seen.get(pid), "end": None, "duration": None})
        prune(seen, {j["id"] for j in active})  # 执行完的任务不用再记开始时间
        history = inst.get(f"/history?max_items={RECENT}", timeout=2.0)
        for k, v in history.items():
            if k not in models:
                p = v.get("prompt")
                models[k] = _models(p[2] if isinstance(p, list) and len(p) > 2 else None)
        done = [_legacy_job(k, v) for k, v in history.items()]
        done.sort(key=lambda j: j["end"] or 0, reverse=True)
        return active, done

    def _job_models(self, inst, job_id, running):
        """一个任务用到的模型，每个任务只读一次并记住；老版本在 _jobs() 里已经记下。
        完成的任务读 /api/jobs/<id>；执行中的任务那里不带节点表，从 /queue 里找。"""
        cache = inst.state.setdefault("job_models", {})
        if job_id in cache or inst.state.get("legacy"):
            return cache.get(job_id) or []
        try:
            if running:
                items = inst.get("/queue", timeout=2.0).get("queue_running") or []
                item = next((x for x in items if len(x) > 2 and x[1] == job_id), None)
                if item is None:
                    return []  # 刚好执行完，下一秒按完成的任务读
                prompt = item[2]
            else:
                detail = inst.get(f"/api/jobs/{job_id}", timeout=2.0)
                workflow = detail.get("workflow") if isinstance(detail, dict) else None
                prompt = workflow.get("prompt") if isinstance(workflow, dict) else None
        except HttpStatusError:
            prompt = None
        except (OSError, ValueError):
            return []  # 一时读不到，下一秒再试
        cache[job_id] = _models(prompt)
        return cache[job_id]

    def poll(self, inst):
        st = inst.state
        now = time.time()
        if now - st.get("stats_t", 0) >= STATS_EVERY:
            stats = inst.get("/system_stats")
            devices = stats.get("devices") or []
            st["devices"] = [
                {"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")} for d in devices
            ]
            st["stats_t"] = now

        active, done = self._jobs(inst)
        running = [j for j in active if j["status"] == "in_progress"]
        current = None
        if running:
            j = running[0]
            began = j["start"] or j["created"]
            current = {"id": j["id"], "start": began, "elapsed": now * 1000 - began if began else None}
        durations = [j["duration"] for j in done if j["status"] == "completed" and j["duration"]]

        # 正在执行的任务用的模型；空闲时是上一个完成的任务用的（通常还留在显存里）
        target = running[0] if running else next((j for j in done if j["status"] == "completed"), None)
        files = self._job_models(inst, target["id"], bool(running)) if target and target["id"] else []
        prune(st.get("job_models", {}), {j["id"] for j in active + done})

        return {
            "status": "busy" if running else "idle",
            "queue": {"running": len(running), "pending": len(active) - len(running)},
            "current": current,
            "jobs": done[:RECENT],
            "last_duration": durations[0] if durations else None,
            "avg_duration": sum(durations[:5]) / len(durations[:5]) if durations else None,
            "devices": st.get("devices"),
            "job_models": {"job": target["id"], "current": bool(running), "files": files} if files else None,
            "speed": None,
        }

    def finished(self, prev, cur):
        """最近完成的任务列表里新出现的任务；批量排队时页面上合成一条，带完成个数。"""
        seen = {j["id"] for j in prev.get("jobs") or []}
        now = time.time() * 1000
        return [
            {
                "key": "job",
                "name": f"任务 {str(j['id'])[:8]}",
                "result": RESULTS[j["status"]],
                "t": j["end"] or now,
                "duration": j["duration"],
                "detail": None,
            }
            for j in cur.get("jobs") or []
            if j["id"] and j["id"] not in seen and j["status"] in RESULTS
        ]
