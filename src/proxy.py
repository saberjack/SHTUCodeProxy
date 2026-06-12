#!/usr/bin/env python3
"""SHTUCodeProxy — HTTP 代理服务模块

职责: HTTP 服务器、请求路由、鉴权、流式/非流式响应处理、main 入口

依赖: logger (日志), transformer (协议转换), config_store (配置)
"""

from __future__ import annotations

import argparse
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
import ssl
import ipaddress
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 日志模块
from logger import (
    log, log_error, log_info, log_debug,
    now_ms, _orjson_dumps, _orjson_dumps_str, _orjson_loads,
    _HAS_ORJSON, json_dumps_compact, usage_cache_debug,
    register_active_config,
)

# 转换模块
from transformer import (
    # 常量
    VISIBLE_TEXT_TRUNCATE_LIMIT, VISIBLE_TEXT_SAMPLE_CHARS,
    DEFAULT_UPSTREAM_URL, DEFAULT_MODEL, ANTHROPIC_VERSION,
    # DSML/PSEUDO 正则
    DSML_OPEN_RE, DSML_CLOSE_RE, DSML_TOOL_CALLS_CLOSE_RE,
    DSML_OPEN_PREFIXES, DSML_TOOL_CALLS_CLOSE_PREFIXES,
    PSEUDO_FUNCTION_RE, PSEUDO_PARAM_RE, PSEUDO_CALL_RE,
    PSEUDO_ATTR_RE, PSEUDO_READ_FILE_RE, PSEUDO_EXEC_COMMAND_RE,
    PSEUDO_COMMAND_RE, PSEUDO_INVOKE_RE, PSEUDO_TOOL_RE,
    PSEUDO_SHELL_RE, PSEUDO_TOOL_INVOKE_RE, PSEUDO_PARAMETER_RE,
    PSEUDO_TOOL_RESULTS_RE, PSEUDO_TOOL_CALL_PLAN_RE,
    # 工具调用
    strip_tags, request_stream_enabled, _text_contains_any,
    is_claude_auto_classifier_request, parse_pseudo_attributes,
    shell_command_argv, is_direct_shell_executable,
    shell_join_command_parts, command_text_from_arguments,
    command_tool_schema, adapt_tool_arguments_for_schema,
    best_tool_name, coerce_pseudo_arguments, parse_pseudo_function_calls,
    # 内容/转换
    normalize_content_part, image_url_from_part, file_chat_part_from_part,
    audio_chat_part_from_part, content_part_to_chat_part,
    content_to_chat_content, content_to_responses_content,
    anthropic_content_to_text,
    # DSML/Thinking
    normalize_dsml_marker, partial_dsml_marker_start,
    # Token
    estimate_text_tokens, estimate_value_tokens, estimate_anthropic_input_tokens,
    # Cache
    copy_cache_metadata, has_cache_metadata, auto_cache_enabled,
    set_default_cache_control, mark_content_cache_boundary,
    apply_auto_cache_control_to_tools, apply_auto_cache_control_to_chat_payload,
    apply_auto_cache_control_to_responses_payload, apply_auto_cache_control,
    # Content modality
    content_modalities, direct_content_modalities, media_string_modalities,
    content_has_multimodal, unsupported_modalities, tool_modalities,
    filter_tools_for_model, sanitized_tool_choice_for_model,
    placeholder_content_part, sanitized_content_for_model,
    sanitized_anthropic_body_for_model, sanitized_responses_body_for_model,
    sanitized_upstream_value_for_model, _strip_cache_control,
    sanitized_upstream_payload_for_model,
    anthropic_current_user_modalities, responses_current_user_modalities,
    unsupported_modalities_message,
    # Thinking
    strip_thinking_markup, strip_markdown_json_fence,
    extract_balanced_json, quote_unquoted_json_keys, parse_json_like_object,
    tool_result_content_to_text, escape_tool_result_attr,
    anthropic_tool_results_visible_text,
    # Anthropic tools
    anthropic_tools_to_chat_tools, anthropic_tools_to_responses_tools,
    anthropic_tool_choice_to_openai, anthropic_tool_choice_to_responses,
    # Anthropic conversion
    split_anthropic_content, anthropic_message_to_chat_content,
    anthropic_message_to_chat_messages, anthropic_message_to_responses_items,
    anthropic_messages_to_responses, anthropic_messages_to_chat_completions,
    anthropic_messages_to_upstream,
    # Responses conversion
    responses_request_to_upstream, responses_content_to_text,
    responses_content_to_chat_content, responses_tool_to_chat_tool,
    responses_tool_choice_to_chat, enable_chat_stream_usage,
    responses_usage_from_chat_usage,
    # Truncation
    _truncate_visible_text,
    responses_request_to_chat_completions, responses_request_to_model_upstream,
    needs_conversion, clamp_max_tokens_in_body,
    # Upstream
    normalize_upstream_url, upstream_error_message, open_upstream, iter_sse_lines,
    # SSE events
    chat_tool_call_payloads, tool_call_kind_from_payloads, extract_text_delta,
    # Tool arguments
    parse_tool_arguments, tool_arguments_json, codex_function_call_item,
    filter_thinking_text_delta,
    # Payload
    stop_reason_from_done, compact_jsonish_outside_strings,
    is_cumulative_tool_argument_snapshot, merge_tool_argument_delta,
    merge_tool_call, merge_tool_call_payloads,
    openai_tool_names, normalize_tool_call_name_for_tools,
    chat_completion_json_to_responses,
    # IDs & payloads
    anthropic_message_id, thinking_requested,
    inject_redacted_thinking_to_content, inject_redacted_thinking_to_responses_output,
    strip_encrypted_content_from_reasoning, strip_encrypted_content_from_output,
    _REDACTED_THINKING_DATA,
    response_id, response_output_item_id, responses_usage,
    responses_completed_payload, response_text_from_upstream_json,
    responses_error_payload, anthropic_error_message_payload,
    responses_unsupported_modalities_payload, responses_json_output_text,
    responses_json_to_anthropic_message, extract_anthropic_usage,
    _codex_emit_recovered_response,
)

