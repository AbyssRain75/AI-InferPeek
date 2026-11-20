"""配置文件和资源路径。打包成 exe 后，网页文件在解包目录里，配置文件放在 exe 旁边。"""

import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .i18n import SETTINGS as LANGUAGE_SETTINGS
from .i18n import tr

FROZEN = getattr(sys, "frozen", False)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
WEB_DIR = RESOURCE_DIR / "web"
DEFAULT_CONFIG = APP_DIR / "aiinferpeek.json"
OLD_CONFIG = APP_DIR / "aiboard.json"  # 改名前（AI Board）的文件名

# endpoints 里能手动添加的服务，对应 runtimes 里 category 为 "service" 的适配器
SERVICE_KINDS = ("ollama", "comfyui")
# disabled_tools 里能写的工具，和 runtimes.default_adapters() 里各适配器的 kind 一一对应
TOOL_KINDS = ("ollama", "comfyui", "claude-code", "codex", "dsh", "grok-build", "opencode", "openclaw", "antigravity", "cursor", "workbuddy")
PATH_KEYS = ("ollama_log", "claude_dir", "codex_dir", "dsh_dir", "grok_dir", "opencode_dir", "openclaw_dir", "antigravity_dir", "cursor_dir", "workbuddy_dir")
# balance_warning 里能写的工具（读得到余额的，适配器的 has_balance）：{"dsh": 10} 是 DeepSeek Harness 的余额低于 10 时提醒
BALANCE_KINDS = ("dsh",)
# 设置页面管理的键；文件里的其他键保存时原样保留
CONFIG_KEYS = ("language", "device_name", "endpoints", "disabled_tools", *PATH_KEYS, "balance_warning")
DEVICE_NAME_MAX = 32  # 给这台电脑起的名字（device_name）最多几个字，看板标题和悬浮窗里要放得下
# Windows 路径里不能有的字符（冒号只能跟在盘符后面，单独判断）
BAD_PATH_CHARS = '<>"|?*' if os.name == "nt" else ""


class ConfigError(Exception):
    pass


class ConfigInvalid(ConfigError):
    """要保存的设置写得不对，problems 是 config_problems() 的结果。"""

    def __init__(self, problems):
        super().__init__("\n".join(p["message"] for p in problems))
        self.problems = problems


class ConfigConflict(ConfigError):
    """配置文件在页面打开后被改过，或者现在不是有效的 JSON：不保存，免得覆盖掉手动改的内容。"""


def load_config(path=None):
    """启动时读配置文件；path 是 None 时读 exe（或源码）旁边的 aiinferpeek.json。
    文件还不存在时返回 {}（全部用默认值），--config 指定的路径也一样：设置页面保存时会创建它。"""
    if path is None:
        if not DEFAULT_CONFIG.exists() and OLD_CONFIG.exists():
            # 从改名前的版本升级：改成新文件名接着用
            try:
                OLD_CONFIG.rename(DEFAULT_CONFIG)
            except OSError as exc:
                raise ConfigError(tr("将旧配置文件 {old} 重命名为 {new} 失败：{error}", old=OLD_CONFIG, new=DEFAULT_CONFIG.name, error=exc)) from exc
        path = DEFAULT_CONFIG
    if not Path(path).exists():
        return {}
    try:
        # 和设置页面读取（read_config_file）一样：记事本存的文件开头可能带 BOM
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(tr("读取配置文件 {path} 失败：{error}", path=path, error=exc)) from exc
    problems = check_config(config)
    if problems:
        raise ConfigError(tr("配置文件 {path} 内容有误：", path=path) + "\n" + "\n".join(f"- {p}" for p in problems))
    return config


def check_config(config):
    """返回配置里写错的地方，没有问题返回空列表。"""
    return [p["message"] for p in config_problems(config)]


