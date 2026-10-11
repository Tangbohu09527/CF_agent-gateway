# 2026-10-11 Gateway 现场摘要：新计划通过、未切换、重启后待复核

证据来源：**用户终端回传证据**。本文归并用户提供的 2026-10-11 现场结论及已有
10 月 7 日、10 月 11 日记录；不是 Codex 连接 CFserver 或检查真实服务取得的结果。
日期按 Asia/Shanghai；未提供各阶段精确时间，不补造时间戳。
这是带日期的现场摘要，状态入口仍为 [production-status](../production-status.md)，
执行方法继续复用既有手册。

## 最后停点

**新计划通过、未切换、重启后待复核。** f9 两端旧配置回退已经完成，不能继续写成
“仍在 rolling_back”；d9 新候选尚未 apply/start/verify，不能写成“修复版已部署”。

1. f9 两端旧配置回退完成；服务器 `complete-rollback` 成功，
   `assets_rewritten=false`。旧 Hermes 启动和服务器 `start-old` 也已成功。
   这些已完成阶段不是当前阻塞点，不再从头停回或回退。
2. 新 deployment `cf-return-env-loopback-20261011-r1` 的候选已构建，
   Windows 与服务器新 plan 均通过。尚未执行新 `apply/start/verify`。
3. 用户随后明确已执行 `sudo reboot`，只重启 CFserver。重启是否完成、重启后
   容器/Gate/微信登录状态尚未核验；Windows 是否关机没有确认。
   不能用重启前 PID、容器健康或零队列快照证明当前状态。

## 固定版本与仓库状态分别记录

