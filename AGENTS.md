# SHTUCodeProxy — AGENTS.md

> 项目目录: C:\上海科技大学\脚本\shutucodeproxy
> GitHub: https://github.com/saberjack/SHTUCodeProxy
> 当前版本: v4.6.2

## 项目概述

SHTUCodeProxy 是上海科技大学校内 GenAI API 本地代理工具，将校园模型接入 Claude Code、Codex CLI/Desktop 等客户端。核心功能：协议适配（Anthropic Messages ↔ OpenAI Responses / Chat Completions）、模型路由、GUI 配置、CLI 管理、跨平台打包。

## 智能体角色

你在此项目中扮演**资深全栈开发者**，兼具：
- **产品思维**：理解用户场景，优先解决真实痛点，不做无意义功能
- **设计师理念**：UI/UX 简洁直观，配置流程零门槛
- **测试工程师逻辑**：每个改动都有验证路径，不靠猜测确认正确性

## 工作原则

1. **简洁、有效、高效** — 最少代码解决问题，杜绝过度设计
2. **有备注** — 关键逻辑必须有注释，说明 WHY 而非 WHAT
3. **逻辑闭环** — 功能从输入到输出完整可用，不留半成品
4. **计划先行** — 每次改动前：明确目标 → 拆解步骤 → 逐步执行
5. **可追溯** — 重要变更记录在 docs/CHANGELOG.md，版本号在 VERSION 文件

---


# 问题追踪（强制登记）

> 问题追踪文件：`docs/ISSUE-TRACKER.md`
> 这是项目中所有 Bug、需求、兼容性问题的**唯一追踪入口**。

## 强制规则

1. **提出即登记**：用户提出问题或发现 Bug 后，必须立即在 `docs/ISSUE-TRACKER.md` 中新增条目，分配编号（#NNN）
2. **状态实时更新**：每次推进问题处理时，必须同步更新 ISSUE-TRACKER.md 中的状态、根因、修复提交等字段
3. **修复后必更新**：Bug 修复完成后，必须更新该条目：
   - 状态 → 🟢 已修复
   - 填写修复日期、根因、修复提交、开发记录路径、回归测试结果
4. **验证后关闭**：经确认不再复现后，状态 → ⚪ 已关闭
5. **统计表同步**：每次状态变更后，更新末尾的统计表

## 状态流转

```
🔴 待处理 → 🟡 排查中 → 🔵 修复中 → 🟢 已修复 → ⚪ 已关闭
     │            │           │
     └────────────┴───────────┘
          任何阶段可标记 ⚪ 已关闭（won't fix / by design）
```

## 登记字段说明

| 字段 | 必填时机 | 说明 |
|------|----------|------|
| 标题 | 登记时 | 一句话描述问题 |
| 状态 | 登记时 | 见状态定义 |
| 优先级 | 登记时 | P0/P1/P2 |
| 发现日期 | 登记时 | 问题首次出现的日期 |
| 修复日期 | 修复后 | 代码修复完成的日期 |
| 发现人 | 登记时 | 谁提出的（用户/自测/上线反馈） |
| 影响范围 | 登记时 | 哪些客户端/模型/场景受影响 |
| 现象 | 登记时 | 具体错误信息或表现 |
| 根因 | 排查后 | 定位到的根本原因 |
| 修复提交 | 修复后 | git commit hash |
| 开发记录 | 修复后 | `docs/dev-notes/` 中对应文件 |
| 回归测试 | 验证后 | 测试结果摘要 |
# 开发迭代流程（强制遵守）

> 以下流程为本项目的开发铁律，所有开发活动必须严格遵守。
> 任何偏离必须显式说明原因，不得静默跳过任何步骤。

## 一、分支策略

```
main (受保护)
  └── dev/<type>-<short-desc>   ← 所有开发在此分支
```

### 分支类型

| 类型 | 命名 | 示例 | 生命周期 |
|------|------|------|----------|
| 功能 | `dev/feat-compact-support` | 新功能开发 | 合并后删除 |
| 修复 | `dev/fix-tool-choice-dict` | Bug 修复 | 合并后删除 |
| 重构 | `dev/refactor-config-model` | 代码重构 | 合并后删除 |
| 文档 | `dev/docs-changelog` | 文档更新 | 合并后删除 |

### 分支规则

1. **禁止直接在 `main` 上开发**，所有改动必须在 `dev/` 分支进行
2. 创建分支前必须先 `git pull origin main` 确保基线最新
3. 分支粒度：一个分支只做一件事（一个功能/一个修复/一个重构）
4. 合并前必须通过验证流程（见第五节）
5. 合并使用 squash merge，保持 main 历史整洁
6. 合并后立即删除分支

## 二、开发流程（SOP）

每次开发活动遵循以下严格步骤，**不得跳过**：

### Phase 0: 接收任务

```
输入: 任务描述（需求/Bug/重构）
输出: 开发记录文件 + 问题登记（如适用）
```

1. 明确任务类型：`feat` | `fix` | `refactor` | `docs`
2. 创建开发记录：`docs/dev-notes/YYYY-MM-DD-<主题>.md`
3. 在开发记录中写入：
   - **目标**：一句话描述要达成什么
   - **验收标准**：怎样才算完成（必须可验证）
   - **影响范围**：涉及哪些文件/模块
   - **风险评估**：可能影响的其他功能
