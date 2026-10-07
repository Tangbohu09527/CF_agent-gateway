# 附件返回的两端现场准备与受控切换

承接 `88f62cfefaf54a7d2c05ca1f916c2196f55491e7`，契约仍为
`cf-artifact-return/v1`。本次只交付部署衔接，不改返回协议、不重跑获批模型样本。
下面的生产操作是**后续维护窗口入口**；本轮没有执行 apply、启停、签发、生成现场
签名秘密、调用模型或微信。本仓功能默认关闭。

## 已有证据及缺口

CFserver 信息来自操作者 **2026-10-07 13:45:45 +08:00** 的只读脚本输出，
不是 Codex 的 SSH 查询。附件只包含 Nginx 指令摘要，没有完整配置正文；不能根据摘要
生成一份新 nginx.conf 覆盖现场。入口将在 CFserver 本地读取完整原文件，未知结构拒绝
规划并给出错误码，保留所有原件。

| 项目 | 已有非敏感引用 / 状态 |
| --- | --- |
| Gateway 部署 | `/opt/cf-agent-gateway/docker-compose.prod.yml`、`config/production.yaml`、`.env` |
| 四个应用的旧镜像 | `sha256:1c78a61f8728bf629ec3cd7ea1037e6f604d7d6e1e4e06e46ade8d8a7e53f163`；该构建不含 artifact return |
| Gateway HTTPS | `https://192.168.1.233:18444`，`cf-gateway-https`，容器内 8443、101:101 |
| HTTPS 资产 | `/opt/cf-agent-gateway-https/{compose.yaml,nginx.conf,tls}`；容器读取 `/etc/nginx/nginx.conf` 与 `/etc/nginx/tls/server.crt`、`server.key` 引用 |
| HTTPS 固定镜像 | `sha256:43d9d8c1f8968f09df8c1aa6c136ecc617e62d64fe4c0b24c97de4eb210bd973` |
| Gateway 公共叶证书 | Issuer `CF Gateway Local CA`；SAN `cf-gateway.internal`、`192.168.1.233`；有效至 2027-09-29；SHA-256 指纹 `5D2459FADEE0DA25096AB12ED953B5A4A59135416A5254EC9CE045018F2E401C` |
| Artifact | `/var/lib/cf-agent-gateway/artifacts`，`gateway-state` 持久卷；API/Dispatch 已由操作者确认读写、10001:10001，Delivery 仍需入口刷新核对 |
| 旧 Hermes | `http://192.168.1.232:8642`；Gateway 未配置 `hermes.ca_file` |
| 现场队列 | 当时在途/待派发/待投递为 0，dead=2 保留；不能用这份历史快照替代维护窗口复查 |

Windows 本轮只读核对：源码仍为官方
`0f4a98f87c17007b81500239d0bd5b9574027b73`，实际选定 generation
`3b7b06fec11340e7b9ed94f8e3e7267e`、Python 3.14.7。已观察 8642 的现用进程，
不依赖旧 PID 执行管理。原启动链为 Startup 的 `Hermes_Gateway.vbs` →
Hermes home 的 `gateway-service/Hermes_Gateway.vbs` → 已选 Python 的
`-m hermes_cli.main gateway run`；正式管理复用已装 `bin/hermes.exe` 的
`--profile default gateway stop/start`，不新建服务或安装体系。

现 `platform_toolsets.api_server` 缺少显式列表。用现有服务认证引用仅在内存发出
只读 `GET /v1/toolsets`，没有 Agent/工具/模型调用，配置摘要前后相同。观察到启用的
17 个 toolset 为：browser、cf_filebridge、cf_filebridge_create、cf_filebridge_inbound、
code_execution、connections、cronjob、delegation、file、image_gen、memory、session_search、
skills、terminal、todo、vision、web。保留这个实际选择后仅追加 `cf_artifact_return`。
每次规划及停机前重新核对动态集合；已有显式列表不被替换，存在默认 MCP 等无法等价
冻结的情况拒绝规划。保留旧插件配置不等于使用 FileBridge 作为返回前置条件。

