import asyncio
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import schemas as S
from .catalog import skill_catalog
from .agent_state import ENGINE, agent_snapshot, ensure_agent_task, grant_continuation
from .config import get_settings
from .db import get_db, SessionLocal
from .dependencies import COOKIE_NAME, current_user, membership, platform_admin, require_origin
from .models import User, SessionRecord, Workspace, Membership, ModelConnection, Template, TemplateVersion, Paper, Reading, Revision, Job, JobEvent, now, new_id
from .model_transport import ModelTransportError, resolve_endpoint, post_model_json
from .security import hash_password, verify_password, encrypt_secret, decrypt_secret, token_hash
from .source_network import SourceNetwork, load_source_network, save_source_network

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_origin)])
ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "partial", "failed", "cancelled", "waiting")

def owned(db, model, ident, wid, *, lock=False):
    stmt = select(model).where(model.id == ident, model.workspace_id == wid)
    if model is Job:
        stmt = stmt.where(Job.kind != "chat", Job.conversation_id.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    value = db.scalar(stmt)
    if not value:
        raise HTTPException(404, "Resource not found")
    return value

def visible_template(db, ident, wid, *, lock=False):
    stmt = select(Template).where(Template.id == ident, or_(Template.workspace_id == wid, Template.is_builtin.is_(True)))
    if lock:
        stmt = stmt.with_for_update()
    value = db.scalar(stmt)
    if not value:
        raise HTTPException(404, "Template not found")
    return value

def template_out(db, template):
    return {"id": template.id, "name": template.name, "owner_id": template.owner_id, "is_builtin": template.is_builtin, "current_version": db.get(TemplateVersion, template.current_version_id)}

def model_out(model):
    fields = ("id", "name", "base_url", "model", "protocol", "thinking_format", "vision", "context_window", "max_tokens")
    return {**{f: getattr(model, f) for f in fields}, "has_api_key": bool(model.encrypted_api_key)}

def source_path(relative):
    root = get_settings().runtime_dir.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "File not found")
    return path

def validate_model_url(value):
    try:
        resolve_endpoint(value, allow_private=get_settings().allow_private_models)
    except ModelTransportError as exc:
        raise HTTPException(422, str(exc)) from None
    return value.rstrip("/")

