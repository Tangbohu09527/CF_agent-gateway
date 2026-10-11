# 可由统一部署入口调用的配置启用/回退

此段仅描述已经实现的配置资产 helper，不表示现场已执行。入口为本仓
`cf_agent_gateway.hermes.return_bridge.enablement`，Python API 与模块 CLI 共用实现；
没有增加服务、安装器、EXE、下载器，也不会调用 subprocess、网络、模型或服务管理。
现有 `deploy/install-clean-device.sh` 仅适用于首次安装，本功能没有将其改成现用环境升级器。

2026-10-07 补充：两台机器的可执行规划/维护窗口衔接现在见
[现场准备入口](../deployment/artifact-return-site.md)。它们复用本 helper 和已有 Controller /
官方 Hermes 管理命令；下面的低层 helper 自身仍不负责服务管理。现场尚未应用。

固定兼容目标是官方 Hermes `0f4a98f87c17007b81500239d0bd5b9574027b73`。
统一部署入口必须先核验安装源码与选定执行器，不能仅凭 manifest 中的版本常量视为现场已核验。

## 输入与保留范围

- Hermes 侧：现有 config.yaml 路径、该 Profile 的 plugins/cf-artifact-return 目标目录、
  本插件 BridgeSettings 的非敏感引用（Gateway HTTPS origin/CA、任务父目录、执行 Profile
  引用/修订、TLS host/port/cert/key 引用）。不提交新的 model/provider/API Key/Skill 配置。
- `plan_hermes()` 读取并保留现有 model/provider/options、API key 字段、其他插件及原工具选择；
  只启用本插件、在现有 `platform_toolsets.api_server` 列表追加 `cf_artifact_return`。
  为防无意改变既有有效工具选择，缺少显式列表且未提供已核验的 `inherited_api_toolsets`
  时拒绝规划；Windows 入口按固定官方源码与认证只读工具目录核验并保留有效列表；
  不回退为全工具。
- 官方 `PlatformConfig.from_dict` 会将顶层 host 提升到 extra，显式 extra.host 优先。
  helper 将已有 extra.host 固定为 127.0.0.1；顶层 host 同时存在时也固定，避免歧义；
  没有 extra.host 时显式设置顶层 host，覆盖 API_SERVER_HOST 的缺省回退。
  原 port（包括已知 8642）保持不变。实际 adapter 必须独立验证 loopback 才启用插件 TLS。
- TLS 由本插件在官方现有 Application/runner 上新增 TLS site，字段是插件的
  `tls_host/tls_port/tls_cert_file/tls_key_file`，不是捏造的官方 TLS 字段。
- Gateway 侧 `plan_gateway(config_path, delta)` 只接受 `hermes.base_url/ca_file`、
  `api.max_request_body_bytes`、
  `artifact.storage_root`、`artifact_return.*` 白名单；不会改变 model、API Key 引用、数据库、
  Worker、legacy_runtime_confirmed 等。默认 Feature 仍关闭，调用 apply 才落配置。

## Python API 与 CLI

```python
from cf_agent_gateway.hermes.return_bridge.enablement import (
    plan_hermes,
    plan_gateway,
    apply_plan,
    rollback,
)

# paths/settings/delta 由最终统一入口从已批准部署资产解析，不提示操作者重填已有凭据。
plan = plan_hermes(existing_hermes_config, dedicated_plugin_directory, bridge_settings)
review = plan.summary()  # 路径、受管字段、前后 SHA-256、未完成部署条件；无配置正文。
receipt = apply_plan(plan, private_journal_root, expected_plan_sha256=review["plan_sha256"])
# 回退前统一入口排空任务并关闭本功能；此调用本身不操作服务。
rollback(receipt["manifest"])
```

模块 CLI 的 `--request` 是仅包含引用的 JSON 文件。`side=hermes` 对应
config_path/plugin_directory/settings（以及可选本仓 package_source）；
`side=gateway` 对应 config_path/delta。它不接受秘密值参数。

