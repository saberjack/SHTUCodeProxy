# 2026-06-07 多模态输入降级为文本占位

## 背景

Claude Code 和 Codex 可能自动发送图片输入。GLM、DeepSeek 等文本模型不支持多模态时，旧逻辑会在 proxy 入口直接返回本地错误，或让图片块继续进入上游后触发 HTTP 500。

## cc-switch 参考

`cc-switch` 的 `media_sanitizer.rs` 对文本模型采用媒体降级策略：基于模型能力声明识别文本模型，把图片块替换为 `[Unsupported Image]` 文本块，而不是让图片进入上游或直接中断请求。

## 修复

- `/v1/messages` 与 `/v1/responses` 不再因当前用户图片输入直接阻断；仅记录降级日志。
- `sanitized_content_for_model` 将不支持的图片/音频/视频块替换为文本占位：`[已移除当前模型不支持的图片/音频/视频输入]`。
- `sanitized_upstream_payload_for_model` 作为最终保险，同样把遗留媒体块替换为占位文本，避免空 content 或媒体数据进入文本模型。
- 历史视觉工具调用仍按既有规则过滤，避免把不可执行的视觉工具结果传给文本模型。

## 验证

- `python -m py_compile src/proxy.py tests/smoke_test.py`：通过。
- 目标 smoke：`exercise_multimodal_capability_config()`：通过。
- 真实 API 临时代理端口 8083：
  - `/v1/responses` + `glm-chat` + 图片输入：HTTP 200。
  - `/v1/messages` + `glm-chat` + 图片输入：HTTP 200。
  - `/v1/responses` + `deepseek-chat` + 图片输入：HTTP 200。
  - `/v1/messages` + `deepseek-chat` + 图片输入：HTTP 200。

## 已知未完成

完整 `python tests/smoke_test.py` 在早期既有断言 `Responses completion-only text should be emitted as a final delta` 处失败，未进入本次新增用例；本次未修改该无关问题。
