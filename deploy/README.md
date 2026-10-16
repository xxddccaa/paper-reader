# 运行与数据管理

工程目录中的 `scripts/manage.py` 是统一操作入口，使用 Python 3.11+ 标准库。生产部署需要 Linux、Docker Engine 和 Docker Compose v2 插件。应用镜像包含 Python 3.11、Node 22.19、构建后的前端与 Pi runner；数据库固定为 PostgreSQL 18，SQL 保持 PostgreSQL 16+ 兼容。

## 初始化与启动

在工程目录运行；也可从任意目录使用脚本绝对路径：

```bash
python scripts/manage.py init
python scripts/manage.py prepare-models --build
python scripts/manage.py up
python scripts/manage.py init-admin --username admin
python scripts/manage.py status
```

`prepare-models --build` 构建镜像并在部署阶段下载 Docling 的本地解析模型，缓存放入 `runtime/cache/docling/models/`，下载成功才启用 Docling。该步骤需要访问模型下载源和足够磁盘空间。若暂时省略，`up --build` 仍可启动；解析使用 PyMuPDF 并在结果中注明解析限制，不会在首次阅读时隐式下载 Docling 模型。缓存未纳入备份，恢复后应重新执行 `prepare-models`。

`init-admin` 通过后台 CLI 的安全交互提示读取密码，不提供默认密码，也不把密码放入命令行参数。应用默认通过 `http://127.0.0.1:8000` 提供前端、API 和 SSE；可使用 SSH 本地转发访问远程服务器。

`init` 根据脚本位置解析相邻的 `paper-reader-runtime/`，创建数据库、论文存储、日志、缓存、临时文件、备份和测试目录，生成随机数据库密码与 Fernet 加密密钥。再次运行不会覆盖配置或删除已有数据。实际配置在 `runtime/config/app.env`，权限为 `0600`；`deploy/app.env.example` 仅解释变量，不是可直接使用的凭据。

覆盖运行目录必须使用绝对路径，既可设置 `PAPER_READER_RUNTIME_DIR`，也可传入 `--runtime`：

```bash
python scripts/manage.py --runtime /srv/paper-reader-runtime init
python scripts/manage.py --runtime /srv/paper-reader-runtime up --build
```

脚本按 runtime 的绝对路径生成唯一 Compose 项目名，多个 runtime 不共享容器或网络。实际服务配置来自该 runtime 的 `config/app.env`，管理脚本不会使用外部 shell 中残留的数据库或模型配置覆盖它；`PAPER_READER_RUNTIME_DIR` 本身用于定位该文件。直接运行后端 Python 时，仍遵循后端的环境变量优先规则。

`up` 先启动健康的 PostgreSQL，协调暂停已有写入服务后执行 Alembic 迁移，再启动 API 与常驻 worker。API 和 worker 在容器中使用非 root 用户；root 执行 `init` 时数据目录分配给 UID/GID 10001，否则采用当前账号 UID/GID。容器只挂载应用所需的 storage、logs、cache、tmp，不直接挂载 PostgreSQL 数据和宿主配置目录。PostgreSQL 数据显式绑定 `runtime/postgres/` 到容器的 `/var/lib/postgresql`，实际 `PGDATA=/var/lib/postgresql/18/docker`，符合 PostgreSQL 18 官方镜像布局。

## 配置与日常管理

平台管理员可在网页「平台管理 → 网络代理」设置论文下载代理，例如 `http://127.0.0.1:10826`，并点击「测试 arXiv 连接」。这里只支持无需认证的 HTTP CONNECT 代理，可访问 HTTP 和 HTTPS 论文链接。地址中的 `127.0.0.1` 指应用所在的服务器或容器；容器部署应填写容器可访问的代理地址。配置覆盖原文、图片、检索和标题获取，模型连接继续使用各自配置。

代理设置保存至 `runtime/storage/config/source-network.json`，权限 0600，API 与 worker 共享，下一次下载自动读取，无需重启。可配置下载总超时（10–300 秒，默认 120 秒）；特定短请求仍保留更小的显式时间限制。代理失败时会报告错误，不自动绕过代理。关闭代理开关并保存即可恢复直连。此前失败的任务在解读页面点击「重试」后使用新设置。该文件随 storage 一起备份和恢复。

在 `config/app.env` 中设置以下变量，更新后重新运行 `up`：

