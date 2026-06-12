"""SHTUCodeProxy — 协议转换模块

职责: 所有协议转换、内容规范化、工具调用解析、token 估算、
      cache 控制、thinking 处理、SSE 事件解析、上游请求构造

被 proxy 模块调用，不依赖 proxy 模块（无循环依赖）。
"""

from __future__ import annotations

import ast
import copy
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
from collections import OrderedDict
from urllib.parse import urlparse
from typing import Any, Dict, Iterable, List, Optional, Tuple

from logger import (
    log, log_error, log_info, log_debug,
    now_ms, _orjson_dumps, _orjson_dumps_str, _orjson_loads,
    _HAS_ORJSON, json_dumps_compact, usage_cache_debug,
)
from platform_utils import app_dir
from config_store import (
    CLAUDE_MODEL_ALIASES, AppConfig, ModelConfig,
    config_path, load_config,
)

# 性能优化: 集中可调参数
VISIBLE_TEXT_TRUNCATE_LIMIT = int(os.getenv("SHTU_VISIBLE_TRUNCATE", "2000"))
VISIBLE_TEXT_SAMPLE_CHARS = max(100, VISIBLE_TEXT_TRUNCATE_LIMIT // 2)

DEFAULT_UPSTREAM_URL = "https://genaiapi.shanghaitech.edu.cn/api/v1/response"
DEFAULT_MODEL = "GPT-5.5"
ANTHROPIC_VERSION = "2023-06-01"

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
                arguments = {"command": f"Get-Content -Path {_orjson_dumps_str(path)} -Raw"}
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
    return copy_cache_metadata(part, {"type": "input_text", "text": _orjson_dumps_str(part)})


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
            text = _orjson_dumps_str(part)
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
                f"{_orjson_dumps_str(part.get('input', {}))}"
            )
        elif part_type == "tool_result":
            text_parts.append(f"[tool_result id={part.get('tool_use_id', '')}] {part.get('content', '')}")
        elif part_type == "image":
            text_parts.append("[image]")
        elif part_type in ("thinking", "redacted_thinking"):
            pass
        else:
            text_parts.append(_orjson_dumps_str(part))
    return "\n".join(part for part in text_parts if part)



def normalize_dsml_marker(text: str) -> str:
    return text.replace("｜", "|").lower()


def partial_dsml_marker_start(text: str, position: int, prefixes: Tuple[str, ...]) -> int:
    for index in range(len(text) - 1, position - 1, -1):
        suffix = normalize_dsml_marker(text[index:])
        if any(prefix.startswith(suffix) and suffix != prefix for prefix in prefixes):
            return index
    return -1


# 性能优化: CJK Unicode 范围检测 (采样法，避免全量遍历)
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Extension A
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
)


