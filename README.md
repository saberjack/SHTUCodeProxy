# SHTUCodeProxy

> **Important:** GPT-series models should use the `responses` API Format.

Current development version: **v4.1.1**

SHTUCodeProxy is a cross-platform local proxy for connecting **Claude Code**, **Codex CLI**, and **Codex Desktop** to the ShanghaiTech University campus **GenAI Response API** or compatible local model endpoints.

This tool was created by **sunyb, ShanghaiTech University Library and Information Center** for internal campus use. It helps users access Claude Code through the university GenAI API by translating Claude Code's Anthropic Messages API traffic into upstream requests and converting streaming responses back into Claude Code-compatible Server-Sent Events. v4.1.1 also exposes an OpenAI Responses-compatible `/v1/responses` endpoint for Codex clients.

> Note: SHTUCodeProxy is a local proxy tool created by sunyb for ShanghaiTech campus GenAI API access from Claude Code  ,CodeX and any other.
> 
<img width="1536" height="1024" alt="myImage (2)" src="https://github.com/user-attachments/assets/97a87f8a-a5ee-40fd-937c-d721abf662a1" />

<img width="1422" height="992" alt="image" src="https://github.com/user-attachments/assets/41a9add0-c71b-4670-8346-549053f8bd2a" />

<img width="1369" height="718" alt="image" src="https://github.com/user-attachments/assets/7aa38261-e18e-4e3e-b25a-2e7eb1b53986" />

Claude Cli
<img width="1195" height="449" alt="image" src="https://github.com/user-attachments/assets/7f9ffe47-b5c1-4a66-ab05-1b6c747335c8" />

<img width="2456" height="1620" alt="5cf20b87c9f360383d6b1c8d2fa02b48" src="https://github.com/user-attachments/assets/602ffb99-90ca-44b0-b302-911641c7dc7e" />

CodeX Desktop
<img width="946" height="764" alt="image" src="https://github.com/user-attachments/assets/421e1bba-9d29-4530-8178-2c299fa9d575" />

Claud Desktop
<img width="1200" height="800" alt="image" src="https://github.com/user-attachments/assets/61440724-7080-4c1e-b43c-d4021eda6abe" />


## What It Does

Claude Code expects an Anthropic-compatible API endpoint such as:

```text
POST /v1/messages
```

ShanghaiTech GenAI Response API returns OpenAI Responses-style streaming events such as:

```text
event: response.output_text.delta
```

Codex clients expect an OpenAI Responses-compatible endpoint such as:

```text
POST /v1/responses
```

SHTUCodeProxy bridges both client formats:

```text
Claude Code
  -> Anthropic Messages request
  -> SHTUCodeProxy on 127.0.0.1:8082
  -> GenAI Response API
  -> OpenAI Responses SSE
  -> Anthropic-style SSE
  -> Claude Code

Codex CLI/Desktop
  -> OpenAI Responses request
  -> SHTUCodeProxy on 127.0.0.1:8082
  -> Responses or Chat Completions upstream
  -> OpenAI Responses SSE
  -> Codex
```

## Features

- Windows GUI release; Linux/macOS can run from source with GUI or CLI mode.
- Guided quick-start GUI with a one-click `Save + Connect + Launch` path plus manual step buttons.
- Full-window scrolling for smaller displays.
- Local Anthropic-compatible endpoint for Claude Code.
- Local OpenAI Responses-compatible endpoint for Codex CLI and Codex Desktop.
- Multiple model configurations.
- Per-Claude-role model routing for `ANTHROPIC_MODEL`, Haiku, Sonnet, Opus, and reasoning model variables.
- Per-model settings:
  - display name
  - Claude Code model ID
  - GenAI Response API base URL
  - API key
  - upstream model ID
  - upstream API format (`responses` or `chat_completions`)
- One-click writing of Claude Code `settings.json`.
- One-click Claude Code launch with proxy environment.
- Auto-detection of npm-installed Claude Code.
- Portable across Windows, Linux, and macOS user accounts where Python/Tkinter is available.
- PyInstaller build script for Windows release packaging.
- Bidirectional tool-call translation for Claude Code `tool_use/tool_result` and upstream `tool_calls/function_call` protocols.

