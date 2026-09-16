# Changelog

用户可见的改动记录在此。版本遵循语义化版本形式；`0.x` 预发布阶段仍可能调整接口。项目首次公开发布之前的本机调试不作为独立发布版本。

## [Unreleased]

暂无已合入的未发布行为变更。

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

[Unreleased]: https://github.com/houhongxu/cpet/compare/v0.1.0-alpha.1...HEAD
[0.1.0-alpha.1]: https://github.com/houhongxu/cpet/releases/tag/v0.1.0-alpha.1
