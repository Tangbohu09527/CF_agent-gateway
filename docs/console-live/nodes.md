# AI 主机接入与分项检查

本页描述候选管理能力。配置、密钥、员工权限与分组仍保留候选边界；登记一个节点不等于接管其正式任务。接口检查不创建 Profile、不请求模型。管理员另行确认的“验证隔离 Profile 操作”才会创建专用空档案；这与实际员工/群 Profile 的创建结果分别记录。

## 官方接口边界

核对未修改官方 `NousResearch/hermes-agent@427d605d4985c2d49a795493ef6c5d66413057d1`：

| 能力 | 固定源码 | 配置与检查 |
| --- | --- | --- |
| OpenAI 兼容业务 API | `gateway/platforms/api_server.py` 的 `APIServerAdapter._check_auth`、`_handle_models`、路由表；`hermes_cli/config_defaults.py` 的 `API_SERVER_*` | 默认端口 8642；`API_SERVER_ENABLED`、`API_SERVER_KEY`、`API_SERVER_PORT`、`API_SERVER_HOST`、`API_SERVER_MODEL_NAME`。只读检查 `/v1/models` 的格式和有效/错误密钥。模型执行保持未验证。 |
| 官方 Profile 管理 | `hermes_cli/subcommands/dashboard.py`、`hermes_cli/web_server.py`、`hermes_cli/web_routers/status.py` | `hermes serve` 默认回环 9119；独立管理认证与业务密钥分别配置。读取 `/api/profiles`、`/api/config/schema`、`/api/health`，连接 `/api/ws` 调用 `gateway.ping`；有效与错误凭据分开检查。 |
| 远程管理认证 | `web_server.py` 的 `_has_valid_session_token`、`should_require_auth`；`dashboard.py` 的 `--insecure`说明 | 非回环监听有独立认证门，旧 `--insecure` 不关闭认证。不能把业务 API 五字段或 `API_SERVER_KEY` 当作管理连接。新主机可先保存业务连接，管理入口标待配置。复用已经验证的受控 HTTPS 管理入口；本轮不安装客户端、修改官方源码或关闭认证。 |

页面内教程按五个官方字段逐项解释。`host` 是 AI 电脑监听地址；`0.0.0.0` 不是 Gateway 连接目标。接口模型名称只影响广告名称。保存官方配置后需按该版本正常启用/加载对应服务；Web 不远程执行重启、升级或任意命令。

前提是官方 Hermes 已安装、原模型已配置可用，且 CFserver 到 AI 电脑已有获准网络路径。固定版本前台启动入口为 `hermes gateway run`；已通过官方服务管理运行的 Gateway 使用其原有方式重新加载/重启，先确认无在途任务。保存五字段不等于后台已加载。本轮不为接入向导重装官方依赖。

新连接只自动生成业务 API 密钥。管理连接必须显式提供**已有受控管理地址和已有会话凭据**，否则后端拒绝保存；已有管理凭据不回显。新业务连接和已有管理连接可以保存在同一节点。**任意新电脑的独立管理入口自动配置尚未接通**；只有五个 API Server 字段时可以先保存业务连接，页面准确显示管理待配置，不能据此创建员工 Profile。

## 同一节点的两种能力

`NodeRegistry` 维护稳定配置身份，同一节点可以同时有业务和管理连接。名称或地址相同只提示可能重复，不静默合并或覆盖密钥。原 `profile_b_nodes`、分组及 Profile 管理服务继续使用：显式登记后写入原 Node 表，管理连接复用同一 `config.hermes_connections` 字典；没有管理连接的节点不能执行 Profile 管理。

`runtime/node-registry.json` 保存候选节点、随机密钥及检查结果，原子替换，POSIX 权限 `0600`，在 Git 外的受保护 runtime。服务端列表不返回密钥。新生成密钥仅经管理员明确 `POST reveal` 返回；前端输入默认为密码遮蔽，关闭窗口清除。已有连接的密钥不回显、不自动轮换。

已配置连接从 server-only `runtime/existing-nodes.json` 定向导入，凭据仍是服务器环境引用：

```json
{
  "version": 1,
  "nodes": [
    {
      "identity": "gateway-config-stable-node-identity",
      "name": "已配置 AI 主机",
      "source": "gateway_config",
      "business": {
        "url": "http://private-node.example:8642",
        "token_env": "EXISTING_HERMES_BUSINESS_TOKEN",
        "model_name": "hermes-agent"
      },
      "management": {"connection_ref": "operator-owned-management-reference"}
    }
  ],
  "filebrowser": {
    "url": "https://private-files.example:8443",
    "token_env": "EXISTING_FILEBROWSER_TOKEN",
    "health_path": "/health"
  }
}
```

