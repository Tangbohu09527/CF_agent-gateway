# 目录修复后的有限现场验收（2026-10-16）

本轮承接 [ACK03 现场取证及目录修复](2026-10-16-artifact-return-403-live.md)。以下是实际现场证据，不以隔离测试替代微信验收。时间均为 UTC。

## 固定现场与授权

- Windows 返回插件继续使用 `f606b9de64b53f7d7b22542451e6ef82e4a95164` 的 `plugin.py`，SHA256 `88f1742a9d57dc853b4147b299373e3a83ba32012f1f084afff540a9845b384d`。
- Hermes 源码保持 `427d605d4985c2d49a795493ef6c5d66413057d1`；普通 Admin 的 PID19208、06:57:17.529247UTC 启动时间及两个监听保持一致。
- Gateway 保持 `d9bae61a043f1be4303e5c23661ec51904d5e708`，镜像 `sha256:bcb3e42fef5ba5a91232ca04e49a959cbecab4b45eb3bec0a0b9b3aadf717185`。
- 未重装插件、重启 Hermes、重新 apply、重建镜像或再次修改目录 owner/DACL。原普通 Hermes 身份的目录访问及固定 HTTPS 鉴权重新核验通过。
- 新任务限时授权沿用原任务密钥和已审认证方式，来源限定、禁止转发、固定命令；新记录与旧已撤销记录分开。初始化后密钥登录、限定 sudo 及未获准 root 命令拒绝实际通过，初始化那一轮临时密码随后删除。后续辅助入口修正使用的新凭据及12小时授权按下述收尾记录处理。

## 文字 ACK04

用户在原测试私聊只发送一次 `CF-RETURN-ACK-20261016-04`。

| 环节 | 实际证据 |
| --- | --- |
| 收件与准入 | Message4301，source_local_id4，原聊天绑定一致，allowed |
| Dispatch | 44，一次尝试，08:26:37→08:26:49 success，无错误 |
| 返回协议 | 本轮唯一自然生成的返回台账记录和请求目录时间与 Dispatch44 对应；固定 Gateway 的返回上下文路径要求 ACK 校验通过才能记录成功。未保存原始授权或响应头 |
| Response | `legacy-response:message:4301`，单文字 part 精确等于标记 |
| Delivery | 40，一次尝试，08:26:53 delivered，有逐 part 投递回执 |
| 微信实收 | 用户明确确认收到完全一致的文字回复 |
| Windows 诊断 | 本轮日志偏移后的固定 stage/reason 拒绝事件为0；日志未轮转或截断。此项单独不作为成功依据 |
| 收口 | 守护在投递终态后自动停止 Poll/Delivery；08:29:12 Collect 再次确认关闭，无排队或在途 Dispatch |

返回协议 ACK 和微信文字实收分别记录：前者依据固定实现路径、自然台账及成功任务结果，后者依据用户实际收件确认。没有把未保存的响应头描述为抓包证据。

## PDF 发送前的辅助入口停止

08:32:53 开始 PDF 窗口准备，尚未通知用户发送说明或附件。原聊天首次轮询显示 `messages_seen=5`、`messages_skipped_self=1`、`messages_skipped_checkpoint=4`、`messages_new=0`、`messages_processed=0`。随后一轮全部跳过5条；checkpoint 自然从4推进到5，generation仍为7。

原辅助脚本要求开窗前后数据库快照完全相等，因此对 ACK04 机器人回复造成的正常 self 消息推进返回 `target_scope_changed_before_send`。入口立即停止收发，08:33:53 Collect 确认 PDF 消息、附件及新 Dispatch 均为空，队列和在途为空。这是辅助验收入口的判断缺陷，不能记作 PDF 业务验收失败，也不修改生产 checkpoint 来迁就它。

临时助手随后仅更新 `server_runtime.py` 和包摘要文件，保留旧失败窗口，使用固定的 PDF 接续目录。修正后重新核验密钥登录、限定 sudo、未获准 root 命令拒绝及关闸空队列。没有修改生产 checkpoint、业务配置或服务版本。

## PDF 实际结果

用户确认后，PDF 接续窗口实际开启并确认原私聊轮询。用户先发送说明，再发送原样本；之后因看到机器人等待文件的文字回复，自行再次发送了同一 PDF。这两次发送不能记作一次。

| 环节 | 实际证据 |
| --- | --- |
| 说明消息 | Message4306，Dispatch45 一次 success，Response `legacy-response:message:4306`，Delivery41 一次 delivered；用户截图显示“请发送本次测试 PDF” |
| 第一份 PDF | Message4307，source_local_id7，app 类型，allowed；InboundJob1 一次 ready，Attachment1 |
| 微信取件字节 | 79,083 字节，SHA256 `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44`，与原测试 PDF 一致；消息及清单绑定一致 |
| 附件 Dispatch | 46，08:53:51 四次尝试后 dead；均为 `hermes_dispatch_error:inbound_parent_runtime_unverified` |
| AI 读取 / 返回 | 附件任务在调用 Hermes 前停止；无 Response、Delivery 或返回附件。不能把取件成功或说明文字回复算作 PDF 闭环成功 |
| 诊断插件 | PDF 观察窗口无固定拒绝事件；附件未到达 Hermes，此项不代表附件处理成功 |
| 收口 | 守护于 08:53:52 停 Poll、08:53:54 停 Delivery。08:54:44–45 Collect 再次确认关闭，无排队或在途；dead 总数从4增至5 |
| 第二份 PDF | 用户截图确认再次发送；截至本次 Collect 尚无第二份入库记录。关闭 Poll 不等于取消聊天中尚未轮询的消息 |

