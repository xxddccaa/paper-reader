# Workflow v2：BitFit 真实模型评估

日期：2026-10-13。实际 API → PostgreSQL → Python worker → Node/Pi → 用户提供的模型服务，全程使用真实 PDF，没有测试桩替代模型输出。

## 最终结果

`live-qwen38-guided-v2-03` 在最终 `guided-v2.3` 规则下，**首次解读和表达修订均成功**。运行生成精华版、完整版及结构化事实，共发布 2 个不可变版本。以下用量为 provider 逐响应累计上报，不是单次上下文长度或费用估算。

| 指标 | 首次解读 | 表达修订 |
|---|---:|---:|
| 状态 | succeeded | succeeded |
| 用时 | 448.32 秒（7 分 28 秒） | 120.68 秒（2 分 01 秒） |
| 输入 token | 260,976 | 39,665 |
| 输出 token | 19,876 | 3,805 |
| 总 token | 280,852 | 43,470 |
| 实际执行工具调用 | 17 | 4 |
| 新执行的模型阶段 | extract、fact_review、compose、product_review | revision_plan、compose、product_review |
| 已有阶段复用 | 无 | source、extract、fact_review |

11 条结论关联 14 条证据，引文检查为 **14/14 匹配**，独立会话中的事实与成品复核均通过。此次首次解读及修订都未进入修补阶段。修订指令是仅改表达与组织、用三个具体步骤解释方法、不增加事实或外部研究；报告确认 `original_revision_immutable=true` 和 `revision_packet_unchanged=true`。这验证了表达修改可以在保留已核验事实的前提下完成。

首次提取约 208.29 秒、事实复核约 135.34 秒、成文约 65.08 秒、成品复核约 39.31 秒；修订规划约 6.00 秒、成文约 106.02 秒、成品复核约 8.30 秒。修订总开销降低来自跳过兼容阶段，并不意味着每个新执行阶段都更快。

改造前同模型的 native 运行在 1800 秒时超时，记录 1,871,742 token，未发布产品。新 guided 流程使用 skill 阅读指导，工作范围与 native 不同；上述变化证明本次产品完成和复用效果，不能读成严格同工作量基准。最终成功运行没有外部研究请求，`cross_paper_status` 和 `native_status` 均为 `not_requested`。

## 可直接查看的真实产物

- [改写后的精华版](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/revised-brief.md) 与 [完整版](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/revised-full.md)。模型原文未人工修改。
- [运行报告](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/evaluation.json)：任务状态、实现哈希、用量、复用与版本不变证据。
- [结构化事实](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/reading-packet.json) 与 [修订阶段记录](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/revised-stages.json)。
- [逐项原文复核](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/human-review.md)：开发助手回查，非独立专家盲审；保留待改进项。
- [真实浏览器报告](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/test-runs/browser-live/browser-report.json)、[桌面截图](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/test-runs/browser-live/real-reading-desktop.png)、[移动端截图](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/test-runs/browser-live/real-reading-mobile.png) 与 [事实页截图](../../paper-reader-runtime/test-runs/live-qwen38-guided-v2-03/test-runs/browser-live/real-reading-packet.png)。

浏览器实际使用已发布结果，报告 `fixtures:false`、`published_result_rendered:true`、`reading_packet_rendered:true`、`claim_count:11`；真实登录、租户 API、桌面/390px 移动端、退出撤销、无浏览器异常均通过。桌面和移动端截图已目视检查。临时浏览器账户已删除，临时 API 已退出。

## 阅读质量及实际边界

回查确认，结果正确区分了 BERT-base/large 参数比例、验证/测试均值、随机参数对照、PTB 数值和 SQuAD 随数据规模变化的反转；保留了任务分类层，也将“暴露预训练知识”标为假设。单次直读历史样本中虚构的 Adapters 验证集单元格、固定学习率倍数和通用数据量阈值，未在本次结果中出现。