业务和管理字段分别可省略。已有管理连接也可使用 `{url, token_env}`。仅已存在的稳定配置身份被导入，不扫描 IP、不全盘寻找密钥；导入显示不自动登记、授权或派发。原 operator `initial_config` 保留，不被向导替换。

## 管理 API

统一前缀 `/admin/console/nodes`，复用现有管理员鉴权、Cookie、CSRF、同源和管理审计；所有响应 `no-store`。

| API | 实际作用 |
| --- | --- |
| `GET /nodes` | 节点、来源、分开的业务/管理状态、时间与教程；无秘密 |
| `GET /nodes/{id}` | 单节点真实详情；已登记但连接缺失的节点同样可查，测试明确返回未配置，不伪造可用 |
| `POST /nodes/drafts` | 保存待配置连接；`Idempotency-Key` 绑定请求，重复点击复用配置，变更请求拒绝误复用 |
| `POST /nodes/{id}/reveal` | 明确展示本次新生成配置；已有凭据拒绝回显 |
| `POST /nodes/{id}/test` | 管理服务器发起只读分项探测；并发检查复用单操作边界，超时可重试 |
| `POST /nodes/{id}/register` | 显式登记到原 Node 表；重复登记返回同一节点 |
| `POST /nodes/{id}/management` | 给同一台业务主机补充尚未配置的管理连接；保留业务密钥，拒绝覆盖已经存在的管理连接 |
| `POST /nodes/{id}/verify-profile` | body 必须为 `{"acknowledge_creation":true}`；管理只读检查需近期通过，然后经官方管理操作建立 `cfcheck-` 空档案并实际回读；稳定命名、未知同名拒绝覆盖，不克隆记忆、定时任务或凭据，不调用模型，不删除测试档案 |
| `GET /nodes/filebrowser-status` | 共享缓存的只读健康检查；认证、存储空间、挂载、可写性没有可证接口则显示未知/未采集；不写正式目录 |

普通探测区分网络、端口、TLS、认证、接口格式和 WS/RPC。TLS 使用系统信任与主机名验证，不关闭校验。只允许明确本地/私有网络 origin 和服务端固定路径；拒绝元数据/link-local/公网目标、userinfo、查询串、代理路径、重定向和超大响应。DNS 解析后验证全部地址并固定拨号 IP，TLS 仍使用原域名校验。HTTP 响应有大小上限和绝对截止；每项结果含来源、`observed_at`、`expires_at` 和 TTL，页面到期显示未知/过期。局域网业务 HTTP 明确显示未使用 TLS，管理跨机要求受信 HTTPS。

## 本轮隔离验证范围

`tests/management_console/test_console_nodes.py` 使用真实候选 FastAPI/SQLite 与显式标注的合成 Hermes HTTP/WS peer，覆盖生成/幂等、注册/持久化、秘密不回显、已有配置保留、认证/CSRF/同源、地址与 DNS 限制、有效/错误 REST 与 WS 密钥、部分能力可用、普通测试无创建、显式空档案创建及重复复用。合成 peer 不代表真实官方版本已在新主机接通；真实节点与现场证据由本轮最终交付另行记录。

准确性修正后定向后端 21 项通过，另通过 Ruff 与两份 JavaScript 语法检查。覆盖新业务与已有管理凭据分别保存、拒绝缺失管理凭据、管理专用连接不生成/回显密钥、无连接已登记节点的实际详情与未配置结果。运行环境为本机隔离虚拟环境；初次测试因工作树 ACL 无法建临时库而中止，使用正常审批的同范围执行后完成，未扩大服务权限。

向导的最短实测路径是添加合成主机 → 保存待配置连接 → 明确查看并复制新密钥 → 填入独立 peer/官方设置 → 从管理服务器测试 → 分项结果 → 保存统一 Node → 分组选择。新 AI 电脑仍需真人完成官方设置；本文件不宣称已完成多机接入或业务模型调用。

本轮实际 Edge 页面 24 项通过，结果与合成截图见 [evidence/README.md](evidence/README.md)。测试结束的上游统计为模型调用 0、仅管理员明确确认后创建专用空档案 1，runner 所有本轮测试进程已停止。真实新主机的业务执行、跨机管理部署和现场 Profile 创建仍分别验收。
