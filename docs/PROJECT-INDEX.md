# SHTUCodeProxy 项目目录索引

> 最后更新: 2026-05-25 (v4.3.3)
> 本文档记录项目中每个文件和目录的职责，防止迭代开发时因目录变更导致问题。

## 根目录结构

```
claude-responses-proxy/
├── app.py              # 应用入口（启动时自动将 src/ 加入 sys.path）
├── VERSION             # 版本号文件（当前: 4.3.3）
├── README.md           # 项目说明
├── DESIGN.md           # 设计文档
├── config.json         # 运行时配置（本地，不入 Git）
├── proxy.log           # 代理运行日志（本地，不入 Git）
├── .gitignore          # Git 忽略规则
│
├── src/                # ★ 源码模块目录（v4.3.3 从根目录移入）
├── build/              # 构建脚本与构建产物
├── docs/               # 文档
├── tests/              # 测试
├── release/            # 发布产物（exe/zip/checksums）
├── dist/               # PyInstaller 输出目录（本地，不入 Git）
├── .github/            # GitHub Actions 工作流
├── backups/            # 本地备份（不入 Git）
└── *.spec              # PyInstaller 历史规范文件（构建时自动生成）
```

## 目录详解

### `src/` — 源码模块

| 文件 | 职责 | 关键导出/接口 |
|------|------|---------------|
| `proxy.py` | 核心代理服务：接收 Anthropic /v1/messages 请求，转发至 OpenAI Responses 端点，做 SSE 流式格式转换 | `ProxyHandler` 类，`run_server()` |
| `cli.py` | CLI 命令行模式：start/stop/status/config/test 等子命令 | `main(argv)` |
| `pyqt_gui.py` | PyQt5 GUI 界面：系统托盘、配置面板、状态显示、日志查看 | `run()` |
| `config_store.py` | 配置数据模型与持久化：AppConfig / ModelConfig 数据类，读写 config.json | `load_config()`, `save_config()`, `AppConfig`, `ModelConfig` |
| `platform_utils.py` | 平台检测与路径工具：OS 判断、Claude/Codex 配置路径、端口检测 | `is_windows()`, `app_dir()`, `default_claude_path()`, `default_codex_config_path()` |
| `safe_io.py` | 安全文件读写：原子写入、自动备份、文件校验 | `atomic_write_text()`, `ORIGINAL_BACKUP_SUFFIX` |
| `linux_launcher.py` | Linux 桌面包启动器：设置环境变量后调用打包后的可执行文件 | `main()` |

**依赖关系：**
```
app.py
  ├──→ src/pyqt_gui.py → src/config_store.py → src/platform_utils.py
  │                                 └→ src/safe_io.py
  └──→ src/cli.py      → src/config_store.py → src/platform_utils.py
                  └→ src/proxy.py                  └→ src/safe_io.py
```

### `build/` — 构建脚本与产物

| 文件/目录 | 职责 |
|-----------|------|
| `build_exe.ps1` | Windows 构建脚本（PowerShell）：支持 OneFile / OneDir / 全量构建 |
| `build_exe.bat` | Windows 构建批处理入口 |
| `build_unix.sh` | Linux/macOS 构建脚本：含 OneDir、OneFile、CLI-only、桌面包 |
| `requirements-build.txt` | 构建依赖（PyInstaller 等） |
| `shtucodeproxy.ico` | 应用图标 |
| `source-package/` | 源码包暂存区（构建时使用） |
| `check-source-424/` | 源码检查暂存区 |
| `SHTUCodeProxy*/` | PyInstaller 构建缓存（各版本） |

### `docs/` — 文档

| 文件 | 职责 |
|------|------|
| `CHANGELOG.md` | 全量变更日志 |
| `CHANGELOG-v4.3.3.md` | v4.3.3 变更记录（本次 src/ 重构） |
| `config.example.json` | 配置文件示例 |
| `headless-config.example.json` | 无头模式配置示例 |
| `CONTRIBUTING.md` | 贡献指南 |
| `LICENSE` | 开源许可 |
| `SECURITY.md` | 安全策略 |

### `tests/` — 测试

| 文件 | 职责 |
|------|------|
| `smoke_test.py` | 基本冒烟测试 |
| `api_notes.py` | API 笔记/测试辅助 |

### `release/` — 发布产物

| 文件模式 | 说明 |
|----------|------|
| `SHTUCodeProxy-v{ver}-windows-x64.exe` | 单文件 Windows 可执行文件 |
| `SHTUCodeProxy-v{ver}-windows-x64.zip` | 便携目录包（Windows） |
| `SHTUCodeProxy-v{ver}-source-linux-macos.zip` | Linux/macOS 源码包 |
| `README-release-v{ver}.txt` | 发布说明 |
| `SHA256SUMS.txt` | 校验和 |

### `.github/` — CI/CD

| 文件 | 职责 |
|------|------|
| `workflows/build-linux-release.yml` | Linux 发布构建工作流 |

## 构建命令速查

```powershell
# Windows 全量构建（OneDir + OneFile + 源码包）
.\build\build_exe.ps1

# 仅 OneFile
.\build\build_exe.ps1 -OneFileOnly

# 仅 OneDir
.\build\build_exe.ps1 -OneDirOnly

# 先安装依赖再构建
.\build\build_exe.ps1 -InstallDeps
```

## 重要注意事项

1. **`src/` 路径**：v4.3.3 起，所有源码模块在 `src/` 目录下。`app.py` 通过 `sys.path.insert` 引入。PyInstaller 构建通过 `--paths src` 和 `--add-data "src\xxx.py;."` 处理。
2. **`build_exe.ps1` 参数格式**：`--paths src` 和 `app.py` 必须分行写，不能在同一行，否则 PowerShell 反引号续行会将它们合并为一个参数。
3. **运行时 `config.json`**：在项目根目录，不入 Git。`config_store.py` 通过 `platform_utils.app_dir()` 定位。
4. **发布 exe 替换**：替换 `release/` 下的 exe 前需先停止运行中的代理进程，否则文件被锁定。
5. **`.spec` 文件**：构建时由 PyInstaller 自动生成，历史版本 spec 保留在根目录供参考，无需手动维护。
