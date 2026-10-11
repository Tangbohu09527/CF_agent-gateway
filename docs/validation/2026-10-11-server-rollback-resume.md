# 2026-10-11 baseline 标签与中断回退接续

本页保留原恢复修复的回归证据；最新现场以
[2026-10-11 现场摘要：用户终端回传证据](2026-10-11-gateway-site-status.md) 为准。
用户已确认 f9 两端旧配置回退完成，`complete-rollback` 成功且 `assets_rewritten=false`，
旧 Hermes 及服务器 `start-old` 成功；外层 `rolling_back` 不再是当前停点。
新 deployment `cf-return-env-loopback-20261011-r1` 固定 release 为
`d9bae61a043f1be4303e5c23661ec51904d5e708`，候选已构建、新双端 plan 通过，
尚未 apply/start/verify。用户随后仅对 CFserver 执行 `sudo reboot`，重启完成及之后的容器/Gate/微信
登录状态仍待核验，Windows 是否关机未确认。当前停在**新计划通过、未切换、重启后待复核**。
本次文档更新未重跑下面回归，也不以文档新提交替换固定 controller/release。

在 `codex/fix-hermes-env-loopback` 的 `cfece2603d07f34eca305256d0365f5e74beca91`
上继续修复；该提交的 Hermes HOST/TLS 实现保留。旧 `2d15` 工作树、旧 release、
request 和 journal 未修改。本轮未连接 CFserver、未启停真实服务。

## 根因与修复边界

原 `check_rendered(baseline_image)` 把 Compose 渲染出的标签字符串直接与不可变
Image ID 比较。现场原标签解析正确且四容器 ID 正确，仍被误判。普通 rollback 和
start-old 共用该错误判断，故只收尾外层状态不足以解决启动前的再次失败。

新 baseline 模式重新渲染原 plan 哈希绑定的 `.before` Compose，核对原环境文件证明；
每个当前 image 引用必须与原记录一致，再由本地 image inspect 解析到原 baseline ID。
标签别名漂移、错 ID、缺失镜像和原记录损坏均拒绝。candidate 路径仍要求渲染字面值
为固定不可变 ID，revision/source/包内源码绑定不变。

`complete-rollback` 只接受资产 journal 已 rolled_back、外层 rolling_back/rolled_back。
它核对原 request/plan、三个原文件和所有前后备份摘要、未变引用、四容器停止状态及
baseline ID、Nginx 语法，再次复核后通过正常状态机写 rolled_back 和原协议回执。
原 server 状态和 peer 回执原字节先作不可覆盖副本；原资产不重写。恢复原 owner 时，
只有 production.yaml 可接受 UID 10001、nginx.conf 可接受 UID 101；备份保持私有。

新控制器 commit/干净 checkout/脚本字节与原 f9 release 分别验证，只开放上述收尾
和 start-old 两阶段。原 f9 request/plan/journal、candidate 镜像和旧 release 仍被
原 preflight 验证；不冒填 release SHA。start-old 必须接同一新 controller 的收尾记录。
普通 rollback、start-old 首次/中断重试/成功后回执重发全部使用 baseline 身份规则。

## 验证

新工作树独立 Python 3.12.10 环境，执行：

```text
python -m pytest tests/test_return_site_server_entry.py
  tests/test_return_site_server_recovery.py tests/test_return_site_server_assets.py -q
```

最终 **154 passed**；一个既有 Starlette/httpx 弃用警告。新增恢复回归使用真实
enablement prepare/apply/rollback journal 和合成私有文件，Docker/服务操作为替身。
覆盖现场标签正确 ID、错 ID、标签漂移、镜像缺失、candidate 不放宽、损坏 journal/
备份/原文件、阶段/版本绑定错误、收尾不改资产字节及 mtime、原状态回执留存、Nginx
失败及检查中漂移、收尾后 start-old、三个 start-old 阶段拒绝被重指向的标签。
所有子进程 stdin 为 DEVNULL，外层 shell 输入不会被消费；临时 Docker run 使用
`--pull=never`，不向 Compose run 传入 2.26.1 不支持的参数。

`test_return_compose_real.py` 新增固定 Compose 2.26.1 和 2.39.4 的真实 config 渲染
回归，使用现场原标签；镜像 inspect 仍为替身。当前 Windows 未配置这两个二进制，
本地该两项 skipped，未把跳过算通过。既有 CI 校验下载二进制的摘要并强制执行这组，
精确提交的 CI 结果另随最终交付报告。

Ruff lint/format、UTF-8/本地 Markdown 链接与 diff 空白检查通过。新恢复手册中的
PowerShell 块通过 Windows PowerShell 5.1 语法解析，未执行其中的现场命令。

## 交付与未覆盖

[恢复手册](../deployment/server-rollback-resume.md) 保留已完成的历史顺序：新控制器
收尾 → 原 Windows start-legacy → 新控制器 start-old。后续现场回传已确认这些阶段
成功，当前不再从此恢复序列开始。新 checkout 与证据目录独立，旧 request 和 plan
原值直接使用，回执均由正式入口产生；接管先核验重启后状态和现存新部署材料。

原代码修复验证阶段未执行真实 Docker daemon、真实 Nginx 镜像/挂载、实际现场队列和
数据库、真实 Windows legacy 恢复及跨机交接。之后由用户回传的现场完成结论另列摘要，
不把测试替身结果当现场证明。HOST/TLS 的前轮隔离证据保留在
[原验证记录](2026-10-11-hermes-env-loopback.md)，不能冒充本轮现场验收。
2026-10-11 远端核对 PR #14 保持 Draft/Open、head=f9、base=main；独立修复分支
`codex/fix-hermes-env-loopback` 的 d9 是其 f9 的线性后继，但没有因此进入 main 或改变
PR #14。原修复只推独立修复分支；本次文档 PR 的 base 是该独立修复分支，不能把文档
commit 当部署版本。未来功能集成另经审阅，不属于本次文档更新，不推进旧工作树、
不 force push、不合并到 main。
