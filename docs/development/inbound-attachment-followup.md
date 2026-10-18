# 已登记附件的显式续办（候选，默认关闭）

本功能只为原私聊中新发的一条操作消息授权，复用已登记且仍可读取的原件字节。它不重派旧 Dispatch，不延长旧 handle/lease，不搜索聊天历史，不默选最近附件，也不修改原件或旧任务。

## 正式入口

继续使用普通微信消息的 NormalizedWechatMessage → MessageAdmissionService → 持久 Dispatch，不提供绕过准入的新管理 API。需要配置 `inbound_media.enabled: true` 和 `inbound_media.followup_enabled: true`；后者默认 `false`，关闭时保持原消息处理行为。

第一版只支持 private 聊天的两个精确动作，首行格式固定：

```text
/attachment 123 read
请读取并说明这个已登记附件的内容。
```

```text
/attachment quoted read-return
请读取所引用的原附件，并将未改变的原件返回本聊天。
```

- `123` 是经现有受认证入站状态接口核实的 Attachment ID，不是本地文件名、opaque handle、Dispatch ID 或任意路径。
- `quoted` 要求真实微信引用消息携带上游 local/server ID。接收端将它映射为 `reply_context.source_local_id/source_server_id`。同时存在时两者都须匹配；引用文本不参与选择。raw1 text 和已规范化的 raw49 reply 可用；不从未知 XML 或正文中猜 ID。
- 本轮只验证规范化 raw49/reply 的构造输入；真实微信引用到 reply_context 的现场协议链尚未验收，不能把该单元证据当作现场引用功能通过。
- 首行只接受一个选择器。第二行起为本次说明，最多 4096 字符，不截断。数字范围为正的 64 位有符号整数。
- 无匹配、多匹配、冲突引用、跨私聊/账号/员工、未知动作，均生成固定本地澄清回复，不调用模型或签发附件 grant。不会返回外部附件是否存在的细节。
- `read` 不签发文件返回 capability；持久化完成时也拒绝结构化或 legacy Artifact 绕过。
- `read-return` 只允许一个原件 Artifact（slot 0），服务器检查 SHA-256、大小、kind 和 MIME。PDF 为 `file/application/pdf`；PNG 为 `image/image/png`，不声称 PNG 以文件消息形式显示。
- 自然语言“刚才那张”、多附件批量操作、群聊续办和 `return`（不解析而只返原件）不在第一版范围。现 FileBridge 的可信 prepare_return 在读取成功后运行，因此不能把 read-return 描述成无需解析的纯返回能力。

这只是入站附件读取/回传权限边界，不是新通用工具沙箱；不会给模型新增文件系统、归档或其他业务工具权限。

## 新授权和原件历史

新消息在正常准入事务中创建自己的唯一 InboundMediaJob，保存 `source_job_id` 和 `followup_action`。原件仍是原 Attachment 行：其 message_id、缓存 reference、hash、size 和原 InboundMediaJob 均不变。source_job_id 只能指向原始 ready job，不能形成续办链或循环。

每次授权重新核对：

1. 当前 source/account/private chat/sender 与原件完全一致；当前 identity/workspace/thread 仍有效，同一准入范围，当前用户/网关策略仍允许此次请求。
2. 新操作消息的完整源指纹与内容/引用未漂移；原始消息指纹、Attachment 登记、manifest 与稳定缓存 reference 一致。
3. 原 Profile/revision 有持久证据：原 v2 admission snapshot 或原 HostBinding；存在矛盾或没有证据时拒绝。新官方 prepare_binding 的 Profile 必须相同；返回 capability 另外绑定本次 claim 的真实 HostBinding/session，拒绝跨会话/错误 Profile。
4. 原缓存文件在新 fork/模型调用前真实读取并验证大小/摘要。后续下载仍在读取前后执行原授权/host fence；文件缺失、损坏、撤权、claim 变化或到期均拒绝。不会重新下载旧微信消息来绕过缺失。

