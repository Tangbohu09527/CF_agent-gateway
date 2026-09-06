# PR #10：基础安装与业务开通解耦

本轮从 `24222ff601b8da7a1b21bdb79c3be266d49f4bc7` 继续原分支
`feat/clean-device-deploy-lan` 和 [PR #10](https://github.com/Tangbohu09527/CF_agent-gateway/pull/10)。
开始已 fetch，HEAD、远端分支及 PR Head 一致、工作区干净；未覆盖其他 worktree。
WeChat 固定 `67cbbb04ce15703428ce165ac38effac19f4b701`（PR #7 已合并，merge
`81d21f451adc61df71077ca3035adeddeca4621f`），仅只读检出。

本页记录本轮行为和验收要求；最终实际结果、完整 Gateway Head、日志和 artifact 链接写入
PR #10 的最终验证段。前轮 A/B 通过记录保留在[历史记录](2026-09-06-clean-device-deploy-lan.md)，
不作为本轮新 Head 的通过证据。

## 实施与测试对应

| 要求 | 正式入口/改动 | 本轮必须验证 |
| --- | --- | --- |
| 无员工、群聊或机器人 ID 的基础安装 | `install-clean-device.sh` / `prepare-clean-host.py`；身份文件可选，基础配置独立固化 | A 正式 configure/database/migrate/start；B 空业务安装和真实重启 |
| 显式错误不能当缺省 | 文件权限/JSON/schema/model 校验；显式 initialize 仍严格检查绑定 | 缺省与 null/空/坏文件区分、业务绑定冲突、原子保护与失败重试 |
| 核心与业务状态分开 | start/boot-service 检查迁移和核心；diagnose 读实时 business status | `awaiting_configuration` 不影响核心 healthy；真实故障仍失败，不调用模型 |
| 自动识别机器人账号 | 同一认证客户端 `/api/status/auth` → `loggedInUser` → `source_account_id` | 合成未登录→登录；缺失/畸形账号、错误 Token；没有昵称/UID/旧账号回退 |
| 账号切换隔离 | 既有按账号 Checkpoint/事件/映射/线程；发送前再核实当前账号 | 同账号重登不重复，另一账号不继承授权/线程，旧投递不经新账号发送，不清历史 |
| 安装后正式业务开通 | `business_access discover/status/approve/disable`，复用既有业务服务 | 未授权拒绝→明确批准→新消息完整成功→停用后新消息拒绝 |
| 不重放历史拒绝 | 既有 durable Admission；已知未配置/停用路由也明确持久拒绝 | 批准或补绑定后旧拒绝消息不重放；DB/损坏契约异常不吞掉 |
| 安装重跑不恢复业务授权 | 基础阶段不重新执行 initial_identity；不保存永久空业务状态 | 停用后重跑保持策略 disabled、Secret/配置/历史数据不变 |
| 授权配置跨真实重启保留 | 现有 B 首轮空业务重启后，显式业务开通并第二次 reboot | 正式状态前后一致、授权仍有效、归档观测未处理；不是普通容器重启 |

Controller v1 固定文件/JSON 含义不改，Poll/Delivery 仍是组合 Gate，Dispatch 不归它管理。
不增加 hold-gateway 要求，不修改 WeChat。Gateway 根目录保持 root:root 0750；管理账户与
服务 UID/GID 隔离，无需加入 root/docker 组。业务 CLI 使用获批的部署 sudo 会话，不能把
Controller-only 权限说成业务写入权限。

## 用户操作顺序

1. 按[基础安装教程](../deployment/clean-device.md)从审核后的固定 GitHub Head 取得入口，
   完成 system→Controller→WeChat configure/Bootstrap→build/configure→database/migrate→start。
   基础输入只有必要服务/凭据，不填机器人 ID，不提供员工/群聊名单或 initial_identity_file。
2. 空业务状态即可执行固定 WeChat fresh QR 管理流程并扫码。脚本先 stop Gate，认证/API
   通过后恢复 Gate；未获批准消息仍由 Admission 拒绝，不能触发 Hermes 或业务投递。
3. 用正式 WeChat 账号诊断核实当前认证账号；用 `business_access discover` 查看真实观测
   消息的稳定 sender/chat 标识。诊断不输出消息正文或 Secret，不自动授权、开群或退群。
4. 取得正确来源的 Hermes、受批准的测试 Profile 和员工资料后，按[业务 CLI](../deployment/initial-identity.md)
   执行 `approve --message-id ... --input ...`。输入只含员工和 Profile，账号来自当前认证并
   与观测消息核对。群聊可以发现；本轮最小开通 CLI 只批准一个私聊测试员工与路由。
5. 发一条新标记文本，核对身份/Profile/线程/Dispatch/Response/Delivery 与一次实际效果。
   重复来源消息不得重复执行。`disable --employee-id ...` 后再发新消息，应持久拒绝；
   重跑基础安装不得恢复授权。历史重处理只能走原显式恢复契约。
6. 需要正式 B 设备验收时，从仍为空白设备运行 before-reboot 承载首次安装，再 reboot/after-reboot；
   不在已安装设备重跑只接受空白状态的 before-reboot。CI 的[VM流程](../../tests/deployment/booted-vm.md)
   另验证正式开通后的第二次真实重启与授权持久化。

## 证据边界与剩余项

- A 使用真实 Debian 包、独立 Docker、PostgreSQL、Gateway、固定 Controller 和 WeChat
  管理脚本；外部微信/Hermes为替身，普通容器的少数 systemd 观察为明确替身。
- B 启动真实 Debian 13 amd64 内核和 systemd，实际 guest reboot。基础安装无业务输入；
  第二阶段的 auth-only WeChat 服务及现有内部 API 的合成观测是明确测试输入，发生在
  第一轮空业务安装/重启成功之后。它验证正式业务服务与持久化；内部摄取 API 只归档，B 不声称它执行了 Admission。真实 Poll 拒绝链由 A 验证，B 不证明实际微信收发。
- 最终 Head 必须运行原全量、Lint/Format、逐文件 bash -n、Compose E2E 及更新后的 A/B。
  以该 Head 实际日志为准，不把旧 SHA 的成功标题改成新 Head。
- C 未执行。Hermes 正确发行来源、固定版本、包装层和受支持 AI 运行环境仍未核实，
  [Hermes 配置/LAN说明](../deployment/hermes-lan.md)保持明确缺口；完整安装、自启与真实 AI
  主机业务对接仍是待完成要求。默认诊断无模型费用，业务探测需要显式 opt-in。
- 可选外部 TLS PostgreSQL 保留原实现及“尚未单独真实组合验收”边界，本轮不扩展。
- 不连接 CFserver/既有 AI 主机，不读取生产 Token/Session/数据库/备份/业务文件；不跨仓库
  修改、不自动合并、不创建生产 Tag或发布镜像。没有管理页面、完整后台、文件/图片、
  FileBrowser、Skills、ERP或RAG功能。