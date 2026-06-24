# SHTUCodeProxy 问题追踪清单

> 本文件是项目中所有问题（Bug、需求、兼容性问题）的唯一追踪入口
> 每次提出问题或发现 Bug 必须在此登记，修复后必须更新状态
> 严禁口头报 Bug 不记录、修了 Bug 不更新

## 状态定义

| 状态 | 含义 |
|------|------|
| 🔴 待处理 | 已登记，尚未开始排查 |
| 🟡 排查中 | 已复现或正在定位根因 |
| 🔵 修复中 | 根因已明确，正在编码修复 |
| 🟢 已修复 | 代码已修改并验证通过 |
| ⚪ 已关闭 | 确认不再复现，或标记为 won't fix / by design |

## 优先级定义

| 优先级 | 含义 | 响应要求 |
|--------|------|----------|
| P0 | 阻塞核心功能 | 立即处理，Hotfix 流程 |
| P1 | 重要功能异常 | 当次迭代内修复 |
| P2 | 体验/改进 | 排入后续迭代 |

---

## 问题清单

### #001 — Claude Code auto mode 不识别模型支持 extended thinking

| 字段 | 值 |
|------|-----|
| **标题** | Claude Code auto mode 不识别模型支持 extended thinking |
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-06 |
| **修复日期** | 2026-06-06 |
| **发现人** | 用户反馈 |
| **影响范围** | Claude Code + 所有通过 chat_completions 格式上游的模型 |
| **现象** | 当请求包含 thinking:{type:enabled} 时，上游模型不支持 extended thinking，Claude Code 无法识别模型具有 thinking 能力，auto mode 不能正常工作 |
| **根因** | 上游不支持 thinking 参数，proxy 将其 strip 后未在响应中注入 thinking block，导致 Claude Code 无法识别模型支持 extended thinking |
| **修复提交** | 待提交 (dev/fix-thinking-block-injection) |
| **开发记录** | docs/dev-notes/2026-06-06-thinking-block-injection.md |
| **回归测试** | 单元测试 5/5 PASS + 端到端 4模型×4场景 15/16 PASS (qwen-instruct流式空文本为已知上游问题) |


### #002 — 无多模态模型收到图片请求时代理返回 HTTP500

| 字段 | 内容 |
|------|------|
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-07 |
| **修复日期** | 2026-06-07 |
| **发现人** | 用户 |
| **影响范围** | Claude Code / Codex 自动触发图片输入；GLM、DeepSeek 等无多模态模型 |
| **现象** | 客户端自动发送多模态请求时，上游模型不支持图片，当前 proxy 直接返回 HTTP500 |
| **根因** | /v1/messages 与 /v1/responses 入口在检测到当前用户多模态输入时直接本地阻断，且最终清洗器会删除图片块；这与 cc-switch 的 media_sanitizer 文本占位降级策略不一致 |
| **修复提交** | 待提交 |
| **开发记录** | docs/dev-notes/2026-06-07-multimodal-text-only-downgrade.md |
| **回归测试** | python -m py_compile src/proxy.py tests/smoke_test.py；目标多模态 smoke 通过；真实 API：GLM/DeepSeek × /v1/responses,/v1/messages 图片请求均 HTTP 200 |
### #003 — Claude Code Read 截图后 GLM 上游 HTTP500

| 字段 | 内容 |
|------|------|
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-07 |
| **修复日期** | 2026-06-07 |
| **发现人** | 用户 |
| **影响范围** | Claude Code + glm-chat；浏览器截图后 Read 图片识别链路 |
| **现象** | Claude Code 通过 kimi-webbridge 截图保存 PNG 后执行 Read 图片，随后代理返回 `[Proxy Error] Upstream HTTP 500` |
| **根因** | Chat Completions 原生流式在含大截图 Read 工具历史的请求上返回上游 500；同一清洗后的非流式请求可成功。同时自动 cache_control 可能误改 Chat tool 消息 content 形态，增加上游兼容风险。 |
| **修复提交** | 待提交 |
| **开发记录** | docs/dev-notes/2026-06-07-stream-read-image-fallback.md |
| **回归测试** | 目标 smoke 通过；1.2MB 截图 Read tool_result + snapshot 多轮历史 + glm-chat + stream=true 复现请求由非流式 fallback 恢复为 HTTP 200 SSE |
### #004 — GLM 反复调用 shell 而非 Claude Code Bash 工具

