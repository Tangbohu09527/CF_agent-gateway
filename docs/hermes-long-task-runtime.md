# Hermes 长工具任务：等待、回传与升级边界

本修复针对 Gateway 在约 30 秒后放弃正常 Hermes 工具调用的故障。只改变 Gateway
请求等待、相关数据库事务和租约安全边界，继续使用 Dispatch → Response → Delivery
Outbox；没有增加新任务平台、守护进程、查询结果 API 或微信直发路径。

## 实际配置

生产 `run_dispatch_worker` 将 `settings.hermes.timeouts` 传给 `HermesClient`。
`load_settings`、`config/config.yaml`、`config/production.yaml` 及
`deploy/prepare-clean-host.py` 生成的配置使用相同默认值。旧 YAML 缺少整个
`timeouts` 或其中部分字段时使用下表默认值。启动日志打印这五项生效值，不打印密钥。
配置变更在 Dispatch Worker 下次启动时生效。

```yaml
hermes:
  # 保留现场已有 enabled、base_url、api_key_env、model。
  timeouts:
    connect_seconds: 5
    read_seconds: 600
    write_seconds: 15
    pool_seconds: 5
    execution_seconds: 600
```

| 字段 | 默认秒数 | 范围 | 含义 |
| --- | ---: | --- | --- |
| `connect_seconds` | 5 | `0 < x <= 120` | 建立连接的等待上限 |
| `read_seconds` | 600 | `0 < x <= 3600` | 等待下一段响应数据的上限，**不是总时长** |
| `write_seconds` | 15 | `0 < x <= 120` | 写入下一段请求数据的等待上限 |
| `pool_seconds` | 5 | `0 < x <= 120` | 等待 HTTP 连接池名额的上限 |
| `execution_seconds` | 600 | `0 < x <= 3600` | 一次 HTTP 请求从连接/发送到完整响应体的总等待预算 |

所有值必须是有限数值，允许小数；零、负数、布尔值、字符串、null、NaN、无穷大、
越界值、未知的 `timeouts` 字段均拒绝。总预算不包括入队等待和返回后的解析/数据库落库。
若 read 配得比 execution 短，网络静默可能先触发 read 超时；持续收到字节也不能延长
execution 总预算。默认可等待接近 10 分钟，配置上限 60 分钟；实际还受连接、读写及
现场 Hermes/代理自身限制，不能保证任何工具一定在预算内完成。

同步 Worker 接口内部使用每次请求独立的异步 HTTPX 客户端与事件循环。
`asyncio.timeout` 取消实际网络等待，HTTPX 退出并关闭连接后返回错误；没有用
`Future.result(timeout=...)` 遗留一个继续运行的 HTTP 调用。请求间不复用连接池，
Worker 的现有并发名额仍是主要并发限制。这里停止的是 Gateway 等待，**不是取消
Hermes 上的模型、工具或已经发生的外部操作**。

独立诊断 `hermes.diagnose` 保留显式 `--timeout`，将该值作为诊断总预算，并限制
连接/写入/连接池等待各不超过 120 秒；它不会继承业务调用的 10 分钟等待。原有
`HermesClient(timeout=...)` 兼容参数只覆盖 HTTP 分阶段等待，总预算另由 `timeouts`
控制。`runtime.wechat` 的兼容工厂参数目前不实例化 Hermes；实际业务调用由独立
Dispatch Worker 承担。安装验收中的合成协议探针使用显式短 timeout；本轮未运行该流程。

## 长调用与安全语义

- 持久派发在请求前复制输入并提交准备事务，释放数据库连接和线程行锁。
- 执行续租仍以 `lease_seconds / 3` 独立运行，Worker 心跳也独立更新。
  同线程后续任务保持 FIFO；一个长调用只占一个现有并发名额。默认并发 4，若显式设为
  1，其他会话自然需要等待名额。
- 完整真实响应返回后，在同一事务内校验 claim token 和未过期租约、推进 Hermes
  会话标识、写入 DispatchResponse 并标记成功，再进入现有 Response/Delivery Outbox。
  旧 owner 的迟到结果不能更新会话或伪造成功。
