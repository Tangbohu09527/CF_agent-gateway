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
修正后的环境调用固定官方 `scripts/install.sh --stage python-deps`，内部由官方 PM
按 `uv.lock` 安装默认完整 `[all]`，包含官方 MCP extra；不再用自选 pip 子集代替。
`--skip-browser --skip-computer-use` 是官方允许的可选工具二进制退出开关，不删减 Python `[all]`。
运行解释器来自 PM 锁定的 Python `3.14.7+20260901`；依赖审计逐项核对 core 和 `[all]`
递归 extras 的版本约束、官方源码干净状态及三份锁定输入哈希。该检查仅执行官方安装器的
依赖阶段，不等同于完整安装器所有阶段或新电脑安装验收。

工作流只保存 `safe-summary.json` 和不含凭据的 `dependency-summary.json`；合成业务 ZIP、临时用户 Token、完整配置和普通进程日志
均不上传为证据附件。生产开关未改变。

## 旧子集环境的适用范围

原检查 `39302151607` / `39302780142` 的官方源码未修改，但其运行环境是
Python 3.14.7 下 `tests/hermes_return_probe/requirements.txt` 加
`mcp==2.0.0`、`Pillow>=11,<13` 的自建 pip 子集，没有运行官方安装器或完整锁定依赖。
该 requirements 文件注释对应旧 `0f4a98f` API/Agent 子集，并非 `427d605` 的安装清单。

最终旧检查的实际安装记录中，MCP `2.0.0` 与 Pillow `12.3.0` 符合固定官方 pin；
但 `PyJWT 2.15.1`、`httpx2 2.14.0`、`starlette 1.9.0` 分别偏离官方的
`2.13.0`、`2.7.0`、`1.3.1`。同时缺少多项官方 core 包。
因此不能说旧探针满足完整官方依赖或证明正式官方安装通过；它只证明所记录子集环境中的
真实源码加载、处理器执行与链路边界。下节另列修正后的官方完整依赖环境结果。

固定官方 `pyproject.toml:10–15` 明确只支持 Python 3.14 运行时；
`requires-python >=3.11,<3.15` 是旧安装升级兼容桥。下面 Windows Python 3.12 的结果
只保留为非标准运行时诊断，不作为官方支持基线验收。

## 官方完整依赖环境的复核结果

Linux 检查 `39315976156` 于 2026-10-20 04:06（Asia/Shanghai）成功，适用代码提交
`eac246359e8baa7fe97c253cd13cd9f0667949b0`。CI 检出的 PR 合并提交为
`cf46b38970e05b6c58b7e263df9e92afee0d0d8b`；通过 GitHub Git API 核对，两者 tree
均为 `b3903ee0e14a5636779dd1fdebb0c001e00113df`。重复触发的 `39315969085`
被并发策略取消，不计作通过。

已下载并检查本次 `dependency-summary.json` 与 `safe-summary.json`：

- 官方依赖安装阶段退出码为 0，使用 PM 锁定的 Python `3.14.7+20260901` 和默认 `[all]`。
  递归展开包含 `acp/all/cron/google/mcp/pty/sms/uvloop/web/youtube`；53 条适用需求
  全部满足，`missing`、`mismatches` 均为空，官方源码干净。
- 实际版本包括 MCP `2.0.0`、`httpx2 2.7.0`、`starlette 1.3.1`、`PyJWT 2.13.0`、
  Pillow `12.3.0`、OpenAI `2.24.0`、Pydantic `2.13.4`，完整实际包版本记录在依赖摘要。
- 官方输入 SHA-256：`pyproject.toml` 为
  `62541c282d5d4b1c4b43c02f3072477597ef024b28a3d1bd3acadd29934f050d`，
  `uv.lock` 为 `7f337b67943cbb287651761c8b40a2d8e1e595b5aff47dd7deb2683717c3be41`，
  `pm/lock.json` 为 `49b79259220160f147255078a19af541a5f328d3871c8041cc1eb909d19eb549`。
- `boundary_probe_passed: true`，两路 API 均为 200，模型调用 14 次、错误为空；
  `content_probe`、`employee_query_probe`、`employee_export_probe`、`query_current_task`
  各经官方实际处理器执行两次，合成文本和原生 PNG 进入正式工具消息。
