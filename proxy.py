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

from config_store import AppConfig, ModelConfig, load_config

DEFAULT_UPSTREAM_URL = "https://genaiapi.shanghaitech.edu.cn/api/v1/response"
DEFAULT_MODEL = "GPT-5.5"
ANTHROPIC_VERSION = "2023-06-01"
ACTIVE_CONFIG: Optional[AppConfig] = None
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
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


def request_stream_enabled(body: Dict[str, Any], default_stream: bool = True) -> bool:
    if "stream" in body:
        return bool(body.get("stream"))
    return bool(default_stream)


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


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("content-length", "0") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


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


def write_sse(handler: BaseHTTPRequestHandler, event: str, data: Dict[str, Any]) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    handler.wfile.write(payload.encode("utf-8"))
    handler.wfile.flush()


def write_data_sse(handler: BaseHTTPRequestHandler, data: str) -> None:
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


def normalize_content_part(part: Dict[str, Any]) -> Any:
    part_type = part.get("type")
    if part_type in ("text", "input_text"):
        return {"type": "input_text", "text": part.get("text", "")}
    if part_type in ("input_image", "input_file"):
        return dict(part)
    if part_type == "image":
        source = part.get("source") or {}
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            return {"type": "input_image", "image_url": f"data:{media_type};base64,{data}"}
        if source.get("type") == "url":
            return {"type": "input_image", "image_url": source.get("url", "")}
    if part_type == "document":
        source = part.get("source") if isinstance(part.get("source"), dict) else {}
        if source.get("type") == "url":
            return {"type": "input_file", "file_url": source.get("url", "")}
        if source.get("type") == "base64":
            file_data = f"data:{source.get('media_type', 'application/octet-stream')};base64,{source.get('data', '')}"
            return {"type": "input_file", "filename": part.get("title") or source.get("filename") or "document", "file_data": file_data}
    if part_type in ("image_url", "file", "input_audio"):
        return dict(part)
    return {"type": "input_text", "text": json.dumps(part, ensure_ascii=False)}


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
        return file_part if file_value else None
    if part_type == "file" and isinstance(part.get("file"), dict):
        return {"type": "file", "file": dict(part["file"])}
    return None


def audio_chat_part_from_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    part_type = part.get("type")
    if part_type == "input_audio":
        input_audio = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else {}
        if input_audio:
            return {"type": "input_audio", "input_audio": dict(input_audio)}
    return None


def content_part_to_chat_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    part_type = part.get("type")
    if part_type in ("text", "input_text", "output_text"):
        return {"type": "text", "text": part.get("text", "")}
    if part_type == "document":
        normalized = normalize_content_part(part)
        return file_chat_part_from_part(normalized) if isinstance(normalized, dict) else None
    image_url = image_url_from_part(part)
    if image_url:
        return {"type": "image_url", "image_url": {"url": image_url}}
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
    return max(1, total)


MODALITY_PART_TYPES = {
    "image": {"image", "input_image", "image_url"},
    "audio": {"input_audio", "audio"},
    "video": {"video", "input_video", "video_url"},
}
MULTIMODAL_PART_TYPES = set().union(*MODALITY_PART_TYPES.values())


def content_modalities(content: Any) -> set[str]:
    found: set[str] = set()
    parts = [content] if isinstance(content, dict) else content if isinstance(content, list) else []
    if not isinstance(parts, list):
        return found
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        for modality, part_types in MODALITY_PART_TYPES.items():
            if part_type in part_types:
                found.add(modality)
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


def sanitized_content_for_model(content: Any, model_config: ModelConfig) -> Any:
    if isinstance(content, list):
        sanitized: List[Any] = []
        for part in content:
            if isinstance(part, dict) and unsupported_modalities(model_config, content_modalities(part)):
                continue
            sanitized.append(part)
        return sanitized
    if isinstance(content, dict) and unsupported_modalities(model_config, content_modalities(content)):
        return ""
    return content


