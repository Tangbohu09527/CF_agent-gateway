# 官方 Hermes 附件返回联合验证

日期：2026-10-07。承接 Gateway 基线 `5cd8b830dfc9a5115135c24bbce2f629230008a5`。
契约：`cf-artifact-return/v1`，包含最终 Accepted ACK 门禁。功能默认关闭。

结论：**代码已完成，真实官方 HTTP/Agent 与隔离 Gateway 联合验证通过；现场未部署，
微信未实收。** 测试接收端是原有协议替身，不是真实微信。此轮没有重复 FileBrowser
下载、PDF 读取、PNG 分析或已收口的文字投递任务。

## 实际入口和安装核验

只读核对已知安装：官方源码 HEAD 为
`0f4a98f87c17007b81500239d0bd5b9574027b73`，工作区干净；十个 HTTP、插件、middleware、
Agent 执行文件的实际 blob 与固定提交一致。既有 PM facts 选中 generation
`3b7b06fec11340e7b9ed94f8e3e7267e`，解释器实际为 Python **3.14.7**。

本轮新观察到现用 `192.168.1.232:8642` listener，未沿用旧 PID 判断状态，未调用或
启停该服务。联合验证在独立私有 home/工作目录启动同一已安装官方执行器和源码，
使用动态 loopback HTTPS 端口；不占用 8642、不加载现场 FileBridge 或 Skill。
没有安装/升级依赖，没有改真实 Profile、配置、凭据或服务。

实际官方入口是 `APIServerAdapter`、官方插件 loader、platform handler、
`pre_llm_call`、`tool_execution` 和 `register_tool`。两处源码能力缺口及标准 API
适配见 [官方接入说明](../development/hermes-artifact-return.md)：请求 Context
跨 executor 传播，以及原 aiohttp runner 上的 TLS site。没有替换 Agent 循环，
没有协议 Hermes 宿主替身、核心 monkey-patch 或第二套下载器。

## 获批模型：每项仅一次正式任务

沿用原批准 `openai-api / gpt-6-astra / codex_responses`，推理 `ultra`、service tier
`normal`；Key 仅从原引用进入内存，经 stdin 和官方 secret_scope 传递，不放入配置、
环境变量、命令行、提示词或工具参数。真实安装配置前后摘要相同。

输入是两份既有批准样本的**本地测试副本**。驱动只复制到新任务授权目录，没有上传；
自然语言只要求返回当前聊天，不提供旧答案、核验 JSON，不要求下载、解析或视觉。
PDF 完成并检查实际轨迹后才提交 PNG；各一次业务 POST，没有失败重发或换会话。

| 项目 | PDF | PNG |
| --- | --- | --- |
| 新官方 session | `v1:cf-agent-gateway:d54996bc-5092-40b0-b573-2a4dff31994a` | `v1:cf-agent-gateway:62e5e3c7-883b-47c4-9b12-5e4975568bc4` |
| 实际返回 tool call | `call_32tTTflKPUGKx5RCFgtaZDqA` | `call_37db0048bdc247808cf5f683e4eda203` |
| 字节数 | 79,083 | 47,740 |
| SHA-256 | `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44` | `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` |
| HTTPS PUT / GET | 1 / 0 | 1 / 0 |
| Artifact | `badf8659-ed29-5c17-a861-695451cffeb1`, READY | `57e68795-c872-50eb-bdf6-378086141f04`, READY |
| 测试来源目标 | 私聊 `wxid-return-user` | 群聊，目标取自该次准入 Message |
| Dispatch / Delivery | success / delivered，各 1 次尝试 | success / delivered，各 1 次尝试 |
| 完整证据凭据扫描 | 38 文件，0 匹配 | 38 文件，0 匹配 |

原始模型 wire 调用为 `tool_describe → tool_call`；后者仅封装一个
`cf_return_current_chat`，官方实际工具行与 middleware observer 都关联到该返回工具。
不是驱动直接调用工具、先上传再让模型复述。没有 read_file、vision_analyze、下载、
Skill 或 FileBridge 工具操作。scope、实际 turn、tool call、PUT、ACK、关闭顺序一致。

Gateway 与 Hermes 两个方向都实际完成 CA/主机名验证的 HTTPS 握手；后台授权未进入
模型输入、会话内容或普通日志。最终 completion 的 ACK 与本次完整 Bearer 字符串的
SHA-256 一致。上传前观察到真实 claim 续租，随后真实仓库写入 READY Artifact、两段
Response（文字＋附件）及 Delivery；每段只有一次成功 attempt 和唯一回执。

