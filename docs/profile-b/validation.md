# 2026-10-20 隔离验证记录

## 业务结果

| 动作 | 结果 | 证据 |
|---|---|---|
| 元数据与官方创建 | 已实现并隔离通过；页面触发四个真实档案，无昵称复用 | browser-result.json、真实Profile目录和官方列表 |
| 实际路由与持续会话 | 已实现并隔离通过；员工甲乙/群A/B四路独立，同群换发起人持续同会话 | native-result.json 的每档案SQLite记录；官方profile_name和本次持久化回执 |
| 模板受管字段同步 | 已实现并隔离通过；SOUL与display.language回读，版本分开 | 模板v1→v2→v3；失败矩阵保留旧已应用版本 |
| 新会话生效 | 已实现并隔离通过；官方对照新会话采用v3 | native-result.json |
| 旧会话生效 | 已验证保持原v1与历史；即时无损刷新能力未接通 | browser-result.json、native-result.json、restart-result.json；UI明确待生效 |
| 隔离和保留 | 已隔离通过；四个state.db、空凭据/无复制cron；同步保留记忆和本地agent.max_turns | native-result.json |
| 权限与管理Web | 已接通；员工、组、群、模板、节点、派生及测试消息真实操作 | 页面截图、13项定向管理测试 |
| 生产微信/异机部署 | 未执行；未切生产，不访问真实账号 | 本轮边界 |

没有静态假成功接口。节点不可达显示失败；部分创建失败显示 create_failed，已应用版本为空；实际创建存在后重试同一绑定成功。同步中途503后 applied=1/desired=2，修复网络后真实同步为2。未知同名官方Profile拒绝覆盖，其description保持未知owner。相同主体重复管理请求保持同一档案，并发请求由数据库唯一约束和操作token串行。

## 验证层次

- 真实组件：Gateway FastAPI、Alembic SQLite、未修改官方Hermes完整锁定环境、官方REST/WS、各Profile磁盘与state.db、官方read_file。
- 替身：确定性OpenAI协议模型（无付费调用）；微信仅为管理台协议测试输入，未启动真实微信。
- 浏览器：已有Playwright驱动无头Edge，真实页面添加甲乙、权限、未分组拒绝、模板与组、节点检查、四Profile创建、私聊/群消息、模板更新与旧会话待生效。刷新重新登录后数据仍在。
- 重启：交付start.ps1/stop.ps1实跑；Gateway及官方节点重新启动后，页面状态、原持久会话、历史均保留；原SOUL仍v1。正常控制台错误和页面异常均为0；刻意断网显示中文错误并恢复；1500/800/390宽度无页面横向溢出。
- 定向后端/迁移：102项通过；最终管理权限/审计定向13项通过。Hermes单元替身仅用于权限、并发、错误状态等单元回归，不算官方验收证据。
- 官方依赖审计：59条展开依赖满足、缺失0、不匹配0，固定源码Git干净；没有修改官方源码、PYTHONPATH注入或Monkeypatch官方行为。
- Windows正常全套回归已执行：2665通过、198跳过、18失败。17项失败来自既有POSIX存储/链接语义在Windows不支持，另1项是既有租约日志时序断言未通过；未修改这些安全边界或把它们算通过。该长回归运行于开发过程中，后续管理改动另以定向测试和真实重启页面复验；提交后Linux CI核对准确提交。
- 提交后CI结果另行补充；未执行或未完成项不计通过。

源码研究与实测边界：普通会话缓存提示的行为已实测。源码存在Bot Chat专属SOUL fingerprint重建机制，但它还要求hermes-bots语义，本候选未开启。普通会话后续压缩/重建何时采用更新没有给出经过实测的承诺，状态持续保留待生效。

文件可信边界：read_file只读取本轮Profile下的小型合成文件，证明同Profile、同会话工具处理。它不是FileBrowser正式ACL验收，不解决PR23 `_meta={}` 和可信task关联、Artifact/Response/Delivery/微信返回缺口，也没有重跑18MB实验。

## 现场与代码边界

依赖及工作树仅在 `D:\Tangbohu09527\.cf-worktrees\profile-b-management` 下；没有连接CFserver、正式文件服务或正式Hermes，没有读取生产.env/Token/私钥，没有操作真实微信、Poll/Delivery、扫码、发送或收件，没有启动旧任务。

Windows首次启动时缺少子进程SystemDrive变量，生成了官方checkout内未跟踪的Windows缓存目录；已停止本轮子进程，把该生成缓存归档到本轮runtime，补齐子进程必要OS变量后复验源码干净。未改官方跟踪文件或系统设置。

所有证据只含合成测试身份/消息与随机测试ID。运行时密钥、connections.json、数据库、日志和完整Hermes HOME不进Git/CI制品。CI只上传明确列出的脱敏JSON与真实页面PNG。

## WeChat并行准备结果

复用PR9 `7bd42b623d405956080a13481bbf4de21aa3477a`，运行时源码无新修改、无需重新构建。准备分支 `codex/docs-wechat-filename-prep-20261020`，提交 `6ba59c68ec5324caa54e15609bc52bfd8054e3e8`，Draft PR12。兼容核对、构建包与校验、中文/空格/括号/同名不同内容样本、现场短方案已准备；本轮该PR的18项CI成功。原构建包有效到2026-11-01北京时间，未切生产。

真实验收仍需用户扫码、在私聊/群聊各发送少量代表文件，核对实收原名、内容和目标；本轮不执行。PNG业务验收口径沿用既定决定。
