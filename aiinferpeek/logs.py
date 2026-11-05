"""日志：同样的错误反复出现时少记几次（ErrorThrottle），桌面版的日志文件有大小上限（LogFile）。"""

import io
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("aiinferpeek")


class ErrorThrottle:
    """后台每秒采集、轮询，一个错误一直不好就会每秒写一遍错误堆栈，日志越写越大。

    同一处（key）的同一个错误（类型和内容都一样）第一次照常写完整的错误堆栈；之后 every 秒内再出现只计数，
    过了这段时间还在出现就再写一次，带上中间省略了几次。不同的错误各算各的；
    出错和正常来回切换时也只按时间算，不会每出现一次就写一次。
    """

    MAX_KEYS = 200  # 错误内容每次都不一样（比如带着时间）时，不让记录无限变多

    def __init__(self, every=3600.0, clock=time.monotonic):
        self.every = every
        self._clock = clock
        self._seen = {}  # (key, 错误类型, 错误内容) -> [上次写日志的时间, 之后省略的次数]
        self._lock = threading.Lock()

    def exception(self, key, msg, *args):
        """在 except 里代替 log.exception(msg, *args)。写了日志返回 True，省略了返回 False。"""
        exc = sys.exc_info()[1]
        ident = (key, type(exc).__name__, str(exc))
        now = self._clock()
        with self._lock:
            seen = self._seen.get(ident)
            if seen and now - seen[0] < self.every:
                seen[1] += 1
                return False
            skipped = seen[1] if seen else 0
            if not seen and len(self._seen) >= self.MAX_KEYS:
                self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self.every}
                if len(self._seen) >= self.MAX_KEYS:
                    self._seen.clear()
            self._seen[ident] = [now, 0]
        if skipped:
            msg, args = f"{msg}（上次记录以来同样的错误又出现了 %d 次）", (*args, skipped)
        log.exception(msg, *args, stacklevel=2)
        return True


class LogFile(io.TextIOBase):
    """打包后的 exe 没有控制台，stdout、stderr 和日志都写到这个文件。

    写到超过 max_bytes 时把它改名成 xxx.old.log（替换掉更早的那份）再从头写，所以最多占两倍 max_bytes 左右；
    启动时已经超过的也先换掉。不用 logging 自带的 RotatingFileHandler：stdout、stderr 也写进同一个文件，
    Windows 上文件被打开着就改不了名，只能由唯一打开它的这里来换。写不进去时不报错，日志不能把程序带崩。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path, max_bytes):
        super().__init__()
        self.path = Path(path)
        self.old_path = self.path.with_name(f"{self.path.stem}.old{self.path.suffix}")
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._open()
        if self._size > max_bytes:
            self._rotate()

    def _open(self):
        self._file = open(self.path, "ab")
        self._size = os.fstat(self._file.fileno()).st_size

    def _rotate(self):
        self._file.close()
        try:
            os.replace(self.path, self.old_path)
        except OSError:
            # 改不了名（比如旧的那份正被别的程序占着）就接着写这一份，再多写 max_bytes 后再试
            self._file = open(self.path, "ab")
            self._size = 0
        else:
            self._open()

    def write(self, s):
        data = s.encode("utf-8", "replace")
        with self._lock:
            try:
                if self._file.closed:
                    self._open()  # 上次换文件时没打开成功
                self._file.write(data)
                self._file.flush()
                self._size += len(data)
                if self._size > self.max_bytes:
                    self._rotate()
            except (OSError, ValueError):
                pass
        return len(s)

    def flush(self):
        with self._lock:
            try:
                self._file.flush()
            except (OSError, ValueError):
                pass

    def writable(self):
        return True

    def fileno(self):
        return self._file.fileno()

    def close(self):
        super().close()
        with self._lock:
            self._file.close()
