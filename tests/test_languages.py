import json
import pandao_batch.i18n as i18n

def test_language_option_preserves_target_command():
    assert i18n.configure(["--lang", "en", "run", "--", "python", "--lang", "zh"]) == ["run", "--", "python", "--lang", "zh"]
    i18n.configure(["--lang", "zh"])

def test_summary_preserves_metadata(tmp_path):
    report = {"status": "完成", "input_units": 12, "output_units": 3, "executions": 7, "records": 444}
    before = json.dumps(report, ensure_ascii=False)
    i18n.write_summary(tmp_path, report)
    assert json.dumps(report, ensure_ascii=False) == before
    text = (tmp_path / "Summary.en.md").read_text(encoding="utf-8")
    assert "Input units: 12" in text and "Output units: 3" in text
    assert "Executions: 7" in text and "Exported records: 444" in text
