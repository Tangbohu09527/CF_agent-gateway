# 2026-10-06：现用 Hermes HTTP 验收与受控恢复前置条件

承接 Gateway `08551b7a926966ea700149bf2a514be23a3037e4`，继续
`feat/wechat-inbound-media` / Draft PR #14。之前库级完整 Agent 的 PDF/PNG
[77/77 记录](2026-10-06-native-agent-media-acceptance.md)保留，没有重复执行。
本轮只核验已经存入 FileBrowser 的样本，不恢复微信，也不处理历史取件任务。

## 现场入口核对

本机配置文件、认证引用、当前进程、TCP listener 和认证 HTTP GET 联合核对：

- 安装源码仍为官方 `0f4a98f87c17007b81500239d0bd5b9574027b73`；8 个涉及
  HTTP、Agent、视觉与文件工具的源码与该提交逐字节一致。这不是整个安装目录的完整性证明。
- 使用本机实际 `.env` 的 `API_SERVER_HOST` / `API_SERVER_PORT`，以及
  `API_SERVER_KEY` 引用；没有猜测端口或把模型供应商 API 当作 Hermes 入口。
  内部地址和绝对路径保存在私有证据，不提交到仓库。
- 2026-10-06 06:10:58 UTC 的 `/health`、`/health/detailed`、`/v1/models`、
  `/v1/capabilities`、`/v1/toolsets` 均为 HTTP 200。服务自报 `0.21.5`，
  `gateway_state=running`，API platform connected；返回 PID 与实际监听进程一致。
  这是当时重新观测，不能外推为持续健康或无人值守自启通过。
- 随后用生产 `HermesClient` 对同一 `/v1/models` 发一次错误 Key 的 GET，得到 401。
  正确引用 GET 200、错误 Key GET 401 只证明此只读路由认证，不等于业务 POST 验收。
- `/v1/models` 公布 `hermes-agent` 路由别名。配置中的实际默认模型为
  `openai-api / gpt-6-astra / codex_responses`；没有 API model_routes 或独立
  api_server toolsets 覆盖。别名不是模型供应商的模型 ID。
- capabilities 表明 `server_agent`、工具在服务端执行、无 split runtime。
  terminal backend 为现有 local；terminal/file/vision 已启用。
- 服务同时暴露现有 FileBridge 读取、创建、入站下载工具及浏览器等工具。
  当前插件的 `consumer_tools=[]`，宿主 middleware 对普通 terminal/read_file/vision
  直接调用 next handler，未发现它拦截本次所需工具。未加载另一个测试插件或修复半升级安装。
  **没有宣称当前服务已隔离插件**：非流式 chat 路由没有每请求缩减工具集的入口，
  请求中的 tools/tool_choice 不能用作权限边界。任务说明也不是服务端权限控制。

现用 HTTP 执行器读取默认 Profile；本次没有伪造 Gateway 的 profile_reference/revision，
没有用请求字段覆盖实际模型，也不据此证明 V2 Profile 身份映射通过。

CFserver 只有用户确认的历史 SSH 地址/用户名以及部署路径，没有本任务可用的认证通道。
用户明确选择 Windows 备用路径；未建立 SSH 凭据或修改授权。
`/opt/cf-agent-gateway/config/production.yaml`、同目录 Compose 和 `.env` 是历史部署引用，
**不是今天已读取的配置**。Gateway 现场配置、API/三个 Worker 镜像与状态、迁移、队列，
以及 CFserver → Hermes 网络/认证均为 **本轮未核对**。仓库模板不能代替它们。

## 正式任务入口与证据规则

[驱动](../../tests/http_agent_acceptance/run.py)直接调用生产
`HermesClient.chat()`；观察子类仅保存请求正文和白名单响应头，实际序列化、认证、
有限超时、禁用代理/重定向、错误分类由原客户端执行。不导入 AIAgent、不替 Agent
下载或调用工具、不启动服务或安装依赖。

