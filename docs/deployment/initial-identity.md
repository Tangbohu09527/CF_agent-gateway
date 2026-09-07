# 安装后的业务开通、停用与查看

基础安装和员工业务开通分开。空库没有员工授权和有效路由时，核心服务和组合 Gate 可以运行；消息会被保存并由 Admission 拒绝，不调用 Hermes，也不产生业务投递。安装输入不需要机器人 ID、员工名单或会话绑定。

本文使用 `cf_agent_gateway.business_access` 最小 CLI。它复用 Identity、AccessPolicy、AgentProfile、Conversation、RouteResolver、MessageStore 和 AdminArchiveStore，不执行手工 SQL、不新增数据模型或管理前端。`discover`、`status`、`disable` 不发网络请求；`approve` 仅额外读取 WeChat 认证状态，不发模型或聊天请求。

## 1. 登录并查看真实身份与会话

先按 [首次安装入口](clean-device.md) 完成基础安装、WeChat fresh QR 和认证/API 检查。机器人账号由受支持的 `GET /api/status/auth` 响应 `loggedInUser` 自动取得，只有 `status=logged_in` 且账号字段有效时才采用。安装者不能填写、猜测或复制旧机器人的 ID。客户端实现见 [client.py](../../src/cf_agent_gateway/adapters/wechat/client.py) 和 [raw_models.py](../../src/cf_agent_gateway/adapters/wechat/raw_models.py)。

让准备批准的员工发送一条新的测试私聊文本。此时未授权消息应被拒绝；发现消息不等于授权。部署管理员从正式部署的有效配置中查看它：

```bash
# Debian / 获批部署 sudo 会话；不加入 root/docker 组
compose=(sudo docker compose --env-file /opt/cf-agent-gateway/.env \
  --file /opt/cf-agent-gateway/docker-compose.prod.yml --profile worker)
"${compose[@]}" run --rm --no-deps -T migration \
  python -m cf_agent_gateway.business_access discover --limit 50 --offset 0
"${compose[@]}" run --rm --no-deps -T migration \
  python -m cf_agent_gateway.business_access status
```

这里的系统部署 sudo 权限与 Controller 的受限 sudo 权限分开。只有 Controller 操作权限的用户不能据此执行任意 Docker 命令；本文不扩大 Controller sudo 规则。Gateway 根目录继续为 `root:root 0750`，容器 CLI 继续使用服务身份。

`discover.items` 只输出管理所需元数据，包括 `message_id`、`source_account_id`、`sender_id`、`conversation_id`、`conversation_type`、`approval_eligible`、`permitted`、`reason_code` 及已有配置 ID。它不输出消息正文、发送者昵称、原始载荷或 Secret。可使用 `--limit 1..100` 和非负 `--offset` 分页；`total` 是观测消息总数。群聊可以被发现，但此最小开通入口只批准私聊；发现群聊不意味着启用群路由。

保存要批准的那条真实 `message_id`。`sender_id` 和 `conversation_id` 分别来自消息的稳定发送者标识与 `chatId`，不能用昵称替代或任意修改。

## 2. 显式批准一位员工和私聊路由

创建业务批准 JSON，内容只有以下字段。所有示例值应由业务批准记录和已核实的实际 Hermes 契约替换；不要放 Token、模型供应商 Key、机器人 ID、sender 或 chatId：

```json
{
  "version": 1,
  "profile": {
    "profile_key": "approved-test-profile",
    "revision": 1,
    "provider": "hermes",
    "external_profile_ref": "profiles/approved-test/1",
    "model": "hermes-agent"
  },
  "identity": {
    "employee_id": "approved-test-employee",
    "display_name": "Approved test employee"
  }
}
```

`employee_id` 是企业为获批员工指定的稳定标识，与 Linux 管理用户、容器 UID 无关。Profile 的 `external_profile_ref` 必须来自实际 Hermes 包装层契约；`model` 必须等于 Gateway 有效配置的 `hermes.model`。当前仓库缺少真实 Hermes 来源证明，因此这两个示例字符串不构成模型兼容承诺，见 [Hermes LAN](hermes-lan.md)。

批准文件大小限制为 1–65536 字节，必须为单硬链接普通文件，拒绝符号链接和 FIFO。Debian 上属主必须为 root 或当前 CLI 服务 UID，模式仅接受 `0400`、`0600`、`0440`、`0640`。下面将经过人工核对的输入安装为 `root:10001 0640`，只读挂载；路径和消息 ID 是本次操作输入：

```bash
# Debian / 获批部署 sudo 会话
APPROVAL_SOURCE=/absolute/path/to/reviewed-approval.json
APPROVAL=/var/lib/cf-agent-gateway-install/approved-business.json
MESSAGE_ID=123  # 替换为 discover 实际返回并经人工核对的 message_id
sudo install --owner=root --group=10001 --mode=0640 \
  "$APPROVAL_SOURCE" "$APPROVAL"
"${compose[@]}" run --rm --no-deps -T \
  --volume "$APPROVAL:/run/approval.json:ro" worker \
  python -m cf_agent_gateway.business_access approve \
  --message-id "$MESSAGE_ID" --input /run/approval.json
```

