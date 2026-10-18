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
