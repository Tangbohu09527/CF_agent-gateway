# Hermes `.env` 监听覆盖：从启动失败现场接续

**2026-10-11 最新停点：新计划通过、未切换、重启后待复核。** 先读
[现场摘要：用户终端回传证据](../validation/2026-10-11-gateway-site-status.md)。
旧 f9 双端回退、`complete-rollback`（`assets_rewritten=false`）、旧 Hermes 恢复和
服务器 `start-old` 均已成功；[回退收尾说明](server-rollback-resume.md) 及本页第 0–2 节
只保留已完成的历史过程，不再指示从头停回/回退。

第 3 节的传包、独立 checkout、构建与新双端 plan 也已完成。deployment 为
`cf-return-env-loopback-20261011-r1`，固定 release 为
`d9bae61a043f1be4303e5c23661ec51904d5e708`，候选为
`sha256:bcb3e42fef5ba5a91232ca04e49a959cbecab4b45eb3bec0a0b9b3aadf717185`；
新计划尚未 apply/start/verify。文档分支的新提交不替换这些部署绑定。

用户随后只对 CFserver 执行 `sudo reboot`；重启完成情况及之后的容器、Gate、微信
登录状态待核验，Windows 是否关机未确认。先核验当前现场和现存材料，满足条件及维护
授权后再从第 4 节接续，不能用重启前 PID 或零队列快照证明当前状态，也不要为恢复
会话变量重跑第 0–3 节的初始化、传包、克隆、构建或回退。

以下保留原故障起点 `f9c601cba25cbc5459bb03519d1954ae55995ded` / PR #14，
deployment `cf-return-site-readonly-20261007-r2`。本次文档任务**没有执行现场操作**。
操作者当时提供的 2026-10-11 快照为：两端已 applied，服务器四应用最后确认停止，
Windows 已启动但报 `native_http_not_loopback`，8642 绑定 LAN、18642 未监听。
不能把它称为全停、未启动或 TLS ready。执行前必须刷新观察。

## 为什么需要新计划

官方固定 `0f4a98f87c17007b81500239d0bd5b9574027b73` 先读 YAML，随后环境覆盖
将 `API_SERVER_HOST` 写入 `extra.host`。旧计划只把 YAML 改为 loopback，
Hermes 自己的 `.env` 仍可把它改回 LAN。插件的 loopback 门禁正确拒绝该 adapter，
不能删除门禁或把明文 LAN 监听算成成功。18642 缺失不能单凭这个现象诊断为证书坏了。

隔离环境通过同一官方 Windows CLI 又发现后续兼容问题：CLI 全局注入的 `truststore`
把插件的服务端 SSL context 也替换了，Windows 握手误检查不存在的客户端证书，报
`Peer sent no certificates to verify`。修复使用局部原生 SSL context，保留 TLS 1.2
下限、客户端 CA/主机名校验和 HTTP 鉴权，不改官方核心或进程全局 TLS 设置。
这是在合成环境复现的第二问题，不能据此声称真实现场已经走到该握手阶段。

新计划精确管理 `.env` 的 `API_SERVER_HOST`；其余字段、注释、编码与换行按原字节保留，
端口保持原值。原字节只在本机私有 journal 备份。外层 Process/User/Machine 环境冲突、
重复或无法确定含义的监听配置拒绝；不设置全局环境变量，不修改官方核心。

现有双端协议绑定相同 release、deployment 和各自 plan。**修复提交不能直接拿旧回执启动**，
也不能把旧 request 中的 SHA 替换后覆盖原件。原恢复路线如下；第 1–3 项和第 4 项中的
候选构建、双端规划已经完成，仅新受控应用与验收尚未开始：

1. 新鲜核对服务器确实停止，导出旧计划的 quiesced 回执。
2. 原 `f9c601cb` Windows 入口按旧 start-intent 身份停回，双方原 journal 回退。
3. 原入口恢复旧 HTTP 和旧服务器核心，Gate 继续关闭；这只是恢复可规划基线。
4. 独立修复 release、新 deployment、新镜像和两端新计划，重新受控应用并验收。

