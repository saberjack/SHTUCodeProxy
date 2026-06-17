#!/usr/bin/env python3
"""
Minimal Anthropic Messages -> OpenAI Responses streaming proxy for Claude Code.

This proxy accepts Claude Code's Anthropic-style /v1/messages requests and
forwards them to an OpenAI Responses-compatible endpoint, converting streaming
response.output_text.delta events into Anthropic-style SSE events.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from typing import Any, Dict, Iterable, List, Optional, Tuple

from platform_utils import app_dir
from config_store import CLAUDE_MODEL_ALIASES, AppConfig, ModelConfig, config_path, load_config

DEFAULT_UPSTREAM_URL = "https://genaiapi.shanghaitech.edu.cn/api/v1/response"
DEFAULT_MODEL = "GPT-5.5"
ANTHROPIC_VERSION = "2023-06-01"
ACTIVE_CONFIG: Optional[AppConfig] = None
DEBUG_SSE = os.environ.get('DEBUG_SSE', '0') == '1'
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
DSML_OPEN_RE = re.compile(r"<\s*[|｜]DSML[|｜](?:tool_calls|invoke|parameter)\b", re.IGNORECASE)
DSML_CLOSE_RE = re.compile(r"</\s*[|｜]DSML[|｜](?:tool_calls|invoke|parameter)\s*>", re.IGNORECASE)
DSML_TOOL_CALLS_CLOSE_RE = re.compile(r"</\s*[|｜]DSML[|｜]tool_calls\s*>", re.IGNORECASE)
DSML_OPEN_PREFIXES = (
    "<|dsml|tool_calls",
    "<|dsml|invoke",
    "<|dsml|parameter",
)
DSML_TOOL_CALLS_CLOSE_PREFIXES = ("</|dsml|tool_calls>",)
PSEUDO_FUNCTION_RE = re.compile(r"<function\b[^>]*>(.*?)</function>", re.IGNORECASE | re.DOTALL)
PSEUDO_PARAM_RE = re.compile(r"<param\s+name=[\"']([^\"']+)[\"']\s*>(.*?)</param>", re.IGNORECASE | re.DOTALL)
PSEUDO_CALL_RE = re.compile(r"<call\b([^>]*)>(.*?)</call>", re.IGNORECASE | re.DOTALL)
PSEUDO_ATTR_RE = re.compile(r"([A-Za-z_][\w.-]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
PSEUDO_READ_FILE_RE = re.compile(r"<read_file\b([^>]*)/?>", re.IGNORECASE | re.DOTALL)
PSEUDO_EXEC_COMMAND_RE = re.compile(r"<exec_command\b[^>]*>(.*?)</exec_command>", re.IGNORECASE | re.DOTALL)
PSEUDO_COMMAND_RE = re.compile(r"<command\b[^>]*>(.*?)</command>", re.IGNORECASE | re.DOTALL)
PSEUDO_INVOKE_RE = re.compile(r"<invoke\b[^>]*>(.*?)</invoke>", re.IGNORECASE | re.DOTALL)
PSEUDO_TOOL_RE = re.compile(r"<tool\b[^>]*>(.*?)</tool>", re.IGNORECASE | re.DOTALL)
PSEUDO_SHELL_RE = re.compile(r"<shell\b[^>]*>(.*?)</shell>", re.IGNORECASE | re.DOTALL)
PSEUDO_TOOL_INVOKE_RE = re.compile(r"<tool_invoke\b([^>]*)>(.*?)</tool_invoke>", re.IGNORECASE | re.DOTALL)
PSEUDO_PARAMETER_RE = re.compile(r"<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>", re.IGNORECASE | re.DOTALL)
PSEUDO_TOOL_RESULTS_RE = re.compile(r"<tool_results\b[^>]*>.*?</tool_results>", re.IGNORECASE | re.DOTALL)
PSEUDO_TOOL_CALL_PLAN_RE = re.compile(r"<tool_call\b[^>]*>.*?</tool_update>", re.IGNORECASE | re.DOTALL)


def now_ms() -> int:
    return int(time.time() * 1000)


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, file=sys.stderr, flush=True)
    try:
        target_dir = app_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "proxy.log"
        if target.exists() and target.stat().st_size > LOG_FILE_MAX_BYTES:
            backup = target_dir / "proxy.log.1"
            if backup.exists():
                backup.unlink()
            target.rename(backup)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def usage_cache_debug(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    candidates = {
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
    }
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    if isinstance(input_details, dict):
        candidates["details_cached_tokens"] = input_details.get("cached_tokens")
    present = {key: value for key, value in candidates.items() if value is not None}
    if not present:
        return ""
    return " cache_usage=" + json.dumps(present, ensure_ascii=False, separators=(",", ":"))


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


def request_stream_enabled(body: Dict[str, Any], default_stream: bool = True) -> bool:
    if "stream" in body:
        return bool(body.get("stream"))
    return bool(default_stream)


def _text_contains_any(value: Any, needles: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        lower = value.lower()
        return any(needle in lower for needle in needles)
    if isinstance(value, list):
        return any(_text_contains_any(item, needles) for item in value)
    if isinstance(value, dict):
        return any(_text_contains_any(item, needles) for item in value.values())
    return False


def is_claude_auto_classifier_request(body: Dict[str, Any]) -> bool:
    # WHY: Claude Code auto mode expects a strict safety verdict before it runs
    # tools like Bash. Auto-injecting thinking makes qwen-instruct answer with a
    # reasoning transcript instead of a parseable verdict, which Claude reports
    # as the classifier model being temporarily unavailable. The classifier is
    # itself a plain model call, not a tool-enabled request.
    needles = ("security monitor", "auto mode", "classification process", "<block>", "permission")
    return _text_contains_any(body.get("system"), needles)


def parse_pseudo_attributes(value: str) -> Dict[str, str]:
    return {name: raw.strip() for name, _, raw in PSEUDO_ATTR_RE.findall(value)}


def shell_command_argv(command: str) -> List[str]:
    if os.name == "nt":
        return ["powershell.exe", "-Command", command]
    return ["bash", "-lc", command]


def is_direct_shell_executable(executable: str) -> bool:
    if os.name == "nt":
        return executable in {"powershell.exe", "powershell", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "python", "python.exe", "node", "node.exe"}
    return executable in {"bash", "sh", "zsh", "fish", "python", "python3", "node"}


def shell_join_command_parts(command_parts: List[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command_parts)
    return shlex.join(command_parts)


def command_text_from_arguments(arguments: Dict[str, Any]) -> str:
    command = arguments.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return shell_join_command_parts([str(part) for part in command if part is not None])
    cmd = arguments.get("cmd")
    if isinstance(cmd, str):
        return cmd
    fallback = arguments.get("arguments")
    return fallback if isinstance(fallback, str) else ""


def command_tool_schema(tool_name: str, tools: Optional[List[Dict[str, Any]]] = None) -> str:
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
        if name == tool_name:
            parameters = tool.get("parameters") or (tool.get("function", {}) or {}).get("parameters") or {}
            properties = parameters.get("properties") if isinstance(parameters, dict) else {}
            if isinstance(properties, dict) and "cmd" in properties and "command" not in properties:
                return "cmd"
    return "command"


def adapt_tool_arguments_for_schema(tool_name: str, arguments: Dict[str, Any], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if command_tool_schema(tool_name, tools) != "cmd":
        return arguments
    command_text = command_text_from_arguments(arguments)
    if command_text:
        adapted = dict(arguments)
        adapted.pop("command", None)
        adapted.pop("arguments", None)
        adapted["cmd"] = command_text
        return adapted
    return arguments


def best_tool_name(requested_name: str, arguments: Dict[str, Any], tool_names: List[str], preferred_shell: Optional[str]) -> str:
    if requested_name in tool_names:
        return requested_name
    lowered = requested_name.lower()
    if lowered in ("shell", "bash", "exec", "run_command", "shell_exec", "exec_command", "execute_command") and preferred_shell:
        return preferred_shell
    if preferred_shell and ("command" in arguments or "path" in arguments or "text" in arguments or lowered in ("send_input", "read_file", "list_dir")):
        return preferred_shell
    if len(tool_names) == 1:
        return tool_names[0]
    return requested_name or "tool"


def coerce_pseudo_arguments(tool_name: str, arguments: Dict[str, Any], preferred_shell: Optional[str]) -> Dict[str, Any]:
    if tool_name == preferred_shell:
        if tool_name == "exec_command" and "cmd" in arguments:
            return arguments
        if "command" not in arguments:
            path = str(arguments.get("path") or "").strip()
            text = str(arguments.get("text") or "").strip()
            candidate = path or text
            path_match = re.search(r"[A-Za-z]:\\[^\r\n\"']+", candidate)
            if path_match:
                path = path_match.group(0).strip()
            if path:
                arguments = {"command": f"Get-Content -Path {json.dumps(path, ensure_ascii=False)} -Raw"}
        command = arguments.get("command")
        if isinstance(command, str):
            arguments = dict(arguments)
            if tool_name == "exec_command":
                arguments.pop("command", None)
                arguments["cmd"] = command
            else:
                arguments["command"] = shell_command_argv(command)
    return arguments


def parse_pseudo_function_calls(text: str, tools: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, List[Dict[str, Any]]]:
    lowered_text = text.lower()
    if "<function" not in lowered_text and "<call" not in lowered_text and "<read_file" not in lowered_text and "<exec_command" not in lowered_text and "<invoke" not in lowered_text and "<shell" not in lowered_text and "<tool_invoke" not in lowered_text and "<tool_results" not in lowered_text and "<tool_call" not in lowered_text:
        return text, []
    tool_names = []
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            if isinstance(tool.get("function"), dict):
                name = tool["function"].get("name")
            else:
                name = tool.get("name")
            if isinstance(name, str) and name:
                tool_names.append(name)
    preferred_shell = next((name for name in tool_names if name.lower() in ("shell", "bash", "exec", "run_command", "exec_command")), None)
    calls: List[Dict[str, Any]] = []

    def append_call(name: str, arguments: Dict[str, Any]) -> None:
        tool_name = best_tool_name(name, arguments, tool_names, preferred_shell)
        arguments = coerce_pseudo_arguments(tool_name, arguments, preferred_shell)
        arguments = adapt_tool_arguments_for_schema(tool_name, arguments, tools)
        calls.append({
            "id": f"call_proxy_{now_ms()}_{len(calls)}",
            "index": len(calls),
            "name": tool_name,
            "arguments": json_dumps_compact(arguments),
            "replace_arguments": True,
        })

    def replace_function(match: re.Match[str]) -> str:
        inner = match.group(1)
        params = {name: strip_tags(value) for name, value in PSEUDO_PARAM_RE.findall(inner)}
        name = params.pop("name", "") or params.pop("tool", "") or ""
        if not name:
            if "command" in params:
                name = preferred_shell or "shell"
            elif len(tool_names) == 1:
                name = tool_names[0]
            else:
                name = "tool"
        append_call(name, params)
        return ""

    def replace_call(match: re.Match[str]) -> str:
        raw_call = match.group(0)
        attrs = parse_pseudo_attributes(match.group(1))
        name = attrs.get("name", "")
        arguments = parse_json_like_object(attrs.get("arguments", "")) if attrs.get("arguments") else None
        if not isinstance(arguments, dict):
            arguments = {key: value for key, value in attrs.items() if key not in ("type", "name")}
        inner_params = {param_name: strip_tags(value) for param_name, value in PSEUDO_PARAM_RE.findall(match.group(2))}
        arguments.update(inner_params)
        if "path" not in arguments and "command" not in arguments:
            path_match = re.search(r"[A-Za-z]:\\[^\r\n\"'{}<>]+", raw_call)
            if path_match:
                arguments["path"] = path_match.group(0).strip()
        append_call(name, arguments)
        return ""

    cleaned = PSEUDO_FUNCTION_RE.sub(replace_function, text)
    cleaned = PSEUDO_CALL_RE.sub(replace_call, cleaned)

    def replace_read_file(match: re.Match[str]) -> str:
        attrs = parse_pseudo_attributes(match.group(1))
        path = attrs.get("path", "")
        append_call("read_file" if "read_file" in tool_names else (preferred_shell or "read_file"), {"path": path})
        return ""

    def replace_exec_command(match: re.Match[str]) -> str:
        inner = match.group(1)
        command_match = PSEUDO_COMMAND_RE.search(inner)
        command = strip_tags(command_match.group(1) if command_match else inner)
        append_call(preferred_shell or "shell", {"command": command})
        return ""

    def replace_invoke(match: re.Match[str]) -> str:
        inner = match.group(1)
        tool_match = PSEUDO_TOOL_RE.search(inner)
        command_match = PSEUDO_COMMAND_RE.search(inner)
        name = strip_tags(tool_match.group(1)) if tool_match else (preferred_shell or "shell")
        arguments: Dict[str, Any] = {}
        if command_match:
            arguments["command"] = strip_tags(command_match.group(1))
        else:
            arguments = {param_name: strip_tags(value) for param_name, value in PSEUDO_PARAM_RE.findall(inner)}
        append_call(name, arguments)
        return ""

    def replace_shell(match: re.Match[str]) -> str:
        append_call(preferred_shell or "shell", {"command": strip_tags(match.group(1))})
        return ""

    def replace_tool_invoke(match: re.Match[str]) -> str:
        attrs = parse_pseudo_attributes(match.group(1))
        inner = match.group(2)
        name = attrs.get("name", "")
        arguments = {param_name: strip_tags(value) for param_name, value in PSEUDO_PARAMETER_RE.findall(inner)}
        append_call(name, arguments)
        return ""

    cleaned = PSEUDO_READ_FILE_RE.sub(replace_read_file, cleaned)
    cleaned = PSEUDO_EXEC_COMMAND_RE.sub(replace_exec_command, cleaned)
    cleaned = PSEUDO_INVOKE_RE.sub(replace_invoke, cleaned)
    cleaned = PSEUDO_SHELL_RE.sub(replace_shell, cleaned)
    cleaned = PSEUDO_TOOL_INVOKE_RE.sub(replace_tool_invoke, cleaned)
    cleaned = PSEUDO_TOOL_RESULTS_RE.sub("", cleaned)
    cleaned = PSEUDO_TOOL_CALL_PLAN_RE.sub("", cleaned)
    return cleaned.strip(), calls


def current_config() -> AppConfig:
    global ACTIVE_CONFIG
    if ACTIVE_CONFIG is None:
        ACTIVE_CONFIG = load_config()
        return ACTIVE_CONFIG
    try:
        target = config_path()
        if target.exists() and target.stat().st_mtime > ACTIVE_CONFIG._loaded_at:
            ACTIVE_CONFIG = load_config()
    except Exception:
        pass
    return ACTIVE_CONFIG


_MAX_BODY_LENGTH = 10 * 1024 * 1024  # 10 MB


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("content-length", "0") or "0")
    if length > _MAX_BODY_LENGTH:
        send_json(handler, 413, {"type": "error", "error": {"type": "invalid_request_error", "message": f"Request body too large: {length} bytes (max {_MAX_BODY_LENGTH})"}})
        raise _BodyTooLargeError()
    if length > 0:
        raw = handler.rfile.read(length)
    else:
        # No Content-Length: read available data with timeout guard
        chunks = []
        try:
            handler.rfile._sock.settimeout(5)
            while True:
                chunk = handler.rfile.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > _MAX_BODY_LENGTH:
                    send_json(handler, 413, {"type": "error", "error": {"type": "invalid_request_error", "message": f"Request body too large (max {_MAX_BODY_LENGTH})"}})
                    raise _BodyTooLargeError()
        except (socket.timeout, OSError):
            pass
        raw = b"".join(chunks)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", errors="replace"))


class _BodyTooLargeError(Exception):
    """Raised by read_json_body when Content-Length exceeds the limit."""
    pass


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(data)))
    handler.send_header("access-control-allow-origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


def send_sse_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream; charset=utf-8")
    handler.send_header("cache-control", "no-cache")
    handler.send_header("connection", "close")
    handler.send_header("x-accel-buffering", "no")
    handler.end_headers()


def _response_is_sse(response) -> bool:
    """Check if an upstream HTTP response is an SSE stream."""
    content_type = response.headers.get("content-type", "")
    return "text/event-stream" in content_type


def write_sse(handler, event: str, data) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    handler.wfile.write(payload.encode("utf-8"))
    handler.wfile.flush()


def write_data_sse(handler: BaseHTTPRequestHandler, data: str) -> None:
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


def normalize_content_part(part: Dict[str, Any]) -> Any:
    part_type = part.get("type")
    # WHY: thinking/redacted_thinking are Anthropic-only types that must not
    # be forwarded to the Responses API as serialized JSON text.
    if part_type in ("thinking", "redacted_thinking"):
        return {"type": "input_text", "text": ""}
    if part_type in ("text", "input_text"):
        return copy_cache_metadata(part, {"type": "input_text", "text": part.get("text", "")})
    if part_type in ("input_image", "input_file"):
        return dict(part)
    if part_type == "image":
        source = part.get("source") or {}
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            return copy_cache_metadata(part, {"type": "input_image", "image_url": f"data:{media_type};base64,{data}"})
        if source.get("type") == "url":
            return copy_cache_metadata(part, {"type": "input_image", "image_url": source.get("url", "")})
    if part_type == "document":
        source = part.get("source") if isinstance(part.get("source"), dict) else {}
        if source.get("type") == "url":
            return copy_cache_metadata(part, {"type": "input_file", "file_url": source.get("url", "")})
        if source.get("type") == "base64":
            file_data = f"data:{source.get('media_type', 'application/octet-stream')};base64,{source.get('data', '')}"
            return copy_cache_metadata(part, {"type": "input_file", "filename": part.get("title") or source.get("filename") or "document", "file_data": file_data})
    if part_type in ("image_url", "file", "input_audio"):
        return dict(part)
    return copy_cache_metadata(part, {"type": "input_text", "text": json.dumps(part, ensure_ascii=False)})


def image_url_from_part(part: Dict[str, Any]) -> Optional[str]:
    part_type = part.get("type")
    if part_type == "image_url":
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            return str(image_url.get("url") or "")
        if isinstance(image_url, str):
            return image_url
    if part_type == "input_image":
        image_url = part.get("image_url") or part.get("url")
        if isinstance(image_url, str):
            return image_url
    if part_type == "image":
        source = part.get("source") or {}
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            return f"data:{media_type};base64,{data}"
        if source.get("type") == "url":
            return str(source.get("url") or "")
    return None


def file_chat_part_from_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    part_type = part.get("type")
    if part_type == "input_file":
        file_part = {"type": "file", "file": {}}
        file_value = file_part["file"]
        for source_key, target_key in (("file_id", "file_id"), ("file_url", "file_url"), ("file_data", "file_data"), ("filename", "filename")):
            if part.get(source_key):
                file_value[target_key] = part[source_key]
        return copy_cache_metadata(part, file_part) if file_value else None
    if part_type == "file" and isinstance(part.get("file"), dict):
        return copy_cache_metadata(part, {"type": "file", "file": dict(part["file"])})
    return None


def audio_chat_part_from_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    part_type = part.get("type")
    if part_type == "input_audio":
        input_audio = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else {}
        if input_audio:
            return copy_cache_metadata(part, {"type": "input_audio", "input_audio": dict(input_audio)})
    return None


def content_part_to_chat_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    part_type = part.get("type")
    if part_type in ("text", "input_text", "output_text"):
        return copy_cache_metadata(part, {"type": "text", "text": part.get("text", "")})
    if part_type == "document":
        normalized = normalize_content_part(part)
        return file_chat_part_from_part(normalized) if isinstance(normalized, dict) else None
    image_url = image_url_from_part(part)
    if image_url:
        return copy_cache_metadata(part, {"type": "image_url", "image_url": {"url": image_url}})
    file_part = file_chat_part_from_part(part)
    if file_part:
        return file_part
    audio_part = audio_chat_part_from_part(part)
    if audio_part:
        return audio_part
    return None


def content_to_chat_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text = str(part)
            parts.append({"type": "text", "text": text})
            text_parts.append(text)
            continue
        chat_part = content_part_to_chat_part(part)
        if chat_part:
            parts.append(chat_part)
            if chat_part.get("type") == "text":
                text_parts.append(str(chat_part.get("text") or ""))
        else:
            text = json.dumps(part, ensure_ascii=False)
            parts.append({"type": "text", "text": text})
            text_parts.append(text)
    if any(part.get("type") == "image_url" for part in parts):
        return parts
    if any(part.get("type") in ("file", "input_audio") for part in parts):
        return parts
    if has_cache_metadata(parts):
        return parts
    return "\n".join(text for text in text_parts if text)


def content_to_responses_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text = str(part)
            parts.append({"type": "input_text", "text": text})
            text_parts.append(text)
            continue
        normalized = normalize_content_part(part)
        if normalized.get("type") == "input_text":
            text_parts.append(str(normalized.get("text") or ""))
        parts.append(normalized)
    if any(part.get("type") != "input_text" for part in parts):
        return parts
    if has_cache_metadata(parts):
        return parts
    return "\n".join(text for text in text_parts if text)


def anthropic_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    text_parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            text_parts.append(part.get("text", ""))
        elif part_type == "tool_use":
            text_parts.append(
                f"[tool_use name={part.get('name', '')} id={part.get('id', '')}] "
                f"{json.dumps(part.get('input', {}), ensure_ascii=False)}"
            )
        elif part_type == "tool_result":
            text_parts.append(f"[tool_result id={part.get('tool_use_id', '')}] {part.get('content', '')}")
        elif part_type == "image":
            text_parts.append("[image]")
        elif part_type in ("thinking", "redacted_thinking"):
            pass
        else:
            text_parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(part for part in text_parts if part)


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def normalize_dsml_marker(text: str) -> str:
    return text.replace("｜", "|").lower()


def partial_dsml_marker_start(text: str, position: int, prefixes: Tuple[str, ...]) -> int:
    for index in range(len(text) - 1, position - 1, -1):
        suffix = normalize_dsml_marker(text[index:])
        if any(prefix.startswith(suffix) and suffix != prefix for prefix in prefixes):
            return index
    return -1


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_value_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return estimate_text_tokens(value)
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, list):
        return sum(estimate_value_tokens(item) for item in value)
    if isinstance(value, dict):
        return sum(estimate_text_tokens(str(key)) + estimate_value_tokens(item) for key, item in value.items())
    return estimate_text_tokens(str(value))


def estimate_anthropic_input_tokens(body: Dict[str, Any]) -> int:
    total = 0
    total += estimate_value_tokens(body.get("system"))
    total += estimate_value_tokens(body.get("tools"))
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        total += 4
        total += estimate_text_tokens(str(message.get("role", "")))
        total += estimate_value_tokens(message.get("content"))
    # Responses API: input can be a string, list of messages, or dict
    input_val = body.get("input")
    if input_val is not None and not body.get("messages"):
        total += estimate_value_tokens(input_val)
    return max(1, total)


MODALITY_PART_TYPES = {
    "image": {"image", "input_image", "image_url"},
    "audio": {"input_audio", "audio"},
    "video": {"video", "input_video", "video_url"},
}
MULTIMODAL_PART_TYPES = set().union(*MODALITY_PART_TYPES.values())
UNSUPPORTED_MODALITY_PLACEHOLDER = "[已移除当前模型不支持的图片/音频/视频输入]"
IMAGE_TOOL_NAME_RE = re.compile(r"(^|[_-])(view|read|analyze|describe|inspect|parse|recognize|ocr|vision)[_-]?(image|img|picture|photo|screenshot|screen|visual)|^(view_image|image|screenshot|ocr)$", re.IGNORECASE)
IMAGE_TOOL_TEXT_RE = re.compile(r"\b(image|picture|photo|screenshot|screen capture|vision|visual|ocr|识图|图片|截图)\b", re.IGNORECASE)
MEDIA_DATA_RE = re.compile(r"data:(image|audio|video)/[a-z0-9.+-]+;base64,", re.IGNORECASE)
CACHE_METADATA_KEYS = ("cache_control",)
DEFAULT_CACHE_CONTROL = {"type": "ephemeral"}


def copy_cache_metadata(source: Any, target: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(source, dict):
        return target
    for key in CACHE_METADATA_KEYS:
        if key in source:
            target[key] = source[key]
    return target


def has_cache_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in CACHE_METADATA_KEYS) or any(has_cache_metadata(item) for item in value.values())
    if isinstance(value, list):
        return any(has_cache_metadata(item) for item in value)
    return False


def auto_cache_enabled() -> bool:
    return os.getenv("SHTU_AUTO_CACHE_CONTROL", "1").strip().lower() not in {"0", "false", "no", "off"}


def set_default_cache_control(item: Any) -> bool:
    if not isinstance(item, dict) or item.get("cache_control") is not None:
        return False
    item["cache_control"] = dict(DEFAULT_CACHE_CONTROL)
    return True


def mark_content_cache_boundary(content: Any, text_type: str) -> tuple[Any, bool]:
    if isinstance(content, str):
        if not content:
            return content, False
        # WHY: Do NOT convert string content to array for cache_control.
        # Chat Completions upstream (GLM, DeepSeek) rejects array content on
        # assistant/user messages and returns HTTP 500.  Only Anthropic Messages
        # accepts the array-with-cache_control form.  Since cache_control is an
        # Anthropic-specific concept, skip it when content is a plain string.
        return content, False
    if isinstance(content, list):
        updated = [dict(part) if isinstance(part, dict) else part for part in content]
        for part in reversed(updated):
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                return updated, set_default_cache_control(part)
        for part in reversed(updated):
            if isinstance(part, dict):
                return updated, set_default_cache_control(part)
    return content, False


def apply_auto_cache_control_to_tools(tools: Any) -> int:
    if not isinstance(tools, list):
        return 0
    count = 0
    for tool in tools:
        if set_default_cache_control(tool):
            count += 1
    return count


def apply_auto_cache_control_to_chat_payload(payload: Dict[str, Any]) -> int:
    count = apply_auto_cache_control_to_tools(payload.get("tools"))
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return count
    stable_indexes = {index for index, message in enumerate(messages) if isinstance(message, dict) and message.get("role") == "system"}
    if len(messages) > 1:
        stable_indexes.add(len(messages) - 2)
    for index in sorted(stable_indexes):
        if index < 0 or index >= len(messages) or not isinstance(messages[index], dict):
            continue
        if messages[index].get("role") == "tool":
            continue
        if messages[index].get("content") is None:
            continue
        content, changed = mark_content_cache_boundary(messages[index].get("content"), "text")
        if changed:
            messages[index]["content"] = content
            count += 1
    return count


def apply_auto_cache_control_to_responses_payload(payload: Dict[str, Any]) -> int:
    count = apply_auto_cache_control_to_tools(payload.get("tools"))
    input_items = payload.get("input")
    if isinstance(input_items, str):
        payload["input"] = [{"role": "user", "content": [{"type": "input_text", "text": input_items, "cache_control": dict(DEFAULT_CACHE_CONTROL)}]}]
        return count + 1
    if not isinstance(input_items, list):
        return count
    stable_indexes = {index for index, item in enumerate(input_items) if isinstance(item, dict) and item.get("role") in ("developer", "system")}
    if len(input_items) > 1:
        stable_indexes.add(len(input_items) - 2)
    for index in sorted(stable_indexes):
        if index < 0 or index >= len(input_items) or not isinstance(input_items[index], dict):
            continue
        if "content" in input_items[index]:
            content, changed = mark_content_cache_boundary(input_items[index].get("content"), "input_text")
            if changed:
                input_items[index]["content"] = content
                count += 1
        elif input_items[index].get("type") in ("function_call", "function_call_output", "tool_result") and set_default_cache_control(input_items[index]):
            count += 1
    return count


def apply_auto_cache_control(payload: Dict[str, Any]) -> int:
    if not auto_cache_enabled() or has_cache_metadata(payload):
        return 0
    if isinstance(payload.get("messages"), list):
        return apply_auto_cache_control_to_chat_payload(payload)
    return 0



def content_modalities(content: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(content, str):
        return media_string_modalities(content)
    if isinstance(content, list):
        for part in content:
            found.update(content_modalities(part))
        return found
    if not isinstance(content, dict):
        return found
    part_type = content.get("type")
    if isinstance(part_type, str):
        if part_type == "image":
            found.add("image")
        for modality, part_types in MODALITY_PART_TYPES.items():
            if part_type in part_types:
                found.add(modality)
        if part_type in ("tool_use", "function_call"):
            found.update(tool_modalities(content))
    if part_type in ("tool_result", "message") or "role" in content:
        found.update(content_modalities(content.get("content")))
    if part_type in ("function_call_output", "tool_result"):
        found.update(content_modalities(content.get("output")))
    return found


def direct_content_modalities(content: Any) -> set[str]:
    if not isinstance(content, dict):
        return set()
    part_type = content.get("type")
    if not isinstance(part_type, str):
        return set()
    if part_type in ("tool_use", "function_call"):
        return tool_modalities(content)
    if part_type == "image":
        return {"image"}
    found: set[str] = set()
    for modality, part_types in MODALITY_PART_TYPES.items():
        if part_type in part_types:
            found.add(modality)
    return found


def media_string_modalities(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    found: set[str] = set()
    for match in MEDIA_DATA_RE.finditer(value[:2048]):
        found.add(match.group(1).lower())
    return found


def content_has_multimodal(content: Any) -> bool:
    return bool(content_modalities(content))


def unsupported_modalities(model_config: ModelConfig, modalities: set[str]) -> set[str]:
    unsupported: set[str] = set()
    if "image" in modalities and not model_config.supports_image:
        unsupported.add("image")
    if "audio" in modalities and not model_config.supports_audio:
        unsupported.add("audio")
    if "video" in modalities and not model_config.supports_video:
        unsupported.add("video")
    return unsupported


def tool_modalities(tool: Dict[str, Any]) -> set[str]:
    name = str(tool.get("name") or "")
    description = str(tool.get("description") or "")
    parameters = tool.get("input_schema") or tool.get("parameters") or {}
    text = f"{name}\n{description}\n{json.dumps(parameters, ensure_ascii=False) if isinstance(parameters, dict) else parameters}"
    modalities: set[str] = set()
    if IMAGE_TOOL_NAME_RE.search(name) or IMAGE_TOOL_TEXT_RE.search(text):
        modalities.add("image")
    return modalities


def filter_tools_for_model(tools: Any, model_config: ModelConfig) -> List[Dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    filtered: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if unsupported_modalities(model_config, tool_modalities(tool)):
            continue
        filtered.append(tool)
    return filtered


def sanitized_tool_choice_for_model(tool_choice: Any, tools: Any, model_config: ModelConfig) -> Any:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "tool" or not tool_choice.get("name"):
        return tool_choice
    allowed_names = {str(tool.get("name")) for tool in filter_tools_for_model(tools, model_config) if isinstance(tool, dict)}
    return {"type": "auto"} if tool_choice.get("name") not in allowed_names else tool_choice


def placeholder_content_part(part: Dict[str, Any]) -> Dict[str, Any]:
    text_type = "input_text" if part.get("type") in ("input_image", "input_audio", "input_video", "video_url") else "text"
    return copy_cache_metadata(part, {"type": text_type, "text": UNSUPPORTED_MODALITY_PLACEHOLDER})


def sanitized_content_for_model(content: Any, model_config: ModelConfig) -> Any:
    if isinstance(content, str) and unsupported_modalities(model_config, media_string_modalities(content)):
        return UNSUPPORTED_MODALITY_PLACEHOLDER
    if isinstance(content, list):
        sanitized: List[Any] = []
        for part in content:
            if isinstance(part, dict) and unsupported_modalities(model_config, direct_content_modalities(part)):
                sanitized.append(placeholder_content_part(part))
                continue
            sanitized_part = sanitized_content_for_model(part, model_config) if isinstance(part, dict) else part
            if sanitized_part not in ("", [], None):
                sanitized.append(sanitized_part)
        return sanitized or UNSUPPORTED_MODALITY_PLACEHOLDER
    if isinstance(content, dict):
        if unsupported_modalities(model_config, direct_content_modalities(content)):
            return placeholder_content_part(content)
        sanitized = dict(content)
        if "content" in sanitized:
            sanitized["content"] = sanitized_content_for_model(sanitized.get("content"), model_config)
        if sanitized.get("type") in ("function_call_output", "tool_result") and "output" in sanitized:
            sanitized["output"] = sanitized_content_for_model(sanitized.get("output"), model_config)
        return sanitized
    return content


def sanitized_anthropic_body_for_model(body: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    sanitized = dict(body)
    # WHY: Extract thinking flag before stripping; inject redacted_thinking in response if requested
    thinking_param = body.get("thinking")
    if isinstance(thinking_param, dict) and thinking_param.get("type") in ("enabled", "adaptive"):
        sanitized["_thinking_requested"] = True
    # Strip "thinking" ? no upstream API supports it; causes 400 errors on non-Anthropic models
    sanitized.pop("thinking", None)
    # WHY: When model supports reasoning and thinking was requested, flag it so the
    # WHY: When enable_thinking is set, always send chat_template_kwargs so vLLM models
    # use reasoning mode. When supports_reasoning is set (but not enable_thinking), only
    # enable reasoning when the client actually requested thinking.
    if getattr(model_config, "enable_thinking", False):
        sanitized["_reasoning_enabled"] = True
    elif sanitized.get("_thinking_requested") and getattr(model_config, "supports_reasoning", False):
        sanitized["_reasoning_enabled"] = True
    sanitized["tools"] = filter_tools_for_model(body.get("tools"), model_config)
    sanitized["tool_choice"] = sanitized_tool_choice_for_model(body.get("tool_choice"), body.get("tools"), model_config)
    removed_tool_ids: set[str] = set()
    for message in body.get("messages", []):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "tool_use" and unsupported_modalities(model_config, tool_modalities(part)):
                removed_tool_ids.add(str(part.get("id") or ""))
    messages: List[Any] = []
    for message in body.get("messages", []):
        if isinstance(message, dict):
            sanitized_message = dict(message)
            content = sanitized_content_for_model(message.get("content"), model_config)
            if isinstance(content, list) and removed_tool_ids:
                content = [
                    part for part in content
                    if not (isinstance(part, dict) and part.get("type") == "tool_result" and str(part.get("tool_use_id") or "") in removed_tool_ids)
                ]
            sanitized_message["content"] = content if content not in ([], None) else UNSUPPORTED_MODALITY_PLACEHOLDER
            messages.append(sanitized_message)
        else:
            messages.append(message)
    sanitized["messages"] = messages
    return sanitized


def sanitized_responses_body_for_model(body: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    sanitized = dict(body)
    # WHY: Extract thinking flag before stripping; inject redacted_thinking in response if requested
    thinking_param = body.get("thinking")
    if isinstance(thinking_param, dict) and thinking_param.get("type") in ("enabled", "adaptive"):
        sanitized["_thinking_requested"] = True
    # Strip "thinking" ? no upstream API supports it; causes 400 errors on non-Anthropic models
    sanitized.pop("thinking", None)
    # WHY: When model supports reasoning and thinking was requested, flag it so the
    # WHY: When enable_thinking is set, always send chat_template_kwargs so vLLM models
    # use reasoning mode. When supports_reasoning is set (but not enable_thinking), only
    # enable reasoning when the client actually requested thinking.
    if getattr(model_config, "enable_thinking", False):
        sanitized["_reasoning_enabled"] = True
    elif sanitized.get("_thinking_requested") and getattr(model_config, "supports_reasoning", False):
        sanitized["_reasoning_enabled"] = True
    sanitized["tools"] = filter_tools_for_model(body.get("tools"), model_config)
    sanitized["tool_choice"] = sanitized_tool_choice_for_model(body.get("tool_choice"), body.get("tools"), model_config)
    input_items = body.get("input")
    removed_call_ids: set[str] = set()
    if isinstance(input_items, list):
        for item in input_items:
            if isinstance(item, dict) and item.get("type") == "function_call" and unsupported_modalities(model_config, tool_modalities(item)):
                removed_call_ids.add(str(item.get("call_id") or item.get("id") or ""))
    if isinstance(input_items, dict):
        sanitized_item = dict(input_items)
        if "content" in sanitized_item:
            sanitized_item["content"] = sanitized_content_for_model(sanitized_item.get("content"), model_config)
        if sanitized_item.get("type") in ("function_call_output", "tool_result") and "output" in sanitized_item:
            sanitized_item["output"] = sanitized_content_for_model(sanitized_item.get("output"), model_config)
        sanitized["input"] = sanitized_item
    elif isinstance(input_items, list):
        sanitized_items: List[Any] = []
        for item in input_items:
            if isinstance(item, dict):
                sanitized_item = dict(item)
                if unsupported_modalities(model_config, direct_content_modalities(sanitized_item)):
                    continue
                if sanitized_item.get("type") in ("function_call_output", "tool_result") and str(sanitized_item.get("call_id") or sanitized_item.get("id") or "") in removed_call_ids:
                    continue
                if "content" in sanitized_item:
                    sanitized_item["content"] = sanitized_content_for_model(sanitized_item.get("content"), model_config)
                if sanitized_item.get("type") in ("function_call_output", "tool_result") and "output" in sanitized_item:
                    sanitized_item["output"] = sanitized_content_for_model(sanitized_item.get("output"), model_config)
                sanitized_items.append(sanitized_item)
            else:
                sanitized_items.append(item)
        sanitized["input"] = sanitized_items
    return sanitized


def sanitized_upstream_value_for_model(value: Any, model_config: ModelConfig) -> Any:
    if isinstance(value, str) and unsupported_modalities(model_config, media_string_modalities(value)):
        return UNSUPPORTED_MODALITY_PLACEHOLDER
    if isinstance(value, list):
        sanitized_items: List[Any] = []
        for item in value:
            if isinstance(item, dict) and unsupported_modalities(model_config, direct_content_modalities(item)):
                sanitized_items.append(placeholder_content_part(item))
                continue
            sanitized_items.append(sanitized_upstream_value_for_model(item, model_config))
        return sanitized_items or [{"type": "text", "text": UNSUPPORTED_MODALITY_PLACEHOLDER}]
    if isinstance(value, dict):
        if unsupported_modalities(model_config, direct_content_modalities(value)):
            return placeholder_content_part(value)
        sanitized_dict = dict(value)
        if "content" in sanitized_dict:
            sanitized_dict["content"] = sanitized_upstream_value_for_model(sanitized_dict["content"], model_config)
        if sanitized_dict.get("type") in ("function_call_output", "tool_result") and "output" in sanitized_dict:
            sanitized_dict["output"] = sanitized_upstream_value_for_model(sanitized_dict["output"], model_config)
        for key in ("input", "messages"):
            if key in sanitized_dict:
                sanitized_dict[key] = sanitized_upstream_value_for_model(sanitized_dict[key], model_config)
        if sanitized_dict.get("content") == []:
            sanitized_dict["content"] = UNSUPPORTED_MODALITY_PLACEHOLDER
        if sanitized_dict.get("output") == []:
            sanitized_dict["output"] = UNSUPPORTED_MODALITY_PLACEHOLDER
        return sanitized_dict
    return value


def _strip_cache_control(value: Any) -> Any:
    """Recursively remove cache_control keys (not supported by Responses API)."""
    if isinstance(value, dict):
        return {k: _strip_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip_cache_control(item) for item in value]
    return value


def _strip_bing_grounding(value: Any) -> Any:
    """Recursively remove Bing Search grounding features (platform policy: disabled).

    Strips:
      - tools with type "bing_grounding" (Chat Completions / Responses API)
      - data_sources entries with type "azure_grounding" (Chat Completions API)
    """
    if isinstance(value, dict):
        result = {k: _strip_bing_grounding(v) for k, v in value.items()}
        if "data_sources" in result and isinstance(result["data_sources"], list):
            filtered = [ds for ds in result["data_sources"] if not (isinstance(ds, dict) and ds.get("type") == "azure_grounding")]
            if len(filtered) < len(result["data_sources"]):
                log("stripped azure_grounding from data_sources (platform policy)")
            result["data_sources"] = filtered
            if not filtered:
                del result["data_sources"]
        return result
    if isinstance(value, list):
        filtered = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "bing_grounding":
                log("stripped bing_grounding tool (platform policy)")
                continue
            filtered.append(_strip_bing_grounding(item))
        return filtered
    return value


# WHY: Upstream GenAI platform rejects requests containing built-in OpenAI tools
# (web_search_preview, file_search, code_interpreter, computer_use_preview, mcp)
# that are not enabled for the organization, causing empty streams or 500 errors.
# Strip them before forwarding so the request succeeds.
# WHY: Upstream GenAI platform rejects certain built-in OpenAI tool types
# that are not enabled for the organization, causing empty streams or errors.
# Default: only strip types proven to cause errors (web_search).
# Override via environment variable STRIP_TOOL_TYPES (comma-separated).
# Set STRIP_TOOL_TYPES= to disable stripping entirely.
_DEFAULT_STRIP_TOOL_TYPES = frozenset({
    "web_search",
    "web_search_preview",
    "web_search_preview_2025_03_11",
    "tool_search",
})
_env_strip = os.environ.get("STRIP_TOOL_TYPES", None)
if _env_strip is not None:
    if _env_strip.strip() == "":
        UNSUPPORTED_TOOL_TYPES = frozenset()  # empty = strip nothing
    else:
        UNSUPPORTED_TOOL_TYPES = frozenset(t.strip() for t in _env_strip.split(",") if t.strip())
else:
    UNSUPPORTED_TOOL_TYPES = _DEFAULT_STRIP_TOOL_TYPES


def _strip_unsupported_tools(value: Any) -> Any:
    """Remove built-in OpenAI tools unsupported by upstream GenAI platform.

    WHY: Codex CLI and other clients may include web_search_preview,
    file_search, etc. in the tools list. The upstream returns empty stream
    or {success:false, message:"Tool 'X' disabled..."} for these, causing
    silent failures. Strip them proactively.
    """
    if isinstance(value, dict):
        result = {k: _strip_unsupported_tools(v) for k, v in value.items()}
        if "tools" in result and isinstance(result["tools"], list):
            filtered = []
            for tool in result["tools"]:
                if isinstance(tool, dict) and tool.get("type") in UNSUPPORTED_TOOL_TYPES:
                    log(f"stripped unsupported tool type={tool.get('type')} name={tool.get('name','')} (upstream disabled)")
                    continue
                filtered.append(tool)
            result["tools"] = filtered
            if not filtered:
                del result["tools"]
        return result
    if isinstance(value, list):
        return [_strip_unsupported_tools(item) for item in value]
    return value




SUPPORTED_INCLUDE_VALUES = frozenset({
    "file_search_call.results",
    "web_search_call.results",
    "web_search_call.action.sources",
    "message.input_image.image_url",
    "computer_call_output.output.image_url",
    "code_interpreter_call.outputs",
    "reasoning.encrypted_content",
    "message.output_text.logprobs",
})


def _filter_include_values(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove unsupported 'include' values from Responses API payload.

    WHY: Clients (e.g. Codex CLI) may send include values like
    'reasoning.encryption_details' that the upstream GenAI Responses API
    does not support, causing HTTP 400/422 errors.  Strip any unsupported
    entries before forwarding.
    """
    include = payload.get("include")
    if not isinstance(include, list) or not include:
        return payload
    filtered = [v for v in include if v in SUPPORTED_INCLUDE_VALUES]
    if len(filtered) < len(include):
        removed = [v for v in include if v not in SUPPORTED_INCLUDE_VALUES]
        log(f"stripped unsupported include values: {removed}")
        if filtered:
            payload["include"] = filtered
        else:
            del payload["include"]
    return payload

