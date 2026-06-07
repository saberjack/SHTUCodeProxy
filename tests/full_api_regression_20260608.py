# -*- coding: utf-8 -*-
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8092"
PORT = "8092"
ROOT = Path(__file__).resolve().parents[1]

MODELS = [
    ("GPT-5.5", True),
    ("glm-chat", True),
    ("deepseek-chat", True),
    ("qwen-instruct", True),
    ("deepseek-pro", False),
]

TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="

results = []
proxy_proc = None


def request(method, path, body=None, headers=None, timeout=80):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(BASE + path, data=data, headers=req_headers, method=method)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw.decode("utf-8", "replace"), time.time() - start
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, raw.decode("utf-8", "replace"), time.time() - start


def add(category, name, ok, status, seconds, detail=""):
    results.append({
        "category": category,
        "name": name,
        "status": "PASS" if ok else "FAIL",
        "http": status,
        "seconds": round(seconds, 2),
        "detail": detail[:500].replace("\n", " "),
    })
    print(f"[{results[-1]['status']}] {category} | {name} | HTTP {status} | {seconds:.2f}s", flush=True)


def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def sse_has_done(text):
    return "[DONE]" in text or "message_stop" in text or "response.completed" in text or "response.done" in text


def has_thinking_marker(endpoint, text):
    if endpoint == "messages":
        return "redacted_thinking" in text
    return "redacted_thinking" in text or "encrypted_content" in text or "\"type\":\"reasoning\"" in text or "\"type\": \"reasoning\"" in text


def message_body(model, stream, thinking):
    body = {
        "model": model,
        "max_tokens": 64,
        "stream": stream,
        "messages": [{"role": "user", "content": "只回复 OK"}],
    }
    if thinking:
        body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    return body


def responses_body(model, stream, thinking):
    body = {
        "model": model,
        "stream": stream,
        "max_output_tokens": 64,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "只回复 OK"}]}],
    }
    if thinking:
        body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    return body


def test_basic():
    status, text, seconds = request("GET", "/health")
    add("basic", "GET /health", status == 200 and "shtu-claude-proxy" in text, status, seconds, text)
    status, text, seconds = request("GET", "/v1/models")
    add("basic", "GET /v1/models", status == 200 and "GPT-5.5" in text, status, seconds, text)
    status, text, seconds = request("POST", "/v1/messages/count_tokens", {"model":"glm-chat","messages":[{"role":"user","content":"hello"}]})
    obj = parse_json(text)
    add("basic", "POST /v1/messages/count_tokens", status == 200 and isinstance(obj, dict) and obj.get("input_tokens", 0) > 0, status, seconds, text)


def test_matrix():
    for model, key_expected in MODELS:
        for endpoint in ("messages", "responses"):
            for stream in (False, True):
                for thinking in (False, True):
                    if endpoint == "messages":
                        status, text, seconds = request("POST", "/v1/messages", message_body(model, stream, thinking))
                        if model == "deepseek-pro" and not key_expected:
                            ok = status in (401, 500, 502) and "No API key configured" in text
                        elif stream:
                            ok = status == 200 and sse_has_done(text) and (not thinking or has_thinking_marker(endpoint, text))
                        else:
                            obj = parse_json(text)
                            ok = status == 200 and isinstance(obj, dict) and obj.get("content") is not None and (not thinking or has_thinking_marker(endpoint, text))
                    else:
                        status, text, seconds = request("POST", "/v1/responses", responses_body(model, stream, thinking))
                        if model == "deepseek-pro" and not key_expected:
                            ok = status in (401, 500, 502) and "No API key configured" in text
                        elif stream:
                            ok = status == 200 and sse_has_done(text) and (not thinking or has_thinking_marker(endpoint, text))
                        else:
                            obj = parse_json(text)
                            ok = status == 200 and isinstance(obj, dict) and obj.get("status") == "completed" and (not thinking or has_thinking_marker(endpoint, text))
                    add("matrix", f"{model} {endpoint} stream={stream} thinking={thinking}", ok, status, seconds, text)