原修复同时更新部署配置、验收入口及插件 TLS context，因此必须通过新计划安装新 bundle，
不得直接覆盖原 12 项已应用文件。已从固定修复 release
**真实构建新候选镜像并完成规划**，因为现有协议要求两端同一 release，服务器验证镜像 revision
和 release 绑定。不能给旧 `sha256:fcfc56...` 镜像冒填新 SHA。本次没有服务器运行时
功能变化、数据库迁移或新业务权限；不重签现有有效证书，复用已有私有签名文件。

## 0. 固定入口和材料目录（Windows PowerShell 5.1）

本节为已完成的历史初始化，不是重启后的接管入口。

全部命令在已批准维护窗口由操作者执行。Git 和跨机传输各只选以下一个可执行路径；
Linux 命令在原有服务器管理会话执行，不另建 SSH 通道。固定修复 SHA 为 d9，不随文档 HEAD 改变。
新公共输出写到新目录，旧 `2d15` 工作树及其 `.task-artifacts` 只读保留。

```powershell
$ErrorActionPreference = 'Stop'
$Git = 'C:\Users\Admin\AppData\Local\hermes\git\cmd\git.exe'
$Scp = 'C:\Windows\System32\OpenSSH\scp.exe'
$Old = 'C:\Users\Admin\.codex\worktrees\2d15\CF_agent-gateway'
$Fix = 'C:\Users\Admin\.codex\worktrees\59ae\CF_agent-gateway'
$OldCommit = 'f9c601cba25cbc5459bb03519d1954ae55995ded'
$OldPlan = '2241259cb9a853d909788c01f6351faee4b091759df532357a3267342dd9567a'
$OldRequest = Join-Path $Old '.task-artifacts\return-csr-20261007\request-tls-f9c601cb.json'
$Recovery = Join-Path $env:LOCALAPPDATA 'cf-return-loopback-recovery-20261011'
if (Test-Path -LiteralPath $Recovery) { throw '请选择一个新的恢复材料目录；不要覆盖已有证据' }
New-Item -ItemType Directory -Path $Recovery | Out-Null
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:GIT_OPTIONAL_LOCKS = '0'
if ((& $Git -C $Old rev-parse HEAD).Trim() -ne $OldCommit) { throw '旧恢复入口版本改变' }
if (& $Git -C $Old status --porcelain --untracked-files=no) { throw '旧入口有受管文件修改' }
$FixCommit = 'd9bae61a043f1be4303e5c23661ec51904d5e708'
if ((& $Git -C $Fix rev-parse HEAD).Trim() -ne $FixCommit) { throw '修复入口与固定 d9 不符' }
if ($LASTEXITCODE -ne 0) { throw '修复入口 Git 核对失败' }
if (& $Git -C $Fix status --porcelain --untracked-files=no) { throw '修复入口不干净' }
$RequestData = Get-Content -LiteralPath $OldRequest -Raw | ConvertFrom-Json
$HermesHome = $RequestData.hermes_home
$OldState = Join-Path $RequestData.state_directory $RequestData.deployment_id
$OldEntry = Join-Path $Old 'deploy\prepare-return-windows.ps1'
$FixEntry = Join-Path $Fix 'deploy\prepare-return-windows.ps1'
function Save-PublicJson($Path, $Lines) {
    $Text = $Lines -join "`n"
    $null = $Text | ConvertFrom-Json
    if (Test-Path -LiteralPath $Path) { throw '公共输出文件已存在' }
    $null = [IO.File]::WriteAllText($Path, $Text + "`n", (New-Object Text.UTF8Encoding($false)))
}
```

不要输出 `$RequestData` 全文或读取 `.env` 正文到终端。确认正式 applied 与交接摘要一致：

```powershell
$Applied = Join-Path $OldState 'applied.json'
$ExpectedApplied = 'bb85cadfd9d1cbc22dceed209d7feab3ff16d86829f1d2aceba45e4bddd6897c'
if ((Get-FileHash -LiteralPath $Applied -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedApplied) {
    throw 'Windows applied 回执变化，停止接续并核对'
}
$Intent = Get-Content -LiteralPath (Join-Path $OldState 'start-intent.json') -Raw | ConvertFrom-Json
if ($Intent.plan_sha256 -ne $OldPlan -or $Intent.action -ne 'start' -or -not $Intent.process) {
    throw '缺少绑定的原启动身份；禁止猜 PID 或重复 start'
}
$Observed = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$Intent.process.pid)
if ($Observed) {
    if ($Observed.ExecutablePath -ne $Intent.process.executable -or
        $Observed.CreationDate.ToUniversalTime().ToString('o') -ne $Intent.process.created) {
        throw 'PID/创建时间/执行器不匹配；禁止停止替代进程'
    }
    [pscustomobject]@{pid=$Observed.ProcessId; created=$Intent.process.created; executable=$Observed.ExecutablePath}
}
$Listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(8642,18642) })
if ($Listeners | Where-Object { -not $Observed -or $_.OwningProcess -ne $Observed.ProcessId }) {
    throw '受管端口被非绑定进程占用；禁止按旧现场继续停止或回退'
}
$Listeners | Select-Object LocalAddress,LocalPort,OwningProcess
```

历史 PID 25184 不是操作参数。原 f9 入口只核对受管进程身份，缺少进程消失后的端口检查；
上面的操作者前置检查及下面 quiesce 后的检查补足该边界。绑定进程已退出而端口仍被
占用时停止接续；不要调用 `Stop-Process`。原日志/start-intent/tls-ready 缺失状态全部保留。

## 1. 服务器刷新旧 quiesced（原管理会话）

本节为已完成的旧部署历史步骤；不用于证明重启后的当前队列或容器状态。

以下只读部分先核对原 request、当前容器和当前数据库计数。`quiesce` 在四应用全停时
只检查容器并复用原停机前数据库记录，因此**它单独不能证明当前队列**。先做下面的新鲜
数据库观察；不会启动四应用、模型或微信。一次性只读容器使用当前 Compose 已配置的
镜像和配置，不构建、不 pull、不启动依赖，PostgreSQL 会话强制只读。

```sh
set -eu
umask 077
OLD_REQUEST=/root/cf-return-f9c601cb-prep/state/server-request-f9c601cb.json
OLD_COMMIT=f9c601cba25cbc5459bb03519d1954ae55995ded
OLD_IMAGE=sha256:fcfc56e702d9c23632497b507c4cb267fd44f6610a1962601523c4a53ca69928
OLD_RELEASE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_root"])' "$OLD_REQUEST")
OLD_STATE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_directory"])' "$OLD_REQUEST")
RECOVERY=/root/cf-return-loopback-recovery-20261011
test ! -e "$RECOVERY"
install -d -m 0700 "$RECOVERY"
old_server() {
  python3 "$OLD_RELEASE/deploy/prepare-return-server.py" "$1" --request "$OLD_REQUEST" \
    --release-commit "$OLD_COMMIT" --candidate-image "$OLD_IMAGE" --apply-authorized
}
cp --no-clobber "$OLD_STATE/peer-receipt.json" "$RECOVERY/server-applied-before-recovery.json"
python3 - "$OLD_RELEASE/deploy/prepare-return-server.py" "$OLD_REQUEST" "$OLD_COMMIT" "$OLD_IMAGE" \
  > "$RECOVERY/current-containers.json" <<'PY'
