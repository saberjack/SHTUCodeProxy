from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import cli
from config_store import AppConfig, MODEL_ENV_KEYS, ModelConfig, save_config, tokens_from_api_notes
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
    responses_request_to_upstream,
    chat_completion_json_to_responses,
    stop_reason_from_done,
    tool_arguments_json,
)


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
        model_env={key: "smoke-model" for key in MODEL_ENV_KEYS},
        timeout=30,
        claude_path="claude",
        claude_settings_path=str(tmpdir / ".claude" / "settings.json"),
        codex_config_path=str(tmpdir / ".codex" / "config.toml"),
        codex_auth_path=str(tmpdir / ".codex" / "auth.json"),
        models=[model],
    )


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


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

    shell_item = codex_function_call_item({"name": "shell_exec", "arguments": '{"command":"Write-Output ok"}'})
    shell_args = json.loads(shell_item["arguments"])
    assert_true(shell_item["name"] == "shell", "Codex shell aliases should normalize to shell")
    assert_true(shell_args["command"] == ["powershell.exe", "-Command", "Write-Output ok"], "Codex shell command should be an argv array")
    echo_item = codex_function_call_item({"name": "shell", "arguments": {"command": ["echo", "sandbox ok"]}})
    echo_args = json.loads(echo_item["arguments"])
    assert_true(echo_args["command"][0] == "powershell.exe", "bare Windows shell commands should be wrapped through PowerShell")


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


def exercise_api_notes_token_parsing(tmpdir: Path) -> None:
    notes = tmpdir / "api.txt"
    notes.write_text("""
GLM 5.1:
curl -H 'Authorization: Bearer glm-key' -d '{}'


deepv4:
curl -H 'Authorization: Bearer deep-key' -d '{}'


qwen3.5:
curl -H 'Authorization: Bearer qwen-key' -d '{}'
""", encoding="utf-8")
    tokens = tokens_from_api_notes(notes)
    assert_true(tokens["glm-chat"] == "glm-key", "glm token parse mismatch")
    assert_true(tokens["deepseek-chat"] == "deep-key", "deepseek token parse mismatch")
    assert_true(tokens["qwen-instruct"] == "qwen-key", "qwen token parse mismatch")


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
        model_env={key: "chatglm" for key in MODEL_ENV_KEYS},
        timeout=30,
        claude_path="claude",
        claude_settings_path="/tmp/settings.json",
        codex_config_path="/tmp/codex-config.toml",
        codex_auth_path="/tmp/codex-auth.json",
        models=[default_model, haiku_model, sonnet_alias, direct_deepseek],
    )

    routed = config.find_model("claude-haiku-4-5-20251001")
    assert_true(routed.model_id == "claude-haiku-4-5", "dated Claude model ID should route to configured alias")
    routed_direct = config.find_model("deepseek-pro")
    assert_true(routed_direct.model_id == "deepseek-pro", "exact model ID should beat upstream model alias matches")


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
    config_file, auth_file = cli.write_codex_files(config)
    text = config_file.read_text(encoding="utf-8")
    assert_true("env_key" not in text, "codex provider should use auth.json instead of requiring an environment variable")
    assert_true('wire_api = "responses"' in text, "codex config must use responses wire API")
    assert_true("requires_openai_auth = true" in text, "codex config must require OpenAI auth")
    assert_true('model_provider = "shtu_proxy"' in text, "codex root model_provider should be set")
    assert_true('sandbox_mode = "workspace-write"' in text, "codex sandbox_mode should default to workspace-write")
    assert_true('[features]' in text and 'hooks = true' in text, "codex hooks feature should be enabled")
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
        'hooks = false',
        '[windows]',
        'sandbox = "read-only"',
        '[projects."C:\\\\Users\\\\Administrator"]',
        'trust_level = "trusted"',
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
    if os.name == "nt":
        assert_true('[windows]' in repaired and 'sandbox = "elevated"' in repaired, "codex config writer should repair Windows sandbox mode")
    assert_true('[model_providers.custom]' not in repaired, "codex config writer should remove old direct custom provider")
    assert_true('[tui.model_availability_nux]' not in repaired, "codex config writer should remove stale tui availability sections")
    assert_true("[model_providers.shtu_proxy]" in repaired, "codex config writer should recover with a clean proxy config")

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
    assert_true(any(model.model_id == "glm-chat" and model.api_format == "chat_completions" for model in default_config.models), "glm-chat config should be available as an optional proxy route")
    default_config.codex_model_id = "qwen-instruct"
    cli.write_codex_files(default_config)
    qwen_text = Path(default_config.codex_config_path).read_text(encoding="utf-8")
    assert_true('model = "qwen-instruct"' in qwen_text, "Codex setup should allow switching to qwen-instruct")

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

        script = launch_script_text(env, "claude")
        assert_true("ANTHROPIC_BASE_URL" in script, "launch script missing base URL")
        assert_true("ANTHROPIC_MODEL" in script, "launch script missing model")
        script_path = tmpdir / ("claude-shtu.ps1" if os.name == "nt" else "claude-shtu.sh")
        script_path.write_text(script, encoding="utf-8")
        assert_true(script_path.exists(), "launch script was not created")

        assert_true(portable_claude_path("") != "", "portable claude path fallback empty")
        assert_true(portable_settings_path("").endswith(str(Path(".claude") / "settings.json")), "settings fallback mismatch")
        exercise_tool_call_translation()
        exercise_chat_completion_json_to_responses()
        exercise_api_notes_token_parsing(tmpdir)
        exercise_mixed_tool_result_ordering()
        exercise_model_suffix_routing()
        exercise_count_tokens_estimate()
        exercise_codex_responses_passthrough()
        exercise_codex_config_writer(tmpdir)
        exercise_multi_tool_call_delta()
        exercise_cumulative_tool_call_delta()
        exercise_tool_argument_repair_and_thinking_filter()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("SHTUClaudeProxy smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
