# Hermes 入站附件工作副本下载契约

本契约面向对应 FileBrowser 独立项目后续实现的 Hermes 用户插件下载适配。本仓实现 Gateway 下载接口和隔离测试，**不包含真实 Hermes 下载工具实现或其落盘验收**。模拟 Hermes HTTP 服务取得字节不证明真实插件可下载、保存或使用附件。功能启用条件见[第三步说明](inbound-media-stage3.md#功能启用门禁真实-hermes-下载)。

## 最小工具接口

下载器内部接口可为 `download_inbound_attachment(descriptor, task_context)`；模型侧只传附件 ID，不能提交 descriptor 或 Authorization。完整描述符必须通过 [cf-inbound-host-binding/v1 认证后台交接](inbound-host-binding-contract.md)进入宿主。现有 HostBridge 与下载器复用本接口，不再实现第二套下载器。工具是可信插件操作，不把下载义务交给模型自由生成 shell 命令。

`descriptor` 为 Gateway 发出的 `cf-inbound-read/v1`：

| 字段 | 用途 |
| --- | --- |
| `schema` | 仅接受已支持的契约版本。 |
| `message_id`、`attachment_id`、`thread_id`、`enterprise_identity_id` | 与可信任务上下文绑定，不能跨身份或任务使用。 |
| `url` | Gateway 内容 GET URL；必须属于预先授权的 Gateway HTTPS origin。 |
| `authorization` | 完整 `Bearer …` 值，只放入 HTTP `Authorization` header；不得拼到 URL。 |
| `expires_at` | 凭据最长有效期；当前 Dispatch 结束、租约失效或换 claim 可更早撤销。 |
| `size`、`sha256`、`mime_type`、`filename` | 内容校验及显示元数据；文件名不能作为任意本地路径。 |
| `declared_quality`、`original_comparison` | 保留来源质量声明和比对结论，下载成功不提升为“原图”。 |
| `formal_archive` | 此时为 `false`，不代表已通过 FileBrowser 正式归档。 |
| `download_policy` | 最大尝试数、总期限和允许重试的 HTTP 状态，详见下节。 |

`task_context` 由可信应用提供，至少包含当前任务/Dispatch 标识、身份与线程、授权工作目录、允许的 Gateway origin，以及任务取消/结束信号。模型输出不能扩大这些权限。Gateway 当前描述符不提供可自行续期或刷新凭据的接口；插件不得自行创建新 claim 来绕过撤销或下载次数限制。

成功结果至少包含任务绑定、受控的本地工作副本句柄、`bytes_written`、`sha256`、`verified: true` 和 `formal_archive: false`。只有完整文件已安全落盘且校验通过才可返回成功。文件名、描述符、HTTP 200 或部分落盘均不是成功结果。内部路径仅在可信工具上下文使用，对聊天或普通日志输出时使用不含路径的句柄与摘要。

失败结果使用固定分类，例如 `not_authorized`、`temporarily_unavailable`、`deadline_exceeded`、`integrity_failed`、`task_cancelled`；不返回 capability、内部路径、原始请求头或响应堆栈。

## 网络、授权和有限重试

实际 AI 主机必须能访问配置的 Gateway HTTPS origin，且插件运行身份能验证证书链和主机名；不使用 `verify=False`、忽略 CA 错误或明文外网回退。开发回环 HTTP 测试只是隔离测试例外。不得将 `Authorization` 发送到跨源重定向目标；最小实现可直接拒绝所有重定向。

Gateway 描述符给出的策略为：

```json
{
  "download_policy": {
    "max_attempts": 4,
    "total_timeout_seconds": 30,
    "retryable_status_codes": [503],
    "retry_after_seconds": 1
  }
}
```

工具强制最多 4 次 HTTP 尝试，**包括首次请求**，并以单调时钟强制最多 30 秒总期限；连接、TLS、响应读取、落盘、摘要校验与退避时间均消耗同一预算。开始时将 `expires_at` 剩余时间转换为单调时钟预算；每次请求的总超时不得超过剩余总期限和凭据剩余有效期，连接/读取子超时也不得超出它。任务结束或取消立即停止，不能启动新的重试；正常下载必须在本次 Dispatch 有效期间完成。插件须限制工具重复调用共享的同一任务/附件下载预算，不能每次模型调用都重新获得 4 次/30 秒。此策略是下载工具的强制实现要求；Gateway 目前不以描述符字段实施服务器请求配额，模拟下载端的有界测试不等于真实插件已实现。

| 响应/故障 | Gateway 行为或插件要求 |
| --- | --- |
| 暂存锁忙或明确可重试的暂存 I/O 暂时不可用 | Gateway 在授权/登记通过后返回通用 `503`，附 `Retry-After: 1`、`Cache-Control: no-store`。插件按 1 秒退避提示重试，前提是尚有尝试数和完整时间预算。 |
| 无效/过期凭据、身份或来源不匹配、登记冲突、文件损坏 | Gateway 返回通用 `403`，不给内部原因；插件立即停止，不更换凭据或自行重派。 |
| 功能关闭/入口不存在 | `404`，立即停止；不能改用管理 API token。 |
| 大小或 SHA-256 不符、越过大小上限、TLS 校验失败、重定向、其他非 `503` 错误 | 不自动重试，不交付工作副本；返回固定失败分类。 |
| 尝试数、30 秒预算或凭据期限用尽；任务已结束 | 停止重试并报告失败，不无限等待、不请求刷新。 |

`Retry-After` 不会延长总期限、凭据或 Dispatch。未来收到大于剩余预算的合法提示时应停止，不能越界等待；非法提示不能扩大重试策略。收到 `403` 不应推断是具体哪一项身份、来源、文件或凭据错误。

HTTP `503` 和 `Retry-After` 的语义参见 [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-503-service-unavailable)。Gateway 在成功读取磁盘后重新校验授权，再开始发送响应；无法撤回已发出的字节，因此插件必须在结束 Dispatch 之前完成工作副本落盘验收。

## 工作副本落盘与后续归档

工作目录由任务授权，Linux 下应为该运行身份私有目录（0700），临时及成品文件为私有文件（0600）；其他系统使用等效 ACL。插件验证解析后的目标仍在该目录中，不跟随可越界的符号链接，不使用来源文件名中的目录分隔符，不覆盖已存在的原件或其他任务文件。

在授权目录内写入唯一临时文件，限制实际读取字节数，边写边计算 SHA-256；仅完整读取且与 `size` 和 `sha256` 一致后，才原子发布为本任务工作副本。失败仅处置本次工具拥有的临时文件，不删除源附件或未知文件。并发重复调用应复用同一任务/附件的已校验结果或共享一次下载及其预算，不能让多个调用各自无限重试。

下载工具不得回显 `Authorization`，也不得将描述符原样写入工具日志、HTTP trace、异常或模型可见调试信息。落盘路径只交给被授权的后续处理工具，不发送到聊天。下载摘要一致只证明与 Gateway 提供的字节一致；`original_comparison: not_checked/different` 仍不能写成原图。

工作副本用于 AI 主机处理。正式分类归档、加工结果另存通过 FileBrowser API 完成，默认保留原件；下载工具不借此增加正文修改、删除或文件夹管理能力。出站继续遵守 READY Artifact / Delivery 边界。

## 启用前证据

由对应项目在另行批准的验收环境记录以下结果，证据不得含真实凭据：

1. 实际 AI 主机及插件运行身份的 HTTPS 连通与 TLS 验证结果。
2. 真实插件在有效 Dispatch 内，持授权 header 下载到任务目录后的存在性、字节数和 SHA-256；证明不是仅收到文件名或描述符。
3. 锁忙 `503` 后有限重试成功；持续 `503` 达到次数/总期限即停止；`403`、`404`、损坏、跨源重定向不自动重试。
4. 任务结束、过期或换 claim 后原 capability 再次读取失败；取消不会继续下载。
5. 工作目录隔离、重复调用共享预算、失败不交付半文件、日志无凭据/路径泄漏，以及 FileBrowser 另存保留原件。

这些证据未取得前，真实 Hermes 下载启用门禁未通过。PDF 长期 pending 的上游阻塞仍独立保留，不能用模拟 ready、图片下载成功或 Gateway CI 通过替代现场 PDF 字节证据。
