# 微信入站附件第三步：持久取件与授权交接

2026-09-27，继续 Issue #13 / PR #14，父提交为 `60baac8f7de78ce6993dcf87007c9997b9ae2ad5`。
本轮只开发和测试隔离副本；没有连接 CFserver、运行中的 Hermes、真实凭据或聊天文件，没有部署、发微信、修改 Checkpoint、重排历史任务、合并或发 Tag。

## 实际接线

`runtime.wechat.run_wechat_poll_once` 将配置传给准入 sink。消息先持久化，只有新消息通过身份与会话准入后，才在同一事务中提交 admission completion、queued Dispatch 和唯一 InboundMediaJob。事务失败整体回滚；已有 completed admission / 历史 Dispatch 不补排取件，旧 dead 原样保留。

`runtime.worker.run_worker` 在轮询进程内启动独立取件线程，独立 Session/HTTP 客户端，不占 Hermes 工作槽，不读取 Hermes API key。AI 离线不影响已准入附件收取。`wechat_poll_once` 单次命令负责持久化及排队；持续执行取件由常驻 worker 负责。配置关闭时保持旧准入行为，不会悄悄重处理旧附件。

支持图片 raw3、已识别的 raw49 file，以及上游已压成裸文件名的 raw49 app 常见后缀候选（pdf/doc/docx/xls/xlsx/ppt/pptx/zip/rar/7z/jpg/jpeg/png/txt/csv）。裸文件名只是探测条件：实际媒体类型、字节、来源前后绑定仍必须全部通过。普通链接、已识别 reply/forward 不进入长时间取件。无后缀和名单外的裸文件名目前无法可靠区分，尚不自动收取；不能据此宣称所有 app 类型都支持。

任务状态为 `pending → fetching → publishing → ready`；终态另有 `unsupported / failed / timed_out`。独立次数、next_attempt_at、deadline、180 秒租约与随机 claim token 持久化。pending 和可重试 HTTP 故障使用 2、4、8、16、32、60 秒上限退避，默认等待 900 秒、最多 32 次；正在执行的请求受 HTTP 总 deadline 限制，调度恢复后会检查过期，不承诺 worker 停机期间仍实时终结。凭据、绑定、格式和不支持错误不盲重试。等待不会增加 Hermes Dispatch attempt_count。

取件 worker 先提交来源指纹、摘要、大小和发布意图，再调用既有 Linux 私有暂存。发布后 Attachment 登记与 job ready 同事务提交。取件中崩溃由租约到期恢复；blob 已落盘而 metadata/数据库未完成时，按已存发布意图核验旧 blob、补 metadata、完成登记，无需上游再次返回字节。数据库事务失败不删除唯一文件。文件尚未发布则可重新取件。未知摘要/路径冲突、链接或权限异常不覆盖。

暂存新增验证读取，并恢复严格验证的 `.intake-<UUID>` 同 inode 孤儿硬链接，修复 link 成功/unlink 前崩溃窗口。未知或外部硬链接仍拒绝；根目录锁采用非阻塞方式，争用返回可重试 busy，避免永久持锁等待。

## FIFO、通知和模型交接

Dispatch 在排队时就占据线程位置。`claim_next` 与显式 `claim` 共用取件门禁；未完成媒体阻塞其后同线程消息，其他线程不受该门禁阻塞。终态失败允许一次本地失败通知进入原 Response/Delivery pipeline，再释放 FIFO，不调用 Hermes。通知 response_id 为 `inbound-media-notice:<job_id>`，可区分模型产物，重复执行不会重复登记通知。

**Dispatch 的 SUCCESS 表示该派发/通知持久化成功，不等于附件成功、模型已调用或正式归档。attempt_count 是派发领取次数，包含本地通知。** 判断附件须看 job.state；状态 API 的 dispatch_kind 为 blocked_on_media、hermes_request 或 local_failure_notice。不把派发数写成模型调用数。

