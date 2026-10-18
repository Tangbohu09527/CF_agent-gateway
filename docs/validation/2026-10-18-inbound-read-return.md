# 入站工作副本读取与原件返回验证（2026-10-18）

## 最新真实验收结论（2026-10-18 08:29 UTC）

| 项目 | 实际结果 | 任务与投递 |
| --- | --- | --- |
| PDF | 正文读取、合法返回引用、原件 Artifact、投递及用户可打开/收件摘要全部通过；收件名未原样保留 | Message4310 / Dispatch49 / attachment4 / Delivery44 |
| 后续文字 | 沿原受管会话说明前次 PDF 记录，用户实收通过 | Message4311 / Dispatch50 / Delivery45 |
| 旧PNG51 | 原件取件通过；read/return未调用，无Artifact，保留未完成业务验收事实 | Message4312 / Dispatch51 / attachment5 / Delivery46仅文字 |
| 新明确说明 | 同一会话承接，准确回复“请发送PNG”，实收通过 | Message4313 / Dispatch52 / Delivery47 |
| 新PNG53 | 实际看图、合法返回引用、图片Artifact及微信图片实收通过；接收PNG像素完全一致，文件字节不同；file附件路由仍未实现 | Message4314 / Dispatch53 / attachment6 / Delivery48 |

最终08:24 UTC Collect确认Poll/Delivery已关闭，无非终态Dispatch、Delivery或媒体任务。本轮任务公钥及sudo授权已提前撤销，08:24:31单次密钥登录确认认证拒绝；临时密码文件不存在。以下保留完整时间顺序与证据边界。运行应用仍为1a8ce964/镜像db2b0e85，Hermes427与FileBridge f3a63895组合未再修改。


## 初期范围与断点（下文按时间保留，最新结果见文首）

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
| 生产 PDF | 业务失败 | Message4309 / Dispatch48；仅投递失败说明，没有正文读取或 Artifact |
| 生产后续文字/PNG | 未执行 | PDF 未通过，未继续发送 |

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

以上为现场部署及返回插件注册通过。FileBridge 工具目录可见不证明其主机 middleware 已注册；本次真实 PDF 未通过，详见下节。
## 真实 PDF 的失败与安全收口

用户在原私聊发送新 PDF 后，正常产生 Message4309 / Dispatch48 / attachment3 / inbound_job3（local11）。本次原件文件名有 `(1)` 后缀；Gateway 已取到 79,083 字节，SHA256 `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44` 与原样本一致。准入 allowed，沿已建立的执行会话正常 fork；未复用旧附件授权或重派历史任务。

实际 Hermes SQLite 的精确会话、时间和工具调用关联记录表明：仅一次 `filebrowser_download_inbound(attachment_id=3)`，返回固定错误 `trusted_context_unavailable`、不可重试。`filebrowser_read_inbound` 与 `cf_return_current_chat` 均未调用。Dispatch48 的一次 HTTP 请求完成，只代表传输完成；Response `legacy-response:message:4309` 仅含失败说明文字，没有 Artifact。Delivery43/attempt43 有一次文字投递回执，用户截图确认收到同一失败说明；没有实际 PDF 回传。返回协议 ACK 仅能由固定 Dispatch 路径推断，未记录直接响应头证据。

Guardian 于05:12:20 UTC关闭 Poll/Delivery，05:13:05及05:17:28再次核验关闭。无同私聊非终态 Dispatch、无 pending/claimed 媒体任务；历史42/43/46的 dead 状态及47的文字结果保留。checkpoint 自正常 Poll 前进到 local11/gen7，未人工修改。

本次临时权限实际起止为2026-10-18 05:06:16至2026-10-19 05:06:16 UTC（北京时间10月18日13:06:16至10月19日13:06:16），但已于10月18日05:17:28前提前撤销。随后一次指定密钥检查返回 public-key denied，DPAPI 临时密码文件已删除。没有开启 PNG 或追加文字测试。

## 后续只读诊断的证据边界

普通 Admin 的固定 PID/启动时间探针已证明当前 host token 在进程中存在、格式合法且与 HOME `.env` 一致；仅保存布尔，不输出值。独立无网络、无 worker、无插件注册、无生产写入探针，使用官方427的 PluginContext 和配置读取逻辑，成功构造实际 HostConfig 与 ControlClient；worker/CA 路径和摘要、TLS 构造均通过。官方配置加载器的建目录和备份写操作仅在一次性探针中替换为只读检查，现场程序未修改。

这些结果排除当前配置构造失败，不能证明实际服务启动时注册成功。现实现将未绑定 handler、middleware 的上下文/控制链异常汇合为同一错误；初始化注册异常还被静默吞掉。现有证据不足以选择其中某一根因。下一步核对 Dispatch48 的只读主机绑定持久记录，并补充固定枚举诊断与模型先调用 download 的隔离顺序回归；不得将缺少细分日志解释成新 ACL 故障。
## Dispatch48 的新增只读证据与时基修正候选

后续05:37:53 UTC审计证明 Dispatch48 的绑定已固定owner/nonce/task及租约，
身份、私聊、Profile、媒体任务和host均匹配，结束时state=revoked、reason=completed、
密文已清除。events_connected=false、closed_at为空。由此可确认服务端resolve
实际提交，不能再把“host middleware完全未注册”当成该次请求的解释。

05:48:51 UTC对固定41秒窗口的访问日志只读投影得到9条有效记录；其中一次resolve
返回HTTP200，时间05:11:46.399677Z，无该绑定events/closed请求。resolve路径本身
不含绑定ID，因此这条HTTP记录仅按固定窗口关联，未声称单条ID证明。当前配置
lease_seconds=30.0。未采集响应头、正文或授权，无法恢复历史实际响应Date/body。

