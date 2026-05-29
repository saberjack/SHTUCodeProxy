# SHTUCodeProxy

让 Claude Code、Codex 及各种支持自定义 API 的工具与校园 GenAI API 真正连起来。

本地协议适配层，把模型路由、客户端配置、工具调用、多轮上下文和跨平台打包整合在一起，让校园 GenAI API 更容易进入日常研发和学习场景。

> **提示**：GPT 系列模型应使用 `responses` API 格式；GLM、Qwen、DeepSeek 等 Chat 模型可使用 `chat_completions`。

Current release: **v4.4.5**

---

## 支持的客户端

| 客户端 | 协议 |
|--------|------|
| Claude Code | Anthropic Messages `/v1/messages` |
| Codex CLI / Desktop | OpenAI Responses `/v1/responses` |
| 通用 API 客户端 | `/v1/responses` 或 `/v1/messages` |

本地代理运行在 `http://127.0.0.1:8082`，在客户端协议和上游模型格式之间自动转换。

## 截图

<img width="1536" height="1024" alt="GUI" src="https://github.com/user-attachments/assets/97a87f8a-a5ee-40fd-937c-d721abf662a1" />

<img width="1422" height="992" alt="Models" src="https://github.com/user-attachments/assets/41a9add0-c71b-4670-8346-549053f8bd2a" />

<img width="1369" height="718" alt="Config" src="https://github.com/user-attachments/assets/7aa38261-e18e-4e3e-b25a-2e7eb1b53986" />

Claude CLI
<img width="1195" height="449" alt="Claude CLI" src="https://github.com/user-attachments/assets/7f9ffe47-b5c1-4a66-ab05-1b6c747335c8" />

<img width="2456" height="1620" alt="Claude CLI Full" src="https://github.com/user-attachments/assets/602ffb99-90ca-44b0-b302-911641c7dc7e" />

Codex Desktop
<img width="946" height="764" alt="Codex Desktop" src="https://github.com/user-attachments/assets/421e1bba-9d29-4530-8178-2c299fa9d575" />

Claude Desktop
<img width="1200" height="800" alt="Claude Desktop" src="https://github.com/user-attachments/assets/61440724-7080-4c1e-b43c-d4022eda6abe" />

## 快速开始

### Windows

1. 下载 `SHTUCodeProxy-v4.4.5-windows-x64.exe`
2. 双击运行，在 GUI 中配置模型和 API Key
3. 点击 **Start Proxy**
4. 打开 Claude Code / Codex 即可使用

### Linux (GUI)

```bash
tar xf SHTUCodeProxy-v4.4.5-linux-x86_64-python-launcher.tar.xz
cd SHTUCodeProxy
./SHTUCodeProxy
```

### Linux (Headless CLI)

```bash
unzip SHTUCodeProxy-v4.4.5-linux-x86_64-headless-cli.zip
cd SHTUCodeProxy-v4.4.5-linux-x86_64-headless-cli
chmod +x shtucodeproxyctl-v4.4.5-linux-x86_64
nano config.json                          # 填入 API Key
./shtucodeproxyctl-v4.4.5-linux-x86_64 apply-config config.json --write-claude --write-codex --start
./shtucodeproxyctl-v4.4.5-linux-x86_64 status
```

## 配置说明

`config.json` 核心字段：

- `models`：定义模型路由。`model_id` 是客户端请求的本地名称，`upstream_model` 是上游真实模型名，`api_format` 为 `responses` 或 `chat_completions`
- `default_model_id`：未指定模型时的默认路由
- `codex_model_id`：写入 Codex `config.toml` 的模型
- `default_stream`：请求省略 `stream` 时的默认行为
- `model_env.ANTHROPIC_MODEL`：Claude Code 主模型

切换模型：`./shtucodeproxyctl use-model MODEL_ID`

## 源码运行

```powershell
python app.py          # GUI
python proxy.py        # 仅代理
```

## API Smoke Test

```powershell
$body = @{
  model = "GPT-5.5"
  max_tokens = 100
  stream = $true
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

Invoke-WebRequest -UseBasicParsing `
  -Uri http://127.0.0.1:8082/v1/messages?beta=true `
  -Method POST -ContentType "application/json" `
  -Headers @{ "anthropic-version" = "2023-06-01"; "x-api-key" = "local-proxy" } `
  -Body $body
```

## 已知限制

- **Chat 模型并发时空响应**：Qwen 等 Chat 模型在高并发时上游可能返回空内容，代理会自动重试并回退到流式请求，但仍偶尔可能出现空回复
- **Qwen 推理模式**：Qwen-instruct 有时会进入推理模式（reasoning），此时实际输出在 `reasoning` 字段而非 `content` 中，代理会自动提取，但展示的是原始推理过程而非最终回答
- **Token 用量为估算**：上游 Chat Completions 模型不返回精确 token 数，代理按字符数估算
- **图片/多模态为 best-effort**：仅部分模型支持图片输入，输出图片不可用
- **仅适用于校园 GenAI API**：未对第三方 API 做兼容性测试

## 常见问题

| 问题 | 解决方案 |
|------|----------|
| `ConnectionRefused` | 确认代理已启动（GUI 中点击 Start Proxy） |
| Claude Code 端口不对 | 检查 `~/.claude/settings.json` 中 `ANTHROPIC_BASE_URL` 为 `http://127.0.0.1:8082` |
| 模型不存在 | 检查 model ID、API Key、上游模型是否有效 |

## Credits

Created by **sunyb**, ShanghaiTech University Library and Information Center.

## License

MIT License. See `LICENSE`.