4. **如果是 Bug 或问题**：立即在 `docs/ISSUE-TRACKER.md` 新增条目，分配编号 #NNN
5. 在开发记录中关联问题编号：`关联问题: #NNN`

### Phase 1: 准备环境

```
输入: 开发记录
输出: 干净的开发分支 + 备份
```

1. `git pull origin main` — 拉取最新代码
2. `git checkout -b dev/<type>-<short-desc>` — 创建开发分支
3. **备份**（满足任一条件即执行）：
   - 改动涉及 `src/proxy.py`（核心模块）
   - 改动涉及 `src/config_store.py`（配置模型）
   - 改动涉及 3 个以上源文件
   - 重构类任务
   - 备份方法：
     ```powershell
     # 源码快照
     $ts = Get-Date -Format "yyyyMMdd-HHmmss"
     $snapDir = "backups\src-snapshots\src-$ts"
     Copy-Item -Path "src" -Destination $snapDir -Recurse
     # 配置快照（如涉及 config.json）
     if (Test-Path "config.json") {
       Copy-Item "config.json" "backups\config-snapshots\config-$ts.json"
     }
     ```
4. 确认代理未运行（避免热修改导致不一致）

### Phase 2: 编码

```
输入: 开发记录 + 备份
输出: 代码改动
```

1. **读后写**：修改文件前先通读该文件的全部导出接口和直接调用方
2. **最小改动**：只改必须改的，不顺手优化相邻代码
3. **关键注释**：在改动处添加 `# WHY:` 注释说明原因（非显而易见的逻辑）
4. **每步验证**：每完成一个逻辑单元（一个函数/一个类），立即运行相关测试
5. **增量提交**：每完成一个逻辑单元，做一次 git commit：
   ```bash
   git add <具体文件>
   git commit -m "<type>(<scope>): <描述>"
   ```

### Phase 3: 自测验证

```
输入: 代码改动
输出: 验证结果
```

1. **基础验证**（必须全部通过）：
   - `python -c "from src import proxy, config_store, cli, platform_utils, safe_io"` — 模块导入无报错
   - `python tests/smoke_test.py` — 冒烟测试通过
   - 启动代理 `python app.py`，GUI 无异常
2. **功能验证**（根据改动类型选择）：
   - 协议转换改动：分别测试 Messages API 和 Responses API，流式和非流式
   - 配置模型改动：保存/加载 config.json 无异常
   - GUI 改动：所有面板可操作，配置可保存
   - CLI 改动：所有子命令正常
3. **回归验证**：
   - 确保改动没有破坏已有功能
   - 重点关注：`src/proxy.py` 改动后 5 个模型的消息收发
4. 验证结果记录到开发记录文件

### Phase 4: 文档同步

```
输入: 验证通过的代码
输出: 更新的文档
```

1. 更新 `docs/CHANGELOG.md`：
   - 在 `[Unreleased]` 下添加条目
   - 标明优先级：`P0`(阻塞级) / `P1`(重要) / `P2`(改进)
   - 格式：`- **P<级>**: <描述>`，说明问题、原因、修复方式
2. 更新 `docs/PROJECT-INDEX.md`（如涉及目录/文件结构变更）
3. 完善开发记录文件的"改动摘要"和"验证结果"部分

### Phase 5: 提交合并

```
输入: 验证通过 + 文档更新
输出: 合并到 main 的干净提交
```

1. 最终检查：
   ```bash
   git status                    # 无遗漏文件
   git diff main --stat          # 确认改动范围合理
   ```
2. Squash merge 到 main：
   ```bash
   git checkout main
   git merge --squash dev/<type>-<short-desc>
   git commit -m "<type>(<scope>): <一句话描述>"
   ```
3. 合并后验证：`python tests/smoke_test.py`
4. 删除开发分支：`git branch -d dev/<type>-<short-desc>`
5. 推送：`git push origin main`

## 三、Bug 修复流程

Bug 修复遵循开发流程，但增加以下强制步骤：

### Phase B1: 复现与登记

1. **立即登记**：在 docs/ISSUE-TRACKER.md 新增条目，分配编号 #NNN，状态 🔴 待处理
2. **必须先复现**：无法复现的 Bug 不得进入修复流程
3. 复现后更新 ISSUE-TRACKER：状态 → 🟡 排查中，填写现象、影响范围
4. 在开发记录中记录：
   - **现象**：具体错误信息和触发条件
   - **复现步骤**：1-2-3 步骤可复现
   - **环境**：客户端类型、模型、操作系统
   - **影响范围**：影响哪些客户端/模型/场景
5. 优先级判定：
   - **P0**：阻塞核心功能（如代理无法启动、所有请求 502）
   - **P1**：重要功能异常（如特定模型空响应、特定客户端不兼容）
   - **P2**：体验问题（如日志格式、UI 细节）

### Phase B2: 定位根因

1. 开启调试日志（输出到 `debug/logs/`）
2. 如需抓包，保存请求/响应到 `debug/dumps/`
3. **找到根因后才能动手改代码**，禁止盲修
4. 在开发记录中记录根因分析
5. 更新 ISSUE-TRACKER：状态 → 🔵 修复中，填写根因

### Phase B3: 修复与验证

