# 微信工作台验证证据

CFserver 实际部署、真实只读资料与 LAN 浏览器核验见[2026-10-21 交付记录](delivery-2026-10-21.md)。`live-*.json` 仅含聚合统计和固定布尔检查，`ci-https-*.json` 为独立合成 HTTPS 检查；它们不属于下列标为合成的截图 manifest。未提交真实资料截图、身份、正文或凭据。

2026-10-21，本地候选的实际页面 → Gateway → SQLite 联合流程已执行。微信联系人、近期会话和消息档案都是隔离合成数据；native HTTP 读取由协议替身提供，未在这些浏览器验收中连接真实微信来源。

完整浏览器流程的 [result.json](evidence/2026-10-21/result.json) 记录 48 项通过，随后最终 UX 的 [ux-checks.json](evidence/2026-10-21/ux-checks.json) 记录 18 项定向通过。完整流程证据与后续 UX 证据分开保留；不把前一轮结果冒充后续所有改动的整套复验。本地使用已有 Playwright 1.62.1 和实际 Edge；新增 [CI](../../.github/workflows/wechat-console.yml) 固定 Playwright 1.56.1 与其 Chromium，CI 结果以实际运行记录为准。

已核对的业务行为包括：真实分页和筛选、联系人关联员工、两级权限、未分组拒绝、模板版本、群设置、节点不可达时准确失败、归档消息分页与北京日期边界、引用及附件元数据、cookie 刷新保留、未认证拒绝、网络错误，以及具名发言者筛选。复制操作在浏览器调用系统剪贴板成功；同时提供只读文本选中回退。群目标模板和电脑来自后端分组关联。

Hermes 在此 console 演示中未连接，Profile 创建成功及真实模型响应不属于这两份证据。页面展示的不可达或未验证结果没有被替换为成功；这里也不重述独立 Profile B 验证的结论。

| 合成截图 | 实际内容 |
| --- | --- |
| [通讯录](evidence/2026-10-21/contacts-1440-synthetic.png) | 搜索、关联员工及权限状态 |
| [联系人详情](evidence/2026-10-21/contact-detail-1440-synthetic.png) | username、Bot 账号、更新时间及复制 |
| [员工权限](evidence/2026-10-21/contact-permissions-1440-synthetic.png) | 启用、两级权限及员工分组 |
| [群列表](evidence/2026-10-21/groups-1440-synthetic.png) | 分组关联模板、电脑及最近活动 |
| [群详情](evidence/2026-10-21/group-detail-1440-synthetic.png) | 稳定来源标识、当前目标配置及群设置 |
| [1920px 聊天](evidence/2026-10-21/chat-1920-synthetic.png) | 实际持久化档案、具名筛选及媒体元数据 |
| [430px 聊天](evidence/2026-10-21/chat-430-synthetic.png) | 窄屏阅读及会话返回入口 |
| [系统状态](evidence/2026-10-21/system-1440-synthetic.png) | 候选边界及默认折叠的诊断 |

截图均来自实际渲染页面。抽屉会遮住原演示标识，因此截图步骤临时在标题上添加“合成测试数据”标注，截图后恢复；没有生成设计图或替换 API 数据。提交证据采用固定文件白名单，没有复制 runtime 登录文件、数据库、连接配置、日志或失败截图。

CI 仅在 `127.0.0.1` 启动本次候选及合成 peer，先运行完整浏览器流程，再运行 `CF_CONSOLE_UX_ONLY=1` 定向流程；结束时停止自己的候选进程，并核对源端请求只包含认证状态、联系人和会话的 GET。它不需要真实来源凭据，也不启动 Hermes、微信或业务模型。

[公开参考与独立实现说明](reference.md)
