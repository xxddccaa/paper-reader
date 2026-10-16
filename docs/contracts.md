# Shared implementation contract

Private paper follow-up conversations are specified in [paper-chat-contract.md](paper-chat-contract.md).
They reuse durable jobs with `kind=chat`, but are visible only to their owner and
never appear in shared reading/job APIs or create reading revisions. Published
source files and source metadata are stored once by content hash in the private
conversation directory; answer messages reference them. Historical answer
context includes per-turn citation locators, and long history is compressed for
both context size and the Pi runner's 400-message boundary.

Workflow v2 is specified in [workflow-v2.md](workflow-v2.md). Its ownership map,
mode defaults, phase protocol, checkpoint rules and publication gates supersede
the monolithic workflow below. New readings use `guided`; legacy readings and
snapshots retain `native`. Native-only execution requirements below apply to
`native` mode, while authentication, isolation, source protection and resource
limits apply to both modes.

## Ownership

| Outcome | Owner | Exclusive files | Prerequisites | Acceptance |
|---|---|---|---|---|
| Auth, workspace resources, migrations | backend agent | backend except app/worker.py, app/pipeline.py, app/source_tools.py, tests/test_worker* and tests/test_source* | this contract | real PostgreSQL auth/isolation/API tests |
| Durable jobs and document tools | pipeline agent | backend/app/worker.py, pipeline.py, source_tools.py; backend/tests/test_worker*, test_source* | DB model contract below | leases, fencing, cancellation, import/SSRF, revision tests |
| Pi runner and upstream skills | runner agent | agent-runner/**, skills/** | JSONL below | compile and offline tool/protocol tests, pinned source |
| MemBench UI | frontend agent | web/** | HTTP below | typecheck/build/browser tests |
| Runtime operations | operations agent | deploy/**, scripts/** | config and CLI below | init, Compose validation, backup isolation |
| Contracts, integration, docs, independent review | lead | docs/**, README.md, AGENTS.md, .gitignore, tests/** | all handoffs | integration and review |

No cross-owner edits without a handoff. Default implementation Python 3.11, Node 22; PostgreSQL 16+ portable SQL, deployment pinned PostgreSQL 18. Frontend follows reference React 18/Ant Design 5. No SQLite production fallback.

## Configuration / Python interfaces

backend/app/config.py exports get_settings() -> Settings with runtime_dir: Path (sibling default), database_url: str (required PAPER_READER_DATABASE_URL), encryption_key: str (required PAPER_READER_ENCRYPTION_KEY, Fernet key), project_dir: Path, allow_private_models: bool (PAPER_READER_ALLOW_PRIVATE_MODELS default false), lease_seconds: int (60), job_timeout_seconds: int (1200), max_tool_calls: int (160, bounded 10–500, PAPER_READER_MAX_TOOL_CALLS), max_attempts: int (2), max_running_jobs: int (2), max_jobs_per_workspace: int (1), secure_cookie: bool. Environment config is loaded explicitly from <runtime>/config/app.env if present, with actual environment precedence. Logs redact secrets.

backend/app/db.py exports Base, engine() and SessionLocal() returning a new SQLAlchemy Session (expire_on_commit=False). backend/app/models.py exports below SQLAlchemy models. Every id is a UUID string (36). Datetimes UTC/timezone aware. JSON columns use generic SQLAlchemy JSON with PostgreSQL semantics. now() is UTC helper, new_id() UUID helper. Defaults at Python level.

- User: id, username, password_hash, is_admin(bool), active(bool), created_at.
- SessionRecord: id (hashed session token), user_id, expires_at, created_at.
- Workspace: id, name, created_at.
- Membership: id, workspace_id, user_id, role ('admin'|'member'); unique(workspace_id,user_id).
- ModelConnection: id, workspace_id, name, base_url, model, protocol ('openai-completions'|'anthropic-messages'), thinking_format ('default'|'qwen-chat-template', default 'default'), encrypted_api_key, vision(bool), context_window(int default128000), max_tokens(int default8192), created_at. The Qwen format is OpenAI-only and explicitly maps disabled thinking to chat_template_kwargs.enable_thinking=false; never infer it from model names or URLs. Include it in public model output and frozen job model snapshots. Legacy snapshots without it mean 'default'.
- Template: id, workspace_id(nullable only for builtin), owner_id(nullable builtin), name, is_builtin(bool), current_version_id(nullable), created_at.
- TemplateVersion: id, template_id, number(int), brief_markdown, full_markdown, instructions, created_at.
- Paper: id, workspace_id, created_by, title, source_type ('pdf'|'arxiv'), source_url(nullable), storage_path(nullable relative runtime path), sha256(nullable), metadata_json(dict including source_mime_type after source persistence and actual coverage), created_at.
- Reading: id, workspace_id, paper_id, created_by, skill_id, model_connection_id, template_id, current_revision_id(nullable), created_at.
- Revision: id, reading_id, workspace_id, number(int), parent_revision_id(nullable), instruction(str), brief_markdown, full_markdown, evidence_index(list), verification(dict), provenance(dict), artifacts(list of {name,path,mime_type}), created_at. Revisions immutable, unique(reading_id,number).
- Job: id, workspace_id, reading_id, created_by, kind ('read'|'revise'), status ('queued'|'running'|'succeeded'|'partial'|'failed'|'cancelled'), stage, progress(int), attempt(int default0), lease_token(nullable str), lease_expires_at(nullable), cancel_requested(bool), input_snapshot(dict), error(nullable safe str), usage(dict), created_at, updated_at. Snapshot: template {id,version_id,brief_markdown,full_markdown,instructions}, model {id,name,base_url,model,protocol,vision,context_window,max_tokens} (NO key), skill_id, paper_id, instruction, base_revision_id. Secret fetched only when running, never persisted in snapshot.
- JobEvent: id (autoincrement integer), job_id, workspace_id, event_type, data(dict), created_at.

backend/app/security.py exports encrypt_secret(str)->str, decrypt_secret(str)->str, hash_password(str)->str, verify_password(str,str)->bool. Backend app CLI supports `python -m app.cli init-admin --username X` (password via PAPER_READER_ADMIN_PASSWORD or getpass); `python -m app.cli export-openapi --output PATH`. Alembic from backend root. Builtin template created idempotently on init-admin. No built-in credentials.

## HTTP API

All paths start /api/v1, all JSON snake_case. Errors HTTPException detail string (422 standard validation detail allowed). Session cookie same-origin; enforce Origin/CSRF for state changes, safe login. Browser fetch credentials same-origin.

- POST /auth/login {username,password} -> User {id,username,is_admin}; POST /auth/logout -> {ok:true}; GET /auth/me -> User.
- GET /workspaces -> [{id,name,role}]; POST /workspaces {name} -> workspace (platform admin only).
- GET /admin/users -> users; POST /admin/users {username,password,is_admin:false} -> user (platform admin).
- GET /admin/network and PUT /admin/network {proxy_enabled,proxy_url,timeout_seconds} -> SourceNetwork (platform admin only). HTTP CONNECT proxies are supported; proxy credentials, URL paths, queries and fragments are rejected. The timeout defaults to 120 seconds and is bounded to 10–300. POST /admin/network/test accepts the same proposed settings and tests a fixed arXiv page without saving. Settings are atomically persisted under runtime/storage/config/source-network.json (0600), shared by API and workers and reloaded at each source download. They apply to public paper downloads/search/figures/title metadata, not model transport. A configured proxy never falls back to direct access; source URL/DNS validation applies before every redirect, and CONNECT uses the validated destination IP with the original Host and TLS SNI.
- GET /workspaces/{wid}/members -> [{user_id,username,role}]; POST same {user_id,role}; DELETE /.../members/{user_id}; admin only; preserve last team admin.
- GET /skills -> [{id,name,description,version,available,unavailable_reason?}], from skills/registry.json. DeepPaperNote default; ljg-paper alternative. Catalog only, no uploads.
- GET /workspaces/{wid}/models -> list; POST same {name,base_url,model,api_key,protocol?,vision?,context_window?,max_tokens?} -> safe model with has_api_key; PATCH /.../models/{id} same optional fields, blank api_key preserves; POST /.../models/{id}/test -> {ok,message}; model routes admin except list.
- GET /workspaces/{wid}/templates -> list of templates including builtin, with {id,name,is_builtin,owner_id,current_version:{id,number,brief_markdown,full_markdown,instructions}}; POST same {name,brief_markdown,full_markdown,instructions} creates v1; PUT /.../templates/{id} same creates immutable new version; GET /.../templates/{id}/versions -> versions. Builtin cannot edit; members only own, admins all team.
- GET /workspaces/{wid}/papers?search= -> list (bounded limit/offset optional); POST same {arxiv:string,title?:string} -> Paper; POST /.../papers/upload multipart `file` PDF -> Paper; GET /.../papers/{pid} -> Paper; GET /.../papers/{pid}/file -> authorized source file (404 before download).
- Paper title enrichment runs independently in the worker, including legacy papers. `title` holds the original title (or an explicit import override); `metadata_json.title_zh` holds the Simplified Chinese translation, empty for Chinese originals. `title_status` is pending/processing/ready/waiting_model/failed, with a safe `title_error`. arXiv bibliographic metadata is authoritative; PDF titles are extracted from front matter using the workspace's earliest configured model. The same model translates English titles. Missing models resume automatically after configuration. POST /.../papers/{pid}/title -> 202 Paper queues explicit re-extraction from the source, clearing any manual override; active requests are idempotent. Search matches original and Chinese titles. Claims use a five-minute recoverable lease and fenced writes, preserving unrelated source metadata and existing titles on failure. No reading or revision is created by enrichment.
- POST /workspaces/{wid}/readings {paper_id,skill_id,model_connection_id,template_id} -> {reading_id,job_id}; GET /.../readings -> list with {id,paper_id,paper_title,skill_id,created_at,current_revision_id,latest_job}; GET /.../readings/{rid} -> {id,paper_id,paper_title,skill_id,current_revision_id,revisions:[Revision],jobs:[Job]}.
- POST /.../readings/{rid}/revisions {instruction,base_revision_id} -> {job_id}; reject stale base or existing queued/running job (409). GET /.../readings/{rid}/export?revision_id=&view=brief|full -> Markdown attachment.
- GET /workspaces/{wid}/jobs -> list; GET /.../jobs/{jid} -> Job; POST /.../jobs/{jid}/cancel -> Job; POST /.../jobs/{jid}/retry -> {job_id} new job preserving paper/skill/template/instruction snapshot but refreshing the public model snapshot from the current connection; check no active writer/stale revision. A queued job whose model configuration has changed is rejected before decrypting the current key.
- GET /.../jobs/{jid}/events -> SSE with replay Last-Event-ID or ?after=; events 'progress'/'completed'/'failed'/'cancelled'/'message'; event data JSON. Every query scoped to workspace and live membership.
- GET /workspaces/{wid}/artifacts/{revision_id}/{index} -> authorized attachment from revision.artifacts, runtime path containment enforced.

## Runner JSONL protocol

Worker launches node agent-runner/dist/index.js with isolated cwd under runtime/tmp/{job_id}/{attempt}. Python worker owns subprocess and JSONL RPC. First stdin message:
{type:'run',run_id,kind,skill_id,skill_root:absolute,work_dir:absolute,model:{...snapshot,api_key},paper:{id,title,source_type,source_url,storage_path:absolute|null},template:{...},instruction,previous_revision:object|null,limits:{timeout_seconds,max_tool_calls:160,max_related_papers:3}}.
The illustrated tool budget is the default; the worker passes the configured integer (10–500) into this per-attempt specification, and both runner and Pipeline enforce that same value.
API key ONLY on stdin/in-memory; never echo spec or persist it. The runtime model also receives deployment-owned allow_private_models (default false). Runner is built from fixed installed SDK; no ambient credentials or extensions. Actual model requests use connection-time validated DNS answers, constrain origin/base path and reject redirects; the API probe likewise dials validated addresses directly.

The model snapshot includes thinking_format. For explicit qwen-chat-template, Pi uses its Qwen thinking protocol with thinking disabled; the probe likewise sends enable_thinking=false. Default and Anthropic requests omit Qwen-specific fields. A model length stop persists usage, terminates the session and reports output truncation; truncated tool arguments are never executed.

Runner stdout: {type:'progress',stage,message,progress?}; {type:'tool_request',id,name,args}; {type:'usage',usage:{input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,total_tokens}}; {type:'result',result:{brief_markdown,full_markdown,evidence_index,verification,artifacts,provenance}}; {type:'error',message}. Usage events are per-response deltas; worker adds and persists them immediately into job lifetime totals and an attempts map, including failures/recovery. Diagnostics stderr redacted. Worker responds {type:'tool_response',id,result} or {type:'tool_response',id,error}; cancellation {type:'cancel'} then process-group termination grace timeout. Result artifact paths relative to work_dir, copied by worker into tenant-owned reading storage after validation.

Tool RPC names supported by Python pipeline:
- prepare_source {} -> {paper_id,title,source_manifest,sections:[{id,text,page?,section?,source_id}],files:[{name,path}],coverage}. DeepPaperNote gets verified PDF; ljg gets HTML when available then PDF. All full text retained on disk; sections list may be indexed/limited but read_source retrieves remaining content.
- read_source {source_id?:string,section_id?:string,page?:number,offset?:number,limit?:number} -> source text and identity (bounded chunks, full corpus accessible).
- search_papers {query} -> candidates with id/title/url/abstract/source.
- fetch_related {id,url?} -> verified full text/source metadata, enforce max3 distinct related sources and public URL policy.
- read_skill_file {path} -> content under pinned skill root only.
- read_native_contract {} -> the exact DeepPaperNote bundle writing contract and coverage projection without bulky image metadata; does not invent or weaken upstream required fields.
- run_skill_step {script,args:[string]} -> {stdout,stderr,exit_code,files}; allowlist only bundled scripts under selected skill, args validated, outputs inside work_dir, credentials/environment filtered, network acquisition mediated (use already downloaded verified PDF; no arbitrary URL or local paths).
- list_artifacts {} -> files in work_dir; read_artifact {path,offset?,limit?}; write_artifact {path,content} -> saved relative path (text/JSON only, size bound). JSON syntax is validated before atomic replacement; malformed writes preserve the previous artifact. Image reads require a configured vision model and return real image content; host records image SHA-256. Source files/corpus, check reports and host ledgers cannot be written through this tool. Once sealed, product writes are limited to product/.

Internal runner-to-worker RPCs are never registered as model tools: host_native_attest binds actual independent review results to the exact note/map SHA-256; host_native_seal validates semantic script results and their exact input/output chain, independently reviewed files and observed images before freezing artifacts; host_native_status verifies any existing seal and returns recovery metadata. Publication revalidates the seal as a hard gate. A recovered initial read with a valid seal skips native execution and continues product generation. Revisions likewise reuse prior sealed artifacts without claiming native re-execution. Paths in checkpoint fingerprints normalize the work-directory prefix to allow relocation without accepting content changes.

Evidence item minimum {id,source_id,source_title,source_url?,quote,section?,page?,kind:'paper'|'external'|'inference',verified:boolean}. Agent-provided verified is NOT trusted: worker validates quoted evidence against source corpus before publication; unresolved citations flagged. Product citation Markdown `[E1](evidence:E1)` or `[E1]` (UI maps known IDs). Product outputs include citations. Native skill artifacts and checks not conflated with product verification.

Runner must actually read/run upstream guidance and required scripts, retain native note/checks, then generate product brief/full from evidence+native artifacts. Use structured submission tool for final result. No fake demo results or hard-coded completed checks. Revisions reuse evidence/native material; fact updates reread source. External failures recorded as limited coverage. Skill own mandatory failures stop native completion. Platform final check separate; one targeted product repair. Worker validates minimal result, quote/citation consistency, file paths and lease fencing; incomplete returns partial with explicit verification errors, never manufactured success.

## Operations and tests

Manage entry scripts/manage.py: init, up, down, status, logs, migrate, prepare-models, init-admin, backup, restore, test. Generated config mode0600, no fixed passwords. PostgreSQL data explicit runtime bind (PG18 official volume layout). No Docker daemon available here initially; deliver Compose and use local PostgreSQL16 for actual integration checks. Backups require matched DB dump/storage/config; restore only to fresh explicitly named target. Scripts resolve roots via __file__, never cwd. No deleting existing runtime on init/down. Published successful/partial task copies are removed from tmp; failed/cancelled diagnostics retain at most two attempts, seven days and bounded bytes, without deleting active/retry checkpoints. Tests require an explicit isolated _test database and put pytest basetemp under the dedicated test runtime.

Test model uses explicit protocol fixture only in test harness; production never silently falls back to fixture. Tests assert real isolation and state transitions. frontend can use browser mocked APIs for UI but report separate from real integration. Dependencies/versions fixed after installation. Full audit and repeated actual model quality runs reported honestly if credentials/services unavailable.
