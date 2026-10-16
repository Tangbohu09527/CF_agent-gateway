# 附件会话接续候选：隔离验证

本候选从 `31f46b2827db9353808ee11ff01ab7fdbf173c8e` 建立独立分支
`codex/fix-inbound-session-continuation`，原 PR16 与固定起点 d9bae61 保留。
这是源码/隔离验证记录，**不是新候选生产部署或 PDF/PNG 现场闭环通过证明**。

## 已有结果与确切缺口

[上一轮现场结果](2026-10-16-artifact-return-403-acceptance.md) 不变：ACK04 实收通过；
第一份 PDF 79083 bytes、SHA256
`ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44`
完整取件通过；Dispatch46 在附件执行前被 `inbound_parent_runtime_unverified` 拒绝。
它不是此前 HTTP403。Dispatch42/43/46 不重派、不改 dead。

缺少的是对该旧父会话适用的 runtime 部署证明。原契约的
`legacy_runtime_confirmed` 是 attestation，不是要求所有旧聊天拥有穷尽逐调用审计。
当前锁、usage 无冲突和当前配置相等不能补造旧历史证明，也无需重复读取已知不保存
此信息的字段。候选保持 false，并提供不继承未知历史的新执行路径。

## 选定实现与上下文影响

[完整契约与回退边界](../development/inbound-execution-continuation.md)：

- 精确私人 thread allowlist 默认空；首次附件由官方 create 建新 root，保留旧会话。
- 只携带附件前最近一条已成功的同身份/同人/同聊文字说明；完整有界，冻结 ID 和
  内容/来源摘要。不是全部历史的自动继承，不增加摘要模型调用。
- 第一次完成仍用原 claim/lease/host barrier/tip CAS 同事务推进；之前不改聊天映射。
- 后续文字显式请求同一 runtime；下一附件从已成功、同 Profile/runtime 的受管 tip
  fork。重启通过持久记录接续。陌生 tip、配置漂移、跨身份/聊天继续拒绝。
- 加列迁移 `20261016_01` 不回填旧记录；存在新 lineage 时拒绝破坏性降级。

若用户说明依赖更早的旧上下文，应先补全说明，不能称已完整继承。此次已有明确的
“读取第一页、原文件返回、保留原件”说明适合按固定消息事实核验后使用。
暂不支持自动压缩 tip 接纳，保持显式 UNCERTAIN；不通过重建空会话隐藏此情况。

## 隔离验证矩阵

所有开发输入、数据库、密钥、工作目录、模型和微信收发均隔离。没有调用生产模型。

| 验证层 | 实际覆盖 | 结果 |
| --- | --- | --- |
| 新连续业务回归 | 旧历史/legacy=false；默认4次失败进入dead；说明→新PDF→文字→PNG→重建engine/执行替身→第二PDF；原dead全字段不变 | 22项通过 |
| 新失败边界与迁移 | 身份/mapping/host禁用；Profile/runtime/tip漂移；同claim冻结、stale claim、禁止跨成功附件选择说明、FIFO与Message ID交错；fallback前置门；非空旧库升级保留、含lineage拒绝降级 | 21项通过 |
| 现有会话与迁移 | host-binding dispatch + migrations | 20项通过 |
| 相关安全/持久化回归 | host HTTP/fixture/runtime/worker、媒体队列、database、migration | 82项通过 |
| 相关客户端/配置回归 | config、Hermes client/dispatch/inbound sessions | 96项通过 |
| 当前官方 Hermes 427d605 | 真实HTTP handler、Agent、SessionDB；新root、两次fork、进程重启、旧历史保留、显式runtime | 定向实测通过 |
| PostgreSQL | 本机未配置独立fixture | 本地1项跳过，CI结果另记 |

前两行使用真实 Gateway admission/claim/SQL、host resolve/events/closed、授权读取、
artifact return PUT/seal、Response 和 Delivery；Hermes 与微信发送端是测试替身。
PDF/PNG 返回字节逐字节一致；PNG按现有 image 路由发送，不能据此声称生产微信原文件
返回成功。原生探针单独验证官方会话与 runtime 行为，不冒充真实 FileBrowser 入站取件。
SQLite 测试不是 PostgreSQL 现场迁移证明。

## 原生探针发现的 runtime 前置条件