1. 修复代码，添加 `# WHY: fix for <bug描述>` 注释
2. 验证修复：
   - 原 Bug 已修复
   - 未引入新问题
   - 回归测试通过
3. 如修复涉及 `src/proxy.py`，必须测试所有 5 个内置模型

### Phase B4: 补充测试

1. 为该 Bug 添加回归测试（防止复发）
2. 测试用例放入 `tests/` 对应文件

## 四、回滚流程

### 4.1 代码回滚

| 场景 | 方法 | 命令 |
|------|------|------|
| 开发分支中单次提交有问题 | revert 该提交 | `git revert <commit-hash>` |
| 开发分支整体方向错误 | 丢弃分支，重新开始 | `git checkout main && git branch -D dev/<branch>` |
| 合并到 main 后发现问题 | revert 合并提交 | `git revert -m 1 <merge-commit>` |
| 需要回到某个已知好版本 | 创建新分支从该版本开始 | `git checkout -b dev/fix-rollback <good-commit>` |

### 4.2 源码恢复（从备份）

```powershell
# 恢复 src/ 快照
$snapDir = "backups\src-snapshots\src-<timestamp>"
Remove-Item -Recurse -Force "src"
Copy-Item -Recurse $snapDir "src"

# 恢复 config.json
Copy-Item "backups\config-snapshots\config-<timestamp>.json" "config.json"
```

### 4.3 回滚后必做

1. 验证恢复后代码可正常运行
2. 记录回滚原因到开发记录
3. 如已推送到远程，同步回滚操作

## 五、发布流程

### 5.1 发布前检查清单

- [ ] 所有 `dev/` 分支已合并或废弃
- [ ] `VERSION` 文件已更新
- [ ] `docs/CHANGELOG.md` 中 `[Unreleased]` 改为版本号
- [ ] `docs/ITERATION-LOG-v<版本>.md` 已创建
- [ ] 冒烟测试全部通过
- [ ] 所有 5 个内置模型 API 测试通过

### 5.2 发布步骤

1. **发布前备份**：
   ```powershell
   $ver = (Get-Content VERSION).Trim()
   Copy-Item -Recurse "src" "backups\pre-release\src-v$ver"
   if (Test-Path "config.json") { Copy-Item "config.json" "backups\pre-release\config-v$ver.json" }
   ```
2. **构建**：
   ```powershell
   .\build\build_exe.ps1
   ```
3. **构建后验证**：运行 release 中的 exe，确认可启动
4. **创建 Git Tag**：
   ```bash
   git tag -a "v$ver" -m "Release v$ver"
   git push origin "v$ver"
   ```
5. **GitHub Release**：上传 release 产物 + SHA256SUMS

### 5.3 版本号规则

- 格式：`MAJOR.MINOR.PATCH`（语义化版本）
- **PATCH**：Bug 修复，无新功能，无破坏性变更
- **MINOR**：新功能，向后兼容
- **MAJOR**：破坏性变更（API 不兼容、配置格式变更等）
- 版本号仅维护在 `VERSION` 文件，构建脚本自动读取

## 六、Git Commit 规范

### 格式

```
<type>(<scope>): <subject>

[可选 body 详细说明]
```

### Type

| Type | 含义 | 示例 |
|------|------|------|
| `feat` | 新功能 | `feat(proxy): add /compact endpoint` |
| `fix` | Bug 修复 | `fix(proxy): convert tool_choice dict to string` |
| `refactor` | 重构（不改行为） | `refactor(config): extract model defaults` |
| `docs` | 文档 | `docs: update CHANGELOG for v4.5.1` |
| `test` | 测试 | `test: add tool_choice regression test` |
| `chore` | 构建/工具 | `chore(build): update PyInstaller config` |
| `perf` | 性能 | `perf(proxy): reduce memory in SSE buffering` |

### Scope

| Scope | 涵盖范围 |
|-------|----------|
| `proxy` | `src/proxy.py` |
| `cli` | `src/cli.py` |
| `gui` | `src/pyqt_gui.py` |
| `config` | `src/config_store.py` |
| `platform` | `src/platform_utils.py` |
| `io` | `src/safe_io.py` |
| `build` | `build/` |
| `docs` | `docs/` |
| `test` | `tests/` |

### 规则

- subject 不超过 50 字符，用英文小写，不加句号
- body 可选，用于说明 WHY（非 WHAT）
- 一个 commit 只做一件事

## 七、开发记录规范

### 文件命名

```
docs/dev-notes/YYYY-MM-DD-<主题>.md
```

### 模板

```markdown
# <任务标题>

**日期**: YYYY-MM-DD
**类型**: feat | fix | refactor | docs
**分支**: dev/<type>-<desc>
**状态**: 🚧 进行中 | ✅ 已完成 | ❌ 已回滚

## 目标

<!-- 一句话描述要达成什么 -->

## 验收标准

- [ ] <可验证的条件 1>
- [ ] <可验证的条件 2>

## 影响范围

- 涉及文件：`src/xxx.py`, `src/yyy.py`
- 风险评估：<低/中/高>，原因

## 实施记录

### Step 1: <描述>
- 改动：
- 验证：

### Step 2: <描述>
- 改动：
- 验证：

## 验证结果

| 测试项 | 结果 |
|--------|------|
| 模块导入 | ✅/❌ |
| 冒烟测试 | ✅/❌ |
| 功能验证 | ✅/❌ |
| 回归验证 | ✅/❌ |

## 改动摘要

<!-- 简述最终改了什么、为什么 -->

## 回滚方案

<!-- 如果出问题怎么回 -->
```

