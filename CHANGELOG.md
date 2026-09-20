# Changelog

用户可见的改动记录在此。版本遵循语义化版本形式；`0.x` 预发布阶段仍可能调整接口。项目首次公开发布之前的本机调试不作为独立发布版本。

## [Unreleased]

### Fixed

- CLI 命名尚未落盘的分页空会话前，通过官方历史读取接口请求持久化并核对文件，避免手动打开命名任务时报 `missing source rollout`；不注入消息、不自动导航桌面。无法确认时拒绝本次命名并提示先发送第一条消息。

- 新建普通对话不再因为尚未生成历史文件而提前打开桌面，避免反复出现 `no rollout found` / `missing source rollout`。
- 首条消息先转发，历史文件可读后再异步建立桌面订阅；等待历史和订阅确认不阻塞 CLI 消息或审批。已有持久任务继续支持恢复和订阅复用。

## [0.1.0-alpha.3] - 2026-09-17

### Fixed

- CLI 实时消息和恢复的历史记录将标准问答回复显示为“问题 / 你的回答”，隐藏同步标签、问题编号和 JSON。
- 保留后台存储及提交内容，关闭通知仍按原问题编号匹配。普通消息、引用示例、附件及未知格式原样传递；退出适配同时停用排版。
- 补充“安装版本不等于旧终端加载版本”的排障说明。

## [0.1.0-alpha.2] - 2026-09-16

### Added

- 实时异步提问兼容层：桌面回答后关闭 CLI 对应提问框；CLI 回答转换为同一任务的桌面兼容消息，无需修改官方 CLI。
- 按连接、任务及问题隔离，已知重复/迟到回答去重；提交失败提示用户检查桌面，不自动重试。
- `CPET_QUESTION_SYNC=0` 关闭适配并恢复原始转发；安装器部署独立的 `question_sync.py`。

### Known limitations

- 仅适配连接后收到的实时问题；历史文本保留，历史未答问题需在桌面回答。
- 标准 CLI 提问框的 `Esc` 保留中断行为；两端完全同时作答没有原子去重保证。
- 原版 CLI `0.154.0-alpha.6.2` 已通过合成事件驱动的真实终端 UI 探针，真实桌面点击与模型的完整端到端验收尚未完成。

## [0.1.0-alpha.1] - 2026-09-16

### Added

- macOS 本机共享 App Server 的启停、登录自启及只读诊断。
- `cx` 交互入口：转发任务并自动建立桌面订阅。
- 订阅状态复用，以及桌面重启、重连、退订和日志轮转后的恢复。
- 临时任务过滤和每个 CLI 连接独立的任务归属判断。
- 使用无路径的 WebSocket 地址及随机 Bearer 凭据，兼容 CLI 的地址校验。
- 固定运行目录与独立依赖环境；安装后可移动或删除源码仓库。
- 中英文 README、排障/架构/兼容性/维护文档、贡献规范、issue/PR 模板、CI 和 MIT 许可证。

### Known limitations

首次订阅可能导航桌面；日志确认不等同于宠物可见；共享后台的文件权限、代理及桌面工具上下文可能与内置后台不同。详见兼容性文档。

[Unreleased]: https://github.com/houhongxu/cpet/compare/v0.1.0-alpha.3...HEAD
[0.1.0-alpha.3]: https://github.com/houhongxu/cpet/releases/tag/v0.1.0-alpha.3
[0.1.0-alpha.2]: https://github.com/houhongxu/cpet/releases/tag/v0.1.0-alpha.2
[0.1.0-alpha.1]: https://github.com/houhongxu/cpet/releases/tag/v0.1.0-alpha.1
