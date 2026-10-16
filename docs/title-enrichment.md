# 论文标题与网页图标（2026-10-14）

论文导入后，由 worker 的独立标题线程自动处理；已有记录也会补全，不需要启动解读。arXiv 优先使用官方页面的 citation_title，其次使用匹配论文编号的 Atom 元数据。PDF 使用首页文字让团队最早配置的模型提取原版标题，模型返回的文字必须能在提供的论文内容中找到；扫描版首页可交给支持图片的模型。英文原题同时生成简体中文译名，中文原题不重复生成译名。明确填写的自定义导入标题会保留，点击“重新获取标题”则恢复从论文识别。

列表显示 14px 原题与下方 12px 中文译名，支持两种标题搜索。等待期间自动刷新，失败显示安全错误和重试入口。解读弹窗中的标题也会更新，不重置用户选择的解读配置。缺少模型时显示提示，配置模型后自动继续。新接口与状态字段见 contracts.md；字段存入现有 metadata_json，不需要数据库迁移。标题任务采用五分钟可恢复租约及写入令牌，避免多 worker 重复领取和过期任务覆盖数据。

网页增加 SVG、ICO 和 Apple touch 图标，使用现有绿色与文档图形。

验证记录：

- 在独立 PostgreSQL `paper_reader_titles_test` 与独立 runtime 下执行后端全量测试：399 通过，包含迁移、租约、取消及新增标题测试。模型 I/O 在自动测试中明确使用 mock。
- 前端 typecheck/build 通过。浏览器全量回归覆盖 61 项；修正新增用例选择器和原标题字号后，相关自动刷新、重试、导入及窄屏场景复跑通过。测试机文件监听器限额已满，使用 `CHOKIDAR_USEPOLLING=true` 运行，无系统配置改动。
- Runner typecheck 和 89 项测试通过。
- 独立只读审查完成；修复了标题刷新可能重置解读选项的问题。
- 真实 Qwen3.8-27B 调用成功识别 BitFit PDF 的完整原题和中文译名。更新预览 API/worker 后，BitFit 与 DSpark 两篇已有论文均自动处理为 ready。
- 实际 `http://127.0.0.1:18080/papers` 登录浏览器验证：两组原题/译名可见，字号分别为 14px/12px，三个图标资源均返回图片，无浏览器应用错误，验证会话已退出。

运行证据在相邻 runtime 的 `test-runs/title-enrichment/`：`papers-live.png`、`live-titles.json`、`web-build.log`、`browser-final.log`、`runner-tests.log`。更新前标题与元数据备份位于 `preview/backups/titles-before-*.json`；进程信息已更新至 `preview/config/processes.json`。
