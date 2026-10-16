# 附件执行会话接续

本变更是 Gateway 候选，默认不启用。ACK04 及 PDF 完整取件的现场结果继续以
[现场验收记录](../validation/2026-10-16-artifact-return-403-acceptance.md) 为准。
它不修改返回插件、Hermes 版本、目录 ACL、历史 Dispatch 或微信 checkpoint。

## 契约及缺口

原 [host binding 契约](inbound-host-binding-contract.md) 要求已有线程 fork 当前
Hermes tip，继承其历史，再明确设置批准的 runtime。`legacy_runtime_confirmed`
是有适用范围的部署 attestation；契约没有要求为所有旧聊天补造完整逐调用审计。
现场 Dispatch46 没有取得可适用于该旧父会话的部署证明，故保持该值 false。
当前配置相等、usage 无冲突或人工断言不替代旧历史证明。

新的路径不继承未知旧历史。精确指定 `host_binding.fresh_session_thread_ids`
才允许该私人线程的首次附件任务创建新的根执行会话。默认空列表保持旧拒绝行为。
原员工、微信账号/私聊、Gateway AIThread、准入、工具权限和返回目标不变。
原旧会话不 fork、不结束、不删改；只在新附件任务成功事务中推进执行 tip。

## 上下文的明确边界

首次新根会话只带入附件前、同身份/同 workspace/thread/来源账号/私聊/发送人的
最近一条成功入站文字。选择遵循 Dispatch 的 `(created_at, id)` FIFO 次序，
要求 SUCCESS 和持久 Response；不跳过中间成功附件寻找更早说明。
文字须完整且不超过 16,000 字符，空缺或超长显式拒绝，不截断、不调用摘要模型。
首次 intent 固定消息 ID、来源与正文摘要，重启或同 claim 恢复不能重新选取。
本条附件自己的文字仍传入。二者都是用户内容，不提升成系统权限。

因此“说明文字 → PDF”可在首次附件的同一次模型调用中执行，不额外发起种子调用。
首次只复制这一条说明，**不声称继承旧会话全部历史、隐藏系统提示或工具轨迹**。
“照上次方案处理”若依赖更早内容，不能视为已完整继承，需在说明中明确任务。

PDF 成功后，后续文字使用新的 tip，并每次显式发送批准的 provider/model/options。
下一份 PNG/PDF 从已成功的受管 tip fork，继承这一新会话中的真实历史，再恢复同一
runtime。后续附件不重复插入首次说明。fork 会结束它的受管父会话为 branched，
但不会修改最初保留的 legacy 根会话；子会话及消息仍由官方接口持久化。

## 未来 runtime 的启用条件

当前官方 Hermes 427d605 的原生隔离探针证实：`/v1/chat/completions` 即使收到显式
provider/model/options，已持久化 `/model` lock，仍可能在主模型失败时使用全局
fallback；在请求正文增加 `require_model_lock=true` 也不能阻止。改用 session `/chat`
虽会强锁，但模型失败仍可能是 HTTP 200 且缺少可靠失败字段，不能替换原链路。

因此启用新模式前，须核验专用执行 Profile 的**有效 fallback 链为空**，留存受保护
配置/启动身份及摘要的证明，才设置 `runtime_fallback_disabled_confirmed=true`。
这个默认 false 的声明只承载未来执行环境的部署核验，不证明旧历史。非空首次切换
列表而未确认时配置加载拒绝；已受管会话也不能在确认条件撤销后退到默认 runtime。
配置需保持受保护且端点只允许 Gateway 调用；改变配置需要重新验证。

公开 session API 无法自动证明隐藏的有效 fallback 配置。runtime digest 证明批准
请求配置没有漂移，**不称为 Hermes 实际执行强锁证明**。当前现场是否满足此条件
必须另做只读核验；不为迁就候选擅自修改 Hermes 配置。批准的 provider 对 options
的实际支持也仍按原 host-binding 契约验证，不能把 `/model` accepted 当作全部选项生效。

## 持久化与拒绝边界

迁移 `20261016_01` 仅给 `inbound_host_bindings` 增加四个 nullable 列：
`execution_policy`、`previous_session_id`、`instruction_message_id`、`instruction_digest`。
不回填旧记录。`fresh_runtime_v1` 是新执行历史的策略标记，不是旧历史 attestation。
首次 `parent_session_id=NULL`，`previous_session_id` 留存原 CAS 预期 tip。
后续绑定的 parent 和 previous 都是已验证的受管 tip。

create/fork ID、策略、前 tip、说明证明和原加密授权在第一次外部副作用前落盘。
仍使用现有 preparation fence、history/runtime digest 和一次性 claim。模糊结果保持
UNCERTAIN，不换 ID、不再自动创建。只有原 lease/claim、host close barrier 及 tip CAS
全部成立，`complete_success()` 才同事务提交 Dispatch、Response 和新 tip。

后续信任依据包括：标记绑定、创建 Dispatch SUCCESS、对应 Response、相同身份/来源/
thread/Profile revision/host/runtime digest。正常 closed/revoked 的 grant 不再可读，
但完成记录可证明执行历史来源；仅关闭授权不代表成功。移除首次切换 allowlist
不会令已受管文字降级为默认 runtime。配置变化、身份撤销、陌生 tip 或跨聊天均拒绝。

当前候选延续原附件路径的限制：不自动接纳压缩后的新 tip。受管文字在调用前后用
真实 session/messages API 检查 tip；响应头回显原 ID 不足以通过。发现轮转进入
UNCERTAIN，不能自动 reset 或丢历史。扩展压缩接续需另行实现 lineage 验证。

已有运行 lineage 时禁止 schema 降级删除这四列。关闭新切换开关不是回到旧会话的
操作；已切换后的源码回滚也必须先关闸并检查在途，不可把旧程序接到新 tip 后任其
使用默认 runtime。数据恢复需独立具体审批，不能自动清空会话或改 dead。

## 生产接续边界

这是 Gateway 代码和 schema 变化，需要新候选镜像、正式加列迁移及仅原私聊的配置
启用，超出之前仅 Windows `plugin.py` 的诊断部署。须先审阅候选和隔离证据再授权。
Hermes 427d605、返回插件 f606b9d、FileBrowser/PostgreSQL 服务配置及历史材料保持。

开窗前必须只读核对第二份已发送 PDF 的源消息身份、时间、文件事实及是否已入库。
数据库没有在途不能说明微信上游为空。优先接入这一份，不让用户发送第三份。
Dispatch46 原 dead 不重派；第二份若未入库，将经原 Poll/准入/取件创建自己的新任务。
若已入库，则先核对它的现有状态，不重复发送或人为重派。

用户确认立即配合后才通过原 Controller 开短窗口。PDF 必须实际读取、原字节返回、
用户收件确认后才进入 PNG。PNG 原文件字节与图片消息显示分开验收；本候选不改变
现有 `kind=image` 与 `kind=file` MIME 规则。每项有限监控后 Collect、关闸并检查在途。
12 小时认证上限不延长收发窗口，任务结束清除临时密码并撤销本轮任务授权。
