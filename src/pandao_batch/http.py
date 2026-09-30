"""同一来源读取、有限重试与响应大小限制。"""
import gzip
import io
import math
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .har import TaskError, strict_json, url_valid


def origin(url):
    parts = url_valid(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise TaskError("来源转到了其他站点，请重新登录后导出浏览记录")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, *, timeout=15, retries=2, backoff=0.5, max_bytes=8 * 1024 * 1024,
                 deadline=None, receipt=None, stop=None):
        self.timeout, self.retries, self.backoff, self.max_bytes = timeout, retries, backoff, max_bytes
        self.deadline, self.receipt, self.stop = deadline, receipt or (lambda value: None), stop
        self.opener = build_opener(SameOriginRedirect())
        self.attempts, self.retry_count = 0, 0

    def remaining(self):
        if self.stop is not None and self.stop.is_set():
            raise TaskError("任务已停止，已完成页面保留，可使用同一配置续跑")
        remaining = self.deadline - time.monotonic() if self.deadline else self.timeout
        if remaining <= 0:
            raise TaskError("达到总耗时上限，已完成页面保留")
        return remaining

    def get(self, url, headers, task, page):
        url_valid(url)
        for attempt in range(self.retries + 1):
            remaining = self.remaining()
            started = time.monotonic()
            self.attempts += 1
            status, wait, error = 0, self.backoff * (2 ** attempt), None
            try:
                with self.opener.open(Request(url, headers=headers, method="GET"), timeout=min(self.timeout, remaining)) as response:
                    status = response.status
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise TaskError("单页内容过大，请缩小每页数量")
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                            raw = stream.read(self.max_bytes + 1)
                        if len(raw) > self.max_bytes:
                            raise TaskError("单页解压内容过大，请缩小每页数量")
                    content_type = response.headers.get_content_type()
                    if content_type == "text/html":
                        raise TaskError("来源返回网页而非查询数据，请重新登录并导出有效查询")
                    try:
                        text = raw.decode(response.headers.get_content_charset() or "utf-8-sig")
                    except (UnicodeError, LookupError) as failure:
                        raise TaskError("来源返回了无法读取的文字编码") from failure
                    return strict_json(text)
            except HTTPError as failure:
                status = failure.code
                if status in {401, 403}:
                    raise TaskError("登录或访问权限失效，请重新登录后建立新任务") from failure
                if status not in {429, 500, 502, 503, 504}:
                    raise TaskError(f"来源返回 HTTP {status}，请检查查询条件") from failure
                hint = failure.headers.get("Retry-After")
                if hint:
                    try:
                        wait = max(wait, float(hint))
                    except ValueError:
                        raise TaskError("来源要求按指定时间重试，请稍后续跑") from failure
                error = TaskError(f"来源返回 HTTP {status}，有限重试后仍未成功")
            except (URLError, OSError, socket.timeout) as failure:
                error = TaskError("网络连接或读取超时，有限重试后仍未成功")
            finally:
                self.receipt({"task": task, "page": page, "attempt": attempt + 1, "status": status,
                              "seconds": round(time.monotonic() - started, 4)})
            if attempt == self.retries:
                raise error
            if not math.isfinite(wait) or wait > 30 or wait >= self.remaining():
                raise TaskError("来源要求的等待超过本轮上限，请稍后续跑")
            self.retry_count += 1
            if self.stop is not None:
                self.stop.wait(max(0, wait))
            else:
                time.sleep(max(0, wait))