import json, runpy, sys
entry_type = runpy.run_path(sys.argv[1])["ServerEntry"]
entry = entry_type(sys.argv[2], release_commit=sys.argv[3], candidate_image=sys.argv[4])
print(json.dumps(entry.stopped()))
PY
docker --context default compose --project-directory /opt/cf-agent-gateway \
  --env-file /opt/cf-agent-gateway/.env --profile worker \
  -f /opt/cf-agent-gateway/docker-compose.prod.yml run --rm --no-deps -T \
  -e 'PGOPTIONS=-c default_transaction_read_only=on' --entrypoint python gateway -c '
import json
from cf_agent_gateway.config import load_settings
from cf_agent_gateway.database import create_database_engine
from cf_agent_gateway.runtime.health import RuntimeHealthService
s=load_settings("/app/config/production.yaml")
e=create_database_engine(s.database.url)
try:
    r=RuntimeHealthService(e,s).snapshot()
    assert all(r["components"][k]["status"] == "ok" for k in ("database","migration_schema"))
    fields = {
        "dispatch": ("queued", "running", "failed", "uncertain", "stale_running",
                     "blocked_threads", "missing_delivery", "reconciliation_backlog",
                     "reconciliation_deferred", "reconciliation_poison"),
        "delivery": ("queued", "delivering", "failed", "uncertain",
                     "stale_delivering", "missing_delivery"),
    }
    assert all(type(r[group].get(k)) is int and r[group][k] == 0
               for group, keys in fields.items() for k in keys)
    print(json.dumps({k:r[k] for k in ("checked_at","dispatch","delivery")}))
