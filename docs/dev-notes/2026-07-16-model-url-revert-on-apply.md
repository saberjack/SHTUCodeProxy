# 2026-07-16 — 修复模型配置 Apply 后 Base URL 回退成校内 genai 默认值

## 目标

用户反馈：修改模型配置点 Apply 后，Base URL 总会变回校内 genai 默认 URL，
导致无法接入外部 URL。

## 验收标准

- 加载一个 API Format 与当前 combo 不同的模型时，已保存的 Base URL 必须原样保留。
- 点 Apply 后字段不回退；再次 Apply 不会把默认 URL 写回 config。
- 回归测试 `tests/test_model_url_revert.py` 在有修复时 PASS，撤掉修复时 FAIL。

## 根因

`src/pyqt_gui.py` 中 `api_format_combo.currentTextChanged` 连到 `on_api_format_changed`，
后者无条件把 `base_url_edit` 覆盖成 `DEFAULT_RESPONSES_URL` / `DEFAULT_CHAT_COMPLETIONS_URL`
（即 `https://genaiapi.shanghaitech.edu.cn/...`）。

`load_model` 先 `base_url_edit.setText(model.base_url)`（正确），再
`api_format_combo.setCurrentText(...)`。当被加载模型的 api_format 与 combo 当前值不同时，
`setCurrentText` 触发 `on_api_format_changed`，把 URL 覆盖成校内默认值。

触发链（点 Apply 时）：
1. `apply_model` 读字段存入 model（此时用户 URL 是对的）。
2. `apply_model` 调 `refresh_model_list()` —— 内部 `selectRow(0)` + `load_model(0)` 把 combo 设成模型0 的 format。
3. `apply_model` 再 `selectRow(current)` -> `on_model_table_selection_changed` -> `load_model(current)`。
4. 若 current 模型 format 与模型0 不同，`setCurrentText` 触发覆盖 -> 字段变 genai 默认 URL。
5. 用户以为没存上、再点一次 Apply -> 这次读到 genai URL -> `save_config` 写回 -> 配置真的回退。

默认配置模型0 是 GPT-5.5(responses)，故任何 chat_completions 模型(deepseek/glm/qwen)
改完点 Apply 都触发回退。

## 修复

`load_model` 中设置 combo 时临时 `blockSignals`，让 `on_api_format_changed` 只响应
用户手动切换下拉框，不在程序化加载时触发。3 行改动，不动 `on_api_format_changed` 本身逻辑。

## 验证

- 离屏 PyQt 构造真实 `IosProxyApp`，3 场景（跨 format 切换 / Apply 后 reload / Apply 往返）：
  - 撤修复 -> 3 场景全 FAIL，URL 变 `https://genaiapi.shanghaitech.edu.cn/api/v1/start`。
  - 加修复 -> 3 场景全 PASS，URL 保持外部值。
- 未启动生产 8082 进程，未触碰生产 config.json（mock 拦截 load/save_config）。

## 影响

仅 GUI 表单加载路径；用户手动切换 API Format 下拉框仍会填入对应默认 URL（保持原行为）。
