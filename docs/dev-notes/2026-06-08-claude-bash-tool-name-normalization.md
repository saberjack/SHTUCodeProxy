# 2026-06-08 Claude Code Bash 工具名归一化

## 背景

用户验证 4.6.1 fixed 后，HTTP500 已消失，但 glm-chat 在需要继续调用 snapshot/Bash 时反复输出 `shell` 工具名。Claude Code 当前可用工具名是 `Bash`，因此模型进入“我应该使用 Bash 但又输出 shell”的循环。

## 根因

- Chat Completions 上游可能返回通用 `shell` tool_call。
- 代理转换为 Anthropic Messages 时，没有根据当前请求的 `tools` 列表把 `shell` 映射回 Claude Code 的真实工具名 `Bash`。
- 现有 `codex_function_call_item` 默认服务 Codex Responses，会把 `bash` 规范成 `shell`；Anthropic 输出不能沿用这个默认行为。

## 修复

- 新增 `openai_tool_names` 与 `normalize_tool_call_name_for_tools`，根据请求工具列表选择真实工具名。
- `chat_completion_json_to_responses` 在保留工具调用前先做工具名归一化。
- `codex_function_call_item` 增加 `normalize_shell_aliases` 参数；Anthropic Messages 路径关闭该参数，避免 `Bash` 被转回 `shell`。

## 验证

- `python -m py_compile src/proxy.py tests/smoke_test.py`：通过。
- 目标 smoke：`exercise_chat_completion_json_to_responses()`、`exercise_multimodal_chat_passthrough()`：通过。
- 新断言覆盖：上游返回 `shell`，请求工具列表为 `Bash` 时，最终 Anthropic `tool_use.name` 为 `Bash`。