每项先排他创建私有 case 目录，保存 session/idempotency intent 和驱动快照，再要求
同 session GET 为 404，然后仅提交一次 POST。失败或超时仅 GET 原 session，禁止
自动重新建会话/重发。旧证据目录不能复用。提示只含自然语言任务、路径和非敏感
HTTPS 说明；Token 只由被测工具在进程内从原引用读取，CA、1 MiB 上限、排他落盘和
账号 Scope 保留，不向模型提供原答案或核验 JSON。

实际会话 messages 用于关联工具调用/结果和最终回答；达到分页上限时不能称轨迹完整。
答案和文件清单冻结后才允许独立程序读取原核验 JSON。HTTP 200、finish_reason 或
Agent 自述都不是单独的成功依据；还需真实工具链、下载摘要和内容核对。

当前官方实现的非流式会话 API 会把图像部分持久化为 `[screenshot]`。因此它可以保留
vision_analyze 调用、下载文件、原生视觉文本投影及最终答案，但不能独立提供
模型供应商请求中的原始图像字节。不得把上一轮库级 HTTPX 观察结果移植成这次的证据，
也不为补证据改用不同请求入口或重复任务挑选答案。

## 本轮执行状态

准备阶段的首次启动命令被自动审批拒绝，发生在进程启动前，没有发送业务请求。
用户随后明确接受现用工具集不能按次隔离的风险，批准两项各一次，并要求先核查 PDF，
发现偏离则不继续 PNG。本次经正常审批执行了 **一个 PDF POST**，没有重试或更换会话。

**PDF 已完成现用 HTTP Agent 自主下载、原生读取和最终回答，内容核对 20/20；
因调用了原驱动禁止的 `skill_view`，整项验收不通过。PNG 按约停止，尚未提交。**

| 实际证据 | PDF |
| --- | --- |
| 入口 | 未改动的 `e6232b1c` 驱动 → 生产 `HermesClient.chat` → 现用 `/v1/chat/completions` |
| 起点 | 新 session 的先行 GET 404；请求体仅一条新用户任务，无旧历史/工具结果/答案 |
| session | `cf-http-8b65ca6869eb40159faab7ec5f22cc04` |
| HTTP completion | `chatcmpl-e74eea0514224f3ba6f9f6ced95d5` |
| 请求/响应保存时间 | 2026-10-06 14:55:44 / 14:57:52（Asia/Shanghai；是本地保存时间） |
| HTTP 与客户端 | 200；`finish_reason=stop`；响应会话与 intent 一致；`client_accepted=true` |
| 完成/失败字段 | 成功响应按官方契约省略 `hermes` 和 Completed 头，无显式 failed/partial/error；不伪造正值 |
| 实际会话 | `source=api_server`，`model=gpt-6-astra`，无 parent；一页 12 条消息完整关联 |
| 下载 | 79083 字节，普通 HTTPS，服务端指定样本只读 |
| 下载 SHA-256 | `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44` |
| 原生读取 | 实际 `read_file` 返回 47 行，`extracted_document=true`、`truncated=false` |
| 内容核对 | 原基准 PDF 正文、表格数值与尾标 20/20；实际提取的 29 条正文全部保留 |
| 执行核对 | 26/27，唯一失败项 `allowed_tools_only`；没有将其豁免 |

实际工具顺序及结果为：

1. `skill_view("ocr-and-documents")`：成功读取通用文档 Skill，偏离原驱动约束。
2. `read_file`：读取本次新建的非秘密 HTTP 引用 JSON，没有读取 Token 文件。
3. `terminal`：普通 HTTPS GET self，确认 `hermes-agent-test` 且非管理员。
4. `terminal`：普通 HTTPS GET 唯一批准 PDF，`xb` 创建带唯一后缀的任务副本，再回读核对摘要。
5. `read_file`：读取刚下载的副本；随后 Agent 自己生成最终回答，并在回答中说明多用了 Skill。

两段真实 terminal 代码均核对 CA 摘要，使用证书链和主机名校验，直连且不跟随重定向；
下载最多读 1 MiB + 1，超限拒绝。Token 仅在进程内进入 Authorization，不在命令参数或
工具输出中回显。未见 FileBridge 专用工具/安装器、Skill 脚本执行、依赖安装、远端业务写入、
服务或配置修改。本机 config.yaml 执行前后摘要一致；没有改动半升级插件。

