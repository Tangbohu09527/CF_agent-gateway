# 2026-10-18 附件接续生产切换与开窗前范围核验

## 03:12 UTC 最终现场结果

**Gateway 接续切换和第二份 PDF 取件通过；PDF 实际内容读取及附件回传失败。**
用户截图确认收到的是失败说明文字，没有 PDF 附件。后续文字和 PNG 未执行。
此前 ACK04 实收、第一份 PDF 完整取件的事实保留，不改写为本次附件闭环通过。
下面的历史步骤保留当时状态，本节覆盖其“待切换／未入库／未授权”断点。

### 实际运行版本与维护结果

- 用户补充批准精确账号／私聊过滤后，独立 r2 计划于 02:54:53—02:55:07 UTC 完成第二次切换。
- 应用提交 `e742461b2f963a862b8f5856f0d78a1277e41ada`，六服务镜像选择为
  `sha256:f2c50392138d0515866748ca3d933371131b544b4accd4eb7aba6ce3ee3a77ac`。
- schema 保持 `20261016_01`；本次过滤切换没有重复迁移，没有再次重启 Hermes。
  gateway / dispatch-worker 健康，最终 worker / delivery-worker 停止。
- 正式配置仍为 `/opt/cf-agent-gateway/config/production.yaml`，实际只读挂载已核；SHA256
  `774762c1ecb9f7c2055f1b6f73e52500f940d35c1402f618e74897d1dfbe93c1`。
  正式 Compose SHA256 `58363d769eed8f83c29c98c51ab9de7fd6a342d9b2ecb5d5ecd79d72fda139bb`。
  `.env`、已核实的外部 env 引用和无关服务语义保持。
- Hermes 仍为 `427d605`，f606 诊断插件及已修复目录 owner/DACL 保持；02:56:59 UTC
  只读核验仍是此前官方重启的同一个普通 Admin 进程及相同受保护输入。
  这是进程连续性与启动输入证明，不是读取进程内存，也不是旧会话历史证明。
- r2 计划 SHA256 `6cc623d634121ca1c6142eed956bb0ef26a0bf173b7475bcc3474894e1078469`；
  activation SHA256 `a45e40efeb364e633633ab2de965be787c7aaa990de24a2804579b482f130661`。
  包与证据分别保留在 `/opt/cf-gateway-poll-scope-20261018-r2`、
  `/root/cf-gateway-poll-scope-20261018-r2`。切换前的新数据库备份通过验证，未执行恢复。

首次新增镜像构建在依赖等值检查停止：fastapi、sqlalchemy 出现版本漂移，尚未切换正式服务。
失败证据保留。r2 从已核实的原镜像实际安装 e742 源码，37 项依赖等值、187 个安装文件、
镜像配置与原层继承核验通过；不是给旧镜像改标签。r2 核心和传输共 108 项针对检查通过。

e742 自身的真实 CI 为六 workflow、24 jobs 全部成功；push / PR 完整套件各为
2638 passed、3 skipped。精确轮询过滤的 35 项针对检查通过。后续本记录的文档提交不是
新的应用镜像，不以旧应用 CI 冒充文档新 head 的 CI。

### 第二份 PDF 的真实链路

只使用原私聊已发送的第二份 PDF，local9 与原账号、私聊、发送人、时间、文件事实共同匹配；
没有让用户重发，没有人工重派。03:04:32 UTC 确认目标私聊已实际轮询，有限窗口截止
03:07:32 UTC；任务完成后 guardian 于 03:05:41 UTC 提前关闸。

