# 入站工作副本读取与原件返回验证（2026-10-18）

## 范围与已确认断点

本轮保持生产 Gateway e742461、镜像 f2c50392138d0515866748ca3d933371131b544b4accd4eb7aba6ce3ee3a77ac、schema 20261016_01 和 Hermes 427d605 不变。Dispatch47 / Message4308 / attachment2 已完成一次请求，仅产生失败说明文字，没有 Artifact；不重派或复用旧授权。

只读安装清单显示 FileBridge 注册入口、host helper 和 manifest 仍是旧组合，内容模块单独存在；配置未启用内容读取，consumer_tools 为空。下载工具返回的入站句柄不是企业文件 source/path，也不是返回工具 file_ref。模型以错误接口尝试解析句柄产生 invalid_tool_input，不能据此诊断新的 ACL 故障。

## 最小接口变更

- `filebrowser_read_inbound(attachment_id, question?, prepare_return=false)` 复用现有原生下载、受保护工作副本和 PDF/PNG 读取实现。
- `prepare_return=true` 在读取成功后，由可信适配层核对当前 Dispatch、Message、员工、私聊、Profile、会话、任务和 turn，将已验证流交给返回插件。
- 返回插件核验真实运行时身份类型、授权声明与身份/会话的一致性、读取工具上下文和原有剩余租约（签名由 Gateway 校验）；只在当前任务返回目录建立受控副本，返回合法相对 file_ref。模型不接触实际工作路径。
- 重复调用复用同一引用及名额。身份/会话/附件不符、撤销、过期和旧引用重放仍拒绝。原件字节不变。
- 保留普通生成文件的原返回契约。插件安装清单增加 staging 模块；本轮不改服务端 worker、PDF 解析器、Gateway 数据库或全局模型配置。

Gateway 运行代码提交：`dd7446040fe0c1ef4968c5f387561e6041fd3cd0`；后续 `ac6f495`、`bc365c7` 仅修正隔离探针。FileBridge 运行代码：`fbb290cfc64d6fca9e5298faf23fbf36ecf1a784`；`218847bdce3d4bbb77bd2eee02756647871f4fb9` 仅更新严格 schema 测试期望。

## 证据矩阵

| 验证 | 结果 | 适用边界 |
| --- | --- | --- |
| FileBridge 读取/返回适配与真实原生 worker | 79 项通过 | 隔离数据与模型 |
| Gateway 新增返回绑定/文件守卫 | 46 项通过 | 包括实际 UUID 身份与 claim/context 流程 |
| Gateway 最终代码相关回归 | 216 通过、1 跳过 | Windows 创建符号链接权限不足的用例跳过；junction/8.3 场景已覆盖 |
| Windows 普通 Admin + 官方427加载/API/middleware | 通过 | 真实 worker/解析器/工具 schema；Gateway HTTPS 是协议替身，不包含真实 Artifact DB/Delivery |
| Linux 真实 Gateway + 官方427 + 两插件 | 通过 | 真实准入、取件、POSIX staging、Artifact DB、Response/Delivery；模型和微信端为本地替身 |
| 生产 PDF/后续文字/PNG | 尚未执行 | 不将隔离结果记为真实微信实收 |

完整链路 CI：[run39097442654](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39097442654)，head `bc365c7`、实际 PR merge 源 `039f632`，固定 FileBridge `218847bd` 与官方 Hermes427，源码清洁。安全摘要 SHA256：`6e18fea26c9f50adcfce91a37c18d98fa8334914de322af925ddad33921f9f54`。

同一隔离私聊执行说明文字、PDF、后续普通文字、PNG。两附件分别只有一次 Dispatch 和一次 Delivery 尝试，Artifact ready、binding closed，接收替身字节一致。PDF775字节，SHA256 `ba4743e6d893597741d38d31bf6603e248e418858f54c87c4cc063f5bdf8dc78`；PNG69字节，SHA256 `b1ff9c8ea3a780bad09b346c423d2d0e46815926879b18e841d928376a946640`。这两个文件是合成隔离夹具，不是生产样本。

Windows 另以现场实际安装的原生 worker 副本（SHA256 `130dbe3dd38530dc19b262a7c674e0d94a7e97ccbb62bd0c06f2d0978c1fd320`）通过普通 Admin、官方加载、PDF/PNG 读取与返回字节的隔离验证；未替换现场 worker。该安全摘要 SHA256 为 `2d040fabc7ba5c53bbe7c897fe0da819b4441e1ec4e61c4f3f0e7164bb36cea4`。其 Gateway 端仍为协议替身，真实 Artifact/Delivery 证据来自上述独立 Linux 联合链路。