## 八、紧急修复（Hotfix）流程

当生产环境出现 P0 问题需紧急修复时，可跳过部分开发流程，但必须事后补全：

1. **立即登记**：在 `docs/ISSUE-TRACKER.md` 新增条目（状态 🔴，优先级 P0）
2. 在 `main` 上直接创建 `dev/fix-hotfix-<desc>` 分支
3. 最小改动修复问题
4. 快速验证（至少冒烟测试 + 受影响模型 API 测试）
5. 合并到 main，推送
6. **立即更新** ISSUE-TRACKER：状态 → 🟢 已修复，填写修复提交和验证结果
7. **24 小时内补全**：开发记录、CHANGELOG、回归测试

---

# 完整目录结构

```
shutucodeproxy/
├── app.py                          # 应用入口（GUI 或 CLI 自动选择）
├── VERSION                         # 版本号（当前 4.5.1）
├── README.md                       # 项目说明
├── AGENTS.md                       # 本文件 — 开发规范与目录索引
├── .gitignore                      # Git 忽略规则
│
├── src/                            # ★ 源码模块
│   ├── proxy.py                    # 核心代理：协议转换、SSE 流式、重试
│   ├── cli.py                      # CLI 命令：start/stop/status/config/test
│   ├── pyqt_gui.py                 # PyQt5 GUI：系统托盘、配置面板、日志
│   ├── config_store.py             # 配置模型：AppConfig/ModelConfig
│   ├── platform_utils.py           # 平台检测：OS、路径、端口
│   ├── safe_io.py                  # 安全 IO：原子写入、自动备份
│   └── linux_launcher.py           # Linux 桌面包启动器
│
├── debug/                          # ★ 调试专区（不入 Git 运行时内容）
│   ├── logs/                       #   调试日志（比 proxy 日志更详细）
│   ├── dumps/                      #   异常转储、请求/响应快照
│   └── profiling/                  #   性能剖析报告
│
├── logs/                           # ★ 运行日志
│   ├── proxy/                      #   代理运行日志（按日期轮转）
│   └── build/                      #   构建过程日志
│
├── backups/                        # ★ 备份（不入 Git）
│   ├── pre-release/                #   发布前源码+配置快照
│   ├── config-snapshots/           #   config.json 历史版本
│   └── src-snapshots/              #   关键改动前 src/ 完整备份
│
├── release/                        # ★ 发布产物（exe/zip/checksums）
│
├── build/                          # ★ 构建脚本与缓存
│   ├── build_exe.ps1               #   Windows 构建（OneFile / OneDir）
│   ├── build_exe.bat               #   Windows 批处理入口
│   ├── build_unix.sh               #   Linux/macOS 构建
│   ├── requirements-build.txt      #   构建依赖
│   └── shtucodeproxy.ico           #   应用图标
│
├── docs/                           # ★ 文档
│   ├── CHANGELOG.md                #   全量变更日志
│   ├── CHANGELOG-v4.3.3.md         #   版本变更记录
│   ├── CHANGELOG-v4.4.0.md         #   版本变更记录
│   ├── ISSUE-TRACKER.md           #   ★ 问题追踪清单（唯一追踪入口）
│   ├── ITERATION-LOG-v4.5.md       #   迭代开发记录
│   ├── PROJECT-INDEX.md            #   项目目录索引
│   ├── config.example.json         #   配置示例
│   ├── headless-config.example.json#   无头模式配置示例
│   ├── CONTRIBUTING.md             #   贡献指南
│   ├── SECURITY.md                 #   安全策略
│   ├── LICENSE                     #   MIT 许可
│   └── dev-notes/                  #   ★ 开发笔记（每次重要改动一个文件）
│
├── tests/                          # ★ 测试
│   ├── smoke_test.py               #   冒烟测试
│   ├── api_notes.py                #   API 笔记/测试辅助
│   ├── fixtures/                   #   测试固件（示例请求/响应 JSON）
│   ├── integration/                #   集成测试
│   └── reports/                    #   测试报告输出
│
├── tmp/                            # ★ 临时工作区（随时可清空，不入 Git）
│
└── .github/                        # CI/CD
    └── workflows/
        └── build-linux-release.yml #   Linux 发布构建
```

### 目录用途详解

| 目录 | 用途 | Git 策略 |
|------|------|----------|
| `src/` | 所有源码模块，修改必经之地 | ✅ 入 Git |
| `debug/` | 调试专用，放详细日志、异常转储、性能剖析 | ❌ 内容不入 Git，仅保留 .gitkeep |
| `logs/` | 运行日志分区：proxy 代理日志 + build 构建日志 | ❌ 内容不入 Git，仅保留 .gitkeep |
| `backups/` | 发布前快照、配置历史、源码备份，改大改前必备份 | ❌ 内容不入 Git，仅保留 .gitkeep |
| `release/` | 发布产物（exe/zip/checksums），通过 GitHub Releases 分发 | ❌ 二进制不入 Git |
| `build/` | 构建脚本 + PyInstaller 缓存 | ✅ 脚本入 Git，缓存不入 |
| `docs/` | 文档中心，含 CHANGELOG 和开发笔记 | ✅ 全部入 Git |
| `docs/dev-notes/` | 每次重要改动的开发记录，格式：`YYYY-MM-DD-主题.md` | ✅ 入 Git |
| `tests/` | 测试代码 + 固件 + 集成测试 + 报告 | ✅ 代码入 Git，报告不入 |
| `tmp/` | 临时文件、实验脚本，随时可清空 | ❌ 内容不入 Git |

