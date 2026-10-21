# CF 微信管理台候选

2026-10-21 已部署 CFserver 独立候选并完成真实来源、LAN 页面和候选重建核验；准确运行版本、数据范围及停止方法见[本轮交付](wechat-console/delivery-2026-10-21.md)。当前微信进程离线，已同步资料明确标为缓存；候选管理不影响正式权限或派发。

在 PR25 `510d904a2022275b13030fa2777130356d80dea9` 上接续开发，复用员工身份、两级权限、分组、模板、节点、派生 Profile 和审计。首页改为聊天、通讯录和已发现群；从联系人登记员工、从群配置业务分组，来源身份由后端绑定。企业姓名不改微信备注。

候选数据与正式运行分离。`demo` 使用合成 HTTP 来源和真实候选数据库；`read_only` 使用操作员登记的微信接口和只读档案连接，不静默回退演示。两种模式拒绝共用 runtime。读取和授权页面均不触发模型、微信发送、清未读、登录或切换账号；未启动 Poll、Dispatch、Delivery。候选权限、分组及 Profile 元数据仅在候选数据库生效。

## 已接通的页面与操作

| 页面 | 数据与操作 |
| --- | --- |
| 聊天 | 近期会话加已归档会话；档案消息后端分页、北京时间日期区间、关键词及具名发言者筛选；引用、附件元数据、图片未加载状态、投递失败及未确认状态 |
| 通讯录 | 独立联系人接口；备注优先并显示昵称、头像或占位、alias、username、Bot 账号；搜索、分页、关联与授权筛选；登记或关联员工、两级权限、启停、分组及批量设置 |
| 已发现群 | 近期会话和档案发现的群；稳定标识、最近活动、分组、模板、节点与 Profile 状态；从详情配置，无需手填群 ID |
| 员工与权限 | 统一 PR25 员工模型；已授权未分组仍阻止执行，不自动授权文件能力 |
| 分组与模板 | 员工组与群组分开；SOUL、已验证配置、版本与同步状态 |
| AI 节点与 Profile | 连接引用、能力检查、实际创建和同步结果；没有已登记且验证成功的 Hermes 连接时不能假报成功 |
| 系统状态 | 来源模式、同步范围、候选生效边界、默认折叠的诊断 |

## 上游字段与范围

参考固定上游 `thisnick/agent-wechat@96c2e90c497d999bf5189a134a9b3b293731f42e` 的 contacts/chats router、tools 和类型序列化。2026-10-21 在 CFserver 内使用现有凭据进行了获准的只读核对，个人资料和凭据未回传；只记录契约字段及脱敏结果。

| 来源 | 实际契约 | Gateway / 页面 |
| --- | --- | --- |
| `GET /api/status/auth` | `status`, `loggedInUser` | 稳定 Bot 身份与来源状态，不猜 wxid |
| `GET /api/contacts?limit=&offset=` | `username`, `nickName`, `remark`, `alias`, `smallHeadUrl`, `contactType` | 规范化为内部字段，账号加 username 唯一；昵称、备注、alias 不作为身份主键 |
| `GET /api/chats?limit=&offset=` | `id`, `username`, `name`, `isGroup`, `unreadCount`, `lastMessagePreview`, `lastActivityAt` 等 | 近期非隐藏会话；微信未读注明快照来源，网页阅读不清零 |
| Gateway 持久化档案 | conversations、messages、attachments、hermes_responses/parts、delivery 记录 | 外部数据库强制只读，不初始化或迁移；按来源、账号及物理会话过滤 |
| 群成员 / 完整群来源 | 当前未取得正式契约 | 显示“已发现群”“成员资料未提供”，不以发言人数充当成员数 |

联系人分页按 offset 前进，短页继续，至空页停止并保存页证据。原生 SQL 分页后才过滤系统账号，且空数组也可能表示资料库未就绪，所以不能仅凭这次扫描声称微信全部好友完整。公众号、企微与个人联系人分开。同步不删除失踪资料，不覆盖员工权限和分组。

`logged_out`、身份未知及接口错误拒绝新同步并保留既有资料。上游明确返回 `app_not_running` 且仍提供稳定已存账号时，允许读取已有资料库，显示“来源离线 · 已读取缓存”，不宣称在线。同步前后账号不一致则拒绝整次更新。

实际核对确认现有档案可用，PostgreSQL `transaction_read_only=on`；未升级正式 schema。当前没有完整微信历史补采：self 跳过、停采、窗口限制和未归档消息继续构成缺口。不同空态分别表示尚未同步、未配置档案、尚无档案、日期无记录、筛选无结果及连接失败。打开聊天不下载原件；预览和下载当前不支持时不给假按钮。

## 本机已准备的入口

在独立工作树安装好的 `.venv` 中运行，不修改系统环境：

```powershell
Set-Location 'D:\Tangbohu09527\.cf-worktrees\wechat-management-console\CF_agent-gateway'
.\scripts\wechat-console\start.ps1
```