只有 ready 且 Attachment 已登记，才构造 `cf-inbound-read/v1` 描述符：精确 message/attachment/identity/thread、URL、短期 Authorization header、大小、SHA-256、MIME、上游 quality 与 original_comparison。描述符交给现有非空字符串 Hermes API；V2 同时进入 session_metadata.inbound_attachments，V1 在正文中交付。普通文本保持原正文；空正文检查保留。没有 Base64 原件、CFserver 本地路径或长期 Token 进入提示词。

`GET /inbound-media/{job_id}/content` 仅接受该附件的随机短期 Bearer capability。库中只保存凭证哈希，绑定当前 RUNNING Dispatch claim，最长 3660 秒，并检查当前租约；派发结束或新 claim 后立即不可用。每次读取重新验证当前来源身份映射、identity/workspace/thread/profile 状态、V2 线程键、来源指纹、Attachment 登记和 blob 的大小及摘要。响应 no-store、nosniff、attachment；拒绝任意磁盘路径和其他附件令牌。不要把 capability 放进 URL、日志、截图或长期文档。

HTTP 错误分类在授权与登记核验之后生效：明确可重试的暂存锁忙/临时 I/O 不可用返回 `503`、`Retry-After: 1` 与 `Cache-Control: no-store`；不把暂时不可用伪装成权限失败。无效/过期凭据、身份或来源不匹配、文件损坏、登记冲突等仍统一 `403`，不暴露错误细节或内部路径；功能关闭为 `404`。成功读取磁盘后会再次核验授权和登记，再返回缓冲字节；这不能撤回已发出的网络字节，插件仍须先完成下载再结束 Dispatch。`Retry-After` 仅是短暂退避提示，不能授权无限重试。描述符的 `download_policy` 声明最多 4 次尝试、单调时钟总计 30 秒，只自动重试 `503`；由下载适配工具强制执行，Gateway 目前不把该字段作为请求配额。网络请求、流式读取、落盘、校验和退避均计入期限，且不能超过凭据有效期。`403`、`404` 或大小/摘要校验失败立即停止。具体工具契约见[授权工作副本下载契约](inbound-media-download-contract.md)。

`GET /inbound-media/{job_id}` 和按消息定位的 `GET /messages/{message_id}/inbound-media` 使用既有 Gateway API token，提供任务状态、期限、次数和静态错误码；不返回 capability、文件正文或磁盘路径。该入口面向受信服务/管理调用，不把全局 API token 发给微信用户。状态改变有结构化日志，实际用户通知仍由原 Delivery 工作者处理。

Hermes 侧的最小契约是使用描述符 URL 和 Authorization header，在 AI 主机下载工作副本并核验大小/摘要。本仓真实回环测试以模拟 Hermes HTTP 服务取得字节并在内存核验大小/摘要；**这不等于真实 Hermes 已有可保存工作副本的下载工具，也没有验证真实 AI 主机落盘或其网络/TLS 配置**。收到描述符或知道文件名均不能记为下载成功。若现有 Agent profile 缺此能力，须由对应的 FileBrowser 独立项目在 Hermes 用户插件适配层实现[最小下载接口](inbound-media-download-contract.md)；本仓只提供 Gateway 接口、契约和隔离测试，不访问或修改其他项目的本地目录，也不改生产配置。下载后处理结果必须通过 FileBrowser API 另存，默认保留原件。

暂存/Attachment 是受控入站缓存，不是正式 FileBrowser 归档，也不是出站 READY Artifact。检索、分类、正式归档和加工逻辑仍属于 Hermes/文件服务；本轮不实现第二套文件服务、正文修改、删除、重命名或新建文件夹。需要回传时沿用已存在的 READY Artifact / Delivery 边界。`not_checked` 或 `different` 的 JPEG 不得称为原图，即使上游声明 full。

## 数据库与配置

