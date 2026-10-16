# ACK03 现场诊断：工作目录访问被拒绝

本记录接续 [2026-10-13 隔离验证](2026-10-13-artifact-return-403.md)。
2026-10-16 已在后续限定授权内完成诊断插件安装、受控重启和一次真实 ACK03。
随后取得用户针对工作目录的追加授权，完成限定权限修复和普通 Hermes 身份的只读核验。
历史隔离验证、权限核验均不能替代修复后的真实微信端到端验收；本轮没有第二次业务测试。

## 实际版本与业务结果

- Windows 仅部署候选 `f606b9de64b53f7d7b22542451e6ef82e4a95164` 的返回插件
  `plugin.py`，SHA256 为
  `88f1742a9d57dc853b4147b299373e3a83ba32012f1f084afff540a9845b384d`。
- 官方 Hermes 源码仍为 `427d605d4985c2d49a795493ef6c5d66413057d1`。
  使用官方管理入口停止/启动，保持原普通用户令牌；认证探针、监听及新增诊断日志已验证。
- CFserver Gateway 仍为 `d9bae61a043f1be4303e5c23661ec51904d5e708`，镜像仍为
  `sha256:bcb3e42fef5ba5a91232ca04e49a959cbecab4b45eb3bec0a0b9b3aadf717185`；
  未重建镜像、重新 apply 或迁移数据库。
- 用户在原测试私聊实际发送一次 `CF-RETURN-ACK-20261013-03`。
  Message **4294** 正常入库且准入 **allowed**，Dispatch **43** 四次
  `hermes_api_error:http_403` 后 **dead**；Response、Delivery 均不存在。
  截图显示用户已发送，服务端没有生成回复或投递回执；不能称微信验收成功。
- 四个不同诊断 UUID 均为 `stage=ledger`、`reason=return_ledger_unavailable`。
  两个 Windows 日志文件重复记录同组 UUID，不能算成八次请求。
  UUID 不是 Dispatch ID；关联依据是限定窗口、原聊天绑定、四次实际尝试和没有其他在途任务。

ACK01 的首窗口漏收仍是独立问题。ACK02 Message4166 / Dispatch42 的旧记录保留；
没有重派42或43、改 dead、改 checkpoint、发送第四条测试。

## 确定的拒绝位置及证据边界

在实际 Hermes Python 3.14.7、相同普通 Admin 用户令牌下，提取并执行部署文件中
未经改写的 `_check_root`、`_check_ledger_file`、`_windows_pin`。所有祖先目录通过；
`HERMES_HOME/cf-artifact-return-work` 的原始
`CreateFileW(access=1, share=3, OPEN_EXISTING)` 返回 **WinError 5**。
原 `_windows_pin` 在 `plugin.py:212` 将该错误转换成 `return_ledger_unavailable`。
只读审计没有打开 SQLite、读取台账或消费授权。

独立的提权元数据读取确认修复前状态：

- 工作根 owner 为 **Administrators**（`S-1-5-32-544`）。
- DACL 受保护，仅有可继承的 **OWNER RIGHTS** 与 **SYSTEM** FullControl：
  `D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)`。
- Hermes 实际以非提权 Admin TokenUser 运行；其用户 SID 与目录 owner 不同。
- `.return-claims.sqlite3` 及其 `-journal/-wal/-shm` 均不存在。

因此本次实际拒绝发生在台账创建之前：运行身份无权打开受保护的工作目录。
这不是只根据 stage/reason 猜测权限问题。新增现场证据不能还原 ACK02 当时没有保存的
内部日志，也不能确定哪一次历史操作造成当前 owner。

源码审计发现 `enablement._private()` 只设置 OW/SYSTEM DACL，不设置或核验 owner；
`site_windows` 创建或复用工作根后调用该函数。既有健康检查不验证真实运行用户对该
目录的访问。不能通过放宽返回校验、改变 `CreateFileW` 访问常量或自动递归改权限修复。

## 安装器适配与安全收口

本轮只读审计复现了任务授权安装器的权限缺陷：`umask 077` 将请求的 sudoers 候选
`0440` 变成 `0400`。语法检查通过，包含文件权限的 `visudo` 检查失败且要求 `0440`。
旧安装入口丢弃了原始 stdout/stderr，不能声称恢复了旧异常原文。按核实的部分安装状态
仅补齐显式 `fchmod(0440)` 的固定规则及原任务密钥，保留失败包和回执；无交互密钥、
限定 sudo preflight 通过，未获准的 root 命令被拒绝。

2026-10-16 07:07 UTC 的最终 Collect 核验 Poll/Delivery 均停止，无排队或在途任务；
随后撤销本任务公钥和 sudoers 授权。撤销后的单次密钥登录返回拒绝。临时 DPAPI 密码
存储已删除，07:25 UTC 本地复核仍不存在。原任务私钥与诊断证据按要求保留。

## 私有证据索引