固定 Gateway `service.py` 的唯一对应分支是：附件已 ready、线程已有父会话，而 `legacy_runtime_confirmed` 未确认时，在创建绑定及调用模型前拒绝。随后对当前 Dispatch 容器配置和正式数据库进行固定只读审计，实际确认 `host_binding.enabled=true`、`legacy_runtime_confirmed=false`、父会话存在、附件 ready、绑定数量0、读取授权不存在。只输出布尔与计数，没有输出会话、运行配置值或凭据。

因此本次 PDF 的具体断点已确认：已有父会话的历史运行配置尚未获得所要求的部署核验证明，Gateway 按现有安全规则在模型调用前拒绝。它不是 HTTP403，也不是 PDF 下载失败。该开关是部署核验证明，不能为通过测试而直接设为 true；本轮尚未取得满足要求的父会话历史 model/provider/options 与获准执行 Profile 一致的证明，不能声称这种证明在所有外部记录中均不存在。保留 Dispatch46 的原 dead 状态，不重派。

进一步只读核对固定 Hermes 源码：当前 `browser_model_lock` 可被后续模型切换或显式请求覆盖；`session_model_usage` 是路由聚合，不保存逐调用生效 options 和批准 Profile revision，且存在无 usage 或持久化失败路径；标准 messages 也没有逐调用 runtime 快照。因此当前配置相等或已有 usage 无冲突，都不能自动替代完整历史证明。未读取会话正文、修改历史记录或补造证明。

PNG 当前尚未发送或开窗：同一附件链路已有明确前置断点，且原聊天中还有用户重复发送、尚未轮询的 PDF。贸然开启 PNG 窗口会同时接入该文件。PNG 不能记录为通过，也没有把图片消息、缩略图或链接算作原文件回传。

## 十二小时授权与最终收口

用户另行明确授权把本任务的临时凭据及 SSH/sudo 授权上限都改为12小时，并在任务结束提前清理。

- 本机仍使用原 CurrentUser DPAPI 和原 ACL。原创建时间保持不变，将有效期固定为创建后12小时；原子替换单一密文，不保留旧密码密文备份，不输出密码。实际迁移回执确认12小时且 staging 已清除。
- SSH/sudo 续期只修改当前任务 metadata 的有效期/公钥行及 `authorized_keys` 中唯一任务公钥的到期值。沿用原密钥、来源限制、禁止转发和精确固定命令；sudoers 字节不变。到期固定为原授权起点后12小时，原安装记录保留原4小时事实，新增独立续期证明。
- 续期后实际验证任务密钥登录、限定 sudo preflight 和未获准 root 命令拒绝。微信窗口的180秒待发送、首消息后约120秒取证及自动关闸期限未延长。
- **09:10:51–53** 最终 Collect：Poll/Delivery 关闭；Gateway/Dispatch 服务仍运行；无排队或在途，dead5、success41、delivered41；checkpoint7/generation7。第二份用户重复 PDF 仍未轮询，不声称已取消。
- 本轮公钥和限定 sudoers 随后实际撤销，固定入口返回 `TASK_AUTHORIZATION_REVOKED=true`；之后仅一次原密钥登录检查得到预期 SSH exit255。
- **09:11:47** 通过12小时兼容清理入口删除临时凭据，独立检查 `credentials.dpapi` 和 staging 都不存在。保留原任务密钥文件及非秘密证据。
- **09:13:45** 当前 Windows 复核：Hermes427d605、原 PID19208/启动时间、两个监听、f606b9d 插件摘要和工作根安全描述符仍一致；没有再次重启或修改权限。

本轮未重跑旧 Hermes 隔离矩阵。新增检查只覆盖临时助手的自回声边界、固定发布/续期事务及12小时凭据适配；这些检查不替代真实微信结果。最终结论是：**文字通过；PDF取件字节通过、AI读取及附件返回失败；PNG未执行。**

证据保存在本任务私有目录 `.task-artifacts/acceptance-20261016/`；服务器保留独立的 text、原 PDF 失败窗口、`pdf-continuation-1` 证据，以及助手更新和授权续期备份目录。最终 Collect 输出 SHA256 为 `0534cff1fae024cee3731df9b487d4bf07afcde46d716c90b026cbaa0ac3575f`。旧 request/plan/journal 和历史 ACK01/ACK02/ACK03 记录保持原样。

## 后续边界

继续文件验收前，需要取得与该父会话逐次请求绑定的批准 Profile revision、实际 provider/model、生效 options 及完整性证据。若这些历史证据确实不可取得，应另行设计保留旧会话、从显式绑定 runtime 的新会话开始的业务方案；不能直接翻转确认开关、丢弃历史或重派既有 dead 任务。本轮未实施这些超出当前配置保留范围的变更。

重开收发前还必须单独核对原聊天中第二份 PDF，不能清 checkpoint 或忽略它。就本轮安全断点而言，可以关闭 AI 主机；重新开机和后续开窗必须重新核验在线版本、登录、队列及聊天中的待收消息。本轮三项验收尚未全部通过。
