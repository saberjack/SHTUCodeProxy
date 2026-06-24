from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import cli
from safe_io import restore_latest_backup, snapshot_original_file
from config_store import AppConfig, MODEL_ENV_KEYS, ModelConfig, load_config, save_config, seed_builtin_model_routes
from platform_utils import launch_script_text, portable_claude_path, portable_settings_path
from proxy import (
    anthropic_messages_to_chat_completions,
    anthropic_messages_to_responses,
    estimate_anthropic_input_tokens,
    extract_text_delta,
    filter_thinking_text_delta,
    merge_tool_call,
    merge_tool_call_payloads,
    codex_function_call_item,
    parse_tool_arguments,
    parse_pseudo_function_calls,
    responses_completed_payload,
    responses_request_to_chat_completions,
    responses_json_to_anthropic_message,
    responses_request_to_upstream,
    anthropic_current_user_modalities,
    anthropic_message_id,
    responses_current_user_modalities,
    response_id,
    chat_completion_json_to_responses,
    stop_reason_from_done,
    tool_arguments_json,
    unsupported_modalities,
    unsupported_modalities_message,
    sanitized_anthropic_body_for_model,
    sanitized_responses_body_for_model,
    sanitized_upstream_payload_for_model,
    usage_cache_debug,
    is_claude_auto_classifier_request,
    log,
    apply_auto_cache_control,
    has_cache_metadata,
)


EXPECTED_SHELL_PREFIX = ["powershell.exe", "-Command"] if os.name == "nt" else ["bash", "-lc"]


def make_config(tmpdir: Path) -> AppConfig:
    model = ModelConfig(
        name="Smoke Model",
        model_id="smoke-model",
        base_url="https://example.invalid/v1/responses",
        api_key="smoke-key",
        upstream_model="smoke-upstream",
        api_format="responses",
    )
    return AppConfig(
        host="127.0.0.1",
        port=18082,
        default_model_id="smoke-model",
        codex_model_id="smoke-model",
        codex_sandbox_mode="workspace-write",
        codex_approval_policy="never",
        codex_personality="default",
        codex_reasoning_effort="medium",
        model_env={key: "smoke-model" for key in MODEL_ENV_KEYS},
        timeout=30,
        claude_path="claude",
        claude_settings_path=str(tmpdir / ".claude" / "settings.json"),
        codex_config_path=str(tmpdir / ".codex" / "config.toml"),
        codex_auth_path=str(tmpdir / ".codex" / "auth.json"),
        default_stream=True,
        diagnostic_logging=False,
        update_check_enabled=True,
        update_check_interval_hours=24,
        update_include_prerelease=False,
        update_auto_download=False,
        models=[model],
    )


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def exercise_anthropic_stream_delta_usage_shape(tmpdir: Path) -> None:
    class FakeHandler:
        def __init__(self) -> None:
            self.events = []
            self.close_connection = False

    handler = FakeHandler()
    with patch("proxy.write_sse", lambda _handler, _event, payload: handler.events.append((_event, payload))):
        from proxy import ProxyHandler
        ProxyHandler._emit_anthropic_message_as_stream(handler, {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        })
    deltas = [payload for event, payload in handler.events if event == "message_delta"]
    assert_true(bool(deltas), "stream fallback should emit message_delta")
    assert_true("input_tokens" in deltas[-1]["usage"], "Anthropic stream delta usage must include input_tokens for Claude auto classifier")


def exercise_claude_auto_classifier_detection() -> None:
    body = {
        "model": "qwen-instruct",
        "system": "You are a security monitor for autonomous AI coding agents. Output <block>yes</block> or <block>no</block> during the Classification Process.",
        "messages": [{"role": "user", "content": "Bash echo hello"}],
    }
    assert_true(is_claude_auto_classifier_request(body), "auto safety classifier requests should skip injected thinking")
    regular = {"system": "You are helpful.", "tools": [{"name": "Bash"}], "messages": []}
    assert_true(not is_claude_auto_classifier_request(regular), "regular tool requests should keep existing thinking behavior")



def exercise_tool_call_translation() -> None:
    body = {
        "model": "smoke-model",
        "stream": True,
        "tool_choice": {"type": "auto"},
        "tools": [{
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }],
        "messages": [
            {"role": "user", "content": "Read README.md"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "README.md"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "hello"}
            ]},
        ],
    }

    chat_payload = anthropic_messages_to_chat_completions(body, "fallback", "upstream")
    assert_true("tools" in chat_payload, "chat payload missing tools")
    assert_true(chat_payload["tools"][0]["function"]["name"] == "read_file", "chat tool name mismatch")
    assert_true(any("tool_calls" in message for message in chat_payload["messages"]), "chat history missing tool_calls")
    assert_true(any(message.get("role") == "tool" for message in chat_payload["messages"]), "chat history missing tool result")
    assert_true(
        any(message.get("role") == "user" and "<tool_result" in message.get("content", "") and "hello" in message.get("content", "") for message in chat_payload["messages"]),
        "chat history missing visible tool result fallback",
    )

    responses_payload = anthropic_messages_to_responses(body, "fallback", "upstream")
    assert_true("tools" in responses_payload, "responses payload missing tools")
    assert_true(any(item.get("type") == "function_call" for item in responses_payload["input"]), "responses history missing function_call")
    assert_true(any(item.get("type") == "function_call_output" for item in responses_payload["input"]), "responses history missing function_call_output")

    kind, parsed = extract_text_delta(None, json.dumps({
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_01",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\":"},
                }]
            }
        }]
    }))
    assert_true(kind == "tool_call_delta" and parsed is not None, "chat tool call delta not detected")
    tool_calls = []
    merge_tool_call(tool_calls, parsed)
    merge_tool_call(tool_calls, {"index": 0, "arguments": "\"README.md\"}"})
    assert_true(tool_calls[0]["id"] == "call_01", "merged tool call id mismatch")
    assert_true(parse_tool_arguments(tool_calls[0]["arguments"])["path"] == "README.md", "tool arguments parse mismatch")
    assert_true(stop_reason_from_done({"finish_reason": "tool_calls"}, tool_calls) == "tool_use", "tool stop reason mismatch")

    kind, parsed = extract_text_delta("response.completed", json.dumps({
        "type": "response.completed",
        "response": responses_completed_payload("resp_done", "smoke-model", [{"id": "msg_done", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}], 1, "OK"),
    }))
    assert_true(kind == "done" and parsed is not None and parsed.get("response", {}).get("status") == "completed", "Responses completed event should end the stream without replaying already-emitted text")

    shell_item = codex_function_call_item({"name": "shell_exec", "arguments": '{"command":"Write-Output ok"}'})
    shell_args = json.loads(shell_item["arguments"])
    assert_true(shell_item["name"] == "shell", "Codex shell aliases should normalize to shell")
    assert_true(shell_args["command"] == [*EXPECTED_SHELL_PREFIX, "Write-Output ok"], "Codex shell command should use the current platform shell")
    echo_item = codex_function_call_item({"name": "shell", "arguments": {"command": ["echo", "sandbox ok"]}})
    echo_args = json.loads(echo_item["arguments"])
    assert_true(echo_args["command"][:2] == EXPECTED_SHELL_PREFIX, "bare shell commands should be wrapped through the current platform shell")
    with patch("proxy.os.name", "posix"):
        linux_shell_item = codex_function_call_item({"name": "shell_exec", "arguments": '{"command":"pwd"}'})
        linux_shell_args = json.loads(linux_shell_item["arguments"])
        assert_true(linux_shell_args["command"] == ["bash", "-lc", "pwd"], "Linux shell strings must not be wrapped with PowerShell")
    exec_item = codex_function_call_item({"name": "exec_command", "arguments": '{"cmd":"echo test"}'})
    exec_args = json.loads(exec_item["arguments"])
    assert_true(exec_item["name"] == "exec_command" and exec_args["cmd"] == "echo test", "Codex exec_command tool name and cmd argument must be preserved")