def sanitized_anthropic_body_for_model(body: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    sanitized = dict(body)
    messages: List[Any] = []
    for message in body.get("messages", []):
        if isinstance(message, dict) and unsupported_modalities(model_config, content_modalities(message.get("content"))):
            sanitized_message = dict(message)
            sanitized_message["content"] = sanitized_content_for_model(message.get("content"), model_config)
            messages.append(sanitized_message)
        else:
            messages.append(message)
    sanitized["messages"] = messages
    return sanitized


def sanitized_responses_body_for_model(body: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    sanitized = dict(body)
    input_items = body.get("input")
    if isinstance(input_items, dict):
        sanitized_item = dict(input_items)
        if unsupported_modalities(model_config, content_modalities(sanitized_item)):
            sanitized_item = {"role": sanitized_item.get("role", "user"), "content": sanitized_content_for_model(sanitized_item.get("content", ""), model_config)}
        else:
            sanitized_item["content"] = sanitized_content_for_model(sanitized_item.get("content"), model_config)
        sanitized["input"] = sanitized_item
    elif isinstance(input_items, list):
        sanitized_items: List[Any] = []
        for item in input_items:
            if isinstance(item, dict):
                sanitized_item = dict(item)
                if unsupported_modalities(model_config, content_modalities(sanitized_item)):
                    sanitized_item = {"role": sanitized_item.get("role", "user"), "content": sanitized_content_for_model(sanitized_item.get("content", ""), model_config)}
                else:
                    sanitized_item["content"] = sanitized_content_for_model(sanitized_item.get("content"), model_config)
                sanitized_items.append(sanitized_item)
            else:
                sanitized_items.append(item)
        sanitized["input"] = sanitized_items
    return sanitized


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
        converted.append({"type": "function", "function": function})
    return converted


def anthropic_tools_to_responses_tools(tools: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append({
            "type": "function",
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        })
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
                "content": tool_result_content_to_text(result.get("content", "")),
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
        return [{"role": "assistant", "content": text or None, "tool_calls": tool_calls}]
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
                "output": tool_result_content_to_text(result.get("content", "")),
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

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        system_texts.append(system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict) and item.get("type") == "text":
                system_texts.append(item.get("text", ""))
            elif isinstance(item, str):
                system_texts.append(item)

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        input_items.extend(anthropic_message_to_responses_items(message))

    payload: Dict[str, Any] = {
        "model": upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model,
        "input": input_items,
        "stream": request_stream_enabled(body, default_stream),
    }

    if system_texts:
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
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    return payload


def anthropic_messages_to_chat_completions(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        system_text = anthropic_content_to_text(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        messages.extend(anthropic_message_to_chat_messages(message))

    payload: Dict[str, Any] = {
        "model": upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model,
        "messages": messages,
        "stream": request_stream_enabled(body, default_stream),
    }
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
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return payload


def anthropic_messages_to_upstream(body: Dict[str, Any], model_config: ModelConfig, fallback_model: str, upstream_model: Optional[str], default_stream: bool = True) -> Dict[str, Any]:
    if model_config.api_format == "chat_completions":
        return anthropic_messages_to_chat_completions(body, fallback_model, upstream_model, default_stream)
    return anthropic_messages_to_responses(body, fallback_model, upstream_model, default_stream)


def responses_request_to_upstream(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(body)
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
    return {"type": "function", "function": function}


def responses_tool_choice_to_chat(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice.get("type") == "function" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return tool_choice


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
        for item in input_items:
            if not isinstance(item, dict):
                messages.append({"role": "user", "content": str(item)})
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_proxy_{now_ms()}"
                arguments = item.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json_dumps_compact(arguments)
                pending_tool_calls[call_id] = {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": item.get("name") or "tool", "arguments": arguments},
                }
                messages.append({"role": "assistant", "content": None, "tool_calls": [pending_tool_calls[call_id]]})
                continue
            if item_type in ("function_call_output", "tool_result"):
                call_id = item.get("call_id") or item.get("id") or ""
                output_text = responses_content_to_text(item.get("output") or item.get("content"))
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output_text})
                if output_text:
                    visible_text = anthropic_tool_results_visible_text([{"tool_use_id": call_id, "content": output_text}])
                    messages.append({"role": "user", "content": visible_text})
                continue
            role = item.get("role") or ("assistant" if item_type == "message" else "user")
            message_role = "system" if role == "developer" else role
            message = {"role": message_role, "content": responses_content_to_chat_content(item.get("content"))}
            if message["role"] == "system":
                message["content"] = responses_content_to_text(item.get("content"))
                system_messages.append(message)
            else:
                messages.append(message)
    else:
        messages.append({"role": "user", "content": ""})

    final_messages = messages
    if system_messages:
        system_content = "\n\n".join(str(item.get("content") or "") for item in system_messages if item.get("content"))
        final_messages = [{"role": "system", "content": system_content}] + messages

    payload: Dict[str, Any] = {
        "model": upstream_model or os.getenv("UPSTREAM_MODEL") or body.get("model") or fallback_model,
        "messages": final_messages,
        "stream": request_stream_enabled(body, default_stream),
    }
    if isinstance(body.get("max_output_tokens"), int):
        payload["max_tokens"] = body["max_output_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        payload["top_p"] = body["top_p"]
    tools = [tool for tool in (responses_tool_to_chat_tool(item) for item in body.get("tools", [])) if tool]
    if tools:
        payload["tools"] = tools
    if body.get("tool_choice") is not None:
        payload["tool_choice"] = responses_tool_choice_to_chat(body["tool_choice"])
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
    while True:
        try:
            raw = response.readline()
        except socket.timeout as exc:
            raise TimeoutError("Timed out while waiting for upstream SSE data") from exc
        if not raw:
            if data_lines:
                yield event, "\n".join(data_lines)
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
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
    if event_type in ("response.output_item.added", "response.output_item.done"):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else obj
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
        response_obj = obj.get("response") if isinstance(obj.get("response"), dict) else obj
        output = response_obj.get("output") if isinstance(response_obj, dict) else None
        if isinstance(output, list):
            tool_payloads: List[Dict[str, Any]] = []
            for output_index, item in enumerate(output):
                if isinstance(item, dict) and item.get("type") == "function_call":
                    tool_payloads.append({
                        "id": item.get("call_id") or item.get("id") or f"toolu_proxy_{now_ms()}",
                        "index": item.get("output_index", output_index),
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                        "replace_arguments": True,
                    })
            if tool_payloads:
                return tool_call_kind_from_payloads(tool_payloads, False)
        return "done", obj

    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else None
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        is_delta = delta is not None and "tool_calls" in delta
        tool_calls = delta.get("tool_calls") if is_delta else message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return tool_call_kind_from_payloads(chat_tool_call_payloads(tool_calls, is_delta), is_delta)
        text = (delta.get("content") if delta else None) or message.get("content") or ""
        if text:
            return "delta", {"text": text}
        if choice.get("finish_reason"):
            finish_reason = choice.get("finish_reason")
            return "done", {"finish_reason": finish_reason, "raw": obj}

    if event_type in ("error", "response.failed") or obj.get("error"):
        return "error", obj
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


def codex_function_call_item(tool_call: Dict[str, Any], offset: int = 0) -> Dict[str, Any]:
    name = str(tool_call.get("name") or "tool")
    arguments = parse_tool_arguments(tool_call.get("arguments", ""))
    normalized_name = name
    if name.lower() in ("shell_exec", "execute_command", "bash"):
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
    finish_reason = parsed.get("finish_reason") if isinstance(parsed, dict) else None
    if finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason in ("length", "max_tokens"):
        return "max_tokens"
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


def chat_completion_json_to_responses(payload: Dict[str, Any], model: str, input_tokens: int, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if isinstance(payload.get("error"), dict):
        message = payload["error"].get("message") or json.dumps(payload["error"], ensure_ascii=False)
        raise ValueError(str(message))
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"Upstream response missing choices: {json.dumps(payload, ensure_ascii=False)[:1000]}")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    output_text = message.get("content") or ""
    output_text, pseudo_tool_calls = parse_pseudo_function_calls(output_text, tools)
    output: List[Dict[str, Any]] = []
    if output_text:
        output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output_text}]})
    parsed_tool_calls = chat_tool_call_payloads(message.get("tool_calls"), False) + pseudo_tool_calls
    for offset, tool_call in enumerate(parsed_tool_calls):
        output.append(codex_function_call_item(tool_call, offset))
    response_payload = responses_completed_payload(response_id(), model, output, input_tokens, output_text)
    usage = payload.get("usage")
    if isinstance(usage, dict):
        response_payload["usage"] = {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or response_payload["usage"]["input_tokens"]),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or response_payload["usage"]["output_tokens"]),
            "total_tokens": int(usage.get("total_tokens") or response_payload["usage"]["total_tokens"]),
        }
    return response_payload