def sanitized_upstream_payload_for_model(payload: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    result = sanitized_upstream_value_for_model(payload, model_config) if isinstance(payload, dict) else payload
    # WHY: cache_control is Anthropic-specific; no upstream API supports it.
    # Leaving it in causes empty streams (GPT-5.5 responses) or errors.
    if isinstance(result, dict):
        result = _strip_cache_control(result)
        result = _strip_bing_grounding(result)
        result = _strip_unsupported_tools(result)
        result = _filter_include_values(result)
    # WHY: When model supports reasoning and thinking was requested, inject
    # chat_template_kwargs so vLLM-based upstreams enable reasoning mode.
    # Only send for chat_completions format; Responses API (GPT-5.5 etc.) rejects it.
    if isinstance(result, dict) and result.pop("_reasoning_enabled", False):
        if model_config.api_format == "chat_completions":
            result["chat_template_kwargs"] = {"enable_thinking": True}
    return result


def anthropic_current_user_modalities(body: Dict[str, Any]) -> set[str]:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return set()
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role", "user") == "user":
            return content_modalities(message.get("content"))
    return set()


def responses_current_user_modalities(body: Dict[str, Any]) -> set[str]:
    input_items = body.get("input")
    if isinstance(input_items, dict):
        return content_modalities(input_items) | content_modalities(input_items.get("content"))
    if isinstance(input_items, list):
        for item in reversed(input_items):
            if isinstance(item, dict) and (content_has_multimodal(item) or content_has_multimodal(item.get("content"))):
                role = item.get("role") or ("assistant" if item.get("type") == "message" else "user")
                return (content_modalities(item) | content_modalities(item.get("content"))) if role == "user" else set()
            if isinstance(item, dict) and item.get("role") == "user":
                return set()
    return set()


def unsupported_modalities_message(model_config: ModelConfig, modalities: set[str]) -> str:
    labels = {"image": "图片识别", "audio": "音频输入", "video": "视频输入"}
    names = "、".join(labels[item] for item in ("image", "audio", "video") if item in modalities)
    return f"模型 {model_config.model_id} 当前配置为不支持{names}。请切换到支持该类型输入的模型，或在模型配置中确认并开启对应能力后重试。"


def strip_thinking_markup(text: str) -> str:
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"^\s*</think>\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*<think\b[^>]*>.*$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json|JSON)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1).strip() if match else stripped


def extract_balanced_json(text: str) -> Optional[str]:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    stack: List[str] = []
    in_string = False
    escape = False
    quote = ""
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return text[start:index + 1]
    return None


def quote_unquoted_json_keys(text: str) -> str:
    return re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)', r'\1"\2"\3', text)


