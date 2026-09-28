# 固定官方 Hermes 的宿主会话兼容性实测

本记录对应官方 `NousResearch/hermes-agent` 固定提交
`4d55ca91656ac5f83e1506679b7f81e0238e5e16`，配套请求依据 FileBrowser 固定
[`2b3894c1` 的要求](https://github.com/Tangbohu09527/CF_filebrowser-enterprise/blob/2b3894c1ae3146a5f6c29084068345f70e6892d9/integrations/hermes-filebridge/GATEWAY_HOST_BINDING_REQUIREMENTS.md)。
没有读取或修改 FileBrowser 本地项目、用户安装的 Hermes、生产配置或凭据。

## 真实执行范围

[`probe.py`](../../tests/hermes_host_probe/probe.py) 在 Gateway 的忽略目录中使用
官方固定源码、独立 venv、空的临时 `HERMES_HOME` 和专用合成 Profile。它运行真实
`APIServerAdapter.connect`、Bearer 认证、HTTP create/fork/messages/model/chat、
官方 SessionDB、AIAgent、插件 loader 和 tool execution middleware。

模型是仅监听回环的 HTTP/SSE 测试替身；插件只是使用公开注册接口的 observer，
**不是真实 FileBrowser 插件，也不包含第二套下载器**。没有替换、猴子补丁或编辑
Hermes API、agent、数据库、工具分派和生命周期代码。源码 HEAD 和十个关键边界
文件的 Git blob SHA 在运行前检查。

子进程只继承必要的 OS 环境变量，Profile、HOME 和临时目录都指向测试目录，
关闭项目和 bundled 插件扫描。审计钩子禁止非回环网络、外部 DNS、任意子进程和
隔离根目录之外的文件读取；只单独允许标准库/psutil 所需的明确 OS 元数据。
不加载真实 API key。随机合成 API key 不进入模型请求、会话数据库、事件或日志，
探针对整个临时 Profile 文件树执行字节检查。成功输出只含有界结构化结论。

## 实测结论及对原提案的修正

| 实际操作 | 观察和集成要求 |
| --- | --- |
| 无 Bearer 访问 session API | 401，尚未触发 agent |
| GET 不存在的 session | 404；不能把此结果解释为允许创建空历史 |
| 指定 ID create；再次或并发 create 同一 ID | 首次 201、重复 409；并发一项 201、一项 409 |
| 两轮原 session/chat，再 fork | 新 child 具有正确 `parent_session_id`，复制的 8 条消息在角色、正文、工具调用关联上完全一致；数据库中的 system prompt 保留 |
| fork 的源 session | `ended_at` 有值，`end_reason=branched`；fork 会改变源 session 生命周期，不能只当作只读复制 |
| fork 的运行配置 | `model` 保留，但 `model_config` 只剩 `_branched_from`，丢失 provider、model options 及确认过的 model lock |
| GET session 的公开投影 | 只提供 `has_model_config` / `has_system_prompt`，不提供完整内容；Gateway 不能声称通过这个 GET 验证了隐藏配置全文 |
| POST child/model，再走原 OpenAI route | 即使持久锁已恢复，实际模型仍用 Profile global model；仅写 `/model` **不足以保留 OpenAI 请求的运行配置** |
| 原 OpenAI route 显式传批准的 model/provider/model_options | 实际使用指定模型；`reasoning.enabled=false` 到达模型为 `reasoning_effort=none`。custom provider 不转发 `service_tier=priority`，不把该字段声称为实测生效 |
| 原 session/chat | 实际尊重持久模型锁，但该端点没有原 OpenAI 的幂等调用包装；响应构造也不保留 failed/partial/completed 信号。不能为继承锁而直接替换原请求端点 |
| 同一 OpenAI 请求使用同一幂等键重试 | 返回同一业务结果，没有再次调用模型 |
| 旧 branch parent 查询消息 | 仍是 parent；分叉 child 不属于 parent 的压缩 continuation tip |
| 压缩前旧 ID 查询消息及 OpenAI 执行 | `/messages.session_id` 返回真实 tip；实际 agent/middleware 和新消息使用 tip，但响应 `X-Hermes-Session-Id` 仍回显旧请求 ID |
| fork 提交后丢失响应 | 客户端真实超时；按预分配的同一 child ID 查询可确认父子和消息，重复 fork 返回 409；没有创建新随机 ID |
| fork 最后一步标题冲突 | 返回 400，但 child、parent 的 branched 状态和消息已持久化；错误状态不能证明“没有发生修改” |
| 模型返回不可重试 400 | Hermes HTTP 200，`hermes.failed=true`；没有对应 `on_session_end`，复现原配套中的生命周期缺口 |
| 官方工具 middleware | 实际 `task_id == session_id`，可以作为后台 resolve 的执行相关字段；本探针不把它们单独当作来源认证 |

压缩 fixture 通过官方 `try_acquire_compression_lock` 和事务性
`publish_compression_child` 建立；之后的 HTTP resolver、agent 和 middleware
全部真实执行。**这证明已有压缩 lineage 的 tip 行为，不宣称本轮运行了真实模型
自动摘要或实际生产压缩。**

最小等价修正是保留普通文本和原 OpenAI 请求行为，对附件 claim 专用 child 使用
批准 Profile 中明确的 model/provider/options，验证 `/model` 接受后还要在附件请求
中显式传递这些批准值。它们必须来自 Gateway 配置和持久绑定，不能取自模型、宿主
请求或不可信 metadata。缺少配置、源历史未知或不能证明兼容时，功能继续关闭并
失败可见；不能悄悄退到默认模型或空会话。

`/model` 回执只能证明该端点接受指定配置，具体 provider/model 对选项的支持仍需
对应候选运行环境验收。HTTP 投影没有提供完整隐藏配置，所以无法对任意旧会话
宣称自动继承所有 provider 锁、系统提示和 Profile 私有配置。

## tip、FIFO 和不确定结果的边界

Gateway 的逻辑线程和企业身份不变。只有当前 claim 的持久预分配 ID 才能用于
create/fork；禁止请求超时后换随机 ID。fork 前需查询已确认源 session 的完整分页
消息，确认消息接口解析出来的真实 tip，再取得这个 tip 自身的元数据。旧 header 的
回显不等于真实 tip；也不能对过时 parent fork 后声称复制了最新历史。

fork 后必须核对 child 的 ID、父关系、来源、模型和消息快照。fork 不是单一事务，
仅存在 child 或仅收到 409 都不足以通过。快照缺少、分页不完整、父源已变化或只
完成部分步骤时，保留原预分配 ID 和不确定状态，不自行清理/补造历史或执行模型。
lost-response 探针证明“完整已提交的 child 可以通过查回确认”，不证明任意部分
fork 都能安全自动修复。

只在 Gateway 原有线程 FIFO 和 claim fence 内执行上述动作。官方 session API 的
同 ID create 原子性不是 Gateway Dispatch FIFO；跨线程并行、线程内有序、旧 claim
失效与 closed/lease 屏障必须由 Gateway 自身的数据库和 HTTP 集成测试证明。
分叉后发生模型失败或不确定响应时也不能自动跳回 parent，忽略已有 child。

## 重放方法与结果

隔离环境准备一次：

```text
git clone --no-checkout https://github.com/NousResearch/hermes-agent.git .pytest_cache/hermes-host-probe/source
git -C .pytest_cache/hermes-host-probe/source checkout --detach 4d55ca91656ac5f83e1506679b7f81e0238e5e16
python -m venv .pytest_cache/hermes-host-probe/venv
<venv-python> -m pip install -r tests/hermes_host_probe/requirements.txt
<venv-python> -I -B tests/hermes_host_probe/probe.py --hermes-source .pytest_cache/hermes-host-probe/source
```

`<venv-python>` 在 Windows 为 `.pytest_cache/hermes-host-probe/venv/Scripts/python.exe`，
Linux 为 `.pytest_cache/hermes-host-probe/venv/bin/python`。

2026-09-28 Windows Python 3.12 隔离运行：上述真实 API 探针成功，结构化 `ok=true`；
这是一个显式入口内的一组断言，不虚报成几十项独立 pytest。
Ruff lint 和 format check 通过。Linux/Windows 的
[`Fixed Hermes host compatibility`](../../.github/workflows/hermes-host-compatibility.yml)
是独立 CI，最终结果必须核验本轮远端 HEAD 对应的运行，不能用本地 Windows 成功代替。

开发时首次探针失败来自测试引导的系统元数据许可、模型替身缺少 SSE 和 observer
签名与真实接口不符，均修正在探针内，没有修改官方实现或关闭断言。官方对 Windows
自带 SQLite 的 WAL 版本警告及降级仍保留；没有升级用户环境来绕过它。

这些结果不表示实际 AI 主机、真实 FileBrowser HostBridge、生产 TLS 或附件下载
已连通。插件接线、批准入口隔离、真实 Profile 选项、撤销监听及落盘仍需对应项目
消费定版契约后的验收。PDF 上游长期 pending 仍是独立阻塞。
