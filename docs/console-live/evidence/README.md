# 合成数据实际页面与组件证据

所有截图来自实际运行的候选页面，数据为合成资料。真实运行的组件为 Edge/Playwright、Gateway、SQLite 和后台归档采集；微信及 Hermes 为明确的协议替身。这里没有真实好友、聊天正文、二维码、登录文件或连接配置。固定文件白名单与 SHA-256 见 [manifest.json](manifest.json)。CFserver 现场结果另见最终交付记录，不能由这里的成功推断。

| 证据 | 已验证范围 |
| --- | --- |
| [live-result.json](live-result.json) | 31 项实际页面断言：无需刷新、同秒、迟到、幂等、自发消息、跨会话摘要、权威分页总数、空会话 0→1、实时发言人选项、看历史不跳页、断线补齐、双 Bot 隔离、退出失效、绿红灰及过期、1920/760 布局、无 JS 异常 |
| [live-peer-summary.json](live-peer-summary.json) | 本次隔离数据库 Dispatch=0、Delivery=0，上游副作用调用为空；runner 已停止 |
| [nodes-result.json](nodes-result.json) | 24 项实际页面断言：生成仅业务密钥、缺管理凭据拒绝、不回显已有管理密钥、保存及持久化、服务器分项探测、普通测试不创建、明确确认空档案测试、1440/1920/760 布局 |
| [nodes-peer-summary.json](nodes-peer-summary.json) | 模型调用 0；明确确认后合成管理 peer 实际创建并回读 1 个专用空档案；runner 已停止 |

实时三条合成采样的上游读取→入库为 13/15/9ms，入库→页面为 1257/939/944ms，总计 1270/954/953ms。合成源出现→采集实际读取另为 1917/872/870ms。时间点完整保存在结果文件；这些值不是现场微信传输延迟。

截图入口：

- [看历史与新消息提示，1440](live-history-1440-synthetic.png)
- [系统分层状态，1440](live-system-1440-synthetic.png)
- [聊天页，1920](live-chat-1920-synthetic.png)
- [节点向导与管理凭据校验，1440](node-wizard-current-1440-synthetic.png)
- [节点配置完成后的真实连接失败，1440](node-wizard-1440-synthetic.png)
- [业务与管理分项探测，1440](node-check-1440-synthetic.png)
- [节点清单，1920](nodes-1920-synthetic.png)

第一次节点浏览器运行误让两个合成节点使用同一管理地址，已有重复地址保护准确拒绝；修正测试样本为独立地址后 24 项通过，未移除或削弱保护。初次后端运行因本地工作树 ACL 无法创建隔离库而中止，正常审批后在同范围重跑，节点 21 项通过。当前证据只包含最终通过运行，不把中止运行算成功。