def exercise_cache_control_passthrough() -> None:
    cache_control = {"type": "ephemeral"}
    anthropic_body = {
        "model": "smoke-model",
        "system": [{"type": "text", "text": "stable system prompt", "cache_control": cache_control}],
        "tools": [{
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "cache_control": cache_control,
        }],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello", "cache_control": cache_control}]}],
    }

    responses_payload = anthropic_messages_to_responses(anthropic_body, "fallback", "upstream")
    assert_true(responses_payload["input"][0]["role"] == "developer", "cached Anthropic system blocks should remain in Responses input")
    assert_true(responses_payload["input"][0]["content"][0]["cache_control"] == cache_control, "Anthropic system cache_control should pass through to Responses")
    assert_true(responses_payload["input"][1]["content"][0]["cache_control"] == cache_control, "Anthropic message cache_control should pass through to Responses")
    assert_true(responses_payload["tools"][0]["cache_control"] == cache_control, "Anthropic tool cache_control should pass through to Responses")

    chat_payload = anthropic_messages_to_chat_completions(anthropic_body, "fallback", "upstream")
    assert_true(chat_payload["messages"][0]["content"][0]["cache_control"] == cache_control, "Anthropic system cache_control should pass through to Chat")
    assert_true(chat_payload["messages"][1]["content"][0]["cache_control"] == cache_control, "Anthropic message cache_control should pass through to Chat")
    assert_true(chat_payload["tools"][0]["cache_control"] == cache_control, "Anthropic tool cache_control should pass through to Chat")

    codex_body = {
        "model": "smoke-model",
        "input": [{"role": "developer", "content": [{"type": "input_text", "text": "stable instructions", "cache_control": cache_control}]}],
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}, "cache_control": cache_control}],
    }
    assert_true(responses_request_to_upstream(codex_body, "fallback", "upstream")["input"][0]["content"][0]["cache_control"] == cache_control, "Codex Responses cache_control should pass through unchanged")
    codex_chat_payload = responses_request_to_chat_completions(codex_body, "fallback", "upstream")
    assert_true(any(part.get("cache_control") == cache_control for part in codex_chat_payload["messages"][0]["content"]), "Codex developer cache_control should pass through to Chat")
    assert_true(codex_chat_payload["tools"][0]["cache_control"] == cache_control, "Codex tool cache_control should pass through to Chat")
    assert_true(codex_chat_payload["stream_options"]["include_usage"] is True, "Chat streaming should request usage chunks")

    tool_payload = {
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "tool output should stay a string"},
            {"role": "user", "content": "continue"},
        ]
    }
    apply_auto_cache_control(tool_payload)
    assert_true(isinstance(tool_payload["messages"][1]["content"], str), "auto cache must not convert Chat tool message content into an array")
    usage_kind, usage_parsed = extract_text_delta(None, json.dumps({"choices": [], "usage": {"prompt_tokens_details": {"cached_tokens": 34}}}))
    assert_true(usage_kind == "usage" and usage_parsed and usage_parsed["usage"]["prompt_tokens_details"]["cached_tokens"] == 34, "Chat usage-only chunks should be parsed")
    assert_true("cache_read_input_tokens" in usage_cache_debug({"cache_read_input_tokens": 12}), "Anthropic cache read usage should be logged")
    assert_true("details_cached_tokens" in usage_cache_debug({"input_tokens_details": {"cached_tokens": 34}}), "OpenAI cached token usage should be logged")

    # WHY: Codex Chat payload auto-cache marks tools but intentionally skips
    # string system content: GLM/DeepSeek reject array content on messages
    # and return HTTP 500.  Production never calls auto-cache on chat payloads
    # (the gate skips chat_completions format), so only tool marking is expected.
    codex_chat_without_cache = responses_request_to_chat_completions({
        "model": "glm-chat",
        "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": "stable developer prompt"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "first question"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "first answer"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "current question"}]},
        ],
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
    }, "fallback", "glm-chat")
    auto_marks = apply_auto_cache_control(codex_chat_without_cache)
    assert_true(auto_marks >= 1, "Codex Chat payload should mark tool definitions")
    assert_true(codex_chat_without_cache["tools"][0]["cache_control"] == cache_control, "auto cache should mark tool definitions")
    assert_true(not has_cache_metadata(codex_chat_without_cache["messages"][-1]), "auto cache should not mark the current user turn")
    # WHY: Codex Responses payload is the real production auto-cache path (the
    # gate runs auto-cache only for non-chat formats).  Developer content is a
    # list here, so cache boundaries apply to tools + developer content.
    codex_responses_without_cache = responses_request_to_upstream({
        "model": "GPT-5.5",
        "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": "stable developer prompt"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "first question"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "first answer"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "current question"}]},
        ],
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
    }, "fallback", "GPT-5.5")
    resp_marks = apply_auto_cache_control(codex_responses_without_cache)
    assert_true(resp_marks >= 2, "Codex Responses payload should get automatic cache boundaries")
    assert_true(codex_responses_without_cache["tools"][0]["cache_control"] == cache_control, "auto cache should mark Responses tool definitions")
    _dev_item = codex_responses_without_cache["input"][0]
    assert_true(any(part.get("cache_control") == cache_control for part in _dev_item["content"]), "auto cache should mark Responses developer content")
    assert_true(not has_cache_metadata(codex_responses_without_cache["input"][-1]), "auto cache should not mark the current Responses user turn")


def exercise_file_logging(tmpdir: Path) -> None:
    log_dir = tmpdir / "app-log"
    with patch("proxy.app_dir", return_value=log_dir):
        log("smoke file log entry")
    log_file = log_dir / "proxy.log"
    assert_true(log_file.exists() and "smoke file log entry" in log_file.read_text(encoding="utf-8"), "proxy.log should be written for GUI builds")


