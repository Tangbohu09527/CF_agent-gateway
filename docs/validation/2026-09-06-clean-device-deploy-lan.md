# 2026-09-06 Gateway 首次安装、WeChat 组合与 Hermes LAN 验证

本记录属于 [Gateway PR #10](https://github.com/Tangbohu09527/CF_agent-gateway/pull/10)。**A 与真实 VM 的 B 已取得固定提交的通过证据；C 未执行，正确 Hermes 来源及完整 AI 主机安装/自启教程仍缺输入，尚未达到最高真实业务验收标准。** 没有连接 CFserver 或既有 AI 主机，没有读取生产 Token、Session、数据库、备份或业务文件。未部署生产、发布镜像、创建 Tag 或合并 PR。

## 版本与文件范围

- 初始工作区为干净 detached `c5518aed12b90235f118ed81bb3cef75d0463443`。先 fetch，再核对 remote、HEAD、origin/main、status/diff、同任务分支/PR及 worktree 占用。
- remote 为 `https://github.com/Tangbohu09527/CF_agent-gateway.git`；`feat/clean-device-deploy-lan` 从已核对 `origin/main` = `4f13039b86c60bc94340edb5468f0102d62d2dff` 创建。未修改 main、覆盖其他 worktree、reset hard 或 force push。
- 本文固定 A/B 证据的 Gateway 为 `cb90b31477bf14854df3087f52275418ce632987`。本记录同批只更新文档和一个既有 E2E 单元夹具，不更改已验证的运行实现。最终提交、远端 Head 和完整 CI 结果以 [PR Checks](https://github.com/Tangbohu09527/CF_agent-gateway/pull/10/checks)及交付消息中的固定链接为准，不把旧运行写成新 Head 的结果。
- WeChat 固定审查提交 `67cbbb04ce15703428ce165ac38effac19f4b701`；[PR #7](https://github.com/Tangbohu09527/CF_agent-wechat/pull/7) 已于 2026-09-06 合并，merge commit `81d21f451adc61df71077ca3035adeddeca4621f`。CI 实际核对 merged=true 和 reviewed Head；组合测试选择该已合并审查提交，没有使用浮动 main。
- 总文档只读固定提交 `8f51cd095c6967f806701b703281b08e5144296f`。两个依赖仓库均未修改。
- Controller 与修改前文件完全一致，Git blob `3039481583437d95e512925fcfdc3c0b51944d08`。测试执行真实固定路径文件，未改 Controller v1、组合 Gate 或 Dispatch 职责。
- 修改范围为 Gateway 的 `deploy/`、原 production Compose/Dockerfile、配置模板、Polling chatId 解析、Hermes client/诊断、正式业务身份初始化与必要 store/schema、测试/CI和部署说明。没有文件/图片、FileBrowser、Skills、ERP或架构重写。

## 实施与测试对应

| 实施项 | 正式入口/资产 | 实际验证 |
| --- | --- | --- |
| Debian 13 amd64 系统、固定源码与 Controller | `deploy/install-clean-device.sh system/controller`；系统准备复用固定 WeChat 脚本 | A空目录/空daemon，B空白真实Debian；静态Contract先于Token/DB/Gateway |
| 管理人与容器身份分离 | 正式入口为新管理人记录root-only创建意图，分配20000起空闲UID/GID | A实际管理人20000；与WeChat1000、Gateway10001、PG999分离；5项无sudo访问被拒绝 |
| 安全配置与重跑 | `configure`，独立API/Admin/PG凭据，受限Hermes Key输入文件 | POSIX权限/原子写入/中断重试测试；A冲突输入、重跑摘要和数据保留 |
| 实际源码构建 | `build`复用原`docker/Dockerfile` | A/B分别构建，记录基础digest、依赖解析版本、Git SHA与实际Image ID |
| 网络与独立 PostgreSQL | 同一正式production Compose + 独立`deploy/postgres/compose.yml` | 统一cf-internal，独立project/volume、无数据库host port；空库迁移、重启持久化 |
| 空库业务身份与 V2 路由 | `initialize` / `cf_agent_gateway.initial_identity` | 原业务stores单事务、显式身份/授权/Profile/private路由；冲突保留，真实PG文本链 |
| WeChat 生命周期与 Gate | 固定Bootstrap/fresh QR与未修改Controller | A真实管理脚本与容器、合成外部微信；认证/API后组合Gate开，停止时Dispatch保留 |
| WeChat chatId接口 | 正式Polling支持已记录的5个字符串别名，歧义失败关闭 | A始终保留chatId响应；canonical/别名/非法值/冲突回归 |
| Hermes LAN/协议 | `cf_agent_gateway.hermes.diagnose` | 默认无HTTP/模型；显式POST opt-in；A真实容器到外部替身的认证、超时、停止、不可达与完整V2请求 |
| 默认 sudo PTY | 正式B helper在同一外层sudo终端中认证并执行管理脚本 | A真实密码/默认tty/use_pty；无密码回显，精确恢复A专用规则；B使用真实交互认证 |
| systemd与真实重启 | 独立database/core units + 正式B before/after入口 | B实际KVM、PID1systemd、boot ID变化、配置/凭据持久化和核心自动启动 |

使用入口：[首次安装](../deployment/clean-device.md)、[最小业务身份](../deployment/initial-identity.md)、[Hermes 来源/LAN](../deployment/hermes-lan.md)、[A/B入口](../../tests/deployment/README.md)、[真实VM入口](../../tests/deployment/booted-vm.md)。

## A：代码与真实容器组合

固定 Gateway `cb90b31477bf14854df3087f52275418ce632987` 的两次 A 均通过：[PR运行](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34025165059)、[push运行](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34025163295)。[主A证据](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34025165059/artifacts/9986888065)为 `clean-device-A-34025165059`，37项检查通过。

- 正式system创建管理人`20000:20000`，补充组仅`20000,27(sudo)`；Gateway PID1`10001:10001`，PG PID1`999:999`；WeChat正式runtime/data属主`1000:1000 0700`，Gateway根目录`0:0 0750`。
- 真正降为管理人、子命令不调用sudo后，Gateway根、WeChat runtime、runtime/data、runtime.env及Token五项访问均PermissionDenied；固定Controller仍通过正常sudo操作。
- 唯一标记 `clean-device-A-57e386fc5d815e0bbdadc4fb`：Gateway消息1、Delivery记录1、模拟微信实际投递1、该标记模拟Hermes执行1，轮询8次，Delivery attempt=1。真实Gateway关联identity/Profile/thread，Dispatch success、Response delivered。
- `text-chain.json`保存必要V2请求字段、会话、幂等键和上述计数。模型执行数只属于模拟服务，不证明真实Hermes/供应商行为。
- 配置/凭据/Token/数据重跑保留，错误输入与顺序失败可安全重试；独立PG重启保留文本，Controller停止不改变Dispatch容器。
- 实际管理目录权限、网络/认证/超时/停服、默认sudo PTY均有独立记录；运行日志没有ERROR/CRITICAL/Traceback，Secret扫描通过，实际遮挡次数为0。

真实组件为Debian包、独立Docker daemon、PostgreSQL、Gateway、固定Controller、固定WeChat管理脚本及非root管理人。外部微信HTTP/WebSocket/进程、Hermes HTTP与普通容器的四项systemd观察为明确替身。WeChat替身的root supervisor单独记录，不能把它当作真实WeChat应用进程的UID证明。A数据库为managed；外部TLS PostgreSQL前检/错误首次凭据安全重试有单元测试，尚未另做真实外部TLS数据库组合验收。

## B：真实 Debian 13 VM安装及重启

两次独立B均实际通过，主[作业日志](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34025165059/job/101464757770)和[白名单证据](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34025165059/artifacts/9986861472)包含以下结果：

- 从[固定Debian官方镜像](https://cloud.debian.org/images/cloud/trixie/20260831-2587/debian-13-generic-amd64-20260831-2587.qcow2)启动，下载前核对SHA512SUMS并校验实际镜像字节；SHA512为 `5a069019420fb9441ad4f8004c661fadb747edd5662ca54a17c8f923dee7d717e21dbdaa4ba72d6fce7f920e0217f0a9af382298a7d46ed4bc9dc33ac19181b6`。
- 实际KVM初始化成功，QEMU8.2.2，Debian13/amd64、PID1systemd、非容器；六个项目/状态/数据库/storage/Docker路径初始全部不存在，docker命令也不存在。
- cloud-init只供应必要管理账户`1100:1100`、标准sudo密码与独立root SSH公钥；此账户是B初始输入，自动分配由A验证。没有预造Token、网络、DB、Gateway配置或业务路由。
- 固定真实WeChat镜像 `ghcr.io/thisnick/agent-wechat@sha256:b5e92047e28ce67e34576e574d8ccf00f8619f485597109f7342a137300285c0` 实际拉取并验证wechat UID/GID1000；执行正式configure/Bootstrap，不启动微信运行进程或扫码。
- 正式system→Controller→WeChat准备→build/configure→DB/migration/identity→start/diagnose→boot-service全部通过。
- 主VM boot ID `b293333d-1674-48b1-aafc-7a72cf4e90cc` → `3ca910ce-acd5-48ec-94d6-d71f5f547ac3`。配置和凭据摘要跨真实guest重启一致；Docker/core自动启动，DB/identity检查通过，Poll/Delivery关闭，WeChat未运行、等待fresh QR。
- B实际Gateway PID1`10001:10001`、PG PID1`999:999`；系统包、pip freeze、来源和本VM镜像记录均在结果白名单。实际systemd257.13、Docker29.8.0、Compose2.40.3。
- Hermes仅有两次默认TCP连接，目标是本runner上的明确TCP-only替身，不返回HTTP/API或模型业务响应。B不构成C。

本机Windows缺少Docker/WSL，Hyper-V虽存在但只读能力查询被本机OS权限拒绝；这一局部阻断没有被当作B不可执行的结论。随后在一次性GitHub runner用已存在kvm组权限启动真实VM，不改设备mode或sudoers。未连接任何既有设备。

## 实际镜像与依赖记录

| 本次构建 | 实际 Image ID |
| --- | --- |
| 主A Gateway | `sha256:72dcfb10d2c79c4a6c0304b37bcf4dcd1eee46e831b0020e3e06180484750a18` |
| 主B Gateway | `sha256:8229be45be3fa22851ee8873cb0d8175ce559de27208b7f430e831c6908a8b3c` |
| 第二次B Gateway | `sha256:bc8a9a524919a307886d396d84afaa10dc3b62137c71212f45124c8c5519ff3b` |

共同Python基础digest为 `python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254`；PG为 `postgres@sha256:bb3e1a57e5407e0a5280b4211980a5e537f4abd234a87014ac979849a78dd825`。完整依赖解析见artifact中的`python-packages.txt`及B嵌入记录，包含构建工具。不同独立构建产生不同Image ID，未承诺固定Git SHA逐字节相同；没有发布这些镜像。

A原始脱敏stage日志、runtime日志、身份/权限、固定提交、image/pip/system包记录均在上述artifact，本地归档为忽略目录`.task-artifacts/ci-34025165059/A/`。B只导出`B-result.json`，本地为`.task-artifacts/b-vm-34025165059/`；不上传VM磁盘、seed、SSH私钥、原始终端内容、Token或Session。CI artifact保留14天，需要在到期前下载归档。

## 全量检查与保留的失败证据

原有全量测试、迁移、真实PG populated fixture、V2 E2E、Lint/Format/UTF-8链接、逐文件bash -n、Compose E2E以及新增A/B均由CI运行。最终Head必须等待实际Checks结果后才能交付，不用预测测试数。

- 候选`b3e83a9`的[原有CI](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34024074181)通过：1253 passed/1 skipped（真实PG fixture另步1 passed），迁移47、V2 E2E6；新增30项POSIX安装和13项B守卫当时均执行。
- `cb90b314`的[真实Compose E2E](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34025165062/job/101464757496)和质量检查通过。该轮全量1286 passed/1 skipped/1 failed，唯一失败为旧E2E单元夹具只有两次心跳读取，与修复竞态后的三次读取不符。本记录同批已更新夹具，明确模拟pause前后的合法时间推进，相关文件11项本地通过；完整全量结果以最终Head重跑为准。
- 更早A真实暴露sudo会话前提、systemctl依赖和chatId接口遗漏；正式代码/资产修复后通过。独立审查另发现默认管理UID1000与WeChat重合，已经正式修复并由当前A实际权限拒绝证明。
- 原Compose冻结检查在`docker pause`前读取时存在合法心跳推进竞态，已改为pause成功后的基准，仍严格检查stale与恢复；没有放宽超时/恢复要求。
- 首次B在VM启动前因runner不允许直接`sudo -g kvm`失败；改为获准root sudo调用runuser，保持原runner UID和现有kvm组后，当前两次B真实通过。失败运行没有被计为guest安装或重启结果。

## C与缺少的输入

C未执行，保持待批准人工验收。用户确认Hermes确切来源、版本及包装层仍未知；已只读搜索获准仓库和历史，没有可核实的安装来源链。完整AI主机安装/自启教程和真实LAN业务验收仍未完成，不能用同名项目或通用OpenAI-compatible服务器代替。

需要的不含Secret输入：正确发行者/仓库及固定提交或制品摘要、包装层SHA或不存在的说明、Windows原生/WSL受支持环境、provider/model/Profile配置来源、认证/监听/会话/幂等/响应契约、启动服务或任务及进程/主机重启方式。不索取Key或Token。当前只交付可证实Gateway配置、精确来源防火墙步骤、默认无业务容器诊断和显式付费POST协议/应用检查，来源证据见[Hermes说明](../deployment/hermes-lan.md)。

如扫码前无法取得批准account_id，固定WeChat fresh QR没有post-auth hold入口；认证/API通过后会立即打开Gate。需要最小WeChat配套支持认证后保持Gate关闭，待正式身份/路由及Hermes检查完成后再开Gate，证据和范围见[初始化说明](../deployment/initial-identity.md)。本PR未跨库修改，不伪造身份或ready。

## 后续真实设备验收步骤

1. 审核PR固定提交；提供干净Debian13 amd64/systemd设备、管理人、固定WeChat镜像和批准身份资料；先补齐正确Hermes来源证据。
2. 按[正式教程](../deployment/clean-device.md)从固定GitHub提交取得入口，执行system→Controller静态Contract→非root WeChat configure/Bootstrap→build/configure→独立DB→迁移/身份初始化→Gateway/Dispatch就绪，保持Gate关闭。
3. 在批准AI测试主机按确认来源安装正确Hermes，配置独立provider/API凭据、模型/Profile与精确Gateway来源防火墙；从真实Gateway容器默认网络诊断，再显式批准POST模型探测，确认认证、完整协议与应用响应。
4. 用B before/after复验真实Debian重启；按正确Hermes来源完成AI进程和主机重启、自启及容器复验，分别核对登录启动和无需登录的开机启动。
5. 运行真实WeChat fresh QR并人工扫码；认证/API通过后打开组合Gate。发送唯一批准文本，核对Message→Admission/Thread/Profile→Dispatch→Response→Delivery及一次实际效果；未授权发送者不得触发AI。
6. 留存脱敏固定版本/image/依赖记录、状态和计数，核对错误认证、不可达、超时、停服与安全恢复；重复首次安装须保留有效配置、Secret和数据。

A/B通过不等于C通过。真实微信、正确来源Hermes及Windows AI主机业务验收完成前，最高验收保持未完成。
