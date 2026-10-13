# ACK02 HTTP 403：隔离复现与安全诊断

日期：2026-10-13。起点为 `d9bae61a043f1be4303e5c23661ec51904d5e708`，
分支 `codex/fix-artifact-return-403`，PR base 为 `codex/fix-hermes-env-loopback`。

## 结论与证据边界

**没有复现 ACK02 的生产 403，也没有确定生产根因。** 指定旧、新官方 Hermes
的独立环境均能完成带完整返回授权的普通文字、PDF、PNG 请求。不能据此将现场
403 归因于升级、密钥、Profile、历史时间或目录权限，也不能称生产已修复。

已证实并修补的是诊断缺口：返回中间件多个拒绝分支只向外返回通用错误，Gateway
又将其归约为 `hermes_api_error:http_403`，原实现不保留安全的内部拒绝原因。
本次业务校验保持原样，只新增固定枚举阶段/原因和随机请求关联号。

现场 ACK02（用户附件证据）：Message 4166 已入库、准入 allowed；Dispatch 42
四次 403 后 dead，无 Response、Delivery。ACK01 首窗口/游标漏收是另一个未解决事项。
没有重派 42、发送 ACK03、打开 Gate、扫码或连接 CFserver。本次也没有操作正式
Hermes 服务、数据库、凭据、旧 request/plan/journal，未升级/回退 Hermes。

## 隔离方法

- 独立官方检出固定为 `0f4a98f87c17007b81500239d0bd5b9574027b73` 和
  `427d605d4985c2d49a795493ef6c5d66413057d1`；逐一验证十个 HTTP、插件、
  hook/executor 相关源码 Git blob。后者为本次新兼容性记录，不修改旧部署证明。
- Gateway Python 3.12 与 Hermes Python 3.14.7 使用本工作树内独立 venv。
  Hermes HOME、USERPROFILE、临时目录、SQLite、工作目录、台账和测试密钥独立。
  TLS/模型/接收端均为随机 loopback 端口，不使用 8642/18642/18444。
- 正常准入创建 Message/Dispatch，经实际 `HermesDispatchService → issue_context →
  HermesClient` 发出非空 messages、会话头及返回授权；官方 loader 加载真实插件，
  实际原生 handler、Agent、pre_llm_call 与 tool_execution 执行。
- 确定性本地模型仅提供协议响应；文件由测试准备函数复制进新任务目录，正式返回
  工具经 HTTPS 上传到真实 Gateway API，由真实持久化、封口、Delivery worker
  交给 loopback 微信协议替身。PDF/PNG 是合成字节，不涉及业务原件/付费模型。
- V2 用正常 Profile/Conversation/绑定存储接口及准入创建 default/revision=1
  任务；已有会话用两个分别准入、成功执行的文字任务建立和续接，不修改数据库绕过。

## 修改

| 文件 | 行为 |
| --- | --- |
| `src/cf_agent_gateway/hermes/return_bridge/plugin.py` | 新增固定枚举 stage/reason 和随机 UUID；外部响应、状态码、鉴权及全部拒绝条件不变 |
| `tests/test_return_bridge_diagnostics.py` | 拒绝路径、未知异常脱敏、断连、关闭作用域及随机关联号回归 |
| `tests/hermes_return_probe/probe.py` / `README.md` | 双版本源码校验、default Profile、V2/既有会话完整路径 |
| `.github/workflows/hermes-host-compatibility.yml` | 返回探针扩展为双 SHA × Windows/Linux；历史其他兼容任务保持 |

日志只有 `stage`、`reason` 和本次生成的随机 `request_id`，不输出请求头、正文、
密钥、授权、会话、文件内容、路径、URL、异常字符串或 traceback。该 ID 不是
Dispatch ID，不能跨系统直接关联。`context/invalid_return_context` 仍汇总解码、
schema/session/时效/字段形状检查；它定位到校验族，并不独自证明具体字段根因。
签名仍由 Gateway 边界验证，Hermes 中间件先验证调用方身份，再做一致性检查；
Profile 隔离、过期、一次性消费、线程/任务绑定及关闭作用域均保留。

## 本地验证

以下为隔离结果，不能替代正式微信 ACK/PDF/PNG 验收。

| 官方 Hermes | legacy 文字/PDF/PNG | V2 文字/PDF/PNG | V2 已有会话文字 |
| --- | --- | --- | --- |
| `0f4a98f` | 通过 | 通过 | 通过（2 个分别准入任务、相同会话） |
| `427d605` | 通过 | 通过 | 通过（2 个分别准入任务、相同会话） |

legacy 六个用例均返回 200 和匹配 ACK；文字不上传 Artifact；PDF/PNG 上传 200、
持久化 READY、封口成功、替身投递成功且字节相同。每个用例的私有证据扫描均为
零凭据匹配。实际端口由绑定的 socket 决定，Gateway 通用启动日志里的配置端口
不是本探针的监听地址。

14 个完整链路场景共 16 次业务 POST，全部 200/ACK 匹配。
[脱敏结果](2026-10-13-artifact-return-403-results.json) 保存各场景源码/插件及私有报告
摘要，不含会话、正文或凭据。V2 已有会话报告来自
`.pytest_cache/official-probe-v2-{old,new}-established-a`，其余来自新建的
`.task-artifacts/{old,new}-*`，未覆盖旧工作树或现场材料。