def exercise_chat_completion_json_to_responses() -> None:
    payload = chat_completion_json_to_responses({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "chatcmpl-tool-1",
                    "type": "function",
                    "function": {"name": "get_current_weather", "arguments": '{"location": "北京", "unit": "celsius"}'},
                }],
            },
        }],
    }, "qwen-instruct", 12)
    output = payload["output"]
    assert_true(len(output) == 1, "chat completion tool call should map to one Responses output")
    assert_true(output[0]["type"] == "function_call", "chat completion tool call should become Responses function_call")
    assert_true(output[0]["name"] == "get_current_weather", "function call name mismatch")
    assert_true(json.loads(output[0]["arguments"])["location"] == "北京", "function call arguments mismatch")

    try:
        chat_completion_json_to_responses({"error": {"message": "model not found"}}, "qwen-instruct", 1)
        raise AssertionError("upstream JSON error should not become an empty successful response")
    except ValueError:
        pass

    text, pseudo_calls = parse_pseudo_function_calls('Let me read it.\n<function><param name="command">Get-Content D:/litellm/eval_cases.json</param></function>', [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true("<function" not in text, "pseudo function markup should be stripped from visible text")
    assert_true(pseudo_calls[0]["name"] == "shell", "pseudo function should map command to shell tool")
    assert_true(json.loads(pseudo_calls[0]["arguments"])["command"][-1].startswith("Get-Content"), "pseudo function command argument mismatch")

    pseudo_payload = chat_completion_json_to_responses({
        "choices": [{"message": {"content": '<function><param name="command">pwd</param></function>'}}],
    }, "deepseek-chat", 1, [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true(pseudo_payload["output"][0]["type"] == "function_call", "pseudo function should become Responses function_call")
    assert_true(pseudo_payload["output"][0]["name"] == "shell", "pseudo function call name mismatch")

    call_text = '<function_calls><call type="tool" name="send_input" arguments="items: [{"path": "D:\\\\litellm\\\\eval_cases.json"}]"></call></function_calls>'
    _, call_style_calls = parse_pseudo_function_calls(call_text, [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true(call_style_calls[0]["name"] == "shell", "call-style pseudo function should map to shell")
    assert_true("Get-Content" in json.loads(call_style_calls[0]["arguments"])["command"][-1], "call-style pseudo function should become file read command")

    _, xml_calls = parse_pseudo_function_calls('<read_file path="D:\\litellm\\eval_cases.json" /><exec_command><command>pwd</command></exec_command>', [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true(len(xml_calls) == 2, "xml-style pseudo calls should be detected")
    assert_true("Get-Content" in json.loads(xml_calls[0]["arguments"])["command"][-1], "read_file pseudo tag should become shell file read")
    assert_true(json.loads(xml_calls[1]["arguments"])["command"][-1] == "pwd", "exec_command pseudo tag command mismatch")

    _, invoke_calls = parse_pseudo_function_calls('<Invoke><tool>shell</tool><command>Get-ChildItem</command></Invoke>', [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true(invoke_calls[0]["name"] == "shell", "invoke pseudo tag tool mismatch")
    assert_true(json.loads(invoke_calls[0]["arguments"])["command"][-1] == "Get-ChildItem", "invoke pseudo tag command mismatch")

    _, shell_calls = parse_pseudo_function_calls('<shell>cat "D:\\litellm\\eval_cases.json"</shell>', [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true(shell_calls[0]["name"] == "shell", "shell pseudo tag tool mismatch")
    assert_true(shell_calls[0]["arguments"], "shell pseudo tag arguments missing")

    cleaned, invoke2_calls = parse_pseudo_function_calls('<tool_invoke name="bash"><parameter name="command" string="true">pwd</parameter></tool_invoke><tool_results>fake</tool_results>', [{"type": "function", "name": "shell", "parameters": {"type": "object"}}])
    assert_true("tool_results" not in cleaned, "pseudo tool results should be stripped")
    assert_true(invoke2_calls[0]["name"] == "shell", "tool_invoke pseudo tag should map bash to shell")
    assert_true(json.loads(invoke2_calls[0]["arguments"])["command"][-1] == "pwd", "tool_invoke pseudo command mismatch")

    exec_tools = [{"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]
    _, exec_calls = parse_pseudo_function_calls('<exec_command><command>pwd</command></exec_command>', exec_tools)
    exec_call_args = json.loads(exec_calls[0]["arguments"])
    assert_true(exec_calls[0]["name"] == "exec_command" and exec_call_args == {"cmd": "pwd"}, "pseudo exec_command should use the real exec_command(cmd) schema")


def exercise_mixed_tool_result_ordering() -> None:
    body = {
        "model": "smoke-model",
        "stream": True,
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "README.md"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": [{"type": "text", "text": "file contents"}]},
                {"type": "text", "text": "Now summarize it."},
            ]},
        ],
    }

    chat_messages = anthropic_messages_to_chat_completions(body, "fallback", "upstream")["messages"]
    assert_true(chat_messages[1]["role"] == "tool", "chat tool result should immediately follow assistant tool call")
    assert_true(chat_messages[2]["role"] == "user", "chat user text should follow tool results")
    assert_true(chat_messages[1]["content"] == "file contents", "chat tool result content mismatch")
    assert_true("file contents" in chat_messages[2]["content"], "chat visible tool result should include tool content")
    assert_true("Now summarize it." in chat_messages[2]["content"], "chat visible tool result should preserve user text")

    response_items = anthropic_messages_to_responses(body, "fallback", "upstream")["input"]
    assert_true(response_items[1]["type"] == "function_call_output", "responses tool output should immediately follow function call")
    assert_true(response_items[2]["role"] == "user", "responses user text should follow tool output")


def exercise_model_suffix_routing() -> None:
    default_model = ModelConfig(
        name="Default GLM",
        model_id="chatglm",
        base_url="https://example.invalid/v1/chat/completions",
        api_key="key",
        upstream_model="glm-chat",
        api_format="chat_completions",
    )
    haiku_model = ModelConfig(
        name="Claude Haiku Alias",
        model_id="claude-haiku-4-5",
        base_url="https://example.invalid/v1/chat/completions",
        api_key="key",
        upstream_model="minimax",
        api_format="chat_completions",
    )
    sonnet_alias = ModelConfig(
        name="Claude Sonnet Alias",
        model_id="claude-sonnet-4-6",
        base_url="https://example.invalid/v1/chat/completions",
        api_key="key",
        upstream_model="deepseek-pro",
        api_format="chat_completions",
    )
    direct_deepseek = ModelConfig(
        name="Direct DeepSeek",
        model_id="deepseek-pro",
        base_url="https://example.invalid/v1/chat/completions",
        api_key="key",
        upstream_model="deepseek-pro",
        api_format="chat_completions",
    )
    config = AppConfig(
        host="127.0.0.1",
        port=18082,
        default_model_id="chatglm",
        codex_model_id="deepseek-pro",
        codex_sandbox_mode="workspace-write",
        codex_approval_policy="never",
        codex_personality="default",
        codex_reasoning_effort="medium",
        model_env={key: "chatglm" for key in MODEL_ENV_KEYS},
        timeout=30,
        claude_path="claude",
        claude_settings_path="/tmp/settings.json",
        codex_config_path="/tmp/codex-config.toml",
        codex_auth_path="/tmp/codex-auth.json",
        default_stream=True,
        diagnostic_logging=False,
        update_check_enabled=True,
        update_check_interval_hours=24,
        update_include_prerelease=False,
        update_auto_download=False,
        models=[default_model, haiku_model, sonnet_alias, direct_deepseek],
    )

    routed = config.find_model("claude-haiku-4-5-20251001")
    assert_true(routed.model_id == "claude-haiku-4-5", "dated Claude model ID should route to configured alias")
    routed_direct = config.find_model("deepseek-pro")
    assert_true(routed_direct.model_id == "deepseek-pro", "exact model ID should beat upstream model alias matches")


def exercise_deleted_model_route_cleanup(tmpdir: Path) -> None:
    config = make_config(tmpdir)
    deleted_model_id = "temp-model"
    config.models.append(ModelConfig(
        name="Temporary Model",
        model_id=deleted_model_id,
        base_url="https://example.invalid/v1/responses",
        api_key="",
        upstream_model=deleted_model_id,
        api_format="responses",
    ))
    config.default_model_id = deleted_model_id
    config.codex_model_id = deleted_model_id
    config.model_env = {key: deleted_model_id for key in MODEL_ENV_KEYS}
    config.models = [model for model in config.models if model.model_id != deleted_model_id]
    remaining_ids = {model.model_id for model in config.models}
    fallback = config.models[0].model_id
    config.model_env = {
        key: value if value in remaining_ids else fallback
        for key, value in config.model_env.items()
    }
    if config.default_model_id not in remaining_ids:
        config.default_model_id = fallback
    if config.codex_model_id not in remaining_ids:
        config.codex_model_id = fallback
    save_config(config, tmpdir / "config.json")
    loaded = AppConfig.from_dict(json.loads((tmpdir / "config.json").read_text(encoding="utf-8")))
    assert_true(all(model.model_id != deleted_model_id for model in loaded.models), "deleted model should not persist")
    assert_true(loaded.default_model_id != deleted_model_id, "default model should not reference deleted model")
    assert_true(loaded.codex_model_id != deleted_model_id, "codex model should not reference deleted model")
    assert_true(all(value != deleted_model_id for value in loaded.model_env.values()), "model env should not reference deleted model")


def exercise_pyqt_model_management_regressions() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication, QPushButton
    import pyqt_gui

    app = QApplication.instance() or QApplication([])
    models = [
        ModelConfig("GLM", "glm-chat", "https://example.invalid/v1/responses", "key", "glm-chat", "responses"),
        ModelConfig("Qwen", "qwen-chat", "https://example.invalid/v1/chat/completions", "key", "qwen-chat", "chat_completions"),
        ModelConfig("DeepSeek", "deepseek-chat", "https://example.invalid/v1/chat/completions", "key", "deepseek-chat", "chat_completions"),
    ]
    config = AppConfig(
        host="127.0.0.1",
        port=18082,
        default_model_id="glm-chat",
        codex_model_id="glm-chat",
        codex_sandbox_mode="workspace-write",
        codex_approval_policy="never",
        codex_personality="default",
        codex_reasoning_effort="medium",
        model_env={key: "glm-chat" for key in MODEL_ENV_KEYS},
        timeout=30,
        claude_path="claude",
        claude_settings_path="settings.json",
        codex_config_path="config.toml",
        codex_auth_path="auth.json",
        default_stream=True,
        diagnostic_logging=False,
        update_check_enabled=True,
        update_check_interval_hours=24,
        update_include_prerelease=False,
        update_auto_download=False,
        models=models,
    )
    with (
        patch("pyqt_gui.load_config", return_value=config),
        patch("pyqt_gui.save_config"),
        patch.object(pyqt_gui.IosProxyApp, "error", lambda self, title, message: None),
        patch.object(pyqt_gui.IosProxyApp, "warning", lambda self, title, message: None),
        patch.object(pyqt_gui.IosProxyApp, "info", lambda self, title, message: None),
    ):
        window = pyqt_gui.IosProxyApp()
        window.model_env_combos["ANTHROPIC_MODEL"].setCurrentText("qwen-chat")
        window.model_env_combos["ANTHROPIC_DEFAULT_HAIKU_MODEL"].setCurrentText("deepseek-chat")
        window.codex_model_combo.setCurrentText("deepseek-chat")
        assert_true(window.save(), "save should succeed after route changes")
        assert_true(window.config_data.model_env["ANTHROPIC_MODEL"] == "qwen-chat", "main route should persist")
        assert_true(window.config_data.model_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-chat", "haiku route should persist")
        assert_true(window.config_data.codex_model_id == "deepseek-chat", "codex route should persist")
        save_button = next(button for button in window.findChildren(QPushButton) if button.text() == "Save Config")
        save_button.click()
        assert_true(window.config_data.default_model_id == "qwen-chat", "button click save should not receive checked state as route override")
        assert_true(window.save(), "repeat save should be idempotent")
        assert_true(window.config_data.default_model_id == "qwen-chat", "repeat save should not revert default model")

        previous_ids = set(window.model_ids())
        window.new_model()
        created_ids = set(window.model_ids()) - previous_ids
        assert_true(len(created_ids) == 1, "new model should create exactly one unique ID")
        new_model = next(model for model in window.config_data.models if model.model_id in created_ids)
        assert_true(new_model.api_format == "responses", "new model should default to responses API format")

        window.model_table.selectRow(1)
        window.model_id_edit.setText("deepseek-chat")
        assert_true(not window.apply_model(), "duplicate model ID should be rejected")
        assert_true(len({model.model_id for model in window.config_data.models}) == len(window.config_data.models), "model IDs should remain unique")

        window.model_id_edit.setText("qwen-plus")
        window.model_env_combos["ANTHROPIC_MODEL"].setCurrentText("qwen-chat")
        assert_true(window.apply_model(), "model rename should succeed")
        assert_true(window.config_data.model_env["ANTHROPIC_MODEL"] == "qwen-plus", "renamed routed model should update routes")

        window.delete_model()
        assert_true("qwen-plus" not in {model.model_id for model in window.config_data.models}, "deleted model should be removed")
        remaining_ids = {model.model_id for model in window.config_data.models}
        assert_true(all(value in remaining_ids for value in window.config_data.model_env.values()), "routes should not reference deleted models")
        assert_true(window.config_data.codex_model_id in remaining_ids, "codex model should not reference deleted models")
        window.close()


def exercise_count_tokens_estimate() -> None:
    body = {
        "system": "You are Claude Code.",
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "Run echo."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "echo hello"}}
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "hello\n"}
            ]},
        ],
    }
    assert_true(estimate_anthropic_input_tokens(body) > 10, "count_tokens estimate should be non-zero")


def exercise_codex_responses_passthrough() -> None:
    function_call_item = {
        "id": "fc_01",
        "type": "function_call",
        "call_id": "call_01",
        "name": "shell",
        "arguments": "{\"command\":\"pwd\"}",
    }
    function_output_item = {
        "type": "function_call_output",
        "call_id": "call_01",
        "output": [{"type": "output_text", "text": "C:/repo"}],
    }
    body = {
        "model": "codex-local",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            function_call_item,
            function_output_item,
            {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
        "stream": True,
        "tools": [
            {"type": "function", "name": "shell", "parameters": {"type": "object"}},
            {"type": "function", "name": "read_file", "parameters": {"type": "object"}},
        ],
    }
    payload = responses_request_to_upstream(body, "fallback", "upstream-local")
    assert_true(payload["model"] == "upstream-local", "codex responses route should use upstream model")
    assert_true(payload["input"] == body["input"], "codex responses input should pass through unchanged")
    assert_true(payload["tools"] == body["tools"], "codex responses tools should pass through unchanged")

    completed = responses_completed_payload("resp_test", "codex-local", [], 3, "hello")
    assert_true(completed["object"] == "response", "completed response object mismatch")
    assert_true(completed["usage"]["total_tokens"] >= completed["usage"]["input_tokens"], "responses usage total mismatch")

    chat_payload = responses_request_to_chat_completions(body, "fallback", "chat-upstream")
    assert_true(chat_payload["model"] == "chat-upstream", "codex chat route should use upstream model")
    first_user_index = next(index for index, message in enumerate(chat_payload["messages"]) if message.get("role") == "user" and message.get("content") == "hello")
    assert_true(chat_payload["messages"][first_user_index]["content"] == "hello", "codex input text should convert to chat content")
    assert_true(chat_payload["messages"][first_user_index + 1]["tool_calls"][0]["id"] == "call_01", "codex function_call should convert to chat assistant tool_calls")
    assert_true(chat_payload["messages"][first_user_index + 2]["role"] == "tool", "codex function_call_output should convert to chat tool role")
    assert_true(chat_payload["messages"][first_user_index + 2]["tool_call_id"] == "call_01", "codex function output should preserve call_id")
    assert_true(chat_payload["messages"][first_user_index + 3]["role"] == "user", "codex function output should include visible fallback context")
    assert_true("C:/repo" in chat_payload["messages"][first_user_index + 3]["content"], "codex visible fallback should include tool output")
    assert_true(chat_payload["messages"][first_user_index + 4]["content"] == "continue", "codex multi-turn user context should remain ordered")
    assert_true(chat_payload["tools"][0]["function"]["name"] == "shell", "codex response tool should convert to chat tool")
    assert_true(chat_payload["tools"][1]["function"]["name"] == "read_file", "codex should preserve multiple tools")

    system_payload = responses_request_to_chat_completions({
        "instructions": "primary system",
        "input": [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "late system"},
            {"role": "developer", "content": "developer note"},
        ],
    }, "fallback", "chat-upstream")
    assert_true(system_payload["messages"][0]["role"] == "system", "codex chat route should keep system message first")
    assert_true(
        sum(1 for message in system_payload["messages"] if message.get("role") == "system") == 1,
        "codex chat route should merge system/developer messages for qwen-compatible upstreams",
    )
    assert_true(system_payload["messages"][1]["role"] == "user", "codex chat route should keep user messages after merged system")


def exercise_default_stream_config() -> None:
    anthropic_body = {"model": "smoke-model", "messages": [{"role": "user", "content": "hello"}]}
    assert_true(
        anthropic_messages_to_responses(anthropic_body, "fallback", "upstream", default_stream=True)["stream"] is True,
        "Anthropic to Responses should default to streaming when configured",
    )
    assert_true(
        anthropic_messages_to_responses(anthropic_body, "fallback", "upstream", default_stream=False)["stream"] is False,
        "Anthropic to Responses should honor configured non-stream default when stream is omitted",
    )
    explicit_non_stream = dict(anthropic_body, stream=False)
    assert_true(
        anthropic_messages_to_chat_completions(explicit_non_stream, "fallback", "upstream", default_stream=True)["stream"] is False,
        "Explicit Anthropic stream=false must override the configured streaming default",
    )

    responses_body = {"model": "smoke-model", "input": "hello"}
    assert_true(
        responses_request_to_upstream(responses_body, "fallback", "upstream", default_stream=True)["stream"] is True,
        "Responses passthrough should default to streaming when configured",
    )
    assert_true(
        responses_request_to_chat_completions(responses_body, "fallback", "upstream", default_stream=False)["stream"] is False,
        "Responses to Chat should honor configured non-stream default when stream is omitted",
    )
    explicit_stream = dict(responses_body, stream=True)
    assert_true(
        responses_request_to_upstream(explicit_stream, "fallback", "upstream", default_stream=False)["stream"] is True,
        "Explicit Responses stream=true must override the configured non-stream default",
    )

    kind, parsed = extract_text_delta("response.function_call_arguments.done", json.dumps({
        "type": "response.function_call_arguments.done",
        "item_id": "fc_01",
        "call_id": "call_01",
        "output_index": 0,
        "arguments": "{\"command\":\"pwd\"}",
    }))
    assert_true(kind == "tool_call" and parsed is not None, "codex responses function-call event should be parsed")
    assert_true(parsed["id"] == "call_01", "codex function-call parser should preserve call_id")

    kind, parsed = extract_text_delta(None, json.dumps({
        "choices": [{
            "message": {
                "content": "ignored when tool_calls exist",
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "shell", "arguments": "{\"command\":\"pwd\"}"}},
                    {"id": "call_b", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"}},
                ],
            },
            "finish_reason": "tool_calls",
        }]
    }))
    assert_true(kind == "tool_calls" and parsed is not None, "non-streaming chat tool_calls should take priority over content")
    assert_true(len(parsed["tool_calls"]) == 2, "non-streaming multi-tool response should preserve all tool calls")

    dsml_state = {"in_thinking": False}
    assert_true(filter_thinking_text_delta("<think>hidden</think>visible", dsml_state) == "visible", "codex thinking text should be filtered")
    split_dsml_state = {"in_thinking": False}
    assert_true(filter_thinking_text_delta("<｜DSM", split_dsml_state) == "", "codex split DSML prefix should be buffered")
    assert_true(filter_thinking_text_delta("L｜tool_calls", split_dsml_state) == "", "codex split DSML wrapper should be suppressed")


def exercise_codex_config_writer(tmpdir: Path) -> None:
    config = make_config(tmpdir)
    codex_model = ModelConfig(
        name="Codex Model",
        model_id="codex-model",
        base_url="https://example.invalid/v1/responses",
        api_key="codex-key",
        upstream_model="codex-upstream",
        api_format="responses",
    )
    config.models.append(codex_model)
    config.codex_model_id = "codex-model"
    config.codex_sandbox_mode = "workspace-write"
    config_file, auth_file = cli.write_codex_files(config)
    text = config_file.read_text(encoding="utf-8")
    assert_true("env_key" not in text, "codex provider should use auth.json instead of requiring an environment variable")
    assert_true('wire_api = "responses"' in text, "codex config must use responses wire API")
    assert_true("requires_openai_auth = true" in text, "codex config must require OpenAI auth")
    assert_true('model_provider = "shtu_proxy"' in text, "codex root model_provider should be set")
    assert_true('sandbox_mode = "workspace-write"' in text, "codex sandbox_mode should default to workspace-write")
    assert_true('[features]' in text and 'hooks = true' in text, "codex hooks feature should be enabled")
    assert_true("codex_hooks" not in text, "deprecated codex_hooks should not be written")
    if os.name == "nt":
        assert_true('[windows]' in text and 'sandbox = "elevated"' in text, "codex windows sandbox should be elevated on Windows")
    assert_true(f'base_url = "http://{config.host}:{config.port}/v1"' in text, "codex config base_url mismatch")
    assert_true('model = "codex-model"' in text, "codex config should use independent Codex model selection")
    auth = json.loads(auth_file.read_text(encoding="utf-8"))
    assert_true(auth["auth_mode"] == "apikey", "codex auth should select API key auth mode")
    assert_true(auth["OPENAI_API_KEY"] == "codex-key", "codex auth should write selected Codex model API key")
    auth["tokens"] = {"id_token": "preserve-me"}
    auth_file.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    config.port = 18083
    codex_model.api_key = "codex-key-updated"
    cli.write_codex_files(config)
    rewritten = config_file.read_text(encoding="utf-8")
    assert_true(rewritten.count("[model_providers.shtu_proxy]") == 1, "codex config writer should replace provider block")
    assert_true(rewritten.count('model = "codex-model"') == 2, "codex config writer should not duplicate model keys")
    assert_true(f'base_url = "http://{config.host}:{config.port}/v1"' in rewritten, "codex config should reflect updated proxy port")
    updated_auth = json.loads(auth_file.read_text(encoding="utf-8"))
    assert_true(updated_auth["auth_mode"] == "apikey", "codex auth should keep API key auth mode")
    assert_true(updated_auth["OPENAI_API_KEY"] == "codex-key-updated", "codex auth should reflect updated selected model API key")
    assert_true(updated_auth["tokens"]["id_token"] == "preserve-me", "codex auth writer should preserve unrelated auth fields")
    assert_true(list(config_file.parent.glob("config.toml.bak_*")), "codex config writer should back up before replacing existing config")
    assert_true(list(auth_file.parent.glob("auth.json.bak_*")), "codex auth writer should back up before replacing existing auth")

    config_file.write_text("\n".join([
        'model_provider = "custom"',
        'approval_policy = "on-request"',
        'model_context_window = 1000000',
        'model = "codex-model"',
        '',
        '[model_providers.custom]',
        'name = "custom"',
        'base_url = "https://genaiapi.shanghaitech.edu.cn/api/v1/response"',
        'wire_api = "responses"',
        'requires_openai_auth = true',
        '',
        'stream = false',
        'sandbox_mode = "read-only"',
        'model_reasoning_effort = "high"',
        '[features]',
        'codex_hooks = true',
        'hooks = false',
        'web_search = true',
        '[windows]',
        'sandbox = "read-only"',
        'wsl_proxy = true',
        '[projects."C:\\\\Users\\\\Administrator"]',
        'trust_level = "trusted"',
        '',
        '[mcp_servers.filesystem]',
        'command = "node"',
        'args = ["server.js"]',
        '',
        '[profiles.work]',
        'model_provider = "openai"',
        'model = "gpt-5.5"',
        '',
        '[tui.model_availability_nux]',
        '"gpt-5.5" = 4',
        '',
        'model = "codex-model"',
        'model_provider = "shtu_proxy"',
        '',
        'model = "codex-model"',
        'model_provider = "shtu_proxy"',
        '',
        '[model_providers.shtu_proxy]',
        'name = "Old Proxy"',
        '',
        '[profiles.shtu_proxy]',
        'model_provider = "shtu_proxy"',
        'model = "codex-model"',
        '',
    ]), encoding="utf-8")
    cli.write_codex_files(config)
    repaired = config_file.read_text(encoding="utf-8")
    first_section_index = repaired.index("[")
    root_text = repaired[:first_section_index]
    assert_true(root_text.count('model = "codex-model"') == 1, "codex model should be written at TOML root")
    assert_true(root_text.count('model_provider = "shtu_proxy"') == 1, "codex model_provider should be written at TOML root")
    assert_true('sandbox_mode = "workspace-write"' in root_text, "codex config writer should repair read-only sandbox mode")
    assert_true('[features]' in repaired and 'hooks = true' in repaired, "codex config writer should enable hooks")
    assert_true("codex_hooks" not in repaired, "codex config writer should remove deprecated codex_hooks")
    if os.name == "nt":
        assert_true('[windows]' in repaired and 'sandbox = "elevated"' in repaired, "codex config writer should repair Windows sandbox mode")
    assert_true('[model_providers.custom]' not in repaired, "codex config writer should remove old direct custom provider")
    assert_true('approval_policy = "never"' in root_text, "codex config writer should manage approval_policy from config")
    assert_true('personality = "default"' in root_text, "codex config writer should manage personality from config")
    assert_true('model_reasoning_effort = "medium"' in root_text, "codex config writer should manage reasoning effort from config")
    assert_true('model_context_window = 1000000' not in repaired, "codex config writer should remove stale unmanaged context window")
    assert_true('web_search = true' in repaired, "codex config writer should preserve other feature flags")
    assert_true('wsl_proxy = true' in repaired, "codex config writer should preserve other windows settings")
    assert_true('[mcp_servers.filesystem]' in repaired, "codex config writer should preserve MCP server blocks")
    assert_true('[profiles.work]' in repaired, "codex config writer should preserve unrelated profiles")
    assert_true('[tui.model_availability_nux]' in repaired, "codex config writer should preserve unrelated Codex tables")
    assert_true("[model_providers.shtu_proxy]" in repaired, "codex config writer should recover with a clean proxy config")

    with patch("cli.os.name", "posix"):
        posix_preserved = cli.codex_preserved_config_block('[features]\nhooks = false\n[windows]\nsandbox = "elevated"\nwsl_proxy = true\n')
        assert_true('[windows]' not in posix_preserved, "non-Windows Codex config should not preserve a Windows-only section")
        assert_true('hooks = true' in posix_preserved, "non-Windows Codex config should still repair features.hooks")
    with patch("cli.os.name", "nt"):
        windows_preserved = cli.codex_preserved_config_block('[windows]\nsandbox = "read-only"\nwsl_proxy = true\n')
        assert_true('[windows]' in windows_preserved and 'sandbox = "elevated"' in windows_preserved, "Windows Codex config should keep and repair windows sandbox")
        assert_true('wsl_proxy = true' in windows_preserved, "Windows Codex config should preserve unmanaged windows settings")

    once_repaired = repaired
    cli.write_codex_files(config)
    twice_repaired = config_file.read_text(encoding="utf-8")
    assert_true(twice_repaired == once_repaired, "repeated Codex config writes should be idempotent")

    for sandbox_mode in ("read-only", "workspace-write", "danger-full-access"):
        config.codex_sandbox_mode = sandbox_mode
        cli.write_codex_files(config)
        sandbox_text = config_file.read_text(encoding="utf-8")
        sandbox_root = sandbox_text[:sandbox_text.index("[")]
        assert_true(f'sandbox_mode = "{sandbox_mode}"' in sandbox_root, f"Codex sandbox_mode should support {sandbox_mode}")
        assert_true(sandbox_root.count("sandbox_mode =") == 1, "Codex sandbox_mode should not duplicate")
    config.codex_sandbox_mode = "workspace-write"

    before_invalid_attempt = config_file.read_text(encoding="utf-8")
    with patch("cli.codex_provider_profile_block", return_value='[profiles.shtu_proxy]\nmodel_provider = "other"\n'):
        try:
            cli.write_codex_config(config)
            raise AssertionError("invalid codex config should not be written")
        except ValueError:
            pass
    assert_true(config_file.read_text(encoding="utf-8") == before_invalid_attempt, "invalid codex config must not replace existing file")

    default_config = make_config(tmpdir / "default_glm")
    default_config.codex_model_id = "smoke-model"
    default_config.codex_config_path = str(tmpdir / "default_glm" / "config.toml")
    default_config.codex_auth_path = str(tmpdir / "default_glm" / "auth.json")
    old_env_config = os.environ.get("CLAUDE_RESPONSES_PROXY_CONFIG")
    os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"] = str(tmpdir / "default_glm" / "app_config.json")
    try:
        cli.write_codex_files(default_config)
    finally:
        if old_env_config is None:
            os.environ.pop("CLAUDE_RESPONSES_PROXY_CONFIG", None)
        else:
            os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"] = old_env_config
    default_text = Path(default_config.codex_config_path).read_text(encoding="utf-8")
    assert_true('model = "smoke-model"' in default_text, "Codex setup should preserve selected Codex model")
    seed_builtin_model_routes(default_config)
    assert_true(any(model.model_id == "glm-chat" and model.api_format == "chat_completions" for model in default_config.models), "glm-chat config should be available after initial route seeding")
    default_config.codex_model_id = "qwen-instruct"
    cli.write_codex_files(default_config)
    qwen_text = Path(default_config.codex_config_path).read_text(encoding="utf-8")
    assert_true('model = "qwen-instruct"' in qwen_text, "Codex setup should allow switching to qwen-instruct")

    persisted_config = tmpdir / "deleted_builtin_routes.json"
    seeded = AppConfig.default()
    seed_builtin_model_routes(seeded)
    seeded.models = [model for model in seeded.models if model.model_id not in {"glm-chat", "deepseek-chat", "qwen-instruct"}]
    save_config(seeded, persisted_config)
    reloaded = load_config(persisted_config)
    assert_true(
        not any(model.model_id in {"glm-chat", "deepseek-chat", "qwen-instruct"} for model in reloaded.models),
        "deleted built-in model routes should not be recreated when loading an existing config",
    )

    case_config = make_config(tmpdir / "case_sensitive")
    case_config.codex_config_path = str(tmpdir / "case_sensitive" / "config.toml")
    case_config.codex_auth_path = str(tmpdir / "case_sensitive" / "auth.json")
    case_file = Path(case_config.codex_config_path)
    case_file.parent.mkdir(parents=True, exist_ok=True)
    mixed_case_header = '[projects."C:\\\\Users\\\\Administrator\\\\MyCaseSensitiveSandbox"]'
    literal_case_header = "[projects.'D:\\Work\\MixedCaseProject']"
    case_file.write_text("\n".join([
        'model = "old"',
        mixed_case_header,
        'trust_level = "trusted"',
        literal_case_header,
        'trust_level = "trusted"',
    ]), encoding="utf-8")
    cli.write_codex_files(case_config)
    case_text = case_file.read_text(encoding="utf-8")
    assert_true(mixed_case_header in case_text, "Codex writer must preserve project path case and quoted header form")
    assert_true(literal_case_header in case_text, "Codex writer must preserve literal project path case")

    health_ok, health_messages = cli.codex_health_report(config)
    assert_true(health_ok, "Codex health check should pass for freshly written config")
    assert_true(any("MCP servers preserved" in item for item in health_messages), "Codex health should report preserved MCP servers")


def exercise_backup_restore(tmpdir: Path) -> None:
    existing = tmpdir / "existing.json"
    existing.write_text('{"old": true}', encoding="utf-8")
    original = snapshot_original_file(existing)
    assert_true(original is not None and original.exists(), "original backup should be created for existing file")
    existing.write_text('{"new": true}', encoding="utf-8")
    restored = restore_latest_backup(existing, original=True)
    assert_true(restored == original, "original backup path should be restored")
    assert_true('"old": true' in existing.read_text(encoding="utf-8"), "original restore should restore initial content")

    missing = tmpdir / "missing.json"
    marker = snapshot_original_file(missing)
    assert_true(marker is not None and marker.exists(), "missing original marker should be created")
    missing.write_text('{"created": true}', encoding="utf-8")
    restored_marker = restore_latest_backup(missing, original=True)
    assert_true(restored_marker == marker, "missing original marker should be used for restore")
    assert_true(not missing.exists(), "restoring originally missing file should remove generated file")


def exercise_headless_cli_model_config(tmpdir: Path) -> None:
    old_env_config = os.environ.get("CLAUDE_RESPONSES_PROXY_CONFIG")
    os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"] = str(tmpdir / "headless" / "config.json")
    try:
        configure_args = [
            "configure-model",
            "--model-id", "linux-smoke",
            "--api-key", "smoke-key",
            "--upstream-model", "linux-upstream",
            "--api-format", "chat_completions",
            "--default",
            "--codex",
            "--host", "127.0.0.1",
            "--port", "19083",
        ]
        assert_true(cli.main(configure_args) == 0, "headless configure-model command should succeed")
        config = load_config(Path(os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"]))
        model = config.find_model("linux-smoke")
        assert_true(model.api_key == "smoke-key", "headless CLI should persist the required API key")
        assert_true(model.upstream_model == "linux-upstream", "headless CLI should persist the upstream model")
        assert_true(model.api_format == "chat_completions", "headless CLI should persist API format")
        assert_true(config.default_model_id == "linux-smoke", "headless CLI should switch Claude default model")
        assert_true(config.codex_model_id == "linux-smoke", "headless CLI should switch Codex model")
        assert_true(config.port == 19083, "headless CLI should persist custom proxy port")
        assert_true(cli.main(["use-model", "linux-smoke", "--codex"]) == 0, "headless use-model command should accept existing model")
    finally:
        if old_env_config is None:
            os.environ.pop("CLAUDE_RESPONSES_PROXY_CONFIG", None)
        else:
            os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"] = old_env_config


def exercise_headless_apply_config(tmpdir: Path) -> None:
    old_env_config = os.environ.get("CLAUDE_RESPONSES_PROXY_CONFIG")
    os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"] = str(tmpdir / "apply_config" / "config.json")
    try:
        source = tmpdir / "headless-config.json"
        source.write_text(json.dumps({
            "host": "127.0.0.1",
            "port": 19084,
            "default_model_id": "file-smoke",
            "codex_model_id": "file-smoke",
            "models": [{
                "name": "File Smoke",
                "model_id": "file-smoke",
                "base_url": "https://example.invalid/v1/response",
                "api_key": "file-key",
                "upstream_model": "file-upstream",
                "api_format": "responses",
            }],
        }), encoding="utf-8")
        assert_true(cli.main(["apply-config", str(source)]) == 0, "headless apply-config should load a JSON config file")
        config = load_config(Path(os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"]))
        assert_true(config.default_model_id == "file-smoke", "apply-config should persist default model")
        assert_true(config.codex_model_id == "file-smoke", "apply-config should persist Codex model")
        assert_true(config.find_model("file-smoke").api_key == "file-key", "apply-config should persist model API key")
        bad = tmpdir / "headless-config-missing-key.json"
        bad.write_text(source.read_text(encoding="utf-8").replace("file-key", ""), encoding="utf-8")
        try:
            cli.apply_config_file(bad)
        except ValueError as exc:
            assert_true("Missing api_key" in str(exc), "apply-config should reject missing API keys loudly")
        else:
            raise AssertionError("apply-config accepted a model without api_key")
        template_path = Path("headless-config.example.json")
        if template_path.exists():
            template = cli.load_config_file(template_path)
            template_ids = {model.model_id for model in template.models}
            assert_true({"deepseek-pro", "deepseek-chat", "glm-chat", "qwen-instruct", "GPT-5.5"}.issubset(template_ids), "headless template should include the documented model routes")
        invalid_route = tmpdir / "headless-config-invalid-route.json"
        invalid_payload = json.loads(source.read_text(encoding="utf-8"))
        invalid_payload["model_env"] = {key: "missing-model" for key in MODEL_ENV_KEYS}
        invalid_route.write_text(json.dumps(invalid_payload), encoding="utf-8")
        try:
            cli.apply_config_file(invalid_route)
        except ValueError as exc:
            assert_true("model_env contains unknown model id" in str(exc), "apply-config should reject unknown Claude route IDs")
        else:
            raise AssertionError("apply-config accepted an unknown Claude route model")

        with patch("cli.background_proxy_running", return_value=True), patch("cli.restart_background") as restart_background:
            restarted = cli.apply_config_file(source, start=True)
            assert_true(restarted.default_model_id == "file-smoke", "apply-config --start should still apply config before restart")
            assert_true(restart_background.call_count == 1, "apply-config --start should restart an already running background proxy")
        with patch("cli.background_proxy_running", return_value=True):
            output = StringIO()
            with redirect_stdout(output):
                cli.apply_config_file(source, start=False)
            assert_true("restart" in output.getvalue().lower(), "apply-config without --start should warn when background proxy is running")
    finally:
        if old_env_config is None:
            os.environ.pop("CLAUDE_RESPONSES_PROXY_CONFIG", None)
        else:
            os.environ["CLAUDE_RESPONSES_PROXY_CONFIG"] = old_env_config


def exercise_background_start_regressions() -> None:
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "/tmp/shtucodeproxyctl"):
        assert_true(cli.background_command() == ["/tmp/shtucodeproxyctl", "serve"], "frozen background start should re-exec the bundled executable")
    with patch.object(sys, "frozen", False, create=True):
        command = cli.background_command()
        assert_true(command[-1] == "serve" and command[0] == sys.executable, "source background start should run cli.py serve")
    with patch("cli.read_proxy_pid", return_value=12345), patch("cli.process_is_running", return_value=True):
        assert_true(cli.background_proxy_running(), "running PID should be detected as an active background proxy")
    with patch("cli.os.name", "nt"), patch("cli.ctypes.windll.kernel32.OpenProcess", return_value=0), patch("cli.subprocess.run") as tasklist:
        tasklist.return_value.stdout = "python.exe                 12345 Console"
        assert_true(cli.process_is_running(12345), "Windows process detection should use tasklist instead of os.kill")
    with patch("cli.stop_background") as stop_background, patch("cli.start_background") as start_background:
        cli.restart_background(AppConfig.default())
        assert_true(stop_background.call_count == 1 and start_background.call_count == 1, "restart should stop then start the background proxy")


def exercise_multi_tool_call_delta() -> None:
    kind, parsed = extract_text_delta(None, json.dumps({
        "choices": [{
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_01",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": "{\"command\":\"pwd\"}"},
                    },
                    {
                        "index": 1,
                        "id": "call_02",
                        "type": "function",
                        "function": {"name": "LS", "arguments": "{\"path\":\".\"}"},
                    },
                ]
            }
        }]
    }))
    assert_true(kind == "tool_calls_delta" and parsed is not None, "multi tool call delta not detected")
    tool_calls = []
    for payload in parsed["tool_calls"]:
        merge_tool_call(tool_calls, payload)
    assert_true(len(tool_calls) == 2, "multi tool call delta dropped a tool")
    assert_true(tool_calls[0]["name"] == "Bash", "first tool call name mismatch")
    assert_true(tool_calls[1]["name"] == "LS", "second tool call name mismatch")

    glm_stream_chunks = [
        {"choices": [{"delta": {"tool_calls": [{"id": "chatcmpl-tool-1", "type": "function", "index": 0, "function": {"name": "shell", "arguments": ""}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"command\": \""}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "echo \\\"test"}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " output\\\""}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\""}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"id": None, "type": None, "index": 0, "function": {"name": None, "arguments": "}"}}]}, "finish_reason": "tool_calls"}]},
    ]
    glm_tool_calls = []
    for chunk in glm_stream_chunks:
        kind, parsed = extract_text_delta(None, json.dumps(chunk))
        if kind in ("tool_call", "tool_call_delta", "tool_calls", "tool_calls_delta"):
            merge_tool_call_payloads(glm_tool_calls, parsed)
    assert_true(glm_tool_calls[0]["name"] == "shell", "GLM stream should preserve tool name")
    assert_true(parse_tool_arguments(glm_tool_calls[0]["arguments"])["command"] == 'echo "test output"', "GLM stream tool arguments should merge correctly")