def _cjk_ratio_sample(text: str, sample_size: int = 512) -> float:
    """采样估算 CJK 字符占比 (0.0~1.0), 避免全量遍历长文本."""
    if len(text) <= sample_size:
        cjk = sum(1 for c in text if any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES))
        return cjk / len(text) if text else 0.0
    # 采样头、中、尾各 sample_size//3 个字符
    step = max(1, len(text) // (sample_size // 3))
    indices = list(range(0, len(text), step))[:sample_size]
    cjk = sum(1 for i in indices if any(lo <= ord(text[i]) <= hi for lo, hi in _CJK_RANGES))
    return cjk / len(indices)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    # 性能优化: CJK 字符约 1.5 字/token, ASCII 约 4 字符/token
    # 原版 chars/4 对纯中文偏高 ~100%, 导致 max_tokens 不必要地缩减
    ratio = _cjk_ratio_sample(text)
    # CJK: ~1.5 char/token (即 2/3 token/char), non-CJK: ~4 char/token
    # 加权: tokens ≈ len * (ratio * 2/3 + (1-ratio) * 1/4)
    # 简化: tokens ≈ (cjk_chars * 2 + non_cjk_chars) / 4
    cjk_chars = int(len(text) * ratio)
    non_cjk_chars = len(text) - cjk_chars
    return max(1, (cjk_chars * 2 + non_cjk_chars + 3) // 4)


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
    text = f"{name}\n{description}\n{_orjson_dumps_str(parameters) if isinstance(parameters, dict) else parameters}"
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


def sanitized_upstream_payload_for_model(payload: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    result = sanitized_upstream_value_for_model(payload, model_config) if isinstance(payload, dict) else payload
    # WHY: cache_control is Anthropic-specific; no upstream API supports it.
    # Leaving it in causes empty streams (GPT-5.5 responses) or errors.
    if isinstance(result, dict):
        result = _strip_cache_control(result)
    # WHY: When model supports reasoning and thinking was requested, inject
    # chat_template_kwargs so vLLM-based upstreams enable reasoning mode.
    # Only send for chat_completions format; Responses API (GPT-5.5 etc.) rejects it.
    if isinstance(result, dict) and result.pop("_reasoning_enabled", False):
        if model_config.api_format == "chat_completions":
            result["chat_template_kwargs"] = {"enable_thinking": True}

    # 自动裁剪 max_tokens 避免 "exceeds context length" 错误
    # WHY: Claude Code 请求 max_tokens=32000, 但如果 input 已占 96001 tokens,
    # deepseek-pro (128k context) 会拒绝: 96001 + 32000 = 128001 > 128000
    # 解决: 估算 input tokens, 裁剪 max_tokens = max_context_tokens - input_tokens - 1
    if isinstance(result, dict) and getattr(model_config, "max_context_tokens", 0) > 0:
        max_ctx = model_config.max_context_tokens
        max_out_key = "max_tokens" if "max_tokens" in result else ("max_output_tokens" if "max_output_tokens" in result else None)
        if max_out_key:
            requested_max = result.get(max_out_key, 0)
            if isinstance(requested_max, int) and requested_max > 0:
                # 估算当前 input tokens
                est_input = estimate_anthropic_input_tokens(result) if result.get("messages") else estimate_value_tokens(result.get("input"))
                # 保留 1 token 安全边距
                allowed_max = max(1, max_ctx - est_input - 1)
                if requested_max > allowed_max:
                    old_max = requested_max
                    result[max_out_key] = allowed_max
                    log_info(f"clamped max_tokens: {old_max} -> {allowed_max} (context={max_ctx}, est_input={est_input})")

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
                return _orjson_loads(value)
            except ValueError:
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


# 性能优化: 内部 helper (避免函数调用开销)
_escape_attr = escape_tool_result_attr


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
            text_parts.append(_orjson_dumps_str(part))
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
            # 性能优化: 用 join 替代 += (避免 O(n²) 字符串复制)
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else str(prev.get("content", ""))
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            prev["content"] = "\n\n".join(part for part in (prev_text, curr_text) if part).strip()
        elif prev_role == curr_role and curr_role == "assistant" and not prev.get("tool_calls") and not msg.get("tool_calls"):
            # 性能优化: 用 join 替代 +=
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else ""
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
            prev["content"] = "\n\n".join(part for part in (prev_text, curr_text) if part).strip()
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
    # 性能优化: 浅层 copy 替代 deepcopy
    # 理由: 本函数只修改顶层 key (model, stream), 不递归修改嵌套值
    # 浅层 copy 避免对嵌套结构 (messages, tools 等) 的 O(n) 复制
    payload: Dict[str, Any] = {k: v for k, v in body.items()}
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
                texts.append(_orjson_dumps_str(part))
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


def _truncate_visible_text(text: str) -> str:
    """性能优化: 截断 visible_text 减少 token 消耗.

    WHY: 工具结果同时存放在 role="tool" 和 visible_text 中. 不截断会让
    上下文翻倍增长. 截断后, 完整内容仍在 tool 消息中, visible_text
    只是给模型的提示.
    """
    if not text or len(text) <= VISIBLE_TEXT_TRUNCATE_LIMIT:
        return text
    sample = VISIBLE_TEXT_SAMPLE_CHARS
    return f"{text[:sample]}\n\n... (省略 {len(text) - 2 * sample} 字符) ...\n\n{text[-sample:]}"


def responses_request_to_chat_completions(body: Dict[str, Any], fallback_model: str, upstream_model: Optional[str] = None, default_stream: bool = True) -> Dict[str, Any]:
    """性能优化版本: 单遍状态机 + OrderedDict + 延迟 visible_text 生成.

    主要优化:
    - pending_tool_calls 改用 OrderedDict (Python 3.7+ dict 已保序, 但显式 OrderedDict 更清晰)
    - deferred_visible 改为延迟生成: 收集 (call_id, output_text) 而非完整 visible_text
    - 连续 user 消息合并使用 join 替代 += (避免 O(n²) 字符串复制)
    - visible_text 在 flush 时才生成, 且自动截断
    """
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
        # 性能优化: 改用 OrderedDict 显式表示插入序, 避免后续 list() 复制
        pending_tool_calls: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        # 性能优化: 延迟生成 visible_text, 仅保存原始 (call_id, content)
        # 在 flush 时一次性构建 visible_text 并截断
        deferred_visible_blocks: List[Tuple[str, str]] = []

        def _flush_visible() -> None:
            """将所有 deferred visible blocks 合并为一个 user 消息.

            性能优化: 用 join 替代 += ; 截断避免 token 爆炸.
            """
            if not deferred_visible_blocks:
                return
            # 一次性构建所有 tool_result XML, 避免多次字符串拼接
            xml_blocks: List[str] = []
            for call_id, content in deferred_visible_blocks:
                attrs = [f'tool_use_id="{_escape_attr(call_id)}"']
                xml_blocks.append(
                    f"<tool_result {' '.join(attrs)}>\n{content}\n</tool_result>"
                )
            # 性能优化: 截断 (避免上下文翻倍增长)
            full_text = "<tool_results>\n" + "\n".join(xml_blocks) + "\n</tool_results>"
            truncated = _truncate_visible_text(full_text)
            messages.append({"role": "user", "content": truncated})
            deferred_visible_blocks.clear()

        def _flush_pending() -> None:
            """Flush 累积的 tool calls 为一个 assistant 消息."""
            if not pending_tool_calls:
                return
            # 性能优化: 直接 list() 构造, OrderedDict.values() 已是 view, list() 一次
            tool_calls_list = list(pending_tool_calls.values())
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": tool_calls_list,
            })
            pending_tool_calls.clear()

        for item in input_items:
            if not isinstance(item, dict):
                _flush_visible()
                _flush_pending()
                messages.append({"role": "user", "content": str(item)})
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                # assistant tool_call: 先 flush 所有 deferred user 消息
                _flush_visible()
                call_id = item.get("call_id") or item.get("id") or f"call_proxy_{now_ms()}"
                arguments = item.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json_dumps_compact(arguments)
                # 性能优化: OrderedDict 保留插入序, 多次赋值无 list() 复制
                pending_tool_calls[call_id] = {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": item.get("name") or "tool", "arguments": arguments},
                }
                continue
            if item_type in ("function_call_output", "tool_result"):
                # tool result: 先 flush assistant tool_calls
                _flush_pending()
                call_id = item.get("call_id") or item.get("id") or ""
                output_text = responses_content_to_text(item.get("output") or item.get("content"))
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output_text})
                # 性能优化: 仅保存原始 (call_id, content), 不立即生成 visible_text
                # 截断在 _flush_visible 时统一处理
                if output_text:
                    deferred_visible_blocks.append((call_id, output_text))
                continue
            # 其他类型: 先 flush 所有累积
            _flush_visible()
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
        _flush_visible()
        _flush_pending()
    else:
        messages.append({"role": "user", "content": ""})

    # 性能优化: 合并连续同角色消息, 使用 join 替代 += 字符串拼接
    # 原实现: prev["content"] = (prev_text + "\n\n" + curr_text).strip()
    # 问题: 当 n 个连续 user 消息时, 每次 += 都触发 O(n) 字符串复制
    # 解决: 先收集到 blocks, 最后用 "".join 一次性拼接
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
        if curr_role == "system" and has_non_system and first_system_idx is not None:
            sys_msg = merged[first_system_idx]
            # 性能优化: 用 join 替代 +=
            sys_text = sys_msg.get("content", "") if isinstance(sys_msg.get("content"), str) else str(sys_msg.get("content", ""))
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            sys_msg["content"] = "\n\n".join(part for part in (sys_text, curr_text) if part).strip()
            continue
        if curr_role == "system" and has_non_system and first_system_idx is None:
            # No prior system message; convert to user role
            msg = dict(msg)
            msg["role"] = "user"
            curr_role = "user"
        if prev_role == curr_role and curr_role == "user":
            # 性能优化: 用 join 替代 += (避免 O(n²) 字符串复制)
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else str(prev.get("content", ""))
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else str(msg.get("content", ""))
            prev["content"] = "\n\n".join(part for part in (prev_text, curr_text) if part).strip()
        elif prev_role == curr_role and curr_role == "assistant" and not prev.get("tool_calls") and not msg.get("tool_calls"):
            # 性能优化: 用 join 替代 +=
            prev_text = prev.get("content", "") if isinstance(prev.get("content"), str) else ""
            curr_text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
            prev["content"] = "\n\n".join(part for part in (prev_text, curr_text) if part).strip()
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
        # 性能优化: 用 final_messages (合并后) 替代 messages (原始)
        final_messages = [{"role": "system", "content": system_content}] + final_messages

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