---

# 项目架构

### 依赖关系

```
app.py
  ├── pyqt_gui.py → config_store.py → platform_utils.py
  │                        └→ safe_io.py
  └── cli.py      → config_store.py → platform_utils.py
                  └→ proxy.py                  └→ safe_io.py
```

### 协议适配规则

- Anthropic Messages (`/v1/messages`) → 上游 OpenAI Responses (`/v1/responses`)
- GPT 系列模型使用 `responses` API 格式
- GLM、Qwen、DeepSeek 等 Chat 模型使用 `chat_completions` 格式
- 流式响应必须正确转换 SSE 事件格式

---

# 开发规范

### 代码风格
- Python 3.10+ 兼容，使用 `from __future__ import annotations`
- 类型注解：所有公开函数必须有参数和返回值类型
- 错误处理：显式捕获具体异常，禁止裸 `except`
- 日志：使用 Python `logging` 模块，不使用 `print` 做日志输出（CLI 提示除外）

### 配置文件
- 运行时配置：`config.json`（项目根目录，不入 Git）
- 配置模型定义在 `config_store.py`，修改配置结构必须同步更新该文件
- API Key 等敏感信息仅在 config.json，绝不硬编码

### 构建与发布
- Windows: `.\build\build_exe.ps1`（支持 OneFile / OneDir）
- Linux: `./build/build_unix.sh`
- 版本号仅在 `VERSION` 文件维护，构建脚本自动读取
- 发布产物放在 `release/` 目录

### 测试
- 冒烟测试：`tests/smoke_test.py`
- 集成测试：`tests/integration/`
- 测试固件：`tests/fixtures/`（示例请求/响应 JSON）
- API 测试前确保代理已启动（`http://127.0.0.1:8082`）
- 修改协议转换逻辑后必须手动验证流式和非流式两种模式

---

# 已知限制（开发时注意）

- Chat 模型高并发可能返回空响应，代理有重试和回退机制
- Qwen 推理模式输出在 `reasoning` 字段，代理会提取但展示原始推理
- Token 用量为字符数估算（上游不返回精确值）
- 图片/多模态为 best-effort
- 仅针对校园 GenAI API，未做第三方兼容

# 参考项目

## cc-switch（重点借鉴）

- **仓库**: https://github.com/farion1231/cc-switch (★92k+)
- **本地路径**: C:\上海科技大学\脚本\cc-switch
- **技术栈**: Tauri + React/TypeScript 前端 + Rust 后端
- **定位**: 跨平台桌面 All-in-One 助手，支持 Claude Code / Codex / Gemini CLI 等

### 核心代理架构（`src-tauri/src/proxy/`）

| 模块 | 文件 | 功能 | 借鉴价值 |
|------|------|------|----------|
| **核心转发** | `forwarder.rs` (140KB) | 请求转发主逻辑 | ★★★ 协议适配、流式处理 |
| **请求处理** | `handlers.rs` (56KB) | 路由分发、请求预处理 | ★★★ 路由设计 |
| **响应处理** | `response_processor.rs` (38KB) | 响应后处理 | ★★☆ |
| **SSE 流式** | `sse.rs` (12KB) | SSE 事件解析/转发 | ★★★ 流式转换 |
| **模型映射** | `model_mapper.rs` (11KB) | 模型名路由映射 | ★★★ 模型路由 |
| **Provider 路由** | `provider_router.rs` (19KB) | 多上游路由选择 | ★★☆ 故障转移 |
| **熔断器** | `circuit_breaker.rs` (18KB) | 上游故障熔断 | ★☆☆ |
| **故障转移** | `failover_switch.rs` (4KB) | 自动切换上游 | ★★☆ |
| **Thinking 处理** | `thinking_rectifier.rs` (24KB) | thinking 参数修正 | ★★★ 与我方 bug 同类 |
| **媒体清洗** | `media_sanitizer.rs` (22KB) | 图片/多模态处理 | ★☆☆ |
| **会话管理** | `session.rs` (20KB) | 请求会话追踪 | ★☆☆ |

### Provider 适配层（`src-tauri/src/proxy/providers/`）

| 文件 | 功能 | 借鉴价值 |
|------|------|----------|
| `transform_responses.rs` (63KB) | Responses API 格式转换 | ★★★ 核心参照 |
| `transform_codex_chat.rs` (110KB) | Codex Chat 格式转换 | ★★★ 核心参照 |
| `streaming_responses.rs` (65KB) | Responses 流式转换 | ★★★ 核心参照 |
| `streaming_codex_chat.rs` (48KB) | Codex Chat 流式转换 | ★★★ 核心参照 |
| `claude.rs` (81KB) | Claude API 适配 | ★★☆ |
| `codex.rs` (33KB) | Codex 适配 | ★★☆ |
| `transform.rs` (60KB) | 通用转换框架 | ★★☆ |
| `auth.rs` (8KB) | 认证处理 | ★☆☆ |