输入副本、任务目录文件、Gateway Artifact、接收端文件和接收请求中解码的字节逐一
相等。PDF 走 file 消息且保留文件名，PNG 走 image 消息；接收端仍是替身，PNG 的
字节相等不代表真实微信不会压缩或转码。

PDF final answer (verbatim):

> 已将 CF-NATIVE-PDF-20261006-A1.pdf 作为 PDF 文件交给 Gateway，等待任务最终成功后投递到当前聊天；尚不表示微信已实收。

UTF-8 SHA-256: `16c4b67347f604b8167747d49a5bae206c17a726474be601c2bc156381851ca5`

PNG final answer (verbatim):

> 已将 CF-NATIVE-IMAGE-20261006-B1.png 作为图片交给 Gateway，等待任务最终成功及投递；不表示微信已实收。

UTF-8 SHA-256: `40b638bb568e2b79093a10edca6cd62b52d1716f21d71e29c97f7f9a360e3965`

## 证据保存和独立核验

私有证据分别保留在本工作树 `.task-artifacts/` 下的
`official-return-approved-file-20261007-once` 和
`official-return-approved-image-20261007-once`。每项包含冻结 intent/源码摘要、官方
会话、工具事件、HTTP/ACK、批准样本副本（本轮未新下载）、SQLite、Artifact、逐段
回执、接收字节、最终回答及凭据扫描计数。完整证据和测试证书私钥不提交到 Git。

回答固定后，另一只读程序按原会话 `data` 和真实 SQLite 关联，每项 27 项检查通过，
前后文件摘要不变。其 v1 检查器误用 `DeliveryAttempt.status=success`；实际合法值是
`delivered`，v2 修正枚举并加强逐段回执关联。两份检查器摘要及所有原证据均保留，
没有改正式任务或分数来凑通过。

早期合成模型探针也保留：第一次官方临时 home 触发懒安装尝试，被审计阻止，未完成
任何安装；随后明确使用官方 `HERMES_DISABLE_LAZY_INSTALLS=1`。最终合成 file、
image/group、text/no-upload 均通过，另将测试 lease 缩至 3 秒验证实际续租。最终
代码合成探针 `official-return-final-code-11` 为 1 POST/1 PUT、36 文件扫描 0 匹配。
早期扫描范围较窄的报告和 ACK 门禁以前的证据不追溯改写。

## 回归与局限

最终本轮核心组：**117 passed、1 skipped**。覆盖两向真实 TLS 正/负例、错误来源及
ACK、并发隔离、旧授权、重启重放、结束/取消/迟到线程、503/总期限/不确定回执、
同 slot、摘要损坏、非法路径/链接/替换、普通读取不自动返回，以及跨进程启用中断
恢复和精确回退。跳过项是 Windows 缺少 symlink 创建权限；Linux CI 保留该门禁。

修复过程中启动的 Windows 全仓尝试记录为 **1961 passed、23 failed、99 skipped**：
17 项依赖 Linux 暂存/权限语义，5 项受借用 Python 3.12 原生扩展到 3.14 的兼容性
影响；1 项是当时已加载的旧 Windows metadata-only 句柄不能阻止 rename。该缺陷
已改用实际目录访问句柄，最终核心组真实 rename 负例通过。原失败日志保留，不将
这次尝试写成全仓通过，也没有跳过或关闭门禁来使其通过。

本机无 Docker CLI/可用隔离 PostgreSQL 服务；Linux 全仓、迁移、PostgreSQL、V2、
容器和 clean-device 由对应新 HEAD 的 CI 执行。新增固定 0f4a98f/Python 3.14.7 的
官方 HTTPS/Agent 合成模型 CI，保留原 4d55 双平台兼容性 job。CI 结果以具体提交
的 GitHub 检查为准，不用它代替上面两项获批模型正式结果。

## 现场最少待办

统一入口调用 [启用／回退助手](../development/hermes-artifact-return-enablement.md)，
自动保留已有配置，生成薄模块和受管配置差异及私有原始备份；不要求重填模型、Token
或手改 YAML。仍须在批准窗口核验实际 URL/CA、证书主机名、私有工作根与 Artifact
存储、仅 API/Dispatch 使用的签名秘密引用，加载模块并验证 ACK 后才开启功能。
助手不操作服务，首次 journal 未完成时失败关闭、保留残留，不能算现场启用成功。

CFserver 网络/入口与生产配置没有核对或更改；没有部署、扫码、微信发送、数据库
修改、历史任务重派或 `legacy_runtime_confirmed` 变更。真实 PDF 文件消息可下载/
打开及接收摘要、真实 PNG 图片消息内容和转码情况，仍须下一次单独批准实收。
历史 Skill 偏差、Poll 告警、微信入站 PDF pending、半升级现场全部保留。