| 字段 | 内容 |
|------|------|
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-08 |
| **修复日期** | 2026-06-08 |
| **发现人** | 用户 |
| **影响范围** | Claude Code + glm-chat；模型生成工具调用时使用 `shell` 而实际可用工具为 `Bash` |
| **现象** | 500 修复后，模型反复说明自己应调用 Bash，但持续输出 `shell` 工具名，导致 Claude Code 工具调用循环失败 |
| **根因** | Chat Completions 上游返回的工具名未按当前请求工具列表映射回 Claude Code 的真实工具名；并且中间 function_call 规范化会把 `Bash` 再转成 Codex Responses 的 `shell` 名称。 |
| **修复提交** | 待提交 |
| **开发记录** | docs/dev-notes/2026-06-08-claude-bash-tool-name-normalization.md |
| **回归测试** | 目标 smoke 验证：上游 `shell` tool_call 在 Claude Code Anthropic 输出中映射为 `Bash` |

### #005 — 原生 Responses thinking 响应缺少 reasoning 标记

| 字段 | 内容 |
|------|------|
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-08 |
| **修复日期** | 2026-06-08 |
| **发现人** | 全量 API 自测 |
| **影响范围** | Codex `/v1/responses` + GPT-5.5 / chat_completions 转 Responses 路径；thinking enabled 场景 |
| **现象** | `/v1/responses` 请求 thinking enabled 时可返回 HTTP 200，但 Responses 输出缺少 reasoning / encrypted_content 标记，不能覆盖 Codex thinking 能力探测语义 |
| **根因** | 之前只在 Anthropic Messages 响应中注入 `redacted_thinking`，未给 OpenAI Responses 格式补 synthetic reasoning output item |
| **修复提交** | 待提交 |
| **开发记录** | docs/dev-notes/2026-06-08-full-api-test-rerun.md |
| **回归测试** | `python tests\full_api_regression_20260608.py`：48/48 PASS；覆盖 GPT-5.5、glm-chat、deepseek-chat、qwen-instruct、deepseek-pro × messages/responses × stream/thinking |

### #006 — Responses tool_choice 在无 tools 时被静默丢弃

| 字段 | 内容 |
|------|------|
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-08 |
| **修复日期** | 2026-06-08 |
| **发现人** | smoke 回归测试 |
| **影响范围** | Codex `/v1/responses` 转 Chat Completions；带 `tool_choice` 但请求未显式带 `tools` 的边界场景 |
| **现象** | `responses_request_to_chat_completions()` 只有在 tools 非空时才转换 `tool_choice`，导致函数选择约束丢失 |
| **根因** | `tool_choice` 转换被错误绑定到 tools 列表存在条件；确定性字段转换不应依赖 tools 是否非空 |
| **修复提交** | 待提交 |
| **开发记录** | docs/dev-notes/2026-06-08-full-api-test-rerun.md |
| **回归测试** | `PYTHONPATH=src python tests\smoke_test.py` PASS；`python tests\full_api_regression_20260608.py` 48/48 PASS |


### #007 — Claude Code auto mode safety classifier 走 qwen-instruct 失败

| 字段 | 内容 |
|------|------|
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-11 |
| **修复日期** | 2026-06-11 |
| **发现人** | 用户 |
| **影响范围** | Claude Code CLI auto mode；需要自动判定 Bash 等工具安全性的场景 |
| **现象** | Claude Code CLI auto mode 执行 Bash 前报错：`qwen-instruct is temporarily unavailable, so auto mode cannot determine the safety of Bash right now` |
| **根因** | Claude Code auto classifier 是普通非流式 Anthropic Messages 调用；代理按全局 default_stream 强制转 SSE，且自动 thinking 注入会让 qwen 输出 reasoning 而非严格 `<block>` 判定，导致 Claude classifier 解析 `usage.input_tokens` 时崩溃并把 qwen 标记为 unavailable。 |
| **修复提交** | 待提交 |
| **开发记录** | docs/dev-notes/2026-06-11-claude-auto-classifier-qwen.md |
| **回归测试** | `python -m py_compile src\proxy.py tests\smoke_test.py` PASS；targeted tests PASS；测试端口 8097 + Claude Code CLI 2.1.172 + qwen-instruct + `--permission-mode auto` 可执行安全 Bash 检查；完整 smoke 存在既有 cache-control 断言失败，未作为本次修复范围。 |

