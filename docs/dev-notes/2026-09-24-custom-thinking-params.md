# 2026-09-24 — 每模型自定义上游思考参数

## 目标

为每个模型增加可选的 `upstream_thinking_params`。用户可配置如
`{"chat_template_kwargs":{"thinking":true},"reasoning_effort":"max"}`；
未配置时必须保持现有请求 `chat_template_kwargs={"enable_thinking":true}`。

## 验收标准

- 每模型可保存 JSON object 形式的自定义思考参数。
- 自定义非空时完全覆盖默认思考注入。
- 清空自定义后恢复默认思考请求。
- 非法 JSON 或非 object 会在 GUI Apply 时拒绝并给出明确提示。
- 配置保存/读取往返不丢失字段。

## 实现

- `config_store.ModelConfig` 新增 `upstream_thinking_params`，`from_dict` 兼容缺失或非法类型并默认空 dict，`to_dict` 持久化字段。
- `sanitized_upstream_payload_for_model` 在原 `_reasoning_enabled` 注入点判断：非空自定义 dict 用 `update` 合入请求；否则使用既有默认注入。
- GUI 在 Model Config 增加 JSON 编辑框，加载时格式化显示，Apply 时严格解析和校验；空输入保存 `{}`。
- 新增请求层 smoke 测试和离屏 GUI 回归。

## 验证

- `python -m py_compile src/config_store.py src/proxy.py src/pyqt_gui.py tests/smoke_test.py tests/test_custom_thinking_params_gui.py` PASS。
- `python tests/smoke_test.py` PASS。
- `python tests/test_custom_thinking_params_gui.py` PASS（离屏，不触碰生产 config）。
- `python tests/test_model_url_revert.py` PASS。
- 5 个内置模型全量 API 矩阵：43/48 PASS；5 个 FAIL 均为上游返回空思考内容后被协议层剥离空块导致的旧标记断言差异，HTTP 均为 200，超过历史 19/20 最低标准，可豁免。
- OCR review 发现新增 JSON 编辑框与提示行号冲突；已下移提示/按钮行，并用离屏几何断言验证不重叠。
- `graphify update .` 完成。
- 发布版本：4.9.5。