def test_specials():
    image_url = "data:image/png;base64," + TINY_PNG
    body = {
        "model": "glm-chat",
        "max_tokens": 128,
        "stream": False,
        "messages": [{"role":"user","content":[{"type":"text","text":"描述图片"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":TINY_PNG}}]}],
    }
    status, text, seconds = request("POST", "/v1/messages", body)
    add("multimodal", "Anthropic image downgraded for glm-chat", status == 200 and "500" not in text, status, seconds, text)

    body = {
        "model": "glm-chat",
        "stream": False,
        "input": [{"role":"user","content":[{"type":"input_text","text":"描述图片"},{"type":"input_image","image_url":image_url}]}],
    }
    status, text, seconds = request("POST", "/v1/responses", body)
    add("multimodal", "Responses image downgraded for glm-chat", status == 200 and "data:image" not in text, status, seconds, text)

    tool_result = {
        "type":"tool_result",
        "tool_use_id":"toolu_read",
        "content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":TINY_PNG}}],
    }
    body = {
        "model":"glm-chat",
        "max_tokens":128,
        "stream":True,
        "messages":[{"role":"user","content":"打开百度并截图"},{"role":"assistant","content":[{"type":"tool_use","id":"toolu_read","name":"Read","input":{"file_path":"C:/tmp/baidu.png"}}]},{"role":"user","content":[tool_result,{"type":"text","text":"现在用 Bash 获取 snapshot"}]}],
        "tools":[{"name":"Bash","description":"Run bash","input_schema":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}],
    }
    status, text, seconds = request("POST", "/v1/messages", body, timeout=100)
    add("regression", "Claude Read screenshot follow-up stream fallback", status == 200 and sse_has_done(text) and "HTTP 500" not in text, status, seconds, text)

    body = {
        "model":"glm-chat",
        "max_tokens":128,
        "stream":False,
        "messages":[{"role":"user","content":"请调用 Bash 输出 hello"}],
        "tools":[{"name":"Bash","description":"Run bash","input_schema":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}],
        "tool_choice":{"type":"tool","name":"Bash"},
    }
    status, text, seconds = request("POST", "/v1/messages", body)
    add("tools", "Bash tool name preserved for Claude Code", status == 200 and "\"name\": \"Bash\"" in text, status, seconds, text)

    body = {"model":"glm-chat","input":[{"role":"user","content":[{"type":"input_text","text":"hello"}]}]}
    status, text, seconds = request("POST", "/v1/responses/resp_test/input_items", body)
    obj = parse_json(text)
    add("codex-special", "POST /v1/responses/{id}/input_items", status == 200 and isinstance(obj, dict) and obj.get("status") == "completed", status, seconds, text)


def wait_health():
    for _ in range(40):
        try:
            status, _, _ = request("GET", "/health", timeout=2)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    global proxy_proc
    log_path = ROOT / "debug-full-api-8092.log"
    with log_path.open("w", encoding="utf-8") as log:
        proxy_proc = subprocess.Popen([sys.executable, "src/proxy.py", "--host", "127.0.0.1", "--port", PORT], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            if not wait_health():
                raise RuntimeError("proxy health check failed")
            test_basic()
            test_matrix()
            test_specials()
        finally:
            if proxy_proc and proxy_proc.poll() is None:
                proxy_proc.terminate()
                try:
                    proxy_proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proxy_proc.kill()
                    proxy_proc.wait(timeout=5)

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = total - passed
    out_json = ROOT / "docs" / "dev-notes" / "2026-06-08-full-api-test-results-rerun.json"
    out_md = ROOT / "docs" / "dev-notes" / "2026-06-08-full-api-test-rerun.md"
    payload = {"total": total, "pass": passed, "fail": failed, "base_url": BASE, "results": results}
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 2026-06-08 Full API Rerun", "", f"Total: {total}  PASS: {passed}  FAIL: {failed}", "", "| Category | Test | Status | HTTP | Seconds | Detail |", "|---|---|---:|---:|---:|---|"]
    for r in results:
        detail = r["detail"].replace("|", "\\|")
        lines.append(f"| {r['category']} | {r['name']} | {r['status']} | {r['http']} | {r['seconds']} | {detail} |")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"SUMMARY total={total} pass={passed} fail={failed}")
    print(f"JSON {out_json}")
    print(f"MD {out_md}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())