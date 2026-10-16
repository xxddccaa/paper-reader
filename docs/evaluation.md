# 解读质量评估

工程测试证明接口、权限和执行机制按预期工作；真实模型任务证明所选模型能完成实际流程；重复论文评估用于观察质量是否稳定。这三种证据分别记录。

## 单篇实际流程

安装后端与 runner 依赖并构建 runner，创建一个独立 PostgreSQL 测试数据库。API Key 放入 `PAPER_READER_EVAL_API_KEY` 或已配置的 `OPENROUTER_API_KEY`，不要放入命令行或提交到源码。

```bash
.venv/bin/python tests/live_smoke.py \
  --database-url "$PAPER_READER_EVAL_DATABASE_URL" \
  --runtime ../paper-reader-runtime/test-runs/bitfit-ljg-01 \
  --skill ljg-paper --workflow-mode guided --paper 2106.10199 \
  --model qwen/qwen3-coder \
  --revision '将精华版压缩到600字左右，保留关键条件与引用，用三个具体步骤解释方法。'
```

测试数据库名必须以 `_test` 结尾，runtime 必须为空。每次创建单独账号和团队，连接探测要求真实工具调用，然后驱动真实 API、Python worker、Node/Pi 和论文来源工具。模型 Key 以密文留在数据库中，runtime/config 仅存该隔离运行的配置与加密密钥。

使用自建内网模型时，传入 `--base-url http://HOST:PORT/v1 --model MODEL --allow-private-models`，只在该隔离 runtime 中启用内网模型访问。如果服务无需鉴权且忽略 Authorization，可以增加 `--no-auth`：SDK 使用非秘密占位值，不读取或发送环境中的其他 provider 密钥；这不表示请求省略了 Authorization 标头。需要真实密钥的接口不要使用该选项。

对默认开启思考的 Qwen/vLLM 接口，增加 `--thinking-format qwen-chat-template`，将连接显式配置为使用 `chat_template_kwargs.enable_thinking=false`。标准接口保持默认；不会根据模型名称猜测协议。该选项会影响完成时间和输出，评估报告单独记录，不能把不同配置当作同一组稳定性重复样本。

`--workflow-mode guided` 是默认值，验证程序编排的分阶段解读；`--workflow-mode native` 验证完整原生 skill 流程，两种模式分开比较。输出包括 `evaluation.json`、`stages.json`、`revision.json`、`reading-packet.json`、`brief.md`、`full.md`，有修订时增加 `revised-*` 文件。报告记录实际状态、证据、用量、耗时、版本不变性和检查结果；修订还记录哪些阶段被复用、事实包是否保持一致。`partial` 会返回非零退出状态，不冒充完整通过。无法产生结果时，失败信息保留在 job 与任务目录。

DeepPaperNote 原生模式检查图表时，选用实际支持图像工具结果的模型，并传 `--vision`。`--vision` 是能力声明，不能把纯文本模型变成视觉模型。脚本默认 1200 秒超时，可通过 `--timeout` 调整。

`--max-tool-calls` 默认 160，范围为 10–500，包含原生流程、独立复核和产品生成的全部工具调用。它与超时分别生效；增大额度会增加潜在用量，不会放宽原生检查。报告记录实际配置，比较重复运行时应保持额度与超时一致。

重复调试同一篇论文时可以加 `--pdf /absolute/path/to/retained-paper.pdf`，通过真实上传 API 使用已下载的原文，避免把来源站点偶发超时混入模型质量比较。报告记录上传输入与文件 SHA-256；这不是合成论文 fixture。arXiv 导入和下载应另行验证。

也可对已有失败任务调用真实重试 API，复用原论文、模型连接和模板快照，不改写旧检查或结果：

```bash
.venv/bin/python tests/retry_live.py \
  --runtime ../paper-reader-runtime/test-runs/bitfit-ljg-01 \
  --max-tool-calls 160 --timeout 2400 \
  --revision '将精华版压缩到600字左右，保留关键条件与引用，用三个具体步骤解释方法。'
```

该脚本默认选择最新任务，可用 `--job-id` 指定。它只接受 `_test` 数据库，并要求没有其他排队或运行中的任务；不要并行调用该测试脚本。它调用现有重试与修订接口，生成 `retry-evaluation.json`、`retry-brief.md` 等报告；旧 `evaluation.json` 保留。执行账号结束后被停用并撤销会话与成员关系，保留任务归属记录。调整额度或适配器后的重试属于调试证据，不能混入同配置稳定性统计。

已有测试任务还可以运行不使用 HTTP fixtures 的浏览器检查：

```bash
.venv/bin/python tests/browser_live.py \
  --runtime ../paper-reader-runtime/test-runs/bitfit-ljg-01
```

它临时启动真实 API 和构建后的前端，创建同团队的临时成员，验证登录、数据加载、论文任务页面、窄屏和退出撤销。结束时关闭 API 并删除临时账号，不启动新解读。报告明确记录任务当时是否已有发布结果，截图保存到该 runtime 的 `test-runs/browser-live/`。

## 重复比较

`tests/evaluation-papers.json` 包含 BitFit、Task Arithmetic、SWE-bench，分别侧重方法/消融、模型合并边界、评测设计。默认每篇每个 skill 重复三次，总计 18 个任务。先查看任务清单：

```bash
.venv/bin/python tests/evaluate_matrix.py \
  --runtime ../paper-reader-runtime/test-runs/matrix-01 \
  --model qwen/qwen3-vl-235b-a22b-instruct --vision
```

添加 `--execute` 才会实际调用模型；数据库通过 `PAPER_READER_EVAL_DATABASE_URL` 提供。各任务按顺序执行，使用独立 runtime 和团队，保留 `matrix-results.json` 与逐项日志；不清空数据库。

可用 `--papers` 指定其他论文清单，`--skills` 指定一个 skill，`--repetitions` 调整重复次数。应先完成单篇调试，再比较相同模型、同一模板与论文版本；中途更改兼容层的运行结果不能混为同一组稳定性统计。

自动汇总只统计实际成功/部分/失败、长度波动、证据数量与用量。它不把“字数少”“引用多”直接变成质量分数，也不生成虚假的人工评分。

## 人工判读记录

每次以精华版为入口，在两分钟左右尝试复述下表内容；随后回查全文和证据。每项记录具体缺失/错误与原文位置，评分可用 0（缺失或错误）、1（部分）、2（准确清楚）。

| 维度 | 核对内容 |
|---|---|
| 贡献 | 能否说明问题、最相关已有工作、实际增量，避免仅重复摘要 |
| 成果 | 核心数字是否正确；比较对象、分母、数据规模与预算是否保留 |
| 方法 | 是否能从输入、操作步骤、输出解释机制，术语能否落到具体动作 |
| 经验 | 是否包含条件 → 做法 → 效果 → 边界；负面结论是否过度泛化 |
| 来源 | 关键结论是否能定位到原文；推断、外部证据与作者主张是否分开 |
| 跨论文 | 是否比较相同设置，是否说明全文覆盖与相互冲突的证据 |
| 密度 | 哪些句子可删除而不损失判断依据；是否存在堆叠背景或重复结论 |
| 稳定性 | 同组多次结果有无关键数字、方法步骤、边界条件的互相矛盾 |

不能只报告平均分。同时记录最差样本、关键事实错误、失败率、耗时与 tokens。比较 skill 时保持执行模式一致；guided 比较阅读指导产生的事实与最终短文，native 还需要检查原生流程是否成功。某个 skill 原生输出更完整，不必然意味着最终短文更适合你的阅读目的。

本次实测和未完成项目统一列在 [validation.md](validation.md)。
