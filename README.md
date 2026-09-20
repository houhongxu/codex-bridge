# Codex Bridge

**让 Codex CLI 与桌面端共享会话。** macOS 上的本地共享后台与会话桥接工具，支持会话接续、任务订阅、异步问答适配和桌面宠物状态展示。

[![CI](https://github.com/houhongxu/cpet/actions/workflows/ci.yml/badge.svg)](https://github.com/houhongxu/cpet/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Status: experimental](https://img.shields.io/badge/status-experimental-orange.svg)](docs/compatibility.md)

简体中文 · [English](README.en.md) · [排障](docs/troubleshooting.md) · [实现原理](docs/architecture.md)

> **实验性项目，当前版本 0.2.0-alpha.1。** Codex Bridge 是社区工具，与 OpenAI 无隶属或背书关系。它依赖桌面应用的任务链接、外部后台连接设置及日志格式；桌面升级可能影响兼容性。首次接入任务可能切换桌面当前页面。

## 0.2 命令迁移（Breaking change）

项目更名为 **Codex Bridge**，唯一入口为 `cb`，不再提供 `cx`、`cpet` 或 `codex-pet`。升级安装会替换安装器管理的别名块并移除指向旧运行副本的 `codex-pet` 链接；用户自己定义的其他文件不会被删除。旧终端执行 `unalias cx cpet 2>/dev/null; source ~/.zshrc`，或打开新终端。

```sh
cb                     # 启动 CLI
cb resume <任务名或ID>  # 接续共享会话
cb status              # 查看后台与桌面接入状态
cb cli --help          # 官方 CLI 帮助
cb --help              # Bridge 管理帮助
cb --version           # Bridge 版本
cb cli --version       # 官方 CLI 版本
```

`on/off/enable/disable/status/install/uninstall/login-start` 是管理子命令；需要把同名参数交给官方 CLI 时使用 `cb cli ...`。配置变量同步改为 `CB_PYTHON`、`CB_QUESTION_SYNC`（旧 `CPET_*` 不再读取）。后台地址、会话目录、launchd 标签和运行目录保持原位，不重启正在运行的任务。旧进程需结束当前工作后重新启动才加载更新。GitHub 仓库 URL 暂仍为 `houhongxu/cpet`。

## 为什么需要它

在终端开始任务，再到桌面查看、接续或处理异步问题，不必维护两套独立会话。Codex Bridge 将 CLI 与桌面连接到同一个本机 App Server，并按需建立桌面任务订阅；原生宠物可使用这些任务状态展示进度。

- `cb` 自动准备共享后台和 CLI 转接，无需手写 `--remote` 地址。
- 已确认的订阅可以复用，连续提问不再逐轮切换桌面页面。
- 检测到桌面重启、连接重建或任务退订后，下次使用任务时尝试恢复订阅。
- 过滤临时任务，避免把没有持久化记录的内部会话打开到桌面。
- 新建空对话等首条消息发送、历史文件可读后再接入桌面，避免提前恢复失败；等待接入不会阻塞 CLI 消息和审批。
- CLI 为尚未落盘的分页空会话命名前，先通过后台持久化历史，避免手动点击桌面列表时恢复失败；不发送占位消息。
- 可选 macOS 登录自启；源码仓库与安装后的运行副本相互独立。
- 实时异步提问默认启用兼容层：桌面回答后关闭 CLI 对应提问框，也可从 CLI 回答，无需修改官方 CLI。

宠物的展示规则由桌面应用决定。**后台可连接、订阅被日志确认、宠物实际显示，是三个不同的验证结果。**

## 使用前确认

- macOS，交互终端使用 zsh；其他 shell 的别名不会自动配置。
- Python 3.9+，建议使用 3.11 或更新的受支持版本；需要 `venv` 和联网安装依赖。
- `/Applications/ChatGPT.app` 或 `/Applications/Codex.app`，应用内包含 `Contents/Resources/codex`。
- 应用内的 CLI 支持 `app-server --listen`、`--remote`、`--remote-auth-token-env`；已在桌面应用中完成登录。
- 若需要宠物显示进度，桌面版本和你的账号必须已有原生宠物功能。cb 不安装或解锁宠物。
- 本机 `127.0.0.1:4500` 可用。已有服务占用时，cb 会报错，不会接管或终止它。

已检查的版本与验证范围见 [兼容性表](docs/compatibility.md)。Windows、Linux、其他厂商 CLI 目前不在支持范围内。

## 快速开始

选择你自己的源码目录，下面以 `~/workspace/cb` 为例：

```sh
mkdir -p ~/workspace
git clone https://github.com/houhongxu/cpet.git ~/workspace/cb
cd ~/workspace/cb
./install.sh
source ~/.zshrc
cb --version
cb on
cb status
```

**首次连接桌面：** 如果桌面应用在执行 `cb on` 前就已经运行，请先结束其中的任务，退出桌面应用，再执行一次 `cb on`。连接设置在桌面应用启动时生效；cb 不会强制重启正在工作的应用。

然后进入需要操作的项目目录：

```sh
cd /path/to/your/project
cb
# 或从所有目录的历史任务中选择恢复：
cb resume --all
```

在桌面中启用宠物，并用一个正在运行或等待确认的普通任务检查显示情况。首次接入可能打开该任务；后续提问复用订阅。`--all` 扩大历史任务选择范围，不会同时运行所有任务。

可选登录自启：

```sh
cb enable
# 取消登录自启，但保留当前运行中的任务：
cb disable
```

## 桌面与 CLI 的异步提问同步

从 `0.1.0-alpha.2` 起，新启动的 `cb` 默认适配**连接后收到的实时异步问题**：桌面回答后，CLI 自动关闭对应提问框；从 CLI 回答则以桌面兼容格式提交到同一任务。官方 CLI 和桌面应用文件均不修改。

CLI 会显示标准提问框，界面与原生异步问题不同；按 `Esc` 会沿用该界面的中断行为。恢复历史任务时保留问题正文，但不重建旧提问框；历史中尚未回答的问题请在桌面回答。已收到的回答及迟到的重复提交会去重，但不能保证两个客户端完全同时作答时只有一份答案。

如收到“cb could not confirm this answer”，请先在桌面确认答案是否到达，再决定是否重答；cb 不自动重试。要关闭兼容层、恢复原始转发行为：

```sh
CB_QUESTION_SYNC=0 cb resume --all
```

验证版本、具体边界见 [兼容性记录](docs/compatibility.md)；升级后要退出旧 `cb` 再恢复任务才生效。

从 `0.1.0-alpha.3` 起，标准问答回复在 CLI 的实时消息和历史记录中显示为：

```text
问题：本次修改哪些页面？
你的回答：只修改列表和模板，其他页面保持不变。
```

标签、问题编号和 JSON 只在 CLI 的显示副本中隐藏；后台保存及提交的原始消息不变，同步仍使用原编号。普通消息、引用代码和无法识别的回复格式保持原样。`CB_QUESTION_SYNC=0` 同时停用此排版。

## 安装与源码分离

| 内容 | 默认位置 |
| --- | --- |
| 源码仓库 | 你选择的 Git clone 目录，可移动或删除 |
| 安装后的代码与独立 Python 环境 | `~/Library/Application Support/Codex CLI Bridge/runtime/` |
| 稳定命令入口 | `~/.local/bin/cb`，指向运行副本 |
| zsh 别名 | `~/.zshrc` 中的 `codex-cli-bridge` 标记块 |
| 自启配置 | `~/Library/LaunchAgents/local.codex-cli-bridge.login.plist` |
| 日志、订阅诊断与备份 | `~/Library/Application Support/Codex CLI Bridge/` |

安装器复制脚本，并在固定运行目录创建 `.venv`；命令不会依赖源码仓库的位置。它使用 `websockets==15.0.1`，从 PyPI 安装到该独立环境，不安装到系统 Python。安装默认不启用登录自启，也不启动后台。

安装器会备份需要修改的 `.zshrc`，拒绝覆盖标记块外已有的 `cb` / `cb` 定义。可用 `CB_PYTHON=/path/to/python3 ./install.sh` 指定安装环境的 Python。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `cb` | 启动交互 CLI，新任务使用终端当前项目目录 |
| `cb resume --all` | 从所有目录的历史任务中选择一个恢复 |
| `cb fork` | 使用 CLI 的任务分支功能，默认保留原任务目录 |
| `cb on` | 准备共享后台、设置桌面连接并打开应用 |
| `cb off` | 停止共享后台并恢复之前的桌面连接设置，会中断依赖它的任务 |
| `cb enable` / `disable` | 开启 / 关闭登录自启 |
| `cb status` / `status --json` | 读取后台状态、宠物设置与最近订阅诊断 |
| `cb --version` | 显示安装版本 |
| `cb uninstall` | 停止服务，移除别名、命令和登录自启，保留运行副本与诊断文件 |

`cb` 面向交互会话及 `resume` / `fork`，不承诺包装所有 Codex 子命令。其他用途可直接使用原始 `codex`。

## 更新、卸载与回退

源码更新不会自动替换运行副本。安装新版脚本不会重启共享后台或桌面；正在运行的 `cb` 仍使用旧代码：

```sh
cd /path/to/cb
git pull --ff-only
./install.sh
cb --version
```

退出旧 `cb` 后重新运行 `cb resume --all`。脚本更新不强制重启后台；桌面应用或其内置 CLI 升级后，应在任务结束后重新检查共享后台连接。

卸载：

```sh
# 先结束所有依赖共享后台的任务
cb uninstall
```

退出并重新打开桌面应用，打开新终端。源码目录不受影响；运行副本和日志保留以便排障，可在确认不再需要后手动移除。`cb off` 不取消登录自启；仅恢复原连接时，应先 `cb disable` 再 `cb off`。

版本回退及完整检查步骤见 [维护与发布](docs/maintaining.md)。

## 已知限制

- 首次订阅或恢复订阅仍可能切换桌面任务页。完全静默的后台订阅尚未实现。
- 订阅确认依赖本机日志；证据不足时记录 `opened_unconfirmed`，正常情况下至少间隔 60 秒再重试。明确失效事件可触发重新接入。
- launchd 后台的代理环境、macOS 文件访问权限和桌面专用工具上下文可能与终端/桌面内置后台不同。已遇到文稿目录权限和桌面工具管道不可用的问题。
- 本地固定共享端口不提供 cb 自己的鉴权；每个 CLI 的临时转接端口使用随机 Bearer 凭据。仅支持本机回环地址，不要向局域网或互联网暴露它。详见 [安全说明](SECURITY.md)。
- 桌面任务静音、活动气泡隐藏、任务已读且空闲等状态会影响宠物显示。
- 自定义 `CODEX_HOME`、非默认应用位置、多个桌面实例与 SSH 远程主机尚未验证。

## 开发与贡献

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/check_repository.py
sh -n install.sh
```

测试使用临时目录和本机临时 WebSocket 端口，不启动真实模型任务、不修改你的登录自启。Linux CI 仅验证隔离的逻辑与协议转发，不能代表 macOS 桌面集成测试。

欢迎可复现的 bug、兼容性报告、文档修正与小范围改进。请先阅读 [贡献指南](CONTRIBUTING.md) 和 [行为准则](CODE_OF_CONDUCT.md)。变更记录见 [CHANGELOG](CHANGELOG.md)，后续方向见 [路线图](docs/roadmap.md)。

## 许可证

[MIT](LICENSE) © 2026 houhongxu。Codex、ChatGPT 及相关产品名称属于其各自权利人；本仓库仅分发 cb 自身的代码，不包含 OpenAI 应用、模型或宠物素材。