这次暴露的是**验收提示与现用官方 Skills 提示的兼容冲突**：官方
`agent/prompt_builder.py` 的 Skills 段要求相关任务加载 Skill，而原验收驱动明确禁止。
不能仅靠请求提示建立工具权限边界，也不能把已读取 Skill 默认为满足原验收约束。
它不是专用 FileBridge 插件被调用的证据。后续需明确只读通用 Skill 是否属于允许的任务工具，
使验收契约与现用服务一致；本轮不事后放宽白名单、不改 Profile 或继续第二项。

答案冻结后才独立读取原核验 JSON，摘要仍为
`2b055f8472167755cd60d16e87d24dc18382dd5433b1bf27bb252da275e76765`。
PNG 没有 case，不伪造其结果或运行两例总评分。PDF 通过现有核验器的冻结读取、答案评分和
单例执行评分函数独立核对，明确仅针对 PDF。正文逐行核对只忽略 Markdown 标题、表格分隔符
和空白排版，保留全部文字、数字和标点；初始字面比较与规范化比较报告都保留。

| 私有证据 | SHA-256 |
| --- | --- |
| HTTP response | `e8a77ff29e618a45f0146cf1cc31f3ecb64aa01081a1197782d805179139bb93` |
| 会话工具轨迹 | `86cda04640af76e1a6ae86ac4301e5bc4f9ad4ef6a4cd4c4b395a915a32e3ef1` |
| 最终回答文件 | `06337e9fe9729feb529946d6f826fd9c8d3e643d84ec2cca774e93d3a69a2c6c` |
| 原 case 冻结清单 | `79cfa432dafb028503ed95513eb20a38cb74201862046d541c900ceec8cc4d59` |

副本绝对路径、原始参数/工具结果、原始最终回答及核对报告保存在私有任务目录，不进入 Git。
Windows 本机结果不代表 CFserver 网络、Gateway Dispatch/claim、宿主绑定或微信端到端通过。

## 验收资产验证

准备阶段的 [驱动回归](../../tests/test_http_agent_acceptance.py) **21 项**和
[离线核验器回归](../../tests/test_http_agent_acceptance_check.py) **38 项**通过。
当时连同原 HermesClient 和原库级验收资产回归，本地 **128 passed**；ruff lint/format 通过。
这些均为合成离线证据，不冒充正式 PDF/PNG 结果。唯一警告来自现有 Starlette 的
httpx TestClient 弃用提示，没有为此安装或升级依赖。

只读错误认证探针首次尝试在 Hermes 3.14 generation 中导入 Gateway 时缺少 SQLAlchemy，
发生在网络调用前。改用既有 Gateway Python **3.12.10** 和现有测试依赖后完成该 GET；
没有向 Hermes 安装依赖。HTTP 驱动与服务端解释器分开，实际 listener 的解释器路径为
**3.14.7**；PDF 的实际 terminal 命令也使用已有的该 generation，不能把驱动版本混写成 Agent 版本。

[独立核验器](../../tests/http_agent_acceptance/check.py)需两个 case 的冻结完整性均通过，
才打开原核验 JSON。它复用已有答案评分逻辑，额外验证真实 HTTP 请求、会话工具配对、
下载与原基准大小/摘要，以及原生读取。工具返回中的原生图像标记只算会话投影，
`model_transport_image_bytes` 始终单列为 `unobserved`，本轮 PNG 尚未执行。
没有重复先前已获准且已完成的一次额外离线凭据扫描。

真实 PDF 证据暴露两个离线格式假设错误：原基准用 `scoped_path` 而非 `path`；
Agent 先读本次非秘密接口引用，再读 PDF，不能用所有 read_file 的总数判断原生读取是否发生。
本轮仅修正核验器：兼容原基准路径字段并拒绝冲突，按实际 path/image_url 精确匹配下载目标，
额外读取只允许本次非秘密引用；仍拒绝 `skill_view`。首次 KeyError 记录保留，冻结证据和驱动
没有更改。修正后的核验器合成回归 **55 项通过**，连同原客户端和验收资产共 **145 passed**，
ruff lint/format 通过；这些代码回归不替代上述正式任务结论。

