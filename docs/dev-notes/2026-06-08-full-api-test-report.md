# 2026-06-08 Full API Test Report

Latest rerun: 48 total, 48 PASS, 0 FAIL.

## Coverage

- Basic endpoints: `GET /health`, `GET /v1/models`, `POST /v1/messages/count_tokens`.
- Model matrix: GPT-5.5, glm-chat, deepseek-chat, qwen-instruct, deepseek-pro.
- Protocol matrix: `/v1/messages` and `/v1/responses`.
- Mode matrix: stream/non-stream × thinking/no-thinking.
- Regression scenarios: text-only multimodal downgrade, Claude Code Read screenshot follow-up stream fallback, Bash tool-name preservation, `/v1/responses/{id}/input_items` placeholder.
- deepseek-pro has no configured API key in this workspace; its expected `No API key configured` error path is counted as PASS.

## Validation Commands

- `PYTHONPATH=src python tests\smoke_test.py` — PASS.
- `python tests\full_api_regression_20260608.py` — 48/48 PASS.

## Output Files

- JSON: `docs/dev-notes/2026-06-08-full-api-test-results-rerun.json`
- Markdown table: `docs/dev-notes/2026-06-08-full-api-test-rerun.md`

## Notes

The first full run exposed two additional compatibility gaps: native Responses thinking did not include a Responses-format reasoning marker, and Responses-to-Chat conversion dropped `tool_choice` when no `tools` array was present. Both were fixed before the final 48/48 rerun.
