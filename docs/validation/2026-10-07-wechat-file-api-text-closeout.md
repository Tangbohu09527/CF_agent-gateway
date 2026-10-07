# 2026-10-07：微信文字指令读取文件 API 并回传文字结果

本次从 `7bf0c463474ec883bd232a3516e700b5080f215c` 继续原工作树与
`feat/wechat-inbound-media`，仅归并已有现场证据。两个指定文件及同次 Hermes 轮次
已经与操作者提供的服务器记录关联，可以收口这两条具体业务链路：

**微信文字指令 → 文件 API → AI 主机保存副本 → 原生读取 → 文字结果返回原聊天。**

这不是微信附件取件或附件投递验收。没有重跑任务、下载文件、调用模型、发送微信、
修改配置或启停服务。另发现 PDF -02 历史轮次附带修改了 Skill，单独记录如下；
业务链路完成不等于所有工具均只读、旧验收约束全部通过。

## 证据来源与关联方式

| 来源 | 本次取得方式与证明范围 |
| --- | --- |
| CFserver 数据库 | **操作者**于 2026-10-07 01:36:26 UTC（北京时间 09:36:26）在现用 Gateway 容器内执行只读查询，随后由用户提供结果；不是 Codex 连接服务器取得 |
| 微信收件 | 操作者确认已在发起任务的原聊天实际收到两条文字回复；Codex 本轮没有发送微信或另取聊天截图 |
| Windows 文件 | Codex 只读检查用户指定的两个确定路径，核对真实文件大小、SHA-256，未重新下载或改写文件 |
| Windows 工具与回答 | 从本机 Hermes 既有 SQLite 记录以 `mode=ro`、`PRAGMA query_only` 和只读事务提取；按精确测试编号找到唯一 user 行，以该会话下一条 user 行界定轮次，不加载 Hermes 应用或触发模型 |

原 Gateway 会话为 `v1:cf-agent-gateway:bc8e2256-1aee-4c87-b134-29b42465b546`，
持久记录的 source 为 `api_server`。这是已有连续会话，不是此前库级或直连 HTTP
验收的新会话。会话级 model 字段不作为当轮模型供应商路由的独立证明。

| 测试 | Windows 会话行范围 | 最终回答行 | 当轮时间（UTC） |
| --- | --- | --- | --- |
| CF-WX-FB-PDF-20261006-02 | 276–285；下一 user 为 286 | 285，finish_reason=stop | 2026-10-06 09:47:24–09:49:42 |
| CF-WX-FB-PNG-20261006-01 | 286–293；读取快照时无下一 user | 293，finish_reason=stop | 2026-10-06 09:55:00–09:56:48 |

以下 Gateway message/dispatch/delivery ID 来自操作者；上述行号来自 Windows Hermes，
两者不是同一种 ID。关联依据为测试编号、同轮下载/读取目标以及最终回答 UTF-8 摘要。
没有拿此前直连 HTTP 的 session、工具轨迹或 49/49 分数代替本次微信任务。

## 两个实际本地文件

只检查了以下两个指定样本路径，没有扫描其他业务目录：

```text
C:\Users\Admin\CF-Acceptance\CF-WX-FB-PDF-20261006-02\run-eae416d6fb0c4b259d4ca6da33b8e911\CF-NATIVE-PDF-20261006-A1.pdf
C:\Users\Admin\CF-Acceptance\CF-WX-FB-PNG-20261006-01\run-bec2f9a0f9a243249ff7ff8cf2a258f7\CF-NATIVE-IMAGE-20261006-B1.png
```

| 对象 | 实际字节数 | 文件 SHA-256 | 核对 |
| --- | --- | --- | --- |
| PDF -02 下载文件 | 79083 | `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44` | 与用户期望及同轮 terminal 下载凭证完全一致 |
| PNG -01 下载文件 | 47740 | `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` | 与用户期望及同轮 terminal 下载凭证完全一致 |

两个文件及父路径未发现 symlink/junction。原生读取参数指向各自同轮下载凭证中的
确切路径，当前文件摘要也匹配该凭证。文件原件保持原位，没有复制旧验收副本充数。

## 同次任务的工具与回答

