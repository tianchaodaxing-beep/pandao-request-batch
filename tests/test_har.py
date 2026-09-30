import base64
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from pandao_batch.core import csv_jobs
from pandao_batch.har import TaskError, arrays, inspect_har, pointer_get, public_choice, request_url, strict_json, template_for, url_valid


def document():
    return {"log": {"entries": [{"request": {"method": "GET", "url": "https://example.test/list?q=one&page=1&token=PRIVATE_VALUE",
                                           "headers": [{"name": "Authorization", "value": "Bearer PRIVATE_VALUE"}, {"name": "Host", "value": "example.test"}],
                                           "cookies": [{"name": "login", "value": "PRIVATE_VALUE"}]},
                                "response": {"status": 200, "content": {"text": json.dumps({"data": {"items": [{"id": 1}], "total": 8}})}}}]}}


def test_import_and_redacted_inspection():
    choice = inspect_har(document())[0]
    assert choice["records_pointer"] == "/data/items"
    assert choice["query_fields"] == ["q", "page"]
    assert "Host" not in choice["headers"]
    assert choice["headers"]["Cookie"] == "login=PRIVATE_VALUE"
    assert "PRIVATE_VALUE" not in json.dumps(public_choice(choice))
    assert choice["totals"] == [{"pointer": "/data/total", "value": 8}]


def test_base64_response_and_empty_array():
    data = document()
    content = data["log"]["entries"][0]["response"]["content"]
    content.update(text=base64.b64encode(b'{"rows":[]}').decode(), encoding="base64")
    assert inspect_har(data)[0]["records_pointer"] == "/rows"


@pytest.mark.parametrize("method,status,text", [("POST", 200, '{"rows":[]}'), ("GET", 302, '{"rows":[]}'), ("GET", 200, "<html>login</html>"), ("GET", 200, '{"data":123}')])
def test_unsupported_sources_are_not_offered(method, status, text):
    data = document()
    entry = data["log"]["entries"][0]
    entry["request"]["method"] = method
    entry["response"].update(status=status, content={"text": text})
    assert inspect_har(data) == []


@pytest.mark.parametrize("value", [{}, {"log": {"entries": {}}}, {"log": {"entries": ["invalid"]}}])
def test_invalid_har(value):
    with pytest.raises(TaskError):
        inspect_har(value)


@pytest.mark.parametrize("url", ["file:///data", "https://user:pass@example.test/", "https://example.test/a#b", "https://example.test:bad/"])
def test_invalid_url(url):
    with pytest.raises(TaskError):
        url_valid(url)


def test_header_injection_rejected():
    data = document()
    data["log"]["entries"][0]["request"]["headers"].append({"name": "X-Value", "value": "x\r\nInjected: yes"})
    with pytest.raises(TaskError):
        inspect_har(data)


def test_query_replacement_encodes_and_preserves_secret():
    template = template_for(inspect_har(document())[0], page_param="page")
    query = parse_qs(urlsplit(request_url(template, {"q": "中文 & other=value"}, 2)).query)
    assert query == {"q": ["中文 & other=value"], "page": ["2"], "token": ["PRIVATE_VALUE"]}


@pytest.mark.parametrize("values", [{"missing": "x"}, {"page": "99"}, {"token": "different"}])
def test_invalid_batch_columns(values):
    with pytest.raises(TaskError):
        request_url(template_for(inspect_har(document())[0], page_param="page"), values, 1)


def test_pointer_escaping():
    assert pointer_get({"a/b": {"~c": [1]}}, "/a~1b/~0c/0") == 1


@pytest.mark.parametrize("pointer", ["data", "/missing", "/a/~z", "/a/01", "/a/-"])
def test_pointer_invalid(pointer):
    with pytest.raises(TaskError):
        pointer_get({"a": [1]}, pointer)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{', '[NaN]', '[Infinity]'])
def test_strict_json(text):
    with pytest.raises(TaskError):
        strict_json(text)


def test_csv_quotes_blank_values_and_newlines():
    assert csv_jobs('\ufeffq,other\n"a,b\nnext",\n') == [{"q": "a,b\nnext", "other": ""}]


@pytest.mark.parametrize("text", ["", "q,q\n1,2\n", "q,\n1,2\n", "q\n1,2\n", "q\n"])
def test_invalid_csv(text):
    with pytest.raises(TaskError):
        csv_jobs(text)
