"""从本机浏览记录中提取读取请求，检查 JSON 记录位置。"""
import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class TaskError(Exception):
    """能直接向使用者说明的错误。"""


SECRET = re.compile(r"token|secret|password|passwd|signature|credential|authorization|api[-_]?key|session|cookie", re.I)
DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "proxy-authorization"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode("utf-8")).hexdigest()


def strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise TaskError("数据中有重复名称：" + key)
            result[key] = value
        return result
    def reject(value):
        raise TaskError("数据含非标准数字")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise TaskError("内容不是有效 JSON 数据") from error


def pointer_get(value, pointer):
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise TaskError("记录位置必须为空或以 / 开头")
    current = value
    for part in pointer[1:].split("/"):
        if re.search(r"~(?![01])", part):
            raise TaskError("记录位置转义无效")
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                current = current[int(key)]
            elif isinstance(current, dict):
                current = current[key]
            else:
                raise TaskError("指定的数据位置不存在")
        except (KeyError, IndexError) as error:
            raise TaskError("指定的数据位置不存在") from error
    return current


def arrays(value, path="", depth=0):
    if depth > 20:
        return []
    if isinstance(value, list):
        return [{"pointer": path, "count": len(value), "fields": list(value[0]) if value and isinstance(value[0], dict) else []}] if all(isinstance(x, dict) for x in value) else []
    if not isinstance(value, dict):
        return []
    found = []
    for key, child in value.items():
        escaped = key.replace("~", "~0").replace("/", "~1")
        found += arrays(child, path + "/" + escaped, depth + 1)
    return found


def totals(value, path="", depth=0):
    if not isinstance(value, dict) or depth > 20:
        return []
    found = []
    for key, child in value.items():
        location = path + "/" + key.replace("~", "~0").replace("/", "~1")
        if key.lower() in {"total", "total_count", "totalcount", "count", "总数", "总量"} and type(child) is int and child >= 0:
            found.append({"pointer": location, "value": child})
        found += totals(child, location, depth + 1)
    return found


def url_valid(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise TaskError("读取地址需要正常的 HTTP/HTTPS 地址，不能含嵌入的账号或片段")
    try:
        parts.port
    except ValueError as error:
        raise TaskError("读取地址端口无效") from error
    return parts


def masked_url(url):
    parts = url_valid(url)
    query = [(key, "已隐藏" if SECRET.search(key) else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def inspect_har(document):
    try:
        entries = document["log"]["entries"]
    except (KeyError, TypeError) as error:
        raise TaskError("请选择浏览器导出的 HAR 浏览记录") from error
    if not isinstance(entries, list):
        raise TaskError("浏览记录格式无效")
    selected = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TaskError("浏览记录含无效条目")
        request, response = entry.get("request", {}), entry.get("response", {})
        if not isinstance(request, dict) or not isinstance(response, dict):
            raise TaskError("浏览记录含无效请求或结果")
        if request.get("method", "").upper() != "GET" or response.get("status") != 200:
            continue
        content = response.get("content", {})
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            if content.get("encoding") == "base64":
                text = base64.b64decode(text, validate=True).decode("utf-8-sig")
            value = strict_json(text)
            candidates = arrays(value)
            total_locations = totals(value)
            if not candidates:
                continue
            parts = url_valid(request["url"])
        except (TaskError, ValueError, UnicodeError, KeyError):
            continue
        headers = {}
        for header in request.get("headers", []):
            name, value = header.get("name", ""), header.get("value", "")
            if not isinstance(name, str) or not isinstance(value, str):
                raise TaskError("浏览记录含无效请求头")
            if name and not name.startswith(":") and name.lower() not in DROP_HEADERS:
                if "\r" in name + value or "\n" in name + value:
                    raise TaskError("浏览记录含无效请求头")
                headers[name] = value
        if not any(key.lower() == "cookie" for key in headers):
            cookies = request.get("cookies", [])
            if cookies:
                headers["Cookie"] = "; ".join(str(x["name"]) + "=" + str(x["value"]) for x in cookies)
        choice = max(candidates, key=lambda x: x["count"])
        selected.append({"entry": index, "url": request["url"], "headers": headers, "records_pointer": choice["pointer"],
                         "arrays": candidates, "totals": total_locations, "fields": choice["fields"], "captured_count": choice["count"],
                         "query_fields": list(dict.fromkeys(key for key, _ in parse_qsl(parts.query, keep_blank_values=True) if not SECRET.search(key)))})
    return selected


def public_choice(choice):
    return {key: value for key, value in choice.items() if key not in {"headers", "url"}} | {"url": masked_url(choice["url"])}


def template_for(choice, *, records_pointer=None, total_pointer="", page_param="", id_field=""):
    result = {"version": 1, "url": choice["url"], "headers": choice["headers"],
              "records_pointer": choice["records_pointer"] if records_pointer is None else records_pointer,
              "total_pointer": total_pointer, "page_param": page_param, "id_field": id_field}
    url_valid(result["url"])
    for name, value in result["headers"].items():
        if not isinstance(name, str) or not isinstance(value, str) or "\r" in name + value or "\n" in name + value:
            raise TaskError("读取配置含无效请求头")
    return result


def load_har(path):
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise TaskError("浏览记录超过 32 MiB，请只导出相关查询")
    return inspect_har(strict_json(path.read_text(encoding="utf-8-sig")))


def request_url(template, values, page):
    parts = url_valid(template["url"])
    captured = parse_qsl(parts.query, keep_blank_values=True)
    allowed = {key for key, _ in captured}
    if set(values) - allowed:
        raise TaskError("参数表有来源中不存在的列：" + "、".join(sorted(set(values) - allowed)))
    if any(SECRET.search(key) for key in values):
        raise TaskError("登录参数不能放进批量参数表，请重新导入登录后的浏览记录")
    replacements = dict(values)
    if template["page_param"]:
        if template["page_param"] not in allowed:
            raise TaskError("翻页参数不在来源中")
        if template["page_param"] in values:
            raise TaskError("翻页参数由工具控制，请从参数表移除这一列")
        replacements[template["page_param"]] = str(page)
    query = [(key, str(replacements.get(key, value))) for key, value in captured]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
