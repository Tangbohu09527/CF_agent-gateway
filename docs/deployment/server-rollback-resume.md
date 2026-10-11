# 旧 f9 服务器回退收尾与旧核心恢复

本说明从操作者最新提供的状态接续：Windows 已按原 start-intent 停回，旧 journal
回退完成，Hermes 停止且 8642/18642 无监听；CFserver 四应用停止，资产 journal 已为
`rolled_back`、三个原文件已恢复，但外层 `server.json` 仍为 `rolling_back`。
这些是操作者提供的观察，不是本轮助手连接现场取得的结果。以下命令交给操作者执行。

旧入口在恢复文件后将 Compose 的 image 字符串直接与 Image ID 比较，因而拒绝原来的
`localhost/cf-agent-gateway:inbound-0ec54bf-build-few9m9fp` 标签。该标签的本地镜像及
四个停止容器最后观察到的 ID 均为
`sha256:1c78a61f8728bf629ec3cd7ea1037e6f604d7d6e1e4e06e46ade8d8a7e53f163`。
新入口按本地 Image ID 核对；它不 pull、不改标签、不改已恢复的 Compose 文件。

**当前不要再执行 Windows quiesce/rollback 或旧服务器 rollback。**也不要使用仍指向
旧脚本的 `cfe`/`old_server` 等 shell 函数。服务器收尾和之后的 `start-old` 均显式调用
新固定 checkout 中的脚本；Windows 的 `start-legacy` 继续使用原 f9 入口。

## 两种版本绑定各自保留

| 项目 | 本次使用值 |
| --- | --- |
| 原部署 release | `f9c601cba25cbc5459bb03519d1954ae55995ded` |
| 原 candidate image | `sha256:fcfc56e702d9c23632497b507c4cb267fd44f6610a1962601523c4a53ca69928` |
| 原 baseline image | `sha256:1c78a61f8728bf629ec3cd7ea1037e6f604d7d6e1e4e06e46ade8d8a7e53f163` |
| 原 server plan | `57587c2979c5bcbe8f7721c149a4b356ddb8da75acbdc4c7717e33af5be8337d` |
| 原 Windows plan | `2241259cb9a853d909788c01f6351faee4b091759df532357a3267342dd9567a` |
| 原 deployment | `cf-return-site-readonly-20261007-r2` |
| 新 controller commit | 本次最终交付的完整修复 SHA；单独通过 `--controller-commit` 指定 |

原 request、release_root、plan、candidate、CA 引用和备份全部保留。新 controller 的
checkout HEAD、干净状态和脚本字节另外校验；不能把 request 的旧 release 改成新 SHA。
原 candidate 的 revision/source 标签及包内资产 helper 仍与原 release 校验。
baseline 本来就是更早的镜像，不能要求它的 revision 等于 f9 或新 controller SHA。
本次只修复宿主恢复控制器，**不需构建服务器新镜像**；之后部署新附件功能仍须另建新计划
并按对应修复 release 真正构建候选镜像。

## 1. 在 Windows 准备新控制器公共材料

在 PowerShell 5.1 中执行。Git/跨机传输分别固定使用下面一个可执行路径；服务器命令
在已存在的批准管理会话执行。`$ControllerCommit` 必须填写最终交付 SHA。