| 变量 | 用途与默认值 |
|---|---|
| `PAPER_READER_DATABASE_URL` | 自动生成的容器网络数据库连接，host 为 `postgres`。 |
| `PAPER_READER_ENCRYPTION_KEY` | 自动生成的 Fernet 密钥；恢复已有模型配置时必须保留。 |
| `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` | 数据库初始化配置。已存在数据库的密码变更需要同时在数据库执行相应修改。 |
| `PAPER_READER_BIND_HOST`、`PAPER_READER_PORT` | 发布地址，默认 `127.0.0.1:8000`。 |
| `PAPER_READER_PUBLIC_ORIGIN` | 可选：反向代理后的完整 origin，例如 `https://papers.example.org`，用于状态变更来源校验。 |
| `PAPER_READER_SECURE_COOKIE` | 默认 `false`；HTTPS 部署设置为 `true`。 |
| `PAPER_READER_ALLOW_PRIVATE_MODELS` | 默认 `false`；团队使用内网模型网关时由部署管理员显式启用。 |
| `PAPER_READER_LEASE_SECONDS` | worker 租约，默认 60 秒。 |
| `PAPER_READER_JOB_TIMEOUT_SECONDS` | 单次任务上限，默认 1200 秒。 |
| `PAPER_READER_MAX_ATTEMPTS` | 任务最大尝试次数，默认 2。 |
| `PAPER_READER_MAX_TOOL_CALLS` | 每次任务尝试的工具调用总预算，包含原生 skill、复核和产品输出阶段；默认 160，允许 10–500。任务时间上限仍独立生效。 |
| `PAPER_READER_MAX_RUNNING_JOBS`、`PAPER_READER_MAX_JOBS_PER_WORKSPACE` | 总并发和单团队并发，默认 2、1。 |
| `PAPER_READER_SESSION_HOURS`、`PAPER_READER_UPLOAD_MAX_BYTES` | 会话和上传限制，默认 24 小时、50 MiB。 |
| `PAPER_READER_DOCLING_ENABLED`、`PAPER_READER_DOCLING_ARTIFACTS_PATH` | 默认关闭；`prepare-models` 下载成功后启用并指向容器内持久化模型路径。 |
| `PAPER_READER_DOCLING_OCR_ENABLED` | 默认 `0`；仅在额外准备好所需 OCR 模型后设为 `1`，否则使用版面解析并明确报告图片页面的覆盖限制。 |
| `PAPER_READER_INSTALL_DOCLING` | 镜像是否安装 Docling，默认 `true`；变更后重新构建。 |
| `PAPER_READER_UID`、`PAPER_READER_GID` | 容器应用用户，与运行数据目录权限配套。 |

使用反向代理时保留 Host/Origin，配置 SSE 不缓存，并设置合理的连接超时。平台 API Key 在团队设置页面写入并加密保存在数据库中，不写入镜像或源码。

```bash
python scripts/manage.py logs worker
python scripts/manage.py logs api --follow
python scripts/manage.py migrate
python scripts/manage.py down
```

`down` 停止并移除本项目容器，不带卷删除参数，不清理数据库、论文或配置。部署时不会自动执行破坏性清理。Docker 日志每份 10 MiB、最多保留五份。成功发布后清理任务临时目录，正式产物保存在 reading storage；失败或取消任务最多保留两次尝试的临时诊断、最长七天，清理跳过排队和执行中的恢复任务。该保留策略暂为固定默认值。运营者需监控磁盘容量。

不要通过 `docker compose config` 将展开的配置发布到工单或日志：该输出包含密码。管理脚本不会输出密钥或展开的数据库 URL。

## 一致性备份与隔离恢复

```bash
python scripts/manage.py backup
python scripts/manage.py restore /absolute/path/backup.tar.gz --target /srv/paper-reader-restore-check
```

备份期间暂时停止该 runtime 正在运行的 API 和 worker，确认写入服务已经停止后，通过数据库容器内的 `pg_dump --format=custom` 保存逻辑备份，再复制匹配的 storage 与 config；结束或失败后恢复原先运行的写入服务。这会产生短暂服务中断，正在执行的任务按照 worker 的租约和重试策略恢复。数据库必须已运行。不要绕过管理入口另起写入进程；它们无法参与上述协调。

备份包含数据库、原始论文、已保存结果、原生 skill 产物和配置；不包含 PostgreSQL 原始数据目录、可重建模型缓存、临时任务目录或日志。归档有文件校验清单，权限为 `0600`，包含恢复模型密钥所需的机密配置。备份目录默认位于同一 runtime；应另行把备份保存到受保护的独立存储，以应对宿主磁盘损坏。

恢复必须指定一个不存在的绝对目标目录，不能是当前 runtime 或其父子目录。脚本先拒绝路径穿越、链接、异常文件及校验失败，再创建目标目录，通过独立 Compose 项目启动一份新 PostgreSQL 并导入逻辑备份。恢复后目标服务全部停止，不触碰当前运行实例。修改目标的端口、origin 等配置后，才能通过该目标的 `up` 验证。恢复失败时保留目标目录供排查，不会自动删除数据；再次恢复使用另一个新目录。

备份以同版本 PostgreSQL 18 镜像恢复。跨数据库主版本升级和应用回滚需单独验证迁移兼容性；不要把生产数据库原始文件挂入不同主版本镜像。

## 验证

无需 Docker、数据库或模型服务即可运行管理脚本测试：

```bash
python scripts/manage.py test --operations-only
```

完整本地验证要求先安装工程依赖，并显式提供独立 PostgreSQL 测试数据库：

```bash
export PAPER_READER_TEST_DATABASE_URL='postgresql+psycopg://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/paper_reader_test'
python scripts/manage.py test
```

测试数据库名必须以 `_test` 结尾，且不能与正式数据库名相同。管理脚本在 `runtime/test-runs/<uuid>/` 生成独立配置和存储，pytest 临时产物通过 `--basetemp` 定位到该目录的 `test-runs/pytest/`。完整验证执行后端 pytest、前端和 runner 类型检查及构建、runner 单元测试；浏览器测试另由 `web/` 下的 `npm run test:e2e` 执行。项目局部 Python 环境依次采用 `.venv/bin/python`、`backend/.venv/bin/python`，均不存在时使用调用脚本的 Python。测试命令不会自动安装依赖。

当前实现环境已运行隔离管理测试，并解析检查 Compose YAML；Docker Compose 插件与 daemon 不可用，因此容器构建、真实容器启动、容器内数据库备份恢复仍需在目标 Docker 主机验证。这些检查不能用静态 YAML 验证代替。