## Intended Audience

This project is intended for ShanghaiTech University users who have access to the campus GenAI Response API and want to use that API from Claude Code.

It is not an official Anthropic product, not an OpenAI product, and not a general-purpose full Anthropic API emulator.

## Important Limitations

This project focuses on text streaming compatibility plus Claude Code tool-call bridging.

Known limitations:

- Tool calls are translated between Anthropic `tool_use/tool_result` and upstream Chat Completions `tool_calls` or Responses `function_call/function_call_output` formats.
- Codex support uses `/v1/responses`; Codex clients should not be configured against `/v1/chat/completions`.
- Token usage fields are approximate.
- Images are best-effort only.
- Very complex Claude Code workflows may still need additional edge-case testing against the exact upstream model behavior.

For normal conversational and many coding-assistance workflows, the proxy can be sufficient. For advanced autonomous coding workflows, validate with a small file-read or command-style Claude Code task first.

## Repository Layout

```text
.
├── app.py                 # GUI entry point
├── gui.py                 # Tkinter desktop UI
├── proxy.py               # Anthropic Messages <-> Responses proxy
├── cli.py                 # Headless command-line tools
├── platform_utils.py      # Cross-platform path, script, and launch helpers
├── config_store.py        # Config loading, defaults, path portability
├── config.example.json    # Safe example config without API key
├── build_exe.ps1          # Windows build script
├── build_exe.bat          # Double-click build helper
├── build_unix.sh          # Linux/macOS build script
├── requirements-build.txt # Build-time dependency list
├── LICENSE
├── SECURITY.md
└── CONTRIBUTING.md
```

## Requirements

### End Users

Use the recommended single-file release:

```text
SHTUCodeProxy-v1.6.0-windows-x64.exe
```

You do **not** need to install Python, pip, PyInstaller, or any Python packages. The single-file EXE bundles the Python runtime and required dependencies.

You still need:

- Windows 10/11 or Windows Server with desktop UI support for the packaged EXE.
- For Linux/macOS: Python 3.10+ from source; Tkinter/display server for GUI, or CLI mode for headless servers.
- Claude Code installed through npm or another method.
- Access to ShanghaiTech GenAI Response API.
- A valid GenAI Response API key.

### Developers / Building from Source

Developers, Linux/macOS users running from source, or anyone rebuilding packages need:

- Python 3.10+
- PyInstaller

Install build dependency:

```powershell
python -m pip install -r requirements-build.txt
```

## Quick Start for End Users

### 1. Install Claude Code or Codex

If Claude Code is installed with npm, the default executable is usually:

```text
%APPDATA%\npm\claude.cmd
```

The GUI tries to detect this automatically.

For Codex CLI/Desktop, install and sign in to Codex separately. SHTUCodeProxy only writes the local Codex provider/profile and serves the model endpoint.

### 2. Start SHTUCodeProxy

Recommended download for normal users:

```text
SHTUCodeProxy-v4.1.1-windows-x64.exe
```

Double-click it directly. No Python installation is required.

Alternative portable zip package:

```text
SHTUCodeProxy-v4.1.1-windows-x64.zip
```

If you use the zip package, extract the whole folder and run `SHTUCodeProxy.exe` inside it.

### 3. Configure Server Settings

Default values:

```text
Host: 127.0.0.1
Port: 8082
Claude Settings Path: %USERPROFILE%\.claude\settings.json
Claude Code Path: %APPDATA%\npm\claude.cmd
Codex config.toml Path: %USERPROFILE%\.codex\config.toml
```

You can change these if your environment is different.

### 4. Configure a Model

For each model entry:

| Field | Meaning | Example |
| --- | --- | --- |
| Display Name | Friendly name shown in the GUI | ShanghaiTech GPT-5.5 |
| Model ID | Model name Claude Code or Codex will request | GPT-5.5 |
| Base URL | Campus GenAI API endpoint | chat_completions: https://genaiapi.shanghaitech.edu.cn/api/v1/start; responses: https://genaiapi.shanghaitech.edu.cn/api/v1/response |
| API Key | Your campus API key | keep private |
| Upstream Model | Model ID sent to GenAI Response API | GPT-5.5 |