绑定租约截止05:12:16.350206Z；本机只读SQLite中精确关联的失败工具结果记录
时间05:11:45.592155Z，差30.758051秒。旧客户端把服务器绝对截止减本机wall time，
要求结果不超过30.5秒。冻结旧源码的离线重现确认
该数值在创建任务目录前拒绝；30和30.5通过、30.500001拒绝。失败结果的时间
戳晚于工具调用，但当时校验瞬间的time.time及期间墙钟变化未记录，因此不能
把差值推导写成历史分支日志。**跨主机时基缺陷已复现；历史首次异常和实际
采样值仍未知。** 在该次审计时尚无新的生产修复或成功PDF；后续部署见末节。

候选改为严格resolve/v2：服务端返回同次时基server_now，客户端验证原30秒上限，
从本地monotonic请求开始计时；请求/处理耗时计入预算，不用调大阈值。旧v1响应
保持原18字段，events/closed保持v1，重复resolve不续租。原生worker只得到更早的
保守截止，任务/身份/租约及返回过程守卫均保留。不改系统时钟、目录权限、模型
配置、数据库或FileBrowser服务器。FileBridge固定候选为
`f3a63895e33958dda2bbc4f3413c5619bdfabdff`。

定向验证：Gateway真实HTTP/DB的v1/v2与拒绝边界23项通过；FileBridge时基13项、
读取/返回25项、固定诊断11项通过。原TLS/native控制链18项通过。最终微秒保守
序列化后的普通Admin完整PDF组件在服务器偏移−2、−0.8、+0.8、+2秒均通过，
每次严格一次v2解析、一次原生下载、读取及原字节回传，30秒原上限未变。
证据SHA256 `843463402fdbb97e7670ff0bc5f8d1f6230da452300f8ee9a9a9e7839d7ad03e`。
该Windows组件的Gateway为协议替身。新固定双仓库Linux真实Gateway完整链
已通过：[run39102290952](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39102290952)，
head `1a8ce964`，实际PR合并源 `ddb098006cbeb10bd9026ea9f39fe07ffe88b397`；
187个Gateway源码文件与候选Git字节逐一相同，两个源码树清洁。固定FileBridge
`f3a63895`，PDF正文、Artifact/Response/Delivery、同一执行会话后续文字及PNG
均通过，微信/模型仍为替身。source-proof SHA256
`8bb8c33dfb9482b89d5ac24f92060bbab1b76142a3690b56d15c9baa969a103a`。

05:48:51 UTC最新只读现场仍为原Gateway/Hermes版本，Poll/Delivery关闭，无在途
Dispatch或媒体任务；任务公钥数量0、限定sudoers不存在、撤权记录在。仅为两次
只读审计重新收取的DPAPI临时密码仍受原12小时存储上限约束，需在接续不再需要
时删除。新Gateway镜像和客户端组合须完成候选验证并取得集中生产授权后才切换。

## 批准前的固定候选检查与生产边界

应用候选 `1a8ce964246c896d6d72dee8a037f962c58447a0` 的主CI
[run39102287582](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39102287582)
已成功：2719通过、6跳过；迁移47、PostgreSQL1、V2场景6通过，实际schema
仍为20261016_01。Quality、真实Compose容器和旧/当前Hermes兼容检查通过。

