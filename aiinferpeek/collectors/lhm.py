"""从 LibreHardwareMonitor 读 CPU 温度（以及读不到 RAPL 能量计时的 CPU 功耗）。

Windows 没有给普通程序读 CPU 温度的接口（任务管理器也不显示 CPU 温度）；ACPI 温区多半是主板或机身附近的温度，
不随 CPU 负载变化。LibreHardwareMonitor 以管理员运行、自带驱动，读的是 CPU 自己报的温度。
新版只通过网页服务对外提供数据（Options → Remote Web Server → Run，默认端口 8085），WMI 已经去掉了。

在后台线程里每秒取一次 /data.json，连不上时每 5 秒试一次，不拖慢其余的采样。
"""

import math
import re
import threading
import time

from ..i18n import N_, tr
from ..runtimes.base import HttpStatusError, request_json

URL = "http://127.0.0.1:8085/data.json"
INTERVAL = 1.0  # 和 LibreHardwareMonitor 默认的刷新间隔一样
RETRY = 5.0  # 连不上时多久再试
STALE = 5.0  # 超过这么久没读到新数据，不再显示旧的

# 温度按顺序取第一个有的：Intel 是 CPU Package，AMD 是 Core (Tctl/Tdie) 这几种；都没有时取其余温度里最高的
TEMP_NAMES = ("cpu package", "core (tctl/tdie)", "core (tdie)", "core (tctl)", "package", "core max")
# 功耗只认整颗 CPU 的，各核心的功耗加起来不等于它
POWER_NAMES = ("cpu package", "package")
# 传感器的 SensorId 形如 /intelcpu/0/temperature/3、/amdcpu/0/power/0：CPU 种类、第几颗、传感器类型
_SENSOR_ID = re.compile(r"^/(\w*cpu)/(\d+)/(temperature|power)/")
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

NOT_RUNNING = N_("CPU 温度需从 LibreHardwareMonitor 读取，当前未连接其网页服务（127.0.0.1:8085）：该程序未运行，或未在 Options → Remote Web Server 中勾选 Run。")


def _number(sensor):
    raw = sensor.get("RawValue")
    if isinstance(raw, (int, float)) and math.isfinite(raw):
        return float(raw)
    # 老版本只有带单位的文字，比如 "65.0 °C"，小数点按系统区域设置可能是逗号
    m = _NUMBER.search(str(sensor.get("Value") or ""))
    return float(m.group().replace(",", ".")) if m else None


def _sensors(tree):
    """树里所有的传感器节点（带 SensorId 的）。"""
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if "SensorId" in node:
            yield node
        children = node.get("Children")
        if isinstance(children, list):
            stack.extend(children)


def _pick(values, names, default=None):
    for name in names:
        if name in values:
            return values[name]
    return default


def read_cpu(tree):
    """从 data.json 的树里取 (温度 °C, 功耗 W)，取不到的是 None。几颗 CPU 时温度取最高的，功耗相加。"""
    temps, powers = {}, {}  # (种类, 第几颗) -> {传感器名（小写）: 值}
    for sensor in _sensors(tree):
        m = _SENSOR_ID.match(str(sensor.get("SensorId")))
        value = _number(sensor) if m else None
        if value is None:
            continue
        name = str(sensor.get("Text") or "").strip().lower()
        if m.group(3) == "power":
            powers.setdefault(m.group(1, 2), {})[name] = value
        elif "distance" not in name:  # Intel 的“离 TjMax 还差几度”不是温度
            temps.setdefault(m.group(1, 2), {})[name] = value
    temp = [_pick(t, TEMP_NAMES, max(t.values())) for t in temps.values()]
    power = [p for p in (_pick(p, POWER_NAMES) for p in powers.values()) if p is not None]
    return (max(temp) if temp else None), (sum(power) if power else None)


def _error_text(exc):
    if isinstance(exc, HttpStatusError):
        if exc.status == 401:
            return tr("LibreHardwareMonitor 的网页服务设置了密码，无法获取 CPU 温度：请在其 Options → Remote Web Server 中关闭 Authentication。")
        return tr("LibreHardwareMonitor 的网页服务返回 HTTP {status}，无法获取 CPU 温度。", status=exc.status)
    if isinstance(exc, ValueError):
        return tr("127.0.0.1:8085 返回的不是 LibreHardwareMonitor 的数据，无法获取 CPU 温度。")
    # 连接时出错 urllib 包成 URLError。Windows 上连本机没人监听的端口要重试 2 秒才报拒绝，先到的是 1 秒超时，也算没连上
    if isinstance(exc, TimeoutError):
        return tr("LibreHardwareMonitor 的网页服务无响应，无法获取 CPU 温度。")
    return tr(NOT_RUNNING)


class LhmReader:
    """后台每秒读一次 LibreHardwareMonitor。latest() 返回 (温度, 功耗, 读不到温度的原因)。"""

    def __init__(self, url=URL):
        self.url = url
        self._latest = (0.0, None, None, tr(NOT_RUNNING))  # 读到的时间、温度、功耗、原因
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lhm", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            ok = self.poll()
            self._stop.wait(INTERVAL if ok else RETRY)

    def poll(self):
        try:
            temp, power = read_cpu(request_json(self.url, timeout=1.0))
        except (OSError, ValueError) as exc:
            self._latest = (time.time(), None, None, _error_text(exc))
            return False
        note = None if temp is not None else tr("LibreHardwareMonitor 中无 CPU 温度：需以管理员身份运行并安装驱动后方可读取。")
        self._latest = (time.time(), temp, power, note)
        return True

    def latest(self):
        t, temp, power, note = self._latest
        if note is None and time.time() - t > STALE:
            return None, None, tr("LibreHardwareMonitor 超过 {n} 秒无新数据。", n=f"{STALE:g}")
        return temp, power, note

    def close(self):
        self._stop.set()
