# 2026-10-11 Hermes 环境监听覆盖修复

任务从固定发布 `f9c601cba25cbc5459bb03519d1954ae55995ded` 接续。
新工作树 `59ae/CF_agent-gateway` 初始 detached HEAD 与固定发布一致、无差异，随后建立
`codex/fix-hermes-env-loopback`。原 `2d15` 工作树及其 `.task-artifacts` 未修改。
远端初次核对 PR #14 为 Draft/Open，head 为 f9；本轮只推独立修复分支。

## 根因与变更

旧 helper 错误地把 `API_SERVER_HOST` 视为 YAML 的缺省回退。固定官方源码在 YAML
后执行 `_apply_env_overrides()`，非空环境值写入 `extra.host`。官方 CLI 会从 Hermes
home 的 `.env` 加载该值。因此交接中的 YAML=127.0.0.1 与实际 LAN 监听并不矛盾。
返回插件在发现原生非 loopback 监听后拒绝附加 TLS，保留该安全检查。

真实官方 CLI 的隔离启动还暴露了第二层兼容问题：`install_truststore()` 全局替换
`ssl.SSLContext` 后，Windows truststore 0.10.4 在服务端握手中错误验证不存在的客户端
证书，抛出 `Peer sent no certificates to verify`。普通直接构造 adapter 的测试没有
经过官方 CLI 的这一步。该问题是在修复 HOST 后的合成环境观察到的，不冒充真实现场
已经发生的第二故障。

- `enablement.py`：把同目录 `.env` 的 HOST 纳入正式 plan 和现有私有 journal；
  仅精确替换 HOST 值，端口只核验，其他字节不重新序列化。重复、歧义、插值和不支持
  的编码拒绝；公共 plan/错误不含凭据，原字节仅留本机私有备份。
- `site_windows.py`：补核官方 CLI/env/config 源码；认证和监听引用采用严格解析；
  按实际环境优先级核对原入口。规划/应用/首次启动检查 Process/User/Machine 冲突；
  已有 intent 的失败恢复仍可继续只读核对。要求 HTTP/TLS 两个监听均实际存在且属于
  同一 PID/创建时间/执行器，认证探测后再次核验。已应用 journal 路径和审阅后文件
  漂移也受约束。
- 插件使用局部原生 SSL context 兼容官方进程的 truststore 注入，并保持显式 CA 的
  信任边界；不解除全局注入，不关闭 TLS/主机名验证或降低 TLS 版本要求。插件 bundle
  纳入新计划安装，官方核心、服务器业务逻辑和业务协议未改动。
- [操作者恢复手册](../deployment/hermes-env-loopback-recovery.md) 专门从“两端已应用、
  Windows 已启动但验收失败”状态接续。旧入口停回/回退后使用全新 deployment/release，
  不伪造新旧回执，不重签证书，不删除原 intents/journal。

## 自动回归证据

新工作树独立 Python 3.12.10 `.venv`，只安装项目开发依赖；不借用或修改真实 Hermes
配置、安装或凭据。针对性组：

```text
python -m pytest -q
  tests/test_return_bridge_enablement.py tests/test_return_bridge_listener_env.py
  tests/test_return_bridge_plugin.py tests/test_return_bridge_scope.py
  tests/test_return_site_windows.py tests/test_return_site_server_entry.py
  tests/test_return_site_server_assets.py tests/test_hermes_tls.py
  tests/test_hermes_listener_probe_audit.py
```

最终整组 **317 passed / 1 skipped / 15 warnings**，74.78 秒。
唯一跳过为 Windows 缺少创建 symlink 权限的作用域链接测试，CI Linux 负责该平台语义；
未禁用安全断言。警告来自既有 aiohttp AppKey 与当前 Starlette/httpx 兼容提示。
包含 Windows 环境名大小写/Unicode uppercase 别名、认证多行伪键、审阅后 `.env`
漂移、真实 truststore 注入子进程及探针审计失败闭合用例。Ruff check、354 个 Python
文件格式检查、394 个文本候选/47 个 Markdown 文件 UTF-8 和本地链接检查、diff 空白
检查均通过。操作者 PowerShell 块只做语法解析，没有执行现场命令。

配置测试包括 BOM/CRLF/LF/CR、原注释和凭据不输出、重复/歧义字段、原件漂移、
原字节备份、真实子进程写到 `.env` 后硬退出和跨进程恢复、原 12 项 bundle/旧 journal
保留、逐层回退。Windows 入口测试使用明确的管理替身，覆盖两端 plan/release 绑定、
监听/认证前后变化、一次性管理命令、失败 start 的只读重试与身份绑定 quiesce。
这些替身测试不等于运行真实 Windows 服务或 CFserver。

## 隔离与现场边界

官方探针使用新检出的固定源码、独立 Python 3.14.7 依赖和随机合成认证、合成 CA/证书，
仅请求只读 `/v1/models`。[探针源码](../../tests/hermes_listener_probe/probe.py) 使用真实
官方 Windows `gateway start/stop`，通过官方管理入口产生 detached `gateway run`。
不是直接实例化 adapter。12 个关键官方源码文件校验固定 Git blob，执行前后工作树干净。
没有调用真实安装的 `hermes.exe` 包装器。

本地成功记录为新工作树 `.task-artifacts/listener-probe-10/report.json`，退出码 0：

| 检查 | 实际结果 |
| --- | --- |
| 合成旧状态，YAML loopback / `.env` wildcard | 原生 HTTP 实际监听 `0.0.0.0:53247`；插件明确拒绝，TLS 未监听 |
| 通过计划持久修复后首次启动 | PID 24656 同时监听 loopback HTTP 53247 / HTTPS 53248 |
| 官方管理停止再启动 | 新 PID 31240 同时监听上述端口；`.env` 字节仍为持久修复结果 |
| 两轮只读鉴权 | 正确 CA/主机名/认证的 HTTPS GET 200；错误认证 401 |
| 两轮证书拒绝 | 错误 CA 与错误主机名均为 `CERTIFICATE_VERIFY_FAILED`，不把普通断连算验证成功 |
| 隔离边界 | 全部运行时子进程审计生效，0 次运行时对外连接尝试、0 次模型调用 |
| 留存与清理 | 20 份日志/审计的合成凭据扫描 0 命中；合成进程全部经官方管理停止 |

随机端口、PID 和合成配置不适用于现场。BOM、CRLF 和无关秘密字段精确保持；该探针
纳入新增 Windows CI job，仅上传不含凭据的 `report.json`，不上传私有配置/证书/journal。
另用无网络 MemoryBIO 在固定 truststore 上复现了服务端错误要求客户端证书的问题。

Process/User/Machine 冲突的组合覆盖使用测试替身，不写用户或机器环境。

未连接 CFserver，未改变真实配置/安装/凭据，未启停真实 Hermes，未调用业务模型、
上传业务附件、发微信或打开 Gate。服务器 Docker/Controller 外层仍为测试替身，
后续真实新镜像尚未构建，现场双端恢复与正式附件功能尚未验收。

PR #14 的安全接回方式与完整 PowerShell 5.1 / Linux 命令均在恢复手册；本轮不合并、
不 force push、不发布 Tag，不推进旧工作树的本地分支。远端 CI 与最终提交状态以交付时
对精确 SHA 的查询为准，历史 CI 不冒充本轮结果。