新授权的最终期限不超过本次 job 的 `deadline_at`（沿用 `wait_seconds`，默认 900 秒），也受新 Dispatch claim 和 host lease 约束。原任务的租约即使已过期或已 dead 也不恢复。重复消息沿现有 message/dispatch/job 唯一约束去重；同 claim 的重复 prepare 复用唯一新 intent，改变 parent 或 claim 时拒绝。

上下文沿现有 managed session 规则：新附件操作使用当前已验证的 tip 创建新的受管执行会话；不会退回原件所在的旧会话或将完整历史复制成系统指令。重建 Gateway 会话对象和官方 Hermes 进程重启的证据分别记录，不能互相替代。

## 配置与迁移边界

迁移 `20261018_01` 在 `20261016_01` 后新增两个可空列、自引用外键与固定动作约束。旧记录不回填、不改状态、不动 checkpoint。已有续办记录时拒绝破坏性 downgrade。生产使用新 Gateway 镜像和正常 migrate/check；不能只替换 Windows 插件或用直接 SQL 变更状态代替。

一旦有续办记录，回退先关闭收发并停用 followup，阻止新操作；由能识别新 schema 的程序完成在途收口。旧程序对新 schema 的兼容性必须单独验证，不能直接让旧镜像接管，也不能删除续办记录以强行 downgrade。

本轮只独立开发与隔离测试，未部署或开启生产收发。将来若另行批准部署，最小准备须核对：

- 精确新镜像/迁移、受保护配置以及 Gateway/Poll/Dispatch 一致读取的 `followup_enabled`；默认仍关闭。
- 原账号/私聊的既有 pre-poll 范围过滤和原准入策略保持；不为续办开放群聊或其他聊天。
- 原 private staging root 和文件身份保持，不重置缓存/队列/checkpoint；只有完整原 Profile 证据且文件可用的附件可续办。
- 原 Hermes/Profile/runtime 固定契约、FileBridge read/prepare 与返回插件兼容证据。没有新增凭据、授权入口或远程服务。
- 备份、正常停机/迁移、有限测试窗口及提前撤权仍需单独生产批准；本候选本身不构成该授权。

## 本地验证范围

`tests/test_inbound_attachment_followup.py` 覆盖精确引用、重复/并发消息、默认兼容、group 拒绝、源权限/策略撤销、期限、错 Profile/parent、原件缺失/漂移、读后过期、原 dead/旧授权不变及迁移保留。

`tests/test_attachment_followup_returns.py` 覆盖服务器签名约束、实际新 HostBinding/session、精确原件 PUT/GET/seal、legacy/禁用返回绕过、撤权/漂移及普通生成文件兼容。

`tests/test_inbound_followup_integration.py` 使用真实 SQL、Gateway HTTP、claim、Artifact/Response/Delivery；Hermes 和微信为明确的确定性替身。验证原任务后新操作回原件、Gateway engine/client 重建后再续办、read 无返回权限、缺失文件在 fork 前失败。它不证明真实模型理解或实际微信收件。

真实官方 Hermes 427 + FileBridge 的专门定向探针由 `tests/hermes_file_return_probe` 记录；必须以该次执行结果为准，不能把上述替身测试称为原生链通过。

## 本轮关联证据

