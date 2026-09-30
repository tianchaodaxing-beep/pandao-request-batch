import csv
import json
from pathlib import Path
import threading

import pytest

from pandao_batch.core import Limits, RunLock, run
from pandao_batch.demo import CATEGORIES, capture, demo, demo_server
from pandao_batch.har import TaskError, inspect_har, template_for
from pandao_batch.http import Transport


def template(server):
    return template_for(inspect_har(capture(server))[0], page_param="page", total_pointer="/data/total", id_field="id")


def test_full_live_demo_and_physical_exports(tmp_path):
    result = demo(tmp_path / "demo")
    assert result["status"] == "完成" and result["records"] == 444
    assert result["completed_tasks"] == 12 and result["pages"] == 48
    assert result["attempts_this_run"] == 49 and result["retries_this_run"] == 1
    assert result["total_checked"] and result["export_checked"]
    directory = tmp_path / "demo/批量结果"
    with (directory / "查询结果.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 444 and len({row["id"] for row in rows}) == 444
    assert rows[0]["说明"] == "模拟数据\n不代表真实客户"
    assert json.loads(rows[0]["规格"])["档位"] == 2
    log = [json.loads(x) for x in (directory / "读取记录.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(log) == 49 and sum(row["status"] == 429 for row in log) == 1
    assert "demo-session" not in (directory / "结论.json").read_text(encoding="utf-8")


def test_live_failure_resume_does_not_refetch_committed_pages(tmp_path):
    with demo_server(rate_once=False, fail_from=3) as server:
        config = template(server)
        jobs = [{"category": CATEGORIES[0]}]
        limits = Limits(interval=0, retries=0)
        first = run(config, jobs, tmp_path / "result", limits=limits)
        assert first["status"] == "未完成" and first["records"] == 20
        saved = sorted((tmp_path / "result/分页结果").glob("*.json"))
        hashes = {path.name: path.read_bytes() for path in saved}
        server.fail_from = 0
        final = run(config, jobs, tmp_path / "result", limits=limits, resume=True)
        assert final["status"] == "完成" and final["records"] == 37
        assert final["pages"] == 4 and final["attempts_this_run"] == 2
        assert all((tmp_path / "result/分页结果" / name).read_bytes() == raw for name, raw in hashes.items())
        assert final["duplicates"] == 0


def test_live_page_limit_can_resume_with_higher_limit(tmp_path):
    with demo_server(rate_once=False) as server:
        config = template(server)
        jobs = [{"category": CATEGORIES[0]}]
        first = run(config, jobs, tmp_path / "result", limits=Limits(interval=0, max_pages=1))
        assert first["status"] == "未完成" and first["records"] == 10
        final = run(config, jobs, tmp_path / "result", limits=Limits(interval=0), resume=True)
        assert final["status"] == "完成" and final["records"] == 37


def test_saved_page_change_blocks_resume(tmp_path):
    with demo_server(rate_once=False) as server:
        config = template(server)
        jobs = [{"category": CATEGORIES[0]}]
        run(config, jobs, tmp_path / "result", limits=Limits(interval=0))
        path = next((tmp_path / "result/分页结果").glob("*.json"))
        path.write_text("{}")
        with pytest.raises(TaskError, match="变化"):
            run(config, jobs, tmp_path / "result", resume=True)


def test_changed_login_blocks_mixing_results(tmp_path):
    with demo_server(rate_once=False) as server:
        config = template(server)
        jobs = [{"category": CATEGORIES[0]}]
        run(config, jobs, tmp_path / "result", limits=Limits(interval=0))
        config["headers"]["Cookie"] = "different"
        with pytest.raises(TaskError, match="配置"):
            run(config, jobs, tmp_path / "result", resume=True)


def test_existing_directory_is_not_overwritten(tmp_path):
    with demo_server(rate_once=False) as server:
        config = template(server)
        directory = tmp_path / "result"
        directory.mkdir()
        protected = directory / "结论.json"
        protected.write_text("existing")
        with pytest.raises(TaskError, match="存在"):
            run(config, [{"category": CATEGORIES[0]}], directory)
        assert protected.read_text() == "existing"


def fake_template():
    return {"url": "https://example.test/list?q=a&page=1", "headers": {}, "records_pointer": "/items", "total_pointer": "/total", "page_param": "page", "id_field": "id"}


@pytest.mark.parametrize("responses,message", [
    ([{"items": [{"id": 1}], "total": 3}, {"items": [], "total": 3}], "不一致"),
    ([{"items": [{"id": 1}], "total": 3}, {"items": [{"id": 2}], "total": 4}], "总量发生变化"),
    ([{"items": [{"id": 1}], "total": 3}, {"items": [{"id": 1}], "total": 3}], "重复页面"),
    ([{"items": "invalid", "total": 3}], "结构变化"),
    ([{"items": [{}], "total": 1}], "唯一编号"),
    ([{"items": [{"id": 1}], "total": "1"}], "有效整数"),
])
def test_page_failures_never_claim_completion(tmp_path, monkeypatch, responses, message):
    values = iter(responses)
    monkeypatch.setattr(Transport, "get", lambda *args: next(values))
    result = run(fake_template(), [{"q": "a"}], tmp_path / "result", limits=Limits(interval=0))
    assert result["status"] == "未完成" and message in result["error"]
    assert not result["total_checked"]


def test_overlap_deduplicates_per_query(tmp_path, monkeypatch):
    values = iter([{"items": [{"id": 1}, {"id": 2}], "total": 3}, {"items": [{"id": 2}, {"id": 3}], "total": 3}])
    monkeypatch.setattr(Transport, "get", lambda *args: next(values))
    result = run(fake_template(), [{"q": "a"}], tmp_path / "result", limits=Limits(interval=0))
    assert result["records"] == 3 and result["duplicates"] == 1 and result["total_checked"]


def test_no_total_requires_terminal_empty_page(tmp_path, monkeypatch):
    values = iter([{"items": [{"id": 1}]}, {"items": []}])
    monkeypatch.setattr(Transport, "get", lambda *args: next(values))
    config = fake_template()
    config["total_pointer"] = ""
    result = run(config, [{"q": "a"}], tmp_path / "result", limits=Limits(interval=0))
    assert result["status"] == "完成" and result["pages"] == 2
    assert not result["total_checked"]


@pytest.mark.parametrize("limits", [Limits(timeout=0), Limits(max_seconds=float("nan")), Limits(max_pages=0), Limits(retries=9), Limits(interval=-1)])
def test_invalid_limits(tmp_path, limits):
    with pytest.raises(TaskError):
        run(fake_template(), [{"q": "a"}], tmp_path / "result", limits=limits)


def test_record_limit_preserves_prior_page(tmp_path, monkeypatch):
    values = iter([{"items": [{"id": 1}], "total": 2}, {"items": [{"id": 2}], "total": 2}])
    monkeypatch.setattr(Transport, "get", lambda *args: next(values))
    result = run(fake_template(), [{"q": "a"}], tmp_path / "result", limits=Limits(max_records=1, interval=0))
    assert result["status"] == "未完成" and result["records"] == 1


def test_stop_and_lock(tmp_path):
    event = threading.Event()
    event.set()
    result = run(fake_template(), [{"q": "a"}], tmp_path / "result", stop=event)
    assert result["status"] == "未完成" and "停止" in result["error"]
    with RunLock(tmp_path / ".lock"):
        with pytest.raises(TaskError, match="另一项"):
            with RunLock(tmp_path / ".lock"):
                pass
