# GitHub Actions 上的真实 Debian VM（B）

入口是 `bash tests/deployment/run_booted_vm.sh`；`clean-device.yml` 的独立 B job
在一次性 GitHub-hosted Ubuntu amd64 runner 上执行。它启动自己的 Debian 内核和 PID 1
systemd，调用仓库已有的 `accept_booted_debian.py before-reboot`，重启 guest，然后调用
同一入口的 `after-reboot`。不能将普通 Debian 容器或能力探测文件当作 B 通过。

## 固定来源与必要输入

- Debian 官方固定镜像：
  [debian-13-generic-amd64-20260831-2587.qcow2](https://cloud.debian.org/images/cloud/trixie/20260831-2587/debian-13-generic-amd64-20260831-2587.qcow2)。
  下载前读取该目录的 [SHA512SUMS](https://cloud.debian.org/images/cloud/trixie/20260831-2587/SHA512SUMS)，
  要求其中记录与代码固定的 SHA512 相同，再验证实际下载的字节。
  SHA512 为 `5a069019420fb9441ad4f8004c661fadb747edd5662ca54a17c8f923dee7d717e21dbdaa4ba72d6fce7f920e0217f0a9af382298a7d46ed4bc9dc33ac19181b6`。
- Gateway 来自 Actions checkout 的固定 PR Head；`GATEWAY_COMMIT` 必须与实际 HEAD 一致。
  guest 的初始源码由该 Git 对象导出；正式安装仍从 GitHub 检出并核对完整提交。
- WeChat 管理代码固定 `67cbbb04ce15703428ce165ac38effac19f4b701`；上游镜像采用其
  `docs/deployment/clean-device.md` 已记录的 amd64 digest
  `ghcr.io/thisnick/agent-wechat@sha256:b5e92047e28ce67e34576e574d8ccf00f8619f485597109f7342a137300285c0`。
  正式 configure 实际拉取并检查服务 UID/GID 1000，不启动微信运行进程。
- cloud-init 只提供初始管理账户和临时 root SSH 公钥。管理账户 `cf-b-manager` 的 UID
  为 1100，属于标准 sudo 组，使用本轮随机密码；没有新增免密码 sudo 规则。这是 VM
  必要账户输入，不证明 Gateway system 阶段自动分配了该 UID。
- 其余 fixture 仅提供独立随机 Hermes API Key、明确批准的最小私聊测试身份和 managed DB
  配置输入。没有模型供应商 Key。Token、目录、网络、数据库、Gateway 配置、业务授权和
  路由都由正式入口生成；不预先修补关键安装资产。

[cloud-init NoCloud 文档](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
说明 seed 的元数据/用户配置机制；[用户模块文档](https://docs.cloud-init.io/en/latest/reference/modules.html#users-and-groups)
说明账户、密码和 sudo 输入。

安装前实际检查 Debian 13/amd64、PID 1 为 systemd、非容器，且项目目录、安装状态、
独立数据库目录、微信 storage、Docker 存储和 docker 命令全部不存在；保存只读结果。
若固定 cloud image 不满足这些条件则失败，不凭镜像名称声称空白。

## 虚拟机条件与边界

[GitHub 官方说明](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners)
指出嵌套虚拟机技术上可行，但未承诺稳定性、性能或兼容性。能力探测只记录设备权限、CPU
扩展和命令存在性。wrapper 从 Ubuntu 包仓库安装 QEMU、cloud-image-utils、PTY/SSH 工具；
若已有 `/dev/kvm` 和 kvm 组，仅让原 runner UID 的这个子进程使用该组，不放宽设备权限。
随后实际探测 KVM 初始化；无法初始化时记录原因并使用 TCG，不因缺少 KVM 就声称 B 不可能。

[QEMU 官方说明](https://www.qemu.org/docs/master/system/introduction.html)
确认 KVM 与 TCG 都可用于整机 guest OS；实际选用方式写入 B 结果。使用独立 16GiB 稀疏
磁盘和 4GiB 内存，镜像下载最多 20 分钟，guest 启动/重启各最多 15 分钟，正式阶段各最多
60 分钟，整个 CI job 最多 90 分钟。超时失败保留具体阶段，不降级为容器验收。

SSH 只连接本次 QEMU 的 `127.0.0.1` 端口转发；没有生产 host 参数。
[QEMU hostfwd 文档](https://www.qemu.org/docs/master/system/invocation.html)
说明可将转发限定到指定宿主接口。使用临时私钥、独立 known_hosts，禁止 SSH agent、
ProxyCommand/ProxyJump、控制连接复用和既有 SSH 配置。PTY 自动回答的只是这个一次性 VM
的管理密码；标准 sudo 实际验证密码，不使用 `sudo -S` 或全局时间戳配置。

Hermes 是 runner loopback 上的 **TCP-only 替身**；guest 通过 QEMU 的隔离用户网络访问。
正式默认诊断仅建立 TCP 连接，不发送 HTTP、API 认证、微信、模型或业务请求。B 不证明
真实 Windows/Hermes 局域网业务，也不替代 C。after-reboot 对 Docker/core 的启动状态最多
只读等待 240 秒，观察失败即停止，不手动启动服务修好验收。

## 证据与失败处理

只上传经过已知测试 Secret 扫描的 `B-result.json`。其中包括固定来源与摘要、实际加速
方式、正式阶段、前后 boot ID、配置/凭据摘要、这台 VM 实际构建的 Image ID/base digest/
依赖解析记录、系统包版本、管理账户组和运行中 Gateway/PostgreSQL PID 1 身份。

不上传 qcow2、seed、cloud-init 用户配置、SSH 私钥、原始 serial/SSH 日志、Token、Session
或 STATE 目录。临时材料位于本次创建的 0700 目录；清理只终止本次持有的 QEMU PID 并移除
该目录。失败结果包含失败阶段及稳定错误码，不能写成已安装或 B 通过。真实结果必须以
对应 GitHub run 的 `result` 与完整固定 Head 为准；本说明本身不构成验收证据。