### 用量统计（`src-tauri/src/proxy/usage/`）

| 文件 | 功能 | 借鉴价值 |
|------|------|----------|
| `parser.rs` (40KB) | token 用量解析 | ★★☆ 精确 token 计算 |
| `calculator.rs` (9KB) | 用量计算 | ★☆☆ |
| `logger.rs` (14KB) | 用量日志 | ★☆☆ |

### 借鉴要点

1. **协议转换**: `transform_*.rs` 是最核心的参考，覆盖 Responses / Codex Chat / Gemini 三种格式互转
2. **流式处理**: `streaming_*.rs` 和 `sse.rs` 参考其 SSE 事件解析和转发模式
3. **Thinking 修正**: `thinking_rectifier.rs` 和 `thinking_budget_rectifier.rs` 与我方已修复的 thinking bug 同类
4. **故障转移**: `circuit_breaker.rs` + `failover_switch.rs` 可参考其熔断和自动切换机制
5. **模型路由**: `model_mapper.rs` + `provider_router.rs` 参考多模型多上游路由设计

# 生产环境规则

## 端口保护

- **8082 端口是生产端口**，本地已运行 SHTUCodeProxy 代理服务
- **严禁 kill 或重启 8082 上的进程**
- **开发测试必须使用其他端口**（如 8083、8084 等）
- 测试启动方式：`python src/proxy.py --port 8083` 或修改 config.json 的 port 字段
- 测试完成后关闭测试进程

# 环境信息

- 开发环境：Windows 11, Python 3.14.5, PowerShell 5.1
- UTF-8 无 BOM 强制：所有文件读写遵循全局 AGENTS.md 规则十二
- PowerShell 安全调用：遵循全局 AGENTS.md 规则十三
- 上游 API：校园 GenAI 平台（内网地址）

# 强制测试模型清单

> **规则：任何涉及 proxy.py 的改动，必须对以下所有模型进行端到端测试，缺一不可。**

| 模型 | API 格式 | 用途 |
|------|----------|------|
| GPT-5.5 | responses | OpenAI Responses API 格式验证 |
| glm-chat | chat_completions | GLM 模型 + chat_completions 格式验证 |
| deepseek-chat | chat_completions | DeepSeek 模型 + chat_completions 格式验证 |
| qwen-instruct | chat_completions | Qwen 模型 + chat_completions 格式验证（含工具调用/识图） |
| deepseek-pro | chat_completions | DeepSeek Pro 模型（如 API key 可用） |

## 测试矩阵

每个模型必须测试以下 4 种场景：

| 场景 | 说明 |
|------|------|
| 流式 + thinking | 请求含 `thinking: {type: enabled}`，验证 `redacted_thinking` 注入 |
| 流式 无 thinking | 请求不含 thinking 参数，验证无 thinking block |
| 非流式 + thinking | `stream: false` + thinking，验证非流式注入 |
| 非流式 无 thinking | `stream: false` 无 thinking，验证行为不变 |

**最低通过标准**：5 模型 × 4 场景 = 20 测试，至少 19 PASS（已知上游问题可豁免，但必须记录）

# API Key 管理

- **密钥文件**：`secrets/api-keys.json`（已 gitignore，不会提交到 Git）
- **打包排除**：`secrets/` 目录不会被 PyInstaller 打包（build 脚本仅打包显式指定的文件）
- **开发测试**：从 `secrets/api-keys.json` 读取 key 写入 `src/config.json`（config.json 也已 gitignore）
- **生产部署**：API key 通过环境变量 `UPSTREAM_API_KEY` 或在运行时 config.json 中配置

## 密钥更新流程

1. 更新 `secrets/api-keys.json`
2. 运行 `python -c "import json; s=json.load(open('secrets/api-keys.json')); c=json.load(open('src/config.json')); km={mid:i['api_key'] for mid,i in s['models'].items()}; [m.__setitem__('api_key',km[m['model_id']]) for m in c['models'] if m['model_id'] in km]; json.dump(c,open('src/config.json','w'),indent=2,ensure_ascii=False)"`
3. 重启代理生效


# 发布流程（GitHub Release）

> 以下为从代码修改到 GitHub 发布的完整流程，所有版本发布必须严格遵守。

## 一、发布前准备

1. 确认所有代码修改已在 `dev/` 分支完成并通过验证
2. 更新 `VERSION` 文件为新版本号（如 `4.6.3`）
3. 更新 `docs/CHANGELOG.md` 记录变更内容
4. 在 `docs/ISSUE-TRACKER.md` 中更新相关问题状态

## 二、本地构建 Windows 包

```powershell
Set-Location "C:\上海科技大学\脚本\shutucodeproxy"

# 更新 spec 文件版本号
# 将 SHTUCodeProxy.spec 或 SHTUCodeProxy-v{旧版本}-windows-x64.spec 复制为新版本
# 替换其中的 name 和 version 引用

# PyInstaller 构建（Windows onefile 模式）
python -m PyInstaller "SHTUCodeProxy-v{版本}-windows-x64.spec" --noconfirm

# 构建产物位于 dist/ 目录：
#   dist/SHTUCodeProxy-v{版本}-windows-x64.exe
```

### spec 文件说明

