# Paper Reader

面向团队的论文解读工作台。上传 PDF 或输入 arXiv 地址，选择阅读 skill、团队模型和解读模板，生成带原文证据的中文精华版与完整解读；通过自然语言修改要求生成新版本，保留旧结果。

内置模板围绕五个问题组织内容：论文做了什么、相对相关工作推进了什么、取得什么结果、方法怎样运行、在什么条件下哪些做法有效或无效。精华版目标约 600–1000 字；经验必须说明条件与边界。外部论文最多读取三篇，未获取全文、未核验图表和系统推断均应明确标注。

技术选型与界面参考相邻的 MemBench 工程：React 18、TypeScript、Vite、Ant Design 5；FastAPI、SQLAlchemy、PostgreSQL、Alembic。参考工程保持原样。

## 开始使用

部署入口需要 Linux、Docker Engine 和 Docker Compose v2。以下命令在 `paper-reader/` 执行，管理脚本需要 Python 3.11+：

```bash
python3 scripts/manage.py init
python3 scripts/manage.py up --build
python3 scripts/manage.py init-admin --username admin
```

访问 `http://127.0.0.1:8000`，使用初始化时设置的密码登录。没有默认密码。先在平台管理中新建工作空间和团队账号，再在团队设置中添加模型连接并测试工具调用，最后导入论文并创建解读。

模型连接支持 OpenAI Chat Completions 兼容接口和 Anthropic Messages 兼容接口。模型必须可靠支持工具调用；原生模式下 DeepPaperNote 的图表检查还需要选择支持图片输入的模型并启用视觉能力。API Key 由工作空间管理员填写，加密存入数据库，页面不回显。

自建 Qwen/vLLM 服务可能默认开启思考；在模型设置的“推理模式适配”中选择“Qwen/vLLM：关闭思考”，使 API 探测与 Pi runner 明确发送 `chat_template_kwargs.enable_thinking=false`。普通接口保持“标准接口”。选择内网模型前，部署配置需开启 `PAPER_READER_ALLOW_PRIVATE_MODELS`；无需鉴权、忽略 Authorization 的本地服务可填写非秘密占位值作为 SDK Key。已有数据库需执行迁移以增加该连接选项。

可选的 Docling 解析模型在部署时预下载：

```bash
python3 scripts/manage.py prepare-models --build
python3 scripts/manage.py up
```

未执行此步骤时使用 PyMuPDF，记录实际解析覆盖情况。原文无法充分提取时不会把缺失章节当作完整证据。Docling 不是扫描件 OCR 完整支持的承诺。详细配置、备份恢复和服务管理见 [部署说明](deploy/README.md)。

## 目录与运行数据

```text
heikesong_paper_reader/
├── MemBench参考工程/          # 已有参考工程
├── paper-reader/             # 本项目源码
│   ├── backend/              # API、权限、数据模型、worker、来源与证据检查
│   ├── agent-runner/         # 单任务 Node/Pi 进程与 skill 兼容层
│   ├── skills/               # 固定版本的上游 skill、许可证与校验清单
│   ├── web/                  # React 工作台
│   ├── deploy/               # Dockerfile、Compose、部署说明
│   ├── scripts/              # 启动、迁移、备份、恢复、测试
│   ├── tests/                # 显式启用的真实模型评估
│   └── docs/                 # 架构、接口与验证记录
└── paper-reader-runtime/     # 持久化运行数据，不属于源码
    ├── config/              # app.env 与加密密钥，0600
    ├── postgres/            # PostgreSQL 数据
    ├── storage/workspaces/  # 团队原文、结果版本、原生 skill 产物
    ├── logs/                # 服务日志
    ├── cache/               # 可重建的解析模型与工具缓存
    ├── tmp/                 # 活跃任务与重试工作目录
    ├── backups/             # 匹配的数据库、存储与配置备份
    └── test-runs/           # 隔离验证数据和报告
```