FileBridge的后续 `8d12d21e59619c10a794d997edc895a912b58cb5` 仅更新两项CI
固定依赖，产品源码仍与f3a63895一致。原联合测试固定Gateway0ec54bf（只支持v1），
因此新版客户端按设计拒绝；更新为1a8及精确ZIP/源码摘要，未改变业务断言。
[run39102930587](https://github.com/Tangbohu09527/CF_filebrowser-enterprise/actions/runs/39102930587)
两个Linux客户端/官方Hermes联合检查已实际成功。Windows旧恢复步骤仍因artifact
11075196992过期返回HTTP410而失败，另有未修改backend lint问题；不称整个
FileBridge仓库CI绿色。

Windows后续候选只替换inbound_control.py与inbound_host.py，另外11目标、
142个既有解析器文件、原生worker、配置及返回插件只验证。manifest SHA256
`452fde3688793ffcdff869fcd6d49c2e59ccae503392aa359db998ac58b520d5`。
7个PS5.1脚本解析无误，17个生命周期模拟及4个真实原SDDL事务隔离场景通过。
06:10:50 UTC普通Admin只读核验仍为Hermes427、既有配置/返回插件，活动任务0、
指定双监听正常；目标摘要和原SDDL均一致。该检查没有停止服务或写生产文件。

上述批准前记录时，新的Gateway镜像尚未构建或部署；固定候选源归档SHA256
`47d735f37dd58d1cf06cdf5d550c67ac23ce9e41708cd3ae525c13a49b7e30de`。
待审切换须先核当前版本/保护记录/配置挂载/队列与可用备份，从固定旧镜像离线
构建新应用并严格验证依赖、源码和六服务镜像选择，再只切换正式Compose的六个
image字段。配置、外部env、schema、历史任务及checkpoint不改；旧记录原样保留。
新Gateway先兼容v1/v2，再安装上述两个Windows文件并官方同版本重启。Gate全程
关闭，未知漂移或部分失败保留证据、按实际阶段收口，不自动全量回退。

这项Gateway镜像变更超出用户最近固定e742镜像的生产边界，必须取得一次明确
批准。生产PDF没有成功，不能用这些隔离和候选证明替代；也未发送PNG或重派48。


## 获准后的实际 resolve-v2 部署

用户明确授权固定源码1a8ce964的新Gateway镜像构建与切换，以及Windows两文件
更新。06:24:04 UTC独立候选准备完成，06:25:00 UTC生产切换与健康核验完成。
实际镜像为 `sha256:db2b0e850e7d087d0de367346ceeb472bb5b418aac7a89b2a1780cdfbfc5ae89`，
计划SHA256 `87352bf82e457b90223ed2c2518e541e361fc06a3988d53e74fb5d6bcb907864`，
切换回执SHA256 `ffa9ce7a3d34fe4d35e19e61da6502e638adc6f7beba4f30c6c726d4d968a2f3`。
源码及安装库存、依赖、基础层、六服务镜像选择均按固定合同核验，切换前数据库
备份已验证。仅Compose的六个image字段变化；正式配置、外部env、schema20261016_01
及历史任务/checkpoint保持不变。切换后Poll/Delivery关闭，核心服务健康。

镜像相对e742包含八个src文件差异：两项时基修正及此前已审阅的六项返回桥接
实现（含staging.py）。这不是只包含两个源文件差异的Gateway镜像。

Windows于06:26:38 UTC完成原SDDL/CAS事务，严格只更新
`plugin/inbound_control.py` 和 `plugin/inbound_host.py` 到FileBridge f3a63895。
独立备份/回执位于Hermes HOME的 `installs/cf-read-return-lease-v2-20261018/`。
另外11个目标、142个解析器文件、原生worker、配置、返回插件及工作目录权限
保持原状。官方427管理入口受控停止旧进程，再由普通Admin启动PID33164，
实际开始06:27:14.830 UTC；双监听正常、活动任务0、active_work为空。
启动证明SHA256 `2ab704c595c6f4d8e6d68380e9cb048291a19e2da2ee82bb5507f54db2389f09`。

这些是部署和进程健康事实；本节记录时新启动的工具注册观察与新短窗入口尚在
核验，尚无修复后的真实PDF正文读取、Artifact或文件回传，未发送PNG。
原Dispatch48及失败文字Delivery43保留，未重派。


## resolve-v2 部署后的真实 PDF 回传（2026-10-18 06:57 UTC）

本次新短窗正常接收 Message4310 / Dispatch49 / attachment4 / inbound_job4
（source local13），准入 allowed，沿已验证的原私聊执行会话继续；入站取件和
Dispatch均为一次尝试。没有重派48或修改历史任务、checkpoint。原Dispatch48的
失败结果及历史首次拒绝分支未知的证据边界继续保留。

实际 Response 为 `return:49:…`，包含非空文字和PDF Artifact两个分片。
Artifact `76c1a72a-a60c-5c93-9103-2789dc375662` 已 ready，79,083字节，
SHA256 `ed8bcf88549b664f456b83891b7435c4e3963323f411fc74a21b36ece441ef44`。
Collect只读核验存储原件字节摘要，与入站附件及固定样本一致。Delivery44的文字
分片attempt44和文件分片attempt45各一次成功、各有回执；最终delivered时间为
06:54:24.906889Z。用户另确认微信收到回复文字和PDF，并提供截图；这是独立的
接收确认，不能由数据库delivered状态替代。

Guardian随后关闭Poll/Delivery，两worker停止事件分别为06:54:26.981385Z和
06:54:26.980921Z。06:57 UTC的Collect再次确认两者停止，数据库快照时间为
06:57:25.789014Z；无非终态Dispatch或媒体任务，媒体claimed计数0。
checkpoint由正常Poll推进至local13/gen7，旧42/43/46/47/48结果保留。
采集日志SHA256 `e5a023af9abc0a5c9ad36dc013bfea638612bbfdae8daf4e7ed23698f8445186`。

这是服务器原件返回链与用户微信接收的阶段性通过。用户打开/下载该PDF后的
摘要、Windows实际工具读取调用的只读投影仍待核验；尚未据此宣称全部验收完成。
本次协议ACK仍仅为 `inferred_from_pinned_dispatch_path`，没有直接响应头或
明文ACK采集证据。后续文字preflight已通过且无在途工作，但preflight没有开闸、
发送文字或调用模型；后续文字与PNG仍待按约定分别验收。


### 07:00 UTC 接收端补充核验

用户确认收到的PDF可正常打开。随后仅读取用户指定的微信接收文件，核得
79,083字节及相同SHA256；读取前后文件身份稳定，与原件和Gateway Artifact
字节一致。未导出文件正文，未用文件系统时间戳证明收件。接收端证明SHA256：
`33ed2bfd22d19bd85da4049e6ac13b1897d35a8380024a776696acf188ccad03`。

实际接收名为 `send_file_1792306462866_CF-NATIVE-PDF-20261006-A1_1_.pdf`，
带传输前缀且括号变为下划线；名称没有原样保留，命名原因另行核查。这与已通过的
原件字节一致性分开记录。至此，上一节待核验的接收端打开和文件摘要已有证据；
Windows实际读取工具调用投影及后续文字/PNG仍待完成。


### PDF 实际工具调用补充核验

普通Admin只读投影已确认，Dispatch49对应的真实Hermes工具记录依次包含一次
filebrowser_download_inbound、一次filebrowser_read_inbound和一次
cf_return_current_chat，三项均成功；另有两项非目标工具结果。读取实际提取
807字、31个带行位置的单元，未截断、未执行OCR。读取调用明确设置
prepare_return=true，准备结果含合法相对file_ref，类型为文件，字节摘要与
79,083字节原PDF一致；正式返回实际使用该准备引用，Artifact与Collect记录匹配。
投影SHA256：019813542bc8745fdb16ac4fa68399e399833e77bd7e954bf72a8d18369070bc。

这项证明来自真实工具参数和结构化回执，未用模型回复自述代替。工作副本的
大小和摘要来自已验证的工具回执，并非事后重新读取已清理的副本；服务器存储
原件及用户接收文件的摘要分别由前述独立只读核验支持。返回工具自身只确认
pending_delivery；最终送达与实际收件由Collect、用户截图、可打开确认和接收端
摘要共同证明。本次PDF的取件、内容读取、准备返回、Artifact、Delivery及用户
收到可打开且字节一致文件已有证据；协议ACK仍仅沿固定Dispatch路径推断，
没有新增直接响应头采集。该结论不替代后续文字和PNG的分别验收。

### 07:05 UTC 后续文字核验

固定后续文字形成 Message4311 / Dispatch50，Dispatch一次成功，Response仅一个
非空文字分片，Delivery45于07:04:15.613114Z送达并有一条分片回执。
followup_managed_tip_confirmed=true，确认沿刚完成PDF的受管会话继续，没有
新增入站附件。用户提供截图，确认回复能回顾attachment4的读取内容及此前
pending_delivery工具状态，未把工具状态误称为微信收件证明；此观察不等于
对该会话全部历史的完整性证明。

07:05:03.559309Z的Collect确认Poll/Delivery停止，07:05:04.536156Z数据库快照
无非终态Dispatch，媒体nonterminal/claimed均为0。采集日志SHA256：
80b0722c681b1feb465074852dd9f87fb5a7ada54790e706e9e517c755cb7f7c。
此后用户已确认PNG准备就绪并开始下一窗口准备；本节没有PNG实际收发结果，
也未据文字阶段的关闭回执推断下一窗口仍关闭。

### 接收文件名的代码级对应关系

a949源码审计工作树内的固定revision96c2e90c的messages.rs第352–365行将文件名按ASCII安全字符清洗，
并使用带毫秒前缀的/tmp/send_file_ms_safe_name临时路径；后续粘贴该路径发送，
可解释本次接收名的send_file前缀及括号替换。该结论是源码与已观察名称的对应，
未新增核验现场运行二进制来源，也未修改协议。文件字节一致性已通过，原名称
未逐字保留，二者分开记录。PNG仍需分别核验入站文件读取与出站image消息；
出站图片消息不能自动作为原PNG文件名、原文件字节回传的证明。


### 07:09 UTC PNG 未完成业务验收

PNG形成 Message4312 / Dispatch51 / attachment5 / inbound_job5，source local18。
入站取件一次成功、状态ready，47,740字节，
SHA256为102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4，
与固定样本字节一致，但sample_basename_matches=false。会话仍绑定原线程，
沿已确认的受管父会话继续；没有重派历史任务。

Dispatch一次完成，服务器仅取得下载情况的文字回复，Response一个text分片，
Delivery46于07:08:11.815932Z送达且有一条回执，没有Artifact或图片返回分片。
因此Dispatch状态success不代表PNG业务验收成功；本次没有完成要求的PNG内容
读取及图片回传链。Windows实际工具调用投影和缺失操作合同分析仍待补充，
此处不以模型回复猜测根因，也不声称已经证明图片内容被读取。

07:09:15.804466Z的Collect确认Poll/Delivery停止，数据库快照
07:09:16.742764Z无非终态Dispatch，媒体nonterminal/claimed均为0，历史结果
保留，checkpoint自然推进至local18/gen7。采集日志SHA256：
2674fbddd9865d93222fe51d84467f790ac915ffb1e7670beef18d26633eac0e。
当前结论为真实PDF链及后续文字已有证据，PNG业务验收未通过，不能报告整个
PDF→文字→PNG目标完成。

### 07:11 UTC 提前撤销本轮任务授权

最终Collect重新采集三个阶段、确认收发关闭及无在途工作，固定
collect --revoke返回TASK_AUTHORIZATION_REVOKED=true。撤销日志SHA256：
e04b5cae53eb9172c2e84a5b0901d07726a09267d460384ed481088d30ca18d7。
随后仅一次独立任务密钥登录探测，在严格主机密钥检查、禁用密码回退条件下
收到认证拒绝，退出255；未导出原始stderr。临时DPAPI密码文件的物理路径确认
不存在。清理证明SHA256：
ace95b09888095a78bb43c0ddcda1c20e619da6a9c7efdb5d4c7df33cfc75481。

该授权原始窗口为2026-10-18 06:48:01Z至2026-10-19 06:48:01Z，
已在本轮结束时提前撤销，没有延长；原任务私钥文件保留不等于仍有服务器授权。
未新发测试、重派任务或改动数据库以通过验收。


## PNG 最终工具及指令证据

普通Admin、SQLite只读投影确认 Dispatch51 的 `filebrowser_download_inbound` 恰好一次成功，附件5绑定、47,740字节及SHA256 `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` 均匹配。`filebrowser_read_inbound` 与 `cf_return_current_chat` 调用次数均为0，无工具错误。投影SHA256：`4525faf99bab9dbc7a1fdaa0a4b7549d08a764605b07ad2807f657fee1cdbc5b`。不能把HTTP/Dispatch success及文字Delivery46当作PNG读取或附件回传成功。

同一绑定会话的5条持久化user记录中，旧4306的PDF指令摘要仍匹配，50的固定状态询问仍存在；当前attachment5消息仅含text/attachment_id，text为空，未另外携带操作说明。这排除了“只能因原件压缩或旧会话已丢失才失败”的推断。持久化记录不等于完整最终渲染模型prompt，也不能据此声称已知模型内部决策。

源码 `hermes/service.py` 的新附件包装仅明确要求先下载核验；受管fork保留历史，不为每份附件自动新增读取并回传命令。旧4306说明针对PDF，50仅问状态，未明确要求这张PNG执行图像读取和原样返回。本轮验收安排让用户只发PNG，未为这次图片任务提供明确操作目标，这是已确认的验收输入/操作契约缺口。不能通过默认所有上传都自动回传来放宽业务意图。

隔离探针 `tests/hermes_file_return_probe/probe.py` 每次显式加入“Read the original and return unchanged bytes to the current chat.”，确定性模型按固定流程调用工具。其通过证明官方schema/handler、鉴权、字节与投递链能力，未验证真实模型面对本次裸PNG和仅状态询问历史时会选择相同工具。本轮没有用替身结果覆盖真实PNG未完成的事实。

当前普通新文字任务没有“使用已完成附件5”的新入站授权关系；旧51完成后读取授权已撤销。不得让用户补一句话来尝试旧handle、延长51租约或重派51。最小后续应先明确本次附件操作意图；若要复用已登记附件而不重发，须另行实现并验证“新明确操作消息—原附件”的持久绑定与新任务短租约，保留原51及旧授权终态。这涉及新的Gateway候选及生产范围，当前未实现、未部署。也未再发送PNG进行碰运气式重试。

PNG当前返回契约是image路由，不支持作为PNG文件附件回传；本轮连图片路由也未被调用。图像识别、图片显示、接收端图片摘要及PNG文件附件回传均不能记为通过。

最终撤权后本地只读复核：Hermes仍是原PID33164，8642 loopback与18642 LAN监听归该进程；两个FileBridge文件摘要与部署值一致。清理证明SHA256：`ace95b09888095a78bb43c0ddcda1c20e619da6a9c7efdb5d4c7df33cfc75481`。本次仅追加脱敏证据文档，未再次变更生产代码、配置、owner/DACL或历史任务。

提交前远端核验：Gateway PR19文档头dff2378的25项检查全部SUCCESS（包含真实Gateway隔离链）；FileBridge PR4头8d12d21仍有Windows旧fixture下载HTTP410及既存backend lint失败，相关Hermes/Linux链与Windows427兼容通过。没有重跑旧测试矩阵。

## 2026-10-18 明确 PNG 指令验收前的补证（08:04 UTC）

本轮先补齐 Dispatch49 两项额外工具事实，再准备“明确说明→新 PNG”的真实验收。之前 PDF49 的正文读取、Artifact、Delivery44、可打开实收及79,083字节接收端摘要一致结论保留；文字50实收保留。PNG51仅下载完成，未调用read/return、无Artifact，不记为业务通过。没有重派这些任务、复用attachment5授权或修改checkpoint。

### 额外工具与持久修改

普通Admin通过SQLite只读事务，限定原PDF49会话摘要、时间区间和结果行，关联了两项非目标调用：

| 工具 | 时间（UTC） | 实际结果 | 持久副作用证据 |
| --- | --- | --- | --- |
| `tool_describe` | 06:53:43.702183 | 仅请求并返回`filebrowser_read_inbound`的schema；无not_found或errors | 该schema查询handler不写技能/配置；此结论不等于全文件系统无变化 |
| `skill_manage` | 06:54:12.467003 | 一项`patch`，`operations_applied=1`，顶层和单项success；无staged/pending字段 | 修改了Hermes HOME下`skills/productivity/ocr-and-documents/SKILL.md`，不是本次插件代码 |

该patch用1,463字符替换33字符标题片段，净增加约1,430字符的入站附件操作指引：使用当前授权附件ID和工具schema、优先读取工具、显式PDF/PNG读回任务使用`prepare_return=true`及合法返回引用、不将入站句柄当路径或寻找隐藏工作副本、区分下载/返回受理/实际送达，并在不可重试授权错误时停止。其内容与本次文件操作有关，但持久修改通用技能并非当时PDF业务验收要求的必要动作，不能因技术内容相关就认定已有授权。

当前唯一目标为9,192字节，SHA256 `0cb3f10e5fd7dfc02346227e6ca0f763c72a12231d7bf95bf56046222f3ba0a1`；mtime为06:54:12.432579Z，与成功结果相距约35ms。新增片段精确存在一次。旧33字符标题也仍存在于新片段内，不代表两次修改。工具回执、handler落盘分支、当前片段及时间共同证明持久技能修改；没有当时整文件修改前快照，不能声称已证明整份历史before/after diff或全部文件系统无其他变化。调用可能附带的技能ledger/usage记录另行区分源码行为与实际核验，不默认为无副作用。

用户本轮明确要求发现实质性未授权修改时暂停相关现场操作。因此新PNG尚未开窗，原技能未被回退或再次修改；已集中询问是否保留现存说明并继续验收。该问题不授权模型以后修改技能或配置。

两次只读目标检查先因跨Windows文件API的`ctime_ns`相差1,515,900ns而停止，失败证据保留。诊断确认仅该字段在path.stat与fstat之间不同；后续读器分别严格验证每种API前后全身份稳定，跨API继续比较dev/ino/size/mtime/mode/nlink/attributes，单独记录ctime差异。此为只读观察器适配，不是生产ACL、owner、插件或服务修改；未把该差异解释为现场文件再次改动。

### 当前现场与认证事实

本轮GUI遮蔽输入成功后，由已有Admin/DPAPI程序完成一次SSH密码认证和一次受控sudo密码提交。07:57:05.435113Z的只读审计确认：

- Gateway仍运行应用`1a8ce964246c896d6d72dee8a037f962c58447a0`对应镜像`sha256:db2b0e850e7d087d0de367346ceeb472bb5b418aac7a89b2a1780cdfbfc5ae89`，schema仍为`20261016_01`；正式计划、受保护配置及实际挂载符合已保存证明。
- Poll/Delivery停止，Gateway/dispatch-worker运行；在途Dispatch/Delivery队列为空，媒体ready=5、nonterminal=0、claimed=0。
- 原账号/私聊精确过滤和受管会话tip51一致，checkpoint仍local18/gen7；42/43/46的dead及47–51历史结果保留。最新原用户入库消息仍4312，新PNG说明尚未入库。
- 上轮任务公钥条数0，旧限定sudoers不存在，旧撤销记录符合。旧名义窗口尚未到期不代表授权仍有效。本轮没有安装/续期任务SSH或sudo授权，因此没有新的24小时起止时间可报告。
- 此审计未轮询微信上游；数据库无在途不证明上游为空，旧机器人回复可能尚未被轮询。

08:04:08.226647Z，Windows普通Admin只读核验确认原Hermes PID33164及启动身份/监听仍一致；13个已部署目标、142个解析依赖和配置摘要保持。现有服务TLS鉴权GET及API工具集、固定启动注册日志通过；没有在检查进程里初始化插件，没有调用模型、重启、安装或改权限。API工具集可用性不等于已记录下一次模型请求的完整schema。

FileBrowser Web旧地址误用已由用户澄清，正确入口为`https://192.168.1.233:18443/`；本轮不把它登记为服务故障，也未改端口、TLS或FileBrowser服务。

### 本地证据索引与共享边界

以下是本轮私有目录`.task-artifacts/read-return-20261018/explicit-png-20261018/`内原始投影的摘要。本文公开必要脱敏执行事实；原始日志、认证材料及完整技能/会话正文未上传。外部读者可以读取本文，但不能据仅本机文件名推定已经访问原始证据。

| 证据 | SHA256 | 范围 |
| --- | --- | --- |
| `pdf49-other-tools-evidence.json` | `a16ba1d35cb8ec67494b0204bbfa572028bf6dd70d43464a46b81b4a02b46a5f` | 两项额外工具名称、请求绑定和结构化结果 |
| `pdf49-skill-patch-target-evidence-v3.json` | `7958d88de0f61a083a49a5b3097781d67766db00517aff4e65b6a4433ee8fc90` | patch成功、唯一目标摘要/mtime、新旧片段匹配；实际相对目录由另一次只读文件定位确认，投影的短路径白名单未含productivity前缀而输出null |
| `skill-patch-observer-v1-stop.json` | `91b877ec9e2a4a92e6410ed76dd78b4d37f44f11dae9f3d03cf174f0d71ac5c9` | 首次观察器停止及退出1 |
| `pdf49-skill-patch-target-evidence-v2.json` | `27d1ebb4a0a69c78a612b13da8d0f8d8733f87b5deafd32ea339f7cf28a02847` | 跨API ctime差异定位，退出1 |
| `explicit-png-baseline-result-6758a38aa49d0a63.json` | `e545056906fedee573a13f369159698086a09badd52d23f91ee294c1ee8ea1f5` | 服务器只读基线，SSH/sudo成功，退出0；无生产写入 |
| `windows-readonly/result.json` | `7a0de7256b778ea39fd9a9d82f0141ba73be37fa114dc1a66effb140cd5461aa` | 当前进程、库存、现有API/日志只读核验，退出0 |

新短窗入口仅本地候选，未安装或开窗。基线入口21项、两阶段入口29项、Windows只读包装6项定向隔离检查通过，均不能替代PNG真实验收。未重跑历史完整隔离矩阵。Gateway文档头`4eae12ad2041d09e3aa2250ac1b56b13280a46e0`对应25项实际检查成功；这一CI结论不外推到后续尚未提交的文档或新功能。FileBridge PR4仍保留旧fixture HTTP410和既存backend lint失败边界，不声称全仓绿色。

附件续办和文件名保留仍为PNG收口之后的独立开发事项，本节没有实现或部署这些功能。当前临时DPAPI凭据仅为正在进行的必要认证保留；最终清理结果必须由后续实测记录覆盖，不能由本节提前推定。
## 2026-10-18 08:29 UTC：明确说明后的 PNG 真实验收收口

本节覆盖前一节08:04的准备状态。真实模型和真实微信完成了新的说明52与PNG53；没有重新部署、重启、修改模型/配置/owner/DACL/schema，也未重派51或复用attachment5授权。原PDF49的正文/原件字节实收通过、文字50通过及PNG51仅下载的历史事实均保留。

### 目标、实际输入和可证实的模型上下文

Codex本轮目标是先明确下一张PNG的操作，再由用户发送一次新样本。微信实际说明全文为：

> 接下来我会发送一张PNG测试图。这个要求只针对我接下来新发送的
> 这一张图片，不针对前面的PDF或已经处理过的图片。
>
> 收到后，请实际读取图片内容，描述其中的文字、形状和相对位置，
> 再使用当前可用的图片返回工具，把同一张图片发回这个聊天。
> 不要修改图片内容，不要归档到企业文件系统，不处理其他文件。
> 现在先回复‘请发送PNG’，等我发送后再执行。

该非敏感验收文本UTF-8共486字节，SHA256为`1aae4d3bc4f9d4804cf91601d2163f23611827052343156ddca23ab033d88c3f`。数据库精确匹配说明Message4313；用户截图确认收到“请发送PNG”后才发送原样本。没有夹入状态询问、额外ACK或别的文件。

Hermes只读持久化证据确认完整说明恰好存在一次，并先于当前attachment6消息进入本次受管会话。新附件包装仅有text/attachment_id，真实前缀与固定Gateway源码一致；current attachment记录为row469、08:15:28.135766Z。这证明持久化输入关系，不宣称持久化user行就是全部最终渲染prompt或外发HTTP请求。

初版新观察器把前缀错误写成短句后直接换行，输出current_attachment_payload_count=0和instruction_precedes=false。实际Gateway在句号后还有完整操作/不可信数据提示，故匹配器不命中。原观察器和结果原样保留；固定同53会话的窄派生检查确认附件6唯一、在选定时间内、说明在先。该失败属于取证匹配器，不是会话丢失或生产代码故障。

### 说明与图片的完整链

| 阶段 | 新消息/任务 | 实际结果 |
| --- | --- | --- |
| 明确说明 | Message4313 / Dispatch52 / Delivery47，local20 | 原账号/原私聊/发送人精确绑定，准入allowed；一次Dispatch；legacy-response:message:4313单text分片精确为“请发送PNG”，一条投递回执，08:13:28.649284Z完成；用户截图实收 |
| 新PNG | Message4314 / Dispatch53 / attachment6 / inbound_job6，local22 | 原绑定、准入allowed；一次取件和一次Dispatch；新的受管父会话fork与当前tip匹配，原会话历史保留 |
| 取件/读取 | Dispatch53 | `filebrowser_read_inbound`一次，直接完成当前授权附件取件核验及图像读取；47,740字节、原件SHA匹配；没有独立download工具行，不代表没有取件 |
| 返回准备 | 同次read | `prepare_return=true`，image/png，合法相对file_ref，47,740字节和原件摘要匹配；未正式归档 |
| 返回受理 | `cf_return_current_chat`一次 | 使用本次准备的引用，返回Artifact `5202edad-83c5-5fea-b313-3e0d34f11f30`；工具当时的pending_delivery只证明Gateway受理 |
| Response/Delivery | Delivery48 | Response `return:53:76fa33925e1a7f176635640c65cb15871bcb1358434d4394838d34f3700db52e:eaae8c20595bc0bad0f8395a723b74252fa0f68595e64336aeb0693415b8e96e`；text和artifact_ref两个分片；attempt49/50各一次成功、两条回执；08:16:10.341049Z完成 |
| 微信实收 | 用户截图与指定另存图片 | 用户看到了准确描述和返回图片；本机只读核对另存的PNG，详见下表 |

Dispatch53 claimed于08:15:27.988664Z，completed于08:16:05.276331Z。Artifact为ready、kind=image、mime=image/png、47,740字节、SHA256 `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4`；原件存储摘要也经服务器核验。协议ACK通过由固定应用dispatch成功路径推得，未录下原始响应header，不将其写为抓包事实。投递回执有两条，但provider_message_id字段没有记录；用户实收为独立证据。

53的完整实际工具顺序为`tool_describe`→`filebrowser_read_inbound`→`cf_return_current_chat`，各一条请求及结果。tool_describe只查询read与return两项schema，无错误；没有skill_manage调用。52工具库存为空。read明确报告native_image_attached，官方427的工具内容经当前模型适配后再持久化为JSON摘要加一个`[screenshot]`占位，且后面存在assistant行：这证明canonical工具消息实际含图像part，不只依赖模型自述。SQLite不保留该图片payload；未验证外发HTTP图像字节，亦不能排除后续provider重试时的降级路径。本次没有额外调用模型或重新执行工具来补证。

用户收到的描述与独立样本基准一致：标题和PIC-6V9K4R，A左上3个红圆、B右上2个蓝方、C左下4个黄三角、D右下1个绿星；A→B向右、B→D向下、D→C向左。该基准未由Codex额外注入被测模型。真实读取证据、描述和用户图片实收共同构成本次正向业务通过。

### 接收图片：像素相同，文件字节不同

保存路径由用户自由选择，与业务正确性无关；本轮只读了用户指定文件和原测试样本。

| 对象 | 格式/尺寸 | 字节 | SHA256 |
| --- | --- | --- | --- |
| 原样本、入站附件、工作副本及Gateway Artifact | PNG / 1400×1000 / RGB | 47,740 | `102c46ea4ba8225d9c6bde10226f4ea902590cfc3666891579d8feac449668a4` |
| 微信返回图片另存结果 | PNG / 1400×1000 / RGB | 48,549 | `974dcea86dfa8cdc61faed309c55fb49f2fde3540672d49701df65d7c246bd6d` |

两者解码RGBA像素摘要均为`1dd8769be922c4d3ca5de71af966f27b89351353bf52546317e50fd39b2f3591`，逐像素一致。接收文件新增pHYs块、IDAT由一块变为六块且压缩负载不同，故文件字节不一致。变化发生在Gateway已验证Artifact之后、用户另存完成之前或之时；没有进一步证据把具体层归因于协议、微信发送/接收端或另存实现。本次**图像识别和image路由实际实收通过；端到端原文件字节不变未通过；PNG作为file附件返回仍未实现**。不据显示正常宣称原文件回传通过。

### PDF49持久副作用的补证与本轮边界

用户明确批准保留既有OCR技能说明后，才继续本次验收；这不授权未来模型修改技能/配置。本轮52/53无skill_manage调用，结束时该技能SHA仍为`0cb3f10e5fd7dfc02346227e6ca0f763c72a12231d7bf95bf56046222f3ba0a1`。不能据此宣称全文件系统无其他变化。

在08:04初始检查之后，又核实了PDF49实际留下的curator ledger条目和两份内容寻址备份：before为7,753字节/SHA `0de6b15b2cd7e427855a8cca9008a65abf73bbc4b61a198a2edf7dfe7d61c1f8`，after为9,192字节/上述0cb摘要，两个备份的实际字节摘要均读验一致。ledger对应agent patch、原会话摘要、单一目标，06:54:12.460710Z；usage记录last_patched为06:54:12.462484Z，当前patch_count/generation为2，缺少此前计数，不能归因本次增加2。prompt snapshot当前存在，mtime07:03:52.012938Z；handler源码支持缓存失效及可选生命周期hook，但该次snapshot删除与其他hook副作用未直接观察。之前“尚无before快照”的描述只代表当时未读取，现由这些实际备份证据补齐；未导出完整技能正文。

### 限时授权、当前现场和安全收口

本轮沿用原任务密钥及遮蔽输入/DPAPI流程，不生成新密钥、不新增认证平台。一次凭据输入用于本轮必要SSH/sudo。固定两阶段管理入口装于`/opt/cf-explicit-png-1a8ce964-20261018`，root所有的依赖、严格主机密钥、来源192.168.1.232、七条固定sudo命令和禁转发限制保留；任意root命令负验证通过。

服务器实际授权起点是2026-10-18 08:10:47 UTC（北京时间16:10:47），安装成功08:10:51 UTC；固定截止2026-10-19 08:10:47 UTC（北京时间16:10:47）。期限从同次安装事务开始计时，成功后可用时长比24小时短4秒，没有延长或循环续期。key登录和限定preflight独立验证后，DPAPI密码即已删除。

文字窗口于08:13:29自动提前关闭。PNG窗口原最迟截止08:17:56Z，实际收到最终投递结果后，Controller于08:16:10.892Z自动关闭Poll/Delivery；08:20:12Z人工Collect再次核验。最终Collect观察08:23:59Z与08:24:01Z：Gateway/dispatch-worker运行，Poll/Delivery停止，Dispatch计数dead5/success48、Delivery全部48条delivered，无非终态Dispatch/Delivery，媒体ready6、nonterminal0、claimed0。checkpoint自然推进至local22/gen7；历史42/43/46及47–51保留，不清理、不重派。

原私聊最后实际轮询08:15:30Z看到22条、没有新待处理用户消息；停止之后没有再次读取上游，机器人本次输出可能还未作为self echo轮询。不能把数据库空在途说成当前上游绝对为空。第二份旧PDF早已入库，与本次等待无关。

08:24阶段固定collect --revoke成功，确认只移除本任务公钥及sudoers、保留证据。08:24:31Z独立一次严格主机密钥、禁密码回退的探测返回认证拒绝/255，无主机密钥或超时错误；DPAPI及暂存文件不存在，原私钥保留。此后不再远程管理。08:25:49Z普通Admin复核Hermes仍为PID33164、同启动时间和8642/18642监听。沙盒进程视图此前查不到该PID；按审批改用Admin只读核验后确认仍在，未重启/杀进程。

实际产品保持Gateway源码`1a8ce964246c896d6d72dee8a037f962c58447a0`、镜像`sha256:db2b0e850e7d087d0de367346ceeb472bb5b418aac7a89b2a1780cdfbfc5ae89`、schema20261016_01；Hermes `427d605d4985c2d49a795493ef6c5d66413057d1`、FileBridge `f3a63895e33958dda2bbc4f3413c5619bdfabdff`及已装返回插件保持。正式配置仍`/opt/cf-agent-gateway/config/production.yaml`，SHA `774762c1ecb9f7c2055f1b6f73e52500f940d35c1402f618e74897d1dfbe93c1`。没有生产业务目录归档/覆盖/删除，没有Gateway/Hermes/FileBrowser部署或权限变更。

### 可共享执行摘录和证据索引

以上表格和脱敏摘录可供外部读者直接核验报告含义；下列原始证据继续在本机私有目录保存，未上传。单凭摘要不能声称读过原日志。认证包、密钥、会话原文及环境凭据不在Git。

| 私有证据 | SHA256 | 主要事实 |
| --- | --- | --- |
| `auth/rendered/continuation-auth24-result-80339dc57fb46101.json` | `dc7422e1275ea4cb73474fa6e728e607568509d0155ce2d5b8dbe639b4fa5eaa` | 实际安装、限定命令正/负验证、密码清理 |
| `text-collect-20261018.log` | `66e6078c1f4c52822b59d1c4f78800cc3ab45cfe3e5e61bd1be4b15a283b0f97` | 4313/52/47及关闸 |
| `png-collect-20261018.log` | `5946b386873fe6be92cbab906b9dacaf43add5ab44d17d2ee90f8018afacd86a` | 4314/53/6/48、Artifact和两条投递 |
| `explicit-png-two-case-tool-evidence.json` | `8a105237ace1fec9c8b948c33fdb22898446e7f6dc8584889521dd201f01862f` | 完整实际工具库存、read图像part、prepare/ref、return |
| `explicit-png-attachment-shape-evidence.json` | `448d70c97d79a18989f99845d677db1f55ed10d95bb4fc69f102bd3b5244a252` | 保留初版结果后的前缀匹配纠正、同一会话指令在先 |
| `recipient-png-byte-proof.json` | `d3a0be4cf9ea11fdd4a7c4d9a6f856095fa8c8a135e1b1be41f2a379aa1d70c6` | 接收文件格式/尺寸/摘要差异 |
| `recipient-png-pixel-proof.json` | `69dfe2b62491e505f5b5f526af3719c9958606eb7c3c6d638b5ac8e5db62fbd4` | 解码像素完全相同、PNG块变化 |
| `pdf49-skill-sideeffects-evidence.json` | `a53abe31b321216b1cc85c1db2e39e9f7d206f682f59320cf9ba4f3df990b13d` | ledger/usage/快照的实际范围 |
| `pdf49-skill-backup-digests.json` | `b3487f1eaeba557aae8681236b5cb5816dd6f3687a931b3e0877420fc2c27d0a` | before/after备份实际摘要 |
| `final-collect-revoke-20261018.log` | `7fffd2cc4cff337ccc6f55f53a812e724f5e9ef8d4ecaed3292f2886b776a223` | 最终关闸、无在途及撤权 |
| `final-cleanup-proof.json` | `127e66ffa3123488fab063479fe5946a63f52c692c49bf1e3e1363c1720545c4` | 撤权后认证拒绝、密码不存在、技能摘要不变 |
| `final-windows-process-proof.json` | `332cc70204c0f7dfc0d5dcc2b9ae93bc6fcac597b8326b592356a3d6266b4b2b` | 当前Hermes进程和监听 |

本轮新增观察器的6项合成投影测试及短窗/认证包装定向检查只验证取证边界，不当成真实模型验收；上面的52/53才是真实模型/微信结果。未重跑旧完整矩阵。Gateway旧文档头4eae12a的25项检查成功；本节提交的CI须按实际新SHA另查。FileBridge PR4旧fixture HTTP410和既存lint问题仍保留，不声称全仓绿色。

### 后续独立候选，尚未部署

PNG闭环收口后才开始A附件续办与B原名保留。A将新操作消息明确关联原附件，产生新的受限任务授权，不复活旧Dispatch/租约；无法唯一选择时澄清。B将内部临时目录与收件basename分离，核对协议及实际paste-file路径对中文的再次改名。两项各在独立分支/工作树实现并定向验证，保留PR19及原生产材料；当前没有切换这些候选。部署范围、迁移/回退边界及源码/运行二进制证明缺口将随各自候选报告，需集中批准后才实施。