from platform_utils import app_dir
from config_store import (
    CLAUDE_MODEL_ALIASES, AppConfig, ModelConfig,
    config_path, load_config,
)

# 模块级配置缓存，由 main() 初始化
ACTIVE_CONFIG: Optional[AppConfig] = None


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
    # 性能优化: 使用 orjson 解析 (快 2-10x)
    return _orjson_loads(raw)


class _BodyTooLargeError(Exception):
    """Raised by read_json_body when Content-Length exceeds the limit."""
    pass


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    # 性能优化: orjson 默认输出 UTF-8 且 ensure_ascii=False
    data = _orjson_dumps(payload)
    try:
        handler.send_response(status)
        handler.send_header("content-type", "application/json; charset=utf-8")
        handler.send_header("content-length", str(len(data)))
        handler.send_header("access-control-allow-origin", "*")
        handler.end_headers()
        handler.wfile.write(data)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        pass  # 客户端已断开，静默忽略


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
    # 性能优化: 使用 orjson.dumps 替代 json.dumps (2-10x faster)
    payload = f"event: {event}\ndata: {_orjson_dumps_str(data)}\n\n"
    try:
        handler.wfile.write(payload.encode("utf-8"))
        handler.wfile.flush()
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        pass  # 客户端已断开，静默忽略


def write_sse_batch(handler, events: List[Tuple[str, Any]]) -> None:
    """性能优化: 批量写入多个 SSE 事件，只 flush 一次.

    WHY: 每次 write_sse 都触发一次 encode + write + flush (3次系统调用).
    在 send_anthropic_text_stream 等固定序列场景，6 个事件可以合并为
    1 次 write + 1 次 flush，减少 5 次系统调用 (约 10-30% 延迟降低).
    """
    buf: List[str] = []
    for event, data in events:
        buf.append(f"event: {event}\ndata: {_orjson_dumps_str(data)}\n\n")
    try:
        handler.wfile.write("".join(buf).encode("utf-8"))
        handler.wfile.flush()
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        pass  # 客户端已断开，静默忽略


def write_data_sse(handler: BaseHTTPRequestHandler, data: str) -> None:
    try:
        handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        pass  # 客户端已断开，静默忽略


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


def _ip_match(client_addr: "ipaddress.IPv4Address | ipaddress.IPv6Address", rules: List[str]) -> bool:
    """检查 client_addr 是否匹配规则列表中的任意一条（单 IP 或 CIDR 网段）。"""
    for rule in rules:
        try:
            if "/" in rule:
                if client_addr in ipaddress.ip_network(rule, strict=False):
                    return True
            else:
                if client_addr == ipaddress.ip_address(rule):
                    return True
        except ValueError:
            log_error(f"ip rule: invalid rule '{rule}', skipping")
            continue
    return False