管理命令按脚本位置定位目录，不依赖调用者当前目录。使用 `--runtime /absolute/path` 或 `PAPER_READER_RUNTIME_DIR` 可以为另一个实例指定独立数据目录。`down` 保留数据，恢复只允许写入新目录。

## 谁维持论文任务

```mermaid
flowchart LR
    Browser[React 工作台] --> API[FastAPI]
    API --> PG[(PostgreSQL\n任务、租约、版本、事件)]
    Worker[常驻 Python worker] --> PG
    Worker --> Workflow[Python 分阶段编排与检查点]
    Workflow --> Runner[每阶段独立 Node/Pi runner]
    Runner --> Model[团队配置的模型服务]
    Runner -->|受控工具 RPC| Worker
    Worker --> Storage[(团队原文、证据、原生笔记)]
    Docker[Docker restart policy] -.进程监督.-> API
    Docker -.进程监督.-> Worker
```

**Worker 是常驻执行服务**，负责领取任务、续租、超时、取消、恢复与结果落盘。默认的分阶段模式由 Python 决定执行顺序，每个模型阶段启动独立的 **Node/Pi runner**，避免持续累积整篇原文和工具历史。原生模式仍以一个 runner 执行完整 skill 流程。**Runtime 是整套运行环境与持久化数据**。关闭浏览器不会终止任务；worker 意外退出后，过期租约会使任务重新排队，在尝试次数内复用通过校验的阶段检查点。恢复不等于从模型生成到一半的 token 精确续接。

初版使用 PostgreSQL 任务队列，没有额外的 Redis/Celery 服务。并发受总量与工作空间配额约束，结果发布使用租约 fencing 和版本锁，避免旧 worker 覆盖新结果。具体边界见 [架构说明](docs/architecture.md)。

## Agent、skill 与模板分别负责什么

| 层 | 本版实现 | 作用 |
|---|---|---|
| Agent engine | Pi SDK 1.1.0 | 运行模型与工具循环；当前未实现其他 engine |
| 模型连接 | 团队配置的 API | 决定推理、工具调用和视觉能力 |
| Reading skill | DeepPaperNote / ljg-paper | 提供阅读指导；原生模式执行其完整笔记与检查流程 |
| 兼容层 | Python workflow + runner + document tools | 控制阶段、证据核验、局部修补与复用；保留原生模式 |
| 产品模板 | 独立精华版/完整版 Markdown | 决定用户最终读到的结构、重点、语言与长度 |

DeepPaperNote 默认入选，ljg-paper 可切换。两者固定上游 commit，保留来源许可证与文件校验清单；不会在任务中静默更新。可用状态表示适配器已打包，不表示所有模型或论文组合都已通过质量评测。上游链接和服务端适配差异见 [skills/ADAPTATIONS.md](skills/ADAPTATIONS.md)。

创建任务时可以选择两种执行模式：

- **分阶段解读（guided，默认）**：遵循选定 skill 的阅读指导，由程序依次执行来源解析、结构化事实提取、证据与数字关系检查、独立事实复核、模板成文、独立成品复核。结构化事实保存实验条件、原文摘录、观察与推断的区别；成文阶段主要读取已核验事实，减少无关上下文。必要时进行有限的局部修补，未解决的事实问题不会标记为成功。这种模式明确标注没有执行完整原生 skill 流程。
- **原生 skill（native）**：执行上游笔记、脚本及独立复核，再生成产品结果；原有质量门槛继续生效。旧任务和迁移前的数据保留此模式。

用户可以调整模板，也可以提出修订要求，形成不可变的新版本。仅修改表达与结构时，兼容的已核验事实可以复用；新增事实、比较或解释需要重新核验。阶段状态、复用来源与证据会在页面显示。详细协议见 [分阶段架构](docs/workflow-v2.md)。已有数据库需要执行 `python3 scripts/manage.py migrate`，增加模式、阶段记录和结构化事实字段。