> **Important:** For GPT-5 and newer GPT models, set `API Format` to `responses`.

Click:

```text
Apply Model Changes
Save Config
```

### 5. Choose Client Mode and Write Settings

Choose `Claude Code` or `Codex CLI/Desktop` in `Client Mode`.

For Claude Code, click:

```text
Write Client Config
```

This writes the Claude settings file exactly as in v2.0.0.

For Codex, click the same button after selecting `Codex CLI/Desktop`. The app writes a `shtu_proxy` provider/profile to:

```text
%USERPROFILE%\.codex\config.toml
```

The Codex provider uses:

```toml
wire_api = "responses"
base_url = "http://127.0.0.1:8082/v1"
```

Do not configure Codex against `/v1/chat/completions`; Codex must use the Responses wire API.

Codex has its own `Codex Model` selector in the GUI. It is independent from Claude Model Routing, so Claude can keep separate Main/Haiku/Sonnet/Opus/Reasoning routes while Codex writes exactly one selected model into `config.toml`.

For Claude Code, the app updates your Claude Code settings file, usually:

```text
%USERPROFILE%\.claude\settings.json
```

The Claude settings contain an `env` block like:

```text
%USERPROFILE%\.claude\settings.json
```

Use `Claude Model Routing` to choose which configured Model ID is written to each Claude Code model variable. They may all point to the same model, or each role can point to a different configured model. It writes an `env` block like:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082",
    "ANTHROPIC_MODEL": "GPT-5.5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GPT-5.5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "GPT-5.5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "GPT-5.5",
    "ANTHROPIC_REASONING_MODEL": "GPT-5.5",
    "ANTHROPIC_AUTH_TOKEN": "local-proxy"
  },
  "includeCoAuthoredBy": false
}
```

`Current Main Model` shows the value used for `ANTHROPIC_MODEL`. To change it, select a different `Main Model` in `Claude Model Routing`.

`ANTHROPIC_AUTH_TOKEN` is only a local placeholder for Claude Code. The real upstream API key is stored in SHTUCodeProxy's local app config and is used only by the proxy when forwarding requests to GenAI Response API.

### 6. Start Proxy

Click:

```text
Start Proxy
```

Expected status:

```text
Running on http://127.0.0.1:8082
```

### 7. Start Claude Code or Codex

For Claude Code, you can either:

- select `Claude Code` and click `Start Proxy / Launch`, or
- start Claude Code manually after writing settings.

For Codex, select `Codex CLI/Desktop`, click `Start Proxy / Launch`, then start Codex with profile `shtu_proxy`.

If starting manually, make sure the proxy is already running.


## Chat Completions API Format

SHTUCodeProxy supports two upstream API formats per model:

| API Format | Upstream endpoint style | Streaming delta parsed from |
| --- | --- | --- |
| `responses` | OpenAI Responses-style `/response` | `response.output_text.delta` |
| `chat_completions` | OpenAI Chat Completions-style `/chat/completions` | `choices[0].delta.content` |

> **Important:** GPT-5 and newer GPT models must use `responses`. Use `chat_completions` only for upstream models or endpoints that explicitly require Chat Completions compatibility.

For ShanghaiTech GenAI Chat Completions endpoints, configure a model like this:

```json
{
  "name": "ShanghaiTech DeepSeek Pro Chat Completions",
  "model_id": "deepseek-pro",
  "base_url": "https://genaiapi.shanghaitech.edu.cn/api/v1/start",
  "api_key": "YOUR_API_KEY",
  "upstream_model": "deepseek-pro",
  "api_format": "chat_completions"
}
```

The equivalent upstream request generated by the proxy is compatible with this style:

```bash
curl -X POST https://genaiapi.shanghaitech.edu.cn/api/v1/start/chat/completions \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -d '{
    "model": "deepseek-pro",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "who are you"}
    ],
    "max_tokens": 4096,
    "stream": true,
    "temperature": 0.7
  }'