def exercise_cumulative_tool_call_delta() -> None:
    tool_calls = []
    partial = '{"description":"检查百度网络请求","prompt":"检查百度","run_in_background":true'
    full = '{"description":"检查百度网络请求","prompt":"检查百度", "run_in_background": true}'

    merge_tool_call(tool_calls, {
        "index": 0,
        "id": "call_agent",
        "name": "Agent",
        "arguments": partial,
    })
    merge_tool_call(tool_calls, {
        "index": 0,
        "arguments": full,
    })
    merge_tool_call(tool_calls, {
        "index": 0,
        "arguments": full,
    })

    repaired_agent_arguments = parse_tool_arguments(tool_calls[0]["arguments"])
    assert_true(
        repaired_agent_arguments["description"] == "检查百度网络请求"
        and repaired_agent_arguments["prompt"] == "检查百度"
        and repaired_agent_arguments["run_in_background"] is True,
        "cumulative tool-call argument snapshots should replace earlier partial snapshots",
    )


def exercise_tool_argument_repair_and_thinking_filter() -> None:
    observed_minimax_arguments = '</think>{\n  "command": "whoami"\n}'
    assert_true(
        parse_tool_arguments(observed_minimax_arguments)["command"] == "whoami",
        "tool argument parser should ignore leading thinking close tag",
    )
    assert_true(
        json.loads(tool_arguments_json(observed_minimax_arguments))["command"] == "whoami",
        "streaming tool arguments should be emitted as strict JSON",
    )
    nested_json_string_arguments = '"{\\"file_path\\": \\"/tmp/sample.txt\\", \\"old_string\\": \\"beta\\", \\"new_string\\": \\"gamma\\"}"'
    repaired_edit_arguments = parse_tool_arguments(nested_json_string_arguments)
    assert_true(
        repaired_edit_arguments["file_path"] == "/tmp/sample.txt",
        "tool argument parser should unwrap JSON strings containing objects",
    )
    assert_true(
        "arguments" not in repaired_edit_arguments,
        "repaired nested tool arguments should not keep an extra arguments wrapper",
    )
    wrapped_arguments_object = '{"arguments":"{\\"pattern\\": \\"beta\\", \\"path\\": \\"/tmp/sample.txt\\", \\"output_mode\\": \\"content\\"}"}'
    repaired_grep_arguments = parse_tool_arguments(wrapped_arguments_object)
    assert_true(
        repaired_grep_arguments["pattern"] == "beta",
        "tool argument parser should unwrap objects that only contain a JSON arguments string",
    )
    wrapped_arguments_dict = '{"arguments":{"description":"review","prompt":"inspect the diff"}}'
    repaired_agent_arguments = parse_tool_arguments(wrapped_arguments_dict)
    assert_true(
        repaired_agent_arguments["description"] == "review" and repaired_agent_arguments["prompt"] == "inspect the diff",
        "tool argument parser should unwrap objects that only contain an arguments object",
    )
    double_wrapped_arguments_object = (
        '{"arguments":"{\\"arguments\\": \\"{\\\\\\"file_path\\\\\\": '
        '\\\\\\"/tmp/sample.txt\\\\\\", \\\\\\"old_string\\\\\\": \\\\\\"beta\\\\\\", '
        '\\\\\\"new_string\\\\\\": \\\\\\"gamma\\\\\\"}\\"}"}'
    )
    repaired_double_wrapped_arguments = parse_tool_arguments(double_wrapped_arguments_object)
    assert_true(
        repaired_double_wrapped_arguments["old_string"] == "beta",
        "tool argument parser should unwrap repeated arguments wrappers",
    )

    state = {"in_thinking": False}
    assert_true(filter_thinking_text_delta("<think>The user", state) == "", "thinking prefix should be suppressed")
    assert_true(filter_thinking_text_delta(" is reasoning</think>Visible", state) == "Visible", "text after thinking block should remain")
    assert_true(filter_thinking_text_delta(" text", state) == " text", "normal text should pass through")

    dsml_state = {"in_thinking": False}
    assert_true(
        filter_thinking_text_delta("\n\n<｜DSML｜tool_calls", dsml_state) == "",
        "DSML tool-call wrapper should not leak as assistant text",
    )
    assert_true(
        filter_thinking_text_delta('\n<｜DSML｜invoke name="Grep">', dsml_state) == "",
        "DSML invoke markup should remain suppressed while the wrapper is open",
    )
    assert_true(
        filter_thinking_text_delta("</｜DSML｜tool_calls>Visible", dsml_state) == "Visible",
        "text after a DSML tool-call wrapper should remain visible",
    )

    split_dsml_state = {"in_thinking": False}
    assert_true(
        filter_thinking_text_delta("\n\n<｜DSM", split_dsml_state) == "",
        "partial DSML wrapper prefix should be buffered instead of leaked",
    )
    assert_true(
        filter_thinking_text_delta("L｜tool_calls", split_dsml_state) == "",
        "split DSML wrapper suffix should complete suppression",
    )
    split_think_state = {"in_thinking": False}
    assert_true(filter_thinking_text_delta("hello <thi", split_think_state) == "hello ", "partial think opening tag should be buffered")
    assert_true(filter_thinking_text_delta("nk>hidden</thi", split_think_state) == "", "split thinking body and closing prefix should be suppressed")
    assert_true(filter_thinking_text_delta("nk>visible", split_think_state) == "visible", "text after split thinking close should remain visible")