仍需改善：将 MNLI matched/mismatched 两列合计称作“GLUE 9 个任务”不够精确，应交代 9 个评估列和 WNLI 排除；将 0.08%–0.09% 偏置参数占比写成“训练预算”，容易与任务头参数、计算量混淆；部分“经验”仍偏机制解释，离直接指导选择的密度有差距。自动检查通过不能替代逐项内容评价，同一模型的独立会话复核也可能漏检。

最终只有一篇论文和一次表达修订的真实成功证据，未执行完整 18 项稳定性矩阵。本次未验证其他 skill/论文组合、真正的外部跨论文研究、Docling/OCR 或容器部署。来源用 PyMuPDF 提取页文字，图像与复杂表格的视觉理解不在此成功样本的覆盖范围。超过配置上下文容量的长文会明确失败，任意长文的分块综合仍未实现。

## 固定输入

- 模型：`Qwen3.8-27B`，`http://127.0.0.1:8000/v1/`。
- 显式 `thinking_format=qwen-chat-template`；服务无需鉴权，使用非秘密 SDK 占位值，未向该地址发送环境中的其他 provider 密钥。
- 论文：BitFit，arXiv:2106.10199v5，PDF SHA-256 `56a52ea59f763e312687f8b0c1c8953a508c471925f17733e6c580be832000ed`。
- `skill=ljg-paper`，`workflow_mode=guided`，使用内置双模板。每次尝试总期限 1200 秒、工具额度 100，独立阶段最多 300 秒。
- 所有输出、真实检查点、公开实现文件哈希、用量和阶段记录保存在源码之外的 `paper-reader-runtime/test-runs/`。

Guided 使用 skill 阅读指导，未执行完整原生笔记/脚本流程。与改造前 native 的比较用于说明产品完成情况和成本变化，不是相同工作量下的严格性能基准。

## 保留的调试结果

| 运行 | 真实状态 | 用时 | 记录 token | 观察 |
|---|---|---:|---:|---|
| `live-qwen38-guided-v2-01` 首次解读 | partial，发布 1 个部分结果 | 522.58 秒 | 403,349 | 12 条结论、20 条引文。Host 拦截了数值关系错误和非原文引句，修复后引句全部匹配；模型自行提出的 3 个外部问题未实际检索，最终保留为部分结果 |
| 同一运行的表达修订 | failed，未覆盖旧版本 | 120.51 秒 | 72,033 | 来源、提取、事实复核实际复用；成品复核要求压缩文字，局部补丁的单次提交额度使精确匹配错误无法重试 |
| `live-qwen38-guided-v2-02` | failed，无发布 | 167.21 秒 | 63,495 | 提取变为 9 条结论、没有无关外部问题；局部修复后仍有 6 条引句被原先的逐字符比较拒绝。逐条核对为 PDF 字形和大小写差异，随后修复解析兼容 |

这些运行中修改了规则，不能合并成固定配置的成功率统计。第一次运行的真实浏览器检查通过，实际渲染了部分结果和 12 条结构化结论；浏览器报告同时保留最新修订任务的 failed 状态，不把页面能展示当作任务成功。

## 由真实运行推动的修复

1. 抽取指导以 6–10 条高价值、不重复结论为软目标，默认使用短的连续引句。未来研究进入局限，不自动创建必须执行的外部研究任务。
2. 成文优先服从本次修订的长度要求；精华版选择关键事实，完整版保留其余必要细节。引文标签与目标使用相同 E ID，审阅 issues 只保存真正需要修复的问题。
3. 无效文字补丁可以在有界额度内得到反馈并纠正。每次实际进入执行的提交都经 host 记账，无效尝试也消耗共享额度；成功结果不重复计数。
4. 引文匹配只规范字母型 PDF 连字、NFC、大小写和空白，并恢复为来源中的原始连续片段。保留上标/下标、数字、否定词、标点和脚注；没有使用模糊匹配。该规则记为 `guided-v2.3`，进入缓存指纹。
5. 引文匹配状态独立于跨论文检查和来源覆盖。全局检查缺口仍阻止成功，界面不会把外部检索缺失错误显示为已匹配引句不可信。

人工核对依据是 [BitFit 原文核对记录](bitfit-review-notes.md)，该答案清单未注入模型提示、来源工具或生产逻辑。历史模型输出保持原样。