### #001 — 示例条目（请删除）

| 字段 | 值 |
|------|-----|
| **标题** | Codex /compact 返回空流 |
| **状态** | 🟢 已修复 |
| **优先级** | P0 |
| **发现日期** | 2026-06-05 |
| **修复日期** | 2026-06-05 |
| **发现人** | 用户反馈 |
| **影响范围** | Codex CLI + glm-chat 模型 |
| **现象** | `/compact` 命令返回 "stream disconnected before completion" |
| **根因** | 1) tool_choice: auto + 空 tools 2) content: None 3) thinking 参数穿透 4) tool_choice dict 未转 string |
| **修复提交** | c55db0c, 73507db, 785a0f4 |
| **开发记录** | docs/ITERATION-LOG-v4.5.md |
| **回归测试** | 23/23 PASS |

---

<!-- 新问题模板：复制以下块，填入实际内容 -->

<!--

### #NNN — <标题>

| 字段 | 值 |
|------|-----|
| **标题** | |
| **状态** | 🔴 待处理 |
| **优先级** | P? |
| **发现日期** | YYYY-MM-DD |
| **修复日期** | |
| **发现人** | |
| **影响范围** | |
| **现象** | |
| **根因** | _排查后填写_ |
| **修复提交** | _修复后填写_ |
| **开发记录** | docs/dev-notes/YYYY-MM-DD-<主题>.md |
| **回归测试** | _验证后填写_ |

-->

### #008 — Codex Responses API 新增端点 input_tokens / DELETE 未实现

| 字段 | 值 |
|------|-----|
| **标题** | Codex Responses API 新增端点 input_tokens / DELETE 未实现 |
| **状态** | 🟢 已修复 |
| **优先级** | P2 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | API 审计 |
| **影响范围** | Codex CLI `/v1/responses/input_tokens` 与 `DELETE /v1/responses/{id}` |
| **现象** | 官方 OpenAI Responses API 新增 POST /responses/input_tokens 和 DELETE /responses/{id} 端点，代理未实现；input_tokens 返回 404，DELETE 无 do_DELETE 方法 |
| **根因** | 代理路由表未覆盖这两个新增端点；实测上游 GenAI 网关仅支持 POST（GET/DELETE 返回业务层 405），且 /responses/input_tokens 上游未实现（回退成普通 /responses 调用） |
| **修复提交** | 待提交 (dev/feat-input-tokens-and-delete) |
| **开发记录** | docs/dev-notes/2026-06-22-responses-input-tokens-and-delete.md |
| **回归测试** | py_compile PASS；端到端 8099 端口 4/4 PASS（input_tokens 返回 {input_tokens,object}、DELETE 返回 204、404 兜底）；既有 cache-control smoke 断言失败为已知历史问题，非本次引入 |

### #009 — src/VERSION 落后发布版本导致更新误报

| 字段 | 值 |
|------|-----|
| **标题** | src/VERSION 落后发布版本导致更新误报 |
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | 代码审计 |
| **影响范围** | updater.current_version() / check-update |
| **现象** | src/VERSION 停在 4.8.2，根 VERSION 为 4.8.4；运行 4.8.4 构建却报告 v4.8.2，触发虚假更新提示 |
| **根因** | current_version() 优先读 src/VERSION，发布时未同步 |
| **修复** | src/VERSION 与根 VERSION 同步至 4.8.5 |
| **回归测试** | current_version() 返回 4.8.5 |

### #010 — CORS 预检未声明 DELETE

| 字段 | 值 |
|------|-----|
| **标题** | do_OPTIONS 未声明 DELETE，新增 do_DELETE 对 CORS 客户端不可达 |
| **状态** | 🟢 已修复 |
| **优先级** | P1 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | 代码审计 |
| **影响范围** | DELETE /v1/responses/{id} |
| **现象** | OPTIONS 返回 GET,POST,OPTIONS，浏览器预检拒绝 DELETE |
| **根因** | do_OPTIONS 方法列表未随 do_DELETE 更新 |
| **修复** | access-control-allow-methods 改为 GET,POST,DELETE,OPTIONS |
| **回归测试** | OPTIONS 预检返回含 DELETE |