```powershell
$ErrorActionPreference = 'Stop'
$Git = 'C:\Users\Admin\AppData\Local\hermes\git\cmd\git.exe'
$Scp = 'C:\Windows\System32\OpenSSH\scp.exe'
$Fix = 'C:\Users\Admin\.codex\worktrees\59ae\CF_agent-gateway'
$ControllerCommit = '<最终交付的40位修复SHA>'
if ($ControllerCommit -notmatch '^[0-9a-f]{40}$') { throw '先填写最终修复 SHA' }
if ((& $Git -C $Fix rev-parse HEAD).Trim() -ne $ControllerCommit) { throw '新控制器 HEAD 不符' }
if ($LASTEXITCODE -ne 0) { throw 'Git 核对失败' }
if (& $Git -C $Fix status --porcelain --untracked-files=normal) { throw '新控制器 checkout 不干净' }
$Resume = Join-Path $env:LOCALAPPDATA ('cf-return-baseline-resume-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $Resume) { throw '证据目录已存在；不要覆盖' }
New-Item -ItemType Directory -Path $Resume | Out-Null
$BundleName = 'server-baseline-resume-' + $ControllerCommit + '.bundle'
$Bundle = Join-Path $Resume $BundleName
& $Git -C $Fix bundle create $Bundle HEAD
if ($LASTEXITCODE -ne 0) { throw 'Git bundle 创建失败' }
& $Git -C $Fix bundle verify $Bundle
if ($LASTEXITCODE -ne 0) { throw 'Git bundle 验证失败' }
Get-FileHash -LiteralPath $Bundle -Algorithm SHA256
& $Scp $Bundle ('linxi@CFserver:/home/linxi/cf-return-f9c601cb-upload/' + $BundleName)
if ($LASTEXITCODE -ne 0) { throw '公共源码包上传失败' }
```

本段仅传输 Git 已提交源码，不传 `.env`、私钥、Hermes 安装或私有 journal。已有同名上传
文件时先保留旧副本，再选择新文件名；不要覆盖未知材料。

## 2. 新服务器入口完成现有回退

以下全段在同一个已有服务器管理 shell 执行。填写与 Windows 一致的 controller SHA 和
刚得到的 bundle 原字节 SHA-256。旧 checkout 只读保留。

