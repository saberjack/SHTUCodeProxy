# SHTUCodeProxy

让 Claude Code、Codex 、各种支持自定义API的工具与校园 GenAI API 真正连起来
面向真实 AI 编程工作流的本地协议适配层,把模型路由、客户端配置、工具调用、多轮上下文、配置恢复和跨平台打包整合在一起，让校园 GenAI API 更容易进入日常研发和学习场景。
目前尚未测试其对第三方 API 的有效性。

Enable Claude Code, Codex, and various tools that support custom APIs to truly connect with the Campus GenAI API.
A local protocol adaptation layer for real-world AI programming workflows, integrating model routing, client configuration, tool calling, multi-turn context, configuration recovery, and cross-platform packaging — making the Campus GenAI API more accessible in everyday development and learning scenarios.
No testing has been done on whether it is effective with third-party APIs.

> **Important:** GPT-series models should use the `responses` API Format. Chat-only models such as GLM, Qwen, or DeepSeek can use `chat_completions` when their upstream endpoint requires it.

Current release: **v4.3.2**

SHTUCodeProxy is a cross-platform local proxy and desktop configuration tool for connecting **Claude Code**, **Codex CLI**, **Codex Desktop**, and ordinary API clients to the ShanghaiTech University campus **GenAI API** or compatible model endpoints.

It runs a local service, usually on `http://127.0.0.1:8082`, and translates between client-facing protocols and the selected upstream model format:

- Claude Code: Anthropic Messages-style `/v1/messages`.
- Codex CLI/Desktop: OpenAI Responses-style `/v1/responses`.
- Generic API clients: local `/v1/responses` or `/v1/messages`.
- Upstream models: `responses` or `chat_completions`, configured per model in the GUI.

The PyQt5 desktop app can save model settings, write Claude Code and Codex client configuration files, start/stop the local proxy, restore client config backups, check Codex config health, and keep the proxy running in the tray on supported desktops.

This tool was created by **sunyb, ShanghaiTech University Library and Information Center** for internal campus use. It is not an official Anthropic or OpenAI product.

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

## Intended Audience

This project is intended for ShanghaiTech University users who have access to the campus GenAI API.

No testing has been done on whether it is effective with third-party APIs.

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
├── pyqt_gui.py            # PyQt5 desktop UI
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
SHTUCodeProxy-v4.3.2-windows-x64.exe
```

You do **not** need to install Python, pip, PyInstaller, or any Python packages. The single-file EXE bundles the Python runtime and required dependencies.

You still need:

- Windows 10/11 or Windows Server with desktop UI support for the packaged EXE.
- For Linux: use the release binary or python-launcher package on a desktop/X11/Wayland environment. CLI mode is available for headless servers.
- Claude Code installed through npm or another method.
- Access to ShanghaiTech GenAI Response API.
- A valid GenAI Response API key.

### Developers / Building from Source

Developers, Linux/macOS users running from source, or anyone rebuilding packages need:

- Python 3.10+
- PyQt5
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
SHTUCodeProxy-v4.3.2-windows-x64.exe
```

Double-click it directly. No Python installation is required.

Alternative portable zip package:

```text
SHTUCodeProxy-v4.3.2-windows-x64.zip
```

If you use the zip package, extract the whole folder and run `SHTUCodeProxy.exe` inside it.

Linux desktop users can download either package:

```text
SHTUCodeProxy-v4.3.2-linux-x86_64
SHTUCodeProxy-v4.3.2-linux-x86_64-python-launcher.tar.xz
shtucodeproxyctl-v4.3.2-linux-x86_64
SHTUCodeProxy-v4.3.2-linux-x86_64-headless-cli.zip
```

For the single-file binary, make it executable and run it from a desktop session:

```bash
chmod +x SHTUCodeProxy-v4.3.2-linux-x86_64
./SHTUCodeProxy-v4.3.2-linux-x86_64
```

For the python-launcher package, extract it and run:

```bash
tar -xf SHTUCodeProxy-v4.3.2-linux-x86_64-python-launcher.tar.xz
cd SHTUCodeProxy-v4.3.2-linux-x86_64-python-launcher
python3 run_shtucodeproxy.py
```

