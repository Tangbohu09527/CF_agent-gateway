# 2026-10-07 当前任务附件返回：Gateway 隔离联合验证

本轮在 `feat/wechat-inbound-media` 上承接
`3deaae19eabfebeb029d2ff5246171740a4599ae`。结果是可供可信 Hermes 宿主接入的
Gateway 字节交接和附件投递链路，默认关闭。**尚未完成现用 Hermes 宿主适配，
也未向真实微信发送文件或图片。**

定版接口为 [cf-artifact-return/v1](../development/artifact-return-contract.md)。
此前 [微信文件 API 文字返回收口](2026-10-07-wechat-file-api-text-closeout.md)
保持原结论，本轮没有重复下载、解析、视觉或文字投递业务验收。

## 实现与实际运行的链路

复用 `WechatHttpMediaSender.send_media`、Delivery 的 `artifact_ref` 分支、
ArtifactRepository 的保存及 READY/完整性检查，以及 Response/Delivery 原聊天
绑定、逐段回执和恢复。新增的是原来缺少的跨机字节交接与成功结果封口：

1. DispatchService 从当前已准入 Message、身份、线程、Profile 和真实 RUNNING
   claim 派生短期能力，只通过 HermesClient 后台 HTTP headers 交接。
2. 可信宿主明确执行“返回当前聊天”时，以原始字节 PUT 到 Gateway 当前任务的
   稳定 slot；验证长度、SHA-256、文件名、PDF/PNG 标识及任务归属后登记 READY。
3. 上传和 Dispatch 完成使用同一个数据库 claim fence。完成时把本 claim 的
   READY 产物封入持久化结果，第一次投递与崩溃恢复使用同一结果；已有结构化
   响应保留文字和图片的交错顺序。
4. 原有 Response/Delivery 使用原 Message 的账号和私聊／群聊目标；PDF 走文件
   消息，PNG 走图片消息。投递失败不重新调用模型，结果不确定保留 uncertain。

没有上传时保留原有文字行为。Gateway 不读取 Windows 路径、不拉取任意 URL，
上传者不能指定 chatId、response ID 或借用别的任务的 Artifact。
文件发布后、数据库提交前失败可能留下没有 READY 行的私有孤立内容；重试不能
将其冒充成功，不在本轮清理未知残留。投递暂存不等于 FileBrowser 正式归档。

实现提交：

- `e021c4deabbf83e4bb1f7cf779a6e385209ffdf0`：Artifact 支持由当前 claim 的外层
  事务负责提交，保留默认调用行为及原有恢复语义。
- `666cd089bec2b19a7f3456e23c44fc0529d60c1a`：认证上传/查询、运行时接线、成功
  结果封口、宿主响应 ACK 门禁及 HTTP/联合回归。

## 测试记录

沿用仓库 pytest、HTTP 服务和接收端测试框架，未新增执行器或安装依赖。
本机为 Windows、现有 Python 3.12；缺少的 cryptography 从既有 Codex runtime
依赖路径导入，不修改安装环境。

| 实际执行 | 结果 | 范围 |
| --- | --- | --- |
| Artifact repository 测试 | 51 passed、2 skipped | 外层事务、回滚、文件/数据库部分完成、已有存储行为；跳过为 Windows symlink 权限。 |
| 既有选定回归 | 169 passed | Client、Dispatch、结果恢复、Response、配置、身份/host/inbound queue。 |
| 最终针对性组 | 103 passed | 52 HTTP、10 冷导入、16 联合、25 HermesClient（包括新增 ACK 门禁）。 |
| 最后来源快照字段补充后的 HTTP 与联合复核 | 68 passed | 与上述 52+16 重叠，不累加为独立用例。 |
| Windows 全仓 | 1858 passed、21 failed、98 skipped | 1977 个用例；执行时尚未加入最后两项 ACK 拒绝回归。不是全仓通过。 |
| 原基线的 21 个失败节点复核 | 21/21 同因失败 | 隔离 `git archive` 导出基线，当前分支、源码、依赖及跳过规则不变。 |

Windows 全仓的失败原因：16 项 POSIX 暂存目录检查
`invalid_media_staging_root`，1 项 symlink `WinError 1314`，3 项测试子进程缺少
cryptography，1 项子进程缺少包路径。这些原因全部在原基线复现；未删除或关闭
测试门禁。最终新增的两项 ACK 拒绝回归已在上述 103 项中执行，最终收集 1979 项。

最终 SHA 的 Linux 全仓、迁移、容器和 clean-device 结果以对应 GitHub Actions
运行及 PR checks 为准；本地没有 Docker，不能将本机检查写成容器验收。本轮不改
CI 工作流，也不因测试平台差异修改产品权限边界。

私有运行记录保存在本工作树 `.task-artifacts/`，不提交原始私有证据：

- `ar-targeted-20261007-final.log`：103 项针对性结果。
- `ar-seal-final-20261007.log`：最后 68 项复核。
- `ar-full-20261007-01.log`：Windows 全仓结果。
- `ba-521db631/baseline.log`、`comparison.json`、`reason-comparison-v2.json`、
  `manifest.json`：基线归因及文件摘要；使用修正了日志分段分类的 v2 报告，保留 v1。