def needs_conversion(base_url: str) -> bool:
    """判断上游是否需要格式转换.

    WHY: genaiapi.shanghaitech.edu.cn 只接受 Chat Completions / Responses API 格式,
    需要从 Anthropic Messages 转换. 其他上游 (如 api.anthropic.com) 本身就
    支持 Anthropic 原生格式, 无需转换, 直接透传即可.

    Returns:
        True = 需要转换 (genaiapi 上游)
        False = 直接透传 (原生 Anthropic/Responses 格式)
    """
    return "genaiapi.shanghaitech.edu.cn" in base_url


def clamp_max_tokens_in_body(body: Dict[str, Any], model_config: ModelConfig) -> Dict[str, Any]:
    """仅对 body 执行 max_tokens 裁剪, 不做任何格式转换.

    WHY: 透传模式下仍需防止 "exceeds context length" 错误.
    """
    if not isinstance(body, dict) or getattr(model_config, "max_context_tokens", 0) <= 0:
        return body
    max_ctx = model_config.max_context_tokens
    max_out_key = None
    for key in ("max_tokens", "max_output_tokens"):
        if key in body and isinstance(body.get(key), int) and body[key] > 0:
            max_out_key = key
            break
    if max_out_key:
        est_input = estimate_anthropic_input_tokens(body) if body.get("messages") else estimate_value_tokens(body.get("input"))
        allowed_max = max(1, max_ctx - est_input - 1)
        if body[max_out_key] > allowed_max:
            old_max = body[max_out_key]
            body[max_out_key] = allowed_max
            log_info(f"passthrough clamped {max_out_key}: {old_max} -> {allowed_max} (context={max_ctx}, est_input={est_input})")
    return body