批准使用一次性 **worker 配置容器**，因为它已挂载受保护的 WeChat Token；命令被显式替换为 CLI，不会启动第二个轮询进程或更改组合 Gate。Gateway/migration 服务本身没有该 Token 挂载，不能用它们运行需要认证的 `approve`。

CLI 从选定消息推导机器人、发送者和会话 ID，并再次读取认证 API，要求当前登录机器人与消息的 `source_account_id` 完全一致。未登录、账号字段无效、服务不可达、认证错误或账号已经切换时失败，不创建业务资产。未知输入键、占位符、通配符和不支持的版本也会失败。

批准在一个事务内创建或验证：明确的 Profile 修订、企业员工、来源身份映射、启用的员工策略、仅允许 normal 风险的 Gateway 策略、既有私聊会话及其 Profile 绑定。它不授予额外 scope/skill。完全相同的重跑复用现有对象；身份、绑定、策略或 Profile 冲突时回滚并报告具体阶段，不替换旧配置。

## 3. 只验证新消息

成功结果包含 `permitted=true`、实际 `enterprise_identity_id`、`conversation_record_id`、`agent_profile_id` 和 `replayed_messages=0`。这里的 `permitted` 是当前普通文本配置解析结果，**不是选中旧消息已执行，也不是模型调用成功**。旧消息的 `historical_admission` 继续保留原拒绝结果；重新收到同一消息也不会自动执行。

让员工发送一条新的唯一标记文本，再查新消息：

```bash
# Debian / 获批部署 sudo 会话
"${compose[@]}" run --rm --no-deps -T migration \
  python -m cf_agent_gateway.business_access discover --limit 10
NEW_MESSAGE_ID=124  # 替换为新的真实消息 ID
"${compose[@]}" run --rm --no-deps -T migration \
  python -m cf_agent_gateway.business_access status --message-id "$NEW_MESSAGE_ID"
```

`ai_thread_id`、`workspace_id` 只报告已经存在的调度关联，不会为了诊断创建线程。`historical_admission.admitted` 表示历史 Admission 结果。真实 Hermes 响应、投递结果和唯一标记去重仍需按 [分层验收](clean-device.md) 检查，不可由 HTTP 200、`permitted` 或配置计数代替。

## 4. 停用与重跑保护

```bash
# Debian / 获批部署 sudo 会话
"${compose[@]}" run --rm --no-deps -T migration \
  python -m cf_agent_gateway.business_access disable --employee-id approved-test-employee
"${compose[@]}" run --rm --no-deps -T migration \
  python -m cf_agent_gateway.business_access status --message-id "$MESSAGE_ID"
```

停用只把这一员工的现有用户策略 `enabled` 设为 false，保留策略 ID、scope、skill、有效期、来源映射、Profile、会话绑定、历史消息及结果。停用是幂等操作；它不会退群，不取消已完成业务，也不删除队列中此前已获准的任务。新消息必须重新经过 Admission 并被拒绝。基础安装重跑不会恢复此授权；重复 `approve` 也会以 `initial_identity_conflict/user_policy` 失败，不静默恢复停用用户。

无 `--message-id` 的 `status` 是数据库配置概览，输出 `business_state=awaiting_configuration|configured`、`counts` 和 `end_to_end_ready=false`。只有同一来源账号下员工授权与私聊路由均经现有解析器通过，才计入 `configured_identity_route_pairs` 和 `configured_private_routes`；仅有 identity 或 profile 不算配置完成。状态不联系微信或 Hermes，所以不能证明当前扫码账号、网络、模型或端到端可用性。此最小概览专门统计私聊配置，不将群聊发现计为开通。

## 可选的旧显式初始化输入

保留 `config/initial-identity.example.json` 与 `python -m cf_agent_gateway.initial_identity --input FILE [--check]` 的现有 schema，供已有明确业务配置的部署兼容。基础安装可以不提供 `initial_identity_file`；显式提供时仍严格校验文件、内容、Profile 和绑定，错误不会退化成“没有配置”。写入模式同样先通过共享认证客户端读取当前微信账号，要求认证成功且 `account_id` 与文件完全一致；未登录、畸形响应、错误凭据或账号不匹配均在写入业务资产前失败。此写入命令使用上述带 Token 挂载的一次性 worker 配置容器。`--check` 保持纯数据库校验，可通过 migration 配置容器执行，不依赖运行中的 WeChat 或 Token。它不是新机器人账号采集入口；新设备按上述真实登录与观测流程开通。基础安装、启动或重跑不自动调用业务开通、不覆盖业务停用状态或 Secret。

所有命令的失败只输出稳定 `error_code` 和 `stage`，不会打印输入内容或底层异常中的连接凭据。发生冲突应核实业务记录后处理，不删除数据库、改 source ID 或用 SQL 绕过业务服务。此入口不提供群聊开通、完整 CRUD、重新授权、文件/图片或 Skills 功能。
