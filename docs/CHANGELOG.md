## v4.6.0 (2026-06-06)

### Fixed

- **P1**: Fixed Claude Code auto mode not recognizing model supports extended thinking. When client sends `thinking: {type: enabled}`, the proxy now injects a synthetic `redacted_thinking` content block at the beginning of the response, enabling Claude Code to correctly identify the model as supporting extended thinking. The `thinking` parameter is still stripped before sending to upstream (which doesn't support it), but a `_thinking_requested` flag is preserved through the request-response cycle to trigger response-side injection. Both streaming and non-streaming Anthropic Messages paths are covered.

## v4.5.1 (2026-06-05)

Fix Responses API tool_choice bug and add missing Codex route handlers.

### Fixed

- **P0**: Fixed `tool_choice: {"type": "auto"}` from Responses API not converted to string `"auto"` for chat_completions upstream. The `responses_tool_choice_to_chat()` function now correctly maps `{"type": "auto"}` -> `"auto"`, `{"type": "none"}` -> `"none"`, `{"type": "required"}` -> `"required"`. Previously these dict values were passed through unchanged, causing 502 errors from glm-chat, deepseek-pro, deepseek-chat, and qwen-instruct upstreams when Codex sends tool_choice as a dict object. The Messages API path (`/v1/messages`) was not affected since `anthropic_tool_choice_to_openai()` already handled this correctly.
- **P1**: Added `/codex/v1/responses` route alias. Codex Desktop sometimes sends requests to `/codex/v1/responses` instead of `/v1/responses`, causing 404 errors. Now both paths are supported.
- **P1**: Added `POST /v1/responses/{id}/input_items` handler. Codex sends this to append follow-up inputs to an existing response. Since the proxy is stateless, returns a minimal completed response to prevent Codex from breaking.
- **P1**: Added `POST /v1/responses/{id}/cancel` handler. Codex sends this to cancel in-progress responses. Returns a cancelled status response.
- **P1**: Added `GET /v1/responses/{id}` handler. Returns proper 404 with "stateless proxy" message instead of generic error.
- **P2**: Updated GLM default context window from 131072 to 200000 to match actual model capability.

## v4.5.0 (2026-06-05)

Codex `/compact` support, context window limits, and glm-chat compatibility fixes.

### Added

- Added Codex `/v1/responses/compact` endpoint handling: for `responses` format upstreams, forward compact requests directly; for `chat_completions` format upstreams, convert via `responses_request_to_chat_completions`, send to upstream, and convert the chat response back to Responses compact format.
- Added `max_context_tokens` field to `ModelConfig` for per-model context window limits, persisted in config and exposed in `/v1/models` API response.
- Added automatic `model_context_window` and `model_auto_compact_token_limit` (90% of context window) injection into Codex `config.toml` based on the selected model's `max_context_tokens`.
- Added `CLAUDE_CODE_MAX_CONTEXT_TOKENS` environment variable injection in Claude CLI launch config based on the selected model.
- Added seed of all 5 builtin model routes (deepseek-pro, GPT-5.5, glm-chat, deepseek-chat, qwen-instruct) with sensible defaults including context window sizes.
- Added `_response_is_sse` helper for detecting SSE responses from upstream.
- Added fallback tools generation in both `responses_request_to_chat_completions` and `anthropic_messages_to_chat_completions`: when messages contain `tool_calls` but the payload lacks a `tools` field, minimal tool definitions are generated from the tool names to satisfy upstream API requirements (especially glm-chat).

### Fixed

- **P0**: Fixed Codex `/compact` command failing with `stream disconnected before completion` when using glm-chat: the proxy now correctly routes compact requests and handles the chat_completions format conversion round-trip.
- **P0**: Fixed `'NoneType' object is not iterable` BadRequestError from glm-chat: assistant messages with `tool_calls` now use `content: ""` instead of `content: None`, which glm-chat cannot parse.
- **P0**: Fixed empty stream responses on non-Anthropic upstreams caused by `thinking` parameter passthrough. Stripped `thinking` parameter in request sanitization to prevent `Unknown parameter: 'thinking'` errors on GPT-5.5, GLM, etc.
- **P1**: Fixed `tool_choice: auto` being sent when `tools` list is empty, causing upstream API rejection. `tool_choice` is now only included when `tools` is non-empty.
- **P1**: Fixed `use_model` command not writing Claude settings or Codex config files after switching models, causing config drift until manual restart.
- **P1**: Fixed consecutive `function_call` items producing separate assistant messages, violating Chat Completions API conventions. Now batched into a single assistant message with multiple `tool_calls`.
- **P1**: Fixed consecutive same-role messages (`user` or `assistant`) causing API rejections from strict upstreams. Added post-processing to merge them.
- **P1**: Fixed user-customized `model_context_window` and `model_auto_compact_token_limit` being overwritten on Save+Connect. Now preserved when model is unchanged.
- **P2**: Fixed GLM default context window from 131072 to 200000 to match actual model capability.
- **P2**: Fixed `max_context_tokens` not auto-inferred for known models when missing from config, causing Codex config.toml to lack context window values.

### Changed

- Updated default context window values: GPT-5.5=400000, deepseek-chat=192000, deepseek-pro=128000, glm-chat=200000, qwen-instruct=131072.
- Updated `config.example.json` with all 5 builtin models, default model set to `glm-chat`, and `codex_sandbox_mode` preset.
- GUI model editor now preserves `max_context_tokens` and `stream_bridge` fields that are not exposed in the GUI.

## v4.4.0 (2026-05-25)

- Fix: apply_auto_cache_control now works for Responses format payloads (Codex)
- Fix: stop_reason_from_done correctly extracts stop_reason from response.completed
- Fix: add Qwen/GLM non-stream bridge to Anthropic Messages streaming endpoint
- Fix: add /v1/models GET endpoint for Claude Code startup compatibility
- New: ModelConfig.stream_bridge config field replaces hardcoded Qwen detection
- Refactor: move source modules into src/ directory
- Refactor: move build scripts to build/, docs to docs/, tests to tests/
- Docs: add PROJECT-INDEX.md and CHANGELOG-v4.3.3.md

# Changelog
## v4.3.2 - 2026-05-20

Model-level multimodal capability guard release.

### Added

- Added per-model `supports_image`, `supports_audio`, and `supports_video` capability flags in GUI, saved config, and headless Linux config files.
- Defaulted GPT-5.5 and qwen-instruct to image-capable, while GLM and DeepSeek chat routes remain text-only by default.

### Fixed

- Blocked unsupported image, audio, and video requests before they reach incompatible upstream models.
- Returned protocol-compatible assistant messages for unsupported multimodal requests on both `/v1/messages` and `/v1/responses`, including streaming and non-streaming modes.
- Preserved image passthrough for routes explicitly marked as image-capable, and left file/document passthrough unchanged.
- Parsed string boolean config values such as `"false"` correctly for manually edited headless config files.

### Verified

- Verified qwen-instruct image URL and base64 image requests against the real upstream API.
- Verified GLM multimodal requests are blocked locally and subsequent text requests still work.
- Re-tested smoke coverage and Python syntax checks.

## v4.3.1 - 2026-05-19

Codex qwen-instruct empty-response hotfix.

### Fixed

- Fixed qwen-instruct on Codex /v1/responses streaming where qwen's long reasoning stream could leave Codex with last_agent_message = null and no assistant output.
- Fixed Codex qwen-instruct requests that include later system/developer messages by merging all system-level instructions into the first Chat Completions system message.
- Kept GLM and DeepSeek on their original upstream streaming path; the bridge is scoped to qwen-style Chat Completions routes only.

### Verified

- Verified qwen-instruct Codex-style 你是谁 now emits a valid Responses assistant message.
- Verified qwen-instruct with real Codex CLI no longer fails with "System message must be at the beginning".
- Re-tested GLM, DeepSeek, and qwen-instruct streaming tool-call paths.
- Re-tested qwen-instruct Claude Code /v1/messages streaming path.


## v4.3.0 - 2026-05-19

Streaming default, GPT-5.5 Responses compatibility, and multimodal validation release.

### Added

- Added a configurable default_stream setting. Requests still honor explicit stream=true or stream=false; the setting only applies when the client omits stream.
- Added GUI, CLI, and headless config support for the default streaming behavior.

### Fixed

- Fixed non-streaming /v1/responses with Responses upstreams so it returns Responses JSON instead of Anthropic message JSON.
- Fixed non-streaming /v1/messages with Responses upstreams so it returns Anthropic message JSON instead of raw Responses JSON.
- Preserved structured image, file, audio, and document content for compatible upstream routes instead of converting supported multimodal parts to plain text.

### Verified

- Re-tested GLM, DeepSeek, Qwen, and GPT-5.5 across streaming, non-streaming, tool calls, multi-turn tool results, and multimodal image input where supported.


## v4.2.15 - 2026-05-18

Runtime config and instance-safety fixes.

### Fixed

- `apply-config --start` now restarts an already running background proxy so model, key, host, and port changes take effect immediately.
- Added CLI warnings when config is changed while a background proxy is still running without restart.
- Added process-safe config writes and background start/stop locking to reduce conflicts from multiple app or CLI instances.
- GUI saves now refresh the running proxy config and restart the listener automatically when host or port changes.
- Non-Windows Codex config writes no longer preserve or create the Windows-only `[windows]` section.

## v4.2.14 - 2026-05-18

Proxy robustness fixes.

### Fixed

- Added UUID entropy to generated Anthropic and Responses IDs to avoid collisions under concurrent requests.
- Added true non-streaming Responses JSON conversion before falling back to SSE streaming assembly.
- Made SSE socket read timeouts fail loudly instead of hanging silently.
- Improved split `<think>` tag handling across streaming chunks.

## v4.2.13 - 2026-05-18

Linux headless release packaging.

### Added

- Added a Linux headless CLI zip bundle containing the no-GUI executable, `headless-config.example.json`, and an editable `config.json` copy.
- Updated release notes and README for the Linux no-GUI startup workflow.

### Fixed

- Confirmed Linux tool-call handling preserves `exec_command(cmd)` and Windows shell wrappers remain unchanged.

## v4.2.12 - 2026-05-18

Cross-platform tool-call matrix coverage.

### Fixed

- Added regression coverage for explicit tool calls, implicit pseudo tool calls, multi-tool switching, multi-turn tool results, and Windows/Linux command wrappers.
- Verified `exec_command(cmd)` is preserved while shell aliases still map to platform-specific command wrappers.

## v4.2.11 - 2026-05-18

Linux command tool schema fix.

### Fixed

- Preserved Codex `exec_command` tool calls and their `cmd` parameter instead of rewriting them to a non-existent `shell` tool.
- Updated tool guidance to require exact provided tool names and avoid inventing `shell` when the client provides `exec_command`.

## v4.2.10 - 2026-05-18

Headless background start reliability fix.

### Fixed

- Fixed Linux packaged CLI background startup by resetting the PyInstaller onefile child environment before re-execing `serve`.
- `start` now waits for the configured port to actually listen before reporting success, and prints recent proxy logs if startup fails.

## v4.2.9 - 2026-05-18

Expanded headless model template.

### Changed

- Expanded `headless-config.example.json` to show `deepseek-pro`, `deepseek-chat`, `glm-chat`, `qwen-instruct`, and `GPT-5.5` in one file.
- Documented how Claude multi-route model variables and the Codex single selected model are chosen in headless mode.

### Fixed

- `apply-config` now rejects unknown Claude model route IDs before writing config.

## v4.2.8 - 2026-05-18

Headless config-file workflow.

### Added

- Added `apply-config` for loading headless JSON config files and optionally writing client configs plus starting the proxy.
- Added `headless-config.example.json` for Linux server users who prefer editing one file instead of typing long commands.

## v4.2.7 - 2026-05-18

Linux shell and headless CLI fix.

### Added

- Added headless CLI commands for model setup, model switching, background proxy start, stop, and status checks.

### Fixed

- Fixed Linux/macOS shell tool conversion so proxy-generated shell calls use `bash -lc` instead of Windows PowerShell.
- Allowed packaged Linux binaries to run CLI commands without requiring a GUI display server.

## v4.2.6 - 2026-05-14

Source package entry-point fix.

### Fixed

- Replaced the stale Tkinter `gui.py` preview entry point with a PyQt5 compatibility launcher so source package users no longer see the old `4.0.4 Preview` interface.
- Avoided the slower legacy GUI path when users run `python gui.py` from the source package.

## v4.2.5 - 2026-05-14

Linux package runtime dependency fix.

### Fixed

- Bundled Linux OpenGL/Qt runtime libraries needed by PyQt5, including `libGL.so.1`, to avoid startup failures on minimal Linux desktops.
- Expanded Linux release workflow system libraries so generated packages include the expected Qt/XCB runtime dependencies.

## v4.2.4 - 2026-05-14

Model routing persistence fix.

### Fixed

- Preserved Model Routing and Codex Model selections when clicking Save Config after editing the selected model.
- Kept route selections synchronized when a model ID is renamed, instead of letting the model list refresh revert to stale saved values.

## v4.1.1 - 2026-05-13

GUI interaction polish release.

### Fixed

- Kept Advanced / Optional action buttons in the right-side area while aligning them vertically in the compact card.
- Prevented combo boxes from changing values on mouse wheel unless the dropdown list is explicitly open.

## v4.1.0 - 2026-05-13

Stable PyQt5 iOS-style GUI release.

### Changed

- Prevented combo boxes from changing values on mouse wheel unless the control is focused or opened.
- Tightened the Advanced / Optional card and improved button alignment.
- Switched Models to a table-style view with headers, row selection, grid lines, and alternating rows.
- Replaced the application icon with the transparent `myImage2` icon.
- Promoted the accepted PyQt5 GUI preview to the stable 4.1.0 release.

## v4.0.5-preview - 2026-05-13

iOS-style PyQt5 GUI preview.

### Changed

- Rebuilt the preview GUI with PyQt5 while preserving the existing SHTUCodeProxy workflow.
- Added iOS-inspired QSS styling with system gray background, metallic navigation bar, glass cards, rounded controls, and blue-white accent colors.
- Added real `QGraphicsDropShadowEffect` shadows on cards and focus glow effects on text inputs.

## v4.0.4-preview - 2026-05-13

Metallic Apple-inspired GUI preview refinement.

### Changed

- Updated the preview to SHTUCodeProxy 4.0.4.
- Switched the preview canvas to pure white and tuned the surrounding palette to pearl white, soft blue, and silver.
- Reduced gray-looking controls by using blue-tinted comboboxes plus raised white/silver button surfaces with stronger relief.

## v3.5.1 - 2026-05-13

User safety and recovery workflow release.

### Added

- Added a red warning confirmation for Codex `danger-full-access` sandbox mode.
- Added connection status showing whether the configured port is not listening, externally listening, or started by this app.
- Added recovery buttons for restoring the most recent client backup or the original client config for both Claude Code and Codex modes.
- Added Codex health check for TOML syntax, auth mode, provider/profile, hooks, sandbox, project entries, and MCP server preservation.

### Fixed

- Captured original client config snapshots before first managed writes, including the case where the original file did not exist.
- Improved the GUI workflow so users can see connection state and recover broken client config without manually searching backup files.

## v3.2.25 - 2026-05-13

Codex config preservation hardening release.

### Fixed

- Preserved unmanaged Codex root settings, MCP server blocks, unrelated profiles, project trust entries, and other Codex tables while still replacing the SHTU proxy provider/profile cleanly.
- Added recovery for partially corrupted config files where stale root `model` or `model_provider` lines had been inserted under unrelated tables.
- Preserved non-deprecated feature flags and Windows settings while migrating `codex_hooks` to `hooks`.

## v3.2.24 - 2026-05-13

Codex sandbox configuration release.

### Added

- Added a Codex Sandbox selector next to Codex Model with `read-only`, `workspace-write`, and `danger-full-access` options.

### Fixed

- Migrated deprecated `[features].codex_hooks` to `[features].hooks` and removed stale `codex_hooks` entries during config rewrites.
- Preserved idempotent Codex config writes so repeated Save Config, Write Client Config, or Save + Connect clicks do not duplicate root keys, feature flags, provider blocks, or profiles.

## v3.2.23 - 2026-05-12

Release packaging consistency update.

### Changed

- Restored the full five-asset release packaging convention: Windows single-file EXE, Windows portable ZIP, Linux/macOS source ZIP, release README, and SHA256 checksums.
- Versioned the Windows portable ZIP asset so each GitHub release contains self-contained package names.

### Security

- Source release packaging only copies git-tracked files and excludes local config files, API keys, backups, probe outputs, and build directories.

## v3.2.22 - 2026-05-12

Codex Windows shell compatibility release.

### Fixed

- Matched CC switch behavior by preserving or forcing `[windows] sandbox = "elevated"` for Codex configs on Windows while keeping `sandbox_mode = "workspace-write"`.
- Normalized Codex shell tool aliases such as `shell_exec`, `exec_command`, and `bash` to `shell`.
- Converted shell string commands and bare argv commands such as `echo` into PowerShell argv arrays so Codex's Windows shell schema can execute them.

## v3.2.21 - 2026-05-12

Codex sandbox path case preservation release.

### Fixed

- Preserved original `[projects...]` TOML table headers exactly when rewriting Codex `config.toml`, including path case and quoting style.
- Added regression coverage for mixed-case sandbox/project paths so Codex authorization path matching is not broken by config rewrites.

## v3.2.20 - 2026-05-12

Dual-client tool-chain validation release.

### Added

- Added `claude_path_probe.py` to validate Claude Code `/v1/messages` with real multi-turn, multi-tool, and tool-switching scenarios.
- Expanded `api_deep_probe.py` to validate Codex `/v1/responses` multi-turn context, multiple tools, tool-result follow-up, streaming tool calls, and switching from file-read to directory-list tools.

### Verified

- Verified `glm-chat`, `deepseek-chat`, and `qwen-instruct` across Claude Code and Codex protocol paths using real upstream calls.
- Confirmed Claude Code returns standard Anthropic `tool_use` for implicit file-reading requests.
- Confirmed Codex returns Responses `function_call` for implicit file-reading, multi-tool, streamed-tool, and follow-up tool-result requests.

## v3.0.14 - 2026-05-12

Codex autonomous tool-call compatibility release.

### Fixed

- Added a short Chat Completions system hint telling local models to use native `tool_calls` instead of writing XML or pseudo tool-call text.
- Converted common pseudo tool-call formats (`<function>`, `<call>`, `<read_file>`, `<exec_command>`, `<Invoke>`, `<shell>`, and `<tool_invoke>`) into real Responses `function_call` outputs for Codex.
- Coerced pseudo shell commands into Codex's actual shell schema with `command` as an argument array, reducing unsupported-call and invalid-argument failures.
- Stripped pseudo `<tool_results>` blocks from model-visible text so fake tool results do not appear as final answers.

## v3.0.13 - 2026-05-12

Multi-round Codex tool-chain validation release.

### Fixed

- Added a visible `<tool_result>` fallback when converting Codex `function_call_output` history to Chat Completions messages, improving GLM's ability to use tool results across turns.
- Verified real proxy scenarios for normal chat, single tool call, multi-tool call, streamed tool call, and tool-result follow-up across `glm-chat`, `deepseek-chat`, and `qwen-instruct`.
- Added `api_deep_probe.py` to exercise Codex/MCP/skills-equivalent Responses tool flows against the local proxy with real upstream models.

## v3.0.12 - 2026-05-12

Codex tool-call forwarding fix release.

### Fixed

- Parsed per-model API keys from `D:\litellm\api.txt` so `glm-chat`, `deepseek-chat`, and `qwen-instruct` do not accidentally share the first GLM token.
- Fixed non-streaming Codex `/v1/responses` requests through Chat Completions routes to call upstream with `stream=false` and convert JSON `tool_calls` directly into Responses `function_call` output.
- Upstream JSON error responses now become proxy errors instead of silent successful Responses payloads with empty `output`.
- Verified real direct and proxied tool-call calls for `glm-chat`, `deepseek-chat`, and `qwen-instruct`, including streaming Responses events used by Codex.

## v3.0.11 - 2026-05-12

Codex config repair release.

### Fixed

- Improved config writing fallback for protected `.codex` paths and documented that writes must run outside the Codex sandbox identity.
- Built-in routes now include `glm-chat`, `deepseek-chat`, and `qwen-instruct` from `D:\litellm\api.txt` so Codex model switching has matching proxy routes.
- CLI `write-codex-config` now accepts `--model` for switching Codex model without opening the GUI.
- Codex config writing now preserves the selected `Codex Model` instead of forcing `glm-chat`; GLM remains an optional route.
- GUI and CLI server startup now try to stop an existing Windows listener on the configured port before starting a fresh proxy.
- Codex config writer now emits a clean minimal config instead of preserving stale sections that can make Codex ignore the custom provider/profile and fall back to `api.openai.com`.
- Config backup/write now falls back to the project `backups` directory when `.codex` ACLs deny same-directory backup or temp-file creation.
- Default Codex setup now switches to the local `shtu_proxy` provider with `glm-chat` routed through the proxy instead of leaving old direct `custom` provider settings in place.
- Added persisted `glm-chat` Chat Completions routing for the ShanghaiTech `/api/v1/start` endpoint when writing Codex setup.
- Verified GLM `/start` tool calls and added regression coverage for GLM streamed `tool_calls` argument merging.
- Codex `config.toml` writer now places root `model` and `model_provider` before the first TOML section instead of appending them into the last section.
- Repeated Save + Connect clicks now remove stale duplicated `shtu_proxy` model keys that older builds could leave inside sections such as `[tui.model_availability_nux]`.
- Config writers now validate generated JSON/TOML before replacing files and keep timestamped `.bak_*` backups of existing config files.
- Codex provider no longer writes `env_key`, avoiding startup failures when `OPENAI_API_KEY` is not set as a process environment variable.
- Codex `auth.json` now writes `auth_mode = apikey` alongside `OPENAI_API_KEY`, matching `codex login --with-api-key` output while preserving unrelated existing fields.

## v3.0.1 - 2026-05-12

Codex setup polish and GUI guidance release.

### Added

- Independent Codex model selection in GUI and config persistence.
- Codex root `model` and `model_provider` writing so users do not have to manually select a profile.
- Red warning in Model Config: GPT models should use `API Format = responses`.

### Fixed

- Codex `auth.json` now follows the selected Codex model instead of the Claude main route.
- Older configs missing Codex paths are normalized when the GUI starts.

## v3.0.0 - 2026-05-12

Codex compatibility release.

### Added

- Local OpenAI Responses-compatible `/v1/responses` endpoint for Codex CLI and Codex Desktop.
- Pass-through Codex Responses requests for Responses upstreams.
- Responses-to-Chat-Completions conversion for local models that only expose Chat Completions-compatible APIs.
- Codex `config.toml` example with `wire_api = "responses"`.
- GUI `Client Mode` switch for Claude Code vs Codex CLI/Desktop setup.
- Independent GUI `Codex Model` selector for `config.toml` profile and `auth.json` key selection.
- Codex `config.toml` writer that preserves unrelated config and replaces only the `shtu_proxy` provider/profile blocks.
- Codex `auth.json` writer with `OPENAI_API_KEY` from the selected upstream model key.
- Smoke coverage for multi-turn Codex context, multi-tool definitions, streamed/non-streamed function calls, `function_call_output` history, DSML/thinking filtering, and model routing.

### Changed

- The project now documents Claude Code and Codex as separate client protocols using the same model routing table while preserving the v2.0 Claude Code setup path.
- Version bumped to `3.0.0`.

## v2.0.0 - 2026-04-30

Tool-call hardening release for Claude Code compatibility.

### Added

- Broader smoke-test coverage for multiple tool calls, cumulative streamed arguments, model suffix routing, and tool argument repair.
- Estimated `/count_tokens` responses instead of a fixed zero count.

### Changed

- Hardened Chat Completions and Responses tool-call parsing for streamed and non-streamed upstream responses.
- Improved `tool_result` ordering and visible fallback context for Chat Completions-compatible upstreams.
- Claude model routing now accepts common date-suffixed model IDs.
- GPT-series models are documented to use the `responses` API Format.

### Fixed

- Chat Completions responses that include both `content` and `tool_calls` now prioritize tool calls instead of dropping them.
- Tool arguments wrapped in JSON strings, markdown fences, thinking tags, or cumulative streamed snapshots are repaired more reliably.
- Multiple tool calls in one upstream chunk are no longer dropped.

## v1.9.0 - 2026-04-28

Claude Code tool-call compatibility release.

### Added

- Bidirectional tool-call translation between Anthropic `tool_use/tool_result` and upstream Chat Completions `tool_calls`.
- Bidirectional tool-call translation between Anthropic `tool_use/tool_result` and upstream Responses `function_call/function_call_output`.
- Streaming conversion from upstream tool-call events into Anthropic-style `tool_use` content blocks.
- Smoke-test coverage for tool schema conversion, tool history conversion, streamed tool-call deltas, and `stop_reason: tool_use`.

### Changed

- Tool schemas are now sent as real upstream tools instead of text-only context notes.
- Tool results are now preserved as structured tool outputs instead of plain text fallbacks.
## v1.8.0 - 2026-04-28

Default API format and Base URL update.

### Changed

- Renamed GUI field `Responses Base URL` to `Base URL`.
- Changed default API Format to `chat_completions`.
- Changed default Base URL to `https://genaiapi.shanghaitech.edu.cn/api/v1/start`.
- API Format selection now automatically updates Base URL:
  - `chat_completions` -> `https://genaiapi.shanghaitech.edu.cn/api/v1/start`
  - `responses` -> `https://genaiapi.shanghaitech.edu.cn/api/v1/response`
- Added GUI hint text listing valid API Format options.
## v1.7.0 - 2026-04-28

Cross-platform source release for Linux and macOS.

### Added

- Linux/macOS source package: `SHTUClaudeProxy-v1.7.0-source-linux-macos.zip`.
- Headless CLI mode with `show-config`, `print-env`, `write-settings`, `install-launch-script`, and `serve` commands.
- Cross-platform path, launch script, and Claude launch helpers.
- X11 forwarding documentation for Linux GUI use.
- Smoke test script for Linux/macOS validation.

### Changed

- GUI text is now English-only to avoid missing Chinese font rendering on Linux.
- Windows v1.6.0 binaries are not rebuilt for this release.
## v1.6.0 - 2026-04-27

Zero-install release focused on ordinary end users.

### Added

- Single-file Windows EXE build: `SHTUClaudeProxy-v1.6.0-windows-x64.exe`.
- Build script support for both one-file and portable-folder packages.
- First-run setup tip explaining that no Python installation is required for release builds.
- One-click `Save + Connect + Launch` path for common first-time setup.

### Changed

- Release packaging now produces both a single-file EXE and the existing portable zip.
- README now recommends the single-file EXE for normal users and the zip for troubleshooting.
## v1.5.0 - 2026-04-27

Stable guided-setup release.

### Added

- Guided quick-start GUI with `Save Config`, `Write Claude Settings`, and `Start Proxy + Launch Claude` steps.
- Full-window vertical scrolling and larger default window for smaller displays.
- Per-role Claude model routing for:
  - `ANTHROPIC_MODEL`
  - `ANTHROPIC_DEFAULT_HAIKU_MODEL`
  - `ANTHROPIC_DEFAULT_SONNET_MODEL`
  - `ANTHROPIC_DEFAULT_OPUS_MODEL`
  - `ANTHROPIC_REASONING_MODEL`
- Effective model-routing summary in the GUI.
- `model_env` configuration block in `config.example.json`.
- Chat Completions upstream URL normalization.

### Changed

- Reworked the GUI layout to prioritize the first-time setup flow.
- Moved advanced actions into a separate optional section.
- Improved non-streaming and streaming upstream error reporting.
- Updated the release zip with the latest Windows build.