首次通过遮蔽输入设置管理员用户名及至少 12 字符密码。打开 `http://127.0.0.1:18770/console`，输入自己设置的用户名和密码。默认是单独的合成演示 runtime；页面始终标注演示。脚本不显示长期 Token，前端只持有 HttpOnly session cookie 和 CSRF 值。正常管理员初始化也可直接运行：

```powershell
.\.venv\Scripts\python.exe -m cf_agent_gateway.management_console.runtime init-admin --runtime '..\runtime\console'
```

停止仅本轮记录的进程：

```powershell
.\scripts\wechat-console\stop.ps1
```

自定义 runtime 启停均传同一个 `-Runtime`。已在 Windows PowerShell 5.1 实测启动、cookie 登录、访问和停止，也在开发 PowerShell 验证；中文脚本使用 UTF-8 BOM。保留了 PR25 工作树人工 BOM 修正，未处理原备份文件。

## CFserver 独立候选部署

使用 [Dockerfile](../deploy/wechat-console/Dockerfile)、[Compose](../deploy/wechat-console/compose.yaml) 和 [Nginx 配置](../deploy/wechat-console/nginx.conf)。本轮服务器实际状态、最终地址与精确提交见交付记录；此说明本身不表示已部署成功。

将准确候选提交放在独立目录，例如 `/home/linxi/cf-wechat-console-candidate-20261021/releases/<full-sha>`。`runtime`、server-only `secrets.env`、Compose 操作员配置放源码目录外，文件仅操作员可读。候选只创建自己的 SQLite schema，不对外部档案执行迁移。

1. `runtime/admin.json` 由上述 `init-admin` 的同一 PBKDF2 初始化流程生成，仅保存密码哈希，无默认密码。
2. 按 [sources.example.json](../deploy/wechat-console/sources.example.json) 写 `runtime/sources.json`。URL 和凭据引用由服务器操作员登记，前端不能提交任意 URL、路径或上游 Token。
3. server-only `secrets.env` 仅含微信读取 Token 与档案数据库 URL。档案读取使用 PostgreSQL `default_transaction_read_only=on` 或 SQLite `mode=ro` 与 `query_only`。绝不把生产完整环境复制进候选。
4. 操作员配置指定 `CANDIDATE_REVISION` 完整 SHA、`CONSOLE_RUNTIME`、`CONSOLE_SECRET_ENV`、现有获准 `CONSOLE_NETWORK`、候选 UID/GID。HTTPS 额外指定现有代理镜像身份、只读证书目录、候选局域网绑定 IP 与独立端口。默认所有宿主端口只绑定回环。

```sh
docker compose --env-file /path/to/candidate/operator.env \
  -f deploy/wechat-console/compose.yaml --profile https up -d --build
```

Compose project 固定 `cf-wechat-console-candidate`。HTTPS 代理为本候选单独容器，不修改或重启原正式 Nginx；与候选后端共享网络命名空间，代理访问后端 `127.0.0.1:8080`，保留 Host/端口并传递 HTTPS。只公开管理页面与获准管理 API。证书按已有信任链正常校验，不关闭 TLS 校验、不放开微信 6174。

服务设置 `unless-stopped`，脱离 Windows PowerShell 持续运行。最短操作：登录 → 同步来源 → 通讯录找到人 → 登记/关联员工 → 设置两级权限及分组；已发现群 → 详情 → 选择业务分组。分组管理里选择模板和已登记节点。没有可用 Hermes 节点时页面显示真实未验证或失败状态，不意味着正式机器人已采用候选配置。

停止自己的候选，保留数据：

```sh
docker compose --env-file /path/to/candidate/operator.env \
  -f /path/to/candidate/release/deploy/wechat-console/compose.yaml --profile https stop
```

不使用 `down -v` 删除资料，不对正式 project 执行操作。

## 验证与边界

候选与既有 Profile B/迁移联合回归 80 项通过；随后真实字段格式、离线缓存和具名发言者增量的 console 定向回归 27 项通过。实际 Edge 页面联合流程 48 项、后续 UX 定向 18 项通过，均使用合成资料，没有 API mock；[浏览器证据与截图](wechat-console/README.md) 记录各轮范围。新 CI 还验证独立容器与 HTTPS，最终是否通过以对应提交的实际运行记录为准。

头像仅受控 qlogo/qpic 来源，拒绝私有地址、重定向、过大文件和错误内容类型；DNS 解析后固定公开 IP 与 TLS 主机，凭据不转发。昵称、备注、正文、文件名按不可信文本安全渲染。审计记录操作者、对象和结果，不保存密码、Token 或完整私聊。

本轮没有重做官方 Hermes Profile 能力验证。沿用 PR25 的未修改官方版本及已验证管理链；旧会话 SOUL 缓存、新会话生效范围、FileBrowser 可信任务及附件 B 接入缺口继续保留。候选不迁移、删除 Profile 历史，不自动提升文件权限，不接正式微信派发，也不增加微信手工发送、删好友、踢群或修改备注能力。