- 查询、导出均各两次经过固定 FileBrowser 的普通用户 TLS HTTP fixture；本次输入 ZIP
  为 18,156,370 字节，SHA-256
  `2f63163f6265c9d7026f8fd5793ad6e099182eedb42ecede55bc9a0237e90a74`。
  下载导出 ZIP 为 18,156,942 字节，SHA-256
  `c37cd162e6ec1432a843891685c544a8ada64760cf55e1319a711fc74fa4bb4d`；
  全部 12 个成员的大小与 SHA-256 符合输入清单，`members_bytes_verified: true`。
- 项目插件为空、子进程无 `PYTHONPATH`；两个当前任务调用仍共用一个 MCP transport，
  全部 8 次 `tools/call` 的 `_meta` 仍为 `{}`，实际收到 `trusted_task_context_missing`。
  `task_return_authorized: false`，微信阶段未进入。

本次结果补齐官方完整 Python 依赖基线，但仍只执行安装器依赖阶段；不代表完整安装器、
新电脑安装、当前 Gateway 任务回传或微信交付验收通过。

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
- `agent/tool_dispatch_helpers.py:467` 起将 MCP 工具识别为不可信内容，`:555` 的
  `_maybe_wrap_untrusted` 包装结果文本；模型替身解析包装内的结构化回执，
  只有真实 `employee_query/export: complete` 回执才推进下一步。

MCP transport session 属于复用的服务连接，不能当成某次 Gateway 任务 ID。
静态 Profile 可用于服务器配置的身份分区；它不能区分同一 Profile 下并发的两个任务。
本候选不以模型参数、提示词中的 task ID 或共享管理员 Token 补齐这个缺口。

## 大文件与未完成范围

`tests/native_mcp_file_service.py` 复用固定 FileBrowser 的真实
`TestProductMaterialsExternalFixture`：普通测试用户、真实 TLS、鉴权、权限和 HTTP 处理器，
新建临时数据库与文件。服务器预置合成 12 图 ZIP，随后由官方模型选中的外部 MCP 工具执行
查询、导出和实际远程下载，核对完整 ZIP 大小、SHA-256 以及每个成员大小与 SHA-256。
该测试用户没有管理员标志；fixture 的 source scope 是 `/`，没有宣称额外覆盖员工子目录授权矩阵。

Linux 检查 `39302151607` 于 2026-10-20 02:26（Asia/Shanghai）成功，
适用探针提交 `1f3a1c2978bcc0b4803e6246e309fc77a7865c51`。
已下载并核对该次工作流的 `safe-summary.json`，结果为：

- `boundary_probe_passed: true`；两路 API 均为 200，模型调用 14 次、错误为空。
- `content_probe`、`employee_query_probe`、`employee_export_probe`、
  `query_current_task` 各实际执行两次；查询、导出均经过固定 FileBrowser 的真实 HTTP 处理器。
- 输入 ZIP 为 18,156,370 字节，本次 SHA-256 为
  `12a212577a83c38bf410e3512036da61955f1af66afe9e0e114f882493db5f29`。
- 实际下载导出 ZIP 为 18,156,942 字节，本次 SHA-256 为
  `c37cd162e6ec1432a843891685c544a8ada64760cf55e1319a711fc74fa4bb4d`；
  下载字节数和哈希符合服务端导出结果，全部 12 个成员的大小与 SHA-256 符合输入清单。
  ZIP 重新打包后的字节与哈希可以不同，断言并不要求两个 ZIP 容器逐字节相等。
- 正式工具消息包含文本与原生图片；插件文件列表为空，子进程无 `PYTHONPATH`。
- 两次当前任务调用共用一个 transport session，全部 8 次工具调用的 `_meta` 均为 `{}`，
  拒绝哨兵结果 `trusted_task_context_missing` 被实际观察到。

输入样本内容由固定生成器产生，ZIP 容器包含打包时间；以上哈希只标识该次检查的完整字节。
最初的探针曾把工具描述误作完成回执，后续又未解析官方不可信文本包装，均准确报红。
最终检查改为解析真实工具结果回执，并继续保留实际下载、完整 ZIP 和逐成员校验断言。

当前共享官方 API 缺少必要的远程每任务可信关联，因而本候选没有进入当前任务 Artifact/Response
发布与微信协议替身交付。业务身份错配、过期、跨会话和重放的完整端到端矩阵也未宣称通过。
没有升级客户端、增加项目插件、放宽鉴权，或引入每任务专属 Hermes 进程/Profile 生命周期来取得通过。

此实验没有调用付费模型，也没有联系真实微信、生产 Gateway、FileBrowser 或 `192.168.1.233`。