class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "shtu-claude-proxy/0.1"
    protocol_version = "HTTP/1.1"

    def route_path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def check_auth(self) -> bool:
        """检查连接密钥。返回 True 表示放行，False 表示已发送 401 响应。"""
        auth_key = getattr(current_config(), "auth_key", "")
        if not auth_key:
            return True
        # 检查 Authorization: Bearer <key>
        auth_header = self.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token == auth_key:
                return True
        # 检查 x-api-key: <key>
        api_key = self.headers.get("x-api-key", "")
        if api_key.strip() == auth_key:
            return True
        # 认证失败
        log_error(f"auth rejected from {self.client_address[0]}")
        send_json(self, 401, {
            "type": "error",
            "error": {"type": "authentication_error", "message": "Invalid or missing API key. Set auth_key in config.json and provide via Authorization: Bearer <key> or x-api-key header."},
        })
        return False

    def check_ip(self) -> bool:
        """检查客户端 IP 访问权限。返回 True 表示放行，False 表示已发送 403 响应。

        优先级: 黑名单 (denied_ips) > 白名单 (allowed_ips)
        - 黑名单命中 → 403 拒绝（无论白名单如何）
        - 白名单非空且不命中 → 403 拒绝
        - 白名单为空 → 放行（仅受黑名单限制）
        - 均为空 → 不限制（向后兼容）

        支持单 IP 和 CIDR 网段 (e.g. "192.168.1.0/24")。
        """
        config = current_config()
        denied = getattr(config, "denied_ips", [])
        allowed = getattr(config, "allowed_ips", [])

        # 快速路径: 两个列表都为空，不做任何限制
        if not denied and not allowed:
            return True

        client_ip = self.client_address[0]
        try:
            client_addr = ipaddress.ip_address(client_ip)
        except ValueError:
            log_error(f"ip rejected: invalid client address {client_ip}")
            send_json(self, 403, {
                "type": "error",
                "error": {"type": "forbidden_error", "message": "Access denied."},
            })
            return False

        # 黑名单检查 (优先级最高)
        if denied and _ip_match(client_addr, denied):
            log_error(f"ip denied: {client_ip} in blacklist")
            send_json(self, 403, {
                "type": "error",
                "error": {"type": "forbidden_error", "message": "Access denied."},
            })
            return False

        # 白名单检查
        if allowed and not _ip_match(client_addr, allowed):
            log_error(f"ip rejected: {client_ip} not in whitelist")
            send_json(self, 403, {
                "type": "error",
                "error": {"type": "forbidden_error", "message": "Access denied."},
            })
            return False

        return True

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.send_header("access-control-allow-headers", "*")
        self.end_headers()

    def do_HEAD(self) -> None:
        if not self.check_ip():
            return
        if not self.check_auth():
            return
        path = self.route_path()
        if path in ("/", "/health", "/v1"):
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:
        if not self.check_ip():
            return
        if not self.check_auth():
            return
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
        if not self.check_ip():
            return
        if not self.check_auth():
            return
        route_path = self.route_path()
        log_info("do_POST path={} raw_path={}".format(route_path, self.path))
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
            if not auth_token:
                send_json(self, 500, {"type": "error", "error": {"type": "authentication_error", "message": f"No API key configured for model {model_config.model_id}"}})
                return
            # WHY: 非 genaiapi 上游 (如 api.anthropic.com) 本身支持 Anthropic 原生格式,
            # 无需转换, 直接透传. 仅保留 max_tokens 裁剪.
            if not needs_conversion(upstream_url):
                self._handle_passthrough(body, auth_token, upstream_url, timeout, model_config)
                return
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
                log_debug(f"degraded unsupported modalities model={model_config.model_id} modalities={','.join(sorted(unsupported))} stream={stream}")
            body_for_upstream = sanitized_anthropic_body_for_model(body, model_config)
            body["_thinking_requested"] = body_for_upstream.get("_thinking_requested", False)
            if not auth_token:
                send_json(self, 500, {"type": "error", "error": {"type": "authentication_error", "message": f"No API key configured for model {model_config.model_id}"}})
                return

            upstream_payload = anthropic_messages_to_upstream(body_for_upstream, model_config, fallback_model, upstream_model, config.default_stream)
            upstream_payload = sanitized_upstream_payload_for_model(upstream_payload, model_config)
            auto_cache_marks = apply_auto_cache_control(upstream_payload) if model_config.api_format != "chat_completions" else 0
            log_debug(
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
            log_error(traceback.format_exc())
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
            if not auth_token:
                send_json(self, 500, responses_error_payload(f"No API key configured for model {model_config.model_id}", "authentication_error"))
                return
            # WHY: 非 genaiapi 上游无需转换, 直接透传
            if not needs_conversion(upstream_url):
                self._handle_passthrough(body, auth_token, upstream_url, timeout, model_config)
                return
            # WHY: When model has Thinking enabled but client didn't send thinking param,
            # inject it so reasoning content is emitted as reasoning items (collapsible
            # in Codex) instead of plain text.
            if not isinstance(body.get("thinking"), dict) and not is_claude_auto_classifier_request(body) and (getattr(model_config, "enable_thinking", False) or getattr(model_config, "supports_reasoning", False)):
                body["thinking"] = {"type": "enabled", "budget_tokens": max(1, (body.get("max_output_tokens") or body.get("max_tokens") or 16384) - 1)}
            body_for_upstream = body
            unsupported = unsupported_modalities(model_config, responses_current_user_modalities(body))
            if unsupported:
                log_debug(f"degraded unsupported Responses modalities model={model_config.model_id} modalities={','.join(sorted(unsupported))} stream={stream}")
            body_for_upstream = sanitized_responses_body_for_model(body, model_config)
            body["_thinking_requested"] = body_for_upstream.get("_thinking_requested", False)
            if not auth_token:
                send_json(self, 500, responses_error_payload(f"No API key configured for model {model_config.model_id}", "authentication_error"))
                return
            upstream_payload = responses_request_to_model_upstream(body_for_upstream, model_config, fallback_model, upstream_model, config.default_stream)
            upstream_payload = sanitized_upstream_payload_for_model(upstream_payload, model_config)
            auto_cache_marks = apply_auto_cache_control(upstream_payload) if model_config.api_format != "chat_completions" else 0
            log_debug(
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
            log_error(traceback.format_exc())
            if not self.wfile.closed:
                try:
                    send_json(self, 500, responses_error_payload(str(exc)))
                except Exception:
                    pass

    def send_anthropic_text_stream(self, model_config: ModelConfig, text: str, _thinking_requested: bool = False) -> None:
        message_id = anthropic_message_id()
        send_sse_headers(self)
        # 性能优化: 6 个固定事件合并为 1 次 write + 1 次 flush
        _thinking_block_index = 0
        write_sse_batch(self, [
            ("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model_config.model_id, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}),
            ("content_block_start", {"type": "content_block_start", "index": _thinking_block_index, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": _thinking_block_index, "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": _thinking_block_index}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": max(1, len(text) // 4)}}),
            ("message_stop", {"type": "message_stop"}),
        ])
        self.close_connection = True

    def send_responses_text_stream(self, model_config: ModelConfig, text: str, input_tokens: int = 0) -> None:
        request_id = response_id()
        item_id = response_output_item_id()
        send_sse_headers(self)
        # 性能优化: 10 个固定事件合并为 1 次 write + 1 次 flush
        output = [{"id": item_id, "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]}]
        write_sse_batch(self, [
            ("response.created", {"type": "response.created", "sequence_number": 0, "response": {"id": request_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model_config.model_id, "output": []}}),
            ("response.in_progress", {"type": "response.in_progress", "sequence_number": 1, "response": {"id": request_id, "status": "in_progress"}}),
            ("response.output_item.added", {"type": "response.output_item.added", "sequence_number": 2, "output_index": 0, "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}}),
            ("response.content_part.added", {"type": "response.content_part.added", "sequence_number": 3, "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}}),
            ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 4, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text}),
            ("response.output_text.done", {"type": "response.output_text.done", "sequence_number": 5, "item_id": item_id, "output_index": 0, "content_index": 0, "text": text}),
            ("response.content_part.done", {"type": "response.content_part.done", "sequence_number": 6, "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text}}),
            ("response.output_item.done", {"type": "response.output_item.done", "sequence_number": 7, "output_index": 0, "item": output[0]}),
            ("response.completed", {"type": "response.completed", "sequence_number": 8, "response": responses_completed_payload(request_id, model_config.model_id, output, input_tokens, text)}),
        ])
        self.close_connection = True

    def _handle_passthrough(self, body: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        """透传模式: 不做任何格式转换, 直接转发原始请求并中继原始响应.

        WHY: 对于非 genaiapi.shanghaitech.edu.cn 的上游 (如 api.anthropic.com),
        上游本身就支持 Anthropic/Responses 原生格式, 无需转换.
        仅保留 max_tokens 裁剪以防止 "exceeds context length" 错误.
        """
        stream = request_stream_enabled(body, model_config.default_stream if hasattr(model_config, 'default_stream') else True)
        # 仅做 max_tokens 裁剪, 不做格式转换
        payload = clamp_max_tokens_in_body(dict(body), model_config)
        # 移除 proxy 内部标记
        payload.pop("_thinking_requested", None)
        payload.pop("_reasoning_enabled", None)
        payload["stream"] = stream

        # 根据 route 决定上游 URL 路径
        route = self.route_path()
        if route in ("/v1/messages", "/messages"):
            # Anthropic Messages: 直接转发到上游 /v1/messages
            target_url = upstream_url.rstrip("/")
            if not target_url.endswith("/v1/messages"):
                target_url = target_url.rstrip("/") + "/v1/messages"
        else:
            # Responses: 直接转发到上游 /v1/responses
            target_url = upstream_url.rstrip("/")
            if not target_url.endswith("/v1/responses"):
                target_url = target_url.rstrip("/") + "/v1/responses"

        log_info(f"passthrough model={body.get('model')} url={target_url} stream={stream}")

        data = _orjson_dumps(payload)
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
            "authorization": f"Bearer {auth_token}",
        }
        request = urllib.request.Request(target_url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if stream and _response_is_sse(response):
                    # 直接中继 SSE 流
                    send_sse_headers(self)
                    while True:
                        try:
                            raw = response.readline()
                        except socket.timeout as exc:
                            raise TimeoutError("Timed out while waiting for upstream SSE data") from exc
                        if not raw:
                            break
                        self.wfile.write(raw)
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    # 非流式: 直接中继 JSON
                    raw = response.read()
                    self.send_response(200)
                    self.send_header("content-type", "application/json; charset=utf-8")
                    self.send_header("content-length", str(len(raw)))
                    self.send_header("access-control-allow-origin", "*")
                    self.end_headers()
                    self.wfile.write(raw)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            log_error(f"passthrough upstream error model={model_config.model_id} status={exc.code} body={error_body[:200]}")
            try:
                error_data = _orjson_loads(error_body)
                send_json(self, exc.code, error_data)
            except Exception:
                send_json(self, exc.code, {"type": "error", "error": {"type": "api_error", "message": error_body[:500]}})
        except Exception as exc:
            log_error(f"passthrough error model={model_config.model_id} error={exc}")
            if not self.wfile.closed:
                try:
                    send_json(self, 502, {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
                except Exception:
                    pass

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
            log_info(f"codex compact request model={body.get('model')} keys={sorted(body.keys())}")
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
                data = _orjson_dumps(body)
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
                            result = _orjson_loads(raw)
                            log_info(f"codex compact done model={model_config.model_id}")
                            send_json(self, 200, result)
                except urllib.error.HTTPError as exc:
                    error_body = exc.read().decode("utf-8", errors="replace")
                    log_error(f"codex compact upstream error model={model_config.model_id} status={exc.code}")
                    self._handle_compact_via_chat(body, model_config, auth_token, upstream_url, timeout, stream, fallback_model, upstream_model, config)
                except Exception as exc:
                    log_error(f"codex compact upstream exception: {exc}")
                    self._handle_compact_via_chat(body, model_config, auth_token, upstream_url, timeout, stream, fallback_model, upstream_model, config)
            else:
                # Upstream is chat_completions - convert and forward
                self._handle_compact_via_chat(body, model_config, auth_token, upstream_url, timeout, stream, fallback_model, upstream_model, config)
        except _BodyTooLargeError:
            return
        except Exception as exc:
            log_error(traceback.format_exc())
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
                    _upstream_raw = _orjson_loads(raw_payload)
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
                    log_info(f"codex compact stream_bridge done model={model_config.model_id} chars={len(output_text)}")
                    return
                except Exception as exc:
                    log_error(f"codex compact stream_bridge error: {exc}")

            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, "chat_completions") as response:
                raw = response.read().decode("utf-8", errors="replace")
            upstream_result = _orjson_loads(raw)
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

            log_info(f"codex compact chat done model={model_config.model_id} stream={stream}")
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
            log_error(f"codex compact chat error: {traceback.format_exc()}")
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
                    write_sse(self, event, _orjson_loads(data) if data.startswith("{") else data)
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            log_info(f"codex compact sse relay done model={model_config.model_id}")
        except Exception as exc:
            log_error(f"codex compact sse relay error: {exc}")
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
                    _orjson_loads(raw_payload),
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
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                log_info(f"upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')}")
                for event, data in iter_sse_lines(response):
                    kind, parsed = extract_text_delta(event, data)
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
            log_error(f"codex upstream http error model={model_config.model_id} status={exc.code} body={message[:200]}")
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
                    chat_json = _orjson_loads(raw_payload)
                    converted = chat_completion_json_to_responses(
                        chat_json,
                        model_config.model_id,
                        estimate_value_tokens(body.get("messages")),
                        fallback_payload.get("tools") if isinstance(fallback_payload.get("tools"), list) else None,
                    )
                    if converted.get("output"):
                        log_error(f"codex stream http error recovered via non-stream model={model_config.model_id}")
                        # Emit the recovered response in Responses SSE format
                        _codex_emit_recovered_response(emit, request_id, message_id, converted, model_config, thinking_requested(body))
                        write_data_sse(self, "[DONE]")
                        return
                except Exception as fallback_exc:
                    log_error(f"codex stream http error non-stream fallback failed model={model_config.model_id} error={fallback_exc}")
            emit("response.failed", {"response": {"id": request_id, "status": "failed", "error": {"type": "api_error", "message": message}}})
            write_data_sse(self, "[DONE]")
            self.close_connection = True
            return
        except Exception as exc:
            log_error(f"codex upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
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
            # WHY: Check for upstream error in done_payload and retry response
            # before falling back to a generic error message.
            codex_upstream_error = ""
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
            retry_payload = dict(upstream_payload)
            retry_payload["stream"] = False
            retry_payload.pop("stream_options", None)
            try:
                with open_upstream(retry_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                fallback_payload = _orjson_loads(raw_payload)
                # WHY: Check if non-stream retry returned an error response
                if isinstance(fallback_payload, dict):
                    err = fallback_payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        codex_upstream_error = codex_upstream_error or err["message"]
                        log_error(f"codex stream retry returned error model={model_config.model_id} error={err['message'][:200]}")
                    elif fallback_payload.get("object") == "error" and fallback_payload.get("message"):
                        codex_upstream_error = codex_upstream_error or fallback_payload["message"]
                        log_error(f"codex stream retry returned error model={model_config.model_id} error={fallback_payload['message'][:200]}")
                    elif fallback_payload.get("detail") and isinstance(fallback_payload.get("detail"), str):
                        codex_upstream_error = codex_upstream_error or fallback_payload["detail"]
                        log_error(f"codex stream retry returned error model={model_config.model_id} detail={fallback_payload['detail'][:200]}")
                log_info(f"codex empty stream fallback model={model_config.model_id}")
                output_text = response_text_from_upstream_json(fallback_payload)
                if not output_text and isinstance(fallback_payload.get("choices"), list):
                    converted = chat_completion_json_to_responses(
                        fallback_payload,
                        model_config.model_id,
                        estimate_anthropic_input_tokens(body),
                        retry_payload.get("tools") if isinstance(retry_payload.get("tools"), list) else None,
                    )
                    output_text = responses_json_output_text(converted.get("output", []))
                log_info(f"codex empty stream fallback model={model_config.model_id} chars={len(output_text)}")
            except urllib.error.HTTPError as retry_http_exc:
                retry_body = retry_http_exc.read().decode("utf-8", errors="replace")
                retry_msg = f"HTTP {retry_http_exc.code}: {retry_body[:300]}"
                codex_upstream_error = codex_upstream_error or retry_msg
                log_error(f"codex stream retry http error model={model_config.model_id} {retry_msg}")
            except Exception as exc:
                log_error(f"codex empty stream fallback failed model={model_config.model_id} error={exc}")
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
        log_info(f"response done model={model_config.model_id} chars={len(output_text)} tools={len(tool_calls)}{usage_cache_debug(completed.get('usage'))}")
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
                _upstream_raw = _orjson_loads(raw_payload)
                if isinstance(_upstream_raw, dict) and _upstream_raw.get("success") is False:
                    upstream_msg = _upstream_raw.get("message", "") or _upstream_raw.get("error", "") or _orjson_dumps_str(_upstream_raw)[:200]
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
                    log_error(f"WARNING empty responses non-stream model={model_config.model_id} attempt={_attempt+1}/2")
                    time.sleep(0.5 * (_attempt + 1))
                    try:
                        with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as retry_resp:
                            raw_payload = retry_resp.read().decode("utf-8", errors="replace")
                        _upstream_raw = _orjson_loads(raw_payload)
                        if isinstance(_upstream_raw, dict) and _upstream_raw.get("success") is False:
                            upstream_msg = _upstream_raw.get("message", "") or _orjson_dumps_str(_upstream_raw)[:200]
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
                        log_error(f"WARNING responses non-stream retry failed model={model_config.model_id} error={retry_exc}")
                if payload.get("output"):
                    log_info(f"response done model={model_config.model_id} non_stream=true{usage_cache_debug(payload.get('usage'))}")
                    send_json(self, 200, payload)
                    return
                log_error(f"WARNING responses non-stream empty after retries, falling back to streaming model={model_config.model_id}")
                # Fall through to streaming fallback below
            except urllib.error.HTTPError as exc:
                send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
                return
            except ValueError as exc:
                send_json(self, 502, responses_error_payload(str(exc)))
                return
            except Exception as exc:
                log_error(f"responses non-stream fallback model={model_config.model_id} error={exc}")
                # Fall through to streaming fallback
        else:
            upstream_payload["stream"] = False
            upstream_payload.pop("stream_options", None)
            try:
                with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                    raw_payload = response.read().decode("utf-8", errors="replace")
                payload = _orjson_loads(raw_payload)
                if isinstance(payload, dict) and payload.get("success") is False:
                    upstream_msg = payload.get("message", "") or payload.get("error", "") or _orjson_dumps_str(payload)[:200]
                    log_error(f"upstream error model={model_config.model_id} message={upstream_msg}")
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
                log_error(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:200]}")
                send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
                return
            except Exception as exc:
                log_error(f"non-stream responses fallback model={model_config.model_id} error={exc}")
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
                        send_json(self, 502, responses_error_payload(_orjson_dumps_str(parsed)))
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
                    chat_json = _orjson_loads(raw_payload)
                    if isinstance(chat_json, dict) and chat_json.get("success") is False:
                        upstream_msg = chat_json.get("message", "") or _orjson_dumps_str(chat_json)[:200]
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
                        log_error(f"WARNING stream_bridge empty model={model_config.model_id} attempt={_bridge_attempt+1}/3")
                        if _bridge_attempt < 2:
                            time.sleep(0.5 * (_bridge_attempt + 1))
                            continue
                    else:
                        converted = None
                        break
                    # Empty after all retries - break and fall through to real streaming
                    if not converted.get("output"):
                        log_error(f"WARNING stream_bridge empty after retries, falling back to real streaming model={model_config.model_id}")
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
                # WHY: 将 max_tokens 映射为 end_turn, 让 Claude Code 继续处理已有输出
                if stop_reason == "max_tokens":
                    stop_reason = "end_turn"
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
                log_error(f"qwen bridge http error model={model_config.model_id} status={exc.code} body={message[:200]}")
                self._send_anthropic_stream_error(message, text_block_started, text_block_stopped)
                return
            except ValueError as exc:
                if "stream_bridge empty" in str(exc):
                    log_info(f"stream_bridge empty, falling back to real streaming model={model_config.model_id}")
                    # Fall through to real streaming below - do NOT return
                else:
                    log_error(f"qwen bridge error model={model_config.model_id} error={exc}")
                    self._send_anthropic_stream_error(f"Qwen bridge error: {exc}", text_block_started, text_block_stopped)
                    return
            except Exception as exc:
                log_error(f"qwen bridge error model={model_config.model_id} error={exc}")
                self._send_anthropic_stream_error(f"Qwen bridge error: {exc}", text_block_started, text_block_stopped)
                return

        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                log_info(f"upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')}")
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
                            _err_msg = _orjson_dumps_str(parsed)[:500]
                        stream_error_message = _err_msg
                        log_error(f"upstream stream error model={model_config.model_id} error={_err_msg[:200]}")
                        self._send_anthropic_stream_error(_err_msg, text_block_started, text_block_stopped)
                        return
                    elif kind == "done":
                        done_payload = parsed
                        break
        except urllib.error.HTTPError as exc:
            message = upstream_error_message(exc)
            log_error(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:200]}")
            if model_config.api_format == "chat_completions" and not text_block_started and not tool_calls:
                fallback_payload = dict(upstream_payload)
                fallback_payload["stream"] = False
                fallback_payload.pop("stream_options", None)
                try:
                    with open_upstream(fallback_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                        raw_payload = response.read().decode("utf-8", errors="replace")
                    chat_json = _orjson_loads(raw_payload)
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
                        log_error(f"stream http error recovered via non-stream model={model_config.model_id}")
                        self._emit_anthropic_message_as_stream(anthropic_msg, _thinking_block_index)
                        return
                except Exception as fallback_exc:
                    log_error(f"stream http error non-stream fallback failed model={model_config.model_id} error={fallback_exc}")
            self._send_anthropic_stream_error(message, text_block_started, text_block_stopped)
            return
        except Exception as exc:
            log_error(f"upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
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
                    retry_json = _orjson_loads(raw_retry)
                    # WHY: Check if non-stream retry returned an error response
                    # (e.g. context overflow). If so, extract the error message
                    # instead of silently producing an empty response.
                    if isinstance(retry_json, dict):
                        err = retry_json.get("error")
                        if isinstance(err, dict) and err.get("message"):
                            upstream_error_message_text = upstream_error_message_text or err["message"]
                            log_error(f"anthropic stream retry returned error model={model_config.model_id} error={err['message'][:200]}")
                        elif retry_json.get("object") == "error" and retry_json.get("message"):
                            upstream_error_message_text = upstream_error_message_text or retry_json["message"]
                            log_error(f"anthropic stream retry returned error model={model_config.model_id} error={retry_json['message'][:200]}")
                        elif retry_json.get("detail") and isinstance(retry_json.get("detail"), str):
                            upstream_error_message_text = upstream_error_message_text or retry_json["detail"]
                            log_error(f"anthropic stream retry returned error model={model_config.model_id} detail={retry_json['detail'][:200]}")
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
                    log_error(f"anthropic stream retry http error model={model_config.model_id} {retry_msg}")
                except Exception as retry_exc:
                    log_error(f"anthropic stream non-stream retry failed model={model_config.model_id} error={retry_exc}")
            # WHY: If both stream and retry failed and we have an upstream error
            # message, send it to the client instead of an empty response.
            # This ensures the user sees the actual error (e.g. context overflow)
            # instead of a silent empty reply.
            if not output_text and not tool_calls and upstream_error_message_text:
                log_error(f"anthropic empty stream with upstream error model={model_config.model_id} error={upstream_error_message_text[:200]}")
                self._send_anthropic_stream_error(upstream_error_message_text, text_block_started, text_block_stopped)
                return
            log_info(f"anthropic empty stream fallback model={model_config.model_id} chars={len(output_text)}")
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
        log_info(f"response done model={model_config.model_id} deltas={delta_count} chars={len(output_text)} tools={len(tool_calls)}{usage_cache_debug(response_usage)}")
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
        _sr = anthropic_msg.get("stop_reason", "end_turn")
        if _sr == "max_tokens":
            _sr = "end_turn"  # WHY: 映射为 end_turn 让 Claude Code 继续处理已有输出
        write_sse(self, "message_delta", {"type": "message_delta", "delta": {"stop_reason": _sr, "stop_sequence": None}, "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}})
        write_sse(self, "message_stop", {"type": "message_stop"})
        self.close_connection = True

    def handle_non_streaming(self, body: Dict[str, Any], upstream_payload: Dict[str, Any], auth_token: str, upstream_url: str, timeout: int, model_config: ModelConfig) -> None:
        upstream_payload["stream"] = False
        upstream_payload.pop("stream_options", None)
        try:
            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as response:
                raw_payload = response.read().decode("utf-8", errors="replace")
            payload = _orjson_loads(raw_payload)
            if thinking_requested(body): payload["_thinking_requested"] = True
            if isinstance(payload, dict) and payload.get("success") is False:
                upstream_msg = payload.get("message", "") or payload.get("error", "") or _orjson_dumps_str(payload)[:200]
                log_error(f"upstream error model={model_config.model_id} message={upstream_msg}")
                raise ValueError(f"Upstream rejected: {upstream_msg}")
            if isinstance(payload.get("output"), list):
                log_info(f"response done model={model_config.model_id} non_stream=true{usage_cache_debug(payload.get('usage'))}")
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
                    log_error(f"WARNING empty non-stream response model={model_config.model_id} attempt={_attempt+1}/3")
                    if _attempt < 2:
                        time.sleep(0.5 * (_attempt + 1))
                        try:
                            with open_upstream(upstream_payload, auth_token, upstream_url, timeout, model_config.api_format) as retry_resp:
                                raw_payload = retry_resp.read().decode("utf-8", errors="replace")
                            payload = _orjson_loads(raw_payload)
                        except Exception as retry_exc:
                            log_error(f"WARNING non-stream retry failed model={model_config.model_id} error={retry_exc}")
                if converted.get("output"):
                    if thinking_requested(body): converted["_thinking_requested"] = True
                    anthropic_msg = responses_json_to_anthropic_message(converted, model_config)
                    log_info(f"response done model={model_config.model_id} non_stream=true{usage_cache_debug(converted.get('usage'))}")
                    send_json(self, 200, anthropic_msg)
                    return
                log_error(f"WARNING non-stream empty after retries, falling back to streaming model={model_config.model_id}")
                # Fall through to streaming fallback below
            else:
                raise ValueError("Responses JSON missing output list")
        except urllib.error.HTTPError as exc:
            send_json(self, 502, responses_error_payload(upstream_error_message(exc)))
            return
        except Exception as exc:
            log_error(f"codex non-stream responses fallback model={model_config.model_id} error={exc}")
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
                log_info(f"upstream connected model={model_config.model_id} format={model_config.api_format} status={getattr(response, 'status', 'unknown')}")
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
                            "error": {"type": "api_error", "message": _orjson_dumps_str(parsed)},
                        })
                        return
                    elif kind == "done":
                        done_payload = parsed
                        break
        except urllib.error.HTTPError as exc:
            message = upstream_error_message(exc)
            log_error(f"upstream http error model={model_config.model_id} status={exc.code} body={message[:200]}")
            send_json(self, 502, {"type": "error", "error": {"type": "api_error", "message": message}})
            return
        except Exception as exc:
            log_error(f"upstream connection error model={model_config.model_id} format={model_config.api_format} error={exc}")
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
        log_info(f"{self.client_address[0]} {format % args}")

    def handle(self) -> None:
        """覆写 handle, 捕获客户端断开导致的 ConnectionResetError/BrokenPipeError。

        WHY: Claude Code 长时间等待用户选择工具时, 客户端可能超时断开,
        导致大量 ConnectionResetError 刷屏。这些是正常行为, 只需静默处理。
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
    register_active_config(lambda: ACTIVE_CONFIG)
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.daemon_threads = True

    # 可选 HTTPS: 当 ssl_cert 和 ssl_key 均配置且文件存在时，自动启用 TLS
    use_ssl = bool(config.ssl_cert and config.ssl_key)
    if use_ssl:
        import pathlib
        cert_path = pathlib.Path(config.ssl_cert)
        key_path = pathlib.Path(config.ssl_key)
        if not cert_path.is_file():
            log_error(f"ERROR: SSL cert file not found: {cert_path}")
            sys.exit(1)
        if not key_path.is_file():
            log_error(f"ERROR: SSL key file not found: {key_path}")
            sys.exit(1)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        except ssl.SSLError as e:
            log_error(f"ERROR: Failed to load SSL cert/key: {e}")
            sys.exit(1)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        log_info(f"Listening on https://{args.host}:{args.port}")
    else:
        log_info(f"Listening on http://{args.host}:{args.port}")

    # IP 白名单/黑名单提示
    if config.denied_ips:
        log_info(f"IP blacklist enabled: {', '.join(config.denied_ips)}")
    else:
        log_info("IP blacklist: disabled")
    if config.allowed_ips:
        log_info(f"IP whitelist enabled: {', '.join(config.allowed_ips)}")
    else:
        log_info("IP whitelist: disabled (allow all)")

    log_info(f"Configured models: {', '.join(model.model_id for model in config.models)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_info("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