```text
python -m cf_agent_gateway.hermes.return_bridge.enablement plan --request <引用JSON>
python -m cf_agent_gateway.hermes.return_bridge.enablement apply --request <同一引用JSON> \
  --state-directory <私有journal目录> --expected-plan-sha256 <已审核plan摘要>
python -m cf_agent_gateway.hermes.return_bridge.enablement rollback --manifest <本次manifest>
```

这里不提供猜测的现场 HTTPS URL、证书位置或已安装标记。
不同机器各在自己的受控入口调用，helper 不创建 SSH 通道、不复制跨机秘密。

## 资产、备份与冲突

`plan_hermes` 仅复制本仓明确列出的薄 Python bundle 和官方插件 shim/manifest，包含
root init、惰性 Hermes init、TLS 与 return_bridge 核心；不复制数据库/安装器，不带依赖环境。
目标同名文件若存在且字节不同即拒绝，不覆盖未知模块，也不遍历清理目标目录。

plan 的内存对象持有拟写配置，但 repr 与公开 JSON 只显示字段名、路径和摘要；
现有配置中若有 inline Key，也不进入公共 plan/log。
apply 先保存原始及拟写配置字节，再写受管资产；POSIX journal 为 0700/0600，Windows 使用
protected owner/System DACL。现有 POSIX owner/mode 与 Windows target DACL 保留。
原始字节可能含既有敏感配置，因此 journal 必须留在管理者私有存储，不能上传 PR 或普通日志。

每次操作使用带固定标识的 OS advisory lock，进程硬退出会释放；未知旧锁仍拒绝并保留。
保存 plan 摘要、每份资产 before/after 摘要，逐份原子替换。
同一 plan 可幂等完成或恢复部分完成；原始备份/审计不删除。回退前先核对全部输出和备份，
出现后来变更、受管路径被替换、备份损坏或 manifest 摘要错误即拒绝覆盖。
新建文件只在摘要与本次 manifest 完全匹配时删除，不递归删除目录或未知文件。
跨文件不是事务文件系统；统一入口必须保持排他的部署窗口，外部写入者须遵守该窗口。
helper 会记录 prepared/applied/rolling_back/rolled_back，部分中断可据同份 journal 恢复。
CLI 在新进程中凭原 request 摘要和已审核 plan SHA 从私有 before/after 恢复，不对
部分完成的当前配置重新规划后扩大权限。若首次 journal 尚未写 manifest 就中断，
目标尚未改变，报告 `incomplete_private_journal` 并保留残留，不自动删除或猜测恢复。

## 统一入口调用的最少现场动作

1. 经批准窗口核验实际源码/执行器和未决任务，自动读取并保留现有配置引用，生成并审核 plan。
2. 提供两向可达、主机名验证正确的 HTTPS endpoint 与公司 CA：Gateway 的 hermes.ca_file
   显式传入 SSLContext，Hermes 插件 gateway_ca_file 也显式传入；trust_env=False，禁止重定向。
3. 将 plan.required 中 CA 的容器只读映射、私有 Artifact 共享存储和服务身份校验接入原统一部署。
   本 helper 只输出 requirements，不擅自生成猜测的 Compose mount。
4. `CF_GATEWAY_ARTIFACT_RETURN_KEY` 或已批准独立签名环境引用只注入 Gateway API/Dispatch，
   不追加到现有 x-runtime 的共享 env_file。Hermes 永不获得签名秘密，只接收本次 capability。
5. 在统一入口执行上述配置资产 apply，再按原服务管理体系加载新模块/配置；helper 不启停服务。
   TLS/模块/ACK 与独立会话验证通过后才打开 Gateway 的功能开关。
6. 回退时先关闭功能、排空/撤销当前回传并保留 Delivery uncertain，再使用原 journal 恢复配置；
   由统一入口恢复受控服务启动参数。无需重填模型/Token，不修改旧数据库或重派历史任务。

以上步骤本轮仅在隔离目录测试，现场未执行。WeChat 接收端仍需另行审批实收，不能用本 helper
或 HTTPS 回执宣布微信已收到。