```sh
set -eu
umask 077
export PYTHONDONTWRITEBYTECODE=1
export GIT_OPTIONAL_LOCKS=0
GIT=/usr/bin/git
CONTROLLER_COMMIT='<最终交付的40位修复SHA>'
BUNDLE_SHA256='<Windows输出的64位bundle原字节SHA256>'
printf '%s\n' "$CONTROLLER_COMMIT" | grep -Eq '^[0-9a-f]{40}$'
printf '%s\n' "$BUNDLE_SHA256" | grep -Eq '^[0-9a-fA-F]{64}$'
NEW_RELEASE=/root/cf-return-baseline-resume-release
BUNDLE="/home/linxi/cf-return-f9c601cb-upload/server-baseline-resume-$CONTROLLER_COMMIT.bundle"
test -f "$BUNDLE" && test ! -L "$BUNDLE"
printf '%s  %s\n' "$BUNDLE_SHA256" "$BUNDLE" | sha256sum --check --strict
chmod 0600 "$BUNDLE"
test ! -e "$NEW_RELEASE"
"$GIT" clone --no-checkout "$BUNDLE" "$NEW_RELEASE"
"$GIT" -C "$NEW_RELEASE" bundle verify "$BUNDLE"
"$GIT" -C "$NEW_RELEASE" checkout --detach "$CONTROLLER_COMMIT"
test "$("$GIT" -C "$NEW_RELEASE" rev-parse HEAD)" = "$CONTROLLER_COMMIT"
test -z "$("$GIT" -C "$NEW_RELEASE" status --porcelain --untracked-files=normal)"

OLD_REQUEST=/root/cf-return-f9c601cb-prep/state/server-request-f9c601cb.json
OLD_COMMIT=f9c601cba25cbc5459bb03519d1954ae55995ded
OLD_IMAGE=sha256:fcfc56e702d9c23632497b507c4cb267fd44f6610a1962601523c4a53ca69928
OLD_PLAN=57587c2979c5bcbe8f7721c149a4b356ddb8da75acbdc4c7717e33af5be8337d
OLD_STATE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state_directory"])' "$OLD_REQUEST")
OLD_PEER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["peer_state_file"])' "$OLD_REQUEST")
RECOVERY="/root/cf-return-baseline-resume-evidence-$(date -u +%Y%m%dT%H%M%SZ)"
test ! -e "$RECOVERY"
install -d -m 0700 "$RECOVERY"
sha256sum "$OLD_REQUEST" > "$RECOVERY/request-before.sha256"
cp --no-clobber "$OLD_STATE/server.json" "$RECOVERY/server-before.json"
cp --no-clobber "$OLD_STATE/peer-receipt.json" "$RECOVERY/server-peer-before.json"
cp --no-clobber "$OLD_PEER" "$RECOVERY/windows-peer-before.json"

resume_server() {
  python3 "$NEW_RELEASE/deploy/prepare-return-server.py" "$1" \
    --request "$OLD_REQUEST" --release-commit "$OLD_COMMIT" --candidate-image "$OLD_IMAGE" \
    --controller-commit "$CONTROLLER_COMMIT" --expected-plan-sha256 "$OLD_PLAN" \
    --apply-authorized
}
COMPLETE_OUTPUT=$(mktemp "$RECOVERY/complete-rollback-output.XXXXXX.json")
resume_server complete-rollback > "$COMPLETE_OUTPUT" 2> "$COMPLETE_OUTPUT.stderr"
sha256sum --check --strict "$RECOVERY/request-before.sha256"
cp --no-clobber "$OLD_STATE/peer-receipt.json" "$RECOVERY/server-rolled-back-f9c601cb.json"
python3 - "$RECOVERY/server-rolled-back-f9c601cb.json" "$OLD_COMMIT" "$OLD_PLAN" <<'PY'
import json, sys
receipt = json.load(open(sys.argv[1]))
assert receipt['role'] == 'server' and receipt['state'] == 'rolled_back'
assert receipt['release_commit'] == sys.argv[2] and receipt['plan_sha256'] == sys.argv[3]
assert receipt['deployment_id'] == 'cf-return-site-readonly-20261007-r2'
assert receipt['activation_allowed'] is False
print('server rollback completion receipt verified')
PY
sha256sum "$RECOVERY/server-rolled-back-f9c601cb.json"
PUBLIC_OUT="/home/linxi/cf-return-f9c601cb-upload/server-rolled-back-resume-$CONTROLLER_COMMIT.json"
test ! -e "$PUBLIC_OUT"
install -m 0644 "$RECOVERY/server-rolled-back-f9c601cb.json" "$PUBLIC_OUT"
```

`complete-rollback` 只核对既有回退结果并完成外层状态/回执：四应用仍必须停止，资产
journal 必须已经 `rolled_back`，原 plan/request 绑定、三个原文件及备份摘要必须匹配，
本地 Compose 镜像必须解析为原 baseline ID，Nginx 语法校验必须通过。它不重新写资产、
不启停四应用、不启动 Hermes。任一步失败就保留状态和本次独立输出文件，不跳过门禁。
正式阶段文件由管理入口正常更新；更新前已保存旧回执和 server 状态原字节。

新 checkout 或证据目录已存在时不要删除重建。先核对它们记录的 controller SHA 和原
request/plan；同一恢复操作可复用已验证 checkout，但每次命令输出使用新的 `mktemp`
文件。不要把上述初始化和上传覆盖步骤当作重试命令。

## 3. Windows 仅恢复旧 HTTP

继续使用第 1 节 PowerShell 会话和 `$Resume`。先下载第 2 节实际产生的服务器
`rolled_back` 回执，填写服务器刚输出的 SHA-256，校验原字节后才能启动。

