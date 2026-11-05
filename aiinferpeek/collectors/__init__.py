"""数据采集模块。

每个采集器提供：
- name：在采样结果里的键名
- info()：启动时读一次的静态信息（型号、总量等）
- sample()：每次采样调用，返回可直接转成 JSON 的数据
- slim(data)（可选）：存进历史时的精简版本，不提供就原样保存
- close()：退出时释放资源
"""

from ..runtimes import RuntimeManager, default_adapters
from .device import DeviceCollector
from .gpu import GpuCollector
from .procs import GpuProcessCollector
from .runtimes import RuntimesCollector
from .system import CpuCollector, MemoryCollector


def default_collectors(config=None):
    config = config or {}
    manager = RuntimeManager(default_adapters(config), endpoints=config.get("endpoints", ()))
    gpus = GpuCollector()
    return [
        DeviceCollector(config),
        RuntimesCollector(manager, config),
        CpuCollector(),
        MemoryCollector(),
        gpus,
        GpuProcessCollector(owners=manager.owners, luids=gpus.main_luids, devices=gpus.luid_index),
    ]
