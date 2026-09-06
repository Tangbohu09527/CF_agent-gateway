# 2026-09-06 Gateway clean-device / WeChat / Hermes LAN 验证记录

本记录属于 PR [#10](https://github.com/Tangbohu09527/CF_agent-gateway/pull/10)。仅修改 Gateway；没有连接 CFserver 或既有 AI 主机，没有读取生产 Token、Session、数据库、备份或业务文件。本轮没有部署生产、发布镜像、创建 Tag 或合并 PR。

## 基线与固定依赖

- 初始工作区为干净 detached `c5518aed12b90235f118ed81bb3cef75d0463443`；先 fetch，再核对 remote、HEAD、origin/main、status、diff、同任务分支/PR及 worktree。
- remote：`https://github.com/Tangbohu09527/CF_agent-gateway.git`。分支 `feat/clean-device-deploy-lan` 从已核对的 `origin/main` = `4f13039b86c60bc94340edb5468f0102d62d2dff` 创建，没有修改 main 或占用其他 worktree。
- WeChat 固定审查提交：`67cbbb04ce15703428ce165ac38effac19f4b701`。[PR #7](https://github.com/Tangbohu09527/CF_agent-wechat/pull/7) 已于 2026-09-06 合并，merge commit `81d21f451adc61df71077ca3035adeddeca4621f`；组合测试选择已合并的审查 Head，并由 CI 核对合并状态。
- 总文档只读固定提交：`8f51cd095c6967f806701b703281b08e5144296f`。两个依赖仓库均未修改。
- Controller 与修改前文件完全一致，Git blob `3039481583437d95e512925fcfdc3c0b51944d08`；组合测试执行正式固定路径的真实文件。

## 实施清单与测试对应

| 实施项 | 正式入口/资产 | 验证 |
| --- | --- | --- |
| Debian 13 amd64 系统与固定源码、Controller | `deploy/install-clean-device.sh system/controller`；复用固定 WeChat 系统准备 | A 空目录/空 daemon；静态 Contract 先于 Token/数据库；非 root 管理人与0750根目录 |
| 安全配置与重复执行 | `configure`；受保护 JSON/独立环境凭据 | POSIX 安装器测试；A 错误输入、冲突、重跑摘要与数据保护 |
| 可执行源码构建 | `build` + 原有 `docker/Dockerfile` | 实际拉基础 digest、构建、依赖解析版本、Git SHA与Image ID证据 |
| 网络与独立 PostgreSQL | 正式 production Compose + `deploy/postgres/compose.yml` | A 同一cf-internal配置、空库迁移、独立project/volume、无host端口、重启持久化 |
| 新库身份、授权、Profile与路由 | `initialize` / `cf_agent_gateway.initial_identity` | 原业务stores单事务；重复/冲突/回滚测试；真实最小身份文本链 |
| 启动顺序与组合 Gate | `start`、固定WeChat Bootstrap/fresh QR、原Controller | A真实管理脚本与容器；原Compose E2E恢复/超时/保护组件检查 |
| WeChat chatId接口 | 正式Polling解析五个已记录字符串别名；冲突失败关闭 | 保留A的chatId响应；46新增参数化回归，受影响Polling/Runtime测试219通过 |
| Hermes LAN与协议 | `cf_agent_gateway.hermes.diagnose` | 默认无HTTP/模型；显式POST opt-in；A真实容器访问外部替身及认证/超时/停止/不可达检查 |
| systemd与重启 | 独立database/core units + B入口 | B要求真实PID1 systemd、宿主boot ID变化；本机环境阻断，未执行 |

详细使用入口：[首次安装](../deployment/clean-device.md)、[最小业务身份](../deployment/initial-identity.md)、[Hermes 来源与 LAN](../deployment/hermes-lan.md)、[A/B 可执行验收入口](../../tests/deployment/README.md)。

## A：代码与真实容器组合回归

已核对的固定 Gateway 候选为 `8374689590504e36d7a1a1676c503c7142286d6d`，配对上述固定 WeChat。该候选两条 A CI 均通过：[PR事件](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34023220896)、[push事件](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34023219261)。对应[原有CI](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34023220877)全绿：Linux全量 **1235 passed / 1 skipped**，迁移47、真实PG迁移1、V2 E2E6；Lint/Format/UTF-8链接、逐文件bash -n和原生产Compose E2E通过。唯一skip是另一步单独启用执行的真实PG迁移fixture。

A-result的35项检查全部通过，实际构建/迁移/非root管理与Gate顺序、错误认证/不可达/超时/停服、配置重跑与PG重启持久化均有stage日志。文本标记 `clean-device-A-770022369e9baa9849c3d84a`：message=1、delivery=1、delivery attempt=1、Wechat polls=8、模拟微信实际投递=1；真实Gateway关联identity/Profile/thread并记录Dispatch success和Response delivered。该标记模拟Hermes执行=1由该固定runner断言，增强版另将请求字段和计数落入text-chain证据。

| 构建记录 | 本次实际值 |
| --- | --- |
| Gateway Image ID | `sha256:efe4aa644a9666f6d2dd6dc095cb1b469dfd509bf1a55bdc21e108669143f9b2` |
| Python基础镜像digest | `python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254` |
| PostgreSQL digest | `postgres@sha256:bb3e1a57e5407e0a5280b4211980a5e537f4abd234a87014ac979849a78dd825` |
| PostgreSQL Image ID | `sha256:5f71c21b69a7977b82247582e2e731ed76bdebaadb7dd7945ed76bcc9ed06632` |

完整解析版本在artifact `python-packages.txt`，含构建工具；例如alembic1.19.2、httpx0.28.1、psycopg3.3.5、SQLAlchemy2.0.52。镜像仅构建于空白隔离daemon，不发布；固定源码并不意味着逐字节相同镜像。

原始脱敏日志位于运行34023220896的artifact `clean-device-A-34023220896`：`A-result.json`、`text-chain.json`、`source-versions.json`、各image JSON、`python-packages.txt`、`system-packages.txt`、`runtime-final.log`及各stage日志。artifact按CI保留14天，应在到期前下载归档；本次本地已保存于忽略目录`.task-artifacts/ci-34023220896/clean-device-A-34023220896/`。runtime日志没有ERROR/CRITICAL/Traceback；Secret扫描通过，实际遮挡计数为0。

保留的失败与增量历史：

- `e48e0770485b4707c4470d37cace9020694f3fb3` 的[原有 CI](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34022553745)通过：Linux全量1189 passed / 1 skipped（另步启用真实PG迁移fixture），迁移47 passed、真实PG迁移1 passed、V2 E2E6 passed、Lint/Format及原生产Compose E2E通过。
- 同一提交的[A回归](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/34022553746)真实发现Polling漏读chatId；安装、fresh QR管理和Gate已通过，但没有文本闭环，故该运行为失败。随后修复正式Gateway解析器，没有把替身改成旧id字段。
- 更早运行分别暴露sudo会话前提、正式systemctl依赖和Controller E2E配置不一致；保留CI历史，不把它们记为成功或靠预造关键资产跳过。

A真实组件：Debian包、隔离Docker daemon、PostgreSQL、源码构建Gateway、固定Controller、固定WeChat管理脚本与非root管理账户。外部微信HTTP/WebSocket/进程、Hermes HTTP，以及普通Debian容器中四项systemd观察明确为替身。A不能证明真实扫码、真实模型、Hermes来源或完整systemd启动。A数据库模式为managed；外部TLS PostgreSQL入口的前检/错误首次凭据安全重试有单元测试，尚未在另一个真实外部TLS数据库执行组合验收。

## B：真实启动 Debian 主机/VM

未执行。本机是Windows工作区，没有Docker/WSL命令；Hyper-V工具和服务存在，但只读`Get-VMHost`/`Get-VMSwitch`被本机OS权限拒绝，因此不能取得可用隔离VM执行权限。没有尝试连接其他主机或把普通Debian容器当作B。

交付 `tests/deployment/accept_booted_debian.py before-reboot/after-reboot`：拒绝已有项目和容器环境，调用正式安装，保存配置/凭据摘要；人工批准重启后要求真实boot ID变化、Docker与核心服务自动启动、数据库/身份持久化、Gate关闭和WeChat等待fresh QR。需要获准的干净Debian13 amd64/systemd VM或测试主机及可交互sudo的非root管理账户。

## C：真实微信、Windows AI主机与Hermes

未执行，保持待批准人工验收。用户明确本轮不授权连接现有设备，且Hermes来源、版本、包装层仍未核实；已在获准代码及历史记录中继续只读核查，不能用同名开源项目或通用OpenAI-compatible服务器代替。

尚缺具体输入：正确发行者/仓库及固定版本/制品摘要、包装层固定SHA或不存在的说明、受支持Windows/WSL环境、provider/model/Profile配置来源、认证/监听/会话/幂等与响应契约、实际安装/服务/任务及进程和主机重启方式。不索取Key/Token。完整AI主机安装/自启教程与真实LAN对接仍未完成；仓库已交付可证实Gateway配置、精确来源防火墙步骤、默认无业务容器诊断和显式付费POST协议/应用检查。

另一外部依赖限制：如扫码前无法取得批准account_id，固定WeChat fresh QR目前没有post-auth hold入口。需要WeChat最小配套新增认证/API通过后保持Gate关闭并返回可配置身份的受控流程；证据和范围见[业务初始化说明](../deployment/initial-identity.md)。本PR不跨仓库实现，不伪造身份或ready。

## 后续真实设备验收步骤

1. 审核PR固定提交，提供干净Debian13 amd64/systemd隔离设备、非root管理人、固定WeChat镜像及批准的最小身份资料；先补全正确Hermes来源证据。
2. 按首次安装教程从GitHub固定提交获取入口，执行system→Controller静态Contract→非root WeChat configure/Bootstrap→build/configure→独立DB→空库迁移/身份初始化→Gateway/Dispatch就绪，保持Gate关闭。
3. 在批准的新AI测试主机按确认来源安装正确Hermes，配置独立provider/API凭据与批准模型/Profile，精确限制Gateway来源；从真实Gateway容器做默认网络检查，再显式批准POST模型探测及契约/应用验证。
4. 在隔离Debian执行B before/after真实reboot检查；按正确Hermes来源完成AI进程及主机重启、自启和容器复验，不混同登录启动与开机启动。
5. 以真实WeChat管理脚本fresh QR并人工扫码；认证/API通过后打开组合Gate。发送唯一批准文本，核对Message→Admission/Thread/Profile→Dispatch→Response→Delivery及一次真实效果，未授权用户不能触发AI。
6. 留存脱敏固定版本/image/依赖记录、状态和计数；验证错误认证、网络不可达、超时、服务停止与安全恢复。重复安装必须保留有效配置、Secret和数据。

本轮不涉及文件/图片、FileBrowser、Skills、ERP或新架构。最高“新设备+真实微信+正确Hermes”验收在B/C与上述缺少输入补齐前保持未完成。