## 真实 HTTP 与状态机证据

16 项联合测试实际经过：HermesClient HTTP 请求 → 协议宿主测试替身 → 真实
Gateway uvicorn HTTP PUT → 真实文件系统和隔离 SQLite Artifact → 真实
Response/Delivery → WechatHttpMediaSender → HTTP 微信接收端测试替身。
宿主不调用模型；接收端不连接真实微信。

覆盖 PDF 私聊、PNG 群聊、不同来源账号和纯文字；同 slot 幂等及结束后拒绝；
HTTP 200 但 `hermes.failed=true` 不发送；明确 429 后恢复及三次预算耗尽；发送
500/超时进入 uncertain，后续不盲发、不重调模型；结果成功后 Response 写入失败
由新 worker 从持久结果恢复；CREATED、损坏或跨任务引用不能进入成功回复。

上传/完成竞争测试持有真实数据库写锁。工作线程首先在既有 host-close 屏障被
该事务阻塞，释放后才能进入完成封口；首个 Response 即包含已上传附件。此证据
证明该实际执行顺序，不将它描述为绕过前置屏障直接测试另一种调度顺序。

HTTP 回归还覆盖无效授权、错误 Profile、身份/来源/claim 变化、旧任务、过期、
重启不刷新预算、并发和重放、元数据/文件名/路径/类型/摘要/限长拒绝、完整性
错误失败关闭、暂时存储故障 503 及有限重试提示、查询后重新校验期限。
冷导入检查在新 Python 进程验证 10 个入口，防止模块循环依赖仅被测试导入顺序掩盖。

## 两份已批准样本的隔离字节传递

只复制用户指定的两份已存在本地文件到隔离目录，各执行一次上述出站字节链路。
原件保持不变，没有重新访问 FileBrowser、下载、解析、调用模型或视觉工具。
每项为 1 次协议宿主 POST、1 次上传 PUT，Dispatch 与 Delivery 各 1 次尝试，
2 个响应段和 2 个回执（测试文字段与附件段）。两项使用独立 SQLite 数据库。

| 样本 | 大小 | SHA-256 | 隔离接收端 |
| --- | ---: | --- | --- |
| `CF-NATIVE-PDF-20261006-A1.pdf` | 79083 | `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44` | `file`，原文件名，私聊来源账号及目标。 |
| `CF-NATIVE-IMAGE-20261006-B1.png` | 47740 | `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` | `image/png`，另一来源账号及群聊目标。 |

原文件、隔离副本、上传回执、READY Artifact 和接收端解码字节的大小/摘要一致。
证据根目录为 `.task-artifacts/outbound-samples-20261007-6f82c1/`；包括
`report.json` 与 `post-run-scope-note.json`，15 个私有文件已冻结并独立核对。
证据清单文件的 SHA-256：
`adce686a1136c53cc42af18c6bd2e9578cec54c4257b15ec3e1f73931dc2a9e5`。

**版本限制：这两项单次样本传递在新增 Hermes 响应 ACK 门禁之前已经完成。**
原记录保留 `new_response_ack_gate_exercised=false`，不重新执行样本、不把旧记录
改成最终 ACK 验收。最终 ACK 成功/缺失/错误摘要由更新后的合成 HTTP/联合测试
覆盖。以上真实样本证据证明隔离字节传递，不能证明现用 Hermes 适配或微信实收；
PNG 在真实微信链路是否压缩/转码尚未观测。

## 尚待现场接入与验收

Gateway 部分已接入既有运行链路；真实宿主仍需实现契约中的最小可信 request
adapter/任务内返回工具。官方 Hermes 不会自动消费新增 headers，缺少正确
`X-CF-Artifact-Return-Accepted` 时即使 HTTP 200 也失败关闭。ACK 只是协议确认，
不能代替真实上传、任务作用域及凭据隔离实证。

现场最少前置操作见 [契约步骤](../development/artifact-return-contract.md)：
可信宿主适配；Gateway↔Hermes 两方向可达且验证 TLS（现有 LAN 明文 HTTP 不可
承载后台 capability）；专用签名秘密引用；Gateway API/Delivery 可用的私有
Artifact 持久目录；批准 Profile/修订及默认关闭功能的最小配置；另行批准部署
和各一条私聊/群聊真实发送。此功能复用现有表，不新增数据库迁移。

届时 PDF 必须在原聊天以文件消息实收、文件名正确、可下载打开并核对接收文件
摘要；PNG 必须以图片消息实收且内容正确，若压缩/转码须保留接收端差异证据。
同时保留普通读取任务只返回文字的行为。当前不能宣称上述现场标准已通过。

本轮未改现场配置、凭据、Profile、Skill 或服务，未部署、扫码、发微信、改真实
数据库或处理旧任务。PDF -01 X-Auth 失败、PDF 工具约束偏差、PDF -02 修改
`ocr-and-documents` 以及 PNG 使用修改后说明的历史全部保留。微信入站 PDF
长期 pending 仍是独立上游阻塞，本出站能力不关闭该问题。