```

In the GUI, `API Format` supports `responses` and `chat_completions`. Selecting `chat_completions` fills Base URL with `https://genaiapi.shanghaitech.edu.cn/api/v1/start`; selecting `responses` fills Base URL with `https://genaiapi.shanghaitech.edu.cn/api/v1/response`.

## Codex CLI and Codex Desktop

Codex only supports the OpenAI Responses wire format for custom providers. Configure Codex to talk to this proxy through `/v1/responses`; do not point Codex at a Chat Completions URL.

The proxy accepts Codex requests at:

```text
http://127.0.0.1:8082/v1/responses
```

Add a custom provider to your Codex `config.toml`:

```toml
[model_providers.shtu_proxy]
name = "SHTUCodeProxy"
base_url = "http://127.0.0.1:8082/v1"
wire_api = "responses"
requires_openai_auth = true

[profiles.shtu_proxy]
model_provider = "shtu_proxy"
model = "GPT-5.5"
```

Write Codex auth at `%USERPROFILE%\.codex\auth.json`:

```json
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "YOUR_UPSTREAM_API_KEY"
}
```

The GUI writes both files for you in `Codex CLI/Desktop` mode. It uses the selected `Codex Model` entry's upstream API key as `OPENAI_API_KEY`, while the proxy still uses SHTUCodeProxy's own model table when forwarding requests. If a configured model uses `api_format: "chat_completions"`, Codex still sends Responses requests to the proxy; the proxy converts them to Chat Completions before calling that upstream.

Codex sandbox mode is written as the root-level `sandbox_mode` value in `config.toml`. The GUI supports Codex's three CLI modes: `read-only`, `workspace-write`, and `danger-full-access`. It also writes `[features] hooks = true`; older `[features] codex_hooks = true` entries are removed because Codex now treats `codex_hooks` as deprecated.

The GUI also includes a `Connection, Safety, and Recovery` panel. It shows whether the configured proxy port is stopped, already used by another process, or started by this app. `danger-full-access` requires a warning confirmation before it is saved. Recovery buttons can restore the latest backup or the original pre-SHTUCodeProxy client config for the selected client mode: Claude Code restores `settings.json`, while Codex restores both `config.toml` and `auth.json`.

Use `Check Codex Health` after changing Codex settings. It validates `config.toml` syntax, `auth.json`, `shtu_proxy` provider/profile, `[features] hooks = true`, sandbox mode, project trust entries, and MCP server blocks.

### MCP and Tool Use

Codex MCP servers are configured in Codex itself, not in SHTUCodeProxy. The proxy only sees the model-facing Responses request that Codex produces after loading MCP tools.

Supported v4.1.1 tool flow:

```text
Codex MCP server
  -> Codex Responses tools
  -> SHTUCodeProxy /v1/responses
  -> upstream Responses function_call, or converted Chat Completions tool_calls
  -> SHTUCodeProxy Responses function_call events
  -> Codex executes the MCP tool
  -> Codex sends function_call_output on the next /v1/responses request
```

This means MCP tool definitions and `function_call_output` history are preserved for Responses upstreams and converted for Chat Completions upstreams.

Codex skills are also client-side. As long as Codex turns a skill into prompt context, MCP tools, or Responses tool definitions, SHTUCodeProxy preserves that context and tool flow through the same `/v1/responses` path.

## Generic API Clients

SHTUCodeProxy can also be used by ordinary API clients, not only Claude Code or Codex. Start the proxy first, then point your client at the local proxy instead of the upstream GenAI endpoint.

Recommended local base URL:

```text
http://127.0.0.1:8082/v1
```

Supported local endpoints:

| Client style | Endpoint | Notes |
| --- | --- | --- |
| OpenAI Responses-style clients | `POST /v1/responses` | Recommended for generic API usage. |
| Anthropic Messages-style clients | `POST /v1/messages` | Useful for Claude-compatible request bodies. |

The local `Authorization` or `x-api-key` value is only a client-compatibility token. The real upstream API keys are read from SHTUCodeProxy's local model configuration.

### OpenAI Responses-style request