| 环节 | 实际结果 |
| --- | --- |
| Message / 准入 | `4308`，`allowed`；原消息发送于 10 月 16 日 08:54:06 UTC，10 月 18 日 03:04:19 UTC 正常入库 |
| Dispatch | `47`，success，1 次 HTTP 尝试，无 dispatch/reconcile 错误 |
| 入站媒体 | job `2` / attachment `2`，ready，1 次尝试；79,083 字节，原样本摘要匹配 |
| 执行接续 | fresh execution；新根会话已创建，原聊天绑定，携带已冻结的文件说明 Message4306；未借用旧父会话 |
| 返回协议 ACK | 固定 e742 路径且返回功能启用时，成功提交前客户端必须通过 ACK 校验；这是源码与状态推断，未记录原始 ACK header |
| Response | `legacy-response:message:4308`，delivered，只有一个 text part，无 Artifact |
| Delivery | `42`，delivered，1 次尝试、1 份投递回执；这是 Delivery42，不是历史 Dispatch42 |
| 用户实收 | 截图及用户确认收到失败说明文字；未收到实际 PDF 附件 |
| 内容读取／原件返回 | 未通过；没有读取或返回工具成功回执，不存在接收 PDF 摘要证明 |

PDF SHA256：`ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44`。
ACK 校验通过与“文件已读取／已返回”分开记录；无 Artifact 时，源码允许提交纯文字响应。
取证中的 `followup_managed_tip_confirmed=false` 是 PDF 分支未执行文字阶段检查的默认值，
不能据此判定会话接续失败。后续文字的持久化续接尚未真实验收。

### 具体失败原因与证据边界

只读 Hermes SQLite 的本次时间范围、匹配样本会话，导出工具名／固定状态码的安全投影：

1. `filebrowser_download_inbound(attachment_id=2)` 成功，verified=true，79,083 字节及上述摘要一致，formal_archive=false。
2. 随后调用 `filebrowser_files` 的 `stat`，传入 `input.handle`，实际返回 `invalid_tool_input`。
3. 该接口只允许 `source/path` 等固定字段；`handle` 在参数字段检查处被拒绝，尚未进入客户端或文件权限检查。
4. 全部工具回执中没有 `filebrowser_read_inbound` 或 `cf_return_current_chat` 调用。

当前安装的 FileBridge manifest 为 0.3.0；入口只接入 inbound 下载和 host，没有注册
`filebrowser_read_inbound`。虽然磁盘有 `inbound_content.py`，它依赖的两个 host helper
在当前已安装 host 文件中不存在，不能只开一个配置项就视为读取能力完整。
已有内容读取开发线可供后续适配，但本轮没有替换 FileBridge 文件。

因此，当前具体阻断是：下载产生的受保护工作副本句柄没有接入模型可用的内容读取路径；
模型尝试把它用于另一个不接收句柄的文件接口，触发了正确的参数拒绝。
返回工具另要求授权工作目录内的相对 `file_ref`，不能直接传入 `inbound:` 句柄。
后者是源码契约缺口，本次未实际调用返回工具，不能虚构返回插件 stage/reason。
模型关于“安全策略阻止本地查找”的文字不作为 ACL 根因证据；本轮没有新的 HTTP403。

已完成 5 项新的纯源码契约复现：使用已安装代码的固定摘要临时副本、假 ctx/下载回执，
阻断网络和 subprocess、隔离 HOME/AppData/TMP；验证注册缺口、stat 对 handle 的拒绝、
返回接口在访问文件系统前拒绝 opaque handle。它证明源码契约，不是运行进程完整工具目录。
Hermes 的 tool_names/system_prompt 持久字段为空，未将其解释为“运行中没有任何工具”。

### 自动收口、撤权与后续边界

03:06:35 UTC Collect 和 03:12:45 UTC 撤权 Collect 均确认 Poll/Delivery 停止；
无非终态 Dispatch、Delivery 或媒体任务。历史 Dispatch42/43/46 保留；
checkpoint 由原 Poll 自然推进到 local9，没有手工修改。第二份 PDF 已处理成 Message4308，
不再称为上游待收，不能再用它重新开窗或重派 Dispatch47。

服务器端本轮限时授权实际为：UTC 2026-10-18 03:02:36 至 2026-10-19 03:02:36，
北京时间 10 月 18 日 11:02:36 至 10 月 19 日 11:02:36；起点是成功安装事务开始时刻，
安装完成于 03:02:40 UTC。key expiry 与固定入口窗口均核验匹配，保持来源／固定命令限制。
初始化正向 key 登录、限定 preflight、未授权 root 命令负验证通过后，临时 DPAPI 密码已删除。
本轮于 **03:12:45 UTC（11:12:45 北京时间）提前撤销**任务公钥和 sudoers，
固定入口持久化并输出撤权确认；随后新 SSH 连接明确被 publickey 认证拒绝。
再次核验临时密码文件不存在。保留原任务私钥、失效授权材料和诊断证据，不继续远程管理。

