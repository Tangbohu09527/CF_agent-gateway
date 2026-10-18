# 2026-10-18 附件接续生产切换与开窗前范围核验

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
