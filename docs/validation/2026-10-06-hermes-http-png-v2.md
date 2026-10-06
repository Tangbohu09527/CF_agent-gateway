# 2026-10-06：PNG HTTP 验收约束 v2

本记录承接 `50d208559205ee29386778eea0d73b610287bb38`，仅适用于尚未执行的
PNG 正式 HTTP 任务。[原 PDF 记录](2026-10-06-hermes-http-media-acceptance.md)
及私有冻结证据保持原样：业务下载、读取、回答成功，内容 20/20，执行 26/27；
原约束禁止 `skill_view`，整项仍未通过，不重跑或改分。

## 事先固定的唯一 Skill

[机器可读规则](../../tests/http_agent_acceptance/png-rules-v2.json)版本为
`hermes-http-png-v2`。仅允许最多一次
`skill_view({"name":"ocr-and-documents"})`，参数必须完全一致；不允许 `file_path`、
`preprocess`、其他名称、命名空间或 linked file 读取。

- 来源：[NousResearch/hermes-agent 固定提交 4a2198bf](https://github.com/NousResearch/hermes-agent/blob/4a2198bf5124f0c4d915cb958f141116ae8607f0/skills/productivity/ocr-and-documents/SKILL.md)。
- 版本 2.3.0，5784 字节；Git blob `0677b70a1598ff270ce78de2b027ce6dc5162750`。
- 原件及本机既有安装文件 SHA-256 均为
  `856c1bce359ca7da0cc83f06e7b6b59f9445c16084d0510fc1541101af57f263`。
- 这是保留在本机的较早官方 Skill；当前 `0f4a98f` 源码树已将其并入 `pdf`，
  不把旧 Skill 冒称为当前源码树文件。来源通过 GitHub 官方 contents API 字节核对。
- 当前七个相关加载器文件与官方 `0f4a98f87c17007b81500239d0bd5b9574027b73`
  Git 对象一致，摘要固定在规则中。

通用 `skill_view` 会对声明的 `deps` 调用包管理、对缺失的声明凭据进入 setup，
也能执行预处理 inline-shell，因而不能按工具名全量放行。上述固定说明没有 deps、
setup、prerequisites、环境变量或凭据文件声明，没有 inline-shell 或 Hermes 模板标记；
该内容的查看不会进入安装、setup 或 shell 执行路径。wrapper 仍记录正常查看/使用计数；
readiness 内部读取现有环境快照，但没有变量声明，不会返回或注册任何凭据。

说明正文含安装和脚本示例、加载器列出 linked script 名称；这仅是文字和目录项，
**不授权执行示例、读取脚本、安装依赖、创建或修改 Skill**。也不授权 FileBridge
专用客户端、插件或安装器。模型任务中明确这些边界，实际 terminal 轨迹仍需逐条审查。
提示词及核验器是验收约束，不是服务端权限隔离。

## 最小驱动与核验变更

[原 HTTP 驱动](../../tests/http_agent_acceptance/run.py)新增显式 PNG v2 开关；不改
`src/` 产品代码、不换执行器、不新建隔离环境。请求前静态读取 Skill/加载器摘要及
当前配置，拒绝外部或受信任项目 Skill 搜索根覆盖，不导入或调用 Hermes Skill 工具。
请求后再次核对，将规则、来源路径、前后检查冻结到该 PNG case。

请求仍由生产 `HermesClient.chat` 构造。排他创建新 case 和 session/intent、GET 404
后仅一次 POST；之后只 GET 原会话。任务只含指定路径、自然语言和非敏感接口说明，
仍由 Agent 自行通过标准 HTTPS 下载、原生 `vision_analyze` 读取并生成回答。

[独立核验器](../../tests/http_agent_acceptance/check.py)只有显式 image/v2 才使用该规则；
缺少版本的原 PDF 保持旧严格规则及检查数量，未知版本失败关闭。实际 Skill 调用除
名称、参数外，还必须返回固定的路径、正文摘要、available 状态和空 setup/依赖需求；
冻结规则必须与仓库固定规则一致，前后来源核对一致。单独 `--task image` 只核对 PNG，
冻结验证完成后才读原核验 JSON，不把两个版本合成“旧约束全部通过”。

普通 HTTPS、原受限 Token/CA、Scope、1 MiB、任务目录内排他写入规则保留。
当前 HTTP messages 的原生图像投影不足以证明模型传输字节；
`model_transport_image_bytes` 无论答案得分如何都保留 `unobserved`。

## 现场边界

不修改现有 Profile、模型、工具集、配置、凭据、权限、安装或服务。CFserver 现场
API/Worker 镜像、迁移、队列、CFserver → Hermes 网络/认证，以及 Gateway/微信端到端
仍未核对；不恢复 Worker、不扫码、不发微信或处理历史任务。微信入站 PDF 持续 pending
继续单列上游阻塞，与读取已经存入 FileBrowser 的 PNG 样本无关。

正式 PNG 结果将在一次实际请求结束、回答冻结并独立核对后追加。
