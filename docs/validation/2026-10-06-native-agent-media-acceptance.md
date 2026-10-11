# 2026-10-06：官方完整 Agent 自主下载与 PDF / PNG 原生读取

承接 FileBrowser 固定提交
[`bb11be49` 的原生读取记录](https://github.com/Tangbohu09527/CF_filebrowser-enterprise/blob/bb11be4944b5325f2d10aa5a1adb88a3f467da72/integrations/hermes-filebridge/validation/2026-10-06-native-media-read.md)。
本轮从 Gateway `0ec54bf0f25f421e37e16e11bd098a814beca258` 继续，原功能分支、提交和
Draft PR 保留。FileBrowser 材料只通过 GitHub 读取，没有访问其本地项目工作树。

**两个全新会话均由真实 Hermes Agent 自行下载、读取并生成最终回答；首次独立核对
77/77 通过。** 这是官方库级完整 Agent 实证，不是生产 HTTP、Gateway Dispatch 或微信验收。

## 实际入口与隔离范围

- 使用已安装官方 Hermes `0f4a98f87c17007b81500239d0bd5b9574027b73`，只读核对
  15 个涉及 Agent、工具、视觉及客户端边界的源文件与该提交 Git 对象逐字节一致。
  不扩大为整个安装目录完整性证明。
- 根据安装级 PM `facts.json` 选择既有 generation；两个实际子进程均为 Python **3.14.7**。
  没有安装依赖、升级 Hermes 或启动安装器。
- 入口为 `run_agent.AIAgent(...).run_conversation(...)`，经过官方 facade、会话 turn lease、
  模型调用、工具分派与最终回答循环。没有直接调用底层 conversation loop 或下载/读取 handler。
- 每例使用全新 session/task、空历史、私有 NTFS 工作目录及独立 HOME/HERMES_HOME/APPDATA。
  `-I -S -B` 加载选中依赖目录，禁止 lazy install；safe mode 禁止插件发现，实际 plugin count 为零。
  没有加载半升级 `cf-filebridge`、真实记忆、SOUL、项目上下文或旧会话。
- 主模型沿用批准的 `openai-api / gpt-6-astra / codex_responses` 路由及 reasoning/service tier。
  每个实际生成请求的 model 均与该配置一致；真实配置文件执行前后摘要不变。
- 工具只取现有 terminal/file/vision 子集。实际可见 schema 包含 terminal、read_file、
  write_file、patch、search_files、vision_analyze 和官方 tool search/describe/call 桥接；
  不能声称只开放三个工具。本次实际调用清单如下，没有创建自定义工具。

Python 审计约束本测试进程的文件与网络，HTTPX profiling 只观察真实发送边界，不替换客户端、
工具 handler 或模型返回。该审计不继承到 Git Bash，**不是 OS 沙箱**。TLS、上限、只读 API 和
排他落盘通过审查本次 Agent 实际 terminal 代码、收据及磁盘结果确认；提示词/Skill 不是权限边界。

## 两个唯一正式任务

驱动只提供自然语言任务、scoped 路径和非敏感 API 说明。没有提供原答案、已提取正文、先前视觉
回答或旧历史。驱动没有提前下载文件，也没有替 Agent 调用 read_file/vision 或注入工具结果。

| 项目 | PDF | PNG |
| --- | --- | --- |
| session | `cf-native-93db29516e69415b89a7ead6bc2b7321` | `cf-native-c00afb2fb6c74de49b2da6cf2c009f41` |
| task | `task-ffdb18b890314926bef710609d6caa8d` | `task-ddd09d414cfe490d99aea320c3a59d9a` |
| Agent 工具顺序 | terminal → terminal → read_file | terminal → terminal → terminal → vision_analyze |
| Agent 报告的 API calls | 4 | 5 |
| completed / failed / partial / interrupted | true / false / false / false | true / false / false / false |
| 下载字节数 | 79083 | 47740 |
| 下载 SHA-256 | `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44` | `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` |

两次下载由 Agent 的普通 Python `http.client.HTTPSConnection` 发出，使用既有受限 Token 引用，
CA 摘要核对、证书链及主机名校验启用；不代理、不跟随重定向。只访问 self 身份查询和批准文件的
GET download，没有上传、修改、删除或扫描远端目录。下载路径相对账号 Scope，没有重复拼接目录。
每次读取最多 1 MiB + 1，超限拒绝；私有任务目录中 `xb` 排他创建，再重新读取验证大小和摘要。
身份核对确认非管理员；账号响应不是 Token 权限全貌，服务端仍负责账号与 Token 权限交集。

PDF 的实际 read_file 指向本次下载文件，返回 `extracted_document=true`、`truncated=false`、
47 工具行。最终回答覆盖全部 **29 条实际正文行**，其余为空行和 2 条 Markdown 表头分隔线；
表格各单元格、必需正文与末尾标记通过原核验 JSON 对照。没有 Unicode 替代字符。
早期逐行报告把表头分隔线计入非空行，留下一个格式差异；原报告与排除两条分隔线后的报告均保留。

PNG 的实际 vision_analyze 指向本次下载文件，返回 `meta.native_vision=true`。
随后主模型 `/v1/responses` 请求包含一个明确的图像部分；解码后的实际 PNG
为 47740 字节，SHA-256 与下载文件及上传前原始基准一致，响应 HTTP 200 后由 Agent 生成最终回答。
这次是**主模型原生图像路径**，没有配置临时 auxiliary 映射，没有沿用前一轮辅助视觉答案。
核验码、四区域位置/图形/颜色/数量与三条有向箭头均通过独立比对。

## 独立核对与私有证据

每个任务的 prompt、session/task、原始工具调用/结果、会话数据库、下载路径与摘要、真实图像请求
元数据、最终回答和执行日志都留在新的私有验收目录。内部地址、绝对现场路径、样本内容和凭据
不进入 Git 或 PR。最终回答另存为私有 Markdown，正文与原始 result 的 final_response 完全相同。

两个 result 与逐文件清单冻结、摘要核对后，独立离线程序才读取原核验 JSON；其 SHA-256 为
`2b055f8472167755cd60d16e87d24dc18382dd5433b1bf27bb252da275e76765`。
没有向被测子进程提供核验文件路径，也未将它列入可读目录。

[离线核验器](../../tests/native_agent_acceptance/check.py) 首次 **77/77** 通过，包含回答内容与
实际执行证据：新历史、调用编号/参数配对、下载收据/实际文件/原始基准三方一致、read_file 目标、
视觉工具之后的真实图像请求、批准模型和插件关闭。合成负例随后加强了负数/小数边界及冻结目录
新增文件检查；对**同一份冻结输出**重核仍为 77/77，没有重新生成答案。

凭据扫描得到用户单独明确批准后执行一次：扫描 **51 个文件**，包括两次会话 SQLite、工具结果、
日志及模型结果；本轮 Token/模型 Key 的原文与常见编码匹配数 **0**，脱敏替换标记 **0**。
扫描仅在内存中使用凭据，报告只保存计数；没有把凭据值写入模型输入、工具返回、命令参数或公共日志。

| 私有证据 | SHA-256 |
| --- | --- |
| PDF result | `2b5efef078a4b4b1a85f9a11f455d5d5bd5e04c5903864b923174a5f5497b0e7` |
| PDF 工具/HTTP 事件 | `16cabeb68fcd784a714ed1f78e2851cc35087e548bd128ff89a91030e0d17e3d` |
| PNG result | `11b0201f354c664be98e04c5ed91d164bbc8220bb866c592f6eafea04ac74c21` |
| PNG 工具/HTTP 事件 | `2c834f448e1bc8ea0abd079b9c2b7f2fbc6b12bd81d4d02bcaa655135e49035e` |
| 最终独立核验报告 | `7ed18581e7181e4a3cea753fad65f470107151eeec6ebf6c8de1fb11951df417` |
| 一次获批凭据扫描报告 | `9556be24f4bf07134d7f10443e854cee4d760a569c6f28e329aaaf726e63e955` |

## 验收代码与验证范围

[显式验收入口](../../tests/native_agent_acceptance/run.py) 不会由 pytest 自动执行现场请求。
沿用已安装 generation 和受限配置引用；传入 `--source`、`--install`、`--site-packages`、
`--output`、`--origin`、`--task`、`--remote-path`。先创建并核验新的私有输出目录，再用已选解释器
以 `-I -S -B` 运行。任何一个 case 目录已存在即拒绝，失败不会自动重开会话。
`--preflight` 只初始化 Agent，不提交用户任务；官方初始化可能查询批准模型端点的能力元数据。
核验器需两个 case 都有冻结结果，再传入原 verifier 路径、固定摘要和新的 report 路径。

本次保留一次 SessionDB 参数类型预检失败；修正驱动后预检成功，未触发模型任务或样本下载。
PDF 和 PNG 正式任务各启动一次。官方循环内的请求/工具迭代及 API 参数回退完整保留，不能把
一次用户任务误称为单个 HTTP 请求；没有为挑选答案重复运行任务。

两次运行各有对应驱动快照和摘要。交付版进一步在启动前保存并执行自身快照、核对 PM selection，
并补充本次测试进程树超时处理与凭据脱敏事件失败标记。这些收尾改动通过离线回归，没有据此重跑
真实模型任务或冒称本次曾触发超时分支。Windows 超时仅按拥有的测试子进程 PID 终止其后代，
不按进程名称终止任何现有服务；不承诺约束恶意逃逸/重归属的本机进程。

本地 **48 项离线测试通过**，覆盖明确图像部分识别、伪图片拒绝、不覆盖、进程树超时保留、错误
区域/数量/箭头/表格数值拒绝与冻结文件篡改拒绝。使用既有 Python 3.12 测试依赖并加载正常仓库
conftest；没有安装新包。连同既有宿主 HTTP、夹具、运行接线和会话回归共 **75 passed**。
Ruff lint/format 通过。完整仓库及其迁移、容器和 clean-device 门禁保留在
原 CI，最终远端运行结果以 PR 对应提交的 checks 为准。

## 配置结论与尚未通过的链路

本机这两个任务不需要持久化模型、视觉或 Profile 变更；非敏感 API 说明已随自然语言任务交给
现有工具。没有新增安装环境、EXE、专用下载 worker、解析器或 FileBridge 实现。后续可将同一说明
整理为轻量 Skill，但它只能指导工具使用，不能替代服务端授权；本轮没有修改实际 Skill。

以下仍未通过，不从本轮结果推导启用许可：

- 生产 Hermes HTTP、Gateway Dispatch/claim 与宿主绑定接线；本轮也未重验新版 `0f4a98f` 的
  create/fork、FIFO、超时恢复与旧宿主契约的兼容性。
- 真实 FileBrowser 插件与 Gateway resolve/events/closed 的集成、现场恢复和启用门禁。
- 微信入站取件、实收、回复与端到端回传。本轮只是读取已存 FileBrowser 的批准样本。
- 历史 PDF 持续 pending 的独立上游问题，仍然开放。

功能默认关闭，`legacy_runtime_confirmed` 未改。旧部分升级、备份、checkpoint 和未知残留原位保留。
未启停现有生产服务、连接 CFserver、恢复 Worker、扫码、发微信、重派历史、部署、合并或发布安装包。
