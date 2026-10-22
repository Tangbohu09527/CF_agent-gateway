# 真实微信实时归档故障：2026-10-22

用户在 Bot 所在群发消息后，管理台没有自动出现，其他会话也显示旧消息。**现场验收未通过**。上一轮合成测试和在线绿灯不能证明真实消息链正常；旧报告的观察时间保留。

## 第一断点与定向证据

2026-10-22 16:51–17:07（北京时间）：

- SSH、sudo 均实际认证成功。候选运行源码仍为 `b7d33d4446a3a3b07076c3fe65f0f678e12887e7`，镜像 `sha256:f71c1f1d964c2c1546abe183b16e104b4c01a306dfc99a05980a72d82e0e5d65`。
- 实际容器挂载与运行配置已核对，使用独立 `/runtime/gateway.db`；宿主准确路径仅存本地运维台账。SQLite 原生只读检查 journal=DELETE；`messages=0`，591 条 observation 均为首次窗口基线，无消息关联。没有因漏复制 WAL 得出空库结论，也不把 591 条基线当作丢消息。
- 采集已启用、22 个会话基线保留，`last_success_at` 持续增长且无整轮错误。上游 API 每轮仍返回同一批旧事件。
- 真实无障碍树只有旧账号登录页的 **Enter Weixin / Switch Account**，无 Chats 列表；微信进程没有打开账号消息数据库。17:04 的最近 20 分钟脱敏日志中，上游监视器因不能识别界面，kill/restart 各 19 次。排障没有主动执行这些重启。
- 固定官方 `96c2e90c497d999bf5189a134a9b3b293731f42e` 的登录识别只匹配 `Log In` / `Open WeChat`，漏掉现场 `Enter Weixin`；`router/status.rs` 在本次识别失败时沿用旧 `ChatOpen/is_logged_in`，因此 API 和绿灯误报在线。**当前第一断点是微信尚未进入接收新消息的聊天态。**
- 另证实 WAL 漏读：同一 `session.db`、同一现有密钥，SQLCipher 4.6.1 的 immutable 查询最新时间为 `1792547869`，含 WAL 的 `mode=ro` 为 `1792550880`。此次消息表两种方式都旧，不能将 WAL 单独写成未登录的原因。密钥与数据留服务器，只回传计数/时间；未 checkpoint 或修改源库。
- 正式 Poll=exited、Dispatch=running、Delivery=exited，本轮未改变；正式档案强制只读。候选不存在准入、派发、投递或附件原件下载依赖。

不清除基线、不倒退 checkpoint、不开放 AI 来造消息。故障期间新消息尚无法从当前上游窗口核验，未伪造或批量回放历史。

## 管理台最小修复与隔离回归

另一个前端竞态已在真实 Edge、实际 Gateway/SQLite、明确合成微信来源上复现：旧快照已在后端取值但延迟返回，新消息随后入库；增量因正文仍 loading 被忽略，却推进游标。释放旧快照后，消息持续不显示；重新打开会话才出现。

修 `app.js` / `live.js` 的批次接受契约：快照未完成时不确认游标，有界重试，快照完成后再应用。保持原布局、过滤、历史位置、切源与退出隔离。`live.py` 仅透传白名单原因码 `auth_observation_unrecognized`，页面据此说明当前登录界面无法识别；不回传任意上游诊断。未改采集写入、唯一键、首次基线、schema、员工权限或 Profile。

| 隔离验证 | 本轮结果 |
| --- | --- |
| 原版竞态 | 两次按目标断言失败：入库 1 条、正文 0 条，重开后 1 条 |
| 修复后同一 Edge 复现 | 7 项通过，加载期间不推进游标，无需刷新出现 1 条，无 JS 异常 |
| 定向后端 | 11 项通过，含同秒同正文不同事件、去重、重建、Message 提交后 journal 中断恢复、当前状态未知及原因码白名单 |
| 管理台后端回归 | 加入原因码测试前 63 项通过；随后新增的 2 项及全部 9 项实时用例共同通过；既有 Starlette/httpx 弃用警告不影响结果 |
| 既有双 Bot 实时浏览器 | 31 项通过，含重连、其他会话摘要、历史阅读、筛选、切源与退出；来源为替身 |
| 副作用 | 上述隔离验证 Dispatch=0、Delivery=0、原生写/发送调用为空 |

入口：`pytest tests/management_console/test_live.py`、`python tests/management_console/realtime_race_browser_runner.py`、`python tests/management_console/live_browser_runner.py`。runner 使用独立 `CF_CONSOLE_RUNTIME`，竞态默认 loopback 18990/18991；由 runner 停止自有服务。CI 只上传白名单合成证据，不上传登录文件、原生日志或现场资料。

## 现场更新与验收

保留既有 HTTPS 管理台书签及管理员登录；准确内网地址存本地运维台账。

2026-10-22 17:31:35–17:32:18（北京时间）已只更新独立候选 console/proxy：