## 下一次受控恢复的前置条件与操作顺序

以下是后续独立授权窗口中的待办，本轮没有执行恢复命令。

1. 由现有现场管理员使用既有交互 SSH/sudo 通道核对真实部署。按
   [恢复手册](../runtime-recovery.md#preserve-evidence-first)保存受保护的 UTC 时间、
   Compose 状态、Controller status、`/health/runtime`。仅取 gateway、worker、
   dispatch-worker、delivery-worker 的配置镜像引用、实际 image ID、运行/健康状态；
   不输出完整 docker inspect、环境文件或含凭据的 Compose 渲染结果。
2. 从现场 YAML 与容器加载结果核对 `hermes.enabled/base_url/api_key_env/model/timeouts`，
   只报告引用和值是否存在，不输出认证值。另只读核对执行 Profile 的 reference/revision、
   真实 provider/model/options 与对应 HTTP 路由。不能用本机默认 Profile 结果替代。
3. 在已存在的 Gateway 容器内运行 `python -m cf_agent_gateway.runtime.startup check`
   核对其自身期望 schema；先确认 `CF_GATEWAY_CONFIG` 指向实际挂载，不能默用默认文件。
   只读记录真实 Alembic revision。用获准只读事务取得
   Message/Admission/Dispatch/Response/Delivery/Checkpoint 的状态计数、最老等待时间和
   uncertain/dead/处理中任务。区分安全待处理与结果不确定；不修改队列或 Checkpoint。
   仓库 head 或 CI schema 不能冒充现场 revision，也不因此运行 migration。
4. 从真实 Gateway 容器复用其配置/环境运行
   `python -m cf_agent_gateway.hermes.diagnose --check-auth`，验证实际网络和双方认证。
   本机已确认现用官方服务提供 `/v1/models`；现场若指向其他入口则先核实契约。
   该检查不调用模型，只证明 models 路由网络/认证，不证明业务 POST 认证或执行。
   需要业务请求时另行批准独立 canary，不能重放历史 Dispatch。
5. 版本/配置/队列完成分类后，才判断是否需要部署或配置差异。`989e6a8`、`08551b7`
   以及本轮新增内容均为验收资产，**HEAD 更新不是重建生产镜像的理由**。
   若现场只需恢复原有文本服务，按现场实际问题制定最小操作；不顺便启用入站附件。
6. 附件启用另需 [宿主契约](../development/inbound-host-binding-contract.md)的真实
   resolve/events/closed、有限 lease、专用执行身份与可信 TLS 验收；现用 `0f4a98f`
   的 create/fork/历史/运行配置/FIFO/超时恢复也需重新验证。真实 FileBrowser 插件配套
   由其独立项目处理，不能把普通 FileBrowser 样本下载当成 Gateway grant 已接通。
   `legacy_runtime_confirmed` 仍须历史运行证据，不能根据这两份样本自动设为 true。
7. 后续明确获准恢复时，先确认外部 WeChat 当前登录状态及 Token Contract；只有确需
   重新登录才按正式流程扫码。Poll/Delivery 只能经 Runtime Controller 的受控 Gate
   操作；Dispatch 按独立恢复边界处理，不能用全量 compose up 绕过分类。
   启动前核实会被领取的既有队列；历史 dead/uncertain 不自动重派。
8. Gate 恢复后用获准新消息核对 Message → Admission → Thread/Profile → Dispatch →
   Response → READY Artifact/Delivery → 微信回执，以及 FIFO、幂等和无重复效果。
   图片/PDF 入站还需实际字节取件和登记，不用文件名或空正文占位冒充。

历史 PDF 持续 pending 仍是独立上游取件阻塞，不妨碍本轮读取已存 FileBrowser 的样本，
也不会因 HTTP 样本读取成功而关闭。本轮不分类、不改正文、不删除/重命名/新建业务目录，
不启停服务、不恢复 Worker、不扫码、不发微信、不改数据库、不合并或发布安装包。
