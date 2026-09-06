# 首次安装：显式业务身份与 V2 私聊路由

空库迁移只创建表，不会授权任何发送者，也不会自动选择 Agent Profile。首次安装入口在迁移之后调用本文模块，创建一位经过批准的测试身份和一个明确的私聊路由。保持 Poll/Delivery 组合 Gate 关闭，直到 Hermes 对接与 WeChat 认证、消息 API 检查均通过。

本文的 `ready` 仅表示数据库中的身份、授权和路由能够经现有业务解析器通过。它不是 Gateway 健康、微信扫码成功、Hermes API 兼容或真实模型调用成功的证据。

## 输入文件

从仓库的 `config/initial-identity.example.json` 复制一个部署输入文件。替换所有 `REPLACE_` 值；保留示例无法通过校验。不修改源码，不执行手工 SQL，不在 JSON 内放任何 Token 或模型供应商 Key。

| 配置字段 | 来源与含义 |
| --- | --- |
| `version` | 固定为 `1`。 |
| `profile.profile_key`、`profile.revision` | 此次批准的 Gateway Profile 键与不可变修订号；同一修订的内容不能被覆盖。 |
| `profile.provider` | 此入口只接受 `hermes`。 |
| `profile.external_profile_ref` | 经实际 Hermes 包装层契约确认的 Profile 引用；不能根据名称猜测。 |
| `profile.model` | 经实际 Hermes 服务确认的模型标识，必须等于 Gateway 有效配置的 `hermes.model`；当前客户端实际发送这个有效配置值。 |
| `identity.employee_id` | 企业侧为这一位批准用户指定的稳定业务标识，用于重跑匹配已有身份。不是 Linux 管理用户或容器 UID。 |
| `identity.display_name` | 经过批准的显示名。已有同一业务标识但显示名不同也报告冲突。 |
| `wechat.account_id` | 微信登录账户的稳定账号 ID，与 Gateway 轮询归一化后的 `source_account_id` 一致；不是 Token。 |
| `wechat.sender_id` | 唯一获批发送者的稳定微信 ID，与原始消息的 `sender` 一致；不是昵称。 |
| `wechat.conversation_id` | 获批私聊的原始 `chatId`。它通常是对方微信 ID，但应从受支持 WeChat API 核实，不能用昵称推测。以 `@chatroom` 结尾的群聊在此入口拒绝。 |

本次基线要求在 fresh QR 前已明确批准并掌握机器人 `account_id`、发送者 `sender_id` 与私聊 `chatId`，可以来自受控账户管理记录。这些是必要配置输入；正式安装顺序为迁移、初始化、启动 Gateway/Dispatch、验证 Hermes，再运行 WeChat fresh QR。无需发送一次未授权业务消息来制造数据库中的 conversation。