最小后续工作是复用既有 FileBridge 内容读取实现，并补齐受同一有效 Dispatch／Profile／
原聊天约束的工作副本到返回工具衔接，保留身份、时效、摘要与重放检查。先做隔离候选，
不能通过暴露真实路径、放宽文件接口、修改目录权限或人工复制生产台账来通过验收。
这需要额外插件变更，超出本轮保持 f606 / FileBridge 不变的生产范围，尚未生产实施。
PDF 闭环成功后才可继续后续文字及 PNG；PNG 原文件字节与图片消息显示仍须分别验收。

已有新执行会话接续记录：禁止破坏性 schema 降级、恢复旧快照清空接续记录，
也不让旧程序未经验证接管新会话。当前核心保持运行、收发关闭，无未收口任务。

脱敏链路证据 SHA256：`279c2a9446e56d6cc10baecc499bf61cf4022a53543bca7efb89eebc9d7c669b`；
撤权 Collect SHA256：`ba227c18ba7b87a64fd946adfdec7198d853bf506b99d051f113cb04bbec436d`。
私有回执在 `.task-artifacts/current-live-20261018/`，源码契约复现位于原 d02d 工作树
`.task-artifacts/pdf47-contract-repro/`。不提交授权包、密码、完整现场日志、会话或业务内容。

## 以下为历史执行步骤及当时断点

## 已完成的生产维护

用户返回后明确恢复了生产切换及真实文件验收授权。原候选于
2026-10-18 01:59:22 UTC（北京时间09:59:22）正式切换成功：

| 项目 | 实际值 |
| --- | --- |
| Gateway应用提交 | `baa4aff959e4a8866f4b71f135a40c1f104d35c1` |
| 六服务镜像选择 | `sha256:5c8115976ddf27c2a56462251800676cb4bf1c87752c77cb08872d501a08b210` |
| 正常迁移后的head | `20261016_01` |
| 正式配置 | `/opt/cf-agent-gateway/config/production.yaml`，四个实际容器均只读挂载 |
| 配置摘要 | `354c660385db4b93f664ce8b121d91205cffde912f5e472cf56ddaa39055a4a6` |
| 正式Compose摘要 | `d08deecf2f820404020d43c0bdf05f62921d3a4c42a372721e57c7954190c04e` |
| 核心 | gateway / dispatch-worker 已运行且健康 |
| 收发 | worker / delivery-worker 停止 |

应用镜像来自先前实际构建的baa4aff；后续14a034a是部署辅助程序和测试记录版本，
没有把旧镜像重新标记成14a构建。六服务包括四个应用、migration、heartbeat-init。
`.env`、外部env_file、其他服务语义和旧request/plan/journal保持。

本轮先验证无在途、文件身份、独立计划及冻结的文件说明，再生成数据库备份；
备份通过格式和目录检查后才停止核心。备份1218797字节，SHA256为
`646a0e47d42332bd6b8f4be1e9fab0f5dbb7c4d1aa9efcf5847ffe631e6626eb`。
未执行数据库恢复；备份存在不等于已做恢复演练。

Hermes通过当前官方入口在01:58:35 UTC受控重启，源码仍为427d605，普通Admin身份，
f606b9d诊断插件、配置和既有owner/DACL未修改。新进程的受保护启动输入及有效fallback
链为空得到核验。此证明针对未来执行，不代替旧会话运行历史证明。
Windows辅助程序只修正其只读PowerShell子进程继承PSMODULEPATH造成模块加载失败，
没有修改全局环境；19项针对验证通过。切换辅助程序66项针对验证通过。