受影响回归：scope 57 passed / 1 skipped（Windows symlink 权限）；HTTP、
client、integration、plugin、enablement、site_windows、probe audit 合计
187 passed；诊断完整用例 26 passed。临时目录/loopback 被执行沙箱阻断的初始尝试
不计通过；使用同一隔离边界的授权执行后完成上述测试。

诊断前后证据：把 d9 的 `src` 归档至新私有目录，运行新增 wrong-profile 用例，
实际获得相同通用 403，但日志断言失败（0 条，而非预期 1 条）；当前代码通过同一
用例，记录 `profile/profile_mismatch`。这是诊断缺口的失败前/修复后证明，
**不是 ACK02 曾因 Profile 不匹配而失败的证明**。

可重放命令（当前工作树；输出目录必须未存在）：

```powershell
$env:TEMP = Join-Path $PWD '.task-artifacts/pytest-temp'
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
$gatewayPython = '.task-deps/gateway-venv/Scripts/python.exe'
$hermesPython = "$PWD/.task-deps/hermes-venv/Scripts/python.exe"
$hermesPackages = "$PWD/.task-deps/hermes-venv/Lib/site-packages"
& $gatewayPython tests/hermes_return_probe/probe.py `
  --hermes-source "$PWD/.task-deps/hermes-new" `
  --hermes-commit 427d605d4985c2d49a795493ef6c5d66413057d1 `
  --hermes-python $hermesPython --site-packages $hermesPackages `
  --output "$PWD/.task-artifacts/new-v2-established-REPLAY" `
  --kind text --routing v2 --established-session
# 两个 SHA 各运行 text/file/image；单次路径移除 --established-session。
# legacy 路径使用 --routing legacy；每次指定全新的 --output。
& $gatewayPython -m pytest tests/test_return_bridge_scope.py `
  tests/test_return_bridge_plugin.py tests/test_return_bridge_diagnostics.py `
  tests/test_artifact_return_http.py tests/test_artifact_return_integration.py `
  tests/test_hermes_client.py tests/test_return_bridge_enablement.py `
  tests/test_return_site_windows.py tests/test_hermes_return_probe_audit.py `
  -q --basetemp .task-artifacts/pytest-REPLAY
& $gatewayPython -m ruff check src/cf_agent_gateway/hermes/return_bridge/plugin.py `
  tests/test_return_bridge_diagnostics.py tests/hermes_return_probe/probe.py
```

## 唯一剩余的最小现场取证方案（本轮未执行）

缺失的是**实际失败请求的响应来源及运行中拒绝分支**。磁盘配置、空消息 400、
GET 200、孤立 SQLite 成功以及当前时钟均不能补足这一证据。ACK02 原始响应未保留，
不能通过重派 dead42 重建同一次历史事件。

后续若另行批准，仅做一次受控证据事务：

1. 审核新 Windows 诊断候选及独立的 `427d605`/config-v50/解释器/插件来源证明，
   确认日志接收正常；只替换受管插件的
   `lib/cf_agent_gateway/hermes/return_bridge/plugin.py`，保留原文件和回退摘要。
   需受控重启才能加载新代码，不在线 monkey-patch，不触碰旧计划。
2. 保持收发控制由原 Controller 管理。在另行授权的维护窗口安排一个新普通文字
   任务，通过正常准入和真实 Gateway 请求链；不使用合成生产授权，不重派 42，
   不在本轮发送任何 marker。保持窄时间窗内只有这一项待观察任务。
3. 只收集 UTC 窗口、HTTP 状态及固定枚举诊断、随机关联号，以及运行版本/模块来源
   和选定 Profile 匹配与否的非敏感证明。不抓包、不保存请求/响应正文或请求头。
   若无诊断，先核实代码实际加载与日志管道；缺日志不能直接断言是上游拒绝。
4. 若仍失败，按观察到的阶段安排下一项定向检查；不据没有复现而改校验。

该事务会写新诊断和独立部署回执；实际请求可能创建台账/工作目录/会话/数据库记录、
消费一次性授权，并可能调用当时配置的模型。因此它**需要新的生产授权**，不是
“只读且零模型”的探测。本轮不要求立即执行。既有队列会立即重试四次的风险保留，
没有重写队列状态机；一个任务不保证只产生一个 HTTP 尝试。

## 最小后续部署与仍未验证事项

本次没有 Gateway 请求/签发/存储运行时代码变化，单独部署诊断不需要 CFserver
新镜像。Windows 实际运行时仅上述 `plugin.py` 有变化。旧 `site_windows` 和
`enablement` 仍固定旧 Hermes 证明，并拒绝既有 bundle 冲突；不能直接套用旧
request/plan/journal，也不能修改常量或摘要假装 v50 已被旧记录批准。必须单独
评审新版本诊断安装/回退记录后部署，不重做已经完成的旧恢复。

本轮隔离通过不证明正式 listener 加载了相同模块、采用相同运行配置或产生过成功
业务结果。现场完全未变，真实微信 ACK/PDF/PNG 均未完成；ACK01 仍独立保留。
最终 commit、PR 及 CI SHA/run 见 PR 验证记录，不能用仅有本地结果声明远端通过。