以下均位于本次工作树的忽略目录，不提交凭据、业务内容或完整生产日志：

| 证据 | 内容 |
| --- | --- |
| `.task-artifacts/auth-audit-20261016/` | 部分安装状态、visudo 失败证据及成功接续回执 |
| `.task-artifacts/diagnostic-20261016/collect-live-20261016.log` | ACK03 全链路与关闸状态 |
| `.task-artifacts/diagnostic-20261016/collect-revoke-live-20261016.log` | 最终关闸、无在途和撤权；SHA256 `0634c78e59c0e1d7e8709666cdeb979f3da905510a69a59f4278bbc13cf4d1d0` |
| `.task-artifacts/ledger-readonly-20261016/production-readonly-result.json` | 原函数、真实令牌与 WinError5 |
| `.task-artifacts/ledger-readonly-20261016/work-root-acl-elevated-readonly.json` | owner、DACL、台账不存在的元数据 |
| `.task-artifacts/ledger-readonly-20261016/owner-only-fixture-v4/result.json` | 仅改 owner 的方案被实际证伪，不能执行 |
| `.task-artifacts/ledger-readonly-20261016/owner-policy-fixture-v5/result.json` | 原 owner+DACL 复现拒绝；最小修复、子对象不变及重放回归 |
| `.task-artifacts/ledger-readonly-20261016/production-postrepair-readonly-result.json` | 修复后原插件目录访问全部通过；SHA256 `a38df8ff27f0f023b7f716355eed517383e9278e80822604ab450ee2c6d99d65` |
| `.task-artifacts/password-auth/handoff-20261016-live-progress.md` | 按时间追加的执行与接续记录 |

备份与安装证明另存于本机已核验的 MSIX 物理位置
`LocalCache/Local/cf-return403-f606b9d-20261016`；旧 request/plan/journal 保留原样。

## 修复状态

最初的 owner-only 候选被实际隔离验证否定：`SetSecurityInfo(mask=1)` 返回成功，
但 Windows 删除了 OW ACE，普通用户访问仍被拒绝。该候选保留且入口禁用，未执行生产。

修正版仅对固定工作根调用一次 `SetFileSecurityW(0x80000005)`，同次指定已核实的
Hermes 用户 owner 与**原样的**受保护 DACL。这使用原实现同一 Win32 API；它不会将
该目录更新传播给已有子对象，属于有意保留的限定兼容调用，并非引入通用权限管理。
[微软 API 文档](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-setfilesecurityw)
将此 API 标为旧接口，同时明确其不向子对象传播的行为。

| 本轮新增针对性验证 | 实际结果 |
| --- | --- |
| 实际普通 Hermes 身份、原源码、生产原工作根 | 创建台账前的目录 pin 返回 WinError5 |
| 全新 v4，BA owner + 原 OW/SYSTEM DACL；仅改 owner | 拒绝可复现；OW ACE 被删除，候选判失败 |
| 全新 v5，原拒绝状态；同次正确 owner + 原 DACL | 原 pin/consume 成功；DACL bytes、group、control 原样 |
| v5 三个既有合成子对象 | owner、DACL、控制位、文件身份及内容摘要全部不变 |
| v5 同一授权再次消费 | `return_context_consumed`，重放保护保留 |
| 最终候选 PS5.1、实际 interop 编译及内存描述符转换 | 通过；未执行生产主体的检查 |
| 生产修复后的普通 Hermes 原函数只读复核 | 所有祖先和工作根 pin 通过，`first_failure=null` |

v5 使用实际 Python3.14.7 及未经改写的插件函数，没有网络或模型调用。其结果 SHA256：
`97f18a0009cb4eeac8f6efca2bdeba3dc2c2e0c9faa6e99d1b3abd581d1d37d3`。
没有重跑已经完成的双版本全链路矩阵或旧132项认证测试。

用户随后明确批准该限定生产修复。已核验候选
`Repair-CFReturn403-WorkRootSecurity-v2.ps1`（SHA256
`ac4bc59606e945999c31b0d85e77497ab3d26039733ccffd202eab77d9348d9c`）于
**2026-10-16 07:34:51 UTC** 执行成功：工作根 owner 从 Administrators 改为实际
Hermes Admin SID；原 DACL 二进制、保护位、control 和 group 完全不变。
祖先及证据目录固定并拒绝重解析点，操作无递归、无子文件修改、无服务或收发动作。
新私有回执 `security-repair-result-20261016-v2.json` 保留修复前后元数据。

修复后的只读审计没有创建生产台账，也没有打开 SQLite 或消费真实授权。
**目录访问故障已实际修复；生产 SQLite 首次创建及微信端到端尚未复测。**
ACK03 仍是 dead，不自动改变或重派。本轮不再发送微信测试；如需新的端到端验收，
必须另行明确授权新的测试编号和有限窗口。无需为了本任务保持 AI 主机待机。