Use this for new generic integrations where you control the request body.

```bash
curl -X POST http://127.0.0.1:8082/v1/responses \
  -H 'Authorization: Bearer local-proxy' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-chat",
    "input": [
      {"role": "user", "content": "Reply with one short sentence about SHTUCodeProxy."}
    ],
    "temperature": 0,
    "stream": false
  }'
```

Streaming is also supported:

```json
{
  "model": "glm-chat",
  "input": [{"role": "user", "content": "Give me three concise bullet points."}],
  "stream": true
}
```

OpenAI Python SDK example:

```python
from openai import OpenAI

client = OpenAI(
    api_key="local-proxy",
    base_url="http://127.0.0.1:8082/v1",
)

response = client.responses.create(
    model="glm-chat",
    input="Reply with one short sentence about SHTUCodeProxy.",
)

print(response.output_text)
```

### Anthropic Messages-style request

Use this when your generic client already sends Anthropic-compatible Messages requests.

```bash
curl -X POST http://127.0.0.1:8082/v1/messages \
  -H 'x-api-key: local-proxy' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-chat",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Reply with one short sentence about SHTUCodeProxy."}
    ],
    "stream": false
  }'
```

### Model names

The `model` value must match a `Model ID` configured in SHTUCodeProxy, for example:

```text
glm-chat
deepseek-chat
qwen-instruct
```

For each request, the proxy looks up that model ID and forwards using the configured upstream Base URL, API Key, Upstream Model, and API Format (`responses` or `chat_completions`).

### Important limitations

- The local proxy currently exposes `/v1/responses` and `/v1/messages` for client requests.
- Do not point generic clients at `http://127.0.0.1:8082/v1/chat/completions`; SHTUCodeProxy converts to upstream Chat Completions internally when a model route is configured as `chat_completions`, but it does not expose a local Chat Completions endpoint.
- Make sure SHTUCodeProxy is running and the GUI shows the configured port is listening before sending generic API requests.

## Multiple Models

You can add multiple model routes.

Example:

| Model ID for Claude Code | Upstream Model | Base URL |
| --- | --- | --- |
| GPT-5.5 | GPT-5.5 | https://genaiapi.shanghaitech.edu.cn/api/v1/start |
| GPT-5.5-fast | GPT-5.5 | another compatible endpoint |
| GPT-5.5-reasoning | GPT-5.5 | another compatible endpoint |

Claude Code selects a route by the model ID it sends. The proxy then forwards to the configured upstream `base_url`, `api_key`, and `upstream_model`.

## Configuration Files

### App Config

Stored locally at:

```text
%APPDATA%\SHTUClaudeProxy\config.json
```

This file may contain your API key in plaintext. Do not commit it.

### Claude Code Settings

Usually stored at:

```text
%USERPROFILE%\.claude\settings.json
```

The GUI writes only the `env` fields needed by Claude Code and preserves other JSON fields when possible.

## Build from Source

Clone the repository and run:

```powershell
python -m pip install -r requirements-build.txt
.\build_exe.ps1
```

Or install PyInstaller automatically:

```powershell
.\build_exe.ps1 -InstallDeps
```

Outputs:

```text
release\SHTUCodeProxy-v1.6.0-windows-x64.exe
release\SHTUCodeProxy-windows-x64.zip
dist\SHTUCodeProxy\SHTUCodeProxy.exe
```

Use `release\SHTUCodeProxy-v1.6.0-windows-x64.exe` for normal users. It is a single-file executable and does not require Python or the `_internal` folder.

Use `release\SHTUCodeProxy-windows-x64.zip` as the Windows portable folder package. If you distribute the zip package, users must extract the whole folder because the `_internal` runtime folder is required by the folder build.

For Linux/macOS packaging from source, run:

```bash
./build_unix.sh
```

This generates a platform-specific single-file binary and a `.tar.gz` folder package under `release/`.

## Version v2.0.0

v2.0.0 hardens Claude Code tool-call compatibility for Chat Completions and Responses upstreams:

- More robust streamed and non-streamed tool-call parsing.
- Better handling for multiple tool calls in one response.
- Safer tool argument repair for wrapped, cumulative, or JSON-like arguments.
- Improved `tool_result` ordering for Chat Completions compatibility.
- Claude model routing now accepts common date-suffixed model IDs.
- GPT-series models should use `responses` API Format.

## Version v1.9.0

v1.9.0 adds real Claude Code tool-call bridging:

- Converts Claude Code `tools` into upstream Chat Completions `tools` or Responses `tools`.
- Converts assistant `tool_use` history into Chat Completions `tool_calls` or Responses `function_call` items.
- Converts Claude Code `tool_result` messages into Chat Completions `tool` messages or Responses `function_call_output` items.
- Converts upstream streamed `tool_calls` / `function_call` events back into Anthropic-style `tool_use` content blocks.
- Returns `stop_reason: tool_use` when the upstream model requests tool execution.

## Version v1.8.0

v1.8.0 updates the default upstream configuration and GUI labels:

- Renames `Responses Base URL` to `Base URL` in the GUI.
- Changes the default API Format to `chat_completions`.
- Uses `https://genaiapi.shanghaitech.edu.cn/api/v1/start` as the default Base URL for `chat_completions`.
- Automatically switches Base URL to `https://genaiapi.shanghaitech.edu.cn/api/v1/response` when API Format is `responses`.
- Adds clearer GUI hint text for the two API Format options.
## Version v1.7.0

v1.7.0 adds Linux/macOS source-based support while keeping the Windows v1.6.0 release package unchanged. It includes:

- Linux/macOS GUI support from source with Tkinter.
- Headless CLI mode for servers without a display.
- X11 forwarding guidance for remote Linux GUI use.
- Cross-platform Claude path, settings path, launch script, and launch helpers.
- A Linux/macOS smoke test script.
- A source zip package that extracts into its own directory.
- English-only GUI text to avoid Linux font fallback issues.
## Version v1.6.0

v1.6.0 is the zero-install release. It includes:

- A one-click setup path: save config, write Claude settings, start the proxy, then launch Claude Code.
- Per-role Claude model routing for main, Haiku, Sonnet, Opus, and reasoning model variables.
- A visible `Effective` routing summary so users can see which model is currently active.
- Support for both OpenAI Responses-style and Chat Completions-style upstream endpoints.
- Better upstream URL normalization and error reporting.
- A larger scrollable UI for smaller displays.
- A single-file Windows EXE release that does not require Python installation or a sidecar `_internal` folder.


## Linux and macOS Usage

Windows users should normally download the single-file EXE from the Release page. Linux and macOS support is source-based at this stage.

### 1. Unpack the Source Package

The Linux/macOS source package should unpack into its own directory:

```bash
unzip SHTUCodeProxy-v1.8.0-source-linux-macos.zip
cd SHTUCodeProxy-v1.8.0-source-linux-macos
```

Run the smoke test first:

```bash
python3 -m py_compile app.py cli.py config_store.py gui.py platform_utils.py proxy.py smoke_test.py
python3 smoke_test.py
```

Expected output:

```text
SHTUCodeProxy smoke test passed
```

### 2. Configure API Key and Model

Start once to generate `config.json`, or copy from `config.example.json`:

```bash
python3 cli.py show-config
cp config.example.json config.json
```

Edit `config.json` with your editor:

```bash
nano config.json
```

At minimum, fill these fields in the model you want to use:

```json
{
  "model_id": "GPT-5.5",
  "base_url": "https://genaiapi.shanghaitech.edu.cn/api/v1/response",
  "api_key": "PASTE_YOUR_GENAI_API_KEY_HERE",
  "upstream_model": "GPT-5.5",
  "api_format": "responses"
}
```

Important fields:

- `model_id`: the model name Claude Code will request.
- `base_url`: the upstream GenAI API endpoint.
- `api_key`: your GenAI API key. Keep it private.
- `upstream_model`: the actual upstream model name sent to GenAI API.
- `api_format`: use `responses` or `chat_completions`.

> **Important:** For GPT-5 and newer GPT models, keep `api_format` as `responses`.

For Chat Completions-compatible upstreams, use:

```json
"api_format": "chat_completions"
```

Model routing is controlled by `model_env`:

```json
"model_env": {
  "ANTHROPIC_MODEL": "GPT-5.5",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GPT-5.5",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "GPT-5.5",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "GPT-5.5",
  "ANTHROPIC_REASONING_MODEL": "GPT-5.5"
}
```

Each value must match one configured `model_id`. They can all be the same, or you can route different Claude roles to different configured models.

### 3. GUI Mode

Run the Tkinter GUI from source:

```bash
python3 app.py
```

On Linux servers without a local desktop, use X11 forwarding:

```bash
ssh -X user@host
cd SHTUCodeProxy-v1.8.0-source-linux-macos
python3 app.py
```

If Tkinter is missing on Linux, install the system package for your distribution, for example:

```bash
sudo apt install python3-tk
```

### 4. Headless CLI Mode

For Linux servers without GUI or X11 forwarding, use CLI mode.

Check resolved config:

```bash
python3 cli.py show-config
```

Write Claude Code settings:

```bash
python3 cli.py write-settings
```

This updates:

```text
~/.claude/settings.json
```

Install a helper launch script:

```bash
python3 cli.py install-launch-script
```

This creates:

```text
~/shtu-claude-proxy/claude-shtu.sh
```

Start the proxy in one terminal:

```bash
python3 cli.py serve
```

In another terminal, launch Claude Code through the helper script:

```bash
~/shtu-claude-proxy/claude-shtu.sh
```

Alternatively, print environment variables and apply them manually:

```bash
python3 cli.py print-env
```

Then run `claude` in the same shell after exporting those variables.

## Run from Source

```powershell
python .\gui.py
```

Run proxy only:

```powershell
python .\proxy.py --host 127.0.0.1 --port 8082
```

## Direct API Smoke Test

After starting the proxy:

```powershell
$body = @{
  model = "GPT-5.5"
  max_tokens = 100
  stream = $true
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

Invoke-WebRequest -UseBasicParsing `
  -Uri http://127.0.0.1:8082/v1/messages?beta=true `
  -Method POST `
  -ContentType "application/json" `
  -Headers @{ "anthropic-version" = "2023-06-01"; "x-api-key" = "local-proxy" } `
  -Body $body
```

Expected SSE events include:

```text
event: message_start
event: content_block_start
event: content_block_delta
event: message_delta
event: message_stop
```

## Troubleshooting

### Claude Code: `ConnectionRefused`

Cause: Claude Code is pointing to `http://127.0.0.1:8082`, but the local proxy is not running.

Fix:

1. Open SHTUCodeProxy.
2. Click `Start Proxy`.
3. Confirm status shows `Running on http://127.0.0.1:8082`.
4. Restart Claude Code.

### Claude Code Still Uses an Old Port

Check:

```text
%USERPROFILE%\.claude\settings.json
```

Make sure `ANTHROPIC_BASE_URL` is:

```text
http://127.0.0.1:8082
```

If not, click `Write Claude Settings` again.

### Claude Code Says Model Does Not Exist

Usually this means the proxy returned an error before reaching upstream.

Check:

- model ID in Claude Code matches `Model ID for Claude Code`
- API key is configured
- upstream model is valid for GenAI Response API
- proxy logs in the GUI

### Response Hangs After First Message

Use the latest version. Older builds used keep-alive SSE behavior that could cause Claude Code to wait for the connection to close.

### API Key Safety

Never paste your API key into GitHub issues. The API key is stored locally in:

```text
%APPDATA%\SHTUClaudeProxy\config.json
```

## Publishing Checklist

Before publishing or creating a release, make sure you do not commit:

```text
config.json
build/
dist/
*.spec
__pycache__/
%APPDATA%\SHTUClaudeProxy\config.json
%USERPROFILE%\.claude\settings.json
```

Run a quick scan for keys before pushing.

## Credits

Created by **sunyb**, ShanghaiTech University Library and Information Center.

Purpose: provide a convenient local bridge for ShanghaiTech campus GenAI Response API access from Claude Code.

## License

MIT License. See `LICENSE`.