def _url_ok(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # 端口不是数字或超出范围时抛 ValueError
    except ValueError:
        return False
    return bool(parts.hostname)


def _path_problem(value):
    if any(ch in BAD_PATH_CHARS or ord(ch) < 32 for ch in value):
        return tr("含有路径中不可使用的字符（{chars} 或控制字符）", chars=" ".join(BAD_PATH_CHARS))
    if os.name == "nt" and ":" in value[2:]:
        return tr("冒号只能位于盘符之后，例如 D:/logs")
    return None


def config_problems(config):
    """[{"field", "message"}]：写错的项和原因。field 是出错的位置（比如 endpoints[0].url、claude_dir），
    设置页面按它把提示放到对应的输入框旁边；最外层不对时是空字符串。"""
    if not isinstance(config, dict):
        return [{"field": "", "message": tr("最外层应为 { } 对象")}]
    problems = []

    def add(field, message):
        problems.append({"field": field, "message": message})

    language = config.get("language")
    if language is not None and language not in LANGUAGE_SETTINGS:
        add("language", tr("language 应为 {values} 之一，或 null（跟随系统）", values=tr("、").join(LANGUAGE_SETTINGS)))

    name = config.get("device_name")
    if name is not None:
        if not isinstance(name, str):
            add("device_name", tr("device_name 应为文字（需加引号）或 null"))
        elif any(ord(ch) < 32 for ch in name):
            add("device_name", tr("device_name 中不能包含换行、Tab 等控制字符"))
        elif len(name.strip()) > DEVICE_NAME_MAX:
            add("device_name", tr("device_name 最多 {max} 个字，当前为 {n} 个", max=DEVICE_NAME_MAX, n=len(name.strip())))

    endpoints = config.get("endpoints", [])
    if not isinstance(endpoints, list):
        add("endpoints", tr("endpoints 应为列表 [ ]"))
        endpoints = []
    for i, ep in enumerate(endpoints):
        where = f"endpoints[{i}]"
        if not isinstance(ep, dict):
            add(where, tr("endpoints 第 {n} 项应为 { } 对象", n=i + 1))
            continue
        if ep.get("kind") not in SERVICE_KINDS:
            add(f"{where}.kind", tr("endpoints 第 {n} 项的 kind 应为 {kinds}", n=i + 1, kinds=tr(" 或 ").join(SERVICE_KINDS)))
        if not _url_ok(ep.get("url")):
            add(f"{where}.url", tr("endpoints 第 {n} 项的 url 应为以 http:// 开头的地址，例如 http://192.168.1.20:11434", n=i + 1))

    disabled = config.get("disabled_tools", [])
    if not isinstance(disabled, list):
        add("disabled_tools", tr("disabled_tools 应为列表 [ ]"))
        disabled = []
    for i, kind in enumerate(disabled):
        if kind not in TOOL_KINDS:
            add(f"disabled_tools[{i}]", tr("disabled_tools 第 {n} 项应为 {kinds} 之一", n=i + 1, kinds=tr("、").join(TOOL_KINDS)))

    for key in PATH_KEYS:
        value = config.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            add(key, tr("{key} 应为路径（需加引号）或 null", key=key))
        elif problem := _path_problem(value):
            add(key, f"{key} {problem}")

    warning = config.get("balance_warning")
    if warning is not None and not isinstance(warning, dict):
        add("balance_warning", tr('余额预警 balance_warning 应为 { } 对象，例如 {"dsh": 10}，或 null'))
    for kind, value in (warning if isinstance(warning, dict) else {}).items():
        where = f"balance_warning.{kind}"
        if kind not in BALANCE_KINDS:
            add(where, tr("无法识别 balance_warning 中的 {kind}，仅支持 {kinds}", kind=kind, kinds=tr("、").join(BALANCE_KINDS)))
        elif value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0):
            add(where, tr("{key} 应为不小于 0 的数字（余额低于该值时提醒），或 null（不提醒）", key=where))
    return problems


# ---------- 设置页面读写配置文件 ----------


def _version(raw):
    """配置文件内容（字节）的摘要，页面读取时带上，保存时用来判断文件在这期间有没有被改过。"""
    return hashlib.sha256(raw).hexdigest()[:16]


def read_config_file(path):
    """(文件里的内容, 版本)。文件不存在时是 ({}, "")；版本是内容的摘要，保存时用来判断页面打开后文件有没有被改过。
    读不了或不是 { } 对象时抛 ConfigConflict。"""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return {}, ""
    except OSError as exc:
        raise ConfigConflict(tr("读取配置文件失败：{error}", error=exc.strerror or exc)) from exc
    version = _version(raw)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:
        raise ConfigConflict(tr("配置文件当前不是有效的 JSON（{error}）。为避免覆盖手动修改的内容，请先修复或删除该文件后再保存", error=exc)) from exc
    if not isinstance(data, dict):
        raise ConfigConflict(tr("配置文件最外层不是 { } 对象。为避免覆盖手动修改的内容，请先修复或删除该文件后再保存"))
    return data, version


def update_config_file(path, settings, version):
    """把设置页面管理的键（CONFIG_KEYS）写进配置文件，其他键原样保留，返回 (写入后的完整配置, 新版本)。

    version 是页面读取时的版本，和现在的文件对不上时抛 ConfigConflict；写得不对时抛 ConfigInvalid，都不写文件。
    写不进去（比如程序放在只读的文件夹里）时抛 OSError。"""
    data, current = read_config_file(path)
    if version != current:
        raise ConfigConflict(tr("配置文件在页面打开后已被修改（可能来自另一个页面或手动编辑），请重新载入后再修改"))
    for key in CONFIG_KEYS:
        value = settings.get(key)
        if value is None or value == [] or value == "":
            # 空的项：文件里本来没有就不加，有就写成空值，文件尽量保持原样
            if key in data:
                data[key] = [] if key in ("endpoints", "disabled_tools") else None
        else:
            data[key] = value
    problems = config_problems(data)
    if problems:
        raise ConfigInvalid(problems)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    write_atomic(path, text)
    return data, _version(text.encode("utf-8"))


def write_atomic(path, text):
    """先写同目录的临时文件再替换，写到一半出错也不会留下坏文件。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for wait in (0.05, 0.1, 0.2, None):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # 别的程序（杀毒软件、编辑器）正开着配置文件时 Windows 不让替换，稍等再试
                if wait is None:
                    raise
                time.sleep(wait)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