- 运行源码 `73eef5c899292afa1b317d9c89242893ff7139d8`，镜像 `sha256:7dc2fc503b47cdd7c3b9295f8aa949b7cb4a04c87e1adac2f290df3d5c6a9499`。实际 Python 模块和两份 JS 的 SHA256 与该提交逐一一致；没有以文档提交代替运行源码。
- 停候选后使用 SQLite backup API 取得一致性备份并检查完整性；原配置、登录哈希、联系人/分组/权限/节点等表指纹保持一致。22 个会话基线和 591 条 observation 保留；没有删除或重建基线。
- 实际 Edge 使用原 HTTPS 地址并验证证书，原管理员登录成功，聊天和通讯录可打开，无 JS 错误。只读真实来源模式与候选权限隔离仍然有效，没有下载原件或调用模型。
- 17:56 定向复核发现服务环境中的旧 `CANDIDATE_REVISION` 覆盖了新镜像的版本字段，实际代码散列一直正确。18:00:39 备份后只修正这一非秘密字段并重建本候选 console/proxy；不改密码、Token、连接或正式服务。原镜像、两次候选一致性备份及准确回退命令留在私有台账。

用户另行明确批准后，17:48:08–17:48:11 仅热替换正式微信容器内的 API 二进制并对原 API PID 发送 TERM，由原入口循环加载。先使用源库只读连接导出加密一致性 snapshot，完整性与保存账号一致性通过；不复用失败的空备份。旧二进制和加密 snapshot 保留，未执行回退演练。

组件修复见 [WeChat Draft PR14](https://github.com/Tangbohu09527/CF_agent-wechat/pull/14)：源码 `180a11cfa32c5d58378c355776bc4687654acaba`，CI 构建 merge SHA `70d0546b34d038b7b5e6928659ad39c49b50a3bc`，运行二进制 SHA256 `5330f4b2fe69f3deb6a2070aabf93b3a02a63f8ff15ec375c71777dd9c7db4ce`。默认补丁只含登录页识别、fresh auth 和只读 WAL；不含旧 PR 的登录守卫、文件名或安装器。

这是现有容器可写层内的 API 热替换，**没有部署新微信镜像**。原 image config `sha256:3b14e58c908ec11e9163ef0bbb51522bea15c1568c8e1012a29890b9adadc48e` 和 manifest `sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43` 不变。重新创建该容器会丢失热补丁，后续固化镜像需要单独授权。API PID 变化，微信 PID 与保存 session 保持不变，正式 Poll=exited、Dispatch=running、Delivery=exited；没有改变其闸门或运行状态。

17:52 实际浏览器显示 `offline / logged_out`、采集 `enabled / blocked`，不再把旧缓存当在线。18:02:01 最终探测确认环境版本为 `73eef5c899292afa1b317d9c89242893ff7139d8`，`messages=0 / observations=591 / linked=0`、候选 Dispatch=0、Delivery=0，微信与 API PID 均未变；从 17:48:11 起的有界日志窗口 kill/spawn/unresponsive 均为 0。这里只说明已知登录页识别正确、所观察时段未见监视器重启，不能保证所有未知界面永不触发监视器。18:02:27 Edge 再次验证原地址证书、原账号登录、聊天/通讯录和新 JS 均正常，无 JS 错误，仍为 `offline / blocked`。

用户明确要求**扫码登录，不点击 Enter Weixin**，并要求下班后无人值守完成可执行部分。本轮没有点击 Enter 或 Switch Account，没有调用 logout、完整 `/api/ws/login`、fresh QR、清 runtime 或重建微信容器。固定官方 `POST /api/status/login` 只解析屏幕上已有二维码，当前保存账号页没有二维码；导航到扫码页的安全固定控制尚未接通。完整 LoginPlan 仍含身份改写/清理分支，未放开管理台的登录能力守卫。次日需真人按明确边界恢复同一 Bot 扫码，再核对活动进程实际账号和原保存身份。

## CI 的准确结论

- [Console live operations 39755470914](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39755470914)：success，包含 Linux 实际浏览器、快照竞态和固定控制/HTTPS 容器验证。
- [WeChat 原生构建 39755928451](https://github.com/Tangbohu09527/CF_agent-wechat/actions/runs/39755928451)：success，RED 3 通过、8 项预期失败；GREEN 11 通过，组合守卫及单二进制包/loader 通过。构建通过不代表现场扫码或消息通过。
- [Gateway 标准 CI 39755470702](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39755470702)：failure，2938 passed / 7 skipped / 1 failed。唯一失败 `test_lost_closed_ack_uses_finite_host_lease` 发生在首次租约续期、模型调用前；该测试与生产 worker/store 未被本轮修改。5 秒 dispatch 租约和慢 runner 的时间竞态是当前解释，缺少精确 claim 时间，未写成完全证实。未为本次聊天修复削弱生产过期检查或扩大修改；后续 migration/PostgreSQL/V2 显式步骤被跳过，不能算通过。该 CI 的 hygiene 与真实 Compose E2E job 分别成功。

真实私聊、群连续消息、其他会话排序、断线补齐、历史阅读提示、样本级无 AI 副作用均未重验通过。隔离毫秒记录不移植为现场延迟。恢复原账号后才集中做少量标记验收，不为排障退出账号、清 runtime 或强制 fresh QR。

候选仅归档后台仍保留启用配置，但当前认证受阻，`messages=0`、候选 Dispatch=0、Delivery=0；没有本轮真实消息样本，不能据总计数声称完成样本级副作用验收。没有发生已知的本轮非预期 AI 任务或回复。管理台、代理与受限 Controller 保持运行，停止/回退方法和真实挂载留在既有运维台账。全量盘点未完成、FileBrowser 原密码未找回的既有结论不变。