- HTTP 分阶段超时沿用 `hermes_timeout_error`；总预算耗尽使用
  `hermes_execution_timeout_error`。连接中断和这些超时均进入 `uncertain`，不自动重派。
- 进程丢失、强制退出或续租失败后，过期 `running` 在下次 claim 扫描时转为
  `uncertain`，保留 attempt_count 和同线程阻塞；不再接管重跑，也不因重试耗尽自动
  `dead` 解锁。即使无法确认是否真正发出请求，也采用保守语义。既有人工恢复接口保留。
- SIGTERM 停止领取新任务，等待活动调用在自身有限预算内结束，期间续租和心跳继续。
  Dispatch 专属 Compose `CF_GATEWAY_DISPATCH_STOP_GRACE_PERIOD` 默认 `3660s`，
  systemd `TimeoutStopSec=3660s`，覆盖支持的最大请求预算并留 60 秒落库余量。
  自定义停止超时（包括 `docker stop -t` 或运维外层时限）若更短，仍可能强制终止并留下
  不确定结果；数据库自身故障也不受 HTTP 预算解决。

## 断线/重启恢复不在本轮承诺内

现场是官方 [Hermes Desktop for Windows](https://hermes-agent.nousresearch.com/desktop)，
已安装版本尚未记录。官网当前页面不能证明现场版本或其后端能力。
本轮没有核实现场版本支持任务查询、取消、SSE 心跳或持久幂等重放，也没有假设这些接口存在。
HTTP 连接丢失且 Gateway 未持久化响应时，不能自动从 Hermes 找回结果。

现有 reconciliation 仅能修复 **Gateway 已保存** 的 DispatchResponse 后续 Response/
Delivery 缺失；它不查询 Hermes。最小后续工作是由用户记录已安装版本/构建号，保存相关
任务证据，再只读核对该版本官方后端是否有带稳定任务标识的结果检索接口及语义。在此之前
保持人工核查，不重复派发有外部副作用的操作。

HTTP 阶段超时语义见 [HTTPX 官方文档](https://www.python-httpx.org/advanced/timeouts/)；
取消机制见 [Python 3.12 asyncio timeout 文档](https://docs.python.org/3.12/library/asyncio-task.html#timeouts)。

## 旧生产 Release 升级与回滚（供后续人工实施，本轮未部署）

已知现场代码为 `f36c798294368263433f6132366ac9a864d9482b`，镜像为
`sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1`。
本分支起点为 `481c693e2297dd9d223c401f2a92fd92e762151d`；它还包含这两个快照之间
已有的其他主线变更。PR 的针对性 diff 不是整个旧 Release 升级 diff，后续选择目标制品时
必须一并核对。此修复不新增迁移；只读代码比较确认旧快照与当前迁移 head 均为
`20260823_04`，迁移实现和 `database.py` 无变化；现场实际 revision 仍须人工核验。

1. 发布/部署另行安排。使用经过审查、明确包含本 PR 的固定版本镜像/代码制品，记录
   digest、当前 Release、现有挂载路径、Controller 配置、业务配置和数据库 revision。
   按既有运维流程备份数据库、配置及相关持久卷，保留旧制品用于回滚。
2. 按现有 [生产升级流程](deployment/production.md) 关闭入口并排空可完成的活动调用。
   核对队列与原有 `uncertain` 基线；不等待 21 号任务自行解除，也不改它或后续队列。
3. 原位保留配置、凭据、身份/路由、数据库和业务数据，仅合并 Hermes timeout 配置与
   Dispatch 停止宽限期；核对启动日志中的生效值。旧配置也可使用新默认值。
   **不要运行 `install-clean-device.sh` / `prepare-clean-host.py` 覆盖现有环境**，
   不改数据库 URL 指向新空库，不删卷，不把干净设备初始化当升级。
4. 核对 Gateway/Dispatch 健康和已有 Controller 的 Poll/Delivery 流程，再由用户验收。
   升级范围只涉及 Gateway；不要因此重装 Hermes、换 CLI/WSL、重建 agent-wechat
   或操作 FileBrowser。
5. 回滚先停止新派发、让活动请求结束并保全不确定状态，再恢复先前固定镜像及其配置/
   Compose/systemd 组合，继续使用保留的业务数据库；本修复不需要 schema downgrade。
   不将旧数据库备份直接覆盖新业务写入。旧程序的 30 秒读取上限和过期 running 可重派
   行为会一起恢复；若仍有 running/uncertain，保持停止并人工核查，不能用回滚解除阻塞。

## 用户人工验收清单（尚未执行）

使用另一个**经授权且未被既有 uncertain 阻塞**的验收会话/线程，不能绕过 21 所在
线程保护来推进它的后续队列。每步记录消息/Dispatch 标识、耗时、attempt_count、
DispatchResponse/Response/Delivery 状态及微信实收结果，避免把敏感正文放入公共日志。

1. 短文本：确认正式链路仍正常，恰好一次回复。
2. 超过 30 秒的只读工具任务：确认运行期间 lease 与 Worker 心跳推进，attempt_count
   保持 1；另一授权会话能处理短文本；同线程后续消息按序等待。
3. 分钟级只读任务（小于配置总预算且留有余量）：确认 Hermes 的真实最终结果落库，
   Delivery 成功，微信实际收到一致结果；检查没有重复执行或重复投递。

代码级验证使用 HTTPX 替身、可控事件循环时钟、短暂 loopback socket 和临时 SQLite，
覆盖 31/185/590 秒逻辑等待、总预算、读超时/断线、Outbox、续租/心跳/停止、跨会话进展、
配置生产构造路径及 stale claim fencing。这些结果不等于官方桌面版或真实微信验收。

## 21 号任务：独立人工恢复事项

`dispatch_record_id=21` / `message_id=1349` 的既有 `uncertain` 必须保持原状。
修复代码和升级都**不会自动解除它**，也不会自动回填它在断线时丢失的响应。
不得把 `mark-dead`、`retry-approved`、`confirm-success` 嵌入升级脚本或迁移。

后续如需恢复，作为单独人工事项：先由用户核对桌面最终结果和外部操作证据，再按
[现有恢复接口](runtime-recovery.md#pending-or-uncertain-queue-work)选择有证据支持的
操作。`retry-approved` 可能再次执行原操作，不能因“没收到微信”就重试；也不能用
`confirm-success` 伪造缺失的 Hermes 响应或微信投递记录。本轮没有执行这些操作。

## 变更文件索引

| 范围 | 文件 |
| --- | --- |
| 超时定义、加载和生产接线 | `src/cf_agent_gateway/hermes_timeouts.py`、`config.py`、`runtime/dispatch_worker.py` |
| 有限网络等待与诊断 | `src/cf_agent_gateway/hermes/client.py`、`errors.py`、`__init__.py`、`diagnose.py` |
| 事务与过期 claim 安全 | `src/cf_agent_gateway/hermes/service.py`、`models.py`、`result_store.py`、`src/cf_agent_gateway/task/model/store.py` |
| 示例、生成配置、停止宽限期 | `config/config.yaml`、`config/production.yaml`、`deploy/prepare-clean-host.py`、`docker-compose.prod.yml`、`deploy/systemd/cf-agent-dispatch-worker.service`、`.env.example` |
| 回归 | `tests/test_hermes_long_tasks.py`、`test_dispatch_worker_entrypoint.py`、`test_dispatch_worker_runtime.py`、`test_production_deployment.py` |
| 运维说明 | 本文、`docs/runtime-recovery.md`、`docs/troubleshooting.md`、`docs/deployment/hermes-lan.md` |

## 本轮代码验证记录

2026-09-16，本工作树在 Windows / Python 3.12.10 上完成 444 项相关本地回归，全部通过。
范围为全部 `test_hermes_*.py`，以及配置、Dispatch Worker、reconciliation、Admin recovery、
Response/Delivery、心跳/健康、routing、本地替身 V2 E2E 和部署配置检查。
测试保留一条已有 FastAPI/Starlette TestClient 依赖弃用提示；无失败。
`ruff check .`、`ruff format --check .`、`git diff --check` 与仓库 CI 原有的
UTF-8/Markdown 链接校验通过。未运行安装器、恢复演练、VM、生产容器、现场故障注入、
真实模型或微信操作；GitHub 自动门禁状态以 PR 当时检查结果为准。