生产activation记录SHA256：
`7e661124b3806c4a4822586b80bed9ab749b79499c99eb7814d6348629a59b9d`。
本地私有证据位于`.task-artifacts/current-live-20261018/activation-audit/`和
`.task-artifacts/continuation-production/switch-r5/`；这些包、凭据及完整日志不提交Git。

## 开窗前发现的准确范围缺口

02:05 UTC独立只读核验：登录仍为原账号，但`GET /api/chats`返回22项。
这不证明产生了22个模型任务；本轮没有启动Poll、没有读取这些其他聊天的消息正文。

现实现的调用顺序是：遍历聊天列表 → 读取该聊天消息 → checkpoint/self过滤 →
消息入库 → sender身份/用户策略及群提及等业务准入。业务准入拒绝可阻止Dispatch，
不能阻止其他聊天读取、归档或自然推进checkpoint。`fresh_session_thread_ids`
仅控制附件的新执行会话入口，不是全局轮询聊天范围。

临时验收辅助程序曾把“列表恰有一个原聊天”作为充分前置条件。这是辅助程序的保守
检查，不是host_binding原契约，也不是`inbound_parent_runtime_unverified`的根因。
不通过改上游聊天配置、删除checkpoint或放宽准入来迁就检查。

因此保持收发关闭，在现有PR17分支准备最小可选前置过滤：明确绑定来源账号和
conversation，在任何该聊天的`list_messages`及checkpoint/ingestion操作之前过滤。
账号不匹配拒绝，默认未配置时保留原行为。其它入站API和已经存在的全局媒体/Dispatch/
Delivery队列不由此配置改变；短窗前仍核验队列为空、无其他媒体在途。

这一生产代码变化需要新镜像及精确配置补充，不能沿用baa镜像宣称已具备过滤。
后续切换不重复执行迁移、不重启Hermes、不修改权限或历史任务；须先完成候选及
针对验证并按明确范围批准。获准和准备完毕前不接收待测PDF。

## 真实业务结果与保留状态

02:05 UTC只读快照仍为messages4307、dispatch46、responses41、delivery41、
inbound_media_jobs1、attachments1、inbound_host_bindings0；无待执行/在途Dispatch或Delivery。
新绑定列存在且没有新增接续记录。checkpoint仍为local7/generation7。
Dispatch42/43/46原状态未修改或重派。

第二PDF身份在本轮只读核验中对应原账号、原私聊、原发送方、local9、type49、
发送时间2026-10-16 08:54:06 UTC及既有样本文件名，尚未入库。local_id并非跨聊天唯一键。
第二份尚未取字节，不把第一份已验证摘要套给它，也不要求第三次发送。

此前ACK04实收及第一PDF完整取件结果保留。本轮尚无PDF实际AI读取、附件返回、
Response/Delivery或用户收件证明，后续文字与PNG均未执行。不能将切换成功或隔离测试
记为真实文件闭环成功。

## 认证和回退边界

本轮凭据由既有遮蔽输入程序一次提供，沿用Admin DPAPI/ACL及原12小时密码存储上限；
SSH及受控sudo实际成功。旧SSH/sudo授权已撤销。本轮24小时固定授权当时仅准备了本地
候选，尚未安装，不能写成已经续期或虚构服务器起止时间。

新schema已生效，不自动降级、清空会话或让旧版本未经检查接管新会话。当前尚未产生
新的执行lineage；若后续产生，回退必须保留其完整历史及绑定，不能破坏性恢复旧快照。
任何异常先关闸并核实在途，再按具体阶段收口，不自动全量回退。

## 本轮新增隔离验证

精确轮询过滤的35项针对性检查通过，包括27个新增case、4个runtime装配case及
4个既有默认/兼容case。22聊天用例使用真实隔离SQLite，验证只有目标产生Message、
Dispatch及checkpoint操作，其他账号/聊天的原游标字段与更新时间不变；还覆盖账号
漂移、目标缺失、身份别名歧义、无效配置、默认兼容和重建runtime后的自然续读。
代码经独立审查，ruff lint/format及diff检查通过。这些结果尚不代表新候选已经部署
或真实PDF/PNG验收通过；旧head14a034a的24项CI成功不替代新提交的CI。
