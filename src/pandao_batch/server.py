"""仅监听本机的操作页面；浏览记录与查询结果不发往其他服务。"""
from contextlib import ExitStack
import csv
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
import webbrowser
import zipfile

from dataclasses import replace
from .core import Limits, csv_jobs, run
from .demo import CATEGORIES, capture, demo_server
from .har import TaskError, inspect_har, public_choice, strict_json, template_for


class Workspace:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.choices = []
        self.status = {"status": "尚未开始", "records": 0, "tasks": 0, "page": 0}
        self.result_path = None
        self.last_task = None
        self.stop = threading.Event()
        self.guard = threading.Lock()
        self.stack = ExitStack()
        self.demo_source = None

    def import_har(self, document):
        with self.guard:
            if self.status["status"] == "运行中":
                raise TaskError("任务运行中，请停止或等待完成后更换来源")
            self.choices = inspect_har(document)
            if not self.choices:
                raise TaskError("没有可用读取来源，请导出包含 JSON 查询结果的 GET 浏览记录")
            return [public_choice(x) for x in self.choices]

    def demonstration(self):
        with self.guard:
            if self.status["status"] == "运行中":
                raise TaskError("任务运行中，请等待完成")
            if self.demo_source is None:
                self.demo_source = self.stack.enter_context(demo_server())
        choices = self.import_har(capture(self.demo_source))
        return {"choices": choices, "csv": "category\n" + "\n".join(CATEGORIES) + "\n", "simulated": True}

    def start(self, values):
        with self.guard:
            if self.status["status"] == "运行中":
                raise TaskError("已有任务运行中，请等待完成")
            resume = bool(values.get("resume"))
            if resume:
                if not self.last_task or not self.result_path:
                    raise TaskError("没有可以续跑的任务")
                template, jobs, limits = self.last_task
                limits = replace(limits, max_pages=int(values.get("max_pages", limits.max_pages)), interval=float(values.get("interval", limits.interval)))
                self.last_task = template, jobs, limits
            else:
                choice = next((x for x in self.choices if x["entry"] == values.get("entry")), None)
                if choice is None:
                    raise TaskError("请先选择读取来源")
                template = template_for(choice, records_pointer=values.get("records_pointer", choice["records_pointer"]),
                                        total_pointer=values.get("total_pointer", ""), page_param=values.get("page_param", ""), id_field=values.get("id_field", ""))
                jobs = csv_jobs(values.get("csv", ""))
                limits = Limits(max_pages=int(values.get("max_pages", 100)), interval=float(values.get("interval", 0.2)))
                self.result_path = self.directory / ("查询-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
                self.last_task = template, jobs, limits
            self.stop.clear()
            self.status = {"status": "运行中", "records": 0, "tasks": len(jobs), "page": 0}
        def progress(value):
            with self.guard:
                self.status.update(value)
        def worker():
            try:
                result = run(template, jobs, self.result_path, limits=limits, resume=resume, progress=progress, stop=self.stop)
                with self.guard:
                    self.status = result
            except Exception as error:
                with self.guard:
                    self.status = {"status": "未完成", "error": str(error) if isinstance(error, (TaskError, OSError)) else "程序异常，请查看终端错误", "records": 0}
                if not isinstance(error, (TaskError, OSError)):
                    import traceback
                    traceback.print_exc()
        threading.Thread(target=worker, daemon=True).start()
        return self.status.copy()

    def results_zip(self):
        with self.guard:
            if self.status["status"] == "运行中" or not self.result_path or not (self.result_path / "结论.txt").exists():
                raise TaskError("还没有可以导出的结果")
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in ["查询结果.csv", "查询结果.jsonl", "结论.txt", "结论.json", "Summary.en.md"]:
                    archive.write(self.result_path / name, name)
            return stream.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, payload, content_type="application/json; charset=utf-8", status=200, filename=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        if self.headers.get("Host") != expected:
            return False
        if self.headers.get("Origin") not in {None, "http://" + expected}:
            return False
        return secrets.compare_digest(self.headers.get("X-Task-Token", ""), self.server.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if not self.authorized():
                self.reply({"error": "请从本机操作页面进入"}, status=403)
                return
            if path == "/api/status":
                with self.server.workspace.guard:
                    self.reply(self.server.workspace.status.copy())
            elif path == "/api/export":
                try:
                    self.reply(self.server.workspace.results_zip(), "application/zip", filename="query-results.zip")
                except TaskError as error:
                    self.reply({"error": str(error)}, status=400)
            else:
                self.reply({"error": "页面不存在"}, status=404)
            return
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path not in static or self.headers.get("Host") != f"127.0.0.1:{self.server.server_address[1]}":
            self.reply({"error": "页面不存在"}, status=404)
            return
        name, kind = static[path]
        payload = (Path(__file__).parent / "web" / name).read_bytes()
        if name == "index.html":
            payload = payload.replace(b"__TOKEN__", self.server.token.encode("ascii"))
        self.reply(payload, kind)

    def do_POST(self):
        if not self.authorized():
            self.reply({"error": "请从本机操作页面进入"}, status=403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 34 * 1024 * 1024:
                raise TaskError("文件过大，请只导出相关查询，单次不超过 32 MiB")
            values = strict_json(self.rfile.read(size).decode("utf-8"))
            path = urlsplit(self.path).path
            workspace = self.server.workspace
            if path == "/api/import":
                result = {"choices": workspace.import_har(strict_json(values["har"]))}
            elif path == "/api/demo":
                result = workspace.demonstration()
            elif path == "/api/run":
                result = workspace.start(values)
            elif path == "/api/stop":
                workspace.stop.set()
                result = {"status": "正在停止"}
            else:
                self.reply({"error": "页面不存在"}, status=404)
                return
            self.reply(result)
        except (TaskError, ValueError, KeyError, TypeError, UnicodeError) as error:
            self.reply({"error": str(error) if isinstance(error, TaskError) else "输入内容不完整，请检查文件和选择项"}, status=400)


def make_server(port, workdir):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.workspace = Workspace(workdir)
    server.token = secrets.token_hex(24)
    return server


def serve(port, workdir, open_browser=False):
    server = make_server(port, workdir)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("本机操作页面：" + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("本机页面已停止。")
    finally:
        server.workspace.stop.set()
        server.workspace.stack.close()
        server.server_close()
