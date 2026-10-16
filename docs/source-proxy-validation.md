# 论文下载代理验证（2026-10-14）

因 DSpark 原文直连下载超时，新增平台管理员网络配置页及 HTTP CONNECT 代理支持。预览服务已在网页保存并启用 `http://127.0.0.1:10826`，下载总超时为 120 秒。配置保存在共享 storage 下，后续下载立即读取，模型传输保持原有独立配置。

验证结果：

- 后端独立 PostgreSQL 全量测试 412 项通过，覆盖迁移、任务租约/取消和新增代理测试。代理传输测试使用实际本地 CONNECT 服务，检查目标 IP、Host、TLS SNI、重定向校验、体积限制、失败无直连回退、持久化热更新及管理员权限。
- Runner 类型检查和 89 项测试通过；前端类型检查和构建通过。
- 既有浏览器 61 项通过。新增两项网络配置用例修正测试路由匹配后通过，覆盖未保存配置测试、保存后重载回显、代理测试失败提示及拒绝带凭据的地址。
- 独立只读审查未发现阻塞问题。
- 使用应用的新下载函数真实通过代理获取 DSpark PDF：1,055,890 字节，约 0.7 秒；HTML：362,897 字节，约 1.22 秒。
- 实际预览页面 arXiv 连通性测试成功，用时 0.6 秒；保存并刷新后开关和地址正确回显，配置文件权限 0600。
- 已通过授权 API 重试原失败的 DSpark 解读，新任务 `9c36a27e-a0ac-4640-a8aa-6d1f990c9bca` 的 source 阶段 succeeded，并进入后续 extract 阶段。此验证仅确认下载/解析问题已解决，不代表整个解读已完成。

证据保存在相邻 runtime 的 `test-runs/source-proxy/`：`backend-tests.log`、`runner-tests.log`、`browser-tests.log`、`browser-network-final.log`、`web-build.log`、`network-live.png`、`retry-job.json` 与 `verification.json`。验证登录会话已退出。