```powershell
$ExpectedServerSha = '<服务器输出的64位rolled_back回执SHA256>'
if ($ExpectedServerSha -notmatch '^[0-9a-fA-F]{64}$') { throw '先填写实际服务器回执摘要' }
$ServerRolledBack = Join-Path $Resume 'server-rolled-back-resume.json'
if (Test-Path -LiteralPath $ServerRolledBack) { throw '下载目标已存在；保留旧副本' }
& $Scp ('linxi@CFserver:/home/linxi/cf-return-f9c601cb-upload/server-rolled-back-resume-' + $ControllerCommit + '.json') $ServerRolledBack
if ($LASTEXITCODE -ne 0) { throw '服务器回执下载失败' }
if ((Get-FileHash -LiteralPath $ServerRolledBack -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedServerSha.ToLowerInvariant()) {
    throw '服务器回执原字节摘要不匹配'
}
$Old = 'C:\Users\Admin\.codex\worktrees\2d15\CF_agent-gateway'
$OldCommit = 'f9c601cba25cbc5459bb03519d1954ae55995ded'
$OldPlan = '2241259cb9a853d909788c01f6351faee4b091759df532357a3267342dd9567a'
$OldRequest = Join-Path $Old '.task-artifacts\return-csr-20261007\request-tls-f9c601cb.json'
$OldEntry = Join-Path $Old 'deploy\prepare-return-windows.ps1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:GIT_OPTIONAL_LOCKS = '0'
if ((& $Git -C $Old rev-parse HEAD).Trim() -ne $OldCommit) { throw '旧 Windows 入口 HEAD 改变' }
if ($LASTEXITCODE -ne 0) { throw '旧入口 Git 核对失败' }
if (& $Git -C $Old status --porcelain --untracked-files=no) { throw '旧 Windows 受管源码改变' }
$RequestData = Get-Content -LiteralPath $OldRequest -Raw | ConvertFrom-Json
$OldState = Join-Path $RequestData.state_directory $RequestData.deployment_id
$Reviewed = Get-Content -LiteralPath (Join-Path $OldState 'reviewed-plan.json') -Raw | ConvertFrom-Json
$Rolled = Get-Content -LiteralPath (Join-Path $OldState 'rolled-back.json') -Raw | ConvertFrom-Json
if ($Reviewed.plan_sha256 -ne $OldPlan -or $Rolled.plan_sha256 -ne $OldPlan -or $Rolled.state -ne 'rolled_back') {
    throw '原 Windows 回退记录不匹配'
}
$Existing = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $Reviewed.executor -and $_.CommandLine -match 'hermes_cli[.]main.*gateway.*run'
})
$Listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(8642,18642) })
if ($Existing.Count -ne 0 -or $Listeners.Count -ne 0) {
    throw '当前状态已不再是 Hermes 停止且无监听；保留现场，先核对原 start-legacy intent'
}
Set-Location -LiteralPath $Resume
& $OldEntry -Phase start-legacy -Request $OldRequest -AuthorizeMaintenance `
    -ExpectedPlanSha256 $OldPlan -PeerReceipt $ServerRolledBack
