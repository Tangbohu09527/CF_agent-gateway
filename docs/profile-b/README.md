# 独立 Profile B 管理候选（测试数据）

本候选在 PR21 产品基线上新增管理后端与同源中文 Web。官方 Hermes、数据库、Profile 和会话真实运行；模型是本地确定性替身，微信是管理台中的协议测试入口。没有连接生产或真实微信。

## 当前电脑直接启动

在 PowerShell 执行：

```powershell
cd D:\Tangbohu09527\.cf-worktrees\profile-b-management\CF_agent-gateway
& .\scripts\profile-b\start.ps1
```

打开 `http://127.0.0.1:18760/manage`。测试管理员凭据由监督器首次运行生成，登录时输入 `D:\Tangbohu09527\.cf-worktrees\profile-b-management\runtime\demo\demo-secrets.json` 中的 `admin` 值。可在本地终端读取：

```powershell
(Get-Content ..\runtime\demo\demo-secrets.json -Raw | ConvertFrom-Json).admin
```

此文件不进 Git；静态前端没有内置密钥，登录凭据只在当前页面内存中保存。刷新页面后重新登录，数据库状态仍在。不要把测试凭据用于生产。

已准备的数据库有员工甲乙、员工组、群 A/B、群组、两个模板和测试节点；四个真实派生 Profile 已存在。员工乙当前允许私聊、禁止群聊，用于展示两级权限。员工模板已更新到 v3，群模板 v1；原有员工会话仍显示待生效。失败矩阵使用另一个数据库，不混入管理页。

最短演示：登录 → 员工管理添加测试员工（来源账号 `isolated-bot`，内部编号/来源 ID 自行唯一填写）→ 勾选 Bot 权限 → 隔离消息验证，未分组请求拒绝 → 员工编辑分配员工测试组 → 派生 Profile 选择此员工并创建 → 隔离消息验证发送两条消息（私聊物理会话 ID 等于员工来源 ID）→ 核对实际 Profile、持久会话和模型收到的历史标记 → 修改员工模板 → 对派生档案同步 → 核对已应用版本和旧会话待生效状态。

在另一个 PowerShell 停止：

```powershell
& D:\Tangbohu09527\.cf-worktrees\profile-b-management\CF_agent-gateway\scripts\profile-b\stop.ps1
```

停止脚本仅写本轮运行目录的停止信号，由监督器关闭自己记录的子进程。`owned-processes.json`、`stopped.json` 可核对 PID 和结果。源码、数据库、官方依赖都保留；无需重新安装。不要删除 `..\runtime\hermes-home\installs`，它是已核验的隔离依赖。要另做空白演示，先停止默认演示，再用 `start.ps1 -Runtime <新的绝对目录>`，不删除当前数据；停止该演示时也须传 `stop.ps1 -Runtime <同一绝对目录>`。

默认端口：管理 Web/Gateway 18760、模型替身 18761、官方 Hermes serve 18762，全部绑定 127.0.0.1。它们都是本轮前台监督器的资源。默认 Gateway 主入口不启用新管理路由、不自动启用微信，也不迁移员工或会话。

## 管理范围与限制

- `/admin/profile-b/state` 及员工、模板、节点、组、群、派生和测试消息 API 均复用既有管理员认证。写请求有操作者指纹、请求 ID、对象、时间和结果审计；审计不保存密钥或完整私聊正文。
- 模板白名单只有 SOUL 和 `display.language`。连接 URL、认证、模型配置由服务器持有，页面只能选择已配置连接引用。首次初始化配置与后续受管字段同步分开。
- 官方普通会话缓存系统提示。已证实新会话采用新 SOUL，原会话保持原 SOUL和历史，且后端/节点重启后仍如此。首版没有已验证的普通旧会话无损即时刷新接口，不承诺下轮或固定时间自动生效。
- 改节点显示“跨节点迁移未验证”，保留数据；没有删除、拷贝记忆、清历史、强制迁移按钮。在途任务不自动打断或重放；异常未决操作保留记录，不因租约到期擅自接管。
- 新 B 管理链当前面向显式 `wechat-test` 隔离协议入口。既有真实 WeChat Poll/Delivery 未切换到它；生产接入和跨主机 HTTPS 部署未验收。
- Profile 不是文件权限/操作系统沙箱。此次官方本地 `read_file` 检查证明工具调用归属；未证明 FileBrowser 每调用可信任务关联，也未补齐 PR23 中 MCP `_meta`/Artifact/Delivery/微信返回缺口。群线程 workspace 仍属于首个获准发起人，后续任务另记实际发起身份；不得据此放宽正式文件权限。

## 固定版本与复现

产品基线：PR21 `91a235fca08362d8c1440e808df56946be3cc1e8`；PR23 只复用审计方法，没有合并 PR23/PR24 实验或安装器分支。需求：总文档 PR13 `bde24c32e77e6af9175a3fbe0721c98c93b67b05`。

官方 Hermes：`427d605d4985c2d49a795493ef6c5d66413057d1`，未修改源码，使用官方安装器 `python-deps` 阶段、PM 完整锁定 `[all]` 和 Python 3.14.7。Windows 使用 install.ps1；新增 CI 使用 install.sh。没有项目插件、EXE、通用远程 Shell 或额外节点客户端；节点运行官方 `hermes serve`。Gateway 经官方认证 REST 创建/配置 Profile，经官方 WebSocket JSON-RPC 创建/恢复/提交会话，核对 profile_name、持久会话 ID 和本次 user_row_id 回执。

同机独立进程的 Gateway 管理链已实测；不同电脑上的同一官方 HTTPS 管理接口尚未实测，不把手工 CLI 创建当跨机管理证据。

自动化入口：

```powershell
# 已准备的隔离 Gateway 环境
& .\.venv\Scripts\python.exe -m pytest -q tests\profile_b

# browser.cjs 要求空白隔离数据库；restart.cjs 使用已完成 native 检查的数据。
# 先停止默认演示，在另一终端运行以下命令，等待 ready 后再执行浏览器测试：
# & .\scripts\profile-b\start.ps1 -Runtime '<空白演示运行目录>'
# 此后停止也传同一目录：stop.ps1 -Runtime '<空白演示运行目录>'
$env:NODE_PATH='C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
$env:CF_BROWSER_CHANNEL='msedge'
$env:CF_PROFILE_B_RUNTIME='<空白演示运行目录>'
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' tests\profile_b\browser.cjs
& .\.venv\Scripts\python.exe tests\profile_b\verify_native.py --runtime $env:CF_PROFILE_B_RUNTIME
```

浏览器流程从页面登录、添加员工开始；API仅作观测断言。原生补充验证只在四档案通过后运行：核对真实磁盘与 SQLite、官方新会话对照、原会话只读工具、真实网络故障后的创建/同步恢复，以及未知同名拒绝。临时故障代理仅用于测试，退出即关闭，不是产品前提。依赖审计使用 `verify_dependencies.py`；CI 配置见 [profile-b-native.yml](../../.github/workflows/profile-b-native.yml)。

本机当前依赖已经齐备。其他电脑/新 runner 的构建顺序：项目独立 venv 中 `pip install -e '.[dev]'` → 固定官方 checkout → 官方完整依赖阶段 → 启动监督器。不要用部分 pip 核心包冒充官方安装。

详细结果和证据见 [validation.md](validation.md)。
