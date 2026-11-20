"""这台电脑：aiinferpeek.json 里给它起的名字（device_name）、界面语言。计算机名在 CPU 的静态信息里（cpu.host）。"""

from .. import i18n


class DeviceCollector:
    name = "device"

    def __init__(self, config=None):
        self.reconfigure(config or {})

    def info(self):
        return {}

    def sample(self):
        # 名字放在每条样本里：设置页面保存后，看板和悬浮窗下一秒就换上，不用刷新或重连。
        # 界面语言也是：页面发现和自己加载时的不一样就重新加载（见 web/i18n.js 的 followLanguage）
        return {"name": self.display_name, "lang": i18n.current()}

    def slim(self, data):
        return None  # 画曲线用不到，历史里不留

    def reconfigure(self, config):
        """设置页面保存后调用；没起名（null 或空白）时是 None，页面上用计算机名或程序名。"""
        name = config.get("device_name")
        self.display_name = (name.strip() or None) if isinstance(name, str) else None

    def close(self):
        pass
