# Feat: POST /responses/input_tokens 本地估算 + DELETE /responses/{id} stub

## 目标

补齐 Codex CLI（OpenAI Responses API）新增的两个端点，使代理对 Codex 的端点覆盖完整：
1. `POST /v1/responses/input_tokens` — 本地 token 估算，返回 `{input_tokens, object}`，不消耗上游模型调用
2. `DELETE /v1/responses/{response_id}` — 返回 204 No Content（无状态代理，无需真实删除）

## 验收标准

- `POST /v1/responses/input_tokens` 返回 HTTP 200，body 为 `{"input_tokens": <int>, "object": "input_token_count"}`
- `DELETE /v1/responses/{id}` 返回 HTTP 204，无 body
- `python -m py_compile src/proxy.py` PASS
- `python tests/smoke_test.py` 不回归
- 不影响既有 /v1/messages、/v1/responses、/v1/responses/compact 等路由

## 影响范围

- `src/proxy.py`：`ProxyHandler.do_POST` 新增 input_tokens 路由；新增 `do_DELETE` 方法

## 风险评估

- 低。input_tokens 纯本地估算，不触达上游。DELETE 返回 204 stub，无状态副作用。
- 上游 GenAI 网关仅支持 POST（GET/DELETE 返回业务层 405），故 DELETE 只能 stub。
- 上游 /responses/input_tokens 未实现（回退成普通 /responses 调用），故本地估算是正确选择。

## 背景（实测结论）

直接调用上游 `https://genaiapi.shanghaitech.edu.cn/api/v1/response`：
- `POST /responses/input_tokens` → HTTP 200 但返回完整 Response 对象（含模型回复），非 token 计数 → 上游未实现此端点
- `DELETE /responses/{id}` → HTTP 200，body `{success:false, message:"不支持DELETE请求方法", code:405}` → 上游不支持 DELETE
- `GET /responses/{id}` → 同上 405 → 代理已 stub 为 404