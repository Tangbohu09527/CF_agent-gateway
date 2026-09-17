# 2026-09-17 Hermes 长任务回传实机验收

> 结论：本次分钟级任务的 Gateway 响应持久化及微信回传验收通过。
>
> 证据日期：2026-09-17；时区为 UTC，另列北京时间（UTC+08:00）。本文依据用户提供的 CFserver 终端输出和微信截图整理，不是另一次远程实时检查。
>
> 当前部署摘要见[Production status](../production-status.md)。本文是带日期的限定场景记录，不代替完整发布、恢复或高可用验收。

## 版本与证据边界

| 层次 | 已记录事实 |
| --- | --- |
| 修复代码 | [PR #11](https://github.com/Tangbohu09527/CF_agent-gateway/pull/11) 已合并；合并提交 `9a1caa237a9053678c80f68fdb15d351d5bfecf8`，2026-09-17 通过 GitHub 重新核验 |
| 现场镜像 | 四个应用核对为 Docker Image ID `sha256:1cd7650543babe75d4fabe71e27e3cbc1d54585d34ffa853280606c2a3ddaa8b` |
| 启动配置 | Dispatch 启动日志 connect/read/write/pool/execution 分别为 `5/600/15/5/600` 秒；Compose Dispatch 停止宽限期 `3660` 秒 |
| 数据库 | 仓库迁移 head 为 `20260823_04`；现场 runtime 的 database/migration_schema 为 `ok`。本次文档整理没有再次连接现场查询 revision |
| 恢复状态 | `DISPATCH_RESUME=PASS`、`STARTUP_600S=PASS`；Controller `ready=true`、Token 契约有效；其他容器未发生该恢复脚本预期外的变化 |
| 代码级验证 | PR #11 记录了 444 项相关本地回归；这是 PR 提交时的测试记录，不是本次文档变更的新 CI 结论 |
| 实机验证 | 本文下述 Message 1634 → Dispatch 24 → Delivery 23 已核验 |

代码合并提交与现场 Image ID 是分别核对的事实；本轮没有独立审查完整构建来源证明，不能声称已证明源码与制品的逐字节映射。Image ID 不是已经核实的 registry manifest digest。新 Release label、Tag、离线镜像档案及新版本备份/回滚材料未在本轮证据中完整提供。

本轮没有重装 Hermes、切换 WSL/CLI 或重建微信入口。AI 主机重启后链路恢复的事实不等于已验证无人值守自启、执行中重启或长期 watchdog。

## 通过的限定场景

测试编号为 `CF-LONG-20260917-02`。用户通过微信发起只读验收，要求在原生 Windows 工具环境同步等待 90 秒，再读取既有合成测试文件，等待进程结束后返回结果；不要求创建、修改或删除业务文件。

| 对象 | 数据库结果 |
| --- | --- |
| 同验收会话的入站测试消息匹配数 | `1` |
| Message | `1634` |
| Dispatch | `24`，`success`，`attempt_count=1`，错误码为空 |
| 原始 Dispatch 响应 | `1` 条 |
| 标准化 Response | `delivered`，`1` 段文本 |
| Delivery | `23`，`delivered`，`attempt_count=1`，`next_part_ordinal=1`，错误码为空 |
| DeliveryAttempt | `23`，`part_ordinal=0`，`attempt_number=1`，`delivered` |
| 匹配回执 | `1` 条 |
| 微信实收 | 用户提供的截图与落库回复一致 |

这条记录链中未发现第二次派发或第二次分段投递尝试；不把它外推为所有外部操作的普遍 exactly-once 保证。

## 服务端时间线

| 事件 | UTC | 北京时间 |
| --- | --- | --- |
| Dispatch claimed_at | `2026-09-17T05:34:29.365607+00:00` | `2026-09-17T13:34:29.365607+08:00` |
| Dispatch completed_at | `2026-09-17T05:36:23.164729+00:00` | `2026-09-17T13:36:23.164729+08:00` |
| Delivery completed_at | `2026-09-17T05:36:26.489926+00:00` | `2026-09-17T13:36:26.489926+08:00` |

Gateway 派发全过程为 **113.799 秒**；派发完成至投递记录完成为 **3.325 秒**；领取至投递记录完成为 **117.124 秒**。均为服务端数据库事件时间差，不是每个 HTTP/模型/工具阶段的单独耗时，也不是微信客户端显示时刻的精密测量。

回复报告的工具起止时间为 `2026-09-17T05:34:47.763171+00:00` 至 `2026-09-17T05:36:17.763864+00:00`，报告耗时 `90.001` 秒。落库回复中的测试编号、运行编号、时间、耗时、路径、合成文件内容和哈希与验收预期及截图的核对均返回 `true`。

回复报告的 Python 版本为 `3.11.16`，不是 Gateway Python 版本，也不是本轮独立查询解释器的结果。为避免公开现场信息，本文不发布 Windows 用户路径、文件名、消息正文、文件内容、账号、会话标识或截图。

## 最终只读核验结果

```text
DB_READ_ONLY="on"
MATCH_COUNT=1
GATEWAY_DISPATCH_SECONDS=113.799
DB_CHAIN_CHECKS={"gateway_elapsed_at_least_90s": true, "stored_reply_matches": true, "delivered_once": true, "all_parts_completed": true, "one_receipted_attempt_per_part": true}
LONG_TASK_DB_CHAIN="PASS"
RAW_HERMES_TOOL_LOG="NOT_REVIEWED"
```

该核验使用单个 PostgreSQL 只读事务，没有修改业务记录、重发微信或调用 Hermes。原始响应条数已核对，但未逐字比对原始 Dispatch 响应正文与标准化分段；一致性检查针对已保存的标准化回复与截图/验收预期。

Hermes 原始工具命令、标准输出和退出码尚未审阅。哈希一致性只证明回复中的值与已知合成测试字节相符，不代替独立读取 Windows 文件或审阅工具日志。**本次服务端等待、持久化及微信回传通过；工具执行证据独立复核仍为未完成。**

## 相关恢复与失败记录

### 历史 Dispatch 21

Message `1349` / Dispatch `21` 已于 2026-09-16 17:35:03.640393（北京时间）通过独立人工恢复标记为 `dead`。Recovery audit `1` 记录 `mark_dead`、`uncertain → dead`；派发次数仍为 `1`，不再阻塞后续派发。保留原错误，未伪造成功回复或重新派发。恢复不是代码升级/迁移的自动副作用，不应重复执行或删除历史审计。

### 积压 Dispatch 22

`CF-READ-20260915-02` 对应 Message `1403` → Dispatch `22` → Delivery `21`。派发成功、尝试一次、耗时 `20.444` 秒；投递成功、尝试一次，并有微信实收截图。Delivery `21` 与历史 Dispatch `21` 是不同对象。该较短任务不单独计作超过 30 秒验收。

### 未通过的首次长任务

`CF-LONG-20260917-01` 的回复报告脚本解析阶段发生 Python `SyntaxError`，未开始 90 秒等待，不计入长任务验收。随后使用新编号 `CF-LONG-20260917-02` 发起新的只读任务，没有把失败记录手改为成功或人工重派旧 Dispatch。

## 保留的运行告警

已审阅的 Poll 日志曾记录 22 个会话中 19 个成功、3 个连续性失败：两个独立历史会话为 `stop_chat_visible_window_empty`，另一个历史会话的 Checkpoint `18` 为 `stop_chat_empty_window_marker_unavailable`。验收会话对应 Checkpoint `12`，观察到轮询成功、`unverified=false`。

没有手工重置 Checkpoint、generation 或指纹来放行验收。三个历史问题未关闭，最后的数据库链核验没有再次实时检查它们。`unverified_checkpoint_count=1` 统计非零 Checkpoint 缺少指纹的数据库记录，不是全部运行时连续性失败的数量；与 `chats_failed=3` 不矛盾。历史 `dead` 及这些告警可以使总健康保持 `degraded`，不能以删除记录的方式变绿。

## 尚未完成与后续责任

| 项目 | 本轮状态 |
| --- | --- |
| Hermes 原始工具记录、文件访问证据、桌面端构建版本 | NOT_REVIEWED / NOT_VERIFIED |
| 接近 600 秒上限、执行中断线/重启及不确定结果恢复 | NOT_RUN |
| 长任务期间跨会话并发、同会话 FIFO、全过程心跳和续租专项 | NOT_RUN；不能从单条任务成功推定全部通过 |
| 三个历史会话连续性问题恢复 | OPEN |
| 新版整机重启、无人值守自启、watchdog、高可用 | NOT_RUN |
| 新版离线归档、备份还原与回滚演练、完整制品来源证明 | NOT_VERIFIED |
| 完整媒体、企业 File Service、Skills、ERP 集成 | 不属于本次范围 |

现场运维/用户负责原始证据保管、Hermes 工具及版本复核、连续性问题与恢复专项；Gateway 文档负责准确记录已验证边界。上述后续工作是待办，不是已经安排的自动任务。

## 证据保管与文档变更边界

证据来源为本轮用户提交的启动/恢复日志、Dispatch 21 恢复审计、Dispatch 22 投递结果、Poll 告警摘要、微信截图及最终 PostgreSQL 只读输出。截图和完整终端上下文不上传公开仓库；尚未提供可再次核验的受保护离线证据路径及归档校验和。

本记录只形成脱敏摘要。文档提交不执行新的生产部署，不修改代码、配置、Compose、工作流、迁移、凭据或业务数据库，不创建生产 Tag，也不代表其他专项已获验收。历史 P1 归档不自动成为本次升级的回滚证明。
