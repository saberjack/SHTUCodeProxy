# Fix #007: Claude Code auto mode safety classifier 走 qwen-instruct 失败

## 目标

修复 Claude Code CLI auto mode 在执行 Bash 前调用 safety classifier 失败的问题。

## 验收标准

- Claude Code CLI auto mode 下触发 Bash 安全判定时，不再报 `qwen-instruct is temporarily unavailable`。
- 修复不影响 `/v1/messages` 与 `/v1/responses` 的既有工具调用转换。
- 通过 smoke 测试中与 Claude 路由、工具调用相关的回归。

## 初始假设

- Claude Code auto mode 的 Bash safety classifier 使用 Haiku/小模型路由。
- 本机配置中 `ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen-instruct`，因此错误信息直接暴露 qwen-instruct。
- qwen-instruct 不是稳定的确定性安全分类模型；应避免作为 Claude Code safety classifier 路由。

## 影响范围

- `src/config_store.py`：Claude 模型路由默认值/兼容迁移。
- `src/cli_integration.py` 或相关配置写入逻辑：如需，保证 Claude settings/env 使用安全路由。
- `tests/smoke_test.py`：补充路由回归。

## 风险评估

- 将 Haiku safety 路由改为 GPT-5.5 会增加 auto mode 判定请求成本，但优先保证工具执行可用。
- 不改变用户主模型选择，避免影响日常对话模型路由。


## 排查结论

- 用户约束：Claude Code 的 Main / Haiku / Sonnet / Opus / Reasoning 路由均必须由用户自由选择，不能在代码里绑定到固定模型。
- 真实复现：测试端口 8097 + Claude Code CLI 2.1.172 + 全路由 qwen-instruct + `--permission-mode auto`，执行 `ANTHROPIC_AUTH_TOKEN` 非泄露检查时，classifier 报 `undefined is not an object (evaluating 'x.usage.input_tokens')`。
- 根因：auto classifier 是普通非流式 Messages 调用；代理使用全局 `default_stream=True` 将其改成 SSE，Claude classifier 按非流式结构读取 usage 时失败。同时 qwen classifier 请求不应自动注入 thinking，否则会破坏严格 `<block>` 输出。

## 修复内容

- 增加 `is_claude_auto_classifier_request()`，识别 Claude Code auto safety classifier 的 system prompt。
- classifier 请求未显式带 `stream` 时强制按非流式返回，保留普通用户请求的默认流式行为。
- classifier 请求跳过自动 thinking 注入，避免 qwen 输出 reasoning transcript 而不是 `<block>` 判定。
- 补齐 Anthropic SSE fallback 的 `message_delta.usage.input_tokens`，避免同类解析器因 usage 形状不完整崩溃。

## 验证结果

- `python -m py_compile src\proxy.py tests\smoke_test.py`：PASS。
- targeted tests：`exercise_claude_auto_classifier_detection()`、`exercise_anthropic_stream_delta_usage_shape()` PASS。
- 真实 CLI：`claude -p --settings .tmp-claude-qwen-settings.json --permission-mode auto ... "Run Bash to check whether ANTHROPIC_AUTH_TOKEN is set without printing its value."` 成功执行并输出 token is set，不再报 qwen unavailable。
- 完整 `PYTHONPATH=src python tests\smoke_test.py` 仍在既有 `Codex Chat payload should get automatic cache boundaries` 断言失败；该失败与本次 classifier 修复无关，未扩大处理。
