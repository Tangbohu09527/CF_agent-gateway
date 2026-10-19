# 固定官方 Hermes 原生 HTTP MCP 候选边界

日期：2026-10-20（Asia/Shanghai）。这是隔离候选实验记录，不是生产启用或微信实收记录。

官方源码固定为 `NousResearch/hermes-agent@427d605d4985c2d49a795493ef6c5d66413057d1`；
FileBrowser 固定为 `3d556786068994826b8105ff4a1d11572b84b1b1`。
源码由官方 Git 仓库下载到独立任务目录；Windows 使用 `git -c core.longpaths=true status --porcelain`
核对检出无差异。没有读取正式 Hermes HOME、服务、凭据或正式文件目录，没有修改官方源码。

## 实际执行方式

`tests/native_mcp_probe.py` 启动外部 MCP 服务和 HTTP 模型替身，再启动未经修改的
`python -B -m hermes_cli.main gateway run`。官方进程只获得新建 HOME 的
`config.yaml`、合成 `.env` 和正常 Python 依赖；没有项目插件、项目 EXE、`PYTHONPATH`、
启动钩子、monkeypatch 或外部工具处理器注入。

MCP 服务配置使用官方 `mcp_servers.cf.url/headers/identity_header`；工具选择为
`platform_toolsets.api_server: [mcp-cf]`。Profile 头通过
`identity_header: {name: X-Hermes-Profile, value_from: profile}` 产生。
模型替身读取官方实际发出的 schema，必要时经官方 `tool_describe/tool_call` 选择工具。
项目实现仅运行在外部 MCP 服务测试进程。

实验创建两个 `/api/sessions` 会话，使用正确的 `X-Hermes-Session-Id` 与不同的
`X-Hermes-Session-Key` 并发请求 `/v1/chat/completions`。HTTP API 的 JSON
`metadata.task_id` 只是本次测试输入，不被视为鉴权或可信任务绑定。

可复现入口（独立依赖环境；不使用正式 HOME）：

```text
python -I -B tests/native_mcp_probe.py \
  --hermes-source <固定官方SHA的独立源码目录> \
  --hermes-python <独立官方依赖环境的Python> \
  --filebridge-source <固定FileBrowser SHA的独立源码目录> \
  --output <全新隔离证据目录> --api
```

Linux 检查入口为 `.github/workflows/native-hermes-mcp.yml`。
它只保存 `safe-summary.json`；合成业务 ZIP、临时用户 Token、完整配置和普通进程日志
均不上传为证据附件。生产开关未改变。

## 已直接证明的边界

Windows 独立 Python 3.12 环境的官方进程完成以下步骤：

- 两个实际官方会话创建返回 201，两路官方模型请求均返回 200。
- 官方加载远程工具，模型替身实际选择 `content_probe` 与 `query_current_task`。
- 远程合成文本和 PNG 的 `image_url` 均进入模型请求中的正式 `role=tool` 消息。
- 两路业务会话的 `query_current_task` 请求共用一个 MCP transport session。
- MCP 请求携带静态 Bearer、`X-Hermes-Profile: default` 和 transport session；
  每次 `tools/call` 的 `_meta` 为空，没有可信的当前 Gateway task/session/employee 绑定。
- `query_current_task` 参数 schema 只有 `query`，没有让模型填写身份或 task ID。

首次受限沙箱执行中，官方缓存目录创建后的写入发生 Windows `PermissionError`，图片未进入消息。
随后只在正常权限的隔离任务进程中重跑，没有修改 ACL、升级管理员权限或修改官方源码，图片成功。
本地成功证据目录名为任务专用 `nh26-api04`；不能用它替代 Linux 大文件检查结果。

`query_current_task` 是实验服务的拒绝策略哨兵，返回
`trusted_task_context_missing`，不是已部署的 Gateway 业务端点。
这里的结论由真实线上格式的请求捕获与固定源码共同支持，不能把哨兵拒绝等同于完整业务验收。

## 固定源码依据

以下行号均相对于固定的官方提交：

- `gateway/platforms/api_server_openai_routes.py:681` 起读取
  `X-Hermes-Session-Key`；`:687` 起读取 `X-Hermes-Session-Id` 继续官方会话。
- `tools/mcp_tool_transport.py:139` 的 `_connect_inputs` 解析连接的 URL/headers，
  `:594` 在建立 HTTP 连接时调用它。
- `tools/mcp_tool_errors.py:245` 的 `_resolve_identity_header` 明确只接受
  `static` 或 `profile`；Profile 在连接时解析。
- `tools/mcp_tool_handlers.py:573` 的处理器接收 `args, **kwargs`，
  `:396` 实际调用 `session.call_tool(tool_name, arguments=args)`；
  每次调用没有把官方本地 task/session 上下文或 Gateway 请求身份转换为远程授权上下文。
- `tools/mcp_tool_content.py:176` 的 `_mcp_result_with_native_images` 是图像进入
  正式工具消息的原生路径；本实验实际经过此路径。

MCP transport session 属于复用的服务连接，不能当成某次 Gateway 任务 ID。
静态 Profile 可用于服务器配置的身份分区；它不能区分同一 Profile 下并发的两个任务。
本候选不以模型参数、提示词中的 task ID 或共享管理员 Token 补齐这个缺口。

## 大文件与未完成范围

`tests/native_mcp_file_service.py` 复用固定 FileBrowser 的真实
`TestProductMaterialsExternalFixture`：普通测试用户、真实 TLS、鉴权、权限和 HTTP 处理器，
新建临时数据库与文件。服务器预置合成 12 图 ZIP，随后由官方模型选中的外部 MCP 工具执行
查询、导出和实际远程下载，核对完整 ZIP 大小、SHA-256 以及每个成员大小与 SHA-256。
该测试用户没有管理员标志；fixture 的 source scope 是 `/`，没有宣称额外覆盖员工子目录授权矩阵。

首轮 Linux 检查 `39300941923` 成功完成 18,156,370 字节 ZIP 的真实 TLS 上传、inspect 和 import，
并再次证明图文消息/并发会话/缺失上下文。它准确报红，因为探针错误地把工具描述中的名字当作
已完成查询/导出的回执，实际计数为零。已改为解析结构化成功回执，保留下载/成员核对断言。
最终大文件检查结果须结合本候选最新提交的 `safe-summary.json` 读取。

当前共享官方 API 缺少必要的远程每任务可信关联，因而本候选没有进入当前任务 Artifact/Response
发布与微信协议替身交付。业务身份错配、过期、跨会话和重放的完整端到端矩阵也未宣称通过。
没有升级客户端、增加项目插件、放宽鉴权，或引入每任务专属 Hermes 进程/Profile 生命周期来取得通过。

此实验没有调用付费模型，也没有联系真实微信、生产 Gateway、FileBrowser 或 `192.168.1.233`。
