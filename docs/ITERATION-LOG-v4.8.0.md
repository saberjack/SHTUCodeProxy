# ITERATION LOG v4.8.0

## 目标

发布 Claude Code auto mode safety classifier 兼容性修复，解决 qwen-instruct 被选作 auto classifier 时 Bash 安全判定失败的问题。

## 范围

- `src/proxy.py`：识别 Claude Code auto classifier 请求；保持用户模型路由可选；修复 classifier 非流式响应和 thinking 注入兼容性；补齐 Anthropic SSE usage 字段。
- `tests/smoke_test.py`：新增 classifier 请求识别与 SSE usage 形状回归。
- `docs/ISSUE-TRACKER.md`、`docs/dev-notes/2026-06-11-claude-auto-classifier-qwen.md`：记录问题、根因、验证。

## 验收标准

- Claude Code CLI `--permission-mode auto` 在 qwen-instruct 路由下执行安全 Bash 检查时不再报 classifier unavailable。
- 不绑定 Main / Haiku / Sonnet / Opus / Reasoning 到固定模型，保留用户可配置能力。
- Windows 发布资产本地构建。
- GitHub Release 资产清单与既有 9 项发布清单一致。

## 验证记录

- `python -m py_compile src\proxy.py tests\smoke_test.py`：PASS。
- Targeted tests：PASS。
- 真实 CLI 测试：Claude Code CLI 2.1.172 + qwen-instruct + test port 8097 + `--permission-mode auto`，Bash token presence check 成功执行。
- 完整 smoke：存在既有 cache-control 断言失败，非本次发布阻断项；已在 Issue #007 开发记录中说明。
