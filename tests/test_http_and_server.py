from contextlib import contextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import pytest

from pandao_batch.har import TaskError
from pandao_batch.http import Transport
from pandao_batch.server import make_server


class Responses(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls += 1
        if self.path == "/denied":
            self.send_response(403)
            self.end_headers()
            return
        if self.path in {"/throttle", "/long-wait"}:
            self.send_response(429)
            self.send_header("Retry-After", "99" if self.path == "/long-wait" else "0")
            self.end_headers()
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/other")
            self.end_headers()
            return
        payload = b'{"items":[{"id":1}]}'
        content_type, encoding = "application/json", None
        if self.path == "/html":
            payload, content_type = b"<html>login</html>", "text/html"
        elif self.path == "/gzip":
            payload, encoding = gzip.compress(payload), "gzip"
        elif self.path == "/big":
            payload = b"x" * 2048
        elif self.path == "/bad-json":
            payload = b'{"items":'
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def response_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Responses)
    server.calls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("path,message", [("/denied", "访问权限"), ("/html", "网页"), ("/redirect", "其他站点"), ("/bad-json", "JSON"), ("/big", "过大"), ("/long-wait", "等待")])
def test_http_boundary_failures(path, message):
    with response_server() as (server, url):
        transport = Transport(backoff=0, max_bytes=1024)
        with pytest.raises(TaskError, match=message):
            transport.get(url + path, {}, 1, 1)
        assert server.calls == 1


def test_http_retry_count_is_bounded():
    with response_server() as (server, url):
        receipts = []
        transport = Transport(backoff=0, retries=2, receipt=receipts.append)
        with pytest.raises(TaskError, match="重试"):
            transport.get(url + "/throttle", {}, 1, 1)
        assert server.calls == 3 and len(receipts) == 3 and transport.retry_count == 2


def test_gzip_and_deadline():
    with response_server() as (server, url):
        assert Transport().get(url + "/gzip", {}, 1, 1) == {"items": [{"id": 1}]}
        with pytest.raises(TaskError, match="耗时"):
            Transport(deadline=time.monotonic() - 1).get(url, {}, 1, 1)


def test_local_web_page_and_complete_task(tmp_path):
    server = make_server(0, tmp_path / "web-results")
    url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def api(path, value=None, *, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Task-Token"] = server.token
        if origin:
            headers["Origin"] = origin
        data = json.dumps(value).encode() if value is not None else None
        with urlopen(Request(url + path, data=data, headers=headers)) as response:
            return response.read(), response.headers
    try:
        with urlopen(url) as response:
            html = response.read().decode("utf-8")
            assert "网页批量查询" in html and "__TOKEN__" not in html
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        with pytest.raises(HTTPError) as error:
            api("/api/demo", {}, token=False)
        assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            api("/api/demo", {}, origin="http://unrelated.test")
        assert error.value.code == 403
        content, _ = api("/api/demo", {})
        demo = json.loads(content)
        choice = demo["choices"][0]
        assert "headers" not in choice
        api("/api/run", {"entry": choice["entry"], "csv": demo["csv"], "page_param": "page", "total_pointer": "/data/total", "id_field": "id", "interval": 0})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = json.loads(api("/api/status")[0])
            if state["status"] != "运行中":
                break
            time.sleep(0.1)
        assert state["status"] == "完成" and state["records"] == 444
        content, headers = api("/api/export")
        assert headers["Content-Type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert set(archive.namelist()) == {"查询结果.csv", "查询结果.jsonl", "结论.txt", "结论.json", "Summary.en.md"}
            assert "Exported records: 444" in archive.read("Summary.en.md").decode("utf-8")
            assert json.loads(archive.read("结论.json"))["total_checked"]
    finally:
        server.shutdown()
        server.workspace.stack.close()
        server.server_close()
        thread.join(5)
