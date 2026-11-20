"""页面上“为什么没有发现某个服务或工具”的检测情况。只有进程名、进程号、地址、路径和错误信息，不读会话内容。

manager 每次发现后用 collect() 按工具整理一份；页面来取时用 merge() 再看最近一轮轮询连不连得上。
每条检测情况的 code 说明是哪种情况，level 是 ok / info / warning。
"""

import os
import time

from ..i18n import tr


def _shown_names(adapter):
    # Windows 上只列 .exe 的进程名，其他系统只列不带 .exe 的
    names = sorted(n for n in adapter.process_names if n.endswith(".exe") == (os.name == "nt"))
    return names or sorted(adapter.process_names)


def _check_paths(adapter, running):
    """数据目录、会话文件夹（或数据库文件）、日志文件不在或打不开时返回一条检测情况（从外到里第一个有问题的），都没问题返回空列表。
    工具在运行时卡片会是空的，算问题；没在运行时多半只是没装，只作说明。日志找不到只是统计不到每次请求，也只作说明。"""
    for role, path in adapter.data_paths():
        try:
            if role in ("log", "db"):
                if not os.path.isfile(path):
                    raise FileNotFoundError(path)
            else:
                with os.scandir(path):
                    pass
        except OSError as exc:
            return [{
                "code": "missing_path",
                "level": "warning" if running and role != "log" else "info",
                "role": role,
                "path": os.path.normpath(path),  # 配置里写的 / 和拼上去的 \ 统一
                "key": adapter.config_key,
                "error": None if isinstance(exc, FileNotFoundError) else exc.strerror or str(exc),
            }]
    return []


def port_check(pid, ports_error, tried):
    """进程在、但还没连上接口时卡在哪一步：读不到端口列表、还没有监听端口、端口都探测失败，或者还有地址没探测完。
    tried 是它监听的每个地址 [{"url", "error"}]，error 是最近一次探测失败的原因，还没探测过是 None。"""
    if ports_error:
        return {"code": "ports_unknown", "level": "warning", "pid": pid, "error": ports_error}
    if not tried:
        return {"code": "no_port", "level": "warning", "pid": pid}
    failed = all(a["error"] for a in tried)
    return {"code": "probe_failed" if failed else "probing", "level": "warning" if failed else "info", "pid": pid, "addrs": tried}


def collect(adapters, instances, waiting, find_errors, every):
    """按工具整理这次发现的检测情况，每个工具一组 checks。

    waiting：进程号 -> 还没连上接口的模型服务卡在哪一步（port_check() 的结果）；find_errors：kind -> 查找进程时出的错。"""
    tools = []
    for adapter in adapters:
        insts = [i for i in instances if i.adapter is adapter]
        checks = []
        for inst in insts:
            if inst.url or adapter.category == "cli":
                checks.append({
                    "code": "found",
                    "level": "ok",
                    "id": inst.id,
                    "url": inst.url,
                    "configured": inst.configured,
                    "pid": inst.main_pid,
                    "processes": len(inst.roots),
                    # 手动添加的地址里指的就是这个本机服务的（写法可能不同），设置页面按它说明“与本机自动发现的为同一服务”
                    **({"endpoints": list(inst.endpoints)} if inst.endpoints else {}),
                })
            else:
                checks.append(waiting.get(inst.main_pid) or {"code": "probing", "level": "info", "pid": inst.main_pid, "addrs": []})
        running = any(i.local for i in insts)
        if adapter.kind in find_errors:
            checks.append({"code": "find_error", "level": "warning", "error": find_errors[adapter.kind]})
        elif not running:
            checks.append({"code": "no_process", "level": "info", "names": _shown_names(adapter), "hint": tr(adapter.match_hint) if adapter.match_hint else ""})
        # 模型服务没在本机运行时不看日志；命令行工具的数据目录不对时连进程都可能找不到（Claude Code 按登记找），总是看
        if running or adapter.category == "cli":
            checks += _check_paths(adapter, running)
        tools.append({"kind": adapter.kind, "label": adapter.label, "category": adapter.category, "checks": checks})
    return {"checked": int(time.time() * 1000), "every": every, "tools": tools}


def _with_poll(check, payload):
    """已发现的实例：最近一轮轮询连不上或读取出错时，改成对应的检测情况。"""
    if check["code"] != "found" or not payload or payload.get("status") not in ("offline", "error"):
        return check
    code = "offline" if payload["status"] == "offline" else "poll_error"
    return {**check, "code": code, "level": "warning", "error": payload.get("error")}


def _overall(checks):
    """一个工具的总体情况：warning 有问题、ok 已发现、probing 进程在但还在探测端口、missing 没有发现。"""
    if any(c["level"] == "warning" for c in checks):
        return "warning"
    if any(c["level"] == "ok" for c in checks):
        return "ok"
    if any(c["code"] == "probing" for c in checks):
        return "probing"
    return "missing"


def merge(diag, latest, every):
    """collect() 的结果加上最近一轮轮询的情况和每个工具的总体情况。diag 是 None（还没发现过）时返回空的；
    latest 是 {实例 id: 最近一轮轮询的结果}。"""
    if diag is None:
        return {"checked": None, "every": every, "tools": []}
    tools = []
    for tool in diag["tools"]:
        checks = [_with_poll(c, latest.get(c.get("id"))) for c in tool["checks"]]
        tools.append({**tool, "status": _overall(checks), "checks": checks})
    return {**diag, "tools": tools}