[定向探针](../../tests/hermes_execution_probe/README.md) 使用当前官方源码的固定 blob、
独立 HOME/数据库/端口以及回环模型，执行结束核实子进程已退出。r7 报告 SHA256：
`27fdf49710f20349262227bfc31eda6606514fa971668c82884ec852b7fa84ff`。

| 官方实际行为 | 主模型次数 | fallback次数 | Gateway观察 |
| --- | ---: | ---: | --- |
| `/v1` 显式runtime，已持久model lock，主模型404 | 1 | 1 | 接受备用模型成功 |
| `/v1` 再加 `require_model_lock=true` | 1 | 1 | HTTP200，仍会fallback |
| session `/chat` 强锁 | 1 | 0 | HTTP200且缺可靠失败字段，不能替代原端点 |
| 专用Profile有效两种fallback配置链均为空 | 1 | 0 | `HermesResponseError`，失败可见 |

因此新增默认 false 的 `runtime_fallback_disabled_confirmed`：必须核验受保护的有效
执行配置为空 fallback 后才能启用 fresh allowlist；已受管会话也不能丢失该条件后
退回默认调用。它针对未来环境，不能拿来证明旧历史。runtime digest 只证明请求配置
一致，不能被称为 Hermes 实际执行强锁。未擅自改现场 Hermes fallback 或 Profile。

## 最小生产接续

1. 使用既有12小时上限凭据入口，只读核验当前 Poll/Delivery、在途和版本；核对第二份
   已发 PDF 的原聊天身份/时间/文件事实及是否入库。保持 checkpoint，不调用 Poll 来
   “检查”，不让用户再发第三份。确认适用的说明消息及首次切换范围。
2. 审阅本候选、CI和上述上下文影响后，集中授权新 Gateway 镜像部署、正常加列迁移、
   仅原私人 thread 的配置启用；旧镜像与数据库备份保留。核验有效空 fallback 的保护
   配置及运行加载依据；若不满足，报告具体差异并另列必要变更，不自动修改 Hermes。
3. 用户确认即时配合后，经原 Controller 开短窗口接入第二份 PDF；自然生成新任务，
   保留46 dead。实际读取与附件回传、摘要、用户实收均通过后才执行 PNG。
4. 每项 Collect、关闸并核查在途，不把关闭收发当作取消 Dispatch。任务结束清除临时
   密码并撤销本轮任务授权。不得提前开窗等人或把12小时认证期当收发窗口。

当前源码回滚边界：未首次切换时关闭allowlist即可阻止新切换；已有受管 lineage 后
保留候选、关闸调查，不自动运行旧代码于新 tip、不降级删列、不恢复旧tip或重派任务。

## 无人值守接续：配置预检的实际修正依据

用户离开前将本轮边界收紧为只读核验、候选构建和开发回归；新的生产切换、
上下文迁移及真实微信验收暂停。此前获批的部署尚未进入停服务或迁移阶段。
辅助程序首轮 prepare 在只读预检以 `unsafe_file` 停止，原包和失败回执保留。

2026-10-16 11:19 UTC 的实际核验显示：正式 Compose 渲染与四个容器的只读配置
挂载一致指向 `/opt/cf-agent-gateway/config/production.yaml`，运行中的 Gateway/
Dispatch 容器读得同一摘要。文件为 root:root、0644、1532 bytes，父链不可由普通
用户修改、无扩展 ACL/链接；SHA256 为
`39f5edd479bc2ec88866327ef0d0fcde939d4d47b5aed9f21b8e4c894aa5005c`。
该文件不在旧授权记录的 trusted map 中，不能把未记录误称为历史摘要漂移。
`production.local.yaml` 是源码支持的另一种入口，不能凭其名称推定当前使用它。

确切缺陷是辅助程序将实际配置数据误套为必须 0600 的私密文件，并额外预设了
10001:10001 所有权。修正版以正式 Compose 选择、实际挂载和本次受保护记录共同
确认路径，再核验配置的准确身份及摘要；私密文件和程序仍使用各自严格检查。
不移动配置，不改 owner/mode，不合并外部 env 文件。合法外部引用继续依赖原
受保护记录；未审核引用及权限、身份或摘要漂移必须拒绝。

连续业务测试另补 Poll checkpoint 全字段哨兵：legacy 拒绝、PDF/PNG 成功及重放
拒绝、执行层重建、第二 PDF 和旧 dead 再输入后均保持原 checkpoint。定向用例
1 passed；它验证处理已规范化消息的路径不改 Poll 游标，不冒充真实轮询验收。