如果是尚无上述记录的新账号，不能假定扫码之后还会暂停等待配置。已检查的 WeChat 固定提交 `67cbbb04ce15703428ce165ac38effac19f4b701` 中，[`scripts/start-qr-login.sh` 第 807–808 行](https://github.com/Tangbohu09527/CF_agent-wechat/blob/67cbbb04ce15703428ce165ac38effac19f4b701/scripts/start-qr-login.sh#L807-L808)在认证与消息 API 检查后直接启动组合 Gate，没有已实现的 post-auth 暂停参数。该情形是当前跨仓库接口剩余项，不能以空身份运行 Gate 或写一个不存在的暂停命令绕过。最小后续配套范围是 WeChat 原 fresh QR 入口增加显式 hold-gateway 选项：认证与消息 API 检查成功后保留 WeChat 运行、保持组合 Gate 关闭，之后由既有 Gateway Controller 在正式初始化、检查及 Hermes 验证成功后打开 Gate。本任务没有修改 WeChat 仓库或宣称该选项已实现。

JSON 拒绝未知字段、空值、通配符和未替换的 `REPLACE_` 值。系统不会默认授权所有微信用户或创建未知群的默认路由。此入口不提供扩展权限、Skills 或群聊开通功能。

## 执行环境与配置一致性

正式安装器的 `initialize` 阶段调用以下模块；`start` 和 `diagnose` 阶段调用只读检查。日常操作应使用正式安装入口及原有固定版本参数，让安装器继续复用同一 Compose 项目、配置和凭据。下面展示的是安装器在 **使用 Gateway 镜像的临时 migration 容器内** 执行的命令；安装器将受保护身份文件只读绑定到 `/run/initial-identity.json`，它不是 Gateway 常驻容器内默认存在的路径：

```bash
python -m cf_agent_gateway.initial_identity \
  --input /run/initial-identity.json

python -m cf_agent_gateway.initial_identity \
  --input /run/initial-identity.json --check
```

该模块遵循现有配置加载器：`CF_GATEWAY_CONFIG` 指定 YAML 路径，`CF_AGENT_GATEWAY_DATABASE_URL` 可覆盖数据库 URL。不要另外创建另一套迁移、初始化或运行配置。必须已经完成当前版本的数据库迁移，并启用有效配置中的 `runtime.v2_routing_enabled: true`。它不代替迁移，不要求旧 Release 或备份。

首次成功返回 `status: ready` 与本次创建的阶段名。相同输入再次运行不会更新已有行，`created_stages` 为空。`--check` 不创建任何缺失配置，也不会向 Hermes 或微信发请求、消耗模型费用。

## 初始化行为与保护

模块复用 `IdentityService`、`AccessPolicyService`、`AgentProfileStore` 以及正式的 `MessageStore.prepare_conversation`，依次准备 Profile、业务身份、来源映射、用户策略、网关策略、空的私聊记录和 Profile 绑定。空的私聊记录不伪造消息，不改变已有会话名称或消息。工作区和 AI Thread 继续由真实消息准入路径按需创建。

用户策略仅对指定业务身份启用；权限范围与允许的 Skills 都为空。全局网关策略只允许 `normal` 风险，不增加权限范围或 Skills。现有 evaluator 仍要求身份已解析且用户被显式允许；其他发送者不因此获得权限。只有本文配置的私聊得到 Profile 绑定，其他会话的路由继续遵循既有 V2 规则。

所有阶段位于同一个数据库事务内；现有业务 Store 内的 `commit()` 不会提前提交外层事务。最后还会运行与消息准入相同的身份解析、授权 evaluator 和 `RouteResolver`。任一步失败都回滚本次新写入。使用 SERIALIZABLE 隔离保护检查与写入之间的并发变更；出现并发事务冲突时，应结束本次命令后完整重试。不要在其他管理操作正在修改相同策略或路由时同时运行首次初始化。

以下情况报告具体阶段并保持原状态：已有映射属于另一身份、Profile 修订内容不同、身份或映射被禁用、已有授权策略不同、会话类型不同、Profile 绑定不同。初始化器不会重新启用被停用的身份，不覆盖已有策略，不清空数据库，也不会将新修订自动绑定到原会话。

## 失败、重试与验收

错误 JSON 只有稳定错误码和阶段，不输出输入文件内容、数据库 URL 或异常堆栈。阶段包括 `input`、`configuration`、`database`、`profile`、`identity`、`source_mapping`、`user_policy`、`gateway_policy`、`conversation`、`profile_binding` 和 `resolution`。

- `initial_identity_failed`：检查所报阶段的文件可读性、输入结构、有效配置或数据库服务与迁移状态；凭据必须通过安全配置文件修正，不打印到日志。
- `v2_routing_required`、`hermes_model_conflict`：检查相应实际配置键，不绕过配置校验。
- `initial_identity_missing`：`--check` 发现缺少某阶段。确认输入确为获批内容后，执行不带 `--check` 的正式初始化。
- `initial_identity_conflict`：保留已有配置，核对业务授权和 Profile 版本；这个首次安装工具不是策略变更或升级入口。
- `initial_identity_verification_failed`：保持 Gate 关闭，检查该版本的身份与路由解析行为；不得忽略错误继续轮询。

每次重试都使用原输入。中断时事务会回滚；如果数据库已有一部分由早先正式管理流程准备、并且与输入完全一致，重跑会只补齐缺失阶段。

自动回归 `tests/test_initial_identity.py` 覆盖空库后的真实 V2 准入与持久化 dispatch、未授权用户拒绝、同一唯一标记消息的去重、重复执行保护、禁用状态保留、映射归属冲突、晚期失败的完整回滚和安全重试。该回归使用本地 SQLite，不是 PostgreSQL 容器验收、微信扫码或真实模型调用证据；PostgreSQL 和组合安装测试由正式部署验收入口执行。

真实设备验收时，在正式安装顺序完成后，用 `--check` 核实初始化，验证 Hermes 的网络、认证、协议与显式 opt-in 应用请求，完成 WeChat fresh QR 和消息 API 检查，然后打开组合 Gate。由获批发送者发一条唯一标记文本，核对同一 message、identity、AI Thread、dispatch、response 和 delivery 的关联结果；重复交付相同来源消息不得新建 dispatch 或重复发送。另用未批准的测试发送者确认拒绝。真实设备、微信与 Windows AI 主机验收需要另行批准，本文件不表示已经操作生产。
