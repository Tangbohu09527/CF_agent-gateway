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
时间戳衔接非交互子进程；保留 Debian sudo 组的密码认证规则。测试镜像通过隔离本地 registry 的 digest 引用进入真实 WeChat 管理流程。

验证顺序及证据：

1. root:root 0750 Gateway 根目录，管理人不在 root/docker 组；无 Token/数据库时真实静态 Contract。
2. WeChat 固定检出、digest 镜像身份检查、正式 Bootstrap；尚无 Runtime/Session。
3. Gateway 安全配置、实际源码构建、错误顺序迁移失败后的安全重试、独立 PostgreSQL 空库迁移和正式 V2 身份初始化。
4. Gateway/Dispatch 就绪且 Poll/Delivery 关闭；从真实 Gateway 容器验证外部模拟 Hermes 的网络、认证、全部 V2 字段、会话响应，以及认证错误/超时/服务关闭与重启。
5. 真实管理脚本 dry-run、fresh QR 交互流程（明确为合成 QR/外部进程），认证/消息 API 后真实 Controller 打开组合 Gate。
6. 唯一标记文本通过真实 Poll、授权、路由、Dispatch、响应和 Delivery；只读 Admin API 核对关联身份/Profile/线程/状态，重复轮询不重复调用/投递。
7. 配置/凭据/Token/数据库/初始化重跑保护；独立 PostgreSQL 重启保留文本；停止 WeChat 只关闭 Poll/Delivery，Dispatch 不受 Controller 管理。
8. 证据检查不包含生成的 Secret。输出 `A-result.json`、`text-chain.json`、固定提交与 stage 日志。

CI 从 GitHub 固定 PR Head 执行安装，记录 WeChat PR #7 的合并结果并使用
`67cbbb04ce15703428ce165ac38effac19f4b701`。本地执行也要求该 Gateway Commit 可从 GitHub 获取；
未提交本地修改不可能冒充固定 GitHub 版本的安装验收。

## B：启动了 systemd 的隔离 Debian 13 主机/VM

必须先提供可用管理账户访问（非 root、非 root/docker 组，具备 sudo）。输入配置及固定版本
准备方式见正式教程。`before-reboot` 拒绝已有项目目录或安装状态；不覆盖既有设备。
以下命令由 Debian 初始安装 root 执行，变量均是操作者的必要输入，不能保留尖括号占位值：

```bash
python3 tests/deployment/accept_booted_debian.py before-reboot \
  --manager "$MANAGER" \
  --gateway-commit "$GATEWAY_COMMIT" --wechat-commit "$WECHAT_COMMIT" \
  --inputs "$INPUTS_JSON" --wechat-image "$WECHAT_IMAGE_DIGEST" \
  --wechat-runtime-uid "$WECHAT_UID" --wechat-runtime-gid "$WECHAT_GID"
```

此入口调用正式 `system`、固定 Controller、WeChat configure/Bootstrap、Gateway 配置/构建/
数据库/迁移/初始化/启动/默认无业务诊断和 `boot-service`。不启用 WeChat 自动开机，不扫码。
记录位于 `/var/lib/cf-agent-gateway-install/boot-acceptance.json`（root 0600）。

确认这是可重启的隔离测试设备后，由操作者执行宿主重启。重连后 Debian root 执行：

```bash
python3 /opt/cf-agent-gateway/tests/deployment/accept_booted_debian.py after-reboot \
  --manager "$MANAGER"
```

只有内核 boot ID 变化、配置/凭据摘要不变、Docker 与 Gateway/Dispatch 自动启动、数据库迁移
和初始身份检查通过、Poll/Delivery 仍关闭、WeChat 未运行时才记录 B 通过。`docker restart`
或普通 Debian 容器无法满足 B。后续必须按教程进行批准后的 C fresh QR 与真实 Hermes 文本验收。