PDF -02 的 4 次工具调用与结果均可配对：

1. `skill_view("ocr-and-documents")`：返回成功的 `unchanged/dedup` 说明，不含正文；
   引用同一 Gateway 会话中先前 PDF -01 已加载的说明，不虚构本轮重新返回完整内容。
2. `terminal`：通过普通 HTTPS 先 GET `/api/users?id=self`，再 GET
   `/api/resources/download` 的指定 PDF；两次 HTTP 200。凭证记录
   `username=hermes-agent-test`、`permissions.admin=false`，exit_code=0。
3. `read_file`：读取上述新下载 PDF，`extracted_document=true`、`total_lines=47`、
   `truncated=false`。最终回答包含本次路径、摘要、两页正文、数字表格及 `END-Z4R8W2` 尾标。
4. `skill_manage`：对 `ocr-and-documents` 执行一次 patch，结果 success=true；
   这是附带的持久修改，不能从工具轨迹中省略，见下一节。

PNG -01 的 3 次工具调用与结果均可配对：

1. `skill_view("ocr-and-documents")`：返回经过上述 patch 的说明。
2. `terminal`：同样通过 HTTPS GET self 验证受限非管理员身份后 GET 指定 PNG；
   两次 HTTP 200，exit_code=0，保存唯一 run 目录中的新副本。
3. `vision_analyze`：输入路径精确匹配该新副本，返回原生加载文本与 `[screenshot]`
   持久化投影；最终回答给出核验码、四区属性和三条箭头方向。

这两段实际下载命令使用 `Authorization: Bearer`，Token 只在请求进程内从原引用
读取；命令和返回未回显凭据值。命令核对 CA 摘要、保留证书和主机名验证、使用直接
HTTPSConnection、拒绝重定向、限制为最多 1 MiB，并以唯一子目录和 `xb` 排他保存。
没有使用 FileBridge 专用下载组件、安装依赖或附带解析脚本。本轮只审查这些历史记录，
未再次读取 Token/CA，也未进行额外真实凭据匹配扫描。

PNG 回答为 `PIC-6V9K4R`，A 左上 3 个红圆、B 右上 2 个蓝正方形、C 左下 4 个
黄三角形、D 右下 1 个绿五角星；箭头 A→B、B→D、D→C。这里只记录同轮实际回答及
服务器摘要关联，没有重新运行模型或套用此前样本评分。
`model_transport_image_bytes` 继续为 **unobserved**；原生工具投影不是供应商请求
原始图像字节的独立观测。

## 必须保留的 Skill 附带修改

PDF -02 的行 283/284 记录 `skill_manage` 将认证企业 PDF 的下载约束补入本机
`ocr-and-documents`，包括 Bearer/self 检查等说明。操作结果为一次 patch 成功。
随后的 PNG -01 `skill_view` 正文与该 patch 应用后的内容逐字一致，不能声称它仍是
此前未经修改的官方说明。

| 说明正文 | SHA-256 |
| --- | --- |
| 同一会话 PDF -01 行 269 的原正文 | `856c1bce359ca7da0cc83f06e7b6b59f9445c16084d0510fc1541101af57f263` |
| PDF -02 patch 后、PNG -01 行 288 返回的正文 | `8f5a7cdf151eca0ccc92d5a7a521a9cbba83f730dfd2e0855621bdb153ee6fc4` |

因此，本次只收口两个具体业务读取/文字交付链路，**不出具“工具全程只读”或
“此前 PNG v2 固定 Skill 字节约束也已通过”的结论**。该历史修改原位保留，本轮没有
创建、修改、恢复 Skill，也没有清理半升级插件、旧计划或未知残留。它不构成 Gateway
产品代码变更，也不据此部署；后续工具副作用约束应独立处理。

## 最终回答字符串与服务器链路

摘要直接取同轮最终 assistant `content` 字符串的 UTF-8 编码，**不 strip、不改行尾、
不添加换行或 BOM**。私有 `.utf8.txt` 以相同原始字节保存，导出文件摘要也一致。
两条原字符串均没有末尾换行；没有出现需要修饰原件才能匹配的差异。

