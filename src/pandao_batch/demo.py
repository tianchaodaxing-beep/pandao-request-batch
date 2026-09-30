"""本机模拟目录服务：多查询、多页、登录与临时限流。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .core import Limits, atomic_json, run
from .har import TaskError, inspect_har, template_for

CATEGORIES = ["制造", "印刷", "酒店", "培训", "设计", "餐饮", "物流", "软件", "维修", "农业", "零售", "会展"]


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path != "/catalog":
            self.send_error(404)
            return
        if self.headers.get("Cookie") != "demo-session=local-only":
            self.send_response(401)
            self.end_headers()
            return
        query = parse_qs(parts.query, keep_blank_values=True)
        category = query.get("category", [CATEGORIES[0]])[0]
        page, size = int(query.get("page", ["1"])[0]), int(query.get("size", ["10"])[0])
        if self.server.fail_from and category == CATEGORIES[0] and page >= self.server.fail_from:
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if self.server.rate_once and category == CATEGORIES[0] and page == 2:
            self.server.rate_once = False
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        records = [{"id": category + "-" + str(i), "类别": category, "名称": "模拟项目," + str(i),
                    "说明": "模拟数据\n不代表真实客户", "规格": {"档位": i % 3 + 1}} for i in range(1, 38)] if category in CATEGORIES else []
        result = {"示例性质": "本机模拟数据", "data": {"items": records[(page - 1) * size:page * size], "total": len(records)}}
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def demo_server(*, rate_once=True, fail_from=0):
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    server.daemon_threads = True
    server.rate_once, server.fail_from = rate_once, fail_from
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def capture(server):
    url = f"http://127.0.0.1:{server.server_address[1]}/catalog?category={CATEGORIES[0]}&page=1&size=10"
    from urllib.parse import quote
    url = quote(url, safe=":/?=&")
    headers = {"Cookie": "demo-session=local-only", "Accept": "application/json"}
    with urlopen(Request(url, headers=headers)) as response:
        text = response.read().decode("utf-8")
    return {"log": {"version": "1.2", "creator": {"name": "PANDAO 本机模拟服务", "version": "1"},
                    "entries": [{"request": {"method": "GET", "url": url,
                                              "headers": [{"name": name, "value": value} for name, value in headers.items()]},
                                 "response": {"status": 200, "content": {"mimeType": "application/json", "text": text}}}]}}


def demo(out):
    out = Path(out).resolve()
    if out.exists():
        raise TaskError("演示目录已存在，请换一个新目录")
    out.mkdir(parents=True)
    with demo_server() as server:
        document = capture(server)
        atomic_json(out / "模拟浏览记录.har", document)
        choice = inspect_har(document)[0]
        template = template_for(choice, total_pointer="/data/total", page_param="page", id_field="id")
        atomic_json(out / "读取配置.json", template)
        jobs = [{"category": category} for category in CATEGORIES]
        (out / "查询参数.csv").write_text("category\n" + "\n".join(CATEGORIES) + "\n", encoding="utf-8-sig", newline="\n")
        result = run(template, jobs, out / "批量结果", limits=Limits(interval=0))
    result["simulated"] = True
    atomic_json(out / "批量结果/结论.json", result)
    expected = {category + "-" + str(i) for category in CATEGORIES for i in range(1, 38)}
    exported = [json.loads(line) for line in (out / "批量结果/查询结果.jsonl").read_text(encoding="utf-8").splitlines()]
    if result["status"] != "完成" or len(exported) != 444 or {x["记录"]["id"] for x in exported} != expected or not result["total_checked"]:
        raise TaskError("模拟演示的数量与名单核对未通过")
    conclusion = out / "批量结果/结论.txt"
    conclusion.write_text("示例性质：本机模拟数据，不代表真实客户或商业效果。\n" + conclusion.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return result