def anthropic_message_id() -> str:
    return f"msg_proxy_{now_ms()}_{uuid.uuid4().hex[:12]}"


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
    output_text = responses_json_output_text(output)
    if output_text:
        content.append({"type": "text", "text": output_text})
    for item in output:
        if isinstance(item, dict) and item.get("type") == "function_call":
            content.append({
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id") or f"toolu_proxy_{now_ms()}_{uuid.uuid4().hex[:8]}",
                "name": item.get("name") or "tool",
                "input": parse_tool_arguments(item.get("arguments", "")),
            })
    if not content:
        content.append({"type": "text", "text": ""})
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


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "shtu-claude-proxy/0.1"

    def route_path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.send_header("access-control-allow-headers", "*")
        self.end_headers()

    def do_HEAD(self) -> None:
        if self.route_path() in ("/", "/health", "/v1"):
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
        send_json(self, 404, {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}})

    def do_POST(self) -> None:
        route_path = self.route_path()
        if route_path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            try:
                body = read_json_body(self)
                input_tokens = estimate_anthropic_input_tokens(body)
            except Exception:
                input_tokens = 1
            send_json(self, 200, {"input_tokens": input_tokens})
            return
        if route_path in ("/v1/responses", "/responses"):
            self.handle_responses_post()
            return
        if route_path not in ("/v1/messages", "/messages"):
            send_json(self, 404, {"type": "error", "error": {"type": "not_found_error", "message": "Use /v1/messages or /v1/responses"}})
            return

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
            body_for_upstream = body
            unsupported = unsupported_modalities(model_config, anthropic_current_user_modalities(body))
            if unsupported:
                message = unsupported_modalities_message(model_config, unsupported)
                log(f"blocked unsupported modalities model={model_config.model_id} modalities={','.join(sorted(unsupported))} stream={stream}")
                if stream:
                    self.send_anthropic_text_stream(model_config, message)
                else:
                    send_json(self, 200, anthropic_error_message_payload(model_config, message, estimate_anthropic_input_tokens(body)))
                return
            body_for_upstream = sanitized_anthropic_body_for_model(body, model_config)
            if not auth_token:
                send_json(self, 500, {"type": "error", "error": {"type": "authentication_error", "message": f"No API key configured for model {model_config.model_id}"}})
                return

            upstream_payload = anthropic_messages_to_upstream(body_for_upstream, model_config, fallback_model, upstream_model, config.default_stream)
            log(
                "request "
                f"model={body.get('model')} route={model_config.model_id} "
                f"upstream_model={upstream_payload.get('model')} "
                f"format={model_config.api_format} "
                f"messages={len(body.get('messages', []))} stream={stream}"
            )
            if stream:
                upstream_payload["stream"] = True
                self.handle_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
            else:
                self.handle_non_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
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
            body_for_upstream = body
            unsupported = unsupported_modalities(model_config, responses_current_user_modalities(body))
            if unsupported:
                message = unsupported_modalities_message(model_config, unsupported)
                log(f"blocked unsupported Responses modalities model={model_config.model_id} modalities={','.join(sorted(unsupported))} stream={stream}")
                if stream:
                    self.send_responses_text_stream(model_config, message, estimate_value_tokens(body.get("input")))
                else:
                    send_json(self, 200, responses_unsupported_modalities_payload(model_config, message, estimate_value_tokens(body.get("input"))))
                return
            body_for_upstream = sanitized_responses_body_for_model(body, model_config)
            if not auth_token:
                send_json(self, 500, responses_error_payload(f"No API key configured for model {model_config.model_id}", "authentication_error"))
                return
            upstream_payload = responses_request_to_model_upstream(body_for_upstream, model_config, fallback_model, upstream_model, config.default_stream)
            log(
                "codex request "
                f"model={body.get('model')} route={model_config.model_id} "
                f"upstream_model={upstream_payload.get('model')} "
                f"format={model_config.api_format} stream={stream}"
            )
            if stream:
                upstream_payload["stream"] = True
                self.handle_responses_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
            else:
                self.handle_responses_non_streaming(body, upstream_payload, auth_token, upstream_url, timeout, model_config)
        except Exception as exc:
            log(traceback.format_exc())
            if not self.wfile.closed:
                try:
                    send_json(self, 500, responses_error_payload(str(exc)))
                except Exception:
                    pass

    def send_anthropic_text_stream(self, model_config: ModelConfig, text: str) -> None:
        message_id = anthropic_message_id()
        send_sse_headers(self)
        write_sse(self, "message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model_config.model_id, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        write_sse(self, "content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        write_sse(self, "content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
        write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": 0})
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

    def handle_responses_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        request_id = response_id()
        message_id = response_output_item_id()
        output_text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        thinking_state: Dict[str, Any] = {"in_thinking": False}
        done_payload: Optional[Dict[str, Any]] = None
        sequence_number = 0
        send_sse_headers(self)

        def emit(event_type: str, payload: Dict[str, Any]) -> None:
            nonlocal sequence_number
            payload.setdefault("type", event_type)
            payload.setdefault("sequence_number", sequence_number)
            sequence_number += 1
            write_sse(self, event_type, payload)

        emit("response.created", {"response": {"id": request_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model_config.model_id, "output": []}})
        emit("response.in_progress", {"response": {"id": request_id, "status": "in_progress"}})
        qwen_stream_bridge = "qwen" in f"{model_config.model_id} {model_config.upstream_model}".lower()
        if model_config.api_format == "chat_completions" and qwen_stream_bridge:
            non_stream_payload = dict(upstream_payload)
            non_stream_payload["stream"] = False
            try:
                with open_upstream(non_stream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                converted = chat_completion_json_to_responses(
                    json.loads(raw_payload),
                    model_config.model_id,
                    estimate_value_tokens(body.get("input")),
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
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                log(f"codex upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')}")
                for event, data in iter_sse_lines(response):
                    kind, parsed = extract_text_delta(event, data)
                    if kind == "delta" and parsed:
                        text = filter_thinking_text_delta(parsed.get("text", ""), thinking_state)
                        if text:
                            output_text_parts.append(text)
                    elif kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta") and parsed:
                        merge_tool_call_payloads(tool_calls, parsed)
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

        output_text = "".join(output_text_parts)
        output_text, pseudo_tool_calls = parse_pseudo_function_calls(output_text, body.get("tools"))
        if pseudo_tool_calls:
            for pseudo_tool_call in pseudo_tool_calls:
                merge_tool_call_payloads(tool_calls, pseudo_tool_call)
        if not output_text and not tool_calls:
            retry_payload = dict(upstream_payload)
            retry_payload["stream"] = False
            try:
                with open_upstream(retry_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                fallback_payload = json.loads(raw_payload)
                output_text = response_text_from_upstream_json(fallback_payload)
                if not output_text and isinstance(fallback_payload.get("choices"), list):
                    converted = chat_completion_json_to_responses(
                        fallback_payload,
                        model_config.model_id,
                        estimate_value_tokens(body.get("input")),
                        retry_payload.get("tools") if isinstance(retry_payload.get("tools"), list) else None,
                    )
                    output_text = responses_json_output_text(converted.get("output", []))
                log(f"codex empty stream fallback model={model_config.model_id} chars={len(output_text)}")
            except Exception as exc:
                log(f"codex empty stream fallback failed model={model_config.model_id} error={exc}")
        if not output_text and not tool_calls:
            emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": "Upstream completed without assistant text or tool calls"}}})
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            return
        output: List[Dict[str, Any]] = []
        if output_text:
            emit("response.output_item.added", {"output_index": 0, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
            emit("response.content_part.added", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
            emit("response.output_text.delta", {"item_id": message_id, "output_index": 0, "content_index": 0, "delta": output_text})
            emit("response.output_text.done", {"item_id": message_id, "output_index": 0, "content_index": 0, "text": output_text})
            text_item = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output_text}]}
            emit("response.content_part.done", {"item_id": message_id, "output_index": 0, "content_index": 0, "part": text_item["content"][0]})
            emit("response.output_item.done", {"output_index": 0, "item": text_item})
            output.append(text_item)
        for offset, tool_call in enumerate(tool_calls):
            item = codex_function_call_item(tool_call, offset)
            output_index = len(output)
            emit("response.output_item.added", {"output_index": output_index, "item": dict(item, status="in_progress")})
            emit("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": output_index, "delta": item["arguments"]})
            emit("response.function_call_arguments.done", {"item_id": item["id"], "output_index": output_index, "arguments": item["arguments"]})
            emit("response.output_item.done", {"output_index": output_index, "item": item})
            output.append(item)
        completed = responses_completed_payload(request_id, model_config.model_id, output, estimate_value_tokens(body.get("input")), output_text)
        if done_payload and isinstance(done_payload.get("response"), dict):
            completed["usage"] = done_payload["response"].get("usage") or completed["usage"]
        emit("response.completed", {"response": completed})
        write_data_sse(self, "[DONE]")
        self.close_connection = True

    def handle_responses_non_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        if model_config.api_format == "chat_completions":
            upstream_payload["stream"] = False
            try:
                with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                payload = chat_completion_json_to_responses(
                    json.loads(raw_payload),
                    model_config.model_id,
                    estimate_value_tokens(body.get("input")),
                    upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
                )
                send_json(self, 200, payload)
                return
            except urllib.error.HTTPError as exc:
                send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
                return
            except Exception as exc:
                send_json(self, 502, responses_error_payload(f"Upstream response error: {exc}"))
                return
        upstream_payload["stream"] = False
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                raw_payload = response.read().decode("utf-8", errors="replace")
            send_json(self, 200, json.loads(raw_payload))
            return
        except urllib.error.HTTPError as exc:
            message = upstream_error_message(exc)
            log(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:500]}")
            send_json(self, 502, {"type": "error", "error": {"type": "api_error", "message": message}})
            return
        except Exception as exc:
            log(f"non-stream responses fallback model={model_config.model_id} error={exc}")
        upstream_payload["stream"] = True
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        done_payload: Optional[Dict[str, Any]] = None
        thinking_state: Dict[str, Any] = {"in_thinking": False}
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
        if output_text:
            output.append({"id": response_output_item_id(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output_text}]})
        for offset, tool_call in enumerate(tool_calls):
            output.append(codex_function_call_item(tool_call, offset))
        payload = responses_completed_payload(response_id(), model_config.model_id, output, estimate_value_tokens(body.get("input")), output_text)
        if done_payload and isinstance(done_payload.get("response"), dict):
            payload["usage"] = done_payload["response"].get("usage") or payload["usage"]
        send_json(self, 200, payload)

    def handle_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        message_id = anthropic_message_id()
        model = model_config.model_id
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
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

        output_text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        delta_count = 0
        text_block_started = False
        text_block_stopped = False
        done_payload: Optional[Dict[str, Any]] = None
        thinking_state: Dict[str, Any] = {"in_thinking": False}
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
                        if not text_block_started:
                            write_sse(self, "content_block_start", {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            })
                            text_block_started = True
                        output_text_parts.append(text)
                        delta_count += 1
                        write_sse(self, "content_block_delta", {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": text},
                        })
                    elif kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta") and parsed:
                        merge_tool_call_payloads(tool_calls, parsed)
                    elif kind == "error":
                        write_sse(self, "error", {
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
            write_sse(self, "error", {
                "type": "error",
                "error": {"type": "api_error", "message": message},
            })
            self.close_connection = True
            return
        except Exception as exc:
            log(f"upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
            write_sse(self, "error", {
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream connection error: {exc}"},
            })
            self.close_connection = True
            return

        output_text = "".join(output_text_parts)
        if text_block_started and not text_block_stopped:
            write_sse(self, "content_block_stop", {"type": "content_block_stop", "index": 0})
            text_block_stopped = True
        next_index = 1 if text_block_started else 0
        for offset, tool_call in enumerate(tool_calls):
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
        log(f"response done model={model_config.model_id} deltas={delta_count} chars={len(output_text)} tools={len(tool_calls)}")
        write_sse(self, "message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": max(1, len(output_text) // 4) if output_text else 0},
        })
        write_sse(self, "message_stop", {"type": "message_stop"})
        self.close_connection = True

    def handle_non_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        upstream_payload["stream"] = False
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                raw_payload = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw_payload)
            if isinstance(payload.get("output"), list):
                send_json(self, 200, responses_json_to_anthropic_message(payload, model_config))
                return
            if isinstance(payload.get("choices"), list):
                converted = chat_completion_json_to_responses(
                    payload,
                    model_config.model_id,
                    estimate_value_tokens(body.get("messages")),
                    upstream_payload.get("tools") if isinstance(upstream_payload.get("tools"), list) else None,
                )
                send_json(self, 200, responses_json_to_anthropic_message(converted, model_config))
                return
            raise ValueError("Responses JSON missing output list")
        except urllib.error.HTTPError as exc:
            send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
            return
        except Exception as exc:
            log(f"codex non-stream responses fallback model={model_config.model_id} error={exc}")
        upstream_payload["stream"] = True
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        done_payload: Optional[Dict[str, Any]] = None
        thinking_state: Dict[str, Any] = {"in_thinking": False}
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
            "usage": {"input_tokens": 0, "output_tokens": max(1, len(text) // 4) if text else 0},
        })

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self.client_address[0]} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Anthropic Messages to OpenAI Responses proxy")
    config = load_config()
    parser.add_argument("--host", default=os.getenv("HOST", config.host))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", str(config.port))))
    args = parser.parse_args()

    global ACTIVE_CONFIG
    ACTIVE_CONFIG = config
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
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