此前 PDF、明确说明后 PNG 的真实验收及其边界见[固定现场报告](https://github.com/Tangbohu09527/CF_agent-gateway/blob/acd756da6ff401cb1267b184faf0730d23977d21/docs/validation/2026-10-18-inbound-read-return.md)。这些是既有入站读取/返回链的现场结果，不是本候选附件续办的生产验收。

文件名保留独立于本功能，见 [WeChat Draft PR #9](https://github.com/Tangbohu09527/CF_agent-wechat/pull/9) 及[固定文件名候选报告](https://github.com/Tangbohu09527/CF_agent-wechat/blob/a500a4cd29612e910df679e31c34638bef7ca43e/docs/validation/2026-10-18-return-filename-candidate.md)。本候选不合并该协议修改，也不把其隔离证据当作现场部署结果。

## 固定候选的验证记录（2026-10-18）

以下已完成 CI 记录针对产品候选 `9abfb30248952babf8624a7c04896258ba479f61`。本次收尾另包含下述 PDF/PNG 测试参数化及证据文档；产品源码没有变化，但新提交仍须由其自身 CI 验证，不能用旧 head 成功替代。

[官方完整隔离链 run 39113515669](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39113515669) 已成功，实际执行 PR merge `dc68a45210c4d66d2455851be7117e9a5a1727cd`（父提交为 base `7f022fd4f28f756e1f21812ba97fc7417308281a` 和上述候选）。下载的 source-proof 中 189 个 Gateway 源文件摘要逐一匹配候选 Git blob；固定 FileBridge 为 `f3a63895e33958dda2bbc4f3413c5619bdfabdff`，Hermes 为 `427d605d4985c2d49a795493ef6c5d66413057d1`。

安全回执 `safe-summary.json`（SHA-256 `a9a3c53bc0ba45e82f87381ab61e5a30d55f61dcbaebe03f29b2a4b6214cc673`）证明：两次显式 read-return 均创建新的 Dispatch、task/session 和 Artifact，仍引用同一原附件，原 completed job/lease 保持不变；原件字节和含中文、空格、括号的 PDF 文件名在实际 Gateway 出站 HTTP JSON 中精确相等。第二次操作发生在同一隔离 HOME 的真实 Hermes 子进程退出、重启后。第三次 read 生成新任务并交付文本，未获得返回 context、未创建 Artifact。过期会话引用与无关附件均被拒绝；credential leak file count 为 0。

该链运行真实 Gateway HTTP/数据库、官方 Hermes 和 FileBridge，模型及微信仍为确定性 loopback 替身。Gateway 只重建 session factory，没有重启 Gateway 进程；PNG 检查的是图片字节，并不证明文件消息显示或原名保留。上述结果不是生产附件续办验收，也不证明自然语言引用或真实微信 quote 协议。

本地定向结果为：80 项新续办业务/SQL/HTTP/迁移安全检查、35 项官方探针合同检查、35 项既有正常媒体/访问/schema 检查、68 项既有返回 HTTP/集成检查通过；ruff、format、diff check 通过。 随后仅参数化现有正向集成用例，补充已登记 PNG 的两次新 read-return：PDF/PNG 两参数实际重跑 2 passed，真实 SQL/HTTP/Delivery 核对新 image Artifact、image 路由、大小/摘要/原字节和原任务历史不变。该补充使用明确的 Hermes/微信替身；它补齐 PNG 续办正向边界，不能把此前官方链的 PDF 续办误称为原生 PNG 续办通过。固定候选 25 项检查均已成功；[PR CI run 39113515681](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39113515681) 于 2026-10-18 09:16:05 UTC 完成，完整套件为 2804 passed、6 skipped，迁移 fixture 为 47 passed，V2 为 6 passed，PostgreSQL 核验单一 schema head `20261018_01`。[push CI run 39113512989](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39113512989) 也成功。新参数化提交的准确 SHA 与实时检查可在 PR #20 查看，尚未完成的检查不计为通过。旧 head `89bf362` 的重复 CI `39113475085`、Hermes `39113475093` 已取消，被最终 head 检查替代，不能计为成功。

文件名协议仍属独立 B 候选。其固定 `a500a4cd29612e910df679e31c34638bef7ca43e` 的 19 项检查及[专用 Rust/Qt run 39112588669](https://github.com/Tangbohu09527/CF_agent-wechat/actions/runs/39112588669) 已成功；该协议候选未实际部署，不能把本链的 Gateway 出站 filename 相等当作真实微信协议端已经保留原名。