**当前尚未具备可应用的现场 TLS 候选：**

- 已找到的 `C:/Users/Admin/CF-FileBridge/client-04c7e717/trust/ca.crt` 属于
  `CF File Service Integration CA`，文件 SHA-256 为
  `68073cc4b06e9db8ce2fb065ae9c3fc1cc0b74a5d8b1b5b4f06b9ed8baf6f95a`。
  它是 FileBrowser 的 CA；对 Gateway 18444 的实际校验失败（证书链错误 20），没有关闭
  校验或向该入口发送凭据。它不能作为 Gateway CA。
- Windows 尚缺经过独立核验的 **CF Gateway Local CA 公共证书引用**。操作者给出的叶
  证书元数据不等于 CA 文件，也不是可信握手成功证明。
- Hermes 尚无已确认的新 HTTPS origin/端口、服务器证书及私钥引用。
  两端共用的新引用必须先准备，不能先把 8642 改为 loopback。
- 拟用工作根 `<HermesHome>/cf-artifact-return-work`、插件目标
  `<HermesHome>/plugins/cf-artifact-return` 是计划目标，当前不表示已创建或已安装。

## 入口及固定发布获取

服务器入口：[prepare-return-server.py](../../deploy/prepare-return-server.py)，只需现有
Python 标准库、Git、Docker/Compose。它在**固定候选镜像的短命离线容器**中调用本仓
`site_server` 与既有 enablement helper，使用完整本地配置及私有候选目录；不挂 Docker
socket，不需要在宿主安装 Python 依赖。Controller 与部署入口始终读取同一份持久
`docker-compose.prod.yml` / `.env`，没有仅对一次命令生效的 override。

Windows 入口：[prepare-return-windows.ps1](../../deploy/prepare-return-windows.ps1)，使用
PM facts 已选 Python 和本仓 `site_windows`；不安装、升级或替换官方源码。两端各自
保留私有操作记录；跨机只传递非秘密的阶段回执及公共证书/CSR，不传 capability、
模型 Key、Token、服务器私钥或 CA 私钥。

两个入口的默认阶段均是 plan；引用 JSON 由入口生成。以下变量是操作者选定的私有
记录文件或已批准的**新公共 TLS 引用**，没有模型/Token/密码参数。`DEPLOYMENT` 两端
相同；服务器接收 `export-public` 的实际输出，不能手工拼造阶段回执。

```powershell
# 在固定 Gateway release checkout 中；这些命令不启停服务。
.\deploy\prepare-return-windows.ps1 -Phase init-request -DeploymentId $Deployment |
    Set-Content -LiteralPath $DraftRequest -Encoding utf8
.\deploy\prepare-return-windows.ps1 -Phase bind-tls -Request $DraftRequest `
    -HermesOrigin $ApprovedHermesHttps -GatewayCaFile $GatewayPublicCa `
    -HermesCaFile $HermesPublicCa -TlsCertFile $HermesCertificate -TlsKeyFile $HermesKeyReference |
    Set-Content -LiteralPath $WindowsRequest -Encoding utf8
.\deploy\prepare-return-windows.ps1 -Request $WindowsRequest -Phase plan
.\deploy\prepare-return-windows.ps1 -Request $WindowsRequest -Phase export-public |
    Set-Content -LiteralPath $WindowsPublicReferences -Encoding utf8
```

材料未齐时先运行 `plan` 得到 pending；只有已批准新 origin 时才运行 `csr-plan`。
`prepare-csr -AuthorizeMaintenance` 属于后续明确授权的证书准备操作，本轮没有执行。
现有 CA 签发身份/私钥引用尚未提供，服务器 `certificate-status` 明确停止在原批准 CA
流程；不会伪造签发权限或自动另建 CA。

