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

## PNG 正式 HTTP 结果

正常审批通过后，使用 `f3dc8c8bda30ed1c1e52cadb355fc875402c1db3` 的驱动和规则，
从 Windows 向现用 Hermes HTTP 发出唯一一次业务 POST。没有重发、换会话或挑选结果。
原批准模型和既有较广工具权限保持原样，未临时映射辅助视觉。

| 项目 | 本次 PNG 证据 |
| --- | --- |
| 规则 | `hermes-http-png-v2`，规则快照摘要 `273fa9ed1e56d070adddb4e6769f1286eca25b0261baaba503bfdbccf4f8f4d5` |
| session | `cf-http-7700dcfe152e4421bdef32cb5ac1cf25` |
| intent / idempotency | `cf-http-acceptance-3d338a0d42a445c68e7924e073b29904` |
| HTTP completion | `chatcmpl-88d5f4e8645c480394e5cfa56a6a9` |
| 请求 / 响应本地保存时间 | 2026-10-06 15:44:25 / 15:47:34，Asia/Shanghai |
| 返回 | HTTP 200、finish_reason=stop、会话匹配、Gateway client 接受；无显式 failed/partial/error |
| 正向完成字段 | 官方成功响应省略 hermes/Completed 头；session ended_at=null，不以结束 hook 作成功依据 |
| 实际会话 | source=api_server、model=gpt-6-astra、无 parent；完整一页 14 条消息、6 次工具调用 |
| 下载 | 47740 字节；仅一次样本 GET，保存新任务 work 目录内唯一文件 |
| 下载 SHA-256 | `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` |
| 文件名 | `CF-NATIVE-IMAGE-20261006-B1-16e6c4980eb244ca814a84d3229b8a8a.png` |
| 冻结后独立核对 | 答案 21/21、执行与 v2 约束 28/28，共 49/49 |
| 图像观测限制 | 原生工具文本及 [screenshot] 投影；model_transport_image_bytes=unobserved |

实际工具顺序：

1. `skill_view`：仅精确参数 `{"name":"ocr-and-documents"}`；来源路径、正文摘要、
   available、空依赖/setup 条件与固定版本一致，没有读取 linked scripts。
2. `read_file`：仅读取本次非秘密 HTTP 引用 JSON。
3. `terminal`：普通 HTTPS GET self；最初错误假定 `perm.admin`，在 identity validation
   处返回 exit 1，**尚未执行下载或写文件**。这次失败保留在原轨迹。
4. `terminal`：再次只读 GET self，仅返回 username 与非管理员字段，确认实际为
   `permissions.admin=false`，不回显完整响应或凭据。
5. `terminal`：保留原 CA、主机名/证书验证、直连无重定向与 1 MiB 上限；GET self
   校验后 GET 指定 PNG，`xb` 排他写入、回读比对字节并输出大小/SHA-256/路径。
6. `vision_analyze`：对刚下载的同一路径进行原生读取，随后 Agent 自己生成最终回答。

三次 terminal 均经当前服务正常 smart approval；没有绕过拒绝。前两次 self 只读核查
发生在同一 Agent 工具循环中，不是重发业务 POST，也不是重复下载样本。未见安装、
Skill 脚本、FileBridge 专用工具、外部解析器、远端业务写入或配置/服务变更。
前后 Skill、加载器及配置摘要一致。

Agent 原回答中的图像结论为：核验码 `PIC-6V9K4R`；A 左上红色圆形 3 个、B 右上
蓝色正方形 2 个、C 左下黄色三角形 4 个、D 右下绿色五角星 1 个；箭头
A → B 向右、B → D 向下、D → C 向左。**这些内容是在回答冻结后才与原核验 JSON
对照**；未在任务、说明或工具输入中提供答案。原始回答还包含本次下载凭证，完整保留。

原核验 JSON 摘要仍为 `2b055f8472167755cd60d16e87d24dc18382dd5433b1bf27bb252da275e76765`。
独立核验只加载冻结 image case；旧 PDF 不重新评分。PNG 49/49 不代表 PDF 的原工具
约束已通过，也不证明模型供应商线上的原始图像字节已被独立观测。

私有证据根沿用原验收目录，新增 `image/`，绝对下载路径保存在
`image/downloads.json`、工具 receipt 和 `image/final.txt`；新评分在
`png-v2-verification.json`，没有改动任何 PDF 文件。关键原始文件 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| image/final.txt（原始文件字节） | `c74123c02339e3a9f78f3f952ee542b9d9f1c908f5bc573cea2286d3eb623f96` |
| image/frozen.json | `9d72689d3d3d386a94519ced89b66687d1647ce609788fe8b90186bf7c36d06c` |
| image/response-02.json | `153994d30e3e68cb60f5e1c9c8f455b642145a7adbc6c45e4445ab5def9e2995` |
| image/response-04.json | `389c33a8e925cd3ea4bb30f51e0f06c10095be7ce770a0df7edc9b0e0d5b5756` |
| 未改变的 pdf/frozen.json | `79cfa432dafb028503ed95513eb20a38cb74201862046d541c900ceec8cc4d59` |

## 代码回归与后续边界

- 原 HTTP/客户端/库级验收资产离线回归 145/145；新 PNG v2 针对性回归 66/66。
  所有这些测试使用合成证据，不替代上述正式 HTTP 结果。
- Windows 完整仓库回归：1769 passed、98 skipped、18 failed（306.24 秒）。其中
  17 项触及 Linux/POSIX 暂存、权限或 symlink 语义；另外一次续租日志测试失败，
  单独复查 1/1 通过，首次失败不抹除。没有为 Windows 改动生产安全检查或新增 skip。
  PostgreSQL 和部分 Linux/安装测试由原条件跳过；完整 Linux、迁移、容器与 clean-device
  结果以该提交对应 GitHub CI 为准，不声称 Windows 完整套件通过。
- Ruff format/check 通过。PR #14 保持 Draft/Open，普通追加提交和 push；未部署。

本次自然语言 → 现用 HTTP Agent → 普通 HTTPS 下载 → 原生图片读取 → 最终回答
按 v2 约束通过；无需持久 Skill、Profile 或模型接线修改。这里只新增验收资产。
下一轮若要受控恢复，仍须先取得既有授权管理通道，核对 CFserver 实际 Gateway 配置、
API/三个 Worker 镜像、数据库迁移/队列和 CFserver → Hermes 认证，再单独批准恢复动作。
本轮没有验证 Gateway 派发、宿主 grant/revoke、微信实收或出站交付；历史 PDF pending
及 [原恢复前置条件](2026-10-06-hermes-http-media-acceptance.md)继续保留。