if ($LASTEXITCODE -ne 0) { throw '旧 HTTP 启动未验收；不要启动服务器核心或删除 intent' }
$LegacyCopy = Join-Path $Resume 'windows-legacy-restored-resume.json'
if (Test-Path -LiteralPath $LegacyCopy) { throw '公共回执副本已存在；保留旧副本' }
Copy-Item -LiteralPath (Join-Path $OldState 'legacy-restored.json') -Destination $LegacyCopy
$Legacy = Get-Content -LiteralPath $LegacyCopy -Raw | ConvertFrom-Json
if ($Legacy.state -ne 'rolled_back' -or $Legacy.legacy_process_started -ne $true -or $Legacy.plan_sha256 -ne $OldPlan) {
    throw '缺少实际旧 HTTP 已启动的原协议回执'
}
Get-FileHash -LiteralPath $LegacyCopy -Algorithm SHA256
& $Scp $LegacyCopy ('linxi@CFserver:/home/linxi/cf-return-f9c601cb-upload/windows-legacy-restored-resume-' + $ControllerCommit + '.json')
if ($LASTEXITCODE -ne 0) { throw 'Windows 旧 HTTP 回执上传失败' }
```

旧入口核对原 release、安装、恢复配置和对端 plan，按持久化 `start-legacy-intent.json`
调用官方管理入口，观察 PID/创建时间/执行器及原认证只读 `/v1/models`。不执行模型。
`2d15` 工作树及其 `.task-artifacts` 不写入；正式运行阶段记录仍写在 Hermes 原 state 目录。
旧 HTTP 恢复后的 LAN 监听属于回退基线，不是新 TLS 功能验收。

如果启动命令失败但进程已出现，不重跑本节的初始无进程判断来假装首次操作。保留
`start-legacy-intent.json`，先按其中 PID/创建时间/执行器只读核对；确认同一身份后，
可以只重复相同 `start-legacy` 入口命令观察结果。已有 intent 时入口不会再次提交 start。
身份变化或端口被另一进程占用必须停止接续，不能删 intent、手动杀进程或用历史 PID。

## 4. 新兼容入口恢复旧 Gateway/Dispatch 核心

继续使用第 2 节原服务器管理 shell。上传副本先核对原字节摘要，再仅收紧这个文件到
0600。输入真实 Windows `legacy-restored` 回执，不能把普通 `rolled_back` 回执补上字段。

```sh
UPLOAD="/home/linxi/cf-return-f9c601cb-upload/windows-legacy-restored-resume-$CONTROLLER_COMMIT.json"
WINDOWS_SHA256='<Windows输出的64位legacy-restored回执SHA256>'
printf '%s\n' "$WINDOWS_SHA256" | grep -Eq '^[0-9a-fA-F]{64}$'
test -f "$UPLOAD" && test ! -L "$UPLOAD"
printf '%s  %s\n' "$WINDOWS_SHA256" "$UPLOAD" | sha256sum --check --strict
chmod 0600 "$UPLOAD"
test -f "$OLD_PEER" && test ! -L "$OLD_PEER"
cp --no-clobber "$OLD_PEER" "$RECOVERY/windows-peer-before-legacy.json"
install -m 0600 "$UPLOAD" "$OLD_PEER"
printf '%s  %s\n' "$WINDOWS_SHA256" "$OLD_PEER" | sha256sum --check --strict
START_OUTPUT=$(mktemp "$RECOVERY/start-old-output.XXXXXX.json")
resume_server start-old > "$START_OUTPUT" 2> "$START_OUTPUT.stderr"
sha256sum --check --strict "$RECOVERY/request-before.sha256"
cp --no-clobber "$OLD_STATE/peer-receipt.json" "$RECOVERY/server-old-started-receipt.json"
```

这里仍执行新脚本，不回到旧 `cfe`。`start-old` 保留原协议：先验证旧 Windows 已恢复
HTTP 的真实回执及原计划；检查保存的停机队列证明，按原 Compose 仅准备四应用，实际
容器 Image ID 必须仍是 baseline。然后启动 Gateway，读取实时 DB/schema/队列健康，
通过可自动派发工作门禁后才启动 Dispatch，恢复原 HTTPS 前端。
`worker` 和 `delivery-worker` 保持停止；不会打开 Gate，不清理 dead/uncertain，也不
为了通过门禁删除队列。这里不额外执行一次性 Compose 数据库容器。

`start-old` 失败时可能只启动了 Gateway，外层保留 `old_starting`；这不是四应用仍全停。
保存输出并先核对当前状态，不执行 apply、重新规划或原 rollback 来清掉失败。
若只是成功后的回执丢失，同一新兼容入口可在 `old_started` 状态只读验证并重发回执，
不会再次启动应用或重建前端。

完成本说明只代表旧基线恢复到 Gate 关闭的可规划状态。新的 `.env`/TLS 修复尚未应用，
正式附件功能仍未验收。后续新 release、新 deployment、新 image 和双端新计划从
[独立修复 release 和全新双端计划](hermes-env-loopback-recovery.md#3-独立修复-release-和全新双端计划)
接续；不要重新执行该旧文档前面的停回和回退步骤，也不要复用其中的旧服务器脚本函数。