def exercise_id_uniqueness_and_non_stream_json() -> None:
    message_ids = {anthropic_message_id() for _ in range(200)}
    response_ids = {response_id() for _ in range(200)}
    assert_true(len(message_ids) == 200 and len(response_ids) == 200, "proxy response IDs should be unique even within the same millisecond")
    model = ModelConfig("Smoke", "smoke-model", "https://example.invalid/v1/response", "key", "smoke-upstream", "responses")
    converted = responses_json_to_anthropic_message({
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "call_1", "name": "exec_command", "arguments": {"cmd": "pwd"}},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }, model)
    assert_true(converted["content"][0] == {"type": "text", "text": "hello"}, "non-stream Responses JSON text should convert to Anthropic text")
    assert_true(converted["content"][1]["name"] == "exec_command" and converted["content"][1]["input"] == {"cmd": "pwd"}, "non-stream Responses JSON tool call should convert to Anthropic tool_use")
    assert_true(converted["stop_reason"] == "tool_use", "non-stream Responses JSON with function_call should stop as tool_use")


def exercise_cross_platform_tool_matrix() -> None:
    exec_tools = [{"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]
    payload = chat_completion_json_to_responses({
        "choices": [{"message": {"tool_calls": [
            {"id": "call_exec", "type": "function", "function": {"name": "exec_command", "arguments": '{"cmd":"pwd"}'}}
        ]}}]
    }, "glm-chat", 1, exec_tools)
    output = payload["output"][0]
    assert_true(output["name"] == "exec_command" and json.loads(output["arguments"])["cmd"] == "pwd", "explicit exec_command(cmd) calls should pass through unchanged")

    pseudo_payload = chat_completion_json_to_responses({
        "choices": [{"message": {"content": '<exec_command><command>pwd</command></exec_command>'}}]
    }, "glm-chat", 1, exec_tools)
    pseudo_output = pseudo_payload["output"][0]
    assert_true(pseudo_output["name"] == "exec_command" and json.loads(pseudo_output["arguments"]) == {"cmd": "pwd"}, "implicit pseudo exec_command should adapt to cmd schema")

    mixed_tools = [
        {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        {"type": "function", "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]
    _, mixed_calls = parse_pseudo_function_calls('<read_file path="/tmp/a.txt" /><exec_command><command>cat /tmp/a.txt</command></exec_command>', mixed_tools)
    assert_true([call["name"] for call in mixed_calls] == ["read_file", "exec_command"], "mixed implicit tools should preserve tool switching order")
    assert_true(json.loads(mixed_calls[0]["arguments"]) == {"path": "/tmp/a.txt"}, "read_file implicit args should remain path-based")
    assert_true(json.loads(mixed_calls[1]["arguments"]) == {"cmd": "cat /tmp/a.txt"}, "exec_command implicit args should use cmd")

    multi_turn_chat = responses_request_to_chat_completions({
        "model": "glm-chat",
        "input": [
            {"role": "user", "content": "list files"},
            {"type": "function_call", "call_id": "call_exec", "name": "exec_command", "arguments": {"cmd": "ls"}},
            {"type": "function_call_output", "call_id": "call_exec", "output": "README.md\n"},
            {"role": "user", "content": "now read it"},
        ],
        "tools": mixed_tools,
    }, "glm-chat")
    assert_true(any(message.get("tool_calls", [{}])[0].get("function", {}).get("name") == "exec_command" for message in multi_turn_chat["messages"] if isinstance(message.get("tool_calls"), list)), "multi-turn conversion should keep exec_command tool name")
    assert_true(any(message.get("role") == "tool" and message.get("tool_call_id") == "call_exec" for message in multi_turn_chat["messages"]), "multi-turn conversion should keep function_call_output as tool result")

    with patch("proxy.os.name", "nt"):
        windows_shell = codex_function_call_item({"name": "shell_exec", "arguments": '{"command":"Write-Output ok"}'})
        assert_true(json.loads(windows_shell["arguments"])["command"][:2] == ["powershell.exe", "-Command"], "Windows shell aliases should use PowerShell")
    with patch("proxy.os.name", "posix"):
        linux_shell = codex_function_call_item({"name": "shell_exec", "arguments": '{"command":"echo ok"}'})
        assert_true(json.loads(linux_shell["arguments"])["command"] == ["bash", "-lc", "echo ok"], "Linux shell aliases should use bash -lc")


def exercise_multimodal_chat_passthrough() -> None:
    image_url = "https://example.invalid/image.png"
    responses_payload = responses_request_to_chat_completions({
        "model": "qwen-instruct",
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "describe image"},
                {"type": "input_image", "image_url": image_url},
            ],
        }],
    }, "qwen-instruct")
    content = responses_payload["messages"][0]["content"]
    assert_true(isinstance(content, list), "Responses image input should stay as multimodal chat content")
    assert_true(content[1] == {"type": "image_url", "image_url": {"url": image_url}}, "Responses image_url should pass through to chat completions")

    anthropic_payload = anthropic_messages_to_chat_completions({
        "model": "qwen-instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe image"},
                {"type": "image", "source": {"type": "url", "url": image_url}},
                {"type": "document", "source": {"type": "url", "url": "https://example.invalid/a.pdf"}},
            ],
        }],
    }, "qwen-instruct")
    anthropic_content = anthropic_payload["messages"][0]["content"]
    assert_true(isinstance(anthropic_content, list), "Anthropic image input should stay as multimodal chat content")
    assert_true(anthropic_content[1] == {"type": "image_url", "image_url": {"url": image_url}}, "Anthropic image source should pass through to chat completions")
    assert_true(anthropic_content[2] == {"type": "file", "file": {"file_url": "https://example.invalid/a.pdf"}}, "Anthropic document URL should pass through to chat file content")

    converted = chat_completion_json_to_responses({
        "choices": [{"message": {"role": "assistant", "content": "vision ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }, "qwen-instruct", 10)
    assert_true(converted["object"] == "response" and isinstance(converted["output"], list), "Chat JSON should stay as Responses format for /v1/responses")
    bash_tool_converted = chat_completion_json_to_responses({
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "call_shell", "type": "function", "function": {"name": "shell", "arguments": "{\"command\":\"echo ok\"}"}}]}, "finish_reason": "tool_calls"}]
    }, "glm-chat", 10, [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}])
    bash_anthropic = responses_json_to_anthropic_message(bash_tool_converted, ModelConfig(
        name="GLM",
        model_id="glm-chat",
        base_url="https://example.invalid/v1/chat/completions",
        api_key="key",
        upstream_model="glm-chat",
        api_format="chat_completions",
    ))
    assert_true(bash_anthropic["content"][0]["name"] == "Bash", "Anthropic tool_use should preserve the actual Claude Code Bash tool name")
    anthropic_message = responses_json_to_anthropic_message(converted, ModelConfig(
        name="Qwen",
        model_id="qwen-instruct",
        base_url="https://example.invalid/v1/start",
        api_key="key",
        upstream_model="qwen-instruct",
        api_format="chat_completions",
    ))
    assert_true(anthropic_message["content"][0]["text"] == "vision ok", "Chat JSON should convert to Anthropic non-stream message text")