def normalize_upstream_url(upstream_url: str, api_format: str) -> str:
    url = upstream_url.strip()
    if api_format == "chat_completions" and not url.rstrip("/").endswith("/chat/completions"):
        return url.rstrip("/") + "/chat/completions"
    return url


def upstream_error_message(exc: urllib.error.HTTPError) -> str:
    error_body = exc.read().decode("utf-8", errors="replace")
    return f"Upstream HTTP {exc.code}: {error_body}"


def open_upstream(payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, api_format: str = "responses") -> urllib.response.addinfourl:
    data = _orjson_dumps(payload)
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
        obj = _orjson_loads(data)
    except ValueError:
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
        # WHY: 映射为 "end_turn" 而非 "max_tokens"。
        # Claude Code 看到 stop_reason="max_tokens" 会认为输出中断而停止工作，
        # 但实际上模型已经产出了有效内容（只是因为 max_tokens 裁剪被截断）。
        # 伪装为 "end_turn" 让 Claude Code 正常处理已输出的内容继续工作。
        return "end_turn"
    # Responses API format: the done payload may be the full response object
    # or wrapped in {"response": ...}; check both for status and function_call output
    response_obj = parsed.get("response") if isinstance(parsed.get("response"), dict) else parsed
    status = response_obj.get("status") if isinstance(response_obj, dict) else None
    if status in ("incomplete", "cancelled"):
        # 同理: 伪装为 end_turn, 让 Claude Code 继续处理已有输出
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
        message = payload["error"].get("message") or _orjson_dumps_str(payload["error"])
        raise ValueError(str(message))
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"Upstream response missing choices: {_orjson_dumps_str(payload)[:1000]}")
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
