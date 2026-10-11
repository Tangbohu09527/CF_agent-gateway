# 当前任务的文件／图片返回契约

契约版本：`cf-artifact-return/v1`。

本接口将本次 Hermes 任务明确选定的文件字节交给 Gateway，再复用 READY Artifact、
Response、Delivery 和微信媒体发送适配器返回发起任务的原聊天。它不把电脑路径、
Markdown 图片链接或图片分析文字解释为附件，也不改变普通读取任务的文字返回行为。
功能默认关闭。本轮开发和隔离测试不能代替现场微信文件／图片实收验收。

官方 Hermes 的实际插件入口、任务作用域与两向 TLS 接线已在本仓提供，详见
[官方宿主接入](hermes-artifact-return.md)和
[统一入口启用／回退](hermes-artifact-return-enablement.md)。
这些代码与隔离验证不表示现用 Hermes 或 Gateway 已部署。

## 已有能力和补齐的边界

已有 `WechatHttpMediaSender.send_media`、`ChannelDeliveryWorker` 的 `artifact_ref`
分支、`ArtifactRepository` 字节保存及 READY/完整性检查，以及 Response/Delivery 的
原聊天目标绑定、逐段回执和恢复逻辑。结构化 Hermes 响应已经能携带 `artifact_ref`，
但引用本身不会把 AI 主机上的文件内容送到 Gateway。

本次增加的边界是：真实运行中 Dispatch claim 的后台产物授权、受认证 HTTP 字节
交接，以及在同一 claim 成功封口时把已就绪产物纳入持久化响应。Gateway 不打开
Windows `C:\` 路径，不抓取模型生成的任意 URL，不接受模型指定的账号或 chatId。
没有新增文件下载器、独立服务、EXE、安装器或正式归档系统。

```text
FileBrowser 现有受限 API（若文件已在库中）
  → AI 主机授权任务私有目录中的工作副本
  → 可信宿主中的明确“返回当前聊天”操作
  → 经验证 TLS 的 Gateway HTTP PUT（原始字节）
  → 本次 claim 的 READY Artifact
  → Dispatch 成功封口并持久化 Response
  → 现有 Delivery 分段发送与回执
  → 任务来源账号中的原私聊／原群聊