def exercise_structured_content_passthrough() -> None:
    responses_payload = responses_request_to_chat_completions({
        "model": "multimodal-model",
        "tool_choice": {"type": "function", "name": "get_weather"},
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "summarize attached content"},
                {"type": "input_file", "file_url": "https://example.invalid/a.pdf", "filename": "a.pdf"},
                {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
            ],
        }],
    }, "multimodal-model")
    chat_content = responses_payload["messages"][0]["content"]
    assert_true(responses_payload["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}, "Responses function tool_choice should convert to Chat Completions format")
    assert_true(isinstance(chat_content, list), "Structured Responses content should stay as multimodal chat content")
    assert_true(chat_content[1] == {"type": "file", "file": {"file_url": "https://example.invalid/a.pdf", "filename": "a.pdf"}}, "Responses input_file should pass through to chat file content")
    assert_true(chat_content[2] == {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}, "Responses input_audio should pass through to chat content")

    anthropic_responses = anthropic_messages_to_responses({
        "model": "multimodal-model",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "summarize document"},
                {"type": "document", "source": {"type": "url", "url": "https://example.invalid/a.pdf"}},
            ],
        }],
    }, "multimodal-model")
    response_content = anthropic_responses["input"][0]["content"]
    assert_true(isinstance(response_content, list), "Anthropic document should stay as structured Responses content")
    assert_true(response_content[1] == {"type": "input_file", "file_url": "https://example.invalid/a.pdf"}, "Anthropic document URL should map to Responses input_file")