```sh
# 原有 SSH/sudo 会话内执行；不建立新管理通道。CA 参数是服务器本地公共文件。
python3 "$RELEASE/deploy/prepare-return-server.py" init-request \
  --request "$SERVER_REQUEST" --deployment-id "$DEPLOYMENT" \
  --release-root "$RELEASE" --release-commit "$COMMIT" --candidate-image "$IMAGE_ID" \
  --peer-public "$WINDOWS_PUBLIC_REFERENCES" \
  --hermes-ca-file "$HERMES_PUBLIC_CA" --gateway-ca-file "$GATEWAY_PUBLIC_CA"
python3 "$RELEASE/deploy/prepare-return-server.py" plan \
  --request "$SERVER_REQUEST" --release-commit "$COMMIT" --candidate-image "$IMAGE_ID"
```

只有后续批准维护窗口才添加 `--apply-authorized`（服务器）或
`-AuthorizeMaintenance -ExpectedPlanSha256 $ReviewedSha -PeerReceipt $ServerReceipt`
（Windows）。服务器阶段为 `quiesce → apply → start → verify`，每次保留原 request、
固定 release/image 和私有状态目录。Windows 为 `stop → apply → start`，必须交错执行
下面的维护顺序，不能在一端连续完成后才处理另一端。服务器状态目录的
`peer-receipt.json` 和 Windows 每阶段输出供对端读取，传递时保留原 JSON，并放到
服务端 root 私有 0600 文件。入口校验 deployment/release/plan、origin/Profile 及两向
公共 CA 摘要；这不替代原服务认证。

`release_commit` 必须填**本轮最终通过 CI 的完整提交 SHA**，不是旧的 `88f62cfe`。
不要移动现用 Gateway checkout 来准备候选；在独立 release checkout 核验该 SHA 与干净
工作树，再按现有 Dockerfile 构建，保留输出的不可变 image ID：

```sh
# 在后续批准的准备阶段，RELEASE 指向独立 checkout，COMMIT 是已审核完整 SHA。
git -C "$RELEASE" rev-parse HEAD
git -C "$RELEASE" status --porcelain
docker build --platform linux/amd64 --file "$RELEASE/docker/Dockerfile" \
  --build-arg BASE_IMAGE="$APPROVED_BASE_IMAGE_DIGEST" \
  --build-arg SOURCE_COMMIT="$COMMIT" \
  --iidfile "$PRIVATE_STATE/candidate-image.id" "$RELEASE"
docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "$(cat "$PRIVATE_STATE/candidate-image.id")"
```

入口还会核对镜像来源标签、包内实际代码与该 release 的文件摘要；不会只相信镜像标签。
新镜像 ID 在实际构建前未知，本记录不伪造可拉取的 registry digest。保留旧镜像 ID
作为回退基线，不清理旧镜像、不重装现用环境、不运行 `install-clean-device.sh`。
`APPROVED_BASE_IMAGE_DIGEST` 沿用现有发布记录中已审核的基础镜像 digest；入口不负责
重新选择基础镜像，也不隐式执行 pull/build。构建出的最终 image ID 仍须由入口独立核验。

## 受管差异与校验

- **Nginx**：只新增 `/internal/hermes/returns/` 的规范 dispatch/artifact slot 路由。
  沿用原 `$gateway_upstream` 及解析方式；只接受 GET/PUT，不开放整个 `/internal/`。
  常见 server 级方法拒绝通过精确方法/路径 map 增量处理，原路径的方法规则不放宽；
  未识别的 if/map、rewrite、include、location 冲突拒绝规划。
  原 server 级 16 KiB 不变，返回 location 单独 1 MiB。
- **代理传递**：原规范路径、Authorization、X-CF-*、Content-Length 保留；禁止缓存、
  重定向改写和上传自动重放，关闭请求/响应代理缓冲，受限内存 body buffer。
  沿用原非 root 容器与已有私有临时存储，不挂载宿主业务目录。完整候选要通过
  `nginx -t`，包括原证书/临时目录约束；不能只做文本匹配。
- **Gateway**：`api.max_request_body_bytes` 与 `artifact_return.max_bytes` 均为
  1,048,576。保留数据库、模型、凭据引用、历史路由及 `legacy_runtime_confirmed=false`。
  若原全局限长已经大于 1 MiB，规划拒绝并要求单独审核，不静默收窄其他接口。
  `hermes.base_url/ca_file` 切换到批准的 HTTPS/显式 CA。规划阶段比较候选镜像与现用
  镜像的 Alembic 单一迁移头，并核对现用 Gateway 的数据库/迁移健康状态；迁移头不同
  即拒绝本次切换。没有 schema 变更，只检查，不运行 migrate/upgrade。