```

若文件只存在于 AI 主机，字节仍通过上述 HTTPS PUT 交付；不能用 SSH/SCP、AI 与
Gateway 共享正式存储目录、临时公开链接或关闭证书校验替代。原件保持不变。
Gateway Artifact 是投递暂存，**不等于 FileBrowser 正式归档**；正式入库仍走
FileBrowser API，默认另存并保留原件。文件正文不进入模型上下文，包括 Base64 正文。

## Hermes 宿主启用门禁

**普通官方 Hermes HTTP 服务不会因为收到新 header 就自动具备上传工具。**
本接口要求现有可信宿主的 request adapter 能在收到本次 Gateway 请求时，提取后台
授权，并只在该请求对应的真实执行和工具作用域中提供它。没有完成此适配并验证前，
现场不可启用；仅把 `host_contract_confirmed` 改为 true 不能证明兼容。

Gateway 的请求交接使用：

| 后台请求 header | 含义 |
| --- | --- |
| `X-CF-Artifact-Return-URL` | 当前 Dispatch 的 Gateway 上传入口前缀。 |
| `X-CF-Artifact-Return-Authorization` | 此 claim 的短期 Bearer capability。 |

宿主必须验证这些字段来自已认证的 Gateway 请求；不能允许模型工具参数、任意
外来请求或旧会话历史覆盖它们。现有 Hermes API 认证和经过验证的 HTTPS 传输仍然
必需。Gateway 签名密钥只由 Gateway 持有；Hermes 不取得签名密钥、微信发送 Token、
Gateway 管理 Token 或 FileBrowser 管理密码。

完成响应还必须带 `X-CF-Artifact-Return-Accepted`，值为本次完整
`X-CF-Artifact-Return-Authorization` 字符串 UTF-8 字节的 SHA-256（64 位小写
十六进制）。Gateway 检查它是否属于本次交接；缺失或不匹配会失败关闭，HTTP 200
也不会生成成功 Response/Delivery。该回执只确认宿主支持后台交接，不证明已经
上传文件，更不证明微信实收。不能仅添加回执而没有实现可信的任务内工具作用域。

最小宿主接口是“把当前任务工作副本作为 file/image 返回当前聊天”。工具可以接收
任务内受控文件引用、类型和文件名；可信实现负责确保文件在授权工作目录、读取
真实字节、计算大小及 SHA-256、选择本次任务的稳定 slot，然后调用本接口。工具
结果只返回非敏感成功标识、产物 ID、大小和摘要；后台授权和内部交接数据不得进入
提示词、模型可见工具结果、会话历史、普通日志或命令行参数。

宿主不得仅依据模型给出的任意本地路径取文件，也不得把会话 ID 当作授权凭据。
作用域必须关联本次已认证请求；请求取消、结束或超时时停止使用并丢弃后台授权。
不能因同一 Hermes 会话后续还有任务就继续使用上一次 capability。

“返回当前聊天”是显式业务动作，说明文字和 Skill 不是强制权限边界。仅下载、
保存、阅读、分析的任务不调用该动作。不能把加载 Skill 当作任意上传授权，也
不能让模型自行通过 `skill_manage` 修改官方通用 Skill 来补接线。

## Gateway 任务授权与预算

上传入口前缀为 `/internal/hermes/returns/{dispatch_id}`。完整授权只作为 HTTP
`Authorization` header 发送；不要放在 URL、查询参数或 JSON 正文中。

授权由 Gateway 当前 RUNNING Dispatch 的真实 claim 派生，包含并校验当前来源
指纹、Message、企业身份、workspace、thread、Profile/revision 和预算。来源账号
及私聊／群聊目标来自原 Message 和已准入线程；上传方不能指定或改写投递目标。
身份停用、映射变化、线程变化、Profile 不符、claim 更换、lease 过期或任务结束均
拒绝使用原授权。配置变更也不能静默扩大已发授权。

| 配置 | 默认值 | 上限／约束 |
| --- | --- | --- |
| `enabled` | false | 只在完成宿主兼容性和现场前置检查后启用。 |
| `host_contract_confirmed` | false | 人工确认真实宿主支持，不能代替验证。 |
| `public_base_url` | 空 | HTTPS；仅隔离回环测试允许 HTTP。 |
| `profile_reference` | 空 | 必须明确批准的执行 Profile。 |
| `profile_revision` | 1 | 正整数，与当前 Profile 修订匹配。 |
| `signing_key_env` | `CF_GATEWAY_ARTIFACT_RETURN_KEY` | Gateway 专用随机秘密引用，至少 32 字符，不能复用其他认证秘密。 |
| `max_bytes` | 1,048,576 | 每个产物最多 1 MiB，且不能超过 Gateway 全局请求体限制。 |
| `max_artifacts` | 4 | 每任务最多 8 个，slot 为 `0..max_artifacts-1`。 |
| `ttl_seconds` | 900 | 最多 3,600 秒，从原 claim 的认领时刻计算。 |

TTL 不因重复交接、同 slot 重试或 Gateway 重启刷新。实际有效期同时受更短的
Dispatch lease 和 RUNNING 状态约束。持久化使用已有 Artifact 与 Dispatch 表，
不为此接口新建数据库迁移或改写历史任务。
完整 capability 不写入数据库；签名密钥由 Gateway API 与 Dispatch Worker 使用
同一专用秘密引用，Hermes 只持有当前请求的短期 capability。

## 字节上传和幂等

`PUT /internal/hermes/returns/{dispatch_id}/artifacts/{slot}` 上传原始字节，不使用
JSON/Base64、multipart、查询参数或路径字段。请求 header 为：

| Header | 要求 |
| --- | --- |
| `Authorization` | 本次后台交接的完整 `Bearer …`。 |
| `X-CF-Return-Intent` | 固定为 `current-chat`，明确要求返回当前聊天。 |
| `X-CF-Artifact-Kind` | PDF 为 `file`，PNG 为 `image`。 |
| `X-CF-Filename` | 文件名的规范 UTF-8 percent encoding，等价于 Python `quote(filename, safe="")`。中文可用；不接受目录、URL、控制字符、首尾空格或路径分隔符。 |
| `Content-Type` | PDF 为 `application/pdf`；PNG 为 `image/png`，不附加 charset。 |
| `Content-Length` | 实际非空正文的字节数，必须提供且准确。 |
| `X-CF-Content-SHA256` | 原始字节 SHA-256，64 位小写十六进制。 |

Gateway 限长并检查实际字节、摘要、类型及文件名后才能登记 READY。本版只接受
PDF 文件和 PNG 图片：PDF 检查 `%PDF-` 开头及末尾范围中的 `%%EOF`，PNG 检查
签名、IHDR 开头和 IEND 结尾，并核对 kind/MIME/扩展名。此处是格式标识检查，
不宣称完整解析、恶意内容检测或文件内容可信度保证。

新上传和一致重放都返回 HTTP 200、`Cache-Control: no-store`，JSON 回执为：

```json
{
  "artifact_id": "Gateway-derived UUID",
  "response_id": "Gateway-derived current-claim response ID",
  "status": "ready",
  "filename": "report.pdf",
  "kind": "file",
  "mime_type": "application/pdf",
  "size": 79083,
  "sha256": "64 lowercase hex characters"
}
```

上述示例只说明字段结构；示例 ID、摘要不是可用上传数据。回执不含授权、内部
存储路径或微信目标。宿主只向模型投影必要的非敏感结果。

Gateway 派生当前 claim 的 response ID 和每个 slot 的 Artifact ID。上传者不能
提交外部 Artifact ID 引用另一任务的文件。相同 slot 与完全一致的元数据、字节
可幂等返回同一产物；相同 slot 的内容或元数据改变返回冲突，不能覆盖已保存产物。
跨账号、跨聊天、跨任务或旧 claim 的引用不因知道 ID 就获得权限。

`GET /internal/hermes/returns/{dispatch_id}/artifacts/{slot}` 使用同一有效
Authorization 查询同一回执。它重新校验所属任务和文件完整性，只返回元数据，
不回传文件字节。PUT 超时／响应丢失时先查询原 slot；没有取得回执前不要结束
任务或换 slot。任务已结束时原授权不能查询或重新上传，改由受控运维核对既有
Dispatch/Artifact/Response，不重新调用模型或制造新任务。

| 状态 | 含义与处理 |
| --- | --- |
| 200 | 已验证 READY 回执；不等于微信投递成功。 |
| 403 | 授权、身份、来源、Profile、claim 或期限不可用，错误细节不外泄；不重试。 |
| 404 | 功能关闭，或有效 GET 的 slot 尚无产物；不能据此绕过权限或换任务。 |
| 409 | slot 冲突、产物状态不可用或完整性失败；不覆盖、不自动换 slot。 |
| 413 | 超过请求体或产物限制；不拆分绕过预算。 |
| 422 | slot、返回意图、文件名、元数据、摘要或类型不符；不自动重试。 |
| 503 | 明确存储/数据库暂时不可用，带 `Retry-After: 1`，可以有限重试。 |

上传成功只表示 Gateway 已接收并验证投递暂存。宿主必须等待上传完成再结束任务。
遇明确 503 或查询确认需要重传时，只能在原 claim 仍有效时用同一 slot、元数据
和字节有限重试；每次显式交接的 PUT/GET 合计最多 3 次、总计不超过 30 秒，并
始终服从更早的授权/lease 期限。这是可信宿主应执行的契约，不声称 Gateway 能
强制客户端等待策略；Gateway 通过 slot 数量、字节数、claim 和期限限制接收。

## 任务完成、发送和恢复

上传发布和 Dispatch 成功封口使用同一个真实 claim fence。只有本 claim 已经
验证为 READY 的显式返回产物才能加入最终 Response；完成事务封口后不接受迟到
上传。任务失败或结果不确定时，不因为存在暂存产物便自动发送它们。

现有普通 Hermes 文本 completion 可以与后台上传产物合成有序 Response；无需把
完整文件正文塞进 Hermes completion。结构化 artifact 引用同样必须属于当前任务
并与 Gateway 登记匹配。首次投递和重启恢复均读取同一份持久化的完成结果。

结构化响应的 `response_id` 必须等于上传回执的当前 claim response ID；其中
`artifact_ref` 的 ID 序列必须恰好包含全部已上传产物，按 slot 数字升序、各一次。
符合该约束时保留文字与附件之间原有的交错顺序；缺失、重复、乱序或外部引用均
失败关闭。普通 completion 则在文字段之后按 slot 升序追加本任务附件。

Delivery 从原 Message 取得 channel/account/conversation，逐段检查所属 response、
READY 和落盘完整性，PDF 走 file 消息并保留正确文件名，PNG 走 image 消息。普通
文本仍走 send_text。Dispatch 成功以后，Delivery 失败不会再次调用模型；部分
已确认投递从首个未确认段恢复。发送结果不确定保持 uncertain，不盲目自动重发。

字节先写入随机私有 storage key，READY 登记与 claim 检查在上传事务中提交。
文件发布后、数据库提交前崩溃可能留下未被 READY 行引用的私有孤立文件；同 slot
重试写新 key，不把孤立内容直接当成功。该存储需要受控保留/清理策略，本轮不扫描
或清理现场未知残留。成功后的产物仍按已有 Artifact 生命周期管理。

## 隔离联合测试与证据范围

原 Gateway 测试沿用现有框架：真实 Gateway HTTP、隔离数据库、真实 Artifact 文件及
Response/Delivery 状态机，与协议 Hermes 宿主及微信接收端测试替身联合运行。
测试应覆盖私聊和群聊源目标、PDF/PNG/文字、同 slot 重试及冲突、跨任务/账号/
聊天拒绝、身份和 claim 变化、过期、限长/摘要/类型/路径拒绝、完成与上传竞争、
文件/数据库部分失败、投递失败恢复、发送 uncertain 和结果恢复不重复模型调用。

测试宿主消费后台 header 并上传样本，不等于现用官方 Hermes 已加载可信适配；
测试微信接收端确认请求字节和状态机，不等于现场微信实收。验收结果和具体运行
证据由对应提交/CI 报告，不用已有读取报告或新增测试数量代替真实附件投递证据。

后续新增的 [固定官方探针](../../tests/hermes_return_probe/README.md)将 Hermes
宿主替换为真实官方 HTTP、插件加载器和 Agent 工具循环，经最终 ACK 门禁运行。
合成模型与获批真实模型分别记录；微信接收端仍是替身。旧 ACK 门禁之前的隔离
报告保持原结论，不追溯改写成新接线的证据。

## 后续现场所需的最少变更与操作

本轮不应用以下现场变更。批准进入现场验收后，应按顺序完成：

1. 通过统一入口规划并加载本仓已实现的官方 Hermes 接入模块，核验固定源码版本、
   实际工具作用域和配置保留。证明授权不进入模型、工具结果、历史或普通日志；
   不能只改 Gateway 开关、提示词或 Skill 便进入发送验收。
2. 确认 AI 主机能访问 Gateway HTTPS 入口，且 Gateway → Hermes 后台授权交接
   也使用经过验证的 HTTPS；两个方向均检查证书链和主机名，不经公开临时链接。
   现有 LAN 明文 Hermes HTTP 入口不能直接承载本授权。按既有受控方式配置
   Gateway 专用签名密钥引用，不交给 Hermes。
3. 审查 Gateway API 和现有投递 Worker 对同一 Gateway 私有 Artifact 根目录的
   访问。这是 Gateway 部署内部的持久存储，不能把它共享给 AI 主机作为传输通道。
4. 审查匹配宿主能力的 `artifact_return` 最小配置差异：URL、批准 Profile/修订、
   预算和启用确认。此功能复用现有表；不因验收资产或文档更新而重建部署，不修改
   `legacy_runtime_confirmed`、现场 `ocr-and-documents`、Checkpoint 或历史任务。
5. 单独批准部署必要代码/配置以及发往两个明确测试来源聊天的真实发送。各任务
   保留新编号和来源链，记录上传摘要、Artifact、Response、Delivery、逐段回执及
   接收端结果。不确定时先核查原任务，不换任务反复发送挑选成功结果。

现场通过标准必须分别满足：

- PDF 在发起任务的原聊天以**文件消息**实收，文件名正确，可下载、可打开；核对
  接收文件的大小和 SHA-256，区分上传暂存摘要与接收文件摘要。
- PNG 在发起任务的原聊天以**图片消息**实收，内容正确。若微信链路压缩或转码，
  保存接收端证据并记录差异；未经接收端核对，不宣称原始字节完全一致。
- 普通读取／分析仍只返回文字，不自动发送附件；私聊与群聊目标都由当次原任务
  绑定，不能固定到某个测试聊天。

## 保留的历史与独立边界

[2026-10-07 文字返回收口](../validation/2026-10-07-wechat-file-api-text-closeout.md)
只证明微信文字指令驱动 FileBrowser 读取及文字结果返回，不能当成本接口附件
投递已通过。PDF -01 的 X-Auth 失败、此前 PDF 工具约束偏差、PDF -02 对
`ocr-and-documents` 的修改及 PNG 使用修改后说明的事实全部保留；不能继续称其
为未经修改的官方固定 Skill。本轮不加载、修改或恢复这份现场 Skill。

微信入站 PDF 长期 pending、上游取件及原图状态仍是另一条链路；不因出站接口
完成而关闭。半升级插件和未知残留保持原位。本轮不部署、不启停服务、不扫码、
不向真实微信发送消息、不改真实数据库、凭据或配置，也不发布安装包。