def exercise_multimodal_capability_config() -> None:
    gpt_model = ModelConfig.from_dict({"model_id": "GPT-5.5", "upstream_model": "GPT-5.5"})
    qwen_model = ModelConfig.from_dict({"model_id": "qwen-instruct", "upstream_model": "qwen-instruct", "api_format": "chat_completions"})
    glm_model = ModelConfig.from_dict({"model_id": "glm-chat", "upstream_model": "glm-chat", "api_format": "chat_completions"})
    deepseek_model = ModelConfig.from_dict({"model_id": "deepseek-chat", "upstream_model": "deepseek-chat", "api_format": "chat_completions"})
    assert_true(gpt_model.supports_image and not gpt_model.supports_audio and not gpt_model.supports_video, "GPT-5.5 should default to image-only multimodal support")
    assert_true(qwen_model.supports_image and not qwen_model.supports_audio and not qwen_model.supports_video, "qwen-instruct should default to image-only multimodal support")
    assert_true(not glm_model.supports_image and not glm_model.supports_audio and not glm_model.supports_video, "glm-chat should default to text-only")
    assert_true(not deepseek_model.supports_image and not deepseek_model.supports_audio and not deepseek_model.supports_video, "deepseek-chat should default to text-only")
    assert_true(ModelConfig.from_dict({"model_id": "custom-vision", "supports_image": True}).supports_image, "explicit image config should be preserved")
    assert_true(ModelConfig.from_dict({"model_id": "legacy-vision", "supports_multimodal": True}).supports_image, "legacy multimodal config should enable image support")
    assert_true(not ModelConfig.from_dict({"model_id": "custom-text", "supports_image": "false"}).supports_image, "string false should parse as disabled")
    anthropic_body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image", "source": {"type": "url", "url": "https://example.invalid/a.png"}}]}]}
    responses_body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": "https://example.invalid/a.png"}]}]}
    assert_true(anthropic_current_user_modalities(anthropic_body) == {"image"}, "Anthropic current image input should be detected")
    assert_true(responses_current_user_modalities(responses_body) == {"image"}, "Responses current image input should be detected")
    assert_true(unsupported_modalities(glm_model, {"image", "audio"}) == {"image", "audio"}, "Unsupported modalities should be specific")
    assert_true("图片识别" in unsupported_modalities_message(glm_model, {"image"}), "Unsupported message should be user-facing")

    vision_tools = [
        {"name": "view_image", "description": "Render and inspect an image or screenshot", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "read_file", "description": "Read a text file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]
    glm_tools_body = sanitized_anthropic_body_for_model({"tools": vision_tools, "tool_choice": {"type": "tool", "name": "view_image"}, "messages": []}, glm_model)
    qwen_tools_body = sanitized_anthropic_body_for_model({"tools": vision_tools, "tool_choice": {"type": "tool", "name": "view_image"}, "messages": []}, qwen_model)
    assert_true([tool["name"] for tool in glm_tools_body["tools"]] == ["read_file"], "Image-disabled model should not receive view_image tool")
    assert_true(glm_tools_body["tool_choice"] == {"type": "auto"}, "Forced removed image tool should fall back to auto")
    assert_true(any(tool["name"] == "view_image" for tool in qwen_tools_body["tools"]), "Image-capable model should keep view_image tool")

    anthropic_history_body = {
        "messages": [
            anthropic_body["messages"][0],
            {"role": "assistant", "content": [{"type": "text", "text": "模型 glm-chat 当前配置为不支持图片识别。"}]},
            {"role": "user", "content": "继续回答纯文本"},
        ]
    }
    assert_true(not anthropic_current_user_modalities(anthropic_history_body), "Historical image should not block current text-only Anthropic turn")
    sanitized_anthropic = sanitized_anthropic_body_for_model(anthropic_history_body, glm_model)
    assert_true(not any(isinstance(part, dict) and part.get("type") == "image" for part in sanitized_anthropic["messages"][0]["content"]), "Historical Anthropic image should not reach a text-only model")
    assert_true(any(isinstance(part, dict) and part.get("text") for part in sanitized_anthropic["messages"][0]["content"]), "Text-only model should receive an image placeholder instead of an empty current turn")
    qwen_sanitized_anthropic = sanitized_anthropic_body_for_model(anthropic_body, qwen_model)
    assert_true(any(isinstance(part, dict) and part.get("type") == "image" for part in qwen_sanitized_anthropic["messages"][0]["content"]), "Image-capable model should keep image input even if audio/video are disabled")

    anthropic_tool_image_body = {
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_img", "name": "screenshot", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_img", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]},
            {"role": "user", "content": "继续回答纯文本"},
        ]
    }
    assert_true(not anthropic_current_user_modalities(anthropic_tool_image_body), "Historical tool image should not block current Anthropic text turn")
    sanitized_tool_image = sanitized_anthropic_body_for_model(anthropic_tool_image_body, glm_model)
    assert_true("data:image" not in json.dumps(sanitized_tool_image), "Anthropic tool_result image data should not reach image-disabled models")
    assert_true("已移除当前模型不支持" in json.dumps(sanitized_tool_image, ensure_ascii=False), "Anthropic tool_result image should become a visible placeholder")
    assert_true("view_image" not in json.dumps(sanitized_tool_image), "Anthropic visual tool_use should be stripped for image-disabled models")
    anthropic_tool_data_url_body = {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_img", "content": "data:image/png;base64,AAAA"}]}]}
    sanitized_tool_data_url = sanitized_anthropic_body_for_model(anthropic_tool_data_url_body, glm_model)
    assert_true("data:image" not in json.dumps(sanitized_tool_data_url), "Anthropic tool_result data URL should be removed for image-disabled models")

    responses_history_body = {
        "input": [
            responses_body["input"][0],
            {"role": "assistant", "content": [{"type": "output_text", "text": "模型 glm-chat 当前配置为不支持图片识别。"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "继续回答纯文本"}]},
        ]
    }
    assert_true(not responses_current_user_modalities(responses_history_body), "Historical image should not block current text-only Responses turn")
    sanitized_responses = sanitized_responses_body_for_model(responses_history_body, glm_model)
    assert_true(not any(isinstance(part, dict) and part.get("type") == "input_image" for part in sanitized_responses["input"][0]["content"]), "Historical Responses image should be stripped before forwarding to text-only model")
    qwen_sanitized_responses = sanitized_responses_body_for_model(responses_body, qwen_model)
    assert_true(any(isinstance(part, dict) and part.get("type") == "input_image" for part in qwen_sanitized_responses["input"][0]["content"]), "Image-capable Responses model should keep image input even if audio/video are disabled")

    responses_tool_image_body = {
        "input": [
            {"type": "function_call", "call_id": "call_img", "name": "screenshot", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_img", "output": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "继续回答纯文本"}]},
        ]
    }
    assert_true(not responses_current_user_modalities(responses_tool_image_body), "Historical tool image should not block current Responses text turn")
    sanitized_responses_tool_image = sanitized_responses_body_for_model(responses_tool_image_body, glm_model)
    assert_true("data:image" not in json.dumps(sanitized_responses_tool_image), "Responses function_call_output image data should not reach image-disabled models")
    assert_true("view_image" not in json.dumps(sanitized_responses_tool_image), "Responses visual function_call should be stripped for image-disabled models")
    responses_tool_data_url_body = {"input": [{"type": "function_call_output", "call_id": "call_img", "output": "data:image/png;base64,AAAA"}]}
    sanitized_responses_tool_data_url = sanitized_responses_body_for_model(responses_tool_data_url_body, glm_model)
    assert_true("data:image" not in json.dumps(sanitized_responses_tool_data_url), "Responses function_call_output data URL should be removed for image-disabled models")

    stale_chat_payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "old image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }]
    }
    cleaned_chat_payload = sanitized_upstream_payload_for_model(stale_chat_payload, glm_model)
    assert_true("data:image" not in json.dumps(cleaned_chat_payload), "Final Chat payload sanitizer should remove stale image data for image-disabled models")
    assert_true("已移除当前模型不支持" in json.dumps(cleaned_chat_payload, ensure_ascii=False), "Final Chat payload sanitizer should keep an image placeholder")
    image_only_payload = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    cleaned_image_only = sanitized_upstream_payload_for_model(image_only_payload, glm_model)
    assert_true(cleaned_image_only["messages"][0]["content"], "Final sanitizer should not leave empty chat content after removing unsupported image")

    stale_responses_payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "old image"}, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]}
    cleaned_responses_payload = sanitized_upstream_payload_for_model(stale_responses_payload, glm_model)
    assert_true("data:image" not in json.dumps(cleaned_responses_payload), "Final Responses payload sanitizer should remove stale image data for image-disabled models")
    assert_true("已移除当前模型不支持" in json.dumps(cleaned_responses_payload, ensure_ascii=False), "Final Responses payload sanitizer should keep an image placeholder")
    stale_tool_output_payload = {"input": [{"type": "function_call_output", "call_id": "call_img", "output": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]}
    cleaned_tool_output_payload = sanitized_upstream_payload_for_model(stale_tool_output_payload, glm_model)
    assert_true("data:image" not in json.dumps(cleaned_tool_output_payload), "Final sanitizer should remove nested tool output image data")
    assert_true("已移除当前模型不支持" in json.dumps(cleaned_tool_output_payload, ensure_ascii=False), "Final sanitizer should keep nested tool output placeholders")
    cleaned_tool_data_url_payload = sanitized_upstream_payload_for_model({"input": [{"type": "function_call_output", "call_id": "call_img", "output": "data:image/png;base64,AAAA"}]}, glm_model)
    assert_true("data:image" not in json.dumps(cleaned_tool_data_url_payload), "Final sanitizer should remove nested tool output data URLs")
    image_only_responses = {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]}
    cleaned_image_only_responses = sanitized_upstream_payload_for_model(image_only_responses, glm_model)
    assert_true(cleaned_image_only_responses["input"][0]["content"], "Final sanitizer should not leave empty Responses content after removing unsupported image")
    current_image_responses = sanitized_responses_body_for_model(responses_body, glm_model)
    current_image_payload = responses_request_to_chat_completions(current_image_responses, "glm-chat")
    assert_true("data:image" not in json.dumps(current_image_payload), "Current Codex image request should be downgraded before reaching text-only Chat upstream")
    assert_true("已移除当前模型不支持" in json.dumps(current_image_payload, ensure_ascii=False), "Current Codex image request should continue with a text placeholder instead of proxy-side blocking")

    current_image_anthropic = sanitized_anthropic_body_for_model(anthropic_body, glm_model)
    current_image_chat = anthropic_messages_to_chat_completions(current_image_anthropic, "glm-chat")
    assert_true("data:image" not in json.dumps(current_image_chat), "Current Claude image request should be downgraded before reaching text-only Chat upstream")
    assert_true("已移除当前模型不支持" in json.dumps(current_image_chat, ensure_ascii=False), "Current Claude image request should continue with a text placeholder instead of proxy-side blocking")

    codex_payload_with_tools = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "tools": [{"type": "function", "function": {"name": "shell", "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}}}}}],
    }
    cleaned_codex_payload = sanitized_upstream_payload_for_model(codex_payload_with_tools, glm_model)
    assert_true(cleaned_codex_payload["tools"][0]["function"]["parameters"]["type"] == "object", "Final sanitizer should not inspect ordinary tool JSON schema type values")


def main() -> int:
    tmpdir = Path.cwd() / ".smoke_tmp"
    if tmpdir.exists():
        shutil.rmtree(tmpdir)
    tmpdir.mkdir(parents=True)
    try:
        config = make_config(tmpdir)
        config_path = tmpdir / "config.json"
        save_config(config, config_path)

        env = cli.claude_env(config)
        assert_true(env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18082", "base URL env mismatch")
        assert_true(env["ANTHROPIC_MODEL"] == "smoke-model", "model env mismatch")
        assert_true(env["ANTHROPIC_AUTH_TOKEN"] == "local-proxy", "auth token env mismatch")

        settings_path = cli.write_claude_settings(config)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert_true(settings["env"]["ANTHROPIC_MODEL"] == "smoke-model", "settings model mismatch")
        assert_true(settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18082", "settings base URL mismatch")
        config.port = 19082
        changed_port_env = cli.claude_env(config)
        assert_true(changed_port_env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:19082", "changed port env mismatch")
        changed_settings_path = cli.write_claude_settings(config)
        changed_settings = json.loads(changed_settings_path.read_text(encoding="utf-8"))
        assert_true(changed_settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:19082", "changed port settings mismatch")
        config.port = 18082

        script = launch_script_text(env, "claude")
        assert_true("ANTHROPIC_BASE_URL" in script, "launch script missing base URL")
        assert_true("ANTHROPIC_MODEL" in script, "launch script missing model")
        script_path = tmpdir / ("claude-shtu.ps1" if os.name == "nt" else "claude-shtu.sh")
        script_path.write_text(script, encoding="utf-8")
        assert_true(script_path.exists(), "launch script was not created")

        assert_true(portable_claude_path("") != "", "portable claude path fallback empty")
        assert_true(portable_settings_path("").endswith(str(Path(".claude") / "settings.json")), "settings fallback mismatch")
        exercise_claude_auto_classifier_detection()
        exercise_anthropic_stream_delta_usage_shape(tmpdir)
        exercise_tool_call_translation()
        exercise_cache_control_passthrough()
        exercise_file_logging(tmpdir)
        exercise_chat_completion_json_to_responses()
        exercise_mixed_tool_result_ordering()
        exercise_model_suffix_routing()
        exercise_deleted_model_route_cleanup(tmpdir)
        exercise_pyqt_model_management_regressions()
        exercise_count_tokens_estimate()
        exercise_codex_responses_passthrough()
        exercise_default_stream_config()
        exercise_codex_config_writer(tmpdir)
        exercise_backup_restore(tmpdir)
        exercise_headless_cli_model_config(tmpdir)
        exercise_headless_apply_config(tmpdir)
        exercise_background_start_regressions()
        app_cli = subprocess.run([sys.executable, "app.py", "status"], cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=10)
        assert_true(app_cli.returncode == 0 and "Proxy URL:" in app_cli.stdout, "app.py should pass CLI arguments through without requiring a GUI")
        exercise_multi_tool_call_delta()
        exercise_cumulative_tool_call_delta()
        exercise_tool_argument_repair_and_thinking_filter()
        exercise_id_uniqueness_and_non_stream_json()
        exercise_cross_platform_tool_matrix()
        exercise_multimodal_chat_passthrough()
        exercise_structured_content_passthrough()
        exercise_multimodal_capability_config()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("SHTUClaudeProxy smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
