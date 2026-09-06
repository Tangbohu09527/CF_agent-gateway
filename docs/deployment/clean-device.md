# 干净设备首次安装（Debian 13 amd64）

本入口负责 Gateway 首次安装，不要求旧 Release、备份、数据库、Token、Session 或旧镜像。
支持基线仅为 Debian 13 amd64、systemd、local rootful Docker 和 Compose v2。
系统准备复用 WeChat 固定版本的 `prepare-clean-host.sh`，Gateway 的代码、配置、镜像、
数据库资产仍由本仓库管理。已有安装升级请走 [production runbook](production.md)，
不要用本安装器切换版本或重置凭据。

当前 Hermes 安装来源、版本及包装层尚未核实，详见 [Hermes LAN 教程与缺少的输入](hermes-lan.md)。
Gateway 的准备和下述 A 层隔离回归可以独立完成；真实 AI 主机安装和业务验收不能据此写成已完成。
本页是后续新设备操作入口，不是已经现场部署的记录。

## 必填项与版本

| 输入 | 要求 |
| --- | --- |
| Gateway 提交 | 本 PR 最终审核过的 40 位 Git SHA，安装器从固定 GitHub URL取得并校验；不要填写浮动分支 |
| WeChat 提交 | `67cbbb04ce15703428ce165ac38effac19f4b701`；PR #7 已合并，merge commit `81d21f451adc61df71077ca3035adeddeca4621f`，本次选择已合并的审查 Head |
| 管理账户 | 任意合法非 root 账户，例如 `cfoperator`；不在 root/docker 组，UID/所有组均与 Gateway `10001:10001`、WeChat 基线 `1000:1000` 及实际服务身份分开 |
| WeChat 镜像 | 可拉取的完整 registry digest 和经镜像验证的服务 UID/GID，不能用管理用户名推断 |
| Hermes | 正确来源与固定版本、批准服务 URL、模型路由名、Profile 引用/版本、独立 API Key 的受保护文件；不要传 provider Key |
| 初始业务身份 | 一个批准员工 ID、机器人 account_id、员工 sender_id、private chatId；见 [正式初始化入口](initial-identity.md) |
| PostgreSQL | 默认 managed；或带独立凭据、容器可达地址和显式 TLS 的外部 `postgresql+psycopg` URL 文件 |
| 两台设备地址 | AI URL 是安装输入；Gateway 的 LAN 地址作为 AI 主机防火墙允许来源输入，不写死现场地址 |

初始账号 ID 必须来自已批准账户记录。若全新账号扫码前无法取得，当前 WeChat start 在认证/API
通过后立即开启组合 Gate，没有已实现的 post-auth hold 入口；不能伪造 ID 或假 ready。
[初始化说明](initial-identity.md)记录此条件下 WeChat 所需的最小配套范围，本仓库不跨库修改。

## 1. 从固定 GitHub 提交取得入口（新 Debian，初始 root 控制台）

只在批准的新设备执行；不在 CFserver 或已有 AI 主机执行。先填完整提交：

```bash
# Debian 13 amd64 / initial root session
GATEWAY_COMMIT=REPLACE_WITH_REVIEWED_FULL_40_HEX_COMMIT
WECHAT_COMMIT=67cbbb04ce15703428ce165ac38effac19f4b701
MANAGER=cfoperator
[[ "$GATEWAY_COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 1
umask 077
apt-get update
apt-get install --yes --no-install-recommends ca-certificates git
SOURCE_DIR=$(mktemp -d /root/cf-gateway-source.XXXXXXXX)
git -c core.hooksPath=/dev/null init "$SOURCE_DIR"
git -C "$SOURCE_DIR" remote add origin https://github.com/Tangbohu09527/CF_agent-gateway.git
git -C "$SOURCE_DIR" fetch --depth 1 origin "$GATEWAY_COMMIT"
git -C "$SOURCE_DIR" -c core.hooksPath=/dev/null checkout --detach FETCH_HEAD
test "$(git -C "$SOURCE_DIR" rev-parse HEAD)" = "$GATEWAY_COMMIT"
INSTALLER="$SOURCE_DIR/deploy/install-clean-device.sh"
COMMON=(--manager "$MANAGER" --gateway-commit "$GATEWAY_COMMIT" --wechat-commit "$WECHAT_COMMIT")
bash "$INSTALLER" system "${COMMON[@]}"
bash "$INSTALLER" controller "${COMMON[@]}"
```

`system` 初次只补齐入口缺少的 Python/Git/CA/systemd 工具，然后直接执行固定 WeChat 系统安装器；复用它的 Debian/架构、
APT 签名、Docker 冲突包、Compose v2、账户和 systemd 检查。不自动删除冲突包、覆盖 daemon
配置或给管理账户增加 root/docker 组。Gateway 只为缺失管理账户选择 20000 起未占用的 UID/GID，
在 root-only `manager-identity.json` 记录创建意图后建立独立组/账户，再复用 WeChat 的系统依赖准备；
不会重新实现 Docker 安装。中断后重跑会复用同一组与账户。已有 UID=1000/10001、
加入对应服务组、或与实际 WeChat 服务身份冲突的管理账户会在固化版本配置前被明确拒绝；
安装器不会更改旧账户的 UID、GID、文件属主或组成员。请选择另一个独立管理账户。
后续每个安装阶段只读解析 WeChat 固定 `docker/.env` 的实际服务 UID/GID；managed PostgreSQL
也在镜像身份检查时验证隔离并记录，任何身份冲突都保留已有配置并停止。
新建管理账户密码默认锁定，设备管理员自行配置密码/SSH：
`passwd "$MANAGER"`。安装不创建 NOPASSWD 授权；后续管理脚本沿用有明确授权的 sudo helper。

`controller` 安装 `/opt/cf-agent-gateway`，根目录 `root:root 0750`，`deploy` 和固定
`deploy/wechat-runtime-control` 为 `root:root 0755`。先执行真正的静态 Contract v1；
此时没有 Token、数据库、Gateway ready，也不启动 Worker。普通用户无需穿过根目录，
通过已授权的 sudo 执行固定路径。受保护 `/var/lib/cf-agent-gateway-install` 保存阶段记录。

## 2. 管理用户准备 WeChat 并 Bootstrap（Debian，非 root 登录）

重新以管理账户登录，并设置同一提交。系统阶段已安装固定管理安装器到可读 `/usr/local/libexec`：

```bash
# Debian / named non-root manager, real interactive sudo session
WECHAT_COMMIT=67cbbb04ce15703428ce165ac38effac19f4b701
MANAGER=$(id -un)
sudo -v
bash /usr/local/libexec/cf-agent-wechat-prepare checkout \
  --manager "$MANAGER" --commit "$WECHAT_COMMIT"
cd /opt/cf-agent-wechat
WECHAT_IMAGE=ghcr.io/thisnick/agent-wechat@sha256:b5e92047e28ce67e34576e574d8ccf00f8619f485597109f7342a137300285c0
bash scripts/prepare-clean-host.sh configure --manager "$MANAGER" \
  --commit "$WECHAT_COMMIT" --image "$WECHAT_IMAGE" --runtime-uid 1000 --runtime-gid 1000
bash scripts/bootstrap-cfserver.sh
```

镜像引用来自 [固定 WeChat 安装记录](https://github.com/Tangbohu09527/CF_agent-wechat/blob/67cbbb04ce15703428ce165ac38effac19f4b701/docs/deployment/clean-device.md)。
configure 会实际拉取并验证服务 UID/GID，记录实际 Image ID；这不证明真实扫码。
Bootstrap 是 WeChat Token、`cf-internal`、storage/secrets/archive 目录的唯一准备者，
不会创建 Runtime/Session 或打开 Gate。本 Gateway 安装器要求其真实 Token 文件校验通过，
不会自己伪造 Token，亦不修改上述两个依赖仓库。

## 3. 必要配置（Debian，初始管理员/受控 sudo）

用设备上的受控编辑器创建绝对路径、属主为 root 或管理账户、0600 的三个输入文件。
不要把文件放在 Git、普通日志或命令参数里，不要开启 shell xtrace。

1. Hermes API Key 文件：仅该 Key（16–4096 个可见非空白字符），可有末尾换行；不要输出文件。
2. 按 `config/initial-identity.example.json` 创建初始身份 JSON，填写真实批准的账户/Profile信息。
3. 安装 JSON，例如 `/root/cf-install-inputs.json`：

```json
{
  "version": 1,
  "hermes_url": "http://REPLACE_WITH_APPROVED_AI_LAN_HOST:REPLACE_WITH_VERIFIED_PORT",
  "hermes_model": "REPLACE_WITH_VERIFIED_HERMES_MODEL",
  "hermes_api_key_file": "/root/hermes-api-key",
  "initial_identity_file": "/root/initial-identity.json",
  "database": {"mode": "managed"}
}
```

这只是输入模板，未替换时不能通过安装。所有实际配置键以 `config.py` 和安装器解析为准。
`hermes_url` 不含 `/v1`，地址必须从 Gateway 容器可达；不能填写其自身 loopback。
两个可选镜像输入为 `python_image`（默认 `python:3.12-slim-bookworm`）及 `postgres_image`
（默认 `postgres:16-bookworm`）。第一次拉取后记录 registry digest，重跑只拉已记录 digest。

接入外部 PostgreSQL 时将 `database` 改为：

```json
{"mode": "external", "external_url_file": "/root/gateway-database-url"}
```

URL 文件仍为 0600，使用独立角色、空的新数据库和允许创建应用 schema 的迁移权限。
地址须从容器访问；明确 `sslmode=verify-full`/`verify-ca`/`require`，优先校验服务证书。
外部模式在保存任何配置前，从已构建镜像真实连接并只读检查迁移所需的 schema/语言权限；错误凭据可修正输入文件后重试，不会先固化无效配置。临时 0600 环境文件及本次前检容器用后清理；迁移沿用同一 URL，不会管理外部服务器或恢复其数据。

## 4. 配置、构建、数据库与核心启动（Debian，初始 root 控制台）

继续第 1 步的固定变量；后续也可经受控 sudo 调用同一固定安装入口：

```bash
bash "$INSTALLER" build "${COMMON[@]}" --inputs /root/cf-install-inputs.json
bash "$INSTALLER" configure "${COMMON[@]}" --inputs /root/cf-install-inputs.json
bash "$INSTALLER" database "${COMMON[@]}"
bash "$INSTALLER" migrate "${COMMON[@]}"
bash "$INSTALLER" initialize "${COMMON[@]}"
bash "$INSTALLER" start "${COMMON[@]}"
bash "$INSTALLER" diagnose "${COMMON[@]}"
```

安装器不运行 upgrade、restore 或清理数据。`configure` 用安全随机数分别生成 Gateway API、
Admin、PostgreSQL 管理和应用凭据，原子保存并复用；Hermes API Key 与它们分开。
生成的 `runtime.env`、`.env` 为 root-only；配置文件为 `root:10001 0640`，不在 YAML 放密钥。
只有 Poll/Delivery 挂载 WeChat Token（10001:10001、0400/0600）。应用进程仍以固定服务 UID/GID
运行，只有受限的 volume 初始化使用 root。

`build` 不依赖 Hermes 凭据、数据库或业务身份，可以不传 `--inputs` 独立构建默认基础镜像。
`configure` 先在此真实镜像内验证业务身份结构，再原子保存配置，错误输入可以修正重试。

`build` 实际复用 `docker/Dockerfile`，传入已记录基础镜像 digest 与 Gateway Git SHA；
记录依赖解析结果和实际 Image ID，正式 `.env` 使用该 ID。不会依赖旧 daemon、本地 Tag、
`docker save` 或占位 registry。不承诺固定 Git SHA 会生成字节完全一致的镜像。
首次完成后运行核心服务不依赖 GitHub 在线。

managed 数据库由 `deploy/postgres/compose.yml` 的独立 `cf-agent-gateway-db` project 管理，
持久 volume 为 `cf-agent-gateway-db_data`，凭据/资产目录 `/var/lib/cf-agent-gateway-postgres`。
没有数据库 host port；应用角色 `cf_gateway` 不是 superuser，与 postgres 管理账号分开。
数据库和 Gateway runtime 都加入 Bootstrap 创建的 `cf-internal`；应用使用
`cf-gateway-postgres:5432`，微信使用 `cf-agent-wechat:6174`。Controller、迁移和 runtime
读取同一个正式 Compose 和 `.env`，不需要 overlay 或手动 `docker network connect`。

`migrate` 针对空库迁移到打包的单一 Alembic head；`initialize` 通过现有业务 stores 的正式入口
一次事务创建最小身份/Profile/权限/private路由。重复执行比对而不扩权，冲突/禁用状态保留并拒绝。
`start` 先只读确认身份与可执行路由，再启动 Gateway/Dispatch；不会打开 Poll/Delivery。
`/ready` 与容器 healthy 只证明核心状态，不能代替 Hermes、微信认证或文本闭环。

## 5. Hermes 验证、fresh QR 与文本（Debian 管理用户 + 批准 AI 主机）

先完成 [Hermes 来源与 LAN 验证](hermes-lan.md)，从实际 Gateway 容器依次验证网络、
认证、全部 API 字段和应用响应。默认 `diagnose` 只做网络检查；HTTP 认证检查显式启用，
实际业务/模型调用必须 `--allow-model-call`，重复请求探测另需 `--check-replay`。
来源/版本未知时本步骤保持未完成，不进入真实业务验收。

在真实 Hermes 门禁及初始化通过后，以管理用户在受控 TTY 执行：

```bash
# Debian / non-root manager
cd /opt/cf-agent-wechat
bash scripts/start-qr-login.sh --dry-run
bash scripts/start-qr-login.sh
bash scripts/status.sh
```

脚本先以真实 Controller 关闭 Poll/Delivery 组合 Gate，再 fresh QR。人工扫码并验证
认证及消息 API 后才打开 Gate；Dispatch 不归 Controller 管理。`agent-wechat` 仍为
`restart: "no"`，Archive 不自动复用。每次 fresh QR 都通过这一入口。

从已批准测试员工的私聊发一条新的唯一标记文本，确认真实回复仅一次。用正式 Admin 只读 API
检查 Message→Admission/Thread→Dispatch→Response→Delivery 的关联和状态，保留脱敏结果。
不要把 Key 放在 curl argv：可在 Gateway 容器内从已配置环境读取独立 Admin Token后请求
`/admin/messages`、`/admin/dispatches`、`/admin/deliveries`；接口细节见 [API](../api.md)。
未授权身份另发新标记文本应保留拒绝证据而不触发 AI。旧消息的 durable denied 不会因以后授权而重放。

## 6. 重跑、失败诊断与重启

每个阶段以 `[PASS:阶段]` 或 `[FAIL:阶段]` 返回。失败不回显 Secret、完整环境或外部响应。
修正该阶段依赖后重复同一命令；它不覆盖有效配置、凭据、Token、Session、数据库或业务数据。

| 错误/阶段 | 操作 |
| --- | --- |
| system 的包/下载失败 | 检查新设备 APT/GitHub/Docker可达性；冲突包单独审查，不自动卸载 |
| checkout/版本/属主/目录冲突 | 核对具体报错路径，保留现有目录；版本变化走单独升级，不 reset/覆盖 |
| configure 输入或已存在配置不同 | 核对受保护文件/键/权限；重跑不会写入新的 Key，不能当轮换工具 |
| build 失败 | 检查 registry/PyPI/磁盘；基础镜像记录保留，依赖版本记录只在成功后生成 |
| database 认证失败 | 核对独立凭据与实际数据库；不删除 volume。自建初始角色/库的部分创建可幂等续作 |
| migrate/initialize 失败 | 核对 schema/初始身份错误，业务事务回滚；不要手工 SQL 改权限或队列 |
| diagnose 网络/认证/超时 | 按 Hermes 教程逐层处理；TCP成功或HTTP200不是应用成功 |

详细版本记录：`/var/lib/cf-agent-gateway-install/{source-versions.json,python-image.json,`
`gateway-image.json,postgres-image.json,python-packages.txt,launcher-packages.txt}`；系统包版本仍由复用安装器保存于
`/var/lib/cf-agent-wechat-install/system-packages.txt`。其他该目录文件可能含 Secret，不整体上传、
不打印 `secrets.json`、`runtime-values.json` 或 `.env`。不要删除 `cf-internal` 或运行
`--remove-orphans` 清理其他组件。

在有真实 systemd 的新测试设备上安装开机核心服务：

```bash
# Debian / initial root; after all preparation stages
bash "$INSTALLER" boot-service "${COMMON[@]}"
systemctl start cf-agent-gateway-database.service  # managed database only
systemctl start cf-agent-gateway-core.service
```

这是短时 root 的 Compose/systemd 控制动作，容器内应用不改为 root。数据库 unit 与 runtime
分开；停止 runtime 不停止数据库。core unit 在 Docker ready 后显式 Controller stop，再启动
Gateway/Dispatch，不迁移/不联网拉源码。Docker 启动与 stop 之间仍有调度窗口，本页不承诺
boot 前绝无 Worker 自动运行；批准 reboot 前先正常 WeChat stop，boot 后仍显式核对/关闭 Gate。

完整 B 入口见 [tests/deployment/README.md](../../tests/deployment/README.md)：
`accept_booted_debian.py before-reboot`/`after-reboot` 记录真实 boot ID、systemd、固定来源、
目录权限、Gate、数据库和核心 readiness。普通 Debian 容器不能替代 B。Reboot 本身由批准的
操作者执行，之后真实微信重新 fresh QR，再做文本闭环；C 保留待批准人工执行。

## 7. 分层验收

- A：原有全量测试、Lint、Format、Compose E2E，以及 `tests/deployment/run_clean_device.sh`
  的真实 Debian 包/独立 Docker/PostgreSQL/Gateway/Controller/WeChat管理脚本组合回归。
  仅外部微信、Hermes以及普通容器缺少的 systemd观察为明确替身；不证明真实扫码/模型。
- B：启动了 systemd 的干净 Debian 13 amd64 主机/VM正式安装与真实 reboot。
- C：正确来源 Hermes 的 Windows/实际环境安装、认证/LAN/模型、真实微信扫码和唯一文本回复。

本次结果、CI链接、固定SHA、具体阻断见[任务验证记录](../validation/2026-09-06-clean-device-deploy-lan.md)；
历史生产截图/日志不替代本次 A/B/C。