Alembic `20260927_01` 只新增 inbound_media_jobs 及约束/索引，不回填历史数据。正常 migrate/check 启动路径和 PostgreSQL schema head 一起升级；没有另走 initialize 绕过生产迁移。存在任务时拒绝破坏性 downgrade。

仓库配置模板默认关闭，须在另行批准的验收环境配置：

```yaml
inbound_media:
  enabled: true
  staging_root: /var/lib/cf-agent-gateway/inbound-media
  public_base_url: https://gateway.example.test
  wait_seconds: 900
```

public_base_url 必须是 AI 主机可到达的 Gateway HTTP(S) origin，外部连接使用验证过的 TLS，不跳过 CA 校验。Poll、Dispatch 和 Gateway 应使用同一份配置及同一持久暂存目录。现有 Compose 的 gateway-state 共享卷可承载该目录。新镜像为新卷准备 UID/GID 10001、0700 目录；**已有挂载卷不会因镜像 mkdir 自动升级**，须在验收准备步骤预置对应目录并验证 ownership/mode。worker 不自行 chmod 操作者目录。已有队列未终结时不要用关闭功能掩盖问题。

### 功能启用门禁：真实 Hermes 下载

**生产功能保持默认关闭，以下条件必须在另行批准的验收环境取得证据后，才可考虑正式启用。** 本轮 Gateway CI、模拟 Hermes GET 和描述符传递通过，均不能代替这些证据，也不构成部署授权。

- 从实际 AI 主机、实际 Hermes 用户插件运行身份验证 Gateway HTTPS 地址可达，证书链及主机名校验成功；不以关闭证书校验或仅在 Gateway 本机可达代替。
- 真实插件使用 `Authorization` header，下载到应用授权的任务私有工作目录，核对字节数和 SHA-256 后才返回工作副本成功结果。证据包含受控工作副本的摘要和落盘结果；聊天与普通日志不回显凭据或内部路径。
- 下载在当前 Dispatch 有效期间完成。实测任务结束、租约/凭据过期及新 claim 后原凭据不可复用；`expires_at` 只是最长上限，派发结束可更早撤销。
- 实测锁忙为 `503`、按有限策略退避后成功；持续不可用达到 4 次或 30 秒上限时停止，不能靠模型自行反复调用绕过限额。无权、登记冲突或损坏文件保持拒绝。
- 对该工作副本的后续加工经 FileBrowser API 另存，默认保留原件；入站暂存、下载成功与正式归档分别留证。JPEG 的下载摘要一致不等于它已被证明为原图。

当前尚无本轮真实 Hermes 下载落盘证据，该门禁未通过。PDF 长期 pending 仍是独立上游阻塞；图片下载门禁通过或模拟 PDF ready 均不能据此关闭该阻塞。

## PDF 长期 pending 的独立调查