Linux notes:

- A graphical desktop, X11 forwarding, or Wayland/XWayland session is required for the GUI.
- Some Linux desktops may print Qt GLX warnings. If the window opens and works normally, they can usually be ignored.
- On headless servers, use `SHTUCodeProxy-v4.3.2-linux-x86_64-headless-cli.zip`. It contains the no-GUI executable, `headless-config.example.json`, and an editable `config.json` copy.

macOS users should run from source for now.

## Usage Notes

- Click `Save Config` after editing model fields, model routing, client paths, or sandbox mode.
- Click `Write Client Config` to update the selected client: Claude Code writes `settings.json`; Codex writes `config.toml` and `auth.json`.
- Click `Save + Connect` for the normal path: save settings, write the selected client config, and start the local proxy.
- SHTUCodeProxy can serve Claude Code and Codex at the same time as long as both clients point to the same local proxy port.
- Do not edit generated client config repeatedly by hand unless needed; use the recovery buttons if a client config is broken.
- Never publish your local API key, `config.json`, `.codex/auth.json`, Claude `settings.json`, diagnostic logs, or screenshots containing keys.

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

example:

| Field | Meaning | Example |
| --- | --- | --- |
| Display Name | Friendly name shown in the GUI | ShanghaiTech University GPT-5.5 |
| Model ID | Model name Claude Code or Codex will request | GPT-5.5 |
| Base URL | Campus GenAI API endpoint | chat_completions: https://genaiapi.shanghaitech.edu.cn/api/v1/start;responses: https://genaiapi.shanghaitech.edu.cn/api/v1/response |
| API Key | Your campus API key | keep private |
| Upstream Model | Model ID sent to GenAI Response API | GPT-5.5 |
| Input Types | Whether this model route supports image, audio, and video input | Images enabled for GPT-5.5 and qwen-instruct |

> **Important:** For GPT-5 and newer GPT models, set `API Format` to `responses`.
> If an input type is disabled and a client sends that type, SHTUCodeProxy returns a normal assistant message explaining that the selected model does not support it instead of forwarding the request upstream. File/document inputs keep the existing passthrough behavior and are not controlled by these switches.

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

In the GUI, `API Format` supports `responses` and `chat_completions`. Selecting `chat_completions` fills Base URL with `https://genaiapi.shanghaitech.edu.cn/api/v1/start`; selecting `responses` fills Base URL with `https://genaiapi.shanghaitech.edu.cn/api/v1/response`.

`Default streaming` is enabled by default. It only applies when a client request omits the `stream` field; explicit `stream=true` and `stream=false` requests are always respected. For GPT-5.5 and other reasoning-heavy Responses models, keep enough `max_output_tokens` for both reasoning and visible output, especially for image input.

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

Supported tool flow:

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

## Configuration Files

### App Config

Stored locally at:

```text
%APPDATA%\SHTUCodeProxy\config.json
```