finally:
    e.dispose()
' < /dev/null > "$RECOVERY/current-queues.json"
old_server quiesce > "$RECOVERY/old-server-quiesce-output.json"
cp --no-clobber "$OLD_STATE/peer-receipt.json" "$RECOVERY/server-quiesced-f9c601cb.json"
sha256sum "$RECOVERY/server-quiesced-f9c601cb.json"
```

任一步失败就停止；保留 `dead=2` 或实际观察到的 dead，不清理、不重派、不手改队列。
`delivered` 是历史成功数、`oldest_*_age_seconds` 是时长或 null，均不当作待办数比较。
原 `peer-receipt.json` 是管理入口的当前阶段文件，入口会更新它；更新前已在新恢复目录
保留其 applied 原字节。旧上传 applied 副本、plan/request/journal 和 Windows applied 不改写。
确认输出 state=quiesced、server plan 为
`57587c2979c5bcbe8f7721c149a4b356ddb8da75acbdc4c7717e33af5be8337d`。
服务器入口实时验证四应用均停止；若实际有运行应用，不能把上述快照当作全停，先重新
核对维护范围和在途，再按原 quiesce 的排空流程处置。不能拿旧 applied 回执改 state。

公共材料由服务器操作者把上述文件原字节复制到已批准上传目录，设置可下载权限；
Windows 使用选定的 `$Scp` 下载并核对服务器实际输出的 SHA-256。不要复制私有 journal。
示例传输路径为 `/home/linxi/cf-return-f9c601cb-upload/server-quiesced-f9c601cb.json`：

```powershell
$ServerQuiesced = Join-Path $Recovery 'server-quiesced-f9c601cb.json'
& $Scp 'linxi@CFserver:/home/linxi/cf-return-f9c601cb-upload/server-quiesced-f9c601cb.json' $ServerQuiesced
if ($LASTEXITCODE -ne 0) { throw '公共回执下载失败' }
Get-FileHash -LiteralPath $ServerQuiesced -Algorithm SHA256
# 与服务器刚输出的摘要逐字核对后才能继续。
```

## 2. 原 Windows 停回；双方原 journal 回退

本节旧部署停回、回退及旧核心恢复均已完成，完整收尾历史见
[服务器回退收尾说明](server-rollback-resume.md)，不要重放下面命令。

`quiesce` 使用旧 reviewed-plan、原 request、12 份已应用资产以及 start-intent 的身份。
不是初始 `stop`；不重复 start，不删除 intent，不预先改变 `.env` 或 bundle。

```powershell
& $OldEntry -Phase quiesce -Request $OldRequest -AuthorizeMaintenance `
    -ExpectedPlanSha256 $OldPlan -PeerReceipt $ServerQuiesced
if ($LASTEXITCODE -ne 0) { throw '原启动停回未确认；停止后续步骤' }
$RemainingListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(8642,18642) })
if ($RemainingListeners.Count -ne 0) {
    throw '旧入口停回后受管端口仍有监听；保留现场，禁止 rollback 或 start-legacy'
}
Copy-Item -LiteralPath (Join-Path $OldState 'requiesced.json') `
    -Destination (Join-Path $Recovery 'windows-quiesced-f9c601cb.json')
