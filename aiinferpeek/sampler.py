"""定时采样，保存最近一段时间的历史，并通知所有订阅的页面。"""

import asyncio
import contextlib
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .logs import ErrorThrottle

log = logging.getLogger("aiinferpeek")


class Sampler:
    def __init__(self, collectors, interval=1.0, history_seconds=600):
        self.collectors = collectors
        self.interval = interval
        # 历史里存精简版样本（只留画曲线要的字段），最新一条完整样本单独保存
        self.history = deque(maxlen=int(history_seconds / interval))
        self.latest = None
        self.info = {}
        self.seq = 0
        self._cond = asyncio.Condition()
        # PDH 和 NVML 都是阻塞调用，固定在一个线程里按顺序执行
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="collect")
        self._task = None
        self._errors = ErrorThrottle()  # 采集器一直出错时不每秒都写日志

    async def start(self):
        loop = asyncio.get_running_loop()
        self.info = await loop.run_in_executor(self._executor, self._collect_info)
        self._task = asyncio.create_task(self._run())

    async def stop(self, timeout=5.0):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # 取消只停得了采样循环，已经在采集线程里跑的那次采集会继续做完。关闭也交给采集线程：
        # 单线程按顺序执行，会排在那次采集后面，不会和它同时用 PDH / NVML
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(self._executor, self._close), timeout)
        except asyncio.TimeoutError:
            log.warning("等采集结束超过 %s 秒，不再关闭采集器", timeout)
        self._executor.shutdown(wait=False)

    def _close(self):
        for c in self.collectors:
            try:
                c.close()
            except Exception:
                log.exception("关闭 %s 失败", c.name)

    def _collect_info(self):
        info = {"interval": self.interval, "history_seconds": self.history.maxlen * self.interval}
        for c in self.collectors:
            try:
                info[c.name] = c.info()
            except Exception:
                log.exception("读取 %s 信息失败", c.name)
                info[c.name] = None
        return info

    def _collect(self):
        t = int(time.time() * 1000)
        full, slim = {"t": t}, {"t": t}
        for c in self.collectors:
            try:
                data = c.sample()
                full[c.name] = data
                slim[c.name] = c.slim(data) if hasattr(c, "slim") and data is not None else data
            except Exception:
                self._errors.exception(c.name, "采集 %s 失败", c.name)
                full[c.name] = slim[c.name] = None
        # 推给页面的完整样本带着它的精简版本：页面历史里也只留最新一条完整的，
        # 下一条来了就换成 slim，精简规则只在各采集器的 slim() 里写一份
        full["slim"] = slim
        return full, slim

    def snapshot(self):
        """页面初次连接时用：历史（精简）+ 最新一条完整样本（带着它的精简版本 slim）。"""
        history = list(self.history)
        if history and self.latest:
            history[-1] = self.latest
        return history

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            full, slim = await loop.run_in_executor(self._executor, self._collect)
            async with self._cond:
                self.history.append(slim)
                self.latest = full
                self.seq += 1
                self._cond.notify_all()
            next_tick += self.interval
            delay = next_tick - loop.time()
            if delay < 0:
                # 采集太慢就从现在重新计时，不连续补采
                next_tick = loop.time()
                delay = 0
            await asyncio.sleep(delay)

    async def wait_next(self, seq, timeout):
        """等到比 seq 更新的样本，返回 (新 seq, 样本)；超时返回 (seq, None)。"""
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self.seq > seq), timeout)
            except asyncio.TimeoutError:
                return seq, None
            return self.seq, self.latest