def parse_json_like_object(arguments: str) -> Optional[Any]:
    candidates = []
    cleaned = strip_markdown_json_fence(strip_thinking_markup(arguments))
    if cleaned:
        candidates.append(cleaned)
    balanced = extract_balanced_json(cleaned or arguments)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    for candidate in candidates:
        for value in (candidate, quote_unquoted_json_keys(candidate)):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError):
                pass
    return None


def tool_result_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return anthropic_content_to_text(content)
    if content is None:
        return ""
    return str(content)


def escape_tool_result_attr(value: Any) -> str:
    return str(value or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def anthropic_tool_results_visible_text(tool_results: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for result in tool_results:
        attrs = [f'tool_use_id="{escape_tool_result_attr(result.get("tool_use_id", ""))}"']
        if "is_error" in result:
            attrs.append(f'is_error="{str(bool(result.get("is_error"))).lower()}"')
        content = tool_result_content_to_text(result.get("content", ""))
        blocks.append(f"<tool_result {' '.join(attrs)}>\n{content}\n</tool_result>")
    return "<tool_results>\n" + "\n".join(blocks) + "\n</tool_results>" if blocks else ""


def anthropic_tools_to_chat_tools(tools: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        function: Dict[str, Any] = {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }
        converted.append(copy_cache_metadata(tool, {"type": "function", "function": function}))
    return converted


def anthropic_tools_to_responses_tools(tools: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append(copy_cache_metadata(tool, {
            "type": "function",
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }))
    return converted


def anthropic_tool_choice_to_openai(tool_choice: Any) -> Optional[Any]:
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    if choice_type == "none":
        return "none"
    return None


def anthropic_tool_choice_to_responses(tool_choice: Any) -> Optional[Any]:
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type in ("auto", "none", "required"):
        return choice_type
    if choice_type == "any":
        return "required"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "name": tool_choice["name"]}
    return None


def split_anthropic_content(content: Any) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    text_parts: List[str] = []
    tool_uses: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    if isinstance(content, str):
        return content, tool_uses, tool_results
    if not isinstance(content, list):
        return ("" if content is None else str(content)), tool_uses, tool_results
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            text_parts.append(part.get("text", ""))
        elif part_type == "tool_use":
            tool_uses.append(part)
        elif part_type == "tool_result":
            tool_results.append(part)
        elif part_type == "image":
            text_parts.append("[image]")
        elif part_type in ("thinking", "redacted_thinking"):
            # WHY: Skip Anthropic thinking blocks - they must not be forwarded
            # as text to upstream APIs (causes empty responses from GPT-5.5 etc.)
            pass
        else:
            text_parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(part for part in text_parts if part), tool_uses, tool_results


def anthropic_message_to_chat_content(content: Any) -> Any:
    return content_to_chat_content(content)


def anthropic_message_to_chat_messages(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    role = message.get("role", "user")
    if role not in ("user", "assistant", "system"):
        role = "user"
    text, tool_uses, tool_results = split_anthropic_content(message.get("content", ""))
    if tool_results:
        messages: List[Dict[str, Any]] = []
        for result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": result.get("tool_use_id", ""),
                "content": ("[ERROR] " if result.get("is_error") else "") + tool_result_content_to_text(result.get("content", "")),
            })
        visible_text = anthropic_tool_results_visible_text(tool_results)
        if text:
            visible_text = f"{visible_text}\n\n{text}" if visible_text else text
        if visible_text:
            messages.append({"role": "user", "content": visible_text})
        return messages
    if role == "assistant" and tool_uses:
        tool_calls = []
        for index, tool_use in enumerate(tool_uses):
            tool_calls.append({
                "id": tool_use.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": tool_use.get("name", ""),
                    "arguments": json_dumps_compact(tool_use.get("input", {})),
                },
            })
        return [{"role": "assistant", "content": text or "", "tool_calls": tool_calls}]
    chat_content = anthropic_message_to_chat_content(message.get("content", ""))
    if isinstance(chat_content, list):
        return [{"role": role, "content": chat_content}]
    return [{"role": role, "content": text}]


def anthropic_message_to_responses_items(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    role = message.get("role", "user")
    if role not in ("user", "assistant", "system"):
        role = "user"
    content_value = message.get("content", "")
    text, tool_uses, tool_results = split_anthropic_content(content_value)
    responses_content = content_to_responses_content(content_value)
    items: List[Dict[str, Any]] = []
    if tool_results:
        for result in tool_results:
            items.append({
                "type": "function_call_output",
                "call_id": result.get("tool_use_id", ""),
                "output": ("[ERROR] " if result.get("is_error") else "") + tool_result_content_to_text(result.get("content", "")),
            })
        if text:
            items.append({"role": role, "content": text})
    else:
        if tool_uses:
            if text:
                items.append({"role": role, "content": text})
        elif responses_content:
            items.append({"role": role, "content": responses_content})
        for tool_use in tool_uses:
            items.append({
                "type": "function_call",
                "call_id": tool_use.get("id", ""),
                "name": tool_use.get("name", ""),
                "arguments": json_dumps_compact(tool_use.get("input", {})),
            })
    if not items:
        items.append({"role": role, "content": ""})
    return items


def anthropic_messages_to_responses(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    input_items: List[Dict[str, Any]] = []
    system_texts: List[str] = []
    system_items: List[Dict[str, Any]] = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        system_texts.append(system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict) and item.get("type") == "text":
                system_texts.append(item.get("text", ""))
                system_items.append(copy_cache_metadata(item, {"type": "input_text", "text": item.get("text", "")}))
            elif isinstance(item, str):
                system_texts.append(item)
                system_items.append({"type": "input_text", "text": item})

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        input_items.extend(anthropic_message_to_responses_items(message))

    payload: Dict[str, Any] = {
        "model": upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model,
        "input": input_items,
        "stream": request_stream_enabled(body, default_stream),
    }

    if system_items and has_cache_metadata(system_items):
        input_items.insert(0, {"role": "developer", "content": system_items})
    elif system_texts:
        payload["instructions"] = "\n\n".join(system_texts)
    if isinstance(body.get("max_tokens"), int):
        payload["max_output_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        payload["top_p"] = body["top_p"]

    tools = anthropic_tools_to_responses_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        payload["parallel_tool_calls"] = False
    tool_choice = anthropic_tool_choice_to_responses(body.get("tool_choice"))
    if tool_choice is not None and tools:
        payload["tool_choice"] = tool_choice

    # Strip "thinking" ? no upstream supports it; passing it causes 400 errors
    # (e.g. GPT-5.5 returns "Unknown parameter: 'thinking'")

    return payload


def anthropic_messages_to_chat_completions(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        system_content = content_to_chat_content(system) if has_cache_metadata(system) else anthropic_content_to_text(system)
        if system_content:
            messages.append({"role": "system", "content": system_content})

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        messages.extend(anthropic_message_to_chat_messages(message))

    # Merge consecutive same-role messages to comply with chat_completions API
    merged: List[Dict[str, Any]] = []
    # WHY: Some upstream APIs (e.g. qwen-instruct) return empty responses when a
    # system message appears after user/assistant messages. Track the first system
    # message index so we can merge later system messages back into it.
    first_system_idx: Optional[int] = None
    has_non_system: bool = False
    for msg in messages:
        if not merged:
            merged.append(msg)
            if msg.get("role") == "system":
                first_system_idx = 0
            continue
        prev = merged[-1]
        prev_role = prev.get("role")
        curr_role = msg.get("role")
        if curr_role != "system":
            has_non_system = True
        # WHY: If a system message appears after user/assistant messages, merge it
        # into the first system message instead of keeping it as a separate message.
        # Many chat_completions APIs reject or silently ignore mid-conversation system messages.
        if curr_role == "system" and has_non_system and first_system_idx is not None:
            sys_msg = merged[first_system_idx]
            sys_text = sys_msg.get("content", "") if isinstance(sys_msg.get("content"), str) else str(sys_msg.get("content", ""))
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            sys_msg["content"] = (sys_text + "\n\n" + curr_text).strip()
            continue
        if curr_role == "system" and has_non_system and first_system_idx is None:
            # No prior system message; convert to user role
            msg = dict(msg)
            msg["role"] = "user"
            curr_role = "user"
        if prev_role == curr_role and curr_role == "user":
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else str(prev.get("content", ""))
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            prev["content"] = (prev_text + "\n\n" + curr_text).strip()
        elif prev_role == curr_role and curr_role == "assistant" and not prev.get("tool_calls") and not msg.get("tool_calls"):
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else ""
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
            prev["content"] = (prev_text + "\n\n" + curr_text).strip()
        else:
            merged.append(msg)
    messages = merged

    payload: Dict[str, Any] = {
        "model": upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model,
        "messages": messages,
        "stream": request_stream_enabled(body, default_stream),
    }
    enable_chat_stream_usage(payload)
    if isinstance(body.get("max_tokens"), int):
        payload["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        payload["top_p"] = body["top_p"]

    tools = anthropic_tools_to_chat_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
    tool_choice = anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if tool_choice is not None and tools:
        payload["tool_choice"] = tool_choice
    # If messages contain tool_calls/tool role but payload has no tools definition,
    # upstream APIs (e.g. glm-chat) require a tools field to accept tool_calls messages.
    if "tools" not in payload:
        message_tool_names: set = set()
        for msg in messages:
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                name = func.get("name")
                if name:
                    message_tool_names.add(name)
        if message_tool_names:
            payload["tools"] = [
                {"type": "function", "function": {"name": name, "description": f"Execute {name}", "parameters": {"type": "object", "properties": {}}}}
                for name in sorted(message_tool_names)
            ]
    # WHY: Preserve reasoning flag for sanitized_upstream_payload_for_model
    if body.get("_reasoning_enabled"):
        payload["_reasoning_enabled"] = True
    if body.get("_thinking_requested"):
        payload["_thinking_requested"] = True
    return payload


def anthropic_messages_to_upstream(body: Dict[str, Any], model_config: ModelConfig, fallback_model: str, upstream_model: Optional[str], default_stream: bool = True) -> Dict[str, Any]:
    if model_config.api_format == "chat_completions":
        return anthropic_messages_to_chat_completions(body, fallback_model, upstream_model, default_stream)
    return anthropic_messages_to_responses(body, fallback_model, upstream_model, default_stream)


def responses_request_to_upstream(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    payload: Dict[str, Any] = copy.deepcopy(body)
    payload.pop("_thinking_requested", None)
    payload["model"] = upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model
    payload["stream"] = request_stream_enabled(body, default_stream)
    return payload


def responses_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    texts: List[str] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            if part.get("type") in ("input_text", "output_text", "text"):
                texts.append(part.get("text", ""))
            elif part.get("type") in ("input_image", "image_url"):
                texts.append("[image]")
            else:
                texts.append(json.dumps(part, ensure_ascii=False))
        else:
            texts.append(str(part))
    return "\n".join(text for text in texts if text)


def responses_content_to_chat_content(content: Any) -> Any:
    return content_to_chat_content(content)


def responses_tool_to_chat_tool(tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if tool.get("type") != "function":
        return None
    if isinstance(tool.get("function"), dict):
        function = dict(tool["function"])
    else:
        function = {
            "name": tool.get("name") or "tool",
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {},
        }
    return copy_cache_metadata(tool, {"type": "function", "function": function})


def responses_tool_choice_to_chat(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice.get("type") == "function" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    choice_type = tool_choice.get("type")
    if choice_type in ("auto", "none", "required"):
        return choice_type
    return tool_choice


def enable_chat_stream_usage(payload: Dict[str, Any]) -> None:
    if not payload.get("stream"):
        return
    stream_options = payload.get("stream_options") if isinstance(payload.get("stream_options"), dict) else {}
    stream_options["include_usage"] = True
    payload["stream_options"] = stream_options


def responses_usage_from_chat_usage(usage: Dict[str, Any], fallback_input_tokens: int, fallback_output_text: str) -> Dict[str, Any]:
    converted: Dict[str, Any] = {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or fallback_input_tokens),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or (max(1, len(fallback_output_text) // 4) if fallback_output_text else 0)),
    }
    converted["total_tokens"] = int(usage.get("total_tokens") or converted["input_tokens"] + converted["output_tokens"])
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens", "cached_tokens"):
        if usage.get(key) is not None:
            converted[key] = usage[key]
    for key in ("input_tokens_details", "prompt_tokens_details"):
        if isinstance(usage.get(key), dict):
            converted[key] = dict(usage[key])
    return converted


def responses_request_to_chat_completions(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    system_messages: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_messages.append({"role": "system", "content": instructions})
    if body.get("tools"):
        system_messages.append({"role": "system", "content": "When tools are needed, call the provided tools by their exact names through the native tool_calls API. Do not invent tool names such as shell unless that exact tool is provided. Do not write XML, pseudo-code, <function>, <Invoke>, or markdown tool-call text. If a file path is requested and a command execution tool is available, call that provided command tool to read it instead of guessing."})

    input_items = body.get("input")
    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
    elif isinstance(input_items, list):
        pending_tool_calls: Dict[str, Dict[str, Any]] = {}
        deferred_visible: List[Dict[str, Any]] = []
        def _flush_deferred() -> None:
            nonlocal deferred_visible
            if deferred_visible:
                messages.extend(deferred_visible)
                deferred_visible = []
        def _flush_pending() -> None:
            nonlocal pending_tool_calls
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": "", "tool_calls": list(pending_tool_calls.values())})
                pending_tool_calls = {}
        for item in input_items:
            if not isinstance(item, dict):
                _flush_deferred()
                _flush_pending()
                messages.append({"role": "user", "content": str(item)})
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                _flush_deferred()
                call_id = item.get("call_id") or item.get("id") or f"call_proxy_{now_ms()}"
                arguments = item.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json_dumps_compact(arguments)
                pending_tool_calls[call_id] = {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": item.get("name") or "tool", "arguments": arguments},
                }
                continue
            if item_type in ("function_call_output", "tool_result"):
                _flush_pending()
                call_id = item.get("call_id") or item.get("id") or ""
                output_text = responses_content_to_text(item.get("output") or item.get("content"))
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output_text})
                if output_text:
                    visible_text = anthropic_tool_results_visible_text([{"tool_use_id": call_id, "content": output_text}])
                    deferred_visible.append({"role": "user", "content": visible_text})
                continue
            _flush_deferred()
            _flush_pending()
            role = item.get("role") or ("assistant" if item_type == "message" else "user")
            message_role = "system" if role == "developer" else role
            message = {"role": message_role, "content": responses_content_to_chat_content(item.get("content"))}
            if message["role"] == "system":
                if not has_cache_metadata(item.get("content")):
                    message["content"] = responses_content_to_text(item.get("content"))
                system_messages.append(message)
            else:
                messages.append(message)
        _flush_deferred()
        _flush_pending()
    else:
        messages.append({"role": "user", "content": ""})

    # Merge consecutive same-role messages (except tool/system) to comply with
    # chat_completions API requirements. Consecutive user messages from
    # function_call_output visible text are a common source of this issue.
    merged: List[Dict[str, Any]] = []
    for msg in messages:
        if not merged:
            merged.append(msg)
            continue
        prev = merged[-1]
        prev_role = prev.get("role")
        curr_role = msg.get("role")
        if prev_role == curr_role and curr_role == "user":
            # Merge consecutive user messages
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else str(prev.get("content", ""))
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            prev["content"] = (prev_text + "\n\n" + curr_text).strip()
        elif prev_role == curr_role and curr_role == "assistant" and not prev.get("tool_calls") and not msg.get("tool_calls"):
            # Merge consecutive text-only assistant messages
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else ""
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
            prev["content"] = (prev_text + "\n\n" + curr_text).strip()
        else:
            merged.append(msg)
    final_messages = merged
    if system_messages:
        if any(has_cache_metadata(item.get("content")) for item in system_messages):
            system_content: Any = []
            for item in system_messages:
                content = item.get("content")
                if isinstance(content, list):
                    system_content.extend(content)
                elif content:
                    system_content.append({"type": "text", "text": str(content)})
        else:
            system_content = "\n\n".join(str(item.get("content") or "") for item in system_messages if item.get("content"))
        final_messages = [{"role": "system", "content": system_content}] + messages

    payload: Dict[str, Any] = {
        "model": upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model,
        "messages": final_messages,
        "stream": request_stream_enabled(body, default_stream),
    }
    enable_chat_stream_usage(payload)
    if isinstance(body.get("max_output_tokens"), int):
        payload["max_tokens"] = body["max_output_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        payload["top_p"] = body["top_p"]
    tools = [tool for tool in (responses_tool_to_chat_tool(item) for item in body.get("tools", [])) if tool]
    if tools:
        payload["tools"] = tools
    # WHY: Only set tool_choice when tools are present. Upstream APIs
    # (e.g. qwen-instruct) reject requests with tool_choice but no tools:
    # "When using `tool_choice`, `tools` must be set."
    if body.get("tool_choice") is not None and tools:
        payload["tool_choice"] = responses_tool_choice_to_chat(body["tool_choice"])
    # If messages contain tool_calls/tool role but payload has no tools definition,
    # upstream APIs (e.g. glm-chat) require a tools field to accept tool_calls messages.
    # Extract tool names from messages and generate minimal tool definitions.
    if "tools" not in payload:
        message_tool_names: set = set()
        for msg in final_messages:
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                name = func.get("name")
                if name:
                    message_tool_names.add(name)
        if message_tool_names:
            payload["tools"] = [
                {"type": "function", "function": {"name": name, "description": f"Execute {name}", "parameters": {"type": "object", "properties": {}}}}
                for name in sorted(message_tool_names)
            ]
    # WHY: Preserve reasoning flag for sanitized_upstream_payload_for_model
    if body.get("_reasoning_enabled"):
        payload["_reasoning_enabled"] = True
    if body.get("_thinking_requested"):
        payload["_thinking_requested"] = True
    return payload


def responses_request_to_model_upstream(body: Dict[str, Any], model_config: ModelConfig, fallback_model: str, upstream_model: Optional[str], default_stream: bool = True) -> Dict[str, Any]:
    if model_config.api_format == "chat_completions":
        return responses_request_to_chat_completions(body, fallback_model, upstream_model, default_stream)
    return responses_request_to_upstream(body, fallback_model, upstream_model, default_stream)


def normalize_upstream_url(upstream_url: str, api_format: str) -> str:
    url = upstream_url.strip()
    if api_format == "chat_completions" and not url.rstrip("/").endswith("/chat/completions"):
        return url.rstrip("/") + "/chat/completions"
    return url


def upstream_error_message(exc: urllib.error.HTTPError) -> str:
    error_body = exc.read().decode("utf-8", errors="replace")
    return f"Upstream HTTP {exc.code}: {error_body}"


def open_upstream(payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, api_format: str = "responses") -> urllib.response.addinfourl:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream" if payload.get("stream") else "application/json",
        "authorization": f"Bearer {auth_token}",
    }
    request = urllib.request.Request(normalize_upstream_url(upstream_url, api_format), data=data, headers=headers, method="POST")
    return urllib.request.urlopen(request, timeout=timeout)


def iter_sse_lines(response: urllib.response.addinfourl) -> Iterable[Tuple[Optional[str], str]]:
    event: Optional[str] = None
    data_lines: List[str] = []
    _raw_line_count = 0
    _non_sse_lines: List[str] = []  # WHY: accumulate non-SSE lines to detect JSON error bodies
    while True:
        try:
            raw = response.readline()
        except socket.timeout as exc:
            raise TimeoutError("Timed out while waiting for upstream SSE data") from exc
        if not raw:
            if data_lines:
                yield event, "\n".join(data_lines)
            # WHY: If upstream returned non-SSE content (e.g. a plain JSON error body),
            # yield it as a synthetic "non_sse_body" event so callers can detect errors
            # instead of silently getting an empty stream.
            if _non_sse_lines and not data_lines:
                joined = "\n".join(_non_sse_lines)
                yield "non_sse_body", joined
            if DEBUG_SSE and _raw_line_count == 0:
                log(f"DEBUG_SSE iter_sse_lines: upstream returned 0 raw lines (empty body)")
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        _raw_line_count += 1
        if DEBUG_SSE and _raw_line_count <= 3:
            log(f"DEBUG_SSE raw_line[{_raw_line_count}]={line[:200]}")
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        else:
            _non_sse_lines.append(line)
            if DEBUG_SSE:
                log(f"DEBUG_SSE non_sse_line={line[:300]}")


def chat_tool_call_payloads(tool_calls: Any, is_delta: bool) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    if not isinstance(tool_calls, list):
        return payloads
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        payloads.append({
            "id": tool_call.get("id") or "",
            "index": tool_call.get("index", 0),
            "name": function.get("name") or "",
            "arguments": function.get("arguments") if function.get("arguments") is not None else "",
            "replace_arguments": not is_delta,
        })
    return payloads


def tool_call_kind_from_payloads(payloads: List[Dict[str, Any]], is_delta: bool) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not payloads:
        return "ignore", None
    if len(payloads) == 1:
        return ("tool_call_delta" if is_delta else "tool_call"), payloads[0]
    return ("tool_calls_delta" if is_delta else "tool_calls"), {"tool_calls": payloads}


def extract_text_delta(event: Optional[str], data: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    if data == "[DONE]":
        return "done", None
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return "ignore", None
    event_type = obj.get("type") or event
    if event_type == "response.output_text.delta":
        return "delta", {"text": obj.get("delta", "")}
    if event_type == "response.output_text.done":
        return "text_done", {"text": obj.get("text", "")}
    # WHY: GPT-5.5 and other models send reasoning summary as separate events.
    # Return as "reasoning" kind so it gets emitted as a thinking block in
    # Claude Code instead of appearing as garbled plain text output.
    if event_type == "response.reasoning_summary_text.delta":
        return "reasoning", {"text": obj.get("delta", "")}
    if event_type == "response.reasoning_summary_text.done":
        return "reasoning", {"text": obj.get("text", "")}
    if event_type in ("response.output_item.added", "response.output_item.done"):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else obj
        # WHY: response.output_item.done for message items contains the full
        # accumulated text. When incremental deltas were missed (e.g. upstream
        # skipped response.output_text.delta), this serves as a fallback to
        # recover the response text instead of returning empty.
        if event_type == "response.output_item.done" and item.get("type") == "message":
            msg_content = item.get("content") if isinstance(item.get("content"), list) else []
            for part in msg_content:
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                    return "text_done", {"text": part["text"]}
        # WHY: When upstream sends a reasoning item (e.g. GPT-5.5), extract
        # its summary text as a fallback so reasoning content is not lost.
        if event_type == "response.output_item.done" and item.get("type") == "reasoning":
            summary = item.get("summary") if isinstance(item.get("summary"), list) else []
            for part in summary:
                if isinstance(part, dict) and part.get("type") == "summary_text" and part.get("text"):
                    return "reasoning", {"text": part["text"]}
        if item.get("type") == "function_call":
            return "tool_call", {
                "id": item.get("call_id") or item.get("id") or f"toolu_proxy_{now_ms()}",
                "index": obj.get("output_index", item.get("output_index", 0)),
                "name": item.get("name", ""),
                "arguments": item.get("arguments", "{}"),
                "replace_arguments": event_type == "response.output_item.done",
            }
    if event_type == "response.function_call_arguments.delta":
        return "tool_call_delta", {
            "id": obj.get("call_id") or obj.get("item_id") or "",
            "index": obj.get("output_index", 0),
            "name": obj.get("name", ""),
            "arguments": obj.get("delta", ""),
        }
    if event_type == "response.function_call_arguments.done":
        return "tool_call", {
            "id": obj.get("call_id") or obj.get("item_id") or f"toolu_proxy_{now_ms()}",
            "index": obj.get("output_index", 0),
            "name": obj.get("name", ""),
            "arguments": obj.get("arguments", "{}"),
            "replace_arguments": True,
        }
    if event_type == "response.completed":
        # response.completed contains the full accumulated output (text + tool calls)
        # which was already sent incrementally via output_text.delta / output_item.added /
        # function_call_arguments.delta events. Return "done" so callers can extract
        # stop_reason and usage from the payload without re-emitting content.
        return "done", obj

    choices = obj.get("choices")
    if isinstance(obj.get("usage"), dict) and (not isinstance(choices, list) or not choices):
        return "usage", {"usage": obj["usage"], "raw": obj}
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        is_delta = delta is not None and "tool_calls" in delta
        tool_calls = delta.get("tool_calls") if is_delta else message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return tool_call_kind_from_payloads(chat_tool_call_payloads(tool_calls, is_delta), is_delta)
        # WHY: Some models (e.g. GLM, DeepSeek Pro) send reasoning content in
        # delta.reasoning_content or delta.reasoning instead of delta.content.
        # Return as separate "reasoning" kind so it can be emitted as an
        # Anthropic thinking block instead of plain text.
        reasoning = (delta.get("reasoning_content") if delta else None) or (delta.get("reasoning") if delta else None) or ""
        if reasoning:
            return "reasoning", {"text": reasoning}
        text = (delta.get("content") if delta else None) or ""
        if text:
            return "delta", {"text": text}
        if choice.get("finish_reason"):
            finish_reason = choice.get("finish_reason")
            payload: Dict[str, Any] = {"finish_reason": finish_reason, "raw": obj}
            if isinstance(obj.get("usage"), dict):
                payload["usage"] = obj["usage"]
            return "done", payload

    if event_type in ("error", "response.failed") or obj.get("error"):
        return "error", obj
    # WHY: vLLM upstreams return errors in various non-standard formats when
    # context overflows or other issues occur during streaming. The error may
    # appear as: {"object": "error", "message": "..."}, or the SSE event type
    # may be "error" while the JSON "type" field is different. Detect these
    # so upstream errors are propagated to the client instead of being silently
    # ignored (which results in empty responses).
    if isinstance(obj, dict):
        # vLLM format: {"object": "error", "message": "...", "type": "..."}
        if obj.get("object") == "error" and obj.get("message"):
            return "error", obj
        # Some vLLM versions: {"detail": "error message", "type": "..."}
        if obj.get("detail") and isinstance(obj.get("detail"), str) and not obj.get("choices"):
            return "error", {"error": {"type": "upstream_error", "message": obj["detail"]}, "raw": obj}
        # GenAI platform format: {"success":false,"message":"Tool 'X' disabled...","code":500}
        if obj.get("success") is False and obj.get("message") and isinstance(obj.get("message"), str):
            return "error", {"error": {"type": "upstream_error", "message": obj["message"]}, "raw": obj}
    return "ignore", obj

def parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        if set(arguments) == {"arguments"}:
            nested_value = arguments.get("arguments")
            if isinstance(nested_value, dict):
                return parse_tool_arguments(nested_value)
            if isinstance(nested_value, str):
                return parse_tool_arguments(nested_value)
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    parsed = parse_json_like_object(arguments)
    if parsed is None:
        return {"arguments": arguments}
    if isinstance(parsed, str):
        nested = parse_tool_arguments(parsed)
        return nested if nested else {"arguments": parsed}
    if isinstance(parsed, dict):
        return parse_tool_arguments(parsed)
    return {"arguments": parsed}


def tool_arguments_json(arguments: Any) -> str:
    return json_dumps_compact(parse_tool_arguments(arguments))


def codex_function_call_item(tool_call: Dict[str, Any], offset: int = 0, normalize_shell_aliases: bool = True) -> Dict[str, Any]:
    name = str(tool_call.get("name") or "tool")
    arguments = parse_tool_arguments(tool_call.get("arguments", ""))
    normalized_name = name
    if normalize_shell_aliases and name.lower() in ("shell_exec", "execute_command", "bash"):
        normalized_name = "shell"
    if normalized_name == "shell":
        command = arguments.get("command")
        if isinstance(command, str):
            arguments["command"] = shell_command_argv(command)
        elif isinstance(command, list):
            command_parts = [str(part) for part in command if part is not None]
            executable = command_parts[0].lower() if command_parts else ""
            if executable and not is_direct_shell_executable(executable):
                joined = shell_join_command_parts(command_parts)
                arguments["command"] = shell_command_argv(joined)
        else:
            fallback = arguments.get("arguments")
            if isinstance(fallback, str) and fallback.strip():
                arguments["command"] = shell_command_argv(fallback)
    return {
        "id": tool_call.get("id") or f"fc_proxy_{now_ms()}_{offset}",
        "type": "function_call",
        "status": "completed",
        "call_id": tool_call.get("id") or f"call_proxy_{now_ms()}_{offset}",
        "name": normalized_name,
        "arguments": json_dumps_compact(arguments),
    }


def filter_thinking_text_delta(text: str, state: Dict[str, Any]) -> str:
    if not text:
        return ""
    pending_think_open = state.pop("pending_think_open", "")
    pending_think_close = state.pop("pending_think_close", "")
    pending_open = state.pop("pending_dsml_open", "")
    pending_close = state.pop("pending_dsml_close", "")
    if pending_think_open or pending_think_close or pending_open or pending_close:
        text = f"{pending_think_open}{pending_think_close}{pending_open}{pending_close}{text}"
    output: List[str] = []
    position = 0
    while position < len(text):
        lower = text.lower()
        if state.get("in_thinking"):
            close_index = lower.find("</think>", position)
            if close_index < 0:
                partial_close = partial_dsml_marker_start(lower, position, ("</think>",))
                if partial_close >= 0:
                    state["pending_think_close"] = text[partial_close:]
                return "".join(output)
            position = close_index + len("</think>")
            state["in_thinking"] = False
            continue
        if state.get("in_dsml"):
            close_match = DSML_TOOL_CALLS_CLOSE_RE.search(text, position)
            if not close_match:
                partial_close = partial_dsml_marker_start(text, position, DSML_TOOL_CALLS_CLOSE_PREFIXES)
                if partial_close >= 0:
                    state["pending_dsml_close"] = text[partial_close:]
                return "".join(output)
            position = close_match.end()
            state["in_dsml"] = False
            continue
        open_index = lower.find("<think", position)
        stray_close_index = lower.find("</think>", position)
        if stray_close_index >= 0 and (open_index < 0 or stray_close_index < open_index):
            output.append(text[position:stray_close_index])
            position = stray_close_index + len("</think>")
            continue
        dsml_open = DSML_OPEN_RE.search(text, position)
        dsml_close = DSML_CLOSE_RE.search(text, position)
        if dsml_close and (not dsml_open or dsml_close.start() < dsml_open.start()):
            prefix = text[position:dsml_close.start()]
            if prefix.strip():
                output.append(prefix)
            position = dsml_close.end()
            continue
        if open_index < 0:
            if not dsml_open:
                partial_think_open = partial_dsml_marker_start(lower, position, ("<think",))
                partial_think_close = partial_dsml_marker_start(lower, position, ("</think>",))
                partial_candidates = [index for index in (partial_think_open, partial_think_close) if index >= 0]
                if partial_candidates:
                    partial_index = min(partial_candidates)
                    output.append(text[position:partial_index])
                    if partial_index == partial_think_open:
                        state["pending_think_open"] = text[partial_index:]
                    else:
                        state["pending_think_close"] = text[partial_index:]
                    break
                partial_open = partial_dsml_marker_start(text, position, DSML_OPEN_PREFIXES)
                if partial_open >= 0:
                    prefix = text[position:partial_open]
                    if prefix.strip():
                        output.append(prefix)
                    state["pending_dsml_open"] = text[partial_open:]
                    break
                output.append(text[position:])
                break
            prefix = text[position:dsml_open.start()]
            if prefix.strip():
                output.append(prefix)
            close_match = DSML_TOOL_CALLS_CLOSE_RE.search(text, dsml_open.end())
            if close_match:
                position = close_match.end()
                continue
            state["in_dsml"] = True
            break
        if dsml_open and dsml_open.start() < open_index:
            prefix = text[position:dsml_open.start()]
            if prefix.strip():
                output.append(prefix)
            close_match = DSML_TOOL_CALLS_CLOSE_RE.search(text, dsml_open.end())
            if close_match:
                position = close_match.end()
                continue
            state["in_dsml"] = True
            break
        output.append(text[position:open_index])
        tag_end = text.find(">", open_index)
        if tag_end < 0:
            state["in_thinking"] = True
            break
        position = tag_end + 1
        state["in_thinking"] = True
    return "".join(output)


def stop_reason_from_done(parsed: Optional[Dict[str, Any]], tool_calls: List[Dict[str, Any]]) -> str:
    if tool_calls:
        return "tool_use"
    if not isinstance(parsed, dict):
        return "end_turn"
    finish_reason = parsed.get("finish_reason")
    if finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason in ("length", "max_tokens"):
        # WHY: return "end_turn" not "max_tokens" — Claude Code treats max_tokens as truncated output and stops working,
        # but the model has already produced valid content (just clipped by max_tokens).
        # Returning "end_turn" lets Claude Code continue processing the output normally.
        return "end_turn"
    # Responses API format: the done payload may be the full response object
    # or wrapped in {"response": ...}; check both for status and function_call output
    response_obj = parsed.get("response") if isinstance(parsed.get("response"), dict) else parsed
    status = response_obj.get("status") if isinstance(response_obj, dict) else None
    if status in ("incomplete", "cancelled"):
        # WHY: return "end_turn" not "max_tokens" — Claude Code treats max_tokens as truncated output and stops working,
        # but the model has already produced valid content (just clipped by max_tokens).
        # Returning "end_turn" lets Claude Code continue processing the output normally.
        return "end_turn"
    output = response_obj.get("output") if isinstance(response_obj, dict) and isinstance(response_obj.get("output"), list) else None
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                return "tool_use"
    if status == "failed":
        return "end_turn"


    return "end_turn"

def compact_jsonish_outside_strings(value: str) -> str:
    output: List[str] = []
    in_string = False
    escape = False
    quote = ""
    for char in value:
        if in_string:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
            output.append(char)
            continue
        if char.isspace():
            continue
        output.append(char)
    return "".join(output)


def is_cumulative_tool_argument_snapshot(existing: str, incoming: str) -> bool:
    existing_start = existing.lstrip()[:1]
    incoming_start = incoming.lstrip()[:1]
    if existing_start not in ("{", "[") or incoming_start != existing_start:
        return False
    return compact_jsonish_outside_strings(incoming).startswith(compact_jsonish_outside_strings(existing))


def merge_tool_argument_delta(existing: str, incoming: str) -> str:
    if not incoming:
        return existing
    if not existing:
        return incoming
    if incoming == existing:
        return existing
    if incoming.startswith(existing) or is_cumulative_tool_argument_snapshot(existing, incoming):
        return incoming
    return existing + incoming


def merge_tool_call(tool_calls: List[Dict[str, Any]], parsed: Dict[str, Any]) -> None:
    index = int(parsed.get("index", 0) or 0)
    while len(tool_calls) <= index:
        tool_calls.append({"id": "", "name": "", "arguments": ""})
    target = tool_calls[index]
    if parsed.get("id"):
        target["id"] = parsed["id"]
    if parsed.get("name"):
        target["name"] = parsed["name"]
    arguments = str(parsed.get("arguments", ""))
    if parsed.get("replace_arguments"):
        target["arguments"] = arguments
    else:
        target["arguments"] = merge_tool_argument_delta(target.get("arguments", ""), arguments)


def merge_tool_call_payloads(tool_calls: List[Dict[str, Any]], parsed: Optional[Dict[str, Any]]) -> None:
    if not parsed:
        return
    payloads = parsed.get("tool_calls")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, dict):
                merge_tool_call(tool_calls, payload)
        return
    merge_tool_call(tool_calls, parsed)


def openai_tool_names(tools: Optional[List[Dict[str, Any]]]) -> List[str]:
    names: List[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def normalize_tool_call_name_for_tools(tool_call: Dict[str, Any], tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    tool_names = openai_tool_names(tools)
    if not tool_names:
        return tool_call
    name = str(tool_call.get("name") or "")
    arguments = parse_tool_arguments(tool_call.get("arguments", ""))
    preferred_shell = next((item for item in tool_names if item.lower() in ("bash", "shell", "exec", "run_command", "exec_command")), None)
    normalized_name = best_tool_name(name, arguments, tool_names, preferred_shell)
    if normalized_name == name:
        return tool_call
    updated = dict(tool_call)
    updated["name"] = normalized_name
    return updated


def chat_completion_json_to_responses(payload: Dict[str, Any], model: str, input_tokens: int, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if isinstance(payload.get("error"), dict):
        message = payload["error"].get("message") or json.dumps(payload["error"], ensure_ascii=False)
        raise ValueError(str(message))
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"Upstream response missing choices: {json.dumps(payload, ensure_ascii=False)[:1000]}")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    # WHY: When upstream returns reasoning_content (e.g. GLM, DeepSeek Pro),
    # convert it to a reasoning output item so Codex/Claude Code can display
    # it as collapsible thinking instead of mixing it with output text.
    reasoning_text = message.get("reasoning_content") or message.get("reasoning") or ""
    output_text = message.get("content") or ""
    # WHY: Some models (e.g. minimax) return <think> tags inside content
    # instead of a separate reasoning_content field. Extract the thinking content
    # and strip the tags so they don't appear as garbled output in Claude Code.
    if output_text and '<think>' in output_text and not reasoning_text:
        think_match = re.search(r'<think>' + r'(.*?)' + r'</think>', output_text, re.IGNORECASE | re.DOTALL)
        if think_match:
            reasoning_text = think_match.group(1).strip()
    output_text = strip_thinking_markup(output_text)
    # WHY: When upstream returns reasoning but no content (e.g. qwen-instruct, glm-chat
    # with enable_thinking), the actual response text is in the reasoning field.
    # Use reasoning as the text output when content is empty, so the user gets
    # a response instead of empty output.
    if not output_text and reasoning_text:
        output_text = reasoning_text
        reasoning_text = ""
    if not output_text and not reasoning_text:
        output_text = ""
    output_text, pseudo_tool_calls = parse_pseudo_function_calls(output_text, tools)
    output: List[Dict[str, Any]] = []
    # WHY: Merge reasoning into text with 🤔 prefix so all clients (Codex,
    # Claude Code) can see the reasoning. No separate reasoning item needed.
    if reasoning_text and output_text:
        combined = "🤔 Thinking\n````\n" + reasoning_text + "\n````\n\n" + output_text
        output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": combined}]})
    elif reasoning_text:
        output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "🤔 Thinking\n````\n" + reasoning_text + "\n````"}]})
    elif output_text:
        output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output_text}]})
    parsed_tool_calls = chat_tool_call_payloads(message.get("tool_calls"), False) + pseudo_tool_calls
    for offset, tool_call in enumerate(parsed_tool_calls):
        output.append(codex_function_call_item(normalize_tool_call_name_for_tools(tool_call, tools), offset, normalize_shell_aliases=False))
    response_payload = responses_completed_payload(response_id(), model, output, input_tokens, output_text)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        response_payload["usage"] = responses_usage_from_chat_usage(usage, response_payload["usage"]["input_tokens"], output_text)
    return response_payload


def anthropic_message_id() -> str:
    return f"msg_proxy_{now_ms()}_{uuid.uuid4().hex[:12]}"



# WHY: When client requests thinking but upstream doesn't support it,
# inject a synthetic redacted_thinking block so Claude Code recognizes
# the model as supporting extended thinking (enables auto mode).
_REDACTED_THINKING_DATA = "c2h0dWNvZGVwcm94eV90aGlua2luZ19ibG9ja192MQ=="
_THINKING_PLACEHOLDER_TEXT = "Thinking..."

def thinking_requested(body: Dict[str, Any]) -> bool:
    """Check if the original request asked for extended thinking."""
    return bool(body.get("_thinking_requested"))


def inject_redacted_thinking_to_content(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prepend a redacted_thinking block to Anthropic Messages response content.
    
    WHY: When client requests thinking:{type:enabled} but upstream doesn't support it,
    Claude Code needs a thinking block in the response to recognize the model as
    supporting extended thinking (enables auto mode). We use redacted_thinking
    (opaque data) instead of a visible thinking block to avoid confusing the user
    with fake reasoning content.
    """
    if any(block.get("type") in ("thinking", "redacted_thinking") for block in content):
        return content  # Already has thinking block from upstream
    return [{"type": "redacted_thinking", "data": _REDACTED_THINKING_DATA}] + content


def inject_redacted_thinking_to_responses_output(output: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prepend a redacted reasoning item to an OpenAI Responses output list."""
    if any(isinstance(item, dict) and item.get("type") in ("reasoning", "redacted_thinking") for item in output):
        return output
    reasoning_item = {
        "id": response_output_item_id(),
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": _THINKING_PLACEHOLDER_TEXT}],
    }
    return [reasoning_item] + output



def strip_encrypted_content_from_reasoning(item: Dict[str, Any]) -> Dict[str, Any]:
    """Remove encrypted_content from a reasoning item.
    WHY: Upstream models (e.g. GPT-5.5) return encrypted_content in reasoning items.
    This data is opaque encrypted thinking that cannot be displayed and causes
    garbled output in Claude Code. Strip it so only readable summary_text remains.
    """
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return item
    cleaned = dict(item)
    cleaned.pop("encrypted_content", None)
    return cleaned

def strip_encrypted_content_from_output(output: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove encrypted_content from all reasoning items in an output list."""
    return [strip_encrypted_content_from_reasoning(item) if isinstance(item, dict) and item.get("type") == "reasoning" else item for item in output]

def emit_redacted_thinking_sse(handler: BaseHTTPRequestHandler, index: int = 0) -> None:
    """Emit a redacted_thinking block as SSE events in an Anthropic stream.
    
    WHY: Same reason as inject_redacted_thinking_to_content, but for streaming path.
    Sends content_block_start + content_block_delta + content_block_stop for
    a redacted_thinking block before other content blocks.
    """
    # WHY: Use redacted_thinking type so Claude Code recognizes thinking
    # support without displaying garbled placeholder text to the user.
    write_sse(handler, "content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "redacted_thinking", "data": _REDACTED_THINKING_DATA},
    })
    write_sse(handler, "content_block_stop", {
        "type": "content_block_stop",
        "index": index,
    })



def response_id() -> str:
    return f"resp_proxy_{now_ms()}_{uuid.uuid4().hex[:12]}"


def response_output_item_id(index: int = 0) -> str:
    return f"msg_proxy_{now_ms()}_{index}_{uuid.uuid4().hex[:12]}"


def responses_usage(input_tokens: int, output_text: str) -> Dict[str, int]:
    output_tokens = max(1, len(output_text) // 4) if output_text else 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def responses_completed_payload(request_id: str, model: str, output: List[Dict[str, Any]], input_tokens: int, output_text: str) -> Dict[str, Any]:
    return {
        "id": request_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": responses_usage(input_tokens, output_text),
    }


def response_text_from_upstream_json(payload: Dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, list):
        text = responses_json_output_text(output)
        if text:
            return text
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def responses_error_payload(message: str, error_type: str = "api_error") -> Dict[str, Any]:
    return {"error": {"message": message, "type": error_type}}


def anthropic_error_message_payload(model_config: ModelConfig, message: str, input_tokens: int = 0) -> Dict[str, Any]:
    return {
        "id": anthropic_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model_config.model_id,
        "content": [{"type": "text", "text": message}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": max(1, len(message) // 4)},
    }


def responses_unsupported_modalities_payload(model_config: ModelConfig, message: str, input_tokens: int = 0) -> Dict[str, Any]:
    item_id = response_output_item_id()
    output = [{
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": message}],
    }]
    return responses_completed_payload(response_id(), model_config.model_id, output, input_tokens, message)


def responses_json_output_text(output: List[Dict[str, Any]]) -> str:
    text_parts: List[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and isinstance(item.get("content"), list):
            for part in item["content"]:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text_parts.append(str(part.get("text") or ""))
        elif item.get("type") in ("output_text", "text"):
            text_parts.append(str(item.get("text") or item.get("content") or ""))
    return "".join(text_parts)


def responses_json_to_anthropic_message(payload: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    content: List[Dict[str, Any]] = []
    # WHY: Extract reasoning from output items and convert to thinking blocks
    # for Claude Code compatibility. Real reasoning content becomes a visible
    # thinking block; if no reasoning but thinking was requested, inject
    # redacted_thinking as fallback.
    has_real_thinking = False
    _reasoning_as_text_fallback = ""
    _wants_thinking = False  # WHY: Reasoning always emitted as text, never as thinking block
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "reasoning":
            summary = item.get("summary") if isinstance(item.get("summary"), list) else []
            reasoning_text = ""
            for part in summary:
                if isinstance(part, dict) and part.get("type") == "summary_text" and part.get("text"):
                    reasoning_text += part["text"]
            if reasoning_text:
                if _wants_thinking:
                    content.append({"type": "thinking", "thinking": reasoning_text})
                    has_real_thinking = True
                else:
                    # WHY: When client did not request thinking, convert reasoning
                    # to regular text so it is not silently dropped (e.g. qwen-instruct,
                    # glm-chat with enable_thinking return all content in reasoning).
                    _reasoning_as_text_fallback = reasoning_text
            continue
        if item.get("type") == "function_call":
            # WHY: Skip function_call items with no name — they are empty padding
            # or malformed items that would create phantom "tool" calls (Bug #1).
            fc_name = item.get("name")
            if not fc_name:
                continue
            content.append({
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or f"toolu_proxy_{now_ms()}_{uuid.uuid4().hex[:8]}",
                "name": fc_name,
                "input": parse_tool_arguments(item.get("arguments", "")),
            })
    output_text = responses_json_output_text(output)
    # WHY: When reasoning was not shown as thinking block, include it as
    # 🤔-prefixed text so users can see the reasoning in any client.
    if _reasoning_as_text_fallback and not has_real_thinking:
        if output_text:
            content.append({"type": "text", "text": "🤔 Thinking\n````\n" + _reasoning_as_text_fallback + "\n````\n\n" + output_text})
        else:
            content.append({"type": "text", "text": "🤔 Thinking\n````\n" + _reasoning_as_text_fallback + "\n````"})
    elif output_text:
        content.append({"type": "text", "text": output_text})
    if not content:
        content.append({"type": "text", "text": ""})
    # WHY: Only inject redacted_thinking when client explicitly requested thinking
    # and no real thinking was found. Since we now always emit reasoning as text,
    # this only triggers for models without reasoning support.
    # WHY: Inject a thinking block when client requested thinking but upstream
    # didn't return one. This enables Claude Code auto mode (Bug #2).
    # Use visible thinking with placeholder text instead of redacted_thinking
    # to avoid garbled output from opaque data.
    if thinking_requested(payload) and not has_real_thinking:
        if not any(block.get("type") in ("thinking", "redacted_thinking") for block in content):
            content = [{"type": "thinking", "thinking": _THINKING_PLACEHOLDER_TEXT}] + content
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {
        "id": anthropic_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model_config.model_id,
        "content": content,
        "stop_reason": "tool_use" if any(part.get("type") == "tool_use" for part in content) else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or (max(1, len(output_text) // 4) if output_text else 0)),
        },
    }


def extract_anthropic_usage(done_payload: Optional[Dict[str, Any]], chat_stream_usage: Optional[Dict[str, Any]], text: str) -> Dict[str, Any]:
    """Extract real usage from done_payload or chat_stream_usage, falling back to estimates."""
    if isinstance(done_payload, dict):
        response_obj = done_payload.get("response") if isinstance(done_payload.get("response"), dict) else done_payload
        usage = response_obj.get("usage") if isinstance(response_obj, dict) else None
        if isinstance(usage, dict):
            return {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or (max(1, len(text) // 4) if text else 0)),
            }
    if isinstance(chat_stream_usage, dict):
        return {
            "input_tokens": int(chat_stream_usage.get("prompt_tokens") or chat_stream_usage.get("input_tokens") or 0),
            "output_tokens": int(chat_stream_usage.get("completion_tokens") or chat_stream_usage.get("output_tokens") or (max(1, len(text) // 4) if text else 0)),
        }
    return {"input_tokens": 0, "output_tokens": max(1, len(text) // 4) if text else 0}




def _codex_emit_recovered_response(emit_fn, request_id: str, message_id: str, converted: Dict[str, Any], model_config: ModelConfig, _thinking_requested: bool = False) -> None:
    """Emit a complete Responses SSE sequence from a recovered non-stream Chat Completions response."""
    # Emit output item added
    emit_fn("response.output_item.added", {
        "output_index": 0,
        "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
    })
    # Inject reasoning if thinking was requested
    if _thinking_requested:
        reasoning_id = response_output_item_id()
        # WHY: Use summary_text instead of encrypted_content for synthetic reasoning.
        # encrypted_content with random hex data causes garbled output in Claude Code.
        emit_fn("response.output_item.added", {
            "output_index": 0,
            "item": {"id": reasoning_id, "type": "reasoning", "summary": [{"type": "summary_text", "text": _THINKING_PLACEHOLDER_TEXT}]},
        })
        emit_fn("response.output_item.done", {
            "output_index": 0,
            "item": {"id": reasoning_id, "type": "reasoning", "status": "completed"},
        })
    # Emit text content
    output_text = responses_json_output_text(converted.get("output", []))
    if output_text:
        emit_fn("response.content_part.added", {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        })
        emit_fn("response.output_text.delta", {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": output_text,
        })
        emit_fn("response.output_text.done", {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "text": output_text,
        })
        emit_fn("response.content_part.done", {
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": output_text},
        })
    # Emit tool calls from output — mirror normal stream sequence exactly
    # (added → arguments delta → arguments done → output_item.done per tool)
    tool_outputs = [item for item in converted.get("output", []) if isinstance(item, dict) and item.get("type") == "function_call"]
    output: list = []
    # Close the message item first
    text_item = {"id": message_id, "type": "message", "status": "completed", "role": "assistant",
                 "content": [{"type": "output_text", "text": output_text}] if output_text else []}
    emit_fn("response.output_item.done", {"output_index": 0, "item": text_item})
    output.append(text_item)
    for tool_call in tool_outputs:
        call_id = tool_call.get("call_id", tool_call.get("id", f"call_{response_id()}"))
        item = {"id": call_id, "type": "function_call", "name": tool_call.get("name", ""), "call_id": call_id, "arguments": tool_call.get("arguments", "")}
        output_index = len(output)
        emit_fn("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress")})
        emit_fn("response.function_call_arguments.delta", {"output_index": output_index, "item_id": call_id, "call_id": call_id, "delta": item["arguments"]})
        emit_fn("response.function_call_arguments.done", {"output_index": output_index, "item_id": call_id, "call_id": call_id, "arguments": item["arguments"]})
        emit_fn("response.output_item.done", {"output_index": output_index, "item": item})
        output.append(item)
    # Emit response completed
    usage = converted.get("usage", {})
    emit_fn("response.completed", {
        "response": {
            "id": request_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model_config.model_id,
            "output": converted.get("output", []),
            "usage": usage,
        }
    })

class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "shtu-claude-proxy/0.1"
    protocol_version = "HTTP/1.1"

    def route_path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.send_header("access-control-allow-headers", "*")
        self.end_headers()

    def do_HEAD(self) -> None:
        path = self.route_path()
        if path in ("/", "/health", "/v1"):
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:
        if self.route_path() in ("/", "/health", "/v1"):
            send_json(self, 200, {"ok": True, "service": "shtu-claude-proxy"})
            return
        if self.route_path() in ("/v1/models", "/models"):
            config = current_config()
            models = []
            for mc in config.models:
                model_entry = {"id": mc.model_id, "object": "model", "owned_by": "shtu-proxy"}
                if getattr(mc, "max_context_tokens", 0) > 0:
                    model_entry["max_context_tokens"] = mc.max_context_tokens
                models.append(model_entry)
            # Add Anthropic-compatible alias entries for Claude Code Desktop discovery.
            # Claude Code Desktop filters models by name, rejecting known non-Anthropic
            # providers (glm, gpt, deepseek, qwen, etc.). Without aliases that pass
            # its filter, it reports "Gateway returned no usable models".
            seen_ids = {m["id"] for m in models}
            for alias_id, env_key in CLAUDE_MODEL_ALIASES.items():
                if alias_id in seen_ids:
                    continue
                resolved = config.model_env.get(env_key, "")
                if not resolved:
                    continue
                alias_tokens = 0
                for mc in config.models:
                    if mc.model_id == resolved:
                        alias_tokens = getattr(mc, "max_context_tokens", 0)
                        break
                alias_entry = {"id": alias_id, "object": "model", "owned_by": "anthropic"}
                if alias_tokens > 0:
                    alias_entry["max_context_tokens"] = alias_tokens
                models.append(alias_entry)
                seen_ids.add(alias_id)
            send_json(self, 200, {"object": "list", "data": models})
            return
        # GET /v1/responses/{response_id} - Codex may query stored responses
        rp = self.route_path()
        if rp.startswith("/v1/responses/") and rp.count("/") == 3:
            response_id = rp.split("/v1/responses/")[-1]
            send_json(self, 404, {"type": "error", "error": {"type": "not_found_error", "message": "Response not found (stateless proxy)"}})
            return

        send_json(self, 404, {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}})

    def do_POST(self) -> None:
        route_path = self.route_path()
        log("do_POST path={} raw_path={}".format(route_path, self.path))
        if route_path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            try:
                body = read_json_body(self)
                input_tokens = estimate_anthropic_input_tokens(body)
            except Exception:
                input_tokens = 1
            send_json(self, 200, {"input_tokens": input_tokens})
            return
        if route_path in ("/v1/responses/compact", "/responses/compact", "/v1/v1/responses/compact", "/codex/v1/responses/compact"):
            self.handle_responses_compact()
            return
        if route_path in ("/v1/responses", "/responses", "/codex/v1/responses"):
            self.handle_responses_post()
            return
        # /v1/responses/{response_id}/input_items - Codex sends this for follow-up inputs
        if route_path.endswith("/input_items") and "/responses/" in route_path:
            # Stateless proxy: we cannot append to a completed upstream response.
            # Return a minimal valid response so Codex does not break.
            try:
                body = read_json_body(self)
            except Exception:
                body = {}
            send_json(self, 200, {
                "id": route_path.split("/responses/")[-1].split("/")[0],
                "object": "response",
                "created_at": int(__import__("time").time()),
                "status": "completed",
                "model": "unknown",
                "output": [],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            })
            return
        # /v1/responses/{response_id}/cancel - Codex sends this to cancel in-progress responses
        if route_path.endswith("/cancel") and "/responses/" in route_path:
            send_json(self, 200, {
                "id": route_path.split("/responses/")[-1].split("/")[0],
                "object": "response",
                "status": "cancelled"
            })
            return
        if route_path not in ("/v1/messages", "/messages"):
            send_json(self, 404, {"type": "error", "error": {"type": "not_found_error", "message": "Use /v1/messages or /v1/responses"}})
            return

        try:
            body = read_json_body(self)
            # WHY: "thinking" param is stripped before sending to upstream (no upstream supports it),
            # but _thinking_requested flag is preserved so we can inject redacted_thinking in the response.

            config = current_config()
            classifier_request = is_claude_auto_classifier_request(body)
            stream = False if classifier_request and "stream" not in body else request_stream_enabled(body, config.default_stream)
            model_config = config.find_model(body.get("model"))
            upstream_url = os.getenv("UPSTREAM_RESPONSES_URL") or model_config.base_url
            fallback_model = os.getenv("UPSTREAM_MODEL") or model_config.model_id
            upstream_model = os.getenv("UPSTREAM_MODEL") or model_config.upstream_model
            auth_token = os.getenv("UPSTREAM_API_KEY") or model_config.api_key or os.getenv("ANTHROPIC_AUTH_TOKEN") or ""
            timeout = int(os.getenv("UPSTREAM_TIMEOUT", str(config.timeout)))
            # WHY: When model has Thinking enabled but client didn't send thinking param,
            # inject it so the proxy treats the request as if thinking was requested.
            # This ensures reasoning content is emitted as thinking blocks (collapsible
            # in Claude Code / Codex) instead of plain text.
            # budget_tokens is required by Anthropic API spec; allocate max_tokens-1
            # so thinking gets most of the budget (cc-switch uses the same strategy).
            if not isinstance(body.get("thinking"), dict) and not classifier_request and (getattr(model_config, "enable_thinking", False) or getattr(model_config, "supports_reasoning", False)):
                _budget = max(1, (body.get("max_tokens") or 16384) - 1)
                body["thinking"] = {"type": "enabled", "budget_tokens": _budget}
            body_for_upstream = body
            unsupported = unsupported_modalities(model_config, anthropic_current_user_modalities(body))
            if unsupported:
                log(f"degraded unsupported modalities model={model_config.model_id} modalities={','.join(sorted(unsupported))} stream={stream}")
            body_for_upstream = sanitized_anthropic_body_for_model(body, model_config)
            body["_thinking_requested"] = body_for_upstream.get("_thinking_requested", False)
            if not auth_token:
                send_json(self, 500, {"type": "error", "error": {"type": "authentication_error", "message": f"No API key configured for model {model_config.model_id}"}})
                return

            upstream_payload = anthropic_messages_to_upstream(body_for_upstream, model_config, fallback_model, upstream_model, config.default_stream)
            upstream_payload = sanitized_upstream_payload_for_model(upstream_payload, model_config)
            auto_cache_marks = apply_auto_cache_control(upstream_payload) if model_config.api_format != "chat_completions" else 0
            # WHY: apply_auto_cache_control re-adds cache_control for prompt caching,
            # but no upstream API supports it; strip again after injection.
            upstream_payload = _strip_cache_control(upstream_payload)
            log(
                "request "
                f"model={body.get('model')} route={model_config.model_id} "
                f"upstream_model={upstream_payload.get('model')} "
                f"format={model_config.api_format} "
                f"messages={len(body.get('messages', []))} stream={stream} "
                f"thinking_in_body={'thinking' in body} thinking_in_payload={'thinking' in upstream_payload} "
                f"cache_control={has_cache_metadata(upstream_payload)} auto_cache_marks={auto_cache_marks}"
            )
            if stream:
                upstream_payload["stream"] = True
                self.handle_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
            else:
                self.handle_non_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
        except _BodyTooLargeError:
            return
        except Exception as exc:
            log(traceback.format_exc())
            if not self.wfile.closed:
                try:
                    send_json(self, 500, {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
                except Exception:
                    pass

    def handle_responses_post(self) -> None:
        try:
            body = read_json_body(self)
            config = current_config()
            stream = request_stream_enabled(body, config.default_stream)
            model_config = config.find_model(body.get("model"))
            upstream_url = os.getenv("UPSTREAM_RESPONSES_URL") or model_config.base_url
            fallback_model = os.getenv("UPSTREAM_MODEL") or model_config.model_id
            upstream_model = os.getenv("UPSTREAM_MODEL") or model_config.upstream_model
            auth_token = os.getenv("UPSTREAM_API_KEY") or model_config.api_key or os.getenv("ANTHROPIC_AUTH_TOKEN") or ""
            timeout = int(os.getenv("UPSTREAM_TIMEOUT", str(config.timeout)))
            # WHY: When model has Thinking enabled but client didn't send thinking param,
            # inject it so reasoning content is emitted as reasoning items (collapsible
            # in Codex) instead of plain text.
            if not isinstance(body.get("thinking"), dict) and not is_claude_auto_classifier_request(body) and (getattr(model_config, "enable_thinking", False) or getattr(model_config, "supports_reasoning", False)):
                body["thinking"] = {"type": "enabled", "budget_tokens": max(1, (body.get("max_output_tokens") or body.get("max_tokens") or 16384) - 1)}
            body_for_upstream = body
            unsupported = unsupported_modalities(model_config, responses_current_user_modalities(body))
            if unsupported:
                log(f"degraded unsupported Responses modalities model={model_config.model_id} modalities={','.join(sorted(unsupported))} stream={stream}")
            body_for_upstream = sanitized_responses_body_for_model(body, model_config)
            body["_thinking_requested"] = body_for_upstream.get("_thinking_requested", False)
            if not auth_token:
                send_json(self, 500, responses_error_payload(f"No API key configured for model {model_config.model_id}", "authentication_error"))
                return
            upstream_payload = responses_request_to_model_upstream(body_for_upstream, model_config, fallback_model, upstream_model, config.default_stream)
            upstream_payload = sanitized_upstream_payload_for_model(upstream_payload, model_config)
            auto_cache_marks = apply_auto_cache_control(upstream_payload) if model_config.api_format != "chat_completions" else 0
            # WHY: apply_auto_cache_control re-adds cache_control for prompt caching,
            # but no upstream API supports it; strip again after injection.
            upstream_payload = _strip_cache_control(upstream_payload)
            log(
                "codex request "
                f"model={body.get('model')} route={model_config.model_id} "
                f"upstream_model={upstream_payload.get('model')} "
                f"format={model_config.api_format} stream={stream} "
                f"thinking_in_body={'thinking' in body} thinking_in_payload={'thinking' in upstream_payload} "
                f"cache_control={has_cache_metadata(upstream_payload)} auto_cache_marks={auto_cache_marks}"
            )
            if stream:
                upstream_payload["stream"] = True
                self.handle_responses_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
            else:
                self.handle_responses_non_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
        except _BodyTooLargeError:
            return
        except Exception as exc:
            log(traceback.format_exc())
            if not self.wfile.closed:
                try:
                    send_json(self, 500, responses_error_payload(str(exc)))
                except Exception:
                    pass

    def send_anthropic_text_stream(self, model_config: ModelConfig, text: str, _thinking_requested: bool = False) -> None:
        message_id = anthropic_message_id()
        send_sse_headers(self)
        write_sse(self, "message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model_config.model_id, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        # WHY: If client requested thinking, inject redacted_thinking block before other content
        _thinking_block_index = 0
        # DISABLED: redacted_thinking causes garbled output; reasoning now as text
        # if _thinking_requested:
        #     emit_redacted_thinking_sse(self, index=0)
        write_sse(self, "content_block_start", {"type": "content_block_start", "index": _thinking_block_index, "content_block": {"type": "text", "text": ""}})
        write_sse(self, "content_block_delta", {"type": "content_block_delta", "index": _thinking_block_index, "delta": {"type": "text_delta", "text": text}})
        write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": _thinking_block_index})
        write_sse(self, "message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": max(1, len(text) // 4)}})
        write_sse(self, "message_stop", {"type": "message_stop"})
        self.close_connection = True

    def send_responses_text_stream(self, model_config: ModelConfig, text: str, input_tokens: int = 0) -> None:
        request_id = response_id()
        item_id = response_output_item_id()
        send_sse_headers(self)
        write_sse(self, "response.created", {"type": "response.created", "sequence_number": 0, "response": {"id": request_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model_config.model_id, "output": []}})
        write_sse(self, "response.in_progress", {"type": "response.in_progress", "sequence_number": 1, "response": {"id": request_id, "status": "in_progress"}})
        write_sse(self, "response.output_item.added", {"type": "response.output_item.added", "sequence_number": 2, "output_index": 0, "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}})
        write_sse(self, "response.content_part.added", {"type": "response.content_part.added", "sequence_number": 3, "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
        write_sse(self, "response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 4, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
        write_sse(self, "response.output_text.done", {"type": "response.output_text.done", "sequence_number": 5, "item_id": item_id, "output_index": 0, "content_index": 0, "text": text})
        write_sse(self, "response.content_part.done", {"type": "response.content_part.done", "sequence_number": 6, "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text}})
        output = [{"id": item_id, "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]}]
        write_sse(self, "response.output_item.done", {"type": "response.output_item.done", "sequence_number": 7, "output_index": 0, "item": output[0]})
        write_sse(self, "response.completed", {"type": "response.completed", "sequence_number": 8, "response": responses_completed_payload(request_id, model_config.model_id, output, input_tokens, text)})
        self.close_connection = True

    def handle_responses_compact(self) -> None:
        """Proxy /responses/compact requests.

        For api_format=responses, forward to upstream /compact endpoint.
        For api_format=chat_completions, convert the compact request via
        responses_request_to_chat_completions (preserving full conversation
        context, tools, and instructions), send to upstream, then convert
        the chat response back to Responses compact format.
        """
        try:
            body = read_json_body(self)
            log(f"codex compact request model={body.get('model')} keys={sorted(body.keys())}")
            config = current_config()
            model_config = config.find_model(body.get("model"))
            if not model_config:
                send_json(self, 400, responses_error_payload(f"Unknown model: {body.get('model')}"))
                return
            upstream_url = os.getenv("UPSTREAM_RESPONSES_URL") or model_config.base_url
            fallback_model = os.getenv("UPSTREAM_MODEL") or model_config.model_id
            upstream_model = os.getenv("UPSTREAM_MODEL") or model_config.upstream_model
            auth_token = os.getenv("UPSTREAM_API_KEY") or model_config.api_key or ""
            if not auth_token:
                send_json(self, 500, responses_error_payload(f"No API key configured for model {model_config.model_id}", "authentication_error"))
                return
            timeout = int(os.getenv("UPSTREAM_TIMEOUT", str(config.timeout)))
            stream = request_stream_enabled(body, config.default_stream)

            if model_config.api_format == "responses":
                # Upstream supports Responses API - forward compact directly
                compact_url = upstream_url.rstrip("/")
                if not compact_url.endswith("/compact"):
                    compact_url += "/compact"
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                accept_header = "text/event-stream" if stream else "application/json"
                headers = {
                    "content-type": "application/json",
                    "accept": accept_header,
                    "authorization": f"Bearer {auth_token}",
                }
                request = urllib.request.Request(compact_url, data=data, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        if stream and _response_is_sse(response):
                            self._relay_responses_sse(body, response, model_config)
                        else:
                            raw = response.read().decode("utf-8", errors="replace")
                            result = json.loads(raw)
                            log(f"codex compact done model={model_config.model_id}")
                            send_json(self, 200, result)
                except urllib.error.HTTPError as exc:
                    error_body = exc.read().decode("utf-8", errors="replace")
                    log(f"codex compact upstream error model={model_config.model_id} status={exc.code}")
                    self._handle_compact_via_chat(body, model_config, auth_token, upstream_url, timeout, stream, fallback_model, upstream_model, config)
                except Exception as exc:
                    log(f"codex compact upstream exception: {exc}")
                    self._handle_compact_via_chat(body, model_config, auth_token, upstream_url, timeout, stream, fallback_model, upstream_model, config)
            else:
                # Upstream is chat_completions - convert and forward
                self._handle_compact_via_chat(body, model_config, auth_token, upstream_url, timeout, stream, fallback_model, upstream_model, config)
        except _BodyTooLargeError:
            return
        except Exception as exc:
            log(traceback.format_exc())
            if not self.wfile.closed:
                try:
                    send_json(self, 500, responses_error_payload(str(exc)))
                except Exception:
                    pass

    def _handle_compact_via_chat(self, body: Dict[str, Any], model_config: ModelConfig, auth_token: str, upstream_url: str, timeout: int, stream: bool, fallback_model: str, upstream_model: Optional[str], config: Any) -> None:
        """Handle compact request by converting to/from chat_completions format.

        Always uses the chat_completions conversion pipeline regardless of the
        provider's api_format, because:
        - For api_format=chat_completions: natural conversion
        - For api_format=responses (fallback from upstream /compact failure):
          we cannot re-send to /responses (that's a normal request, not compact),
          so we must convert to chat_completions, let the model process the
          compaction, then convert back to Responses compact format.

        Uses responses_request_to_chat_completions to preserve the full
        conversation context, tools, and instructions.
        """
        try:
            body_for_upstream = sanitized_responses_body_for_model(body, model_config)
            # Always convert to chat_completions format for compact
            upstream_payload = responses_request_to_chat_completions(body_for_upstream, fallback_model, upstream_model, config.default_stream)
            upstream_payload = sanitized_upstream_payload_for_model(upstream_payload, model_config)
            upstream_payload["stream"] = False
            upstream_payload.pop("stream_options", None)

            if stream and model_config.stream_bridge:
                try:
                    with open_upstream(upstream_payload, auth_token, upstream_url, timeout, "chat_completions") as response:
                        raw_payload = response.read().decode("utf-8", errors="replace")
                    _upstream_raw = json.loads(raw_payload)
                    if isinstance(_upstream_raw, dict) and _upstream_raw.get("success") is False:
                        raise ValueError(f"Upstream rejected: {_upstream_raw.get('message', '')}")
                    converted = chat_completion_json_to_responses(
                        _upstream_raw,
                        model_config.model_id,
                        estimate_anthropic_input_tokens(body),
                        upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
                    )
                    output_text = responses_json_output_text(converted.get("output", []))
                    self.send_responses_text_stream(model_config, output_text, estimate_anthropic_input_tokens(body))
                    log(f"codex compact stream_bridge done model={model_config.model_id} chars={len(output_text)}")
                    return
                except Exception as exc:
                    log(f"codex compact stream_bridge error: {exc}")

            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, "chat_completions") as response:
                raw = response.read().decode("utf-8", errors="replace")
            upstream_result = json.loads(raw)
            if isinstance(upstream_result, dict) and upstream_result.get("success") is False:
                raise ValueError(f"Upstream rejected: {upstream_result.get('message', '')}")

            compact_response = chat_completion_json_to_responses(
                upstream_result,
                model_config.model_id,
                estimate_anthropic_input_tokens(body),
                upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
            )
            if not compact_response.get("output"):
                raise ValueError("Empty compaction output from upstream")

            log(f"codex compact chat done model={model_config.model_id} stream={stream}")
            if stream:
                request_id = compact_response.get("id", response_id())
                seq = 0
                def _emit(event_type: str, payload: dict) -> None:
                    nonlocal seq
                    payload.setdefault("type", event_type)
                    payload.setdefault("sequence_number", seq)
                    seq += 1
                    write_sse(self, event_type, payload)
                send_sse_headers(self)
                _emit("response.created", {"response": {"id": request_id, "object": "response", "created_at": compact_response.get("created_at", int(time.time())), "status": "in_progress", "model": model_config.model_id, "output": []}})
                _emit("response.in_progress", {"response": {"id": request_id, "status": "in_progress"}})
                for output_index, item in enumerate(compact_response.get("output", [])):
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "message":
                        content = item.get("content") if isinstance(item.get("content"), list) else []
                        _emit("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress", content=[])})
                        for content_index, part in enumerate(content):
                            _emit("response.content_part.added", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "part": {"type": part.get("type", "output_text"), "text": ""}})
                            if part.get("type") in ("output_text", "text") and part.get("text"):
                                _emit("response.output_text.delta", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "delta": part.get("text", "")})
                                _emit("response.output_text.done", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "text": part.get("text", "")})
                            _emit("response.content_part.done", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "part": part})
                        _emit("response.output_item.done", {"output_index": output_index, "item": item})
                    elif item.get("type") == "function_call":
                        _emit("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress")})
                        _emit("response.function_call_arguments.delta", {"item_id": item.get("id"), "output_index": output_index, "delta": item.get("arguments", "{}")})
                        _emit("response.function_call_arguments.done", {"item_id": item.get("id"), "output_index": output_index, "arguments": item.get("arguments", "{}")})
                        _emit("response.output_item.done", {"output_index": output_index, "item": item})
                _emit("response.completed", {"response": compact_response})
                write_data_sse(self, "[DONE]")
                self.close_connection = True
            else:
                send_json(self, 200, compact_response)
        except Exception as exc:
            log(f"codex compact chat error: {traceback.format_exc()}")
            if stream:
                request_id = response_id()
                seq = 0
                def _emit(event_type: str, payload: dict) -> None:
                    nonlocal seq
                    payload.setdefault("type", event_type)
                    payload.setdefault("sequence_number", seq)
                    seq += 1
                    write_sse(self, event_type, payload)
                send_sse_headers(self)
                _emit("response.created", {"response": {"id": request_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model_config.model_id, "output": []}})
                _emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "server_error", "message": f"Compaction failed: {str(exc)[:200]}"}}})
                write_data_sse(self, "[DONE]")
                self.close_connection = True
            else:
                error_resp = {
                    "id": response_id(),
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": model_config.model_id,
                    "status": "failed",
                    "error": {
                        "type": "server_error",
                        "message": f"Compaction failed: {str(exc)[:200]}",
                    },
                    "output": [],
                }
                send_json(self, 200, error_resp)

    def _relay_responses_sse(self, body: Dict[str, Any], response: Any, model_config: ModelConfig) -> None:
        """Relay upstream SSE stream for Responses compact API directly."""
        try:
            send_sse_headers(self)
            for event, data in iter_sse_lines(response):
                if event and data:
                    write_sse(self, event, json.loads(data) if data.startswith("{") else data)
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            log(f"codex compact sse relay done model={model_config.model_id}")
        except Exception as exc:
            log(f"codex compact sse relay error: {exc}")
            self.close_connection = True

    def handle_responses_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        request_id = response_id()
        message_id = response_output_item_id()
        output_text_parts: List[str] = []
        reasoning_parts: List[str] = []
        _reasoning_code_open = False  # WHY: Track if reasoning code block is open
        tool_calls: List[Dict[str, Any]] = []
        thinking_state: Dict[str, Any] = {"in_thinking": False}
        chat_stream_usage: Optional[Dict[str, Any]] = None
        done_payload: Optional[Dict[str, Any]] = None
        input_tokens = estimate_value_tokens(body.get("input"))
        sequence_number = 0
        text_item_started = False
        reasoning_item_started = False
        reasoning_item_id = ""
        send_sse_headers(self)

        def emit(event_type: str, payload: Dict[str, Any]) -> None:
            nonlocal sequence_number
            payload.setdefault("type", event_type)
            payload.setdefault("sequence_number", sequence_number)
            sequence_number += 1
            write_sse(self, event_type, payload)

        emit("response.created", {"response": {"id": request_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model_config.model_id, "output": []}})
        emit("response.in_progress", {"response": {"id": request_id, "status": "in_progress"}})
        stream_bridge = model_config.stream_bridge
        if model_config.api_format == "chat_completions" and stream_bridge:
            non_stream_payload = dict(upstream_payload)
            non_stream_payload["stream"] = False
            non_stream_payload.pop("stream_options", None)
            try:
                with open_upstream(non_stream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                converted = chat_completion_json_to_responses(
                    json.loads(raw_payload),
                    model_config.model_id,
                    estimate_anthropic_input_tokens(body),
                    non_stream_payload.get("tools") if isinstance(non_stream_payload.get("tools"), list) else None,
                )
                output_text = responses_json_output_text(converted.get("output", []))
                for output_index, item in enumerate(converted.get("output", [])):
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "message":
                        content = item.get("content") if isinstance(item.get("content"), list) else []
                        emit("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress", content=[])})
                        for content_index, part in enumerate(content):
                            emit("response.content_part.added", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "part": {"type": part.get("type", "output_text"), "text": ""}})
                            if part.get("type") in ("output_text", "text") and part.get("text"):
                                emit("response.output_text.delta", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "delta": part.get("text", "")})
                                emit("response.output_text.done", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "text": part.get("text", "")})
                            emit("response.content_part.done", {"item_id": item.get("id"), "output_index": output_index, "content_index": content_index, "part": part})
                        emit("response.output_item.done", {"output_index": output_index, "item": item})
                    elif item.get("type") == "function_call":
                        emit("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress")})
                        emit("response.function_call_arguments.delta", {"item_id": item.get("id"), "output_index": output_index, "delta": item.get("arguments", "{}")})
                        emit("response.function_call_arguments.done", {"item_id": item.get("id"), "output_index": output_index, "arguments": item.get("arguments", "{}")})
                        emit("response.output_item.done", {"output_index": output_index, "item": item})
                emit("response.completed", {"response": converted})
                write_data_sse(self, "[DONE]")
                self.close_connection = True
                return
            except urllib.error.HTTPError as exc:
                message = upstream_error_message(exc)
                emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": message}}})
                write_data_sse(self, "[DONE]")
                self.close_connection = True
                return
            except Exception as exc:
                emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": f"Upstream response error: {exc}"}}})
                write_data_sse(self, "[DONE]")
                self.close_connection = True
                return
        if DEBUG_SSE:
            _tools = upstream_payload.get("tools", [])
            log(f"DEBUG_SSE upstream_payload_tools_count={len(_tools) if isinstance(_tools, list) else 'N/A'}")
            if isinstance(_tools, list):
                for _ti, _t in enumerate(_tools):
                    if isinstance(_t, dict):
                        log(f"DEBUG_SSE tool[{_ti}] type={_t.get('type','?')} name={_t.get('name','?')}")
        codex_upstream_error = ""
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                log(f"codex upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')} url={normalize_upstream_url(upstream_url, model_config.api_format)}")
                _sse_count = 0
                for event, data in iter_sse_lines(response):
                    # WHY: If upstream returned a non-SSE body (e.g. plain JSON error),
                    # parse it immediately as an error to avoid silent empty stream.
                    if event == "non_sse_body":
                        try:
                            non_sse_obj = json.loads(data)
                            if isinstance(non_sse_obj, dict):
                                err = non_sse_obj.get("error")
                                if isinstance(err, dict) and err.get("message"):
                                    log(f"codex upstream non-SSE error model={model_config.model_id} error={err['message'][:300]}")
                                    codex_upstream_error = err["message"]
                                elif non_sse_obj.get("success") is False and non_sse_obj.get("message"):
                                    log(f"codex upstream non-SSE genai error model={model_config.model_id} error={non_sse_obj['message'][:300]}")
                                    codex_upstream_error = non_sse_obj["message"]
                        except json.JSONDecodeError:
                            pass
                        if DEBUG_SSE:
                            log(f"DEBUG_SSE non_sse_body={data[:500]}")
                        continue
                    _sse_count += 1
                    if DEBUG_SSE:
                        log(f"DEBUG_SSE event={event} data={data[:500]}")
                    kind, parsed = extract_text_delta(event, data)
                    if DEBUG_SSE and kind != "ignore":
                        log(f"DEBUG_SSE kind={kind} parsed_keys={list(parsed.keys()) if parsed else None}")
                    if kind == "delta" and parsed:
                        text = filter_thinking_text_delta(parsed.get("text", ""), thinking_state)
                        if text:
                            if _reasoning_code_open:
                                _reasoning_code_open = False
                                emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": "\n````\n\n"})
                                output_text_parts.append("\n````\n\n")
                            output_text_parts.append(text)
                            if not text_item_started:
                                text_item_started = True
                                emit("response.output_item.added", {"output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                                emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
                            emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": text})
                    elif kind == "reasoning" and parsed:
                        reasoning_text = parsed.get("text", "")
                        if reasoning_text:
                            # WHY: When client did NOT request thinking, emit reasoning as
                            # regular text so it is not silently dropped (e.g. qwen-instruct,
                            # glm-chat with enable_thinking return all content in reasoning).
                            _use_as_text = True  # WHY: Always emit reasoning as text (with 🤔 prefix) so all clients can see it
                            if _use_as_text:
                                # WHY: Prepend 🤔 marker to reasoning so users can distinguish
                                # thinking from the actual answer in any client (Codex, Claude Code, etc.)
                                _reasoning_prefix = "🤔 Thinking\n````\n"
                                if not _reasoning_code_open:
                                    output_text_parts.append(_reasoning_prefix)
                                _reasoning_code_open = True
                                output_text_parts.append(reasoning_text)
                                if not text_item_started:
                                    text_item_started = True
                                    emit("response.output_item.added", {"output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                                    emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
                                    if _reasoning_prefix:
                                        emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": _reasoning_prefix})
                                emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": reasoning_text})
                    elif kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta") and parsed:
                        merge_tool_call_payloads(tool_calls, parsed)
                    elif kind == "text_done" and parsed and parsed.get("text"):
                        # WHY: If no incremental deltas were received but the stream
                        # provides complete text via text_done, use it as a fallback.
                        if not output_text_parts and not text_item_started:
                            fallback_text = filter_thinking_text_delta(parsed["text"], thinking_state)
                            if fallback_text:
                                output_text_parts.append(fallback_text)
                                text_item_started = True
                                emit("response.output_item.added", {"output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                                emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
                                emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": fallback_text})
                    elif kind == "usage" and parsed and isinstance(parsed.get("usage"), dict):
                        chat_stream_usage = parsed["usage"]
                    elif kind == "error":
                        emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": parsed}})
                        write_data_sse(self, "[DONE]")
                        self.close_connection = True
                        return
                    elif kind == "done":
                        done_payload = parsed
                        break
        except urllib.error.HTTPError as exc:
            message = upstream_error_message(exc)
            log(f"codex upstream http error model={model_config.model_id} status={exc.code} body={message[:500]}")
            # WHY: GLM and other Chat Completions upstreams may reject streaming requests
            # (e.g. when auto_cache_control modifies message content format) but accept
            # the same payload as non-streaming. Retry once before failing.
            if model_config.api_format == "chat_completions" and not text_item_started and not tool_calls:
                fallback_payload = dict(upstream_payload)
                fallback_payload["stream"] = False
                fallback_payload.pop("stream_options", None)
                try:
                    with open_upstream(fallback_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                        raw_payload = response.read().decode("utf-8", errors="replace")
                    chat_json = json.loads(raw_payload)
                    converted = chat_completion_json_to_responses(
                        chat_json,
                        model_config.model_id,
                        estimate_value_tokens(body.get("messages")),
                        fallback_payload.get("tools") if isinstance(fallback_payload.get("tools"), list) else None,
                    )
                    if converted.get("output"):
                        log(f"codex stream http error recovered via non-stream model={model_config.model_id}")
                        # Emit the recovered response in Responses SSE format
                        _codex_emit_recovered_response(emit, request_id, message_id, converted, model_config, thinking_requested(body))
                        write_data_sse(self, "[DONE]")
                        return
                except Exception as fallback_exc:
                    log(f"codex stream http error non-stream fallback failed model={model_config.model_id} error={fallback_exc}")
            emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": message}}})
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            return
        except Exception as exc:
            log(f"codex upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
            emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": f"Upstream connection error: {exc}"}}})
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            return

        if DEBUG_SSE:
            log(f"DEBUG_SSE stream_event_count={_sse_count} output_text_parts_len={len(output_text_parts)}")
        output_text = "".join(output_text_parts)

        output_text, pseudo_tool_calls = parse_pseudo_function_calls(output_text, body.get("tools"))
        if pseudo_tool_calls:
            for pseudo_tool_call in pseudo_tool_calls:
                merge_tool_call_payloads(tool_calls, pseudo_tool_call)
        if not output_text and not tool_calls:
            # WHY: Check for upstream error in done_payload and retry response
            # before falling back to a generic error message.
            codex_upstream_error = codex_upstream_error or ""
            if isinstance(done_payload, dict):
                raw_obj = done_payload.get("raw", done_payload)
                if isinstance(raw_obj, dict):
                    err = raw_obj.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        codex_upstream_error = err["message"]
                    elif isinstance(err, str) and err:
                        codex_upstream_error = err
                    elif raw_obj.get("object") == "error" and raw_obj.get("message"):
                        codex_upstream_error = raw_obj["message"]
                    elif raw_obj.get("detail") and isinstance(raw_obj.get("detail"), str):
                        codex_upstream_error = raw_obj["detail"]
                    elif raw_obj.get("success") is False and raw_obj.get("message") and isinstance(raw_obj.get("message"), str):
                        codex_upstream_error = raw_obj["message"]
                        log(f"codex stream retry returned genai platform error model={model_config.model_id} error={raw_obj['message'][:200]}")
            retry_payload = dict(upstream_payload)
            retry_payload["stream"] = False
            retry_payload.pop("stream_options", None)
            try:
                with open_upstream(retry_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                if DEBUG_SSE:
                    log(f"DEBUG_SSE retry_raw_payload={raw_payload[:2000]}")
                fallback_payload = json.loads(raw_payload)
                if DEBUG_SSE:
                    log(f"DEBUG_SSE retry_fallback_keys={list(fallback_payload.keys()) if isinstance(fallback_payload, dict) else type(fallback_payload).__name__}")
                # WHY: Check if non-stream retry returned an error response
                if isinstance(fallback_payload, dict):
                    err = fallback_payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        codex_upstream_error = codex_upstream_error or err["message"]
                        log(f"codex stream retry returned error model={model_config.model_id} error={err['message'][:200]}")
                    elif fallback_payload.get("object") == "error" and fallback_payload.get("message"):
                        codex_upstream_error = codex_upstream_error or fallback_payload["message"]
                        log(f"codex stream retry returned error model={model_config.model_id} error={fallback_payload['message'][:200]}")
                    elif fallback_payload.get("detail") and isinstance(fallback_payload.get("detail"), str):
                        codex_upstream_error = codex_upstream_error or fallback_payload["detail"]
                        log(f"codex stream retry returned error model={model_config.model_id} detail={fallback_payload['detail'][:200]}")
                log(f"codex empty stream fallback model={model_config.model_id}")
                output_text = response_text_from_upstream_json(fallback_payload)
                if not output_text and isinstance(fallback_payload.get("choices"), list):
                    converted = chat_completion_json_to_responses(
                        fallback_payload,
                        model_config.model_id,
                        estimate_anthropic_input_tokens(body),
                        retry_payload.get("tools") if isinstance(retry_payload.get("tools"), list) else None,
                    )
                    output_text = responses_json_output_text(converted.get("output", []))
                log(f"codex empty stream fallback model={model_config.model_id} chars={len(output_text)}")
            except urllib.error.HTTPError as retry_http_exc:
                retry_body = retry_http_exc.read().decode("utf-8", errors="replace")
                retry_msg = f"HTTP {retry_http_exc.code}: {retry_body[:300]}"
                codex_upstream_error = codex_upstream_error or retry_msg
                log(f"codex stream retry http error model={model_config.model_id} {retry_msg}")
            except Exception as exc:
                log(f"codex empty stream fallback failed model={model_config.model_id} error={exc}")
        if not output_text and not tool_calls:
            error_msg = codex_upstream_error or "Upstream completed without assistant text or tool calls"
            emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": error_msg}}})
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            return
        output: List[Dict[str, Any]] = []
        # WHY: If we received reasoning content during streaming, add the completed
        # reasoning item to the output before the text message item.
        reasoning_text_joined = "".join(reasoning_parts)
        if reasoning_text_joined:
            if reasoning_item_started:
                emit("response.reasoning_summary_text.done", {"item_id": reasoning_item_id, "output_index": 0, "text": reasoning_text_joined})
            reasoning_item = {"id": reasoning_item_id if reasoning_item_started else response_output_item_id(1), "type": "reasoning", "status": "completed", "summary": [{"type": "summary_text", "text": reasoning_text_joined}]}
            emit("response.output_item.done", {"output_index": 0, "item": reasoning_item})
            output.append(reasoning_item)
        if output_text:
            if _reasoning_code_open:
                _reasoning_code_open = False
                emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": "\n````\n\n"})
                output_text_parts.append("\n````\n\n")
            if not text_item_started:
                emit("response.output_item.added", {"output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
                emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": output_text})
            emit("response.output_text.done", {"item_id": message_id, "output_index": 0, "content_index": 0, "text": output_text})
            text_item = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output_text}]}
            emit("response.content_part.done", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": text_item["content"][0]})
            emit("response.output_item.done", {"output_index": 0, "item": text_item})
            output.append(text_item)
        # WHY: Filter out empty padding entries from merge_tool_call() (Bug #1).
        valid_tool_calls = [tc for tc in tool_calls if tc.get("name") and tc.get("id")]
        for offset, tool_call in enumerate(valid_tool_calls):
            item = codex_function_call_item(tool_call, offset)
            output_index = len(output)
            emit("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress")})
            emit("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": output_index, "delta": item["arguments"]})
            emit("response.function_call_arguments.done", {"item_id": item["id"], "output_index": output_index, "arguments": item["arguments"]})
            emit("response.output_item.done", {"output_index": output_index, "item": item})
            output.append(item)
        # WHY: Inject a reasoning item when client requested thinking but upstream
        # didn't return one. This enables Claude Code auto mode (Bug #2).
        # Use reasoning with placeholder text instead of redacted_thinking.
        if thinking_requested(body):
            if not any(isinstance(item, dict) and item.get("type") in ("reasoning",) for item in output):
                reasoning_item = {
                    "id": response_output_item_id(),
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": _THINKING_PLACEHOLDER_TEXT}],
                }
                output = [reasoning_item] + output
        completed = responses_completed_payload(request_id, model_config.model_id, output, input_tokens, output_text)
        if done_payload and isinstance(done_payload.get("response"), dict):
            completed["usage"] = done_payload["response"].get("usage") or completed["usage"]
        elif chat_stream_usage:
            completed["usage"] = responses_usage_from_chat_usage(chat_stream_usage, completed["usage"]["input_tokens"], output_text)
        log(f"codex response done model={model_config.model_id} chars={len(output_text)} tools={len(tool_calls)}{usage_cache_debug(completed.get('usage'))}")
        emit("response.completed", {"response": completed})
        write_data_sse(self, "[DONE]")
        self.close_connection = True

    def handle_responses_non_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        if model_config.api_format == "chat_completions":
            upstream_payload["stream"] = False
            upstream_payload.pop("stream_options", None)
            try:
                with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                _upstream_raw = json.loads(raw_payload)
                if isinstance(_upstream_raw, dict) and _upstream_raw.get("success") is False:
                    upstream_msg = _upstream_raw.get("message", "") or _upstream_raw.get("error", "") or json.dumps(_upstream_raw, ensure_ascii=False)[:200]
                    raise ValueError(f"Upstream rejected: {upstream_msg}")
                payload = chat_completion_json_to_responses(
                    _upstream_raw,
                    model_config.model_id,
                    estimate_anthropic_input_tokens(body),
                    upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
                )
                if payload.get("output"):
                    # WHY: Inject reasoning placeholder for auto mode (Bug #2)
                    # if thinking_requested(body):
                    #     payload["output"] = inject_redacted_thinking_to_responses_output(payload["output"])
                    #     payload["output"] = strip_encrypted_content_from_output(payload["output"])
                    send_json(self, 200, payload)
                    return
                # Empty output - retry then fall back to streaming
                for _attempt in range(2):
                    log(f"WARNING empty responses non-stream model={model_config.model_id} attempt={_attempt+1}/2")
                    time.sleep(0.5 * (_attempt + 1))
                    try:
                        with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as retry_resp:
                            raw_payload = retry_resp.read().decode("utf-8", errors="replace")
                        _upstream_raw = json.loads(raw_payload)
                        if isinstance(_upstream_raw, dict) and _upstream_raw.get("success") is False:
                            upstream_msg = _upstream_raw.get("message", "") or json.dumps(_upstream_raw, ensure_ascii=False)[:200]
                            raise ValueError(f"Upstream rejected: {upstream_msg}")
                        payload = chat_completion_json_to_responses(
                            _upstream_raw,
                            model_config.model_id,
                            estimate_anthropic_input_tokens(body),
                            upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
                        )
                        if payload.get("output"):
                            break
                    except ValueError:
                        raise
                    except Exception as retry_exc:
                        log(f"WARNING responses non-stream retry failed model={model_config.model_id} error={retry_exc}")
                if payload.get("output"):
                    log(f"codex response done model={model_config.model_id} non_stream=true{usage_cache_debug(payload.get('usage'))}")
                    send_json(self, 200, payload)
                    return
                log(f"WARNING responses non-stream empty after retries, falling back to streaming model={model_config.model_id}")
                # Fall through to streaming fallback below
            except urllib.error.HTTPError as exc:
                send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
                return
            except ValueError as exc:
                send_json(self, 502, responses_error_payload(str(exc)))
                return
            except Exception as exc:
                log(f"responses non-stream fallback model={model_config.model_id} error={exc}")
                # Fall through to streaming fallback
        else:
            upstream_payload["stream"] = False
            upstream_payload.pop("stream_options", None)
            try:
                with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                payload = json.loads(raw_payload)
                if isinstance(payload, dict) and payload.get("success") is False:
                    upstream_msg = payload.get("message", "") or payload.get("error", "") or json.dumps(payload, ensure_ascii=False)[:200]
                    log(f"upstream error model={model_config.model_id} message={upstream_msg}")
                    send_json(self, 502, responses_error_payload(f"Upstream rejected: {upstream_msg}"))
                    return
                # DISABLED: redacted_thinking causes garbled output; reasoning now in code block
                # if thinking_requested(body) and isinstance(payload.get("output"), list):
                #     payload["output"] = inject_redacted_thinking_to_responses_output(payload["output"])
                #     payload["output"] = strip_encrypted_content_from_output(payload["output"])
                send_json(self, 200, payload)
                return
            except urllib.error.HTTPError as exc:
                message = upstream_error_message(exc)
                log(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:500]}")
                send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
                return
            except Exception as exc:
                log(f"non-stream responses fallback model={model_config.model_id} error={exc}")
        upstream_payload["stream"] = True
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        _reasoning_code_open = False  # WHY: Track if reasoning code block is open
        reasoning_item_started = False
        reasoning_item_id = ""
        tool_calls: List[Dict[str, Any]] = []
        done_payload: Optional[Dict[str, Any]] = None
        thinking_state: Dict[str, Any] = {"in_thinking": False}
        chat_stream_usage: Optional[Dict[str, Any]] = None
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                for event, data in iter_sse_lines(response):
                    kind, parsed = extract_text_delta(event, data)
                    if kind == "delta" and parsed:
                        text = filter_thinking_text_delta(parsed.get("text", ""), thinking_state)
                        if text:
                            text_parts.append(text)
                    elif kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta") and parsed:
                        merge_tool_call_payloads(tool_calls, parsed)
                    elif kind == "error":
                        send_json(self, 502, responses_error_payload(json.dumps(parsed, ensure_ascii=False)))
                        return
                    elif kind == "reasoning" and parsed:
                        reasoning_text = parsed.get("text", "")
                        if reasoning_text:
                            _use_as_text = True  # WHY: Always emit reasoning as text (with 🤔 prefix) so all clients can see it
                            if _use_as_text:
                                # WHY: Prepend 🤔 marker to reasoning so users can distinguish
                                # thinking from the actual answer in any client (Codex, Claude Code, etc.)
                                _reasoning_prefix = "🤔 Thinking\n````\n"
                                if not _reasoning_code_open:
                                    output_text_parts.append(_reasoning_prefix)
                                _reasoning_code_open = True
                                output_text_parts.append(reasoning_text)
                                if not text_item_started:
                                    text_item_started = True
                                    emit("response.output_item.added", {"output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                                    emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
                                    if _reasoning_prefix:
                                        emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": _reasoning_prefix})
                                emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": reasoning_text})
                    elif kind == "done":
                        done_payload = parsed
                        break
        except urllib.error.HTTPError as exc:
            send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
            return
        except Exception as exc:
            send_json(self, 502, responses_error_payload(f"Upstream connection error: {exc}"))
            return
        output_text = "".join(text_parts)
        output: List[Dict[str, Any]] = []
        # WHY: If we received reasoning content during streaming, add the completed
        # reasoning item to the output before the text message item.
        reasoning_text_joined = "".join(reasoning_parts)
        # WHY: Merge reasoning into text with 🤔 code block format so all clients can see it
        if reasoning_text_joined and output_text:
            combined = "🤔 Thinking\n```\n" + reasoning_text_joined + "\n```\n\n" + output_text
            output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": combined}]})
        elif reasoning_text_joined:
            combined = "🤔 Thinking\n```\n" + reasoning_text_joined + "\n```"
            output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": combined}]})
        elif output_text:
            output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output_text}]})
        for offset, tool_call in enumerate(tool_calls):
            output.append(codex_function_call_item(tool_call, offset))
        # DISABLED: redacted_thinking causes garbled output; reasoning now in code block
        # if thinking_requested(body):
        #     output = inject_redacted_thinking_to_responses_output(output)
        #     output = strip_encrypted_content_from_output(output)
        payload = responses_completed_payload(response_id(), model_config.model_id, output, estimate_anthropic_input_tokens(body), output_text)
        if done_payload and isinstance(done_payload.get("response"), dict):
            payload["usage"] = done_payload["response"].get("usage") or payload["usage"]
        send_json(self, 200, payload)

    def _send_anthropic_stream_error(self, error_message: str, text_block_started: bool, text_block_stopped: bool) -> None:
        """Send an error as a proper Anthropic SSE stream ending sequence instead of a non-standard error event."""
        if not text_block_started:
            write_sse(self, "content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            text_block_started = True
        if not text_block_stopped:
            write_sse(self, "content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f"[Proxy Error] {error_message}"},
            })
            write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": 0})
            text_block_stopped = True
        write_sse(self, "message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        write_sse(self, "message_stop", {"type": "message_stop"})
        self.close_connection = True


    def handle_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        message_id = anthropic_message_id()
        model = body.get("model", model_config.model_id)  # WHY: Use original model name from request so Claude Code CLI recognizes it and enables streaming rendering
        send_sse_headers(self)
        write_sse(self, "message_start", {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": estimate_anthropic_input_tokens(body), "output_tokens": 0},
            },
        })


        # WHY: If client requested thinking and model does NOT support native reasoning,
        # inject a synthetic redacted_thinking block so Claude Code recognizes extended thinking.
        # If model DOES support reasoning, skip pre-injection so the real thinking block
        # from upstream reasoning_content can be emitted instead.
        _thinking_block_index = 0
        _model_supports_reasoning = getattr(model_config, 'supports_reasoning', False)
        # WHY: Inject a thinking block when client requested thinking but model
        # doesn't support native reasoning. This enables Claude Code auto mode (Bug #2).
        # Use visible thinking with placeholder text instead of redacted_thinking
        # to avoid garbled output from opaque data.
        if thinking_requested(body) and not _model_supports_reasoning:
            write_sse(self, "content_block_start", {
                "type": "content_block_start",
                "index": _thinking_block_index,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            write_sse(self, "content_block_delta", {
                "type": "content_block_delta",
                "index": _thinking_block_index,
                "delta": {"type": "thinking_delta", "thinking": _THINKING_PLACEHOLDER_TEXT},
            })
            write_sse(self, "content_block_stop", {
                "type": "content_block_stop",
                "index": _thinking_block_index,
            })
            _thinking_block_index += 1

        output_text_parts: List[str] = []
        reasoning_parts: List[str] = []
        _reasoning_code_open = False  # WHY: Track if reasoning code block is open
        tool_calls: List[Dict[str, Any]] = []
        delta_count = 0
        text_block_started = False
        thinking_block_started = False
        text_block_stopped = False
        done_payload: Optional[Dict[str, Any]] = None
        chat_stream_usage: Optional[Dict[str, Any]] = None
        thinking_state: Dict[str, Any] = {"in_thinking": False}
        # WHY: Collect upstream error info from SSE events so it can be
        # propagated to the client instead of producing an empty response.
        stream_error_message: str = ""
        stream_bridge = model_config.stream_bridge
        if model_config.api_format == "chat_completions" and stream_bridge:
            non_stream_payload = dict(upstream_payload)
            non_stream_payload["stream"] = False
            non_stream_payload.pop("stream_options", None)
            try:
                for _bridge_attempt in range(3):
                    with open_upstream(non_stream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                        raw_payload = response.read().decode("utf-8", errors="replace")
                    chat_json = json.loads(raw_payload)
                    if isinstance(chat_json, dict) and chat_json.get("success") is False:
                        upstream_msg = chat_json.get("message", "") or json.dumps(chat_json, ensure_ascii=False)[:200]
                        raise ValueError(f"Upstream rejected: {upstream_msg}")
                    if isinstance(chat_json.get("choices"), list):
                        converted = chat_completion_json_to_responses(
                            chat_json,
                            model_config.model_id,
                            estimate_value_tokens(body.get("messages")),
                            non_stream_payload.get("tools") if isinstance(non_stream_payload.get("tools"), list) else None,
                        )
                        if converted.get("output"):
                            break
                        log(f"WARNING stream_bridge empty model={model_config.model_id} attempt={_bridge_attempt+1}/3")
                        if _bridge_attempt < 2:
                            time.sleep(0.5 * (_bridge_attempt + 1))
                            continue
                    else:
                        converted = None
                        break
                    # Empty after all retries - break and fall through to real streaming
                    if not converted.get("output"):
                        log(f"WARNING stream_bridge empty after retries, falling back to real streaming model={model_config.model_id}")
                        converted = None
                        break
                if converted and converted.get("output"):
                    if thinking_requested(body): converted["_thinking_requested"] = True
                    anthropic_msg = responses_json_to_anthropic_message(converted, model_config)
                else:
                    # Fall through to real streaming below
                    raise ValueError("stream_bridge empty, use real streaming")
                # The rest of the stream_bridge SSE emission
                for block_index, block in enumerate(anthropic_msg.get("content", []), _thinking_block_index):
                    block_type = block.get("type", "text")
                    # WHY: Anthropic protocol requires thinking blocks to have empty thinking
                    # in content_block_start, then thinking_delta events for the actual content.
                    # Putting full text in content_block_start causes Claude Code to display
                    # raw thinking text as garbled output.
                    if block_type == "thinking":
                        thinking_text = block.get("thinking", "")
                        write_sse(self, "content_block_start", {"type": "content_block_start", "index": block_index, "content_block": {"type": "thinking", "thinking": ""}})
                        if thinking_text:
                            write_sse(self, "content_block_delta", {"type": "content_block_delta", "index": block_index, "delta": {"type": "thinking_delta", "thinking": thinking_text}})
                    else:
                        write_sse(self, "content_block_start", {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": block,
                        })
                        if block_type == "text" and block.get("text"):
                            write_sse(self, "content_block_delta", {
                                "type": "content_block_delta",
                                "index": block_index,
                                "delta": {"type": "text_delta", "text": block["text"]},
                            })
                        elif block_type == "tool_use":
                            write_sse(self, "content_block_delta", {
                                "type": "content_block_delta",
                                "index": block_index,
                                "delta": {"type": "input_json_delta", "partial_json": json_dumps_compact(block.get("input", {}))},
                            })
                    write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": block_index})
                stop_reason = anthropic_msg.get("stop_reason", "end_turn")
                usage = anthropic_msg.get("usage", {})
                write_sse(self, "message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)},
                })
                write_sse(self, "message_stop", {"type": "message_stop"})
                self.close_connection = True
                return
            except urllib.error.HTTPError as exc:
                message = upstream_error_message(exc)
                log(f"qwen bridge http error model={model_config.model_id} status={exc.code} body={message[:500]}")
                self._send_anthropic_stream_error(message, text_block_started, text_block_stopped)
                return
            except ValueError as exc:
                if "stream_bridge empty" in str(exc):
                    log(f"stream_bridge empty, falling back to real streaming model={model_config.model_id}")
                    # Fall through to real streaming below - do NOT return
                else:
                    log(f"qwen bridge error model={model_config.model_id} error={exc}")
                    self._send_anthropic_stream_error(f"Qwen bridge error: {exc}", text_block_started, text_block_stopped)
                    return
            except Exception as exc:
                log(f"qwen bridge error model={model_config.model_id} error={exc}")
                self._send_anthropic_stream_error(f"Qwen bridge error: {exc}", text_block_started, text_block_stopped)
                return

        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                log(f"upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')}")
                for event, data in iter_sse_lines(response):
                    kind, parsed = extract_text_delta(event, data)

                    if kind == "delta":
                        raw_text = parsed.get("text", "") if parsed else ""
                        text = filter_thinking_text_delta(raw_text, thinking_state)
                        if not text:
                            continue
                        if _reasoning_code_open:
                            _reasoning_code_open = False
                            write_sse(self, "content_block_delta", {
                                "type": "content_block_delta",
                                "index": _thinking_block_index,
                                "delta": {"type": "text_delta", "text": "\n````\n\n"},
                            })
                            output_text_parts.append("\n````\n\n")
                        if not text_block_started:
                            # WHY: Close thinking block before starting text block.
                            # Anthropic SSE requires blocks to be properly nested:
                            # content_block_start -> deltas -> content_block_stop
                            # before the next content_block_start.
                            if thinking_block_started:
                                write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": _thinking_block_index - 1})
                                thinking_block_started = False
                            write_sse(self, "content_block_start", {
                                "type": "content_block_start",
                                "index": _thinking_block_index,
                                "content_block": {"type": "text", "text": ""},
                            })
                            text_block_started = True
                        output_text_parts.append(text)
                        delta_count += 1
                        write_sse(self, "content_block_delta", {
                            "type": "content_block_delta",
                            "index": _thinking_block_index,
                            "delta": {"type": "text_delta", "text": text},
                        })
                    elif kind == "reasoning" and parsed:
                        reasoning_text = parsed.get("text", "")
                        if reasoning_text:
                            # WHY: When client requested thinking, emit reasoning as a thinking
                            # block so Claude Code displays it as collapsible reasoning.
                            # When client did NOT request thinking, emit reasoning as regular
                            # text so it is not silently dropped (e.g. qwen-instruct, glm-chat
                            # with enable_thinking return all content in reasoning).
                            _use_as_text = not thinking_requested(body)  # WHY: When client requests thinking, emit as thinking block so Claude Code renders it incrementally; otherwise emit as text with 🤔 prefix
                            if _use_as_text:
                                # WHY: Prepend 🤔 marker to reasoning so users can distinguish
                                # thinking from the actual answer in any client
                                _reasoning_prefix = "🤔 Thinking\n````\n"
                                if not _reasoning_code_open:
                                    output_text_parts.append(_reasoning_prefix)
                                _reasoning_code_open = True
                                if not text_block_started:
                                    write_sse(self, "content_block_start", {
                                        "type": "content_block_start",
                                        "index": _thinking_block_index,
                                        "content_block": {"type": "text", "text": ""},
                                    })
                                    text_block_started = True
                                    if _reasoning_prefix:
                                        write_sse(self, "content_block_delta", {
                                            "type": "content_block_delta",
                                            "index": _thinking_block_index,
                                            "delta": {"type": "text_delta", "text": _reasoning_prefix},
                                        })
                                output_text_parts.append(reasoning_text)
                                delta_count += 1
                                write_sse(self, "content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": _thinking_block_index,
                                    "delta": {"type": "text_delta", "text": reasoning_text},
                                })
                            else:
                                reasoning_parts.append(reasoning_text)
                                if not thinking_block_started:
                                    thinking_block_started = True
                                    write_sse(self, "content_block_start", {
                                        "type": "content_block_start",
                                        "index": _thinking_block_index,
                                        "content_block": {"type": "thinking", "thinking": ""},
                                    })
                                    _thinking_block_index += 1
                                write_sse(self, "content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": _thinking_block_index - 1,
                                    "delta": {"type": "thinking_delta", "thinking": reasoning_text},
                                })
                    elif kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta") and parsed:
                        merge_tool_call_payloads(tool_calls, parsed)
                    elif kind == "text_done" and parsed and parsed.get("text"):
                        # WHY: If no incremental deltas were received but the stream
                        # provides the complete text via text_done (e.g. from
                        # response.output_item.done or response.output_text.done),
                        # use it as a fallback delta to avoid empty responses.
                        if not output_text_parts and not text_block_started:
                            fallback_text = filter_thinking_text_delta(parsed["text"], thinking_state)
                            if fallback_text:
                                write_sse(self, "content_block_start", {
                                    "type": "content_block_start",
                                    "index": _thinking_block_index,
                                    "content_block": {"type": "text", "text": ""},
                                })
                                text_block_started = True
                                output_text_parts.append(fallback_text)
                                delta_count += 1
                                write_sse(self, "content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": _thinking_block_index,
                                    "delta": {"type": "text_delta", "text": fallback_text},
                                })
                    elif kind == "usage" and parsed and isinstance(parsed.get("usage"), dict):
                        chat_stream_usage = parsed["usage"]
                    elif kind == "error":
                        # WHY: Extract a human-readable error message from the upstream
                        # error payload before sending to client. This message is also
                        # saved in stream_error_message as a safety net in case the
                        # SSE write fails and we fall through to the empty-stream path.
                        _err_msg = ""
                        if isinstance(parsed, dict):
                            err_obj = parsed.get("error")
                            if isinstance(err_obj, dict):
                                _err_msg = err_obj.get("message", "")
                            elif isinstance(err_obj, str):
                                _err_msg = err_obj
                            if not _err_msg and parsed.get("message"):
                                _err_msg = parsed["message"]
                            if not _err_msg and parsed.get("detail"):
                                _err_msg = str(parsed["detail"])
                        if not _err_msg:
                            _err_msg = json.dumps(parsed, ensure_ascii=False)[:500]
                        stream_error_message = _err_msg
                        log(f"upstream stream error model={model_config.model_id} error={_err_msg[:200]}")
                        self._send_anthropic_stream_error(_err_msg, text_block_started, text_block_stopped)
                        return
                    elif kind == "done":
                        done_payload = parsed
                        break
        except urllib.error.HTTPError as exc:
            message = upstream_error_message(exc)
            log(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:500]}")
            if model_config.api_format == "chat_completions" and not text_block_started and not tool_calls:
                fallback_payload = dict(upstream_payload)
                fallback_payload["stream"] = False
                fallback_payload.pop("stream_options", None)
                try:
                    with open_upstream(fallback_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                        raw_payload = response.read().decode("utf-8", errors="replace")
                    chat_json = json.loads(raw_payload)
                    converted = chat_completion_json_to_responses(
                        chat_json,
                        model_config.model_id,
                        estimate_value_tokens(body.get("messages")),
                        fallback_payload.get("tools") if isinstance(fallback_payload.get("tools"), list) else None,
                    )
                    if converted.get("output"):
                        if thinking_requested(body):
                            converted["_thinking_requested"] = True
                        anthropic_msg = responses_json_to_anthropic_message(converted, model_config)
                        log(f"stream http error recovered via non-stream model={model_config.model_id}")
                        self._emit_anthropic_message_as_stream(anthropic_msg, _thinking_block_index)
                        return
                except Exception as fallback_exc:
                    log(f"stream http error non-stream fallback failed model={model_config.model_id} error={fallback_exc}")
            self._send_anthropic_stream_error(message, text_block_started, text_block_stopped)
            return
        except Exception as exc:
            log(f"upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
            self._send_anthropic_stream_error(f"Upstream connection error: {exc}", text_block_started, text_block_stopped)
            return

        output_text = "".join(output_text_parts)
        output_text, pseudo_tool_calls = parse_pseudo_function_calls(output_text, body.get("tools"))
        if pseudo_tool_calls:
            for pseudo_tool_call in pseudo_tool_calls:
                merge_tool_call_payloads(tool_calls, pseudo_tool_call)
        if not output_text and not tool_calls:
            # WHY: When upstream returns 200 but stream deltas contain no text
            # (e.g. upstream skipped incremental events, or all deltas were
            # filtered by filter_thinking_text_delta), retry with a non-streaming
            # request to recover the response, mirroring the Codex fallback logic.
            # Step 1: Try extracting from done_payload first (cheaper, no new request)
            if isinstance(done_payload, dict):
                response_obj = done_payload.get("response") if isinstance(done_payload.get("response"), dict) else done_payload
                fallback_output = response_obj.get("output") if isinstance(response_obj, dict) else None
                if isinstance(fallback_output, list):
                    output_text = responses_json_output_text(fallback_output)
                if not output_text:
                    fallback_choices = done_payload.get("raw", {}).get("choices") if isinstance(done_payload.get("raw"), dict) else None
                    if isinstance(fallback_choices, list) and fallback_choices:
                        output_text = fallback_choices[0].get("message", {}).get("content", "") if isinstance(fallback_choices[0], dict) else ""
            # WHY: Check if the ignored SSE events contained an upstream error.
            # When upstream returns HTTP 200 but the SSE body contains an error
            # (e.g. vLLM context overflow), extract_text_delta() may have
            # returned "error" before the fix, but some edge-case formats
            # might still be missed. Check done_payload and stream_error_message
            # as safety nets.
            upstream_error_message_text = stream_error_message
            if isinstance(done_payload, dict):
                # Check done_payload for error info (chat completions format)
                raw_obj = done_payload.get("raw", done_payload)
                if isinstance(raw_obj, dict):
                    err = raw_obj.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        upstream_error_message_text = err["message"]
                    elif isinstance(err, str) and err:
                        upstream_error_message_text = err
                    elif raw_obj.get("object") == "error" and raw_obj.get("message"):
                        upstream_error_message_text = raw_obj["message"]
                    elif raw_obj.get("detail") and isinstance(raw_obj.get("detail"), str):
                        upstream_error_message_text = raw_obj["detail"]
            # Step 2: If done_payload extraction failed, re-issue as non-streaming
            if not output_text:
                retry_payload = dict(upstream_payload)
                retry_payload["stream"] = False
                retry_payload.pop("stream_options", None)
                try:
                    with open_upstream(retry_payload, auth_token, upstream_url, timeout, model_config.api_format) as retry_resp:
                        raw_retry = retry_resp.read().decode("utf-8", errors="replace")
                    retry_json = json.loads(raw_retry)
                    # WHY: Check if non-stream retry returned an error response
                    # (e.g. context overflow). If so, extract the error message
                    # instead of silently producing an empty response.
                    if isinstance(retry_json, dict):
                        err = retry_json.get("error")
                        if isinstance(err, dict) and err.get("message"):
                            upstream_error_message_text = upstream_error_message_text or err["message"]
                            log(f"anthropic stream retry returned error model={model_config.model_id} error={err['message'][:200]}")
                        elif retry_json.get("object") == "error" and retry_json.get("message"):
                            upstream_error_message_text = upstream_error_message_text or retry_json["message"]
                            log(f"anthropic stream retry returned error model={model_config.model_id} error={retry_json['message'][:200]}")
                        elif retry_json.get("detail") and isinstance(retry_json.get("detail"), str):
                            upstream_error_message_text = upstream_error_message_text or retry_json["detail"]
                            log(f"anthropic stream retry returned error model={model_config.model_id} detail={retry_json['detail'][:200]}")
                    output_text = response_text_from_upstream_json(retry_json)
                    if not output_text and model_config.api_format == "chat_completions" and isinstance(retry_json.get("choices"), list):
                        converted = chat_completion_json_to_responses(
                            retry_json, model_config.model_id,
                            estimate_anthropic_input_tokens(body),
                            retry_payload.get("tools") if isinstance(retry_payload.get("tools"), list) else None,
                        )
                        output_text = responses_json_output_text(converted.get("output", []))
                except urllib.error.HTTPError as retry_http_exc:
                    retry_body = retry_http_exc.read().decode("utf-8", errors="replace")
                    retry_msg = f"HTTP {retry_http_exc.code}: {retry_body[:300]}"
                    upstream_error_message_text = upstream_error_message_text or retry_msg
                    log(f"anthropic stream retry http error model={model_config.model_id} {retry_msg}")
                except Exception as retry_exc:
                    log(f"anthropic stream non-stream retry failed model={model_config.model_id} error={retry_exc}")
            # WHY: If both stream and retry failed and we have an upstream error
            # message, send it to the client instead of an empty response.
            # This ensures the user sees the actual error (e.g. context overflow)
            # instead of a silent empty reply.
            if not output_text and not tool_calls and upstream_error_message_text:
                log(f"anthropic empty stream with upstream error model={model_config.model_id} error={upstream_error_message_text[:200]}")
                self._send_anthropic_stream_error(upstream_error_message_text, text_block_started, text_block_stopped)
                return
            log(f"anthropic empty stream fallback model={model_config.model_id} chars={len(output_text)}")
            if output_text and not text_block_started:
                # Fallback recovered text - emit content_block events
                write_sse(self, "content_block_start", {
                    "type": "content_block_start",
                    "index": _thinking_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
                text_block_started = True
                write_sse(self, "content_block_delta", {
                    "type": "content_block_delta",
                    "index": _thinking_block_index,
                    "delta": {"type": "text_delta", "text": output_text},
                })
        # WHY: When thinking was requested but upstream only returned reasoning with no
        # actual text content, the response would have only thinking blocks and no text.
        # Claude Code CLI's reactive compact counts text blocks as "assistant messages"
        # and reports "no assistant message in summarization response" if none exist.
        # Emit reasoning as a text block so compact and other CLI features can work.
        if not text_block_started and reasoning_parts and thinking_requested(body):
            reasoning_text = "".join(reasoning_parts)
            if thinking_block_started:
                write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": _thinking_block_index - 1})
                thinking_block_started = False
            write_sse(self, "content_block_start", {
                "type": "content_block_start",
                "index": _thinking_block_index,
                "content_block": {"type": "text", "text": ""},
            })
            text_block_started = True
            output_text_parts.append(reasoning_text)
            write_sse(self, "content_block_delta", {
                "type": "content_block_delta",
                "index": _thinking_block_index,
                "delta": {"type": "text_delta", "text": reasoning_text},
            })
        # WHY: Close the thinking block if it was started during streaming
        if thinking_block_started:
            write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": _thinking_block_index - 1})
            thinking_block_started = False
        if _reasoning_code_open:
            _reasoning_code_open = False
            write_sse(self, "content_block_delta", {
                "type": "content_block_delta",
                "index": _thinking_block_index,
                "delta": {"type": "text_delta", "text": "\n````\n"},
            })
            output_text_parts.append("\n````\n")
        if text_block_started and not text_block_stopped:
            write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": _thinking_block_index})
            text_block_stopped = True
        next_index = (1 if text_block_started else 0) + _thinking_block_index
        # WHY: Filter out empty padding entries from merge_tool_call() that have
        # no real id/name. Without this filter, padding entries are emitted as
        # phantom "tool" calls that Claude Code cannot handle (Bug #1).
        valid_tool_calls = [tc for tc in tool_calls if tc.get("name") and tc.get("id")]
        for offset, tool_call in enumerate(valid_tool_calls):
            block_index = next_index + offset
            tool_id = tool_call.get("id") or f"toolu_proxy_{now_ms()}_{offset}"
            tool_name = tool_call.get("name") or "tool"
            arguments = tool_call.get("arguments", "")
            write_sse(self, "content_block_start", {
                "type": "content_block_start",
                "index": block_index,
                "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
            })
            write_sse(self, "content_block_delta", {
                "type": "content_block_delta",
                "index": block_index,
                "delta": {"type": "input_json_delta", "partial_json": tool_arguments_json(arguments)},
            })
            write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": block_index})
        stop_reason = stop_reason_from_done(done_payload, tool_calls)
        response_usage = done_payload.get("response", {}).get("usage") if isinstance(done_payload, dict) and isinstance(done_payload.get("response"), dict) else None
        if not response_usage and chat_stream_usage:
            response_usage = responses_usage_from_chat_usage(chat_stream_usage, 0, output_text)
        log(f"response done model={model_config.model_id} deltas={delta_count} chars={len(output_text)} tools={len(tool_calls)}{usage_cache_debug(response_usage)}")
        write_sse(self, "message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": estimate_anthropic_input_tokens(body), "output_tokens": max(1, len(output_text) // 4) if output_text else 0},
        })
        write_sse(self, "message_stop", {"type": "message_stop"})
        self.close_connection = True

    def _emit_anthropic_message_as_stream(self, anthropic_msg: Dict[str, Any], start_index: int = 0) -> None:
        for block_index, block in enumerate(anthropic_msg.get("content", []), start_index):
            block_type = block.get("type", "text") if isinstance(block, dict) else "text"
            if block_type == "thinking":
                # WHY: Anthropic protocol requires thinking blocks to have empty thinking
                # in content_block_start, then thinking_delta events for the actual content.
                # Putting full text in content_block_start causes Claude Code to display
                # raw thinking text as garbled output.
                thinking_text = block.get("thinking", "")
                write_sse(self, "content_block_start", {"type": "content_block_start", "index": block_index, "content_block": {"type": "thinking", "thinking": ""}})
                if thinking_text:
                    write_sse(self, "content_block_delta", {"type": "content_block_delta", "index": block_index, "delta": {"type": "thinking_delta", "thinking": thinking_text}})
            elif block_type == "redacted_thinking":
                # WHY: redacted_thinking blocks contain opaque encrypted data.
                # Emit as content_block_start with the redacted data, then stop.
                # Claude Code recognizes this as thinking support without showing text.
                write_sse(self, "content_block_start", {"type": "content_block_start", "index": block_index, "content_block": {"type": "redacted_thinking", "data": block.get("data", "")}})
            else:
                write_sse(self, "content_block_start", {"type": "content_block_start", "index": block_index, "content_block": block})
                if block_type == "text" and block.get("text"):
                    write_sse(self, "content_block_delta", {"type": "content_block_delta", "index": block_index, "delta": {"type": "text_delta", "text": block["text"]}})
                elif block_type == "tool_use":
                    write_sse(self, "content_block_delta", {"type": "content_block_delta", "index": block_index, "delta": {"type": "input_json_delta", "partial_json": json_dumps_compact(block.get("input", {}))}})
            write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": block_index})
        usage = anthropic_msg.get("usage", {}) if isinstance(anthropic_msg.get("usage"), dict) else {}
        write_sse(self, "message_delta", {"type": "message_delta", "delta": {"stop_reason": anthropic_msg.get("stop_reason", "end_turn"), "stop_sequence": None}, "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}})
        write_sse(self, "message_stop", {"type": "message_stop"})
        self.close_connection = True

    def handle_non_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        upstream_payload["stream"] = False
        upstream_payload.pop("stream_options", None)
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                raw_payload = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw_payload)
            if thinking_requested(body): payload["_thinking_requested"] = True
            if isinstance(payload, dict) and payload.get("success") is False:
                upstream_msg = payload.get("message", "") or payload.get("error", "") or json.dumps(payload, ensure_ascii=False)[:200]
                log(f"upstream error model={model_config.model_id} message={upstream_msg}")
                raise ValueError(f"Upstream rejected: {upstream_msg}")
            if isinstance(payload.get("output"), list):
                log(f"response done model={model_config.model_id} non_stream=true{usage_cache_debug(payload.get('usage'))}")
                send_json(self, 200, responses_json_to_anthropic_message(payload, model_config))
                return
            if isinstance(payload.get("choices"), list):
                for _attempt in range(3):
                    converted = chat_completion_json_to_responses(
                        payload,
                        model_config.model_id,
                        estimate_value_tokens(body.get("messages")),
                        upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
                    )
                    if converted.get("output"):
                        break
                    log(f"WARNING empty non-stream response model={model_config.model_id} attempt={_attempt+1}/3")
                    if _attempt < 2:
                        time.sleep(0.5 * (_attempt + 1))
                        try:
                            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as retry_resp:
                                raw_payload = retry_resp.read().decode("utf-8", errors="replace")
                            payload = json.loads(raw_payload)
                        except Exception as retry_exc:
                            log(f"WARNING non-stream retry failed model={model_config.model_id} error={retry_exc}")
                if converted.get("output"):
                    if thinking_requested(body): converted["_thinking_requested"] = True
                    anthropic_msg = responses_json_to_anthropic_message(converted, model_config)
                    log(f"response done model={model_config.model_id} non_stream=true{usage_cache_debug(converted.get('usage'))}")
                    send_json(self, 200, anthropic_msg)
                    return
                log(f"WARNING non-stream empty after retries, falling back to streaming model={model_config.model_id}")
                # Fall through to streaming fallback below
            else:
                raise ValueError("Responses JSON missing output list")
        except urllib.error.HTTPError as exc:
            send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
            return
        except Exception as exc:
            log(f"codex non-stream responses fallback model={model_config.model_id} error={exc}")
        upstream_payload["stream"] = True
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        reasoning_item_started = False
        reasoning_item_id = ""
        tool_calls: List[Dict[str, Any]] = []
        done_payload: Optional[Dict[str, Any]] = None
        thinking_state: Dict[str, Any] = {"in_thinking": False}
        chat_stream_usage: Optional[Dict[str, Any]] = None
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                log(f"upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')}")
                for event, data in iter_sse_lines(response):
                    kind, parsed = extract_text_delta(event, data)
                    if kind == "delta" and parsed:
                        text = filter_thinking_text_delta(parsed.get("text", ""), thinking_state)
                        if text:
                            text_parts.append(text)
                    elif kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta") and parsed:
                        merge_tool_call_payloads(tool_calls, parsed)
                    elif kind == "usage" and parsed and isinstance(parsed.get("usage"), dict):
                        chat_stream_usage = parsed["usage"]
                    elif kind == "error":
                        send_json(self, 502, {
                            "type": "error",
                            "error": {"type": "api_error", "message": json.dumps(parsed, ensure_ascii=False)},
                        })
                        return
                    elif kind == "done":
                        done_payload = parsed
                        break
        except urllib.error.HTTPError as exc:
            message = upstream_error_message(exc)
            log(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:500]}")
            send_json(self, 502, {"type": "error", "error": {"type": "api_error", "message": message}})
            return
        except Exception as exc:
            log(f"upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
            send_json(self, 502, {"type": "error", "error": {"type": "api_error", "message": f"Upstream connection error: {exc}"}})
            return
        text = "".join(text_parts)
        content: List[Dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for offset, tool_call in enumerate(tool_calls):
            content.append({
                "type": "tool_use",
                "id": tool_call.get("id") or f"toolu_proxy_{now_ms()}_{offset}",
                "name": tool_call.get("name") or "tool",
                "input": parse_tool_arguments(tool_call.get("arguments", "")),
            })
        if not content:
            content.append({"type": "text", "text": ""})
        send_json(self, 200, {
            "id": anthropic_message_id(),
            "type": "message",
            "role": "assistant",
            "model": model_config.model_id,
            "content": content,
            "stop_reason": stop_reason_from_done(done_payload, tool_calls),
            "stop_sequence": None,
            "usage": extract_anthropic_usage(done_payload, chat_stream_usage, text),
        })

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self.client_address[0]} {format % args}")

    def handle(self) -> None:
        """Suppress ConnectionResetError/BrokenPipeError from client disconnects.

        WHY: Claude Code long-running requests may time out on the client side,
        causing ConnectionResetError/BrokenPipeError spam in logs. These are
        normal behavior — the server just needs to stop writing to the closed socket.
        """
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass



def main() -> None:
    parser = argparse.ArgumentParser(description="Anthropic Messages to OpenAI Responses proxy")
    config = load_config()
    parser.add_argument("--host", default=os.getenv("HOST", config.host))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", str(config.port))))
    args = parser.parse_args()

    global ACTIVE_CONFIG
    ACTIVE_CONFIG = config
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.daemon_threads = True
    log(f"Listening on http://{args.host}:{args.port}")
    log(f"Configured models: {', '.join(model.model_id for model in config.models)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