实际模型 HTTP 请求含读取出的 PDF 正文及 PNG 原始图像字节。重复准备得到相同 descriptor，错误附件和后续旧引用拒绝；审计拒绝数0、凭据泄漏文件数0，宿主正常退出。Windows 独立测试另覆盖普通令牌、错误身份/会话及租约撤销。

探针曾在收尾时被自身隔离器拒绝：CPython3.14原生 worker 的已校验 Popen 使用 posix_spawn，以及官方 pytest 祖先检测。现限定同线程一次性已校验启动票据，并使用官方 HERMES_TEST_ISOLATION=1；未开启数据库守卫 bypass，未放宽生产校验。两次失败证据保留。

## CI 与生产限制

Gateway `7ac5f05` 的全部七个 CI 事件成功：主 Python 测试 2703 通过、6 跳过，迁移 47、真实 PostgreSQL 1、V2 6；官方兼容、Quality、Compose 和完整文件返回链分别通过。记录来源为实际 GitHub job 日志，并非本地推测。

上述完整联合链路已真实通过，不代表两个仓库的全部历史工作流均绿色。FileBridge 的无关 backend lint112 项仍失败；Windows 旧升级兼容步骤还受到历史 artifact HTTP410 过期阻塞。这两项与本轮读取/返回链路分别记录，不修改无关代码或降低 CI 门槛。

现有服务端只接受 PNG 作为 `kind=image,image/png`。原件 Artifact/上传字节、图片消息显示及微信接收保存后的摘要必须分别报告，不能把图片显示等同于 PNG 原文件附件回传。

## 最小现场切换边界

私有部署包仅处理7个 FileBridge 插件文件、4个返回模块和新增 staging、独立 PDF/PNG 解析运行时及对应窄配置；未变摘要的文件仅核验。保留精确 owner/group/DACL，旧工作目录权限不动。先备份并冻结身份/摘要，固定 UAC 成功后才官方 drain；事务完成后以普通 Admin 使用官方同版本入口启动。

部署前必须有本轮真实关闸、队列和进程证明。失败保留部分回执，不盲目重跑/全量回退。部署与加载核验成功后，才让用户发送新的 PDF，产生正常新 Message/Dispatch；保留旧任务和 checkpoint。每项短窗自动 Collect、关闸并核查在途。凭据和限定授权按既定限时程序清理。
## 现场插件加载（2026-10-18）

05:03 UTC 已完成受控备份和最小插件组合安装，并以普通 Admin 通过官方入口启动 Hermes427。新进程活动任务0，指定监听、认证 HTTPS 和 API toolsets 核验通过；读取与返回两个工具实际 enabled/configured。新返回插件 SHA256 `97e8c6fa3be4731c39577211a0a3668320761f8487493773e6935d1c1674035e`，窄配置 SHA256 `a08670d5e0dbf845afb81ece6ff6b3f276c74106c7e0261e1e62f82c2670b79b`。

官方 `agent.log` 中05:03:53.208Z的固定注册事件晚于新进程开始05:03:51.630Z，版本 `inbound-staging-v1`、工具内部 schema 摘要 `e7cddc80f7a975c1f5d8329af2a4a979542e090bb18f8fe837c746668b4f44e1` 匹配。实际 API 工具目录证明与某一次模型请求的完整 schema 证明分开；后者目前来自隔离链路。

备份及事务回执位于 Hermes HOME 的 `installs/cf-read-return-20261018/`，独立解析器位于 `runtime/cf-inbound-pdf-png-20261018/`。13个目标均验证，11个有字节变化，其余仅核验；所有目标 owner/group/DACL 保留，既有工作目录权限及 `.env` 不变。Gateway镜像/schema、FileBrowser服务端和模型配置均未改变。

此前维护失败证据保留：首次官方drain后，过宽的停止检查拒绝无关CLI进程，生产目标未变并已恢复原服务；下一次候选因误复制旧隔离回执，在drain前安全停止。最终干净候选按白名单装配并检查所有固定输出不存在后成功。未强杀无关进程、删除失败证据或盲目重跑。

以上为现场部署及加载通过，不代表 PDF/PNG 微信验收已通过。收发仍关闭，等待用户确认立即发送新PDF后才开有限窗口。