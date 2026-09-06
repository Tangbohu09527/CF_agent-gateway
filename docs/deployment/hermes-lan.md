# Hermes 局域网对接与证据边界

本页提供 Gateway 已实现的配置与容器内诊断入口。**本项目依赖的 Hermes 来源、固定版本、包装层和 Windows 安装方式尚未核实，因此完整 AI 主机安装/自启教程以及真实 LAN 业务验收仍未完成。** 本轮没有连接或读取现有 AI 主机；以下命令供后续批准的隔离设备验收使用，不能当作已执行记录。

## 1. 来源核查结果与待补输入

核查基线为 Gateway `4f13039b86c60bc94340edb5468f0102d62d2dff`、总文档 `8f51cd095c6967f806701b703281b08e5144296f` 和 WeChat `67cbbb04ce15703428ce165ac38effac19f4b701`。已搜索上述仓库文档及获准 Gateway/总文档 Git 历史，没有找到将本项目 Hermes 与具体上游 URL、安装制品、固定提交对应起来的证据。

- [总文档职责与 Hermes 边界](https://github.com/Tangbohu09527/CF_ecommerce-automation-docs/blob/8f51cd095c6967f806701b703281b08e5144296f/02_%E7%B3%BB%E7%BB%9F%E8%AE%BE%E8%AE%A1.md)只确认 Windows AI 主机承载外部 Hermes；当前文档明确不把历史版本写成当前版本。
- [2026-08-13 历史记录](https://github.com/Tangbohu09527/CF_ecommerce-automation-docs/blob/8f51cd095c6967f806701b703281b08e5144296f/status/2026-08-13-wechat-runtime-closeout.md)出现名称 `Hermes Gateway 0.20.0`，这不是可下载制品或 Git SHA 的来源证明。
- Gateway 非 main 历史分支的 [watchdog README](https://github.com/Tangbohu09527/CF_agent-gateway/blob/c1d294a32aef7678a2f13b165bcf0900c926f61d/ops/windows/hermes-gateway-watchdog/README.md)描述原生 `hermes.exe`、Desktop 和登录启动任务；其前提是 Hermes 已安装，且明确真实主机验证仍未完成。本 PR 不移入或运行该历史 watchdog。

继续完成安装教程需要部署负责人提供不含 Secret 的以下资料：

| 必填证据 | 用途 |
| --- | --- |
| 正确项目/发行者 URL，固定提交或 Release、安装制品摘要 | 确定真正依赖，形成可重复安装步骤 |
| 包装层仓库及固定 SHA；如无包装层，明确说明 | 确认 Gateway 额外 Profile、metadata、幂等语义由谁实现 |
| 支持的 Windows 原生/WSL/其他环境、版本要求和原始安装说明 | 决定实际安装、自启、进程身份及网络路径 |
| provider/model 的配置文档、批准的模型标识与配置档案引用/版本 | `hermes.model` 不能证明实际使用了某模型 |
| 认证、监听、端口、非业务诊断、会话和幂等的固定版本契约 | 确认全部调用字段及重启后的语义 |
| 固定版本启动/停止命令、服务或任务定义与重启验收方式 | 区分登录自启、主机启动、进程失败恢复 |

公开候选的名称和 HTTP 路径相似不能弥补这条来源链。为审查接口风险，另只读检查了 [NousResearch 候选 API 实现](https://github.com/NousResearch/hermes-agent/blob/9a84bee265daad14340a80d7585928cd8ea1f9eb/gateway/platforms/api_server_openai_routes.py#L409)；它不是本项目已选用或组合验收的 Hermes 版本。该路由未读取 `profile_reference`、`profile_revision`、`thread_id`、`session_metadata`；其 chat 幂等 fingerprint 也不包含这些字段。候选公开文档的多 Profile 方案依赖 URL 前缀和各 Profile 凭据，不能据此宣称本项目的 JSON Profile 选择已兼容。需正确包装层提供映射/拒绝行为的实现与测试证据；本 PR 不跨仓库补写包装层。

## 2. Gateway 的真实配置和请求契约

配置加载见 [config.py](../../src/cf_agent_gateway/config.py)，正式安装生成的 YAML 使用以下已有键：

| 键 | 输入与含义 |
| --- | --- |
| `hermes.enabled` | 仅在已确认外部服务时启用；来源未确定时保持关闭 |
| `hermes.base_url` | 批准 AI 主机的 HTTP(S) origin 和必要路径前缀；客户端自行追加 `v1/chat/completions`，不要再加 `/v1` |
| `hermes.api_key_env` | 环境变量名称，默认 `HERMES_API_KEY`；YAML 禁止 `hermes.api_key` 明文 |
| `hermes.model` | 外部契约规定的模型/服务路由名称，默认 `hermes-agent`；不是 provider 支持承诺 |

Gateway 安装配置必须输入 AI 主机地址；Windows 防火墙必须另输入获准 Gateway Debian 主机的 LAN 地址。不要把历史现场 IP 写入配置模板。同机 WeChat 使用 `http://cf-agent-wechat:6174`；AI 主机是独立设备，不能使用 Gateway 容器内的 `127.0.0.1`。

模型供应商 Key 仅配置在正确 Hermes 的 provider 配置内；Hermes API Key 由 AI 主机认证服务与 Gateway 共享。Gateway 普通 API Token、Admin Token、WeChat Token 和 PostgreSQL 密码分别独立生成/管理，不得互相替用。Secret 通过正式安装器的隐藏输入/受限配置传入，不加在命令行，不执行 `env` 或输出 Compose 完整渲染结果。

[生产客户端](../../src/cf_agent_gateway/hermes/client.py)发送：

- `POST <base_url>/v1/chat/completions`，`Authorization: Bearer ...`，JSON `model` 和单个 `messages=[{role: user, content: ...}]`；没有 `stream: true`、`provider` 或任意未实现变量。
- `X-Hermes-Session-Id`：从 Gateway 持久线程绑定取得；响应也必须提供非空会话头，最多 255 字符。首次绑定、续接与返回 ID 的实际语义需上游证据。
- `Idempotency-Key`：持久 Dispatch 的稳定键，1–255 可见 ASCII 字符；Gateway 去重不能证明外部执行去重或跨重启的缓存有效期。
- V2 一起发送 `profile_reference`、正整数 `profile_revision`、`thread_id`、`session_metadata`。metadata 的 message/source/account/conversation/identity/thread/policy/context 字段见 [service.py](../../src/cf_agent_gateway/hermes/service.py)；不能只接受字段却忽略 Profile 和授权边界。

响应必须为 JSON，并符合 [models.py](../../src/cf_agent_gateway/hermes/models.py) 的 legacy `choices[0].message={role: assistant, content: string}`，或 V2 `{response_id, parts:[{type:text,text:...}]}`。本轮只验文本。客户端同时拒绝明确 `X-Hermes-Completed: false`、`X-Hermes-Partial: true`、`X-Hermes-Error` 以及 `hermes` metadata 的明确失败/部分结果，避免把 HTTP 200 的失败当作已完成 Dispatch。没有这些扩展字段的旧有效响应保持兼容。这一防御行为不证明候选上游已成为正式依赖。

## 3. Windows 地址、防火墙与运行环境

先从正确 Hermes 固定版本文档确定真实监听配置键及端口。监听仅绑定批准 LAN 地址；若实际环境要求通配监听，必须先配置精确来源过滤和网络边界。不得开放公网、路由器端口映射、公网隧道，也不把全部局域网作为批准来源。HTTP 的 Bearer 会在链路上明文传输；跨不可信网段使用经验证的 TLS 或加密网络，不能通过禁用证书校验解决连接失败。

以下只适用于**确认使用 Windows 原生进程后**的管理员 PowerShell，尚未执行。使用交互输入，不保存真实地址到仓库：

```powershell
# Windows / 管理员 PowerShell；输入批准的单个 IPv4 地址与实际监听端口
$gatewayLan = [System.Net.IPAddress]::Parse((Read-Host 'Gateway Debian LAN IPv4'))
$aiLan = [System.Net.IPAddress]::Parse((Read-Host 'AI host LAN IPv4'))
$hermesPort = [int] (Read-Host 'Verified Hermes TCP port')
if ($gatewayLan.AddressFamily -ne 'InterNetwork' -or $aiLan.AddressFamily -ne 'InterNetwork' -or
    $hermesPort -lt 1 -or $hermesPort -gt 65535) { throw 'Invalid approved endpoint' }
Get-NetConnectionProfile
Get-NetFirewallProfile -Name Private | Select-Object Enabled,DefaultInboundAction
if (Get-NetFirewallRule -Name 'CF-Gateway-Hermes-LAN' -ErrorAction SilentlyContinue) {
    throw 'Existing rule: inspect its ownership and filters before changing it'
}
New-NetFirewallRule -Name 'CF-Gateway-Hermes-LAN' -DisplayName 'CF Gateway to Hermes LAN' `
    -Direction Inbound -Action Allow -Enabled True -Profile Private -Protocol TCP `
    -LocalAddress $aiLan.IPAddressToString -LocalPort $hermesPort `
    -RemoteAddress $gatewayLan.IPAddressToString -EdgeTraversalPolicy Block
Get-NetFirewallRule -Name 'CF-Gateway-Hermes-LAN' | Get-NetFirewallAddressFilter
Get-NetTCPConnection -State Listen -LocalPort $hermesPort |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

先确认 LAN 网卡确实处于批准 Private 配置且防火墙启用、默认入站拒绝。窄范围 Allow 规则不会撤销其他已有宽泛 Allow 规则；人工核对有效规则及企业策略，不自动删改其他应用规则。单条规则只批准输入的 Gateway 来源。Docker 默认 bridge 出站通常经 Debian 主机 NAT，验收时应确认 AI 主机实际看到的来源仍是批准地址；不要为解决路由问题扩大为整个子网。[Microsoft 防火墙参数说明](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule?view=windowsserver2025-ps)说明这些地址/端口/配置约束。

若最终证据确定为 WSL，则记录发行版、WSL 版本、NAT/镜像网络模式和实际 Linux 监听地址，分别检查 Windows、Hyper-V 与 Linux 防火墙以及主机重启后的地址变化。上述原生规则不能单独证明 WSL 可达；按 [Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)为已确认模式制定精确端口与来源规则，不照抄全局 `DefaultInboundAction Allow`。本页不未经来源确认就切换 WSL 网络模式或新增端口代理。

## 4. 从真实 Gateway 容器逐层验证

先完成正式安装入口的数据库迁移和 Gateway/Dispatch 启动，Poll/Delivery 保持关闭。以下 Debian 命令由具有一次性部署权限的安装管理员执行；日常管理账户仍无需 root/docker 组。实际诊断进程在已有 Gateway 容器内，以 Compose 服务的 UID/GID 运行，复用其 `CF_GATEWAY_CONFIG`、网络和环境密钥。

```bash
# Debian / 安装管理员；默认仅 TCP，不发 HTTP、不调用模型、不读取业务数据库
sudo docker compose --project-directory /opt/cf-agent-gateway \
  --env-file /opt/cf-agent-gateway/.env \
  --file /opt/cf-agent-gateway/docker-compose.prod.yml \
  exec -T gateway python -m cf_agent_gateway.hermes.diagnose
```

出口 JSON 的 `network=tcp_connected` 仅证明端口可达。`ok=true` 表示**本次显式选择的检查**通过；未执行层仍是 `not_checked`，`production_acceptance` 始终为 false。默认不假设任何 `/health` 存在，TLS/HTTP 层也尚未检查。

仅当正确版本已确认支持受认证的 `GET /v1/models` 后，显式运行只读认证检查。它先用随机错误 Key 要求 401/403，再用配置 Key 要求有效 models JSON；不会发送模型请求，也不会输出模型列表或远端响应正文。

```bash
# Debian / 安装管理员；显式 HTTP 认证检查，无业务请求
sudo docker compose --project-directory /opt/cf-agent-gateway \
  --env-file /opt/cf-agent-gateway/.env \
  --file /opt/cf-agent-gateway/docker-compose.prod.yml \
  exec -T gateway python -m cf_agent_gateway.hermes.diagnose --check-auth
```

若正确包装层没有这个只读契约，本检查会报告不支持，不能改写为成功；需提供包装层的真实非业务诊断接口证据后补充适配。认证读取接口通过不证明 POST 认证或 Profile 语义。下面显式业务探测也会先验证错误 Key 的 POST 被 401/403 拒绝，再使用正确 Key。

下面业务探测需要单独批准，可能发生模型费用和外部执行。先配置一个无业务权限/工具的真实测试 Profile；诊断中的“不要使用工具”文本不是权限隔离。Profile 引用/版本必须来自正式初始化配置与正确 Hermes 包装层，不能填任意字符串假装通过。

```bash
# Debian / 安装管理员；显式付费 opt-in，仅在批准后运行
read -r -p 'Approved test profile reference: ' probe_profile
read -r -p 'Approved test profile revision: ' probe_revision
sudo docker compose --project-directory /opt/cf-agent-gateway \
  --env-file /opt/cf-agent-gateway/.env \
  --file /opt/cf-agent-gateway/docker-compose.prod.yml \
  exec -T gateway python -m cf_agent_gateway.hermes.diagnose \
  --allow-model-call --timeout 60 \
  --profile-reference "$probe_profile" --profile-revision "$probe_revision"
```

探测先认证，只在付费 opt-in 内向业务 POST 发送随机错误 Key（认证损坏时也可能执行/收费）；确认 401/403 后，再用正确 Key 的生产 HermesClient 发送 V2 字段、独立诊断会话和唯一幂等键，要求合法响应、会话头及准确回显唯一标记。仅输出阶段、稳定错误码和非业务 probe ID。合成 metadata 不引用真实业务身份、会话或数据库；因此本探测是容器到应用的协议与响应检查，**不能取代正式身份/路由/微信文本链**。

需要重放检查时，在上一业务命令中额外加 `--check-replay`：同一次运行重复完全相同的请求和幂等键，可能再收费。相同响应只报告 `response_consistent_execution_count_unverified`。必须从获准隔离 Hermes 执行记录证明实际运行次数为 1、Profile 选择正确，再分别核对幂等有效期、相同键不同请求拒绝和服务重启后的行为。不要对生产不确定 Dispatch 重发探测。

## 5. 失败诊断、重启和 C 验收记录

| 检查结果 | 下一步 |
| --- | --- |
| `hermes_disabled_or_unconfigured` | 确认来源后通过正式配置启用；不要填虚假 ready |
| `connect_failed` / `connect_timeout` | 在 AI 主机确认进程与实际 listener；核对两端地址、路由和精确源规则；不要打开公网 |
| `hermes_api_key_or_client_invalid` | 确认受限环境文件有独立 Hermes API Key，禁止打印它 |
| `configured_key_rejected` | 检查正确实例/端口/Profile 与密钥配对，不替换为 Gateway Admin Token |
| `wrong_key_not_rejected` / `wrong_post_key_not_rejected` | 服务认证或端点契约不符，停止应用验收；不能以200放行 |
| `models_contract_unavailable` / `models_response_invalid` | 核对正确固定版本和路径；代理登录页、health JSON不是协议通过 |
| `http_transport_failed` / `http_timeout` | 核对 TLS 信任/服务进程/代理与超时，不关闭证书验证 |
| `hermes_response_error` / `probe_marker_mismatch` | 会话头、响应格式、明确部分失败或实际应用结果不符；核对包装层和 provider |
| 模型调用超时 | 外部结果可能不明；保留隔离证据，不自动重试，不用重复模型调用修复 Delivery |

正确 Hermes 固定版本资料齐备后，完整安装教程还须补充以下真实步骤并逐项留证：安装制品/依赖锁定 → 独立进程账户及配置权限 → provider 设置 → 受控监听和防火墙 → 前台启动 → 真实容器认证/协议/应用检查 → 固定服务或任务安装 → 进程重启 → Windows/实际环境整机重启（分别核对未登录与登录后状态）。Windows 登录任务不能写成无需登录的开机服务，WSL 内 systemd 正常也不能证明 Windows 重启会启动该发行版。

C 层审批后的执行顺序：

1. 填完第 1 节来源清单并核对固定 Gateway/WeChat/Hermes/包装层 SHA 和镜像记录；在新隔离主机按正式安装入口执行。
2. 在批准 AI 测试主机配置正确服务与精确网络限制；从真实 Gateway 容器依次执行本页网络、只读认证、获准模型探测。
3. 完成服务进程和 AI 主机重启，重复上述检查；此时若 Debian 和 WeChat 未动，不强制 fresh QR。
4. 按 WeChat 正式脚本 fresh QR，认证及消息 API 均通过，使用正式业务接口配置最小测试身份、授权和 V2 Profile/路由，再由 Controller 开组合 Poll/Delivery Gate。
5. 发一条唯一标记的真实微信文本，核对 Message → Admission Allowed → Thread/Profile → Dispatch → Response → Delivery 与微信回执，重放来源事件不产生第二次模型执行或微信效果。
6. 用获准测试请求核对错误 POST 认证、服务停止、不可达、超时与恢复；保留关联ID、状态、数量及脱敏错误码，排除业务正文、Secret、QR和Session文件。

本 PR 单元测试和任何外部替身链路均属于 A 层。B 需要启动 systemd 的干净 Debian 主机及整机重启，C 需要真实微信、真实 AI 主机和正确 Hermes；本页不把上述待执行步骤写成通过记录。
