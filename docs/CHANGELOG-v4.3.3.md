# SHTUCodeProxy v4.3.3 变更记录

## 2026-05-25 源码目录重构

### 变更概述
将根目录下的 7 个源码模块移入 `src/` 子目录，使项目结构更清晰，根目录只保留入口文件和配置/构建目录。

### 变更文件

| 操作 | 文件 | 说明 |
|------|------|------|
| 移动 | `cli.py` → `src/cli.py` | CLI 命令行模式入口与子命令 |
| 移动 | `config_store.py` → `src/config_store.py` | 配置数据模型与持久化 |
| 移动 | `linux_launcher.py` → `src/linux_launcher.py` | Linux 桌面包启动器 |
| 移动 | `platform_utils.py` → `src/platform_utils.py` | 平台检测与路径工具 |
| 移动 | `proxy.py` → `src/proxy.py` | 核心代理服务（Anthropic→OpenAI 转换） |
| 移动 | `pyqt_gui.py` → `src/pyqt_gui.py` | PyQt5 GUI 界面 |
| 移动 | `safe_io.py` → `src/safe_io.py` | 安全文件读写与备份工具 |
| 修改 | `app.py` | 添加 `sys.path.insert` 使 `src/` 可导入；将顶层 import 移至模块顶部 |
| 修改 | `build/build_exe.ps1` | `--add-data` 路径从 `xxx.py` 改为 `src\xxx.py`；新增 `--paths src`；修复 `--paths src` 与 `app.py` 参数分隔问题 |
| 修改 | `build/build_unix.sh` | `--add-data` 路径从 `xxx.py` 改为 `src/xxx.py`；CLI 入口从 `cli.py` 改为 `src/cli.py`；`linux_launcher.py` 路径更新 |

### 关键修复
- **`build_exe.ps1` 参数分隔 bug**：原 `--paths src     app.py` 在 PowerShell 反引号续行中被当作一个参数，导致 PyInstaller 找不到入口脚本。修复为 `--paths src `` + 换行 + `app.py`。
- 两种构建模式（OneFile / OneDir）均已验证通过。

### Git commit
`740297c` refactor: move source modules into src/ directory

### 已知遗留
- `release\SHTUCodeProxy-v4.3.3-windows-x64.exe` 因运行中的代理进程占用未更新，需重启代理后从 `dist\` 复制。