- **持久 Compose**：四个应用协调使用同一固定新 image；仅 API/Dispatch 追加 Hermes CA
  只读挂载与专用签名 env 文件引用。`x-runtime` 共用 env_file、Poll/Delivery 环境不
  追加签名秘密。签名秘密只在正式 apply 窗口于 CFserver 本地产生，留私有 0600 文件，
  不进入 Git、输出或 Hermes。
- **Artifact**：继续 `gateway-state` 和 10001:10001。入口核对真实容器的 mount source、
  目标、读写属性及配置字节；维护窗口验证使用本次新建的私有探针文件核对三个消费者
  所见字节，不借 FileBrowser 正式目录。
- **Windows**：在现有 config 自动保留模型、Token、其他插件/工具，追加本模块设置。
  正式切换才把旧 HTTP listener 限制到 loopback，并在同一官方 app/runner 启用 TLS site。
  使用 `gateway_ca_file` 显式 SSLContext，不修改 certifi、系统信任或工具权限。

代理语义依据：[Nginx proxy 模块](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering)、
[server rewrite 执行顺序](https://nginx.org/en/docs/http/ngx_http_rewrite_module.html)。
以上仅描述生成器可接受的结构；现场完整原配置尚未由 Codex 取得，最终以本地规划输出为准。

## 维护窗口顺序

1. **准备全部候选**：两端 `init-request` / `plan` 从已有配置建立引用文件，输出明确
   pending。只补新 HTTPS origin、经过验证的公共 CA/证书引用及固定 release/image，
   不重填模型、Token、密码，不手改 YAML。Windows `csr-plan` 先展示所需 SAN/用途；
   后续获批 `prepare-csr` 才在 AI 主机私有目录生成服务器私钥及 CSR。由批准的 CA 流程
   在 CA 所在主机签发，只返回证书及公共 CA；私钥不跨机。没有批准的 CA 引用就停在
   pending，不借 CFserver 服务器私钥，不自动新建 CA 或 mTLS。
2. **服务器 quiesce**：显式授权后经原 Controller 关闭 Poll/Delivery Gate。单独对
   实际 Dispatch 进程正常 SIGTERM、停止新 claim 并有界等待；不直接用 core systemd
   的 240 秒上限截断最长 3660 秒 drain。DB/migration 健康且实际在途为 0 才继续。
   不把健康接口 DB 失败时的占位零当作排空。保留 queued/dead/uncertain，不自动重派。
3. **Windows stop**：消费同一 deployment/release 的服务器 quiesced 回执，再复查
   工具集合、配置/源码/执行器及进程身份，通过原官方入口停止 Hermes。回执是操作者
   传递的协调记录，不是服务认证凭据；每端仍重新检查本地状态。
4. **协调 apply**：双方确认停止并复核审核过的 plan SHA 后，应用原件备份过的候选。
   任一失败保持 Gate 关闭并保留 journal，按同一请求/摘要恢复，不能对半完成状态重新
   规划扩大范围。四个 Gateway 应用协调换版本，Poll/Delivery 只创建不启动。
5. **Windows start、服务器 start/verify**：按顺序启动原官方 Hermes，验证实际新 HTTPS
   与只读认证接口；服务器收到 tls_ready 后才启动新 API/Dispatch，并验证实际读取的
   配置/模块、CA/存储及两向 TLS。单文件 bind 原子替换后会遗留旧 inode，所以 Nginx
   走原 Compose 重建该 HTTPS 服务并核对容器内实际字节，不能仅 reload。
6. **ACK 与有限恢复另行授权**：此入口不调用模型，也不会自动打开全部流量。
   只读 TLS/`GET /v1/models` 成功不等于 Accepted ACK。ACK 必须由一个正常准入的新
   Message → 真实 Dispatch claim → 官方 Agent 请求产生；会发生模型调用，若要求
   返回文件还会上传、持久化并产生 Delivery，需要另行批准任务、来源聊天及允许的
   投递效果。禁止伪造 RUNNING claim、手改 DB 或先开放所有流量试错。记录原请求，
   超时/uncertain 只核查，不换任务重试。通过后才按批准范围由原 Controller 恢复收发。
   现 `/internal/messages` 仅做消息持久化，不能拿它冒充完整准入或伪造测试 claim。
   后续验收须先明确唯一准入来源和收发窗口；本入口没有“为测 ACK 打开所有 Gate”的步骤。

   最小 ACK 任务可指定为“仅回复 `CF-RETURN-ACK-<deployment_id>`，不要读取、上传文件或
   调用工具”。它仍会创建新消息/真实 Dispatch、调用当前模型、写入会话和持久 Response/
   Delivery；Delivery Gate 未恢复前不应发送到微信。必须单独批准该消息的来源账号/聊天、
   准入窗口、一次模型调用及文字是否允许投递，保留请求编号和 HTTP Accepted ACK 证据。
   此项只核验真实请求作用域和 ACK，不证明现场附件上传或附件实收；后两者需另行批准。

`cf-agent-wechat` 登录 Runtime、PostgreSQL、FileBrowser 不在这些入口的启停目标内。
Poll degraded、dead=2 与既有 Skill/半升级偏差不在本轮修复范围；功能准备成功不抹除告警。

## 回退和部分失败

先保持/关闭 Gate，撤销或排空当前任务并保存 uncertain；Windows 通过原入口停止，
双方使用本次私有 journal 恢复原始字节。任何后来的未知修改或备份摘要不匹配都拒绝覆盖。
按两端回退回执协调恢复旧 HTTP 与旧四应用镜像，不能先启动一个指向尚未恢复入口的
Dispatch。保留新建私有签名文件、日志和失败 journal，不清队列、不重派历史任务。
恢复收发仍由原 Controller 与操作者批准决定。
实际顺序为服务器 `quiesce` → Windows `quiesce` → 双方 `rollback` → Windows
`start-legacy` → 服务器 `start-old`；最后一步要求对端已实际恢复旧 HTTP 的回执，
仅恢复配置字节不够。启动门禁还拒绝可重试 FAILED 等会自动被 claim 的工作，不能靠
清空记录凑通过。Artifact 新目录/已收紧的私有权限与本次签名文件保留，不自动放宽权限。

配置原件可能内含已有秘密，备份仅在本机私有目录保存，不能上传 PR。全部候选、plan SHA、
维护阶段和原/新镜像身份均可核查；没有自动清理未知文件或历史安装残留。
首次保存候选、尚未落 manifest 时进程退出，目标尚未修改，但私有残留需要核对；入口
失败关闭，不宣称此阶段也能无条件自动恢复。
成功启动后的最终回执丢失，可按原请求重跑 `start` / `start-old`；只读复核实际容器、
配置挂载与健康状态后重发回执，不再次启动应用或重建 Nginx。

## 本轮验证边界

针对性测试覆盖候选保持、原件回退、部分完成恢复、后来修改拒绝、工具选择保持和维护阶段
门禁。Nginx 联合测试用自建测试 CA、非 root Nginx、真实 Gateway HTTP/SQLite/Artifact，
原始合成上传超过 16 KiB 且不超过 1 MiB；验证 READY、文件完整性、超限/无权/错误路径
拒绝及原路由限制。它不使用获批 PDF/PNG、不请求模型、不代表现场 nginx 配置已通过。

Windows 本机没有 Nginx/Docker；真实 Nginx 用 CI 的隔离 Linux runner 强制执行，缺少
可执行文件会失败而不是跳过门禁。具体最终 SHA 与 CI 结果在 PR 当前进度记录。
本机最终针对性回归为 **137 passed、1 skipped**；唯一跳过项是上述真实 Nginx 联合测试。
服务器 Docker/Controller 调用由测试替身接收，不能据此称为 CFserver 已运行通过。
现场 apply、证书签发、真实挂载/TLS/ACK 与微信实收仍未执行。