| 回答对象 | UTF-8 字节数 | reply_utf8_sha256 | 与操作者记录 |
| --- | --- | --- | --- |
| PDF -02 最终回答（行 285） | 2279 | `7d8a0794422ce580ae046257910eab741ca0cdb3b48195b4297a70e99c9b03fc` | 完全一致 |
| PNG -01 最终回答（行 293） | 1084 | `4c823d456442ffff78b1e35442eb138861d6a940bdff17b47676cfd419b31fec` | 完全一致 |

这些是**回答字符串摘要**，不是上表中的下载文件摘要。

操作者提供的 `MEDIA_DB_CHAINS=PASS`：

| 测试 | Message | Dispatch | Response | Delivery |
| --- | --- | --- | --- | --- |
| PDF -02 | 3672 | 40，success，attempt_count=1 | legacy-response:message:3672 | 38，delivered，attempt_count=1，part_count=1 |
| PNG -01 | 3673 | 41，success，attempt_count=1 | legacy-response:message:3673 | 39，delivered，attempt_count=1，part_count=1 |

操作者确认两条的准入、线程、派发、回答一致性、投递目标、分段完成和逐段唯一成功
尝试/回执检查全部通过，并在原微信聊天实收两条回复。Windows 同轮最终回答摘要与
这些记录相等，完成跨端关联。Codex 没有取得完整服务器数据库或独立执行该 SQL，
不能把操作者提供的检查扩写成 Codex 的现场直查。

## 失败记录、冻结材料与本轮验证

- `CF-WX-FB-PDF-20261006-01` 保留在同一 Gateway 会话行 267–275。其 X-Auth
  身份请求 HTTP 401，最终回答明确停止，未下载 PDF；最终回答 UTF-8 摘要为
  `de6bd803743212afd7d5fda873cab5f772bb92fa60111cb9d23b97d58817a9a3`。
  PDF -02 是用户明确纠正认证格式后的另一测试编号，不回写 -01 为成功。
- [此前直连 HTTP 的 PDF 工具约束偏差](2026-10-06-hermes-http-media-acceptance.md)
  与 [PNG v2 记录](2026-10-06-hermes-http-png-v2.md)均保持原评分、原会话和原证据。
- 新私有证据包括操作者来源说明、三个精确轮次的原始摘录/最终回答、关联结果和
  Skill 副作用记录；冻结清单涵盖 10 个文件，清单 SHA-256 为
  `8256fb6c09721abd661365ba4b377e22889f68b0c3b214e947478489aa2a28c1`。
  原数据库、样本和旧验收材料未改写。首次归档因 Skill 去重返回没有 content 字段而
  中止，保留错误记录后使用同一会话已保存的正文完成关联，没有重复任务或修改答案。
- 本轮只有说明和 PR 进度更新，没有产品代码、测试框架或客户端新增。核验为两个
  实际文件的大小/摘要、精确轮次与工具配对、两个回答原始 UTF-8 摘要及冻结材料检查；
  文档/链接/whitespace 检查与提交对应 CI 单独报告，不用 CI 代替本次业务证据。

## 当前完成范围与下一项

对这两条明确编号的任务，已有操作者服务器派发/投递证据、Windows 同轮下载/读取/
最终回答，以及原聊天实收确认，可收口当前**文字结果回传模式**。复用了非秘密连接
引用，不代表复用旧样本、旧回答或旧验收轨迹。

仍未证明：整个现场的镜像/配置/迁移/全队列审计、通用故障/重启/FIFO 覆盖、真实宿主
grant/resolve/events/closed 全生命周期、模型传输图像字节，以及任意文件类型或任意
聊天都可成功。未修改 `legacy_runtime_confirmed`、Checkpoint 或历史任务。

微信入站附件实际字节取件、历史 PDF 持续 pending、图片原图状态仍是独立边界；
读取已经存入 FileBrowser 的样本不能关闭这些问题。

**下一项才是“实际 PDF 文件/图片返回发起任务的原微信聊天”。** 需要单独验收
READY Artifact → Delivery → 原聊天的文件/图片消息与附件回执。本轮两个
`part_count=1` 和 delivered 只证明文字投递，不能写成 PDF/PNG 附件投递已经通过。
本轮不启动这项任务，不合并、部署或发布。
