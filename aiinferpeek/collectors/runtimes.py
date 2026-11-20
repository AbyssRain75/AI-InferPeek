"""把后台发现、轮询到的部署系统状态放进每次采样。"""

from ..runtimes import default_adapters


class RuntimesCollector:
    name = "runtimes"

    def __init__(self, manager, config=None):
        self.manager = manager
        self.config = config or {}
        self.adapters = list(manager.adapters)  # 最近一次交给 manager 的适配器（manager 可能还没换上）
        manager.start()

    def info(self):
        # 用最近一次交给 manager 的：manager.adapters 是后台线程在读写的，保存设置后也可能还没换上
        return {"supported": [a.label for a in self.adapters]}

    def sample(self):
        return self.manager.snapshot()

    def diagnostics(self):
        # 不放进采样：只在页面上展开“为什么没有发现”时才取（/api/diagnostics）
        return self.manager.diagnostics()

    def reconfigure(self, config, timeout=None):
        """设置页面保存后不重启就生效。数据位置（适配器的 config_key 对应的值）没变的工具沿用原来的适配器，
        manager 会保留它的实例和状态；变了的、新打开的换成新建的，关掉的不再交给 manager。返回是否已经生效。
        调用方保证不会同时调用（app.py 里保存时持锁）。"""
        current = {a.kind: a for a in self.adapters}
        adapters = []
        for adapter in default_adapters(config):
            old = current.get(adapter.kind)
            key = adapter.config_key
            if old is not None and (key is None or (self.config.get(key) or None) == (config.get(key) or None)):
                for name in old.settings:  # 余额预警这类不影响读数据的设置直接换上
                    setattr(old, name, getattr(adapter, name))
                adapter = old
            adapters.append(adapter)
        self.config, self.adapters = config, adapters
        return self.manager.reconfigure(adapters, config.get("endpoints") or (), timeout=timeout)

    def slim(self, data):
        # 历史里只留画曲线要用的字段
        return [{"id": r["id"], "status": r.get("status"), "speed": r.get("speed")} for r in data]

    def close(self):
        self.manager.stop()
