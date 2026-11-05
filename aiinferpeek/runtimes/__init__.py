"""本地模型部署系统和 AI 命令行工具的自动发现与读取。新增时写一个 Adapter 子类并加到 default_adapters()。"""

from .antigravity import AntigravityAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .comfyui import ComfyUIAdapter
from .dsh import DshAdapter
from .grok import GrokBuildAdapter
from .manager import RuntimeManager
from .ollama import OllamaAdapter
from .opencode import OpencodeAdapter
from .openclaw import OpenclawAdapter


def default_adapters(config):
    """按配置建好各适配器；aiinferpeek.json 的 disabled_tools 里关掉的不建，不再发现和轮询。kind 要和 config.TOOL_KINDS 对应。"""
    disabled = set(config.get("disabled_tools") or ())
    adapters = [
        OllamaAdapter(log_path=config.get("ollama_log")),
        ComfyUIAdapter(),
        ClaudeCodeAdapter(home=config.get("claude_dir")),
        CodexAdapter(home=config.get("codex_dir")),
        DshAdapter(home=config.get("dsh_dir"), balance_warn=(config.get("balance_warning") or {}).get("dsh")),
        GrokBuildAdapter(home=config.get("grok_dir")),
        OpencodeAdapter(home=config.get("opencode_dir")),
        OpenclawAdapter(home=config.get("openclaw_dir")),
        AntigravityAdapter(home=config.get("antigravity_dir")),
    ]
    return [a for a in adapters if a.kind not in disabled]


__all__ = ["RuntimeManager", "default_adapters"]