This file may contain your API key in plaintext. Do not commit it.

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
release\SHTUCodeProxy-v4.3.2-windows-x64.exe
release\SHTUCodeProxy-v4.3.2-windows-x64.zip
dist\SHTUCodeProxy\SHTUCodeProxy.exe
```

Use `release\SHTUCodeProxy-v4.3.2-windows-x64.exe` for normal Windows users. It is a single-file executable and does not require Python or the `_internal` folder.

Use `release\SHTUCodeProxy-v4.3.2-windows-x64.zip` as the Windows portable folder package. If you distribute the zip package, users must extract the whole folder because the `_internal` runtime folder is required by the folder build.

For Linux/macOS packaging from source, run:

```bash
./build_unix.sh
```

This generates a platform-specific single-file binary and a `.tar.xz` python-launcher folder package under `release/`.

## Linux and macOS Usage

Windows users should normally download the single-file EXE from the Release page. Linux users can use the single-file binary or the python-launcher `.tar.xz` package. macOS users should run from source for now.

### Linux Release Binary

```bash
chmod +x SHTUCodeProxy-v4.3.2-linux-x86_64
./SHTUCodeProxy-v4.3.2-linux-x86_64
```

### Linux Python Launcher Package

```bash
tar -xf SHTUCodeProxy-v4.3.2-linux-x86_64-python-launcher.tar.xz
cd SHTUCodeProxy-v4.3.2-linux-x86_64-python-launcher
python3 run_shtucodeproxy.py
```

Linux notes:

- A desktop display, X11 forwarding, or Wayland/XWayland session is required for GUI mode.
- Some desktops may print Qt GLX warnings. If the window opens and works normally, they can usually be ignored.
- On headless servers, use CLI mode.

### Source Package

```bash
unzip SHTUCodeProxy-v4.3.2-source-linux-macos.zip
cd SHTUCodeProxy-v4.3.2-source
python3 -m pip install -r requirements-build.txt
python3 smoke_test.py
python3 app.py
```

### Headless CLI Mode

For Linux servers without GUI or X11 forwarding, use CLI mode.

```bash
unzip SHTUCodeProxy-v4.3.2-linux-x86_64-headless-cli.zip
cd SHTUCodeProxy-v4.3.2-linux-x86_64-headless-cli
chmod +x shtucodeproxyctl-v4.3.2-linux-x86_64
nano config.json
./shtucodeproxyctl-v4.3.2-linux-x86_64 apply-config config.json --write-claude --write-codex --start
./shtucodeproxyctl-v4.3.2-linux-x86_64 status
```

The JSON file stores host, port, default streaming behavior, selected Claude/Codex model IDs, sandbox mode, and model routes. The example includes `deepseek-pro`, `deepseek-chat`, `glm-chat`, `qwen-instruct`, and `GPT-5.5`; replace each `api_key` value you plan to use, then adjust the selected IDs.

Model selection in `config.json` works like this:

- `models`: defines all available routes. `model_id` is the local name clients request; `upstream_model` is the real provider model name; `api_format` is `responses` or `chat_completions`; `supports_image`, `supports_audio`, and `supports_video` control image/audio/video inputs for that route.
- `default_model_id`: fallback route used by the proxy when a request does not match a configured model.
- `codex_model_id`: the single model written into Codex `config.toml` as the root `model` and profile model.
- `default_stream`: when `true`, requests that omit `stream` default to streaming. Explicit request values still win.
- `model_env.ANTHROPIC_MODEL`: Claude Code main model.
- `model_env.ANTHROPIC_DEFAULT_HAIKU_MODEL`: Claude Code Haiku / fast model route.
- `model_env.ANTHROPIC_DEFAULT_SONNET_MODEL`: Claude Code Sonnet / balanced model route.
- `model_env.ANTHROPIC_DEFAULT_OPUS_MODEL`: Claude Code Opus / strongest model route.
- `model_env.ANTHROPIC_REASONING_MODEL`: Claude Code reasoning model route.

All model selection values must match one of the `model_id` values in `models`.

Source package users can run the same flow with `python3 cli.py`:

```bash
cp headless-config.example.json config.json
nano config.json
python3 cli.py apply-config config.json --write-claude --write-codex --start
python3 cli.py status
```

Use `python3 cli.py use-model MODEL_ID` to switch both Claude Code and Codex routing to an existing model. Use `--claude` or `--codex` to switch only one client. Use `python3 cli.py stop` to stop the background proxy started by `start`. If you prefer direct flags instead of a file, `configure-model` is still available.

Install a helper launch script if needed:

```bash
python3 cli.py install-launch-script
```

Then run:

```bash
~/shtu-claude-proxy/claude-shtu.sh
```

## Run from Source

```powershell
python .\app.py
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
%APPDATA%\SHTUCodeProxy\config.json
```

## Publishing Checklist

Before publishing or creating a release, make sure you do not commit:

```text
config.json
build/
dist/
*.spec
__pycache__/
%APPDATA%\SHTUCodeProxy\config.json
%USERPROFILE%\.claude\settings.json
```

Run a quick scan for keys before pushing.

## Credits

Created by **sunyb**, ShanghaiTech University Library and Information Center.

## License

MIT License. See `LICENSE`.

