"""界面语言：简体中文（源码里直接写的）和英文（web/i18n/en.json 里的译文）。

界面上的文字在源码里照常写中文，用 tr("中文") 包起来：当前语言是英文时换成 en.json 里对应的译文，没有译文的原样用中文。
文字里的 {名字} 是占位，tr("版本 {v}", v="1.0") 把值填进去（先翻译再填，译文里的占位可以换位置）；
英文要分单复数的，译文里写 {n?model|models}，n 是 1 时用前一个。
同一句中文在不同地方要译成不同英文时用 trc("说明", "中文")，译文的键是“说明|中文”。
模块加载时就算好的常量（菜单项名字、状态文字这类）用 N_("中文") 标出来，用的时候再 tr(常量)：
N_ 原样返回，只是让检查译文的测试（tests/test_i18n.py）找得到这些文字。
前端的 web/i18n.js 用同一份译文、同样的写法。日志、注释、命令行（python -m aiinferpeek）的帮助和输出不翻译。

用哪种语言看 aiinferpeek.json 的 language：zh-CN、en，没写或 auto 时跟着 Windows 的显示语言（中文用中文，其余用英文）。
"""

import ctypes
import json
import locale
import logging
import re
import threading

log = logging.getLogger("aiinferpeek")

LANGUAGES = ("zh-CN", "en")  # 能选的语言；第一个是源码里写的
SOURCE = "zh-CN"
SETTINGS = ("auto", *LANGUAGES)  # 配置文件 language 能写的值，auto（或不写）是跟随系统
# {n}，或者译文里按数量选单复数的 {n?model|models}（n 是 1 时用前一个）
_PARAM = re.compile(r"\{(\w+)(?:\?([^|{}]*)\|([^{}]*))?\}")

_lock = threading.Lock()
_language = None  # 现在用的语言，第一次用到时按系统语言定
_messages = None  # en.json 的内容，第一次用到时读
_listeners = []


def system_language():
    """Windows 的显示语言是中文时 zh-CN，其余 en。不是 Windows 时看系统的区域设置。"""
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return SOURCE if langid & 0x3FF == 0x04 else "en"  # 主语言 0x04 是中文（简体、繁体都算）
    except (AttributeError, OSError):
        name = (locale.getlocale()[0] or "").lower()
        return SOURCE if name.startswith(("zh", "chinese")) else "en"


def resolve(setting):
    """配置里的 language → 实际用的语言：写了 zh-CN / en 就用它，没写、auto 或写错了跟随系统。"""
    return setting if setting in LANGUAGES else system_language()


def current():
    global _language
    if _language is None:
        with _lock:
            if _language is None:
                _language = system_language()
    return _language


def set_language(setting):
    """按配置里的 language 换语言（启动时、设置页面保存后调用），换了的话通知 on_change 登记的函数。"""
    global _language
    new = resolve(setting)
    with _lock:
        changed, _language = new != _language, new
    if not changed:
        return
    log.info("界面语言：%s", new)
    for callback in list(_listeners):
        try:
            callback(new)
        except Exception:
            log.exception("切换界面语言后通知失败")


def on_change(callback):
    """语言换了之后调用 callback(新语言)，比如桌面版重新生成托盘菜单。"""
    _listeners.append(callback)


def messages():
    """英文译文 {中文: 英文}，读不了时是空的（界面用中文）。"""
    global _messages
    if _messages is None:
        from .config import WEB_DIR  # config 里也要用 tr，放在这里免得互相导入

        try:
            data = json.loads((WEB_DIR / "i18n" / "en.json").read_text(encoding="utf-8"))
            _messages = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            log.exception("读取英文译文失败，界面用中文")
            _messages = {}
    return _messages


def fill(text, params):
    """把 {名字} 换成 params 里的值，{n?单数|复数} 按 n 是不是 1 选一个；params 里没有的原样留着（文字里本来就有的 { } 不受影响）。"""
    if not params:
        return text

    def sub(m):
        name, one, other = m.groups()
        if name not in params:
            return m.group(0)
        if one is None:
            return str(params[name])
        try:
            return one if float(params[name]) == 1 else other
        except (TypeError, ValueError):
            return other

    return _PARAM.sub(sub, text)


def tr(text, **params):
    if current() != SOURCE:
        text = messages().get(text, text)
    return fill(text, params)


def trc(context, text, **params):
    """同一句中文在不同地方要译成不同英文时，带上说明用在哪：译文的键是“说明|中文”，中文界面照样显示中文。"""
    if current() != SOURCE:
        text = messages().get(f"{context}|{text}", text)
    return fill(text, params)


def N_(text):
    """标出要翻译、但现在还不翻译的文字（模块加载时定好的常量），用的时候再 tr()。"""
    return text
