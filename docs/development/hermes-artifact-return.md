# 官方 Hermes 任务内附件返回接入

契约：`cf-artifact-return/v1`，工具：`cf_return_current_chat`，工具集：
`cf_artifact_return`。Gateway 和宿主插件均默认关闭。此文与
[字节交接契约](artifact-return-contract.md)共同使用；不是现场部署完成记录。

## 固定官方入口与最小适配

兼容目标为官方 Hermes `0f4a98f87c17007b81500239d0bd5b9574027b73`。
本轮只读核对已安装源码与选中 Python 3.14.7；不覆盖安装、不升级依赖。
插件入口是本仓 `hermes/return_bridge/plugin.py` 的 `setup(ctx)`，由官方插件
`register(ctx)` 调用，使用该版本真实提供的扩展点：

| 官方源码入口 | 本模块用途 |
| --- | --- |
| `hermes_cli/plugins.py:857` 的 `register_platform_handler` | 获得原 `api_server` 的 aiohttp Application 和 adapter，添加请求中间件。 |
| `hermes_cli/plugins.py:456` 的 `register_tool` | 注册当前任务返回工具；不注册下载、解析或微信发送工具。 |
| `hermes_cli/plugins.py:934` 的 `register_middleware` | 用官方 `tool_execution` 的 session/task/turn/tool-call 上下文校验真实执行。 |
| `agent/turn_context.py:759` 的 `pre_llm_call` hook | 固定真实 Agent 执行三元组，只加入非敏感工作目录和工具说明。 |
| `gateway/platforms/api_server.py:4458` | 原 HTTP app 在 AppRunner.setup 前调用插件 factory；completion 仍由官方生成。 |

源码存在两个明确缺口。`api_server.py:4158` 仅手动传递官方 Profile/browser
ContextVar，`api_server_runs.py:49` 的 executor 提交不复制整个 Context。
插件使用标准 `ThreadPoolExecutor.submit` 子类，在每次提交时 `copy_context()`，
并通过标准 `loop.set_default_executor` 接入；原 Agent 循环和核心函数未改写。
已有未知自定义 executor 则拒绝启用，不能静默覆盖。官方后续工具线程继续复制
Context。这里不是以 session ID 查询授权的全局表。

`tcp_site.py:32` 的官方 TCP helper 没有 SSLContext 参数。本模块通过已暴露的
原 Application/runner 添加一个 `aiohttp.web.TCPSite(ssl_context=...)`，与官方
HTTP 在同一进程；不增加代理、守护进程或运行平台。原明文 listener 必须明确
绑定 loopback，否则插件拒绝启用。TLS site 有界等待 runner 就绪，随原 app 清理。
现场应使用已有证书与管理入口，不能捏造官方已有 TLS 配置项。

## 请求级授权与工具边界

后台上下文仅接受已启用真实 API key 认证的 HTTPS、非流式
`POST /v1/chat/completions`。拒绝匿名请求、重复/不完整 header、命名 Profile
路由、错误 session、Profile/revision、非规范接口路径及非批准 Gateway origin。
宿主不持有 Gateway 签名秘密；Gateway 在 PUT/GET 时验证 HMAC、真实 claim fence、
准入身份、来源线程和期限。上传目标及 slot 由可信代码计算，模型不能指定。

每个请求产生新的私有子目录和可撤销作用域，随后绑定官方实际 session/task/turn。
工具只接受 `file_ref`、`kind` 和可选 `filename`。完整 capability 只在请求作用域
内存中；SQLite 消耗账本只留 SHA-256、随机 request ID 和原到期时间，以原子唯一
约束拒绝重复请求与重启重放，不保存完整授权，也不刷新预算。

完成、异常、取消、断连、超时和宿主清理先撤销作用域，再有界等待正在退出的网络
调用。已进入线程/异步任务的迟到操作也检查同一撤销对象。官方 HTTP 幂等缓存命中
若没有进入本次真实 Agent turn，则不返回新 ACK。只有真实作用域已绑定的官方
completion 才附带契约计算的 Accepted ACK；Gateway 仍独立检查失败状态和 ACK。

相对路径必须位于本次任务目录，拒绝绝对路径、URL、路径穿越、符号链接、junction、
硬链接、非普通文件与读取期间替换。Windows 以不允许删除共享的目录/文件句柄固定
路径，POSIX 使用逐层 `openat`/`O_NOFOLLOW`。先核对文件名、类型和大小，再读取
有界字节并计算摘要；PDF/PNG 的格式标识检查与 Gateway 一致。原件不被修改。

同一文件/元数据对应稳定 slot，重试使用同一份不可变字节。PUT 回执不确定先查原
slot；PUT/GET 合计最多三次、实际网络与响应读取总计最多 30 秒，服从更短的授权
期限与撤销。503 不成为权限失败；READY 的 ID、类型、大小、摘要须逐项吻合。
工具成功只投影“已交给 Gateway，等待任务成功及投递”，不表示微信实收。

无后台上下文的普通请求保持原行为。带上下文的普通读取任务若未调用返回工具，
仍只完成文字响应；没有自动扫描目录、上传或附带文件。任务目录通过非敏感 hook
说明告知 Agent；业务工具须把工作副本写入该目录，不允许开放整个用户目录去发送
历史文件。隔离探针的输入复制 hook 仅供测试，不是生产预上传能力。

这些是请求授权和文件约束，不是同一 Windows 用户下所有 terminal 工具的系统级
沙箱。说明文字也不强制识别自然语言意图；现有通用工具权限仍需现场独立审查。

## 两向 HTTPS

Gateway `HermesSettings.ca_file` 显式交给统一 `verified_ssl_context()`，用于正常
chat、session API 和诊断；`trust_env=False`、`follow_redirects=False` 保持不变。
不设置时兼容原 certifi 根集；配置时使用指定 CA 根集并验证主机名。
`SSL_CERT_FILE` 不替代此设置。不能全局替换 certifi 或给模型修改信任的工具。

Hermes 插件 `gateway_ca_file` 也显式创建 SSLContext。上传仅允许配置中的 HTTPS
origin 与规范 `/internal/hermes/returns/.../artifacts/...` 路径，不跟随重定向。
错误 CA、错误主机名与 HTTPS 握手失败均终止，没有明文回退。TLS 不要求额外 mTLS。

签名秘密只注入 Gateway API/Dispatch；Hermes 只取得当前请求 capability，不获得
微信 Token、Gateway 管理 Token 或签名秘密。证书私钥也仅在宿主 TLS 服务端使用。

## 验证与启用

真实官方联合探针见 [入口说明](../../tests/hermes_return_probe/README.md)。
它复用 Gateway HTTP、Artifact/Response/Delivery 和原测试微信接收端，明确区分
合成模型与另行批准的真实模型。临时官方进程不是现用 8642 服务。
历史 ACK 门禁之前的样本证据和 Skill 偏差不改写。

配置资产的统一入口 API、备份及回退见
[启用与回退](hermes-artifact-return-enablement.md)。现场仍需核验可达 HTTPS URL、
证书名/CA、私有存储、模块版本和实际工具权限；不能只改功能开关或更新镜像就宣称
可用。真实微信实收须另行批准，PDF 应以文件消息可打开并核对接收摘要，PNG 应以
图片消息实收并记录压缩/转码情况。
