"""批量任务、分页检查、断点记录和独立导出核对。"""
import csv
from dataclasses import dataclass
import datetime
import io
import json
from .i18n import write_summary
import math
import os
from pathlib import Path
import time
import uuid

from .har import TaskError, canonical, pointer_get, request_url, sha, strict_json, template_for
from .http import Transport


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class RunLock:
    def __init__(self, path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise TaskError("这个结果目录正在被另一项任务使用，请等它完成") from error
        return self

    def __exit__(self, *args):
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def csv_jobs(text):
    try:
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
        fields = next(reader, [])
        if not fields or any(not key for key in fields) or len(set(fields)) != len(fields):
            raise TaskError("参数表需要不重复的列名")
        rows = list(reader)
        if not rows or len(rows) > 10000 or any(len(row) != len(fields) for row in rows):
            raise TaskError("参数表需有 1 至 10,000 行，且每行列数与表头一致")
        return [dict(zip(fields, row)) for row in rows]
    except csv.Error as error:
        raise TaskError("参数表格式无效，请使用 CSV 文件") from error


@dataclass
class Limits:
    start_page: int = 1
    max_pages: int = 100
    max_records: int = 100000
    timeout: float = 15
    max_seconds: float = 600
    retries: int = 2
    interval: float = 0.2
    max_bytes: int = 8 * 1024 * 1024


def record_key(record, field):
    if field:
        if field not in record or record[field] is None or isinstance(record[field], (dict, list)):
            raise TaskError("返回记录缺少有效的唯一编号，请检查唯一编号选择")
        return canonical(record[field])
    return canonical(record)


def materialize(directory, state, template):
    output, duplicates, keys = [], 0, {}
    for item in state["pages"]:
        path = directory / item["file"]
        if not path.resolve().is_relative_to((directory / "分页结果").resolve()) or not path.is_file():
            raise TaskError("已保存的页面文件位置无效")
        raw = path.read_bytes()
        if sha(raw) != item["sha256"]:
            raise TaskError("已保存的页面文件发生变化，不能继续使用原结论")
        page = strict_json(raw.decode("utf-8"))
        if page["task"] != item["task"] or page["page"] != item["page"] or len(page["records"]) != item["count"] or sha(page["records"]) != item["records_sha256"]:
            raise TaskError("页面记录与断点信息不一致")
        task_keys = keys.setdefault(item["task"], set())
        for record in page["records"]:
            key = record_key(record, template["id_field"])
            if key in task_keys:
                duplicates += 1
                continue
            task_keys.add(key)
            output.append({"查询序号": item["task"], "页码": item["page"], "记录": record})
    return output, duplicates, keys


def export(directory, state, template):
    rows, duplicates, keys = materialize(directory, state, template)
    jsonl = directory / "查询结果.jsonl"
    jsonl.write_text("".join(canonical(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
    fields = []
    for row in rows:
        for field in row["记录"]:
            if field not in fields:
                fields.append(field)
    provenance = "查询序号"
    while provenance in fields:
        provenance = "_" + provenance
    page_field = "读取页码"
    while page_field in fields or page_field == provenance:
        page_field = "_" + page_field
    with (directory / "查询结果.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[provenance, page_field] + fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            values = {key: canonical(value) if isinstance(value, (dict, list)) else value for key, value in row["记录"].items()}
            writer.writerow({provenance: row["查询序号"], page_field: row["页码"], **values})
    json_count = sum(1 for line in jsonl.read_text(encoding="utf-8").splitlines() if line)
    with (directory / "查询结果.csv").open(encoding="utf-8-sig", newline="") as stream:
        csv_count = sum(1 for _ in csv.DictReader(stream))
    if json_count != len(rows) or csv_count != len(rows):
        raise TaskError("导出记录数核对不一致")
    return {"records": len(rows), "duplicates": duplicates, "export_checked": True,
            "per_task": {str(i): len(values) for i, values in keys.items()}}


def run(template, jobs, out, *, limits=None, resume=False, progress=None, stop=None):
    started = time.monotonic()
    limits, progress = limits or Limits(), progress or (lambda value: None)
    if any(not math.isfinite(value) or value <= 0 for value in [limits.timeout, limits.max_seconds]) or not math.isfinite(limits.interval) or limits.interval < 0:
        raise TaskError("等待时间与总耗时必须大于零，查询间隔不能小于零")
    if limits.start_page < 1 or limits.max_pages < 1 or limits.max_records < 1 or not 0 <= limits.retries <= 5 or limits.max_bytes < 1:
        raise TaskError("页数、记录限额或重试次数无效")
    if not jobs or any(not isinstance(row, dict) for row in jobs):
        raise TaskError("参数表没有有效查询")
    template = template_for(template, records_pointer=template["records_pointer"], total_pointer=template.get("total_pointer", ""),
                            page_param=template.get("page_param", ""), id_field=template.get("id_field", ""))
    if template["page_param"] and template["total_pointer"] and limits.start_page != 1:
        raise TaskError("核对来源总量时需要从第 1 页开始")
    for job in jobs:
        request_url(template, job, limits.start_page)
    identity = sha({"template": template, "jobs": jobs, "start_page": limits.start_page})
    out = Path(out).resolve()
    if out.exists() and not resume:
        raise TaskError("结果目录已存在，请选择新目录或使用续跑")
    if resume and not out.is_dir():
        raise TaskError("没有可续跑的结果目录")
    out.mkdir(parents=True, exist_ok=resume)
    (out / "分页结果").mkdir(exist_ok=True)
    state_path = out / "断点.json"
    with RunLock(out / ".运行锁"):
        if resume:
            state = strict_json(state_path.read_text(encoding="utf-8"))
            if state.get("identity") != identity:
                raise TaskError("读取配置、登录信息或参数表发生变化，请建立新任务")
        else:
            state = {"version": 1, "identity": identity, "pages": [], "tasks": {str(i): {"done": False, "next_page": limits.start_page, "total": None} for i in range(1, len(jobs) + 1)}}
            atomic_json(state_path, state)
        _, _, keys = materialize(out, state, template)
        receipts = out / "读取记录.jsonl"
        def receipt(value):
            with receipts.open("a", encoding="utf-8") as stream:
                stream.write(canonical(value) + "\n")
        transport = Transport(timeout=limits.timeout, retries=limits.retries, max_bytes=limits.max_bytes,
                              deadline=started + limits.max_seconds, receipt=receipt, stop=stop)
        result = {"status": "未完成", "tasks": len(jobs), "resumed": resume, "source_time": "来源未提供数据更新时间"}
        try:
            for number, job in enumerate(jobs, 1):
                task = state["tasks"][str(number)]
                if task["done"]:
                    continue
                seen = keys.setdefault(number, set())
                hashes = {item["records_sha256"] for item in state["pages"] if item["task"] == number and item["count"]}
                while not task["done"]:
                    if task["next_page"] >= limits.start_page + limits.max_pages:
                        raise TaskError("达到每项查询的页数上限但仍未读完，请增加页数后续跑")
                    page_number = task["next_page"]
                    value = transport.get(request_url(template, job, page_number), template["headers"], number, page_number)
                    records = pointer_get(value, template["records_pointer"])
                    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
                        raise TaskError("来源数据结构变化，请重新选择记录位置")
                    total = pointer_get(value, template["total_pointer"]) if template["total_pointer"] else None
                    if total is not None and (type(total) is not int or total < 0):
                        raise TaskError("来源总量不是有效整数，请重新选择总量位置")
                    if task["total"] is not None and total != task["total"]:
                        raise TaskError("翻页期间来源总量发生变化，请建立新任务重新读取")
                    page_hash = sha(records)
                    if records and page_hash in hashes and template["page_param"]:
                        raise TaskError("连续读取到了重复页面，请检查翻页参数")
                    new_keys = {record_key(row, template["id_field"]) for row in records}
                    unique_count = len(seen | new_keys)
                    if sum(len(v) for v in keys.values()) - len(seen) + unique_count > limits.max_records:
                        raise TaskError("达到记录数量上限，请缩小查询范围或增加限额后续跑")
                    if total is not None and unique_count > total:
                        raise TaskError("实际记录超过来源总量，请重新核对读取范围")
                    done = not template["page_param"] or not records or (total is not None and unique_count == total)
                    if done and total is not None and unique_count != total:
                        raise TaskError("查询结束但记录数与来源总量不一致，已保存页面保留")
                    name = "分页结果/" + f"{number:05d}-{page_number:06d}.json"
                    payload = {"task": number, "page": page_number, "records": records, "total": total}
                    atomic_json(out / name, payload)
                    state["pages"].append({"task": number, "page": page_number, "file": name, "sha256": sha((out / name).read_bytes()),
                                           "count": len(records), "records_sha256": page_hash})
                    seen.update(new_keys)
                    if records:
                        hashes.add(page_hash)
                    task.update(done=done, next_page=page_number + 1, total=total)
                    atomic_json(state_path, state)
                    progress({"task": number, "tasks": len(jobs), "page": page_number, "pages": len(state["pages"]),
                              "completed_tasks": sum(x["done"] for x in state["tasks"].values()), "records": sum(len(v) for v in keys.values())})
                    if not task["done"] or number < len(jobs):
                        delay = min(limits.interval, transport.remaining())
                        if stop is not None:
                            stop.wait(delay)
                        else:
                            time.sleep(delay)
            result["status"] = "完成"
        except TaskError as error:
            result["error"] = str(error)
        except KeyboardInterrupt:
            result["error"] = "任务已停止，已完成页面保留"
        except Exception as error:
            result["error"] = "程序异常：" + type(error).__name__ + "；保留结果并检查终端错误"
            raise
        finally:
            result.update(export(out, state, template))
            result.update(completed_tasks=sum(x["done"] for x in state["tasks"].values()), pages=len(state["pages"]),
                          attempts_this_run=transport.attempts, retries_this_run=transport.retry_count,
                          seconds=round(time.monotonic() - started, 3), completed_at=datetime.datetime.now().astimezone().isoformat(),
                          total_checked=bool(template["total_pointer"]) and all(x["done"] and x["total"] == result["per_task"].get(k, 0) for k, x in state["tasks"].items()))
            atomic_json(out / "结论.json", result)
            write_summary(out, result)
            lines = ["本轮结论：" + result["status"], f"查询：{result['completed_tasks']} / {result['tasks']} 项完成",
                     f"保存页面：{result['pages']} 页；导出：{result['records']} 条；重复：{result['duplicates']} 条",
                     "导出记录数核对：通过", "来源总量核对：" + ("通过" if result["total_checked"] else "没有全部通过或来源未提供总量"),
                     "采集完成时间：" + result["completed_at"], "数据更新时间：" + result["source_time"],
                     f"本轮请求：{result['attempts_this_run']} 次；重试：{result['retries_this_run']} 次；耗时：{result['seconds']} 秒"]
            if result.get("error"):
                lines.append("停止原因：" + result["error"])
                lines.append("下一步：按停止原因处理后，使用同一配置和结果目录续跑；更换登录或读取配置时建立新任务。")
            (out / "结论.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return result