@router.post("/auth/login", response_model=S.UserOut)
def login(data: S.LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == data.username))
    # Verify a real hash even for absent users to reduce username timing disclosure.
    valid = verify_password(data.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid or not user.active:
        raise HTTPException(401, "Invalid username or password")
    old = request.cookies.get(COOKIE_NAME)
    if old:
        old_session = db.get(SessionRecord, token_hash(old))
        if old_session:
            db.delete(old_session)
    token = secrets.token_urlsafe(48)
    settings = get_settings()
    db.add(SessionRecord(id=token_hash(token), user_id=user.id, expires_at=now() + timedelta(hours=settings.session_hours)))
    db.commit()
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_hours * 3600, httponly=True, secure=settings.secure_cookie, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return user

_DUMMY_HASH = hash_password("unused-session-validation-value")

@router.post("/auth/logout", response_model=S.Ok)
def logout(request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    session = db.get(SessionRecord, token_hash(request.cookies[COOKIE_NAME]))
    if session:
        db.delete(session)
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}

@router.get("/auth/me", response_model=S.UserOut)
def me(user: User = Depends(current_user)):
    return user

@router.get("/workspaces", response_model=list[S.WorkspaceOut])
def workspaces(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(Workspace, Membership.role).join(Membership, Membership.workspace_id == Workspace.id).where(Membership.user_id == user.id).order_by(Workspace.created_at)).all()
    return [{"id": w.id, "name": w.name, "role": role} for w, role in rows]

@router.post("/workspaces", response_model=S.WorkspaceOut, status_code=201)
def create_workspace(data: S.WorkspaceIn, db: Session = Depends(get_db), user: User = Depends(platform_admin)):
    workspace = Workspace(name=data.name.strip())
    db.add(workspace)
    db.flush()
    db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
    db.commit()
    return {"id": workspace.id, "name": workspace.name, "role": "admin"}

@router.get("/admin/users", response_model=list[S.UserOut])
def users(db: Session = Depends(get_db), user: User = Depends(platform_admin)):
    return list(db.scalars(select(User).order_by(User.username)))

@router.get("/admin/network", response_model=SourceNetwork)
def source_network_settings(user: User = Depends(platform_admin)):
    try:
        return load_source_network()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from None

@router.put("/admin/network", response_model=SourceNetwork)
def update_source_network(data: SourceNetwork, user: User = Depends(platform_admin)):
    try:
        return save_source_network(data)
    except OSError:
        raise HTTPException(503, "Network settings could not be saved") from None

@router.post("/admin/network/test", response_model=S.ModelTest)
def test_source_network(data: SourceNetwork, user: User = Depends(platform_admin)):
    from .source_tools import download_public, SourceError
    started = time.monotonic()
    try:
        result = download_public("https://arxiv.org/abs/2106.10199", max_bytes=2 * 1024 * 1024,
                                 timeout=min(data.timeout_seconds, 20), network=data)
        if b"citation_title" not in result.data:
            return {"ok": False, "message": "连接已响应，但未获取到有效的 arXiv 论文页面。"}
        elapsed = round(time.monotonic() - started, 1)
        return {"ok": True, "message": f"{'代理' if data.proxy_enabled else '直连'}访问 arXiv 成功，用时 {elapsed} 秒。"}
    except SourceError:
        return {"ok": False, "message": "无法访问 arXiv，请检查代理地址、端口及服务是否启动。"}

@router.post("/admin/users", response_model=S.UserOut, status_code=201)
def create_user(data: S.UserIn, db: Session = Depends(get_db), user: User = Depends(platform_admin)):
    value = User(username=data.username.strip(), password_hash=hash_password(data.password), is_admin=data.is_admin)
    db.add(value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username already exists")
    return value

@router.get("/workspaces/{wid}/members", response_model=list[S.MemberOut])
def members(wid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user, admin=True)
    return [{"user_id": member.user_id, "username": account.username, "role": member.role} for member, account in db.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(Membership.workspace_id == wid))]

def lock_members(db, wid):
    # Serialize add/remove/role changes on a stable parent row.
    db.scalar(select(Workspace).where(Workspace.id == wid).with_for_update())

def preserve_admin(db, member):
    count = db.scalar(select(func.count()).select_from(Membership).where(Membership.workspace_id == member.workspace_id, Membership.role == "admin"))
    if member.role == "admin" and count <= 1:
        raise HTTPException(409, "Cannot remove the last workspace administrator")

@router.post("/workspaces/{wid}/members", response_model=S.MemberOut)
def set_member(wid: str, data: S.MemberIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_members(db, wid)
    membership(db, wid, user, admin=True)
    account = db.get(User, data.user_id)
    if not account or not account.active:
        raise HTTPException(404, "User not found")
    member = db.scalar(select(Membership).where(Membership.workspace_id == wid, Membership.user_id == account.id))
    if member:
        if member.role == "admin" and data.role != "admin":
            preserve_admin(db, member)
        member.role = data.role
    else:
        db.add(Membership(workspace_id=wid, user_id=account.id, role=data.role))
    db.commit()
    return {"user_id": account.id, "username": account.username, "role": data.role}

@router.delete("/workspaces/{wid}/members/{user_id}", response_model=S.Ok)
def remove_member(wid: str, user_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lock_members(db, wid)
    membership(db, wid, user, admin=True)
    member = db.scalar(select(Membership).where(Membership.workspace_id == wid, Membership.user_id == user_id))
    if not member:
        raise HTTPException(404, "Member not found")
    preserve_admin(db, member)
    db.delete(member)
    db.commit()
    return {"ok": True}

@router.get("/skills", response_model=list[S.SkillOut])
def skills(user: User = Depends(current_user)):
    return skill_catalog()

@router.get("/workspaces/{wid}/models", response_model=list[S.ModelOut])
def models(wid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    return [model_out(m) for m in db.scalars(select(ModelConnection).where(ModelConnection.workspace_id == wid).order_by(ModelConnection.created_at))]

@router.post("/workspaces/{wid}/models", response_model=S.ModelOut, status_code=201)
def create_model(wid: str, data: S.ModelIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user, admin=True)
    if data.thinking_format == "qwen-chat-template" and data.protocol != "openai-completions":
        raise HTTPException(422, "Qwen chat-template thinking format requires the OpenAI completions protocol")
    if data.max_tokens > data.context_window:
        raise HTTPException(422, "Output token limit cannot exceed the context window")
    fields = data.model_dump(exclude={"api_key"})
    fields["base_url"] = validate_model_url(data.base_url)
    value = ModelConnection(workspace_id=wid, encrypted_api_key=encrypt_secret(data.api_key), **fields)
    db.add(value)
    db.commit()
    return model_out(value)

@router.patch("/workspaces/{wid}/models/{ident}", response_model=S.ModelOut)
def update_model(wid: str, ident: str, data: S.ModelPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user, admin=True)
    value = owned(db, ModelConnection, ident, wid)
    effective_protocol = data.protocol if data.protocol is not None else value.protocol
    effective_format = data.thinking_format if data.thinking_format is not None else value.thinking_format
    if effective_format == "qwen-chat-template" and effective_protocol != "openai-completions":
        raise HTTPException(422, "Qwen chat-template thinking format requires the OpenAI completions protocol")
    for name, field in data.model_dump(exclude_unset=True, exclude_none=True).items():
        if name == "api_key":
            if field:
                value.encrypted_api_key = encrypt_secret(field)
        else:
            setattr(value, name, validate_model_url(field) if name == "base_url" else field)
    if value.max_tokens > value.context_window:
        raise HTTPException(422, "Output token limit cannot exceed the context window")
    db.commit()
    return model_out(value)

@router.post("/workspaces/{wid}/models/{ident}/test", response_model=S.ModelTest)
def test_model(wid: str, ident: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user, admin=True)
    value = owned(db, ModelConnection, ident, wid)
    base = validate_model_url(value.base_url)
    secret = decrypt_secret(value.encrypted_api_key)
    # Allow reasoning-capable models enough room while keeping the probe bounded.
    probe_tokens = min(512, value.max_tokens)
    # Test a forced tool invocation, not only a successful HTTP handshake.
    tool = {"name": "connection_check", "description": "Confirm that tools work", "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}
    if value.protocol == "anthropic-messages":
        endpoint = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {"x-api-key": secret, "anthropic-version": "2023-06-01"}
        payload = {"model": value.model, "max_tokens": probe_tokens, "messages": [{"role": "user", "content": "Call connection_check with ok true."}], "tools": [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}], "tool_choice": {"type": "tool", "name": tool["name"]}}
    else:
        endpoint = base + "/chat/completions"
        headers = {"Authorization": f"Bearer {secret}"}
        payload = {"model": value.model, "max_tokens": probe_tokens, "messages": [{"role": "user", "content": "Call connection_check with ok true."}], "tools": [{"type": "function", "function": tool}], "tool_choice": {"type": "function", "function": {"name": tool["name"]}}}
    if value.thinking_format == "qwen-chat-template":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        status, body = post_model_json(endpoint, headers=headers, payload=payload, allow_private=get_settings().allow_private_models)
        if not 200 <= status < 300:
            return {"ok": False, "message": f"Model endpoint returned HTTP {status}; check credentials, model and protocol."}
        finish_reason = body.get("stop_reason") if value.protocol == "anthropic-messages" else body.get("choices", [{}])[0].get("finish_reason")
        if finish_reason in {"length", "max_tokens"}:
            return {"ok": False, "message": f"Model probe reached its {probe_tokens}-token output limit before completing the tool call. Increase this connection's output limit if below 512, or select a model that completes bounded tool calls."}
        calls = [c for c in body.get("content", []) if c.get("type") == "tool_use"] if value.protocol == "anthropic-messages" else body.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
        matched = False
        for call in calls:
            name = call.get("name") if value.protocol == "anthropic-messages" else call.get("function", {}).get("name")
            args = call.get("input", {}) if value.protocol == "anthropic-messages" else json.loads(call.get("function", {}).get("arguments", "{}"))
            if name == "connection_check" and isinstance(args, dict) and args.get("ok") is True:
                matched = True
        if not matched:
            return {"ok": False, "message": "Endpoint responded but did not perform the required tool call."}
        return {"ok": True, "message": "Model connection and tool invocation succeeded. Vision is configured separately."}
    except (ModelTransportError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return {"ok": False, "message": "Model connection failed or returned an unsupported response; check endpoint and protocol."}

@router.get("/workspaces/{wid}/templates", response_model=list[S.TemplateOut])
def templates(wid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    values = db.scalars(select(Template).where(or_(Template.workspace_id == wid, Template.is_builtin.is_(True))).order_by(Template.is_builtin.desc(), Template.created_at))
    return [template_out(db, t) for t in values]

@router.post("/workspaces/{wid}/templates", response_model=S.TemplateOut, status_code=201)
def create_template(wid: str, data: S.TemplateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    template = Template(workspace_id=wid, owner_id=user.id, name=data.name)
    db.add(template)
    db.flush()
    version = TemplateVersion(template_id=template.id, number=1, **data.model_dump(exclude={"name"}))
    db.add(version)
    db.flush()
    template.current_version_id = version.id
    db.commit()
    return template_out(db, template)

@router.put("/workspaces/{wid}/templates/{ident}", response_model=S.TemplateOut)
def update_template(wid: str, ident: str, data: S.TemplateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    member = membership(db, wid, user)
    template = visible_template(db, ident, wid, lock=True)
    if template.is_builtin:
        raise HTTPException(403, "Copy the built-in template to edit it")
    if template.owner_id != user.id and member.role != "admin":
        raise HTTPException(403, "Only the owner or workspace administrator may edit this template")
    previous = db.get(TemplateVersion, template.current_version_id)
    version = TemplateVersion(template_id=template.id, number=previous.number + 1, **data.model_dump(exclude={"name"}))
    db.add(version)
    db.flush()
    template.name, template.current_version_id = data.name, version.id
    db.commit()
    return template_out(db, template)

@router.get("/workspaces/{wid}/templates/{ident}/versions", response_model=list[S.TemplateVersionOut])
def template_versions(wid: str, ident: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    visible_template(db, ident, wid)
    return list(db.scalars(select(TemplateVersion).where(TemplateVersion.template_id == ident).order_by(TemplateVersion.number.desc())))

@router.get("/workspaces/{wid}/papers", response_model=list[S.PaperOut])
def papers(wid: str, search: str = Query(default="", max_length=300), limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    stmt = select(Paper).where(Paper.workspace_id == wid)
    if search:
        stmt = stmt.where(or_(Paper.title.ilike(f"%{search}%"), Paper.metadata_json["title_zh"].as_string().ilike(f"%{search}%")))
    return list(db.scalars(stmt.order_by(Paper.created_at.desc()).limit(limit).offset(offset)))

_ARXIV_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v[1-9]\d*)?$")

@router.post("/workspaces/{wid}/papers", response_model=S.PaperOut, status_code=201)
def create_paper(wid: str, data: S.ArxivIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    value = data.arxiv.strip()
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            raise HTTPException(422, "Invalid arXiv URL")
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise HTTPException(422, "Use an arXiv identifier or an arxiv.org URL")
        value = re.sub(r"^/(?:abs|pdf|html)/", "", parsed.path).removesuffix(".pdf")
    value = value.removeprefix("arXiv:")
    if not _ARXIV_RE.fullmatch(value):
        raise HTTPException(422, "Invalid arXiv identifier")
    title = (data.title or "").strip()
    paper = Paper(workspace_id=wid, created_by=user.id, title=title or f"arXiv:{value}", source_type="arxiv", source_url=f"https://arxiv.org/abs/{value}", metadata_json={"arxiv_id": value, "title_status": "pending", "title_override": bool(title)})
    db.add(paper)
    db.commit()
    return paper

@router.post("/workspaces/{wid}/papers/upload", response_model=S.PaperOut, status_code=201)
def upload_paper(wid: str, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    ident = new_id()
    relative = Path("storage") / "workspaces" / wid / "papers" / ident / "source.pdf"
    path = get_settings().runtime_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    digest, count = hashlib.sha256(), 0
    try:
        with path.open("xb") as output:
            first = True
            while chunk := file.file.read(1024 * 1024):
                if first and not chunk.startswith(b"%PDF-"):
                    raise HTTPException(422, "File is not a PDF")
                first = False
                count += len(chunk)
                if count > get_settings().upload_max_bytes:
                    raise HTTPException(413, "PDF exceeds the configured upload size")
                output.write(chunk)
                digest.update(chunk)
        if not count:
            raise HTTPException(422, "PDF is empty")
        import fitz
        with fitz.open(path) as document:
            if document.needs_pass or document.page_count == 0:
                raise HTTPException(422, "PDF is encrypted or has no readable pages")
            metadata = {"source_mime_type": "application/pdf", "pages": document.page_count, "filename": Path(file.filename or "paper.pdf").name, "title_status": "pending"}
            title = document.metadata.get("title") or Path(file.filename or "Untitled paper.pdf").stem
        paper = Paper(id=ident, workspace_id=wid, created_by=user.id, title=title[:1000], source_type="pdf", storage_path=str(relative), sha256=digest.hexdigest(), metadata_json=metadata)
        db.add(paper)
        db.commit()
        return paper
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        db.rollback()
        path.unlink(missing_ok=True)
        if isinstance(exc, (ValueError, RuntimeError)):
            raise HTTPException(422, "PDF could not be opened")
        raise
    finally:
        file.file.close()

@router.get("/workspaces/{wid}/papers/{pid}", response_model=S.PaperOut)
def paper_detail(wid: str, pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    return owned(db, Paper, pid, wid)

@router.post("/workspaces/{wid}/papers/{pid}/title", response_model=S.PaperOut, status_code=202)
def refresh_paper_title(wid: str, pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    paper = owned(db, Paper, pid, wid, lock=True)
    # Repeated clicks must not supersede an active claim or make duplicate calls.
    if paper.metadata_json.get("title_status") not in {"pending", "processing"}:
        paper.metadata_json = {**paper.metadata_json, "title_status": "pending", "title_error": None,
                               "title_task_token": None, "title_override": False}
        db.commit()
    return paper

@router.get("/workspaces/{wid}/papers/{pid}/file")
def paper_file(wid: str, pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    paper = owned(db, Paper, pid, wid)
    if not paper.storage_path:
        raise HTTPException(404, "Source file has not been downloaded yet")
    path = source_path(paper.storage_path)
    tenant_root = (get_settings().runtime_dir / "storage" / "workspaces" / wid).resolve()
    if not path.is_relative_to(tenant_root):
        raise HTTPException(404, "Source file not found")
    mime_type = paper.metadata_json.get("source_mime_type")
    if mime_type not in {"application/pdf", "text/html", "text/plain"}:
        mime_type = {".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html", ".txt": "text/plain"}.get(path.suffix.lower(), "application/octet-stream")
    inline = mime_type == "application/pdf"
    return FileResponse(path, media_type=mime_type, filename=f"{paper.id}{path.suffix}", content_disposition_type="inline" if inline else "attachment", headers={"Content-Security-Policy": "sandbox; default-src 'none'"})

def snapshot(db, reading, *, instruction="", base_revision_id=None):
    template = visible_template(db, reading.template_id, reading.workspace_id)
    version = db.get(TemplateVersion, template.current_version_id)
    model = owned(db, ModelConnection, reading.model_connection_id, reading.workspace_id)
    value = {"template": {"id": template.id, "version_id": version.id, "brief_markdown": version.brief_markdown, "full_markdown": version.full_markdown, "instructions": version.instructions}, "model": {k: v for k, v in model_out(model).items() if k != "has_api_key"}, "skill_id": reading.skill_id, "workflow_mode": reading.workflow_mode, "paper_id": reading.paper_id, "instruction": instruction, "base_revision_id": base_revision_id}
    if reading.workflow_mode == "guided":
        settings = get_settings()
        if base_revision_id:
            parent = owned(db, Revision, base_revision_id, reading.workspace_id)
            if parent.reading_id != reading.id:
                raise HTTPException(404, "Revision not found")
            parent_rules = (parent.provenance or {}).get("workflow", {}).get("version", "guided-v2.3")
            if parent_rules == "guided-v2.3":
                # Existing immutable evidence/section IDs keep their original
                # parser interpretation throughout revisions and retries.
                value.update(workflow_version="guided-v2.3", source_rules_version="source-sections-v2")
                return agent_snapshot(value) if settings.pi_agent_enabled else value
        value.update(workflow_version="guided-v3.0", source_rules_version="source-structured-v3",
                     workflow_limits={"timeout_seconds": settings.job_timeout_seconds,
                                      "max_tool_calls": settings.max_tool_calls,
                                      "long_timeout_seconds": settings.long_job_timeout_seconds,
                                      "long_max_tool_calls": settings.long_max_tool_calls,
                                      "segment_concurrency": settings.segment_concurrency})
        if settings.pi_agent_enabled:
            value = agent_snapshot(value)
    return value

def queued_job(db, reading, user, kind, inputs):
    job = Job(workspace_id=reading.workspace_id, reading_id=reading.id, created_by=user.id, kind=kind, input_snapshot=inputs)
    db.add(job)
    db.flush()
    ensure_agent_task(db, job)
    db.add(JobEvent(job_id=job.id, workspace_id=reading.workspace_id, event_type="progress", data={"stage": "queued", "progress": 0, "message": "Queued for background reading"}))
    return job

def reject_active(db, reading_id):
    if db.scalar(select(Job.id).where(Job.reading_id == reading_id, Job.kind != "chat", Job.conversation_id.is_(None), Job.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, "This reading already has an active task")

@router.post("/workspaces/{wid}/readings", response_model=S.ReadingCreated, status_code=201)
def create_reading(wid: str, data: S.ReadingIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    owned(db, Paper, data.paper_id, wid)
    owned(db, ModelConnection, data.model_connection_id, wid)
    visible_template(db, data.template_id, wid)
    skill = next((skill for skill in skill_catalog() if skill["id"] == data.skill_id), None)
    if not skill or not skill["available"]:
        raise HTTPException(422, "Selected skill is unavailable")
    reading = Reading(workspace_id=wid, created_by=user.id, **data.model_dump())
    db.add(reading)
    db.flush()
    job = queued_job(db, reading, user, "read", snapshot(db, reading))
    db.commit()
    return {"reading_id": reading.id, "job_id": job.id}

@router.get("/workspaces/{wid}/readings", response_model=list[S.ReadingListOut])
def readings(wid: str, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    rows = db.execute(select(Reading, Paper.title).join(Paper, Paper.id == Reading.paper_id).where(Reading.workspace_id == wid).order_by(Reading.created_at.desc()).limit(limit).offset(offset)).all()
    return [{"id": reading.id, "paper_id": reading.paper_id, "paper_title": title, "skill_id": reading.skill_id, "workflow_mode": reading.workflow_mode, "created_at": reading.created_at, "current_revision_id": reading.current_revision_id, "latest_job": db.scalar(select(Job).where(Job.reading_id == reading.id, Job.workspace_id == wid, Job.kind != "chat", Job.conversation_id.is_(None)).order_by(Job.created_at.desc()).limit(1))} for reading, title in rows]

@router.get("/workspaces/{wid}/readings/{rid}", response_model=S.ReadingDetail)
def reading_detail(wid: str, rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    reading = owned(db, Reading, rid, wid)
    return {"id": reading.id, "paper_id": reading.paper_id, "paper_title": owned(db, Paper, reading.paper_id, wid).title, "skill_id": reading.skill_id, "workflow_mode": reading.workflow_mode, "current_revision_id": reading.current_revision_id, "revisions": list(db.scalars(select(Revision).where(Revision.reading_id == rid, Revision.workspace_id == wid).order_by(Revision.number.desc()))), "jobs": list(db.scalars(select(Job).where(Job.reading_id == rid, Job.workspace_id == wid, Job.kind != "chat", Job.conversation_id.is_(None)).order_by(Job.created_at.desc())))}

@router.post("/workspaces/{wid}/readings/{rid}/revisions", response_model=S.JobCreated, status_code=201)
def revise(wid: str, rid: str, data: S.RevisionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    reading = owned(db, Reading, rid, wid, lock=True)
    reject_active(db, rid)
    if not reading.current_revision_id or data.base_revision_id != reading.current_revision_id:
        raise HTTPException(409, "Revision is stale; reload the latest result")
    job = queued_job(db, reading, user, "revise", snapshot(db, reading, instruction=data.instruction, base_revision_id=data.base_revision_id))
    db.commit()
    return {"job_id": job.id}

@router.get("/workspaces/{wid}/readings/{rid}/export")
def export_reading(wid: str, rid: str, revision_id: str | None = None, view: str = Query(default="brief", pattern="^(brief|full)$"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    reading = owned(db, Reading, rid, wid)
    revision = db.scalar(select(Revision).where(Revision.id == (revision_id or reading.current_revision_id), Revision.reading_id == rid, Revision.workspace_id == wid))
    if not revision:
        raise HTTPException(404, "Revision not found")
    content = revision.brief_markdown if view == "brief" else revision.full_markdown
    content += "\n\n---\n\n## Evidence / 证据来源\n\n"
    for evidence in revision.evidence_index:
        source = evidence.get("source_url") or evidence.get("source_title", "")
        kind = evidence.get("evidence_type", "text_quote")
        if kind == "image_observation":
            content += f"- [{evidence.get('id', '?')}] 图像观察 · {evidence.get('figure_id', '')} · {source} — {evidence.get('observation', '')}（条件：{evidence.get('conditions', '')}；图像 SHA-256：{evidence.get('image_sha256', '')}）\n"
        elif kind == "table_cell":
            content += f"- [{evidence.get('id', '?')}] 表格单元格 · {source} · {' / '.join(evidence.get('headers', []))} — {evidence.get('quote', '')}\n"
        else:
            label = "原文段落定位" if evidence.get("selection") == "section" else "原文摘录"
            content += f"- [{evidence.get('id', '?')}] {label} · {source} — {evidence.get('quote', '')}\n"
    return Response(content, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="reading-{rid}-v{revision.number}-{view}.md"'})

@router.get("/workspaces/{wid}/jobs", response_model=list[S.JobOut])
def jobs(wid: str, limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    return list(db.scalars(select(Job).where(Job.workspace_id == wid, Job.kind != "chat", Job.conversation_id.is_(None)).order_by(Job.created_at.desc()).limit(limit).offset(offset)))

@router.get("/workspaces/{wid}/jobs/{jid}", response_model=S.JobOut)
def job_detail(wid: str, jid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    return owned(db, Job, jid, wid)

@router.post("/workspaces/{wid}/jobs/{jid}/cancel", response_model=S.JobOut)
def cancel_job(wid: str, jid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    job = owned(db, Job, jid, wid, lock=True)
    if job.status in ACTIVE:
        job.cancel_requested = True
        if job.status == "queued":
            job.status, job.stage = "cancelled", "cancelled"
            db.add(JobEvent(job_id=jid, workspace_id=wid, event_type="cancelled", data={"status": "cancelled"}))
        else:
            db.add(JobEvent(job_id=jid, workspace_id=wid, event_type="message", data={"message": "Cancellation requested"}))
        db.commit()
    return job

@router.post("/workspaces/{wid}/jobs/{jid}/retry", response_model=S.JobCreated, status_code=201)
def retry_job(wid: str, jid: str, data: S.RetryIn | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    old = owned(db, Job, jid, wid)
    if old.status not in ("failed", "cancelled", "partial"):
        raise HTTPException(409, "Only failed, cancelled or partial tasks can be retried")
    reading = owned(db, Reading, old.reading_id, wid, lock=True)
    reject_active(db, reading.id)
    if old.kind == "revise" and old.input_snapshot.get("base_revision_id") != reading.current_revision_id:
        raise HTTPException(409, "Revision base is stale; create a new revision task")
    if old.kind == "read" and reading.current_revision_id:
        raise HTTPException(409, "This reading has a result; revise it or create another reading")
    inputs = copy.deepcopy(old.input_snapshot)
    inputs["workflow_mode"] = old.input_snapshot.get("workflow_mode", "native")
    inputs["retry_of_job_id"] = old.id
    current_model = owned(db, ModelConnection, reading.model_connection_id, wid)
    inputs["model"] = {k: v for k, v in model_out(current_model).items() if k != "has_api_key"}
    job = queued_job(db, reading, user, old.kind, inputs)
    db.commit()
    return {"job_id": job.id}

@router.post("/workspaces/{wid}/jobs/{jid}/continue", response_model=S.JobCreated, status_code=202)
def continue_job(wid: str, jid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    old = owned(db, Job, jid, wid)
    reading = owned(db, Reading, old.reading_id, wid, lock=True)
    old = owned(db, Job, jid, wid, lock=True)
    allowed = ("waiting", "failed", "cancelled") if old.execution_engine == ENGINE else ("failed", "partial", "cancelled")
    if old.status not in allowed:
        raise HTTPException(409, "Only a stopped task can be continued")
    reject_active(db, reading.id)
    if old.kind == "revise" and old.input_snapshot.get("base_revision_id") != reading.current_revision_id:
        raise HTTPException(409, "Revision base is stale; create a new revision task")
    if old.kind == "read" and reading.current_revision_id:
        raise HTTPException(409, "This reading has a result; revise it or create another reading")
    if old.input_snapshot.get("paper_id") != reading.paper_id:
        raise HTTPException(409, "Paper input no longer matches this reading")
    current_model = owned(db, ModelConnection, reading.model_connection_id, wid)
    inputs = agent_snapshot(old.input_snapshot, old.kind)
    inputs["model"] = {k: v for k, v in model_out(current_model).items() if k != "has_api_key"}
    if old.execution_engine == ENGINE:
        old.input_snapshot = inputs
        grant_continuation(db, old)
        job = old
    else:
        inputs["retry_from_job_id"] = old.id
        inputs["retry_of_job_id"] = old.id
        # This is an explicit takeover, not mutation of the historical workflow.
        inputs["workflow_mode"] = "guided"
        job = queued_job(db, reading, user, old.kind, inputs)
    db.commit()
    return {"job_id": job.id}


@router.get("/workspaces/{wid}/jobs/{jid}/events")
async def events(wid: str, jid: str, request: Request, after: int = Query(default=0, ge=0), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    owned(db, Job, jid, wid)
    try:
        header_cursor = int(request.headers.get("last-event-id", "0"))
        if header_cursor < 0:
            raise ValueError
        cursor = max(after, header_cursor)
    except ValueError:
        raise HTTPException(422, "Invalid Last-Event-ID")
    user_id, session_id = user.id, token_hash(request.cookies[COOKIE_NAME])
    db.rollback()
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with SessionLocal() as session:
                login = session.get(SessionRecord, session_id)
                account = session.get(User, user_id)
                member = session.scalar(select(Membership.id).where(Membership.workspace_id == wid, Membership.user_id == user_id))
                if not login or login.expires_at <= now() or not account or not account.active or not member:
                    return
                job = session.scalar(select(Job).where(Job.id == jid, Job.workspace_id == wid, Job.kind != "chat", Job.conversation_id.is_(None)))
                if not job:
                    return
                records = list(session.scalars(select(JobEvent).where(JobEvent.job_id == jid, JobEvent.workspace_id == wid, JobEvent.id > cursor).order_by(JobEvent.id).limit(100)))
                terminal = job.status in TERMINAL
                terminal_kinds = {"completed", "failed", "cancelled", "waiting"}
                latest_terminal = session.scalar(select(func.max(JobEvent.id)).where(
                    JobEvent.job_id == jid, JobEvent.workspace_id == wid,
                    JobEvent.event_type.in_(terminal_kinds))) if terminal else None
                outcome = job.status if job.status in {"failed", "cancelled", "waiting"} else "completed" if terminal else None
                sent_waiting = False
                for event in records:
                    cursor = event.id
                    # Continuations reuse this Job and its journal. A terminal
                    # record from an earlier allocation must not close the new
                    # stream or mislabel its current outcome.
                    if event.event_type in terminal_kinds and (event.event_type != outcome or event.id != latest_terminal):
                        continue
                    sent_waiting = sent_waiting or event.event_type == "waiting"
                    yield f"id: {event.id}\nevent: {event.event_type}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"
                if job.status == "waiting" and len(records) < 100 and not sent_waiting:
                    payload = {"status": "waiting", "agent_task": job.agent_task, "execution_engine": job.execution_engine}
                    yield f"event: waiting\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if terminal and len(records) < 100:
                return
            if not records:
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.get("/workspaces/{wid}/artifacts/{revision_id}/{index}")
def artifact(wid: str, revision_id: str, index: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    revision = owned(db, Revision, revision_id, wid)
    if index < 0 or index >= len(revision.artifacts):
        raise HTTPException(404, "Artifact not found")
    item = revision.artifacts[index]
    path = source_path(item["path"])
    expected = get_settings().runtime_dir / "storage" / "workspaces" / wid
    if not path.is_relative_to(expected.resolve()):
        raise HTTPException(404, "Artifact not found")
    return FileResponse(path, media_type=item.get("mime_type", "application/octet-stream"), filename=Path(item.get("name", path.name)).name)
