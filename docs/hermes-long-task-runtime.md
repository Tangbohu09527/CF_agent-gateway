# Hermes 长工具任务：等待、回传与升级边界

> 状态更新：2026-09-17。PR #11 已合并；现场新镜像已核对并完成一次分钟级响应持久化及微信回传验收。
>
> 具体结果见[2026-09-17 实机验收](validation/2026-09-17-hermes-long-task-acceptance.md)，当前部署摘要见[生产状态](production-status.md)。下文代码级验证、历史升级前提和现场验收分层记录，不将“本轮未部署”的旧开发结论当作当前状态。

本修复针对 Gateway 在约 30 秒后放弃正常 Hermes 工具调用的故障。只改变 Gateway
请求等待、相关数据库事务和租约安全边界，继续使用 Dispatch → Response → Delivery
Outbox；没有增加新任务平台、守护进程、查询结果 API 或微信直发路径。

## 实际配置

生产 `run_dispatch_worker` 将 `settings.hermes.timeouts` 传给 `HermesClient`。
`load_settings`、`config/config.yaml`、`config/production.yaml` 及
`deploy/prepare-clean-host.py` 生成的配置使用相同默认值。旧 YAML 缺少整个
`timeouts` 或其中部分字段时使用默认值。启动日志打印生效值，不打印密钥；配置变更在 Dispatch Worker 下次启动时生效。

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
| `read_seconds` | 600 | `0 < x <= 3600` | 等待下一段响应数据的上限，不是总时长 |
| `write_seconds` | 15 | `0 < x <= 120` | 写入下一段请求数据的等待上限 |
| `pool_seconds` | 5 | `0 < x <= 120` | 等待 HTTP 连接池名额的上限 |
| `execution_seconds` | 600 | `0 < x <= 3600` | 从连接/发送到完整响应体的一次 HTTP 请求总预算 |

值须为有限数值，允许小数；零、负数、布尔值、字符串、null、NaN、无穷大、越界值及未知字段均拒绝。
总预算不包括入队等待和返回后的解析/落库。read 短于 execution 时网络静默可能先触发 read 超时；持续收到字节不能延长 execution 总预算。
默认可等待接近 10 分钟，配置上限 60 分钟；仍受其他阶段及 Hermes/代理限制，不保证所有工具在预算内完成。

同步 Worker 使用每次请求独立的异步 HTTPX 客户端与事件循环。
`asyncio.timeout` 取消网络等待，HTTPX 退出并关闭连接后返回错误；没有通过
`Future.result(timeout=...)` 遗留后台 HTTP 调用。请求间不复用连接池，Worker 并发名额仍是主要限制。
停止 Gateway 等待不等于取消 Hermes 模型、工具或已经发生的外部操作。

`hermes.diagnose` 保留显式短 `--timeout`，不继承业务 10 分钟预算；连接/写入/连接池各不超过 120 秒。
兼容的 `HermesClient(timeout=...)` 只覆盖分阶段等待，总预算由 `timeouts` 控制。
`runtime.wechat` 兼容工厂不实例化 Hermes；业务调用由独立 Dispatch Worker 执行。
安装验收的合成协议探针有独立短预算，不等于实机业务验收。

## 长调用与安全语义

- 派发前复制输入并提交准备事务，释放数据库连接和线程行锁。
- 续租以 `lease_seconds / 3` 独立运行，心跳独立更新；同线程 FIFO。默认并发 4，显式为 1 时其他会话等待名额。
- 真实完整响应返回后，在同一事务内校验 claim token/未过期租约、推进会话标识、保存 DispatchResponse 并标记成功，之后进入 Response/Delivery Outbox。旧 owner 迟到结果不能越过 fencing。
- HTTP 阶段超时使用 `hermes_timeout_error`；总预算耗尽使用 `hermes_execution_timeout_error`；连接中断/超时进入 `uncertain`，不自动重派。
- 进程丢失或续租失败后，过期 `running` 在下次 claim 扫描转 `uncertain`，保留次数与线程阻塞；不接管重跑，也不因重试耗尽自动 `dead` 解锁。即使不能确认请求已发出，也采用保守语义。
- SIGTERM 停止领取，等待活动调用在有限预算内结束，期间续租和心跳继续。Compose Dispatch `CF_GATEWAY_DISPATCH_STOP_GRACE_PERIOD` 默认 `3660s`，systemd `TimeoutStopSec=3660s`，覆盖最大预算并留 60 秒落库余量。更短的运维停止时限仍可能强杀并留下不确定结果；数据库故障不由 HTTP 预算解决。

## 断线/重启恢复不在本轮承诺内

现场使用外部 Windows Hermes Desktop，已安装 Desktop 版本/构建号尚未记录。
回复中报告的 Python 版本不是 Desktop 构建号，官网当前页面也不能证明现场版本。
本轮没有核实该版本的结果查询、取消、SSE 心跳或持久幂等重放能力。
HTTP 连接丢失且 Gateway 未持久化响应时，不能宣称会自动从 Hermes 找回结果。

现有 reconciliation 仅修复 Gateway 已保存的 DispatchResponse 后续 Response/Delivery 缺失，不查询 Hermes。
后续先由现场保留原始工具证据和 Desktop 构建号，再只读核对官方能力；未核实前不重复派发有外部副作用的操作。

