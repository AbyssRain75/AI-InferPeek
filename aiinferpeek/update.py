"""检查新版本：问 GitHub 公开仓库的最新发布（不算草稿和预发布），比现在的版本新就提醒，不自动下载、不替换文件。

只向 api.github.com 发一个 GET 请求，不带本机的任何数据（请求头里只有程序名和版本号）；走系统代理。
桌面版启动后过一会儿查一次，之后开着时每天查一次；托盘菜单里可以关掉自动检查，也可以随时手动查。
看板顶栏的“检查更新”按钮也用这里（app.py 的 /api/update/check）：桌面版和托盘共用同一个 UpdateChecker，命令行版只在点了按钮时查。
"""

import http.client
import json
import logging
import re
import threading
import urllib.error
import urllib.request

from . import __version__
from .i18n import tr

log = logging.getLogger("aiinferpeek")

REPO = "AbyssRain75/AI-InferPeek"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
TIMEOUT = 10  # 秒
MAX_BODY = 1024 * 1024  # 发布说明再长也到不了这么大，多的不读
FIRST_DELAY = 30  # 启动后过这么多秒再查，不和启动抢
INTERVAL = 24 * 3600  # 查到了（不管有没有新版本）之后，过一天再查
RETRY = 3600  # 没查成（断网、被限流）时，过一小时再试


class UpdateError(Exception):
    """没查成，消息是给人看的原因。"""


class NoRelease(UpdateError):
    """公开仓库里没有正式发布（GitHub 返回 404）：这时也没有比现在新的版本。"""


def parse_version(text):
    """"v1.2.0"、"1.2" 这样的版本号换成可以比大小的元组，末尾的 0 去掉（1.2 和 1.2.0 一样）；看不懂返回 None。"""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest, current=__version__):
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def fetch_latest(timeout=TIMEOUT):
    """问 GitHub 最新的正式发布，返回 (版本号, 发布页面地址)；没查成抛 UpdateError。"""
    request = urllib.request.Request(
        LATEST_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"AI-InferPeek/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read(MAX_BODY))
    except urllib.error.HTTPError as exc:
        exc.close()  # 它带着这次的响应（连接），不读了就关掉
        if exc.code == 404:
            raise NoRelease(tr("公开仓库中尚无正式发布的版本")) from exc
        if exc.code in (403, 429):
            # 只说原因：托盘手动检查的消息框后面自己有一句“请稍后重试”，这里再写就说了两遍
            raise UpdateError(tr("GitHub 暂时限制了访问次数")) from exc
        raise UpdateError(tr("GitHub 返回错误（HTTP {status}）", status=exc.code)) from exc
    except (OSError, http.client.HTTPException) as exc:  # URLError 也是 OSError
        reason = getattr(exc, "reason", None) or exc
        if isinstance(reason, TimeoutError):
            raise UpdateError(tr("连接 GitHub 超时（{n} 秒）", n=timeout)) from exc
        raise UpdateError(tr("无法连接 GitHub（{reason}）", reason=reason)) from exc
    except ValueError as exc:
        raise UpdateError(tr("无法解析 GitHub 返回的内容")) from exc
    if not isinstance(data, dict):
        raise UpdateError(tr("无法解析 GitHub 返回的内容"))
    tag = data.get("tag_name")
    version = tag.strip().lstrip("vV") if isinstance(tag, str) else ""
    if parse_version(version) is None:
        raise UpdateError(tr("无法解析最新发布的版本号 {tag}", tag=repr(tag)))
    url = data.get("html_url")
    # 只打开这个仓库自己的页面，别的地址一律换成发布列表
    if not (isinstance(url, str) and url.startswith(f"https://github.com/{REPO}/")):
        url = RELEASES_PAGE
    return version, url


class UpdateChecker:
    """后台定时检查新版本。

    enabled()：现在要不要自动检查（托盘菜单里的开关），关着时到点了也不查；
    on_found(version, url)：自动检查查到比现在新的版本时调用（在检查线程里），同一个版本只调一次；手动检查（quiet）不调。
    on_change()：查到的新版本（latest）变了时调用：查到了新的，或者之前查到的这次又不比现在新了（公开仓库撤回或改小了最新发布，
    或者发布全删了）。自动检查、托盘里的手动检查、看板上的检查都算，托盘菜单里的“下载新版本”跟着出现、去掉。
    on_found、on_change 都在 check() 里调用，不能再调 check()。
    """

    def __init__(self, enabled, on_found, fetch=fetch_latest, current=__version__, on_change=None):
        self.enabled = enabled
        self.on_found = on_found
        self.on_change = on_change
        self.fetch = fetch
        self.current = current
        self.latest = None  # 查到的比现在新的版本 (版本号, 地址)，没有是 None；只在 check() 里改，别的线程直接读
        self._lock = threading.Lock()  # check() 同时只跑一个
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="update", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """马上查一次（打开自动检查时调用）。"""
        self._wake.set()

    def check(self, quiet=False):
        """查一次，返回比现在新的 (版本号, 地址)，已经是最新返回 None；没查成抛 UpdateError。会阻塞，最多 TIMEOUT 秒。

        quiet：查到新版本也不调用 on_found（手动检查时自己告诉用户）。
        自动检查和手动检查碰在一起时，后来的等前一个查完再查，结果按先后生效，不会让先发出、后返回的旧结果盖掉新的。
        """
        with self._lock:
            try:
                version, url = self.fetch()
            except NoRelease:
                self._clear()
                raise
            if not is_newer(version, self.current):
                log.info("检查更新：已经是最新版本（最新发布 %s，现在 %s）", version, self.current)
                self._clear()
                return None
            found = (version, url)
            changed, self.latest = self.latest != found, found
            if changed:
                log.info("检查更新：发现新版本 %s（现在 %s）", version, self.current)
                self._changed()
            if changed and not quiet:
                try:
                    self.on_found(version, url)
                except Exception:
                    log.exception("处理新版本提醒失败")
            return found

    def _clear(self):
        """之前查到过的新版本没有了：去掉提醒。在 check() 里调用（拿着锁）。"""
        if self.latest is None:
            return
        self.latest = None
        self._changed()

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("更新新版本提醒失败")

    def _run(self):
        delay = FIRST_DELAY
        while True:
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return
            delay = self._tick()

    def _tick(self):
        """自动检查一次（关着时不查），返回多少秒后再来。"""
        if not self.enabled():
            return INTERVAL
        try:
            self.check()
        except UpdateError as exc:
            log.info("检查更新失败：%s，%s 秒后再试", exc, RETRY)
            return RETRY
        except Exception:
            log.exception("检查更新出错")
            return RETRY
        return INTERVAL
