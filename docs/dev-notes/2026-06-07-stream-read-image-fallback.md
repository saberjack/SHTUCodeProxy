# 2026-06-07 Claude Code Read 大截图流式 500 修复

## 背景

用户使用 Claude Code + glm-chat 执行“打开 www.baidu.com 并截图识别”。Claude Code 先通过 kimi-webbridge 截图保存 PNG，再用 Read 读取图片；随后继续尝试 snapshot/JPEG，代理向客户端输出 `[Proxy Error] Upstream HTTP 500:`。

## 复现

构造 1.2MB 级 base64 PNG 的 `tool_result`，并包含 Bash 截图、Read 图片、snapshot 三段工具历史：

- `/v1/messages`
- `model=glm-chat`
- `stream=true`
- Chat Completions 上游

4.6.1 会以 SSE 文本返回 `[Proxy Error] Upstream HTTP 500:`。

## 根因

- 已有多模态清洗可以移除图片数据，但 GLM 的 Chat Completions 原生流式接口在该类多轮工具历史上仍可能返回 HTTP 500。
- 同一上游 payload 改为 `stream=false` 后可以成功返回答案。
- 自动 cache_control 还可能把 Chat `role=tool` 消息的字符串 content 改成数组，违反部分 Chat Completions 上游兼容预期。

## 修复

- `apply_auto_cache_control_to_chat_payload` 跳过 `role=tool` 消息，避免把 tool content 从字符串改成数组。
- `apply_auto_cache_control` 不再对 Responses payload 自动添加 cache_control，保持既有测试意图。
- Anthropic Messages 流式处理遇到 Chat Completions 上游 HTTPError 且尚未输出正文/工具调用时，自动用同一清洗 payload 做一次 `stream=false` fallback，并把非流式结果转换成 Anthropic SSE 返回给 Claude Code。
- `type=image` 无论 `source.type` 是什么都识别为图片，覆盖 Read 输出变体。

## 验证

- `python -m py_compile src/proxy.py tests/smoke_test.py`：通过。
- 目标 smoke：`exercise_cache_control_passthrough()` 和 `exercise_multimodal_capability_config()`：通过。
- 真实请求：1.2MB 截图 Read tool_result + snapshot 多轮历史 + `glm-chat` + `stream=true`：HTTP 200 SSE，且不再包含 `[Proxy Error] Upstream HTTP 500`。