| 对象 | 2026-10-11 记录 | 证据与限制 |
| --- | --- | --- |
| 新 deployment | `cf-return-env-loopback-20261011-r1` | 用户终端回传；现存新计划的部署身份 |
| 新 release commit | `d9bae61a043f1be4303e5c23661ec51904d5e708` | 固定部署版本；不能替换为本次文档提交或以后分支 HEAD |
| 已构建新候选 Image ID | `sha256:bcb3e42fef5ba5a91232ca04e49a959cbecab4b45eb3bec0a0b9b3aadf717185` | 用户回传的候选身份，不是已运行镜像或独立核验的 registry digest |
| 旧 f9 release | `f9c601cba25cbc5459bb03519d1954ae55995ded` | 旧 request/plan/journal 继续保留原绑定，不改填 d9 |
| 旧恢复基线 Image ID | `sha256:1c78a61f8728bf629ec3cd7ea1037e6f604d7d6e1e4e06e46ade8d8a7e53f163` | 既有恢复记录的 baseline；重启后实际容器身份待复核 |
| `main` | `7963bc0db5a01e38099e021959feabf648db7ede` | 本次只读查询远端分支所得；不是新候选的部署依据 |
| [PR #14](https://github.com/Tangbohu09527/CF_agent-gateway/pull/14) | Draft/Open，head `f9c601cba25cbc5459bb03519d1954ae55995ded`，base `main`，未合并 | 本次 GitHub 只读核对；d9 未进入该 PR |
| 独立修复分支 | `codex/fix-hermes-env-loopback` → `d9bae61a043f1be4303e5c23661ec51904d5e708` | 本次只读查询远端所得；d9 未进入 main |
| 本次文档分支与 PR base | `codex/docs-gateway-status-20261011` → base `codex/fix-hermes-env-loopback` | 从固定 d9 建立；仅 Markdown 更新，普通 commit/push、Draft PR；不改变功能 PR |

本次只在新的 `5152/CF_agent-gateway` 工作树修改文档；旧 `2d15`、`59ae` 工作树及
原分支不推进。文档提交不改变已有候选、计划、发布身份或部署批准范围。

## 新计划脱敏摘要

| 项目 | 用户回传结论 | 适用范围 |
| --- | --- | --- |
| Windows plan | 通过，共 13 项 | 包含 Hermes `.env` 的 `API_SERVER_HOST` 单字段修改；不代表已写入正式配置 |
| 原工具集 | 原 17 项工具集保留 | 既有计划仅追加附件返回工具；不替换原工具选择 |
| 服务器 plan | 通过 | 原 `env_file` 解析与环境等价校验通过；不输出环境值或生产凭据 |
| 迁移 | 新旧迁移头均为 `20260928_01` | 本次不需要迁移；不是重启后数据库实时检查 |
| 后续阶段 | 新 `apply/start/verify` 均未执行 | 计划通过不等于切换、TLS ready、ACK 或微信附件实收 |

当前可纳入的计划细节以上述用户回传结论为限；本次未取得并独立核对详细附件中的
原始新 plan JSON、摘要值或逐项清单，因此不补造新 plan SHA、路径或 13 项明细。
下次可对照现存详细版附件与原 plan/公共回执，补充脱敏字段并保留原始证据来源。
旧 f9 plan 摘要仍只属于 [旧恢复记录](../deployment/server-rollback-resume.md)，
不能当作本次新 plan 的摘要。

## 重启前快照与重启后未知项

| 对象 | 重启前用户回传 | 重启后 |
| --- | --- | --- |
| 旧 Hermes | 已成功恢复并运行 | Windows 是否关机未确认；当前进程及监听待核验 |
| Gateway API / Dispatch | 旧核心运行 | 容器、镜像、进程及健康待核验 |
| Poll / Delivery / Gate | Poll、Delivery 停止，Gate 关闭 | 不假定跨重启保留，须重新核验实际 Gate 与 Worker 状态 |
| 数据库与工作计数 | 数据库正常；待处理/在途/uncertain 为 0 | 实时状态待核验；零值不跨重启沿用 |
| 历史 dead | `dead=2` 保留 | 不删除、不重派历史记录来使状态看似健康 |
| CFserver / 微信登录 | 用户之后已执行 `sudo reboot` | 重启完成与否、`agent-wechat` 及登录状态均未知 |

重启前 `start-old` 成功不证明重启后核心已经恢复，Gate 关闭的旧快照也不证明重启后
仍关闭。既有 [CFserver 重启恢复规则](../runtime-recovery.md#agent-wechat-offline-after-host-reboot)
要求正式 Gate 停止确认后处理微信 fresh QR；本摘要不声称已经执行登录恢复或重新开闸。
具体顺序应与尚未切换的新计划维护窗口协调，不能直接跳到通用手册最后的 Gate reopen。

## 三条业务链路不能互相替代

| 链路 | 已有证据 | 尚未证明 |
| --- | --- | --- |
| 微信文字指令 → 文件 API → Hermes 实际读取 → 文字结果回原聊天 | [10 月 7 日文字收口](2026-10-07-wechat-file-api-text-closeout.md) 的两条具体 PDF/PNG 任务，含用户回传的服务器链路与原聊天文字实收 | 不证明微信附件原件取回，也不证明 PDF/PNG 附件发回 |
| 微信入站文件/图片 → 实际附件字节 | [入站取件契约与检查项](../development/inbound-media-stage3.md) 区分元数据、下载、持久保存与后续读取 | 历史 PDF pending、图片原图状态仍须独立核验；已有 FileBrowser 文件不能替代微信取件 |
| 当前任务实际 PDF/PNG → READY Artifact → Delivery → 发起任务原聊天 | [隔离字节链路](2026-10-07-artifact-return-isolated.md) 与 [官方 Hermes 联合验证](2026-10-07-official-hermes-artifact-return.md) 保留，接收端是测试替身 | 真实 PDF 文件消息可下载/打开及摘要、PNG 图片消息内容及可能转码、原聊天实收与回执仍未验收 |

文字收口记录中的 PDF -02 附带 Skill 修改、PNG 使用修改后说明，以及隔离验证的
平台限制/失败记录继续保留。不能将隔离测试、HTTP 成功、ACK 或文字 delivered
升级为现场附件实收，也不需要因为此次文档更新重跑已完成的样本。

当前优先微信文件收发。局域网权限、聊天记录 Web 仅为后续考虑；本任务不设计或开发。

## 下次接管导航

以下是后续工作入口，本次没有执行其中的现场检查或命令：

1. 先核验 CFserver 是否完成重启，以及当前容器/镜像、Gate、微信登录、数据库和
   队列；单独确认 Windows 当前状态。使用新鲜观察，不能借历史 PID 或重启前快照。
2. 清点现存源码 checkout、候选镜像、双端 request/plan、公共回执和恢复材料；对照
   固定 deployment/d9/Image ID、新 plan 摘要及证书/环境绑定，不读出或外发凭据。
   现存材料符合条件就复用，不重复传包、克隆、构建或创建另一套 deployment。
   若漂移或材料不符，保留证据并查明差异，不改旧 SHA、回执、journal 或历史任务。
3. [两端现场部署总入口](../deployment/artifact-return-site.md) 保留通用约束；
   [环境恢复手册第 3 节](../deployment/hermes-env-loopback-recovery.md#3-独立修复-release-和全新双端计划)
   用于核对已完成的新规划材料，**不重跑初始化**。
   核验通过并具备维护授权后，再从该手册
   [第 4 节既有交错切换顺序](../deployment/hermes-env-loopback-recovery.md#4-新计划按原交错顺序应用)
   接续。变量从已核对的现存材料恢复，不靠重跑第 0–3 节取得。
4. [服务器回退收尾手册](../deployment/server-rollback-resume.md) 仅用于查阅已经完成的
   f9 恢复过程；不再把 `complete-rollback`、旧 `start-legacy/start-old` 当作当前待办。
   [HOST/TLS 验证](2026-10-11-hermes-env-loopback.md) 和
   [baseline 恢复验证](2026-10-11-server-rollback-resume.md) 保留各自代码/隔离测试边界。
5. 新版本启用后的微信入站原件、实际 PDF/PNG 回原聊天仍按上表分别取证；本次文档
   任务不触发模型、消息、附件、登录、迁移或业务重验。

本次仅核对 Git/文档与更新 Markdown，未连接 CFserver，未操作 Hermes、微信、Docker
或数据库，未读取生产凭据，未执行手册中的命令。文档链接、UTF-8、围栏和 diff 检查
只证明文档质量，不证明重启完成、部署成功或业务验收通过。