### #011 — 超大请求体双重响应破坏 keep-alive

| 字段 | 值 |
|------|-----|
| **标题** | input_tokens/count_tokens 超大 body 触发 413 后再发 200 |
| **状态** | 🟢 已修复 |
| **优先级** | P2 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | 代码审计（实测复现） |
| **影响范围** | POST /v1/responses/input_tokens、/v1/messages/count_tokens |
| **现象** | read_json_body 发 413 并抛 _BodyTooLargeError，except Exception 捕获后再发 200；日志同请求出现 413+200，后续连接出现 414 URI Too Long |
| **根因** | 宽泛 except 捕获了已响应的 _BodyTooLargeError 并继续发送 |
| **修复** | 捕获 _BodyTooLargeError 直接 return；并设置 close_connection=True 避免未读 body 污染下一请求 |
| **回归测试** | 超大请求仅 413，连接被干净关闭 |

### #012 — 自动缓存对 Responses 载荷无效

| 字段 | 值 |
|------|-----|
| **标题** | apply_auto_cache_control 未分发 Responses 载荷，生产自动缓存为空操作 |
| **状态** | 🟢 已修复 |
| **优先级** | P2 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | 代码审计 |
| **影响范围** | GPT-5.5 Responses 请求自动缓存 |
| **现象** | apply_auto_cache_control 仅处理 messages 载荷，Responses（input）直接 return 0；而生产网关仅对非 chat 格式调用，导致 Responses 从未被缓存 |
| **根因** | 缺少 input 字段分发到 apply_auto_cache_control_to_responses_payload |
| **修复** | 增加 input 分发分支；smoke 改为测试真实生产路径 |
| **回归测试** | Responses 载荷自动缓存 marks >= 2 |

### #013 — do_DELETE 未消费请求体

| 字段 | 值 |
|------|-----|
| **标题** | do_DELETE 未读取请求体，keep-alive 残留字节污染 |
| **状态** | 🟢 已修复 |
| **优先级** | P3 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | 代码审计 |
| **影响范围** | DELETE /v1/responses/{id} |
| **现象** | 带 Content-Length 的 DELETE 不读 body，残留字节污染下一请求 |
| **修复** | do_DELETE 开头按 content-length 读取并丢弃 body |
| **回归测试** | 带 body 的 DELETE 返回 204 且不污染连接 |

### #014 — 剥离全部工具后 tool_choice 孤立

| 字段 | 值 |
|------|-----|
| **标题** | _strip_unsupported_tools 删除全部工具后未清 tool_choice |
| **状态** | 🟢 已修复 |
| **优先级** | P3 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | 代码审计 |
| **影响范围** | sanitized_upstream_payload_for_model |
| **现象** | 剥离全部不支持工具后 del tools，但 tool_choice 残留，可能触发上游 400 |
| **修复** | del tools 时同步 pop tool_choice |
| **回归测试** | 代码审查确认；主路径非函数工具在转换阶段已丢弃，属防御性加固 |

### #015 — 非流式 Responses JSON 转 Anthropic 顺序颠倒

| 字段 | 值 |
|------|-----|
| **标题** | responses_json_to_anthropic_message 输出 [tool_use, text] 而非 [text, tool_use] |
| **状态** | 🟢 已修复 |
| **优先级** | P2 |
| **发现日期** | 2026-06-22 |
| **修复日期** | 2026-06-22 |
| **发现人** | smoke 暴露 |
| **影响范围** | 非流式 Responses JSON → Anthropic 消息转换 |
| **现象** | function_call 在循环内立即 append，text 在循环后 append，导致顺序颠倒 |
| **修复** | function_call 收集到 _tool_use_items，text 之后再 extend |
| **回归测试** | content 顺序为 [text, tool_use] |

---

## 统计

| 优先级 | 🔴 待处理 | 🟡 排查中 | 🔵 修复中 | 🟢 已修复 | ⚪ 已关闭 |
|--------|-----------|-----------|-----------|-----------|-----------|
| P0     | 0         | 0         | 0         | 1         | 0         |
| P1     | 0         | 0         | 0         | 9         | 0         |
| P2     | 0         | 0         | 0         | 3         | 0         |
| **合计** | **0**     | **0**     | **0**     | **14**    | **0**     |

> 最后更新: 2026-06-22（审计修复）