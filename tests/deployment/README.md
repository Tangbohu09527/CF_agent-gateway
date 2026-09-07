# 干净设备分层验收入口

这些入口只用于明确指定的隔离测试环境。正式安装说明见
[clean-device.md](../../docs/deployment/clean-device.md)。不连接已有 CFserver、AI 主机或生产数据。

| 层级 | 执行入口 | 真实组件 | 替身与边界 |
| --- | --- | --- | --- |
| A | `run_clean_device.sh` / `clean-device.yml` | Debian 13 包、独立真实 Docker daemon、源码构建、PostgreSQL、Gateway、固定 Controller、固定 WeChat 管理脚本、非 root 管理身份 | 外部微信 HTTP/WebSocket/进程、外部 Hermes HTTP；普通容器仅模拟 Bootstrap 的四项 systemd 查询。不能证明系统启动或真实扫码/模型 |
| B | `accept_booted_debian.py before-reboot`，人工重启后 `after-reboot` | 已启动 systemd 的 Debian 13 amd64 VM/隔离主机、正式安装、宿主重启 | 禁止容器和 Bootstrap 测试覆盖；不调用模型，不扫码。无 VM 条件时必须写未执行 |
| C | 部署教程中的人工业务验收 | 批准后的真实微信、Windows AI 环境、已核实 Hermes 来源与协议 | 本 PR 不执行，不能用 A 的模拟回复代替 |

## A：隔离 Docker 组合回归

Linux CI runner 执行：

```bash
export GATEWAY_COMMIT="$(git rev-parse HEAD)"
export EVIDENCE_DIR="$(mktemp -d)"
bash tests/deployment/run_clean_device.sh
```

外层容器只有只读源代码输入和证据输出目录；不挂载宿主 Docker socket，也不预填部署配置。
内层先检查不存在项目、数据库、Token、Session 和 Docker 存储，通过正式 `system-packages`
入口安装系统/Docker及生命周期所需的真实 systemctl 包，再启动空白 daemon。A 检查该
依赖确实由正式入口提供，并记录 systemd 包版本；不启动 PID 1 systemd，也不计作 B 通过。镜像、网络、目录、凭据、空库迁移及业务身份均由
正式安装入口和固定 WeChat `configure`/Bootstrap 创建。测试仅供应必要输入及测试管理账户的真实密码认证，使用该隔离账户的全局 sudo
时间戳衔接非交互子进程；保留 Debian sudo 组的密码认证规则。另有独立 PTY 检查临时恢复
默认 `timestamp_type=tty`、确认 `use_pty` 开启并清除缓存，实际调用正式 B helper 认证后读取
真实 Controller Contract；只有密码提示出现且终端关闭回显后才输入测试密码，不记录终端内容，
最后核对 A sudoers 文件字节/属主/权限恢复。`sudo-default-tty-probe.json` 只证明此认证流程，
不证明 systemd 启动或宿主重启。测试镜像通过隔离本地 registry 的 digest 引用进入真实 WeChat 管理流程。

验证顺序及证据：

1. root:root 0750 Gateway 根目录，管理人不在 root/docker 组；无 Token/数据库时真实静态 Contract。
2. WeChat 固定检出、digest 镜像身份检查、正式 Bootstrap；尚无 Runtime/Session。
3. 仅基础配置和模型输入，省略 `initial_identity_file`、机器人 ID、员工、群聊和业务路由；实际源码构建、独立 PostgreSQL 空库迁移、Gateway/Dispatch 核心启动。显式提供畸形或不存在的身份文件必须失败且可安全重试。
4. 默认诊断无模型调用；真实 Gateway 容器对合成 Hermes 验证网络、认证、V2 协议及故障。WeChat 未启动时诊断准确报告服务不可达。
5. 固定 fresh QR 管理脚本创建真实 runtime，合成扫码事件先挂起：正式账号诊断报告未登录且 Gate 关闭；模拟扫码完成后由真实管理脚本打开组合 Gate，诊断从 auth API 自动取得当前账号。
6. 无授权的新消息经真实 Poll/Admission 持久化拒绝，Hermes 执行和业务回复都不增加。正式 `business_access discover` 提供观察信息，`approve --message-id` 从记录派生账号/发送者/会话，批准文件只包含明确员工和 Profile。
7. 批准不重放旧拒绝消息；之后唯一标记的新文本通过真实授权、路由、Dispatch、响应和 Delivery，重复轮询仍只执行/投递一次。同一发送者的未绑定新私聊也必须持久化拒绝；正式补绑定后旧消息仍不重放。
8. 缺失/畸形 auth 账号和错误认证失败关闭；同账号注销/重登不重置 checkpoint 或重放历史。切换账号保留独立 checkpoint、消息历史、授权和线程；证据只读查询真实 ORM，不修改数据库。
9. 正式 `disable` 后新消息被拒绝；重跑基础配置/数据库/迁移/可选初始化/启动/诊断不恢复停用授权，不覆盖 Secret 或历史。保留非 root 管理用户、0750 Gateway 根、实际服务 UID 分离及无 sudo 私有路径访问拒绝。
10. 独立 PostgreSQL 重启保留所有消息；停止 WeChat 只关闭 Poll/Delivery，Dispatch 不受 Controller 管理。证据扫描不含生成的 Secret。

输出包含 `A-result.json`、`login-discovery.json`、`business-onboarding.json`、
`unconfigured-route.json`、`account-isolation.json`、`text-chain.json`、固定提交和阶段日志。
文本链 counts 仅统计批准后的主标记；模拟 Hermes 执行次数不代表真实 Hermes 或模型供应商调用。
此前被拒绝的消息和新账号消息另行保留，不作为成功链计数。A 的所有账号、扫码事件和外部回复均为合成数据。

CI 从 GitHub 固定 PR Head 执行安装，记录 WeChat PR #7 的合并结果并使用
`67cbbb04ce15703428ce165ac38effac19f4b701`。本地执行也要求该 Gateway Commit 可从 GitHub 获取；
未提交本地修改不可能冒充固定 GitHub 版本的安装验收。

## B：启动了 systemd 的隔离 Debian 13 主机/VM

CI 自动创建并重启真实 Debian VM 的入口见 [booted-vm.md](booted-vm.md)。它调用下面同一个正式 before/after 入口，使用固定官方镜像与独立 SSH 身份；不能用 A 容器或能力探测代替。

必须先提供已设置密码、获准使用标准 sudo 规则的管理账户（非 root，非 root/docker 组）。
初始 root 操作者必须在真实交互 TTY 中运行 `before-reboot` 和 `after-reboot`，不能从无 TTY
的 CI、管道或重定向输出启动；入口在首次安装变化或重启后报告修改之前检查
stdin/stdout/stderr，缺少 TTY 时明确停止。
输入配置及固定版本准备方式见正式教程。`before-reboot` 拒绝已有项目目录或安装状态；
不覆盖既有设备。变量均是操作者的必要输入，不能保留尖括号占位值：

```bash
python3 tests/deployment/accept_booted_debian.py before-reboot \
  --manager "$MANAGER" \
  --gateway-commit "$GATEWAY_COMMIT" --wechat-commit "$WECHAT_COMMIT" \
  --inputs "$INPUTS_JSON" --wechat-image "$WECHAT_IMAGE_DIGEST" \
  --wechat-runtime-uid "$WECHAT_UID" --wechat-runtime-gid "$WECHAT_GID"
```

正式 `system` 完成后，入口先运行 `sudo -H -u "$MANAGER" -- sudo -v`，由 sudo
直接在终端提示操作者输入**管理账户密码**。每个 WeChat 管理阶段随后使用一次外层
`sudo -H -u "$MANAGER" -- python3 ...`：其中先执行继承 stdio 的 `sudo -v`，再由同一
管理用户进程捕获执行正式脚本，认证与脚本共用该终端/session 的标准 sudo 时间戳。
阶段间可能再次提示密码，不能假定先前另一条外层 sudo 的时间戳仍可复用。

这是为兼容 Debian 默认 `use_pty`：不同外层 sudo 可能创建不同 PTY，单纯先验证再启动
另一条外层 sudo 不能保证时间戳可用。参见 [Debian sudoers 手册](https://manpages.debian.org/trixie/sudo/sudoers.5.en.html)。
认证调用完整继承 stdio/控制 TTY，不读取、保存或记录密码；管理脚本的捕获仅发生在认证
完成之后。不要使用 `sudo -S`、`NOPASSWD: ALL`、关闭 `use_pty` 或调整全局时间戳代替认证。
认证失败时停止并检查账户授权。
`after-reboot` 也在单个管理用户 helper/session 内先交互 `sudo -v`，再执行
`sudo -n -- /opt/cf-agent-gateway/deploy/wechat-runtime-control status`，要求退出码为 1
且 JSON 的 `ready` 严格为 false。首次安装并不创建 NOPASSWD 规则；此处依赖管理账户
真实获准的标准 sudo 权限和当前 TTY 认证，不扩展为免密码权限。

此入口调用正式 `system`、固定 Controller、WeChat configure/Bootstrap、Gateway 配置/构建/
数据库/迁移/核心启动/默认无模型诊断和 `boot-service`，不要求 initial_identity_file、员工、群聊或机器人 ID。
业务状态独立报告；不启用 WeChat 自动开机，不扫码。CI 在首次空业务重启通过后，另用正式业务入口开通测试配置并第二次重启核对持久化，替身边界见 [VM说明](booted-vm.md)。
记录位于 `/var/lib/cf-agent-gateway-install/boot-acceptance.json`：父目录要求 root:root 0700，
报告从临时文件创建时即为 root:root 0600，文件与目录 fsync 后原子替换。读取/更新拒绝
符号链接、硬链接、错误属主或权限；不会先写出可公开读取的报告再补权限。报告只含固定
版本、状态及配置/凭据摘要，不含密码或 Token 正文。

确认这是可重启的隔离测试设备后，由操作者执行宿主重启。重连后 Debian root 在真实
交互 TTY 执行，并在 sudo 提示时输入管理账户密码：

```bash
python3 /opt/cf-agent-gateway/tests/deployment/accept_booted_debian.py after-reboot \
  --manager "$MANAGER"
```

只有内核 boot ID 变化、配置/凭据摘要不变、Docker 与 Gateway/Dispatch 自动启动、数据库迁移
和当前业务配置概览可读、Poll/Delivery 仍关闭、WeChat 未运行时才记录 B 通过。`docker restart`
或普通 Debian 容器无法满足 B。后续必须按教程进行批准后的 C fresh QR 与真实 Hermes 文本验收。