& $OldEntry -Phase rollback -Request $OldRequest -AuthorizeMaintenance `
    -ExpectedPlanSha256 $OldPlan -PeerReceipt $ServerQuiesced
if ($LASTEXITCODE -ne 0) { throw 'Windows 回退未完成；保留 journal' }
Copy-Item -LiteralPath (Join-Path $OldState 'rolled-back.json') `
    -Destination (Join-Path $Recovery 'windows-rolled-back-f9c601cb.json')
```

服务器原会话：

```sh
old_server rollback > "$RECOVERY/old-server-rollback-output.json"
cp --no-clobber "$OLD_STATE/peer-receipt.json" "$RECOVERY/server-rolled-back-f9c601cb.json"
sha256sum "$RECOVERY/server-rolled-back-f9c601cb.json"
```

将服务器 rolled_back 原字节传回并核对摘要，Windows 执行：

```powershell
$ServerRolledBack = Join-Path $Recovery 'server-rolled-back-f9c601cb.json'
& $Scp 'linxi@CFserver:/home/linxi/cf-return-f9c601cb-upload/server-rolled-back-f9c601cb.json' $ServerRolledBack
if ($LASTEXITCODE -ne 0) { throw '公共回退回执下载失败' }
Get-FileHash -LiteralPath $ServerRolledBack -Algorithm SHA256
# 与服务器刚输出的摘要逐字核对后继续。
& $OldEntry -Phase start-legacy -Request $OldRequest -AuthorizeMaintenance `
    -ExpectedPlanSha256 $OldPlan -PeerReceipt $ServerRolledBack
if ($LASTEXITCODE -ne 0) { throw '旧 HTTP 恢复未确认；禁止启动服务器核心' }
Copy-Item -LiteralPath (Join-Path $OldState 'legacy-restored.json') `
    -Destination (Join-Path $Recovery 'windows-legacy-restored-f9c601cb.json')
Get-FileHash -LiteralPath (Join-Path $Recovery 'windows-legacy-restored-f9c601cb.json') -Algorithm SHA256
& $Scp (Join-Path $Recovery 'windows-legacy-restored-f9c601cb.json') `
    'linxi@CFserver:/home/linxi/cf-return-f9c601cb-upload/windows-legacy-restored-f9c601cb.json'
if ($LASTEXITCODE -ne 0) { throw '公共回执上传失败' }
```

Linux 对每份上传副本都先核对原字节摘要，再仅收紧该文件为 0600，复制进原 request
指定 peer 路径；原 peer 回执先另存到新恢复目录。`EXPECTED` 必须填写 Windows 实际
输出值，不是 JSON 重序列化的摘要。

```sh
UPLOAD=/home/linxi/cf-return-f9c601cb-upload/windows-legacy-restored-f9c601cb.json
EXPECTED='<Windows 实际输出的 64 位 SHA-256>'
test -f "$UPLOAD" && test ! -L "$UPLOAD"
printf '%s  %s\n' "$EXPECTED" "$UPLOAD" | sha256sum --check --strict
chmod 0600 "$UPLOAD"
PEER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["peer_state_file"])' "$OLD_REQUEST")
cp --no-clobber "$PEER" "$RECOVERY/peer-before-legacy.json"
install -m 0600 "$UPLOAD" "$PEER"
old_server start-old > "$RECOVERY/old-server-start-output.json"
```

原 `start-old` 只启动 Gateway/Dispatch 核心并恢复旧 HTTPS 前端，worker/delivery-worker
保持停止。它在启动 Dispatch 前再次检查实时 DB/schema 和可派发/运行/失败/待对账计数；
本说明另要求先保存上述包含 uncertain 的新鲜只读观察。原 `dispatch_start_safe` 不把
所有历史异常都视为自动阻断，不可声称该入口会自行拒绝任何 uncertain；出现这类变化应
停止本流程并另行核对，不清理记录来满足条件。旧 HTTP 的 LAN 监听只属于回退状态，
**不是新功能验收**。
该历史阶段建立了 Gate 关闭的可规划基线；后续新规划已完成。当前须先核验重启后现场，
不能据此立即进入切换窗口或手工 Controller start。

## 3. 独立修复 release 和全新双端计划