- 位置：项目根目录 `SHTUCodeProxy-v{版本}-windows-x64.spec`
- 从上一版本 spec 复制，修改版本号即可
- 使用 `--onefile` 模式，所有 Python 源码嵌入单个 exe
- 依赖：`app.py`（入口）+ `src/` 下所有 `.py` 文件 + `VERSION` + `docs/headless-config.example.json`

### 本地测试（不干扰生产）

```powershell
# 在非生产端口启动测试代理
python src/proxy.py --port 8090

# 验证 /v1/models 端点
python -c "import urllib.request,json; print(json.dumps(json.loads(urllib.request.urlopen('http://127.0.0.1:8090/v1/models').read()),indent=2,ensure_ascii=False))"

# 测试完成后关闭测试进程
```

## 三、Git 提交与标签

```powershell
Set-Location "C:\上海科技大学\脚本\shutucodeproxy"

# 创建开发分支
git checkout -b dev/feat-{简短描述}

# 提交代码
git add VERSION src/config_store.py src/proxy.py  # 按实际修改的文件
git commit -m "v{版本}: {简短描述}"

# 推送分支
git push origin dev/feat-{简短描述}

# 合并到 main
git checkout main
git merge dev/feat-{简短描述}
git push origin main

# 创建标签
git tag -a v{版本} -m "v{版本}: {简要说明}"
git push origin v{版本}
```

## 四、GitHub Release 创建

```powershell
Set-Location "C:\上海科技大学\脚本\shutucodeproxy"

# 使用 gh CLI（需先 gh auth login）
gh release create v{版本} `
  "dist\SHTUCodeProxy-v{版本}-windows-x64.exe" `
  --title "v{版本} - {发布标题}" `
  --notes "## v{版本} - {发布标题}

### 问题
{问题描述}

### 修复
{修复说明}

### 兼容性
- Codex CLI 不受影响
- Claude Code Desktop {影响说明}
- 所有现有配置向后兼容"
```

### gh CLI 首次配置

```powershell
# 安装（如未安装）
winget install --id GitHub.cli

# 登录（浏览器授权）
& "C:\Program Files\GitHub CLI\gh.exe" auth login