现场 Image ID/RepoDigest 见[第一步](inbound-media-stage1.md)，仍没有可验证的应用 revision 映射。CF_agent-wechat 固定部署提交 [67cbbb04](https://github.com/Tangbohu09527/CF_agent-wechat/blob/67cbbb04ce15703428ce165ac38effac19f4b701/docs/api.md) 仅管理部署/生命周期，媒体接口是历史观察契约，不能从它推导现场原生下载能力。

独立审查公开上游固定提交 [96c2e90c](https://github.com/thisnick/agent-wechat/tree/96c2e90c497d999bf5189a134a9b3b293731f42e)：

- [消息清洗](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/packages/agent-server-rust/src/tools/wechat_messages.rs) 可将 raw49 XML 压为 title，丢失 file subtype，解释为何不能仅筛 normalized FILE。
- [媒体路由](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/packages/agent-server-rust/src/router/messages.rs) 包含查缓存、metadata、queue 和有限等待；[原生队列引入提交](https://github.com/thisnick/agent-wechat/commit/641367c3e586ea7529d7d9d23f3698a673bcfdef) 之前可只有缓存查询。pending 仍不证明 queue 成功。
- [原生 helper](https://github.com/thisnick/agent-wechat/blob/96c2e90c497d999bf5189a134a9b3b293731f42e/docker/tools/media-download.py) 要求匹配的完整 WeChat Build ID；metadata、版本支持、身份、helper 和缓存校验均可能阻塞。公开源码不是现场镜像能力证明。

不能据此宣称已定位现场唯一根因或 PDF 已可下载。若需要上游升级，先选择不可变候选镜像，建立源码/制品映射，在隔离环境验证版本、Build ID、helper、未缓存 PDF/图片、重复 GET、身份切换及源样本摘要，再另行走固定制品和回滚流程；禁止盲拉 latest。现有用户样本和历史任务保持不动，不要求重发已确认样本证明已知故障。

## 测试及汇总实机验收流程

新增 portable 队列测试验证持久化/幂等/FIFO/身份/失败通知/迁移；Linux 集成测试用真实回环 WeChat HTTP → 临时私有磁盘 → Gateway 授权 HTTP → 模拟 Hermes HTTP 读取实际字节。它证明接线，不证明现场下载、模型理解、FileBrowser 入库或真实微信投递。完整测试、Ruff、迁移、PostgreSQL、V2、Compose、clean-device 必须以**精确提交**的结果为准，不能沿用父提交绿灯；本地 Windows 的 Linux 语义失败和无 Docker/WSL 能力须另列。

以下流程供后续另行批准的实机验收统一执行，本轮未执行：

1. 固定最终 Gateway commit/image，保存配置/数据库备份及回滚材料；核验微信镜像来源，不修改历史 Checkpoint、dead 或会话。确认新 schema、共享 0700 暂存目录；从 AI 主机的实际插件运行身份确认 Gateway HTTPS 可达及证书链/主机名验证。逐项准备上述启用门禁，正式功能保持关闭，隔离验收实例按授权启用。
2. 在限定验收账号和预授权资料群确认身份及路由。记录新验收消息 ID；既有故障事实不重新取证。验收素材使用经批准的合成/非敏感样本，与历史样本分开。
3. 连续文字、空正文图片、PDF、图片、文字；逐条核对消息、准入、job、Dispatch。pending 不触发模型，同线程不越序，其他线程可继续。
4. 在隔离媒体服务验证 pending→ready、始终 pending→超时、unsupported、403、账号切换。现场 PDF 若继续 pending，应记录独立阻塞，不以模拟通过替代现场证据。
5. 在独立验收实例分别中断 fetching、blob 发布后、metadata/数据库提交前，再重启；检查恢复后附件仅一条、摘要一致、不覆盖原件、不重复通知。不要以生产断电来执行故障注入。
6. AI 离线时确认取件仍继续；AI 恢复后核对 FIFO 和授权下载。在隔离实例持有暂存锁，核验有效授权收到 `503`/`Retry-After: 1`，释放后成功；并发读取/发布不产生半文件，持续锁忙按次数/总期限停止。验证无 token、其他附件 token、过期/已完成 claim、身份停用/重新映射、登记冲突和篡改 blob 都被拒绝，错误不泄漏路径或凭据。
7. 使用真实 Hermes 插件在当前有效 Dispatch 中下载到授权任务工作目录，实际检查文件存在、大小及 SHA-256；仅收到描述符/文件名不得登记成功。安全保存任务绑定与校验结果，聊天/普通日志不回显凭据和内部路径；任务结束后再用旧凭据读取必须失败。对原始字节有可信外部期望值的样本另行比对，未知或预览保留 not_checked/different，不视作原图。
8. 验证指定资料群分类通过 FileBrowser API 正式入库；加工结果另存且原件保留。按既有 READY Artifact/Delivery 回传实际微信附件，核对文件服务记录、微信回执和收端大小/摘要，文字成功不算附件成功。
9. 检查失败通知与管理状态、迁移前后历史 dead/attempt/idempotency 不变。归档验收证据并逐项标明已验、未验、阻塞；未完成项不关闭 Issue #13，不合并或发布。