本节为**已完成的新计划准备记录**，保留原流程供核对，不是重启后重复执行的清单。
用户终端回传：Windows 新 plan 共 13 项，含 `.env` 的 `API_SERVER_HOST` 单字段修改，
原 17 项工具集保留；服务器原 `env_file` 解析及环境等价检查通过。新旧迁移头均为
`20260928_01`，本次不需要迁移。后续先复用并核对现存候选、request、plan 和公共回执；
本节 shell 初始化中的排他创建检查不应通过删除或重建现有目录来绕过。

从这一步起不能再使用旧部署 ID/plan/intents。Windows 新请求仅复用已验证的本机引用，
不复制凭据或证书正文，不动旧 request。已保存的新 request 的 `$FixCommit` 固定为
`d9bae61a043f1be4303e5c23661ec51904d5e708`；新部署 ID 为 `cf-return-env-loopback-20261011-r1`。

```powershell
$NewDeployment = 'cf-return-env-loopback-20261011-r1'
$NewRequest = Join-Path $Recovery 'request-loopback-fix.json'
$New = Get-Content -LiteralPath $OldRequest -Raw | ConvertFrom-Json
$New.release_commit = $FixCommit
$New.deployment_id = $NewDeployment
if (Test-Path -LiteralPath (Join-Path $New.state_directory $NewDeployment)) {
    throw '新 deployment 已存在；核对该计划，不覆盖或删除目录'
}
Save-PublicJson $NewRequest @($New | ConvertTo-Json -Depth 12)
$PlanOutput = & $FixEntry -Phase plan -Request $NewRequest
if ($LASTEXITCODE -ne 0) { throw '新计划失败；不做服务变更' }
Save-PublicJson (Join-Path $Recovery 'windows-new-plan.json') $PlanOutput
$NewPlan = (($PlanOutput -join "`n") | ConvertFrom-Json).plan_sha256
$PublicOutput = & $FixEntry -Phase export-public -Request $NewRequest
if ($LASTEXITCODE -ne 0) { throw '新公共引用导出失败' }
Save-PublicJson (Join-Path $Recovery 'windows-new-prepared.json') $PublicOutput
Get-FileHash -LiteralPath (Join-Path $Recovery 'windows-new-prepared.json') -Algorithm SHA256
```

审核新 plan 的 `.env` 变更只有 HOST、旧端口保持 8642、模型/认证/原工具集保持，额外工具
仅 `cf_artifact_return`。如果 Process/User/Machine 冲突或文件歧义阻止规划，交操作者定位
实际继承来源；不 `setx`、不删变量来掩盖不明来源、不用临时 shell 值冒充持久修复。

服务器已使用新独立 checkout，未推进旧 `$OLD_RELEASE`。已按
[固定发布获取和实际构建](artifact-return-site.md#入口及固定发布获取) 完成构建，固定候选
为本页顶部的 `sha256:bcb3e42...`。基础镜像仍使用该文档已核验的固定 digest，
`SOURCE_COMMIT` 为固定 d9。复用有效证书和保留的签名文件；不运行安装器或重复构建。

服务器设定下列新变量后，沿用原入口的新 plan 流程。`RELEASE` 是新 checkout，
`IMAGE_ID` 由实际构建产生。先用前文同一 `$Scp` 上传 `windows-new-prepared.json`，
再按原字节摘要核对、单文件 0600 流程安装为 `$RECOVERY/windows-new-prepared.json`。
此公共文件作为新 request 的阶段 peer 路径；后续更换阶段前均另存现有原字节。
CA 路径从原 request 读取，不复制私钥。

```sh
COMMIT=d9bae61a043f1be4303e5c23661ec51904d5e708
RELEASE=/root/cf-return-env-loopback-20261011-r1-release
DEPLOYMENT=cf-return-env-loopback-20261011-r1
NEW_STATE=/root/cf-return-env-loopback-20261011-r1-state
SERVER_REQUEST="$NEW_STATE/request.json"
WINDOWS_PUBLIC_REFERENCES="$RECOVERY/windows-new-prepared.json"
HERMES_CA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hermes_ca_file"])' "$OLD_REQUEST")
GATEWAY_CA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway_ca_file"])' "$OLD_REQUEST")
GIT=/usr/bin/git
test -x "$GIT"
test ! -e "$RELEASE"
test ! -e "$NEW_STATE"
install -d -m 0700 "$NEW_STATE"
"$GIT" init "$RELEASE"
"$GIT" -C "$RELEASE" remote add origin https://github.com/Tangbohu09527/CF_agent-gateway.git
"$GIT" -C "$RELEASE" fetch --no-tags origin "$COMMIT"
"$GIT" -C "$RELEASE" checkout --detach FETCH_HEAD
test "$("$GIT" -C "$RELEASE" rev-parse HEAD)" = "$COMMIT"
test -z "$("$GIT" -C "$RELEASE" status --porcelain --untracked-files=normal)"
APPROVED_BASE_IMAGE_DIGEST=python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
test "$(docker --context default image inspect --format '{{.Id}}' "$APPROVED_BASE_IMAGE_DIGEST")" \
  = sha256:59bf1d95c965f12dfc14afaf5af778fc1dbe5b372bd5c281645fc34d4c75d4e7