HTTP 阶段语义见 [HTTPX 官方文档](https://www.python-httpx.org/advanced/timeouts/)；取消机制见
[Python 3.12 asyncio timeout](https://docs.python.org/3.12/library/asyncio-task.html#timeouts)。

## 旧生产 Release 升级与回滚

以下保留升级前历史前提及复用规则，不是要求对已完成现场重复部署。
PR #11 开发时已知旧代码为 `f36c798294368263433f6132366ac9a864d9482b`、旧镜像为
`sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1`。
修复分支起点 `481c693e2297dd9d223c401f2a92fd92e762151d` 还包含其他主线变化；PR diff 不等于整个升级 diff。
修复不新增迁移；代码比较旧/新 head 均为 `20260823_04`，迁移实现和 `database.py` 未变。

1. 后续发布应选择经审查的固定制品，记录代码来源、Image ID/registry digest 的准确类型、配置挂载及数据库 revision；按运维流程保留备份和旧制品。
2. 按[生产升级流程](deployment/production.md)关闭入口并排空可完成的调用，先记录旧 `uncertain` 基线，不把人工恢复嵌入升级。
3. 保留现场配置、凭据、身份/路由、数据库及数据；只合并超时与停止宽限期并核对启动日志。不得用干净设备安装器覆盖现有现场、改指向空库或删除卷。
4. 核对 Gateway/Dispatch 和 Controller，再做限定场景验收。不要因 Gateway 升级重装 Hermes、切换 CLI/WSL、重建微信入口或操作 FileBrowser。
5. 回滚应先停止新派发、排空并保全不确定状态，再恢复固定旧镜像及配置，保留业务库。本修复不需要 schema downgrade。不能用旧备份覆盖新业务写入；旧 30 秒限制及过期 running 接管行为可能随旧代码恢复。仍有 running/uncertain 时不得借回滚绕过人工核查。

## 用户人工验收清单

2026-09-17 已完成的限定结果见[验收记录](validation/2026-09-17-hermes-long-task-acceptance.md)：
旧只读积压任务回传通过；新的分钟级任务数据库派发耗时超过 90 秒，响应、一次投递及微信实收匹配。
这是当前状态，不能继续标为全部“尚未执行”；也不能把下面所有专项一并勾选完成。

| 专项 | 本次状态 |
| --- | --- |
| 分钟级任务等待、持久化及微信回传 | 已通过本次限定记录 |
| 原始工具调用/标准输出/退出码独立复核 | 未审阅 |
| 长任务期间心跳与续租全过程、另一线程并发、同线程 FIFO | 未完成独立生产专项 |
| 接近 600 秒、断线、执行中重启、完整恢复 | 未完成本次专项 |
| 新制品来源证明、离线归档及恢复/回滚演练 | 未独立核验 |

新验收须使用经授权且没有未处理 `uncertain` 阻塞的会话；每项记录消息/Dispatch 标识、耗时、次数、响应和投递结果。
不能绕过线程保护；21 号的独立带审计恢复已完成后，才推进该线程的后续任务。

## 21 号任务：独立人工恢复事项

历史 `dispatch_record_id=21` / `message_id=1349` 已有 `mark_dead` 审计，记录 `uncertain → dead`；
具体日期/审计编号见[现场记录](validation/2026-09-17-hermes-long-task-acceptance.md)。不再把它写成当前尚未处理，也不重复执行恢复。
修复代码和升级没有自动解除该记录，未自动找回缺失响应；没有伪造成功回复或重派它。

一般规则仍是：先核对外部证据，再通过[正式恢复接口](runtime-recovery.md#pending-or-uncertain-queue-work)选择一次动作。
`retry-approved` 可能再次执行原操作，不能因微信未收到就重试；`confirm-success` 不能伪造 Hermes 响应。
保留错误与不可变审计，不删除队列或 Checkpoint。人工恢复不得嵌入迁移或一般升级脚本。

## 变更文件索引

| 范围 | 文件 |
| --- | --- |
| 超时定义与生产接线 | `src/cf_agent_gateway/hermes_timeouts.py`、`config.py`、`runtime/dispatch_worker.py` |
| 有限等待与诊断 | `src/cf_agent_gateway/hermes/client.py`、`errors.py`、`__init__.py`、`diagnose.py` |
| 事务与过期 claim | `src/cf_agent_gateway/hermes/service.py`、`models.py`、`result_store.py`、`src/cf_agent_gateway/task/model/store.py` |
| 配置与部署 | `config/config.yaml`、`config/production.yaml`、`deploy/prepare-clean-host.py`、`docker-compose.prod.yml`、`deploy/systemd/cf-agent-dispatch-worker.service`、`.env.example` |
| 回归 | `tests/test_hermes_long_tasks.py`、`test_dispatch_worker_entrypoint.py`、`test_dispatch_worker_runtime.py`、`test_production_deployment.py` |
| 运维 | 本文、`docs/runtime-recovery.md`、`docs/troubleshooting.md`、`docs/deployment/hermes-lan.md` |

## PR #11 代码验证记录（2026-09-16）

PR 原记录：Windows / Python 3.12.10，444 项相关本地回归通过，含 `test_hermes_*.py`、配置、Dispatch Worker、
reconciliation、Admin recovery、Response/Delivery、心跳/健康、routing、本地替身 V2 E2E 和部署配置检查。
有一条已有 FastAPI/Starlette TestClient 弃用提示；Ruff lint/format、diff check、UTF-8/Markdown 链接通过。
31/185/590 秒场景使用替身/可控时钟等，不是实际官方桌面版的长时验收。
当时未运行安装器、恢复演练、VM、生产容器、现场故障注入、真实模型或微信；后续现场结果由单独的 September 17 记录补充。
GitHub CI 必须按具体提交/run 查询，本次文档变更不冒用旧测试结果。