新建 guided 任务使用 [结构化来源与长文流程](docs/workflow-v3.md)：两个 skill 的 arXiv 输入统一优先经身份、版本及正文结构验证的 HTML，必要时回退同版本 PDF；用户上传的 PDF 保留原始文件。解析保留章节、附录、公式 LaTeX、表格合并单元格与多级表头、图注和引用锚点。原始文件与图像均按哈希保留；文本摘录、表格单元格、图像观察分别展示核验方式。

正文和附录超过 64 KiB，或完整阶段输入超出安全预算时，按原文段落分段提取和复核。常规分段为 16 KiB、硬上限为 32 KiB，最多同时执行两个分段；每个模型阶段最多 5 分钟，长文任务默认 40 分钟、共享 240 次工具调用。已通过哈希和租约检查的分段可在重试时复用。全文合并保留各段结论与条件，并可回查完整原文。图表需实际读取图片，PDF 缺字页的视觉补读作为独立观察，不会变成“原文引句匹配”。页面展示来源选择、章节覆盖、图表核查与缺口；精华版仍以 600–1000 字为目标。旧版任务与旧版解读的修订保持原有规则。工程检查和真实模型验收的范围分别记录于 [v3 验证记录](docs/workflow-v3-evaluation.md)。

## 本地开发

依赖 Python 3.11+、Node 22.19+、PostgreSQL 16+。使用独立的开发数据库，先设置 `PAPER_READER_DATABASE_URL`、`PAPER_READER_ENCRYPTION_KEY` 和绝对路径 `PAPER_READER_RUNTIME_DIR`。加密密钥使用 `Fernet.generate_key()` 创建并安全保存；不要使用正式数据库进行开发测试。

```bash
python3.11 -m venv .venv
.venv/bin/pip install -c backend/requirements.lock -e 'backend[dev]'
npm --prefix agent-runner ci
npm --prefix agent-runner run build
npm --prefix web ci
npm --prefix web run build
```

在 `backend/` 中初始化并启动 API：

```bash
../.venv/bin/python -m alembic upgrade head
../.venv/bin/python -m app.cli init-admin --username admin
../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另一个终端在相同配置下启动 `../.venv/bin/python -m app.worker`。API 会提供已构建的前端；需要热更新时在 `web/` 执行 `npm run dev`，API 请求代理到本机 8000 端口。

## 验证与质量评估

后端测试必须显式提供独立测试数据库，名称以 `_test` 结尾。测试会重建或清空测试表：

```bash
export PAPER_READER_TEST_DATABASE_URL='postgresql+psycopg://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/paper_reader_test'
python3 scripts/manage.py test
npm --prefix web run test:e2e
```

真实模型测试由 `tests/live_smoke.py` 驱动实际 API、worker、Pi 与模型服务；`tests/evaluate_matrix.py` 提供三篇论文、两个 skill、每组重复三次的比较矩阵。API Key 从环境读取，生产逻辑没有测试数据回退。执行示例和人工评价标准见 [质量评估说明](docs/evaluation.md)，本次实际检查范围见 [验证记录](docs/validation.md)。

Workflow v2 已通过一次真实模型首次解读及表达修订验收：用户指定的 Qwen3.8-27B、BitFit 原始 PDF、ljg-paper 指导、guided 模式，首次 448.32 秒、280,852 token；修订 120.68 秒、43,470 token。两次均成功发布，保留 2 个不可变版本、11 条结构化结论、14/14 条匹配原文的引文；修订实际复用来源、事实提取与事实复核。真实浏览器也验证了成品、阶段与结构化结论展示。详见 [Workflow v2 实测](docs/workflow-v2-evaluation.md)。逐项回查仍发现任务计数和参数占比措辞不够严谨，一篇论文的成功不能证明普遍稳定性；完整原生 skill 流程的历史失败和单次直读对照保留于 [Qwen 历史实测](docs/qwen38-bitfit-evaluation.md)。

当前宿主没有可用的 Docker Compose 插件和 Docker daemon，因此镜像构建、容器启动和容器备份恢复尚未实测；本地 PostgreSQL、API、任务与浏览器测试分别执行。发布前应在目标 Docker 主机完成这些部署验收。