test "$(docker --context default image inspect --format '{{.Os}}/{{.Architecture}}' "$APPROVED_BASE_IMAGE_DIGEST")" \
  = linux/amd64
docker --context default build --platform linux/amd64 --file "$RELEASE/docker/Dockerfile" \
  --build-arg BASE_IMAGE="$APPROVED_BASE_IMAGE_DIGEST" --build-arg SOURCE_COMMIT="$COMMIT" \
  --iidfile "$NEW_STATE/candidate-image.id" "$RELEASE"
IMAGE_ID=$(cat "$NEW_STATE/candidate-image.id")
test "$(docker --context default image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_ID")" \
  = "$COMMIT"
docker --context default image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE_ID"
python3 "$RELEASE/deploy/prepare-return-server.py" init-request \
  --request "$SERVER_REQUEST" --deployment-id "$DEPLOYMENT" --state-directory "$NEW_STATE" \
  --release-root "$RELEASE" --release-commit "$COMMIT" --candidate-image "$IMAGE_ID" \
  --peer-public "$WINDOWS_PUBLIC_REFERENCES" --hermes-ca-file "$HERMES_CA" --gateway-ca-file "$GATEWAY_CA"
new_server() {
  python3 "$RELEASE/deploy/prepare-return-server.py" "$1" --request "$SERVER_REQUEST" \
    --release-commit "$COMMIT" --candidate-image "$IMAGE_ID" --apply-authorized
}
new_server plan > "$RECOVERY/server-new-plan.json"
```

这里 `init-request` 自动发现实际已恢复的旧四应用镜像作为新回退基线。若与旧基线
`sha256:1c78a61f...` 不符就先核对，不替换摘要凑通过。新双方 plan 的 deployment/release、
HTTPS origins、CA 摘要与 Profile 必须一致。

## 4. 新计划按原交错顺序应用

**尚未执行；不能直接从历史 shell 会话继续。** 先按现场摘要核验 CFserver 重启完成、
当前容器/镜像/Gate/数据库与队列、微信登录以及 Windows/Hermes 实际状态。用户回传的
重启前基线是旧 Hermes、Gateway API、Dispatch 运行，Poll/Delivery 停止、Gate 关闭、
数据库正常，待处理/在途/uncertain 为 0、历史 dead=2 保留；这些不是重启后证明。

当前材料核验一致且进入批准维护窗口后，才采用下表已有交错顺序。重新打开的会话中，
从已保存的新 request、reviewed plan 和阶段回执核对并绑定 `$FixEntry`、`$NewRequest`、
`$NewPlan`、`RELEASE`、`SERVER_REQUEST`、`NEW_STATE` 和实际公共回执文件路径；固定
`COMMIT` 为 d9、`IMAGE_ID` 为本页顶部候选，重新定义 `new_server` 也只采用这些已核验
绑定。不以当前文档 HEAD 推导版本，不为获得这些变量重跑前面的初始化或构建块。

每步成功才进入下一步；每份跨机回执都采用前文原字节摘要验证、单文件 0600 入受管
peer 路径的流程。新服务器 `peer_state_file` 从新 request 读取，绝不覆盖旧请求或
把旧 `server-plan.json` 复制到新 deployment。

| 顺序 | 操作者命令 / 回执 |
| --- | --- |
| 1 | Linux `new_server quiesce`；保存/传递新 `$NEW_STATE/peer-receipt.json` |
| 2 | Windows `$FixEntry -Phase stop -Request $NewRequest -AuthorizeMaintenance -ExpectedPlanSha256 $NewPlan -PeerReceipt $NewServerQuiesced`；传递新 deployment 的 `quiesced.json` |
| 3 | Linux 验证并安装 Windows 新 quiesced 回执至新 peer 路径；`new_server apply`；传递新 server applied |
| 4 | Windows `$FixEntry -Phase apply -Request $NewRequest -AuthorizeMaintenance -ExpectedPlanSha256 $NewPlan -PeerReceipt $NewServerApplied` |
| 5 | Windows `$FixEntry -Phase start -Request $NewRequest -AuthorizeMaintenance -ExpectedPlanSha256 $NewPlan -PeerReceipt $NewServerApplied` |
| 6 | Windows 成功后传递新 deployment 的 `tls-ready.json`；Linux 验证并安装后 `new_server start`、`new_server verify` |

PowerShell 表中的调用前加 `&`，每次按前文检查 `$LASTEXITCODE`。输出文件变量必须
指向本次新计划实际收到的文件。最后仍不开 Gate。新 `tls_ready` 要求原生 8642 仅
loopback、新 18642 的监听归属同一个绑定进程，前后观察均一致，真实 CA/主机名和
原认证的只读 `/v1/models` 成功。插件拒绝、TLS 缺失或认证失败都不能产出成功回执。

这仍不等于正式附件功能验收。后续 ACK、模型调用、上传和微信实收需另行批准唯一
正常准入任务及业务范围；这里没有业务测试命令。

## 部分失败、回退与 PR 接续

- 旧 quiesce 身份不符：停在原现场，只读核对真实进程；不 kill、不重放 start。
- stop/start 管理调用超时或回执丢失：用完全相同 request/plan 重跑同一阶段，只观察
  原 intent；入口不会再发第二次管理命令。若原 intent 未绑定进程，保留并人工核对。
- 任一资产、CA、源码、环境或 journal 漂移：保留全部证据，拒绝覆盖；不要重新 plan
  一个部分应用的目录以规避原 before/after。
- 已有 apply journal 的中断：相同 request/审核 SHA 恢复该 journal；回退使用其
  `rollback`，精确恢复包括 `.env` 在内的原字节。未知后来变化或备份损坏拒绝回退。
- 新启动后失败：**新服务器 quiesce → 新 Windows quiesce → 双方新 rollback →
  新 Windows start-legacy → 新服务器 start-old**。新 plan 的回退回到本轮旧 HTTP
  基线，旧 f9 的日志、plan、request、journal 和 intents 始终保留。
- 任意恢复后都需当前队列/DB/schema 检查；不得清除 uncertain/dead、重派历史任务，
  也不能用保存的零队列快照代替实时查询。无 TLS ready 就不得新服务器 start。

原修复分支从 f9 普通追加提交，固定 d9 位于 `codex/fix-hermes-env-loopback`。
2026-10-11 远端核对 PR #14 为 Draft/Open、head=f9、base=main；不能将独立修复分支
或文档分支的 d9 后继写成已进入 main 或 PR #14 已合并。本次文档 PR 使用
`codex/fix-hermes-env-loopback` 为 base，只推新文档分支，不改变 PR #14。
下面只保留未来功能集成的说明，不是本次文档任务的操作：后续接回 PR 时先读取远端
`feat/wechat-inbound-media`：若仍为 f9，可在独立 checkout 验证修复是其后代后普通
fast-forward 推送该远端 PR 分支；若已有新提交，在独立集成分支 cherry-pick 修复并
重跑相关测试，再普通推送。**不要 checkout、reset、merge 或推进旧 2d15 的本地分支**。
远端 PR 更新不要求移动旧本地分支；其 HEAD 和旧恢复材料继续固定在 f9。