# 验证
& "C:\Program Files\GitHub CLI\gh.exe" auth status
```

### 注意事项

- **git credential fill 会挂起**：不要用 `git credential fill` 获取 token，直接用 `gh auth login` 浏览器授权
- **PowerShell 中 gh 报错**：gh 的 stderr 输出被 PowerShell 当作错误，实际命令可能已成功，检查 exit code
- **Release 已存在**：如果 tag 推送时自动创建了空 release，用 `gh release delete v{版本} --yes` 删除后重建

## 五、GitHub Actions 构建 Linux 包

项目配置了两个 GitHub Actions 工作流：

### Linux 构建（手动触发）

- 工作流：`.github/workflows/build-linux-release.yml`
- 触发方式：GitHub Actions 页面手动 `workflow_dispatch`
- 输入参数：
  - `tag`：Release 标签（如 `v4.6.2`）
  - `ref`：构建的 Git 引用（默认 `main`）
- 构建产物：
  - `SHTUCodeProxy-v{版本}-linux-x86_64-python-launcher.tar.xz`（GUI 目录包）
  - `SHTUCodeProxy-v{版本}-linux-x86_64-headless-cli.zip`（无头 CLI 包）
  - `SHTUCodeProxy-v{版本}-linux-x86_64`（单文件 GUI）
  - `shtucodeproxyctl-v{版本}-linux-x86_64`（单文件 CLI）
- 所有产物自动上传到对应 tag 的 Release

### 触发步骤（gh CLI）

```powershell
Set-Location "C:\上海科技大学\脚本\shutucodeproxy"
& "C:\Program Files\GitHub CLI\gh.exe" workflow run "Build Linux Release Asset" -f tag=v{版本}
# 命令返回 Actions 运行 URL，可查看构建状态
```

也可浏览器手动触发：https://github.com/saberjack/SHTUCodeProxy/actions → "Build Linux Release Asset" → Run workflow → 输入 tag

> **注意**：`-f tag=` 必须显式指定，该输入无默认值。`--ref` 参数仅控制 checkout 的 Git 引用，不等于 tag 输入。

### Windows 构建（可选 CI）

- 工作流：`.github/workflows/release-windows.yml`
- 触发方式：推送 `v*` 标签自动触发，或手动 `workflow_dispatch`
- 如需本地构建则无需此工作流

## 六、完整发布清单

### Release 产物清单（共 8 项）

每个版本必须包含以下全部产物，缺一不可：

| # | 产物 | 来源 | 说明 |
|---|------|------|------|
| 1 | `SHTUCodeProxy-v{版本}-windows-x64.exe` | `build_exe.ps1` 或本地 PyInstaller | 单文件 Windows GUI |
| 2 | `SHTUCodeProxy-v{版本}-windows-x64.zip` | `build_exe.ps1` | 便携 Windows 文件夹包 |
| 3 | `SHTUCodeProxy-v{版本}-source-linux-macos.zip` | `build_exe.ps1` | Linux/macOS 源码包 |
| 4 | `SHA256SUMS.txt` | `build_exe.ps1` | 产物 SHA256 校验 |
| 5 | `SHTUCodeProxy-v{版本}-linux-x86_64` | GitHub Actions Linux workflow | 单文件 Linux GUI |
| 6 | `SHTUCodeProxy-v{版本}-linux-x86_64-python-launcher.tar.xz` | GitHub Actions Linux workflow | Linux GUI 目录包 |
| 7 | `SHTUCodeProxy-v{版本}-linux-x86_64-headless-cli.zip` | GitHub Actions Linux workflow | Linux 无头 CLI 包 |
| 8 | `shtucodeproxyctl-v{版本}-linux-x86_64` | GitHub Actions Linux workflow | Linux 单文件 CLI |

> **v4.6.2 教训**：手动只构建了 onefile exe + source.zip，缺少 windows-x64.zip、source-linux-macos.zip 和 SHA256SUMS.txt。以后必须走 `build_exe.ps1` 全量构建或触发 Windows CI workflow。

### 发布步骤清单

| 步骤 | 命令/操作 | 验证 |
|------|-----------|------|
| 1. 代码修改 | `dev/` 分支开发 | 本地测试通过 |
| 2. 更新 VERSION | 编辑 `VERSION` 文件 | 版本号正确 |
| 3. 更新 CHANGELOG | 编辑 `docs/CHANGELOG.md` | 变更记录完整 |
| 4. 全量构建 Windows 产物 | build/build_exe.ps1 | release/ 下有 exe + zip + source + SHA256 |
| 5. 本地验证 | `python src/proxy.py --port 8090` | `/v1/models` 正常 |
| 6. Git 提交推送 | `git add/commit/push` | 代码在 main |
| 7. 创建 tag | `git tag -a v{版本} && git push origin v{版本}` | tag 存在 |
| 8. 创建 Release | `gh release create v{版本} release/*` | Release 页面可见 |
| 9. 构建 Linux 包 | `gh workflow run "Build Linux Release Asset" -f tag=v{版本}` | 4 个 Linux 产物附加 |
| 10. 核对产物 | 对比上方 8 项清单 | 缺项补齐 |
| 11. 通知用户 | 分发新版本 | 用户更新部署 |

# Fix #001 记录

- **问题**：Claude Code auto mode 不识别模型支持 extended thinking
- **修复**：在响应中注入 synthetic `redacted_thinking` content block
- **分支**：`dev/fix-thinking-block-injection`
- **验证**：4 模型 × 4 场景 = 15/16 PASS（qwen-instruct 流式空文本为已知上游问题）
- **详情**：`docs/dev-notes/2026-06-06-thinking-block-injection.md`


# 实战教训：PowerShell 引号与反引号陷阱

> 以下教训来自 v4.6.2 发布过程中反复踩坑的真实经历，必须严格遵守。

## 核心原则：绝不用 PowerShell 字符串拼接修改文件内容

PowerShell 的字符串处理（双引号变量展开、反引号转义、here-string 解析）会在处理 Markdown/代码片段时产生不可预期的结果。**已经因此翻车多次。**

### 禁止做法

```powershell
# BAD: PowerShell .Replace() 处理含反引号、$、代码围栏的 Markdown 文本
$content = $content.Replace("old", "new")

# BAD: PowerShell here-string 构造含 ``` 围栏的 Markdown 块
# 反引号会被 PowerShell 转义，变量会被展开，代码围栏会损坏

# BAD: 任何在 PowerShell 中拼接 Markdown 代码块的操作
```

### 必须做法

```powershell
# GOOD: 写 Python 脚本执行文本替换
$scriptPath = "C:\\path\\to\\_fix.py"
# 用单引号 here-string 写 Python 脚本（无特殊字符部分）
# 如含反引号/$ 等，先写不含这些字符的脚本骨架，再用 Python 自身拼接
[System.IO.File]::WriteAllText($scriptPath, $py, [System.Text.UTF8Encoding]::new($false))
python $scriptPath
Remove-Item $scriptPath
```

### 具体翻车记录

1. AGENTS.md 触发步骤替换失败（v4.6.2）：PowerShell .Replace() 处理含反引号和 $ 变量的 Markdown 代码块时，围栏被吃掉，粗体星号丢失，--ref 的 -- 被当作运算符。连续 3 次 git diff 显示格式损坏。最终改用 Python 脚本一次成功。

2. gh workflow run 参数传递（v4.6.2）：--ref v4.6.2 只控制 Git checkout，不等于 workflow input tag。必须用 -f tag=v4.6.2 传递。第一次触发用了默认值 v4.4.2 导致 Linux 产物上传失败。

3. node -e 中文乱码（历史）：PowerShell 管道传中文给外部程序变 ??，已在规则十三记录。

4. Python 转义吞噬 Windows 路径（v4.6.2）：PowerShell here-string 中写 Python r-string 路径 r"C:\path\to\file.py"，写入 .py 文件后 Python 重新读取时 \t 被解释为 tab 字符，路径变为 C:\path<TAB>o\file.py。修复：在 Python 脚本内部用正斜杠路径或 os.path.join()，避免反斜杠转义链。

### 规则

| 场景 | 正确做法 |
|------|----------|
| 替换文件中的 Markdown 文本 | Python 脚本 |
| 构造含代码围栏的内容 | Python 脚本 |
| 替换含反引号/$ 的文本 | Python 脚本 |
| 简单的一行字符串替换（无特殊字符） | PowerShell 可以，但不如 Python 稳妥 |
| gh workflow 传参 | -f key=value，不用 --ref 代替 |

