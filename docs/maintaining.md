# 维护、发布与运营

这份文档面向仓库维护者。先保证陌生用户能装、能理解限制、能反馈，再扩大宣传。

## 初始仓库设置

- About：一句话描述 macOS、Codex CLI 与桌面宠物的连接能力。
- Topics：`codex`、`macos`、`cli`、`desktop-pet`、`python`、`websocket`。
- 开启 Issues 和私密漏洞报告，保留明确的 bug/feature 模板；暂不拆分多个社区。
- 开启 GitHub Actions，确认 `CI` 在 `main` 和 PR 上运行。
- 确认 CI 后再将对应检查设为分支合并要求；不要配置不存在的检查名。
- 首版使用预发布标签，不以“稳定版”“全平台”“官方插件”宣传。

## 发布流程

1. 在 `cpet.py` 更新 `__version__`，更新两份 README、CHANGELOG 和兼容性记录。
2. 在 `docs/releases/` 写版本说明：新增/修复、已知限制、验证范围、安装及回退。
3. 运行贡献指南中的检查。CI 通过后，在已知 macOS 环境做必要的手工验证，未测项目明确保留。
4. 在 `main` 的目标提交上创建版本标签，例如 `v0.1.0-alpha.1`。
5. 创建对应 GitHub Release。含 `alpha` / `beta` / `rc` 的版本勾选预发布；说明应使用仓库内已审核的版本说明。
6. 检查 Release 链接、源码归档和安装说明可访问，再发布一次介绍。

回退示例（先结束依赖共享后台的任务）：

```sh
git fetch --tags
git checkout v0.1.0-alpha.1
./install.sh
# 退出旧 CLI 后恢复任务
cx resume --all
```

安装更新会备份变更的运行脚本，但备份不包含完整依赖环境；恢复已发布版本应重新运行对应版本安装器。后台二进制由桌面应用提供，回退 cpet 不能回退桌面应用。

## 前四周怎么运营

| 阶段 | 动作 | 观察结果 |
| --- | --- | --- |
| 第 1 周 | 录一个 20–40 秒真实演示：终端提问 → 首次接入 → 宠物变化 → 再提问不反复跳转 | 找到 3–5 位愿意试装的 macOS 用户，记录能否独立安装 |
| 第 2 周 | 集中处理安装、权限、版本兼容问题；将重复问题写入 FAQ | 安装成功数、复现步骤完整度、尚未解决的阻塞 |
| 第 3 周 | 发布一次带实际修复的版本；给文档/测试小任务标 `good first issue` | 问题是否真的消失，是否有人能按贡献指南完成小改动 |
| 第 4 周 | 根据反馈梳理下一版范围，写简短进展 | 用户是否持续使用、维护耗时是否可承受 |

数字是目标，不是已取得的成果。不要编造用户量、兼容性、下载量或 Star 增长。

## 日常维护节奏

- 每周固定查看一次 Issues/PR，区分 bug、使用问题、版本不兼容和新需求。
- 可复现且影响安装/现有行为的问题优先；大功能先讨论，不立即承诺。
- 不自动关闭仍能复现的旧问题，不为了活跃度制造无意义 issue 或批量发宣传。
- 依赖机器人只提交升级 PR，不自动合并。`websockets` 主版本升级需单独评估 Python 最低版本和协议回归，当前配置不自动提这类升级。
- 桌面应用升级后检查核心链路；必要时更新兼容性表或暂停推荐受影响版本。
- 用 Release/CHANGELOG 对外说明变化，避免让用户依靠提交记录猜升级影响。
- Star 可以观察兴趣；更重要的是安装成功、持续使用、真实问题修复和维护者时间。

## 可复用的首次介绍文案

> 我做了一个 macOS 小工具 cpet，让 Codex CLI 的普通任务接入桌面应用现有的宠物进度显示。`cx` 自动连接共享后台，首次建立订阅后复用，减少重复跳转。源码与安装副本分离，MIT 开源。目前是实验性版本，依赖桌面版本，首次接入仍可能导航页面；欢迎愿意反馈安装和兼容性问题的用户试用。仓库：https://github.com/houhongxu/cpet

配一段真实演示再发。可先在自己的 GitHub 主页、朋友圈或常用开发者社区分享一次；向其他社区投稿前阅读其自荐规则。宣传内容由维护者自行发布，本项目不自动向他人发送消息。

## 参考

- [GitHub 社区健康文件](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
- [Open Source Guides：寻找用户](https://opensource.guide/finding-users/)
- [GitHub 私密漏洞报告](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
