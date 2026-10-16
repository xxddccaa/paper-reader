"""Private chat execution on the existing durable job/lease infrastructure."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import (ChatConversation, ChatMessage, Job, JobEvent, Membership,
                     ModelConnection, Paper, Reading, Revision, User, now)
from .pipeline import BudgetExhausted, PipelineError, TaskCancelled, task_path
from .security import decrypt_secret
from .source_tools import SourceDocument

MAX_DRAFT_CHARACTERS = 200000


def sync_chat_job(db, job):
    """Called in the transaction changing a job's lifecycle (including recovery)."""
    if job.kind != "chat":
        return
    message = db.scalar(select(ChatMessage).where(ChatMessage.job_id == job.id,
        ChatMessage.conversation_id == job.conversation_id, ChatMessage.workspace_id == job.workspace_id))
    if message:
        message.status, message.error = job.status, job.error
        message.usage, message.updated_at = deepcopy(job.usage or {}), now()


def _conversation(db, job, *, lock=False):
    query = select(ChatConversation).where(ChatConversation.id == job.conversation_id,
        ChatConversation.workspace_id == job.workspace_id, ChatConversation.reading_id == job.reading_id,
        ChatConversation.user_id == job.created_by)
    if lock:
        query = query.with_for_update()
    conversation = db.scalar(query)
    if conversation is None:
        raise TaskCancelled("Private conversation is unavailable")
    member = db.scalar(select(Membership.id).join(User, User.id == Membership.user_id).where(
        Membership.workspace_id == job.workspace_id, Membership.user_id == conversation.user_id, User.active.is_(True)))
    if member is None:
        raise TaskCancelled("Conversation membership is no longer active")
    return conversation


def _message(db, job):
    message = db.scalar(select(ChatMessage).where(ChatMessage.id == job.input_snapshot.get("chat", {}).get("message_id"),
        ChatMessage.job_id == job.id, ChatMessage.conversation_id == job.conversation_id,
        ChatMessage.workspace_id == job.workspace_id, ChatMessage.role == "assistant"))
    if message is None:
        raise TaskCancelled("Chat response was superseded")
    return message


def persist_chat_event(job_id, token, event):
    from .worker import _owned_job
    with SessionLocal() as db, db.begin():
        job = _owned_job(db, job_id, token)
        _conversation(db, job)
        message = _message(db, job)
        kind = event.get("type")
        data = {"message_id": message.id, "attempt": job.attempt}
        if kind == "delta":
            chunk = str(event.get("text", ""))
            if len(message.content) + len(chunk) > MAX_DRAFT_CHARACTERS:
                raise PipelineError("Chat draft exceeded its size limit")
            message.content += chunk
            data["text"] = chunk
        elif kind == "replace":
            content = str(event.get("content", ""))
            if len(content) > MAX_DRAFT_CHARACTERS:
                raise PipelineError("Chat draft exceeded its size limit")
            message.content = content
            data["content"] = content
        elif kind == "progress":
            data.update(stage=str(event.get("stage", "answer"))[:100], message=str(event.get("message", ""))[:2000])
            job.stage = data["stage"]
        else:
            raise PipelineError("Unknown chat event")
        message.status, message.updated_at = "running", now()
        job.updated_at = now()
        db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type=kind, data=data))


class ChatStream:
    """Coalesce small SDK deltas while persisting before any browser sees them."""
    def __init__(self, job_id, token, secret):
        self.job_id, self.token, self.secret = job_id, token, secret
        self.pending = ""
        self.last_flush = time.monotonic()
        self.reset_on_delta = False

    def start_answer(self):
        # Keep the last useful draft if a repair fails before producing text.
        # Once a new draft starts it replaces, rather than appends to, that text.
        self.flush(force=True)
        self.reset_on_delta = True

    def emit(self, event):
        from .worker import _safe_log_value
        event = _safe_log_value(event, self.secret)
        if event.get("type") == "delta":
            if self.reset_on_delta and event.get("text"):
                persist_chat_event(self.job_id, self.token, {"type": "replace", "content": ""})
                self.reset_on_delta = False
            self.pending += event.get("text", "")
            self.flush()
        else:
            self.flush(force=True)
            if event.get("type") == "replace":
                self.reset_on_delta = False
            persist_chat_event(self.job_id, self.token, event)

    def flush(self, *, force=False):
        if self.pending and (force or len(self.pending.encode()) >= 1024 or time.monotonic() - self.last_flush >= .1):
            content = self.pending.replace(self.secret, "[REDACTED]") if self.secret else self.pending
            tail = ""
            if self.secret and not force:
                # A credential could straddle provider chunks; keep any suffix
                # that could still complete it before committing public text.
                for size in range(min(len(content), len(self.secret) - 1), 0, -1):
                    if content.endswith(self.secret[:size]):
                        content, tail = content[:-size], content[-size:]
                        break
            if content:
                persist_chat_event(self.job_id, self.token, {"type": "delta", "text": content})
            self.pending, self.last_flush = tail, time.monotonic()


def _copy_file(runtime, allowed_root, entry, work_dir, *, expected=None):
    original = task_path(allowed_root, str(runtime / entry["path"]), must_exist=True)
    target = task_path(work_dir, entry["name"])
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    if expected and digest != expected:
        raise PipelineError("Retained chat source hash does not match")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise PipelineError("Retained source paths conflict")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(original, target)
        except OSError:
            shutil.copyfile(original, target)
            target.chmod(0o600)
    return target


def _restore_revision_sources(revision, runtime, work_dir):
    root = runtime / "storage" / "workspaces" / revision.workspace_id
    artifacts = {item.get("name"): item for item in revision.artifacts}
    corpus = artifacts.get("source_corpus.json")
    if corpus is None:
        raise PipelineError("This reading has no retained source corpus for follow-up questions")
    original = task_path(root, str(runtime / corpus["path"]), must_exist=True)
    payload = json.loads(original.read_text())
    required = {}
    for doc in payload.get("documents", []):
        required[doc["path"]] = doc["sha256"]
        for rep in doc.get("coverage", {}).get("representations", []):
            required[rep["path"]] = rep["sha256"]
    for bucket in ("visual_assets", "prepared_visuals"):
        for item in payload.get(bucket, {}).values():
            required[item["path"]] = item["sha256"]
    for name, expected in required.items():
        if name not in artifacts:
            raise PipelineError("Reading source artifact is unavailable")
        _copy_file(runtime, root, artifacts[name], work_dir, expected=expected)
    _copy_file(runtime, root, corpus, work_dir)


def build_chat_spec(job, work_dir):
    settings = get_settings()
    with SessionLocal() as db:
        conversation = _conversation(db, job)
        message = _message(db, job)
        reading = db.scalar(select(Reading).where(Reading.id == job.reading_id, Reading.workspace_id == job.workspace_id))
        revision = db.scalar(select(Revision).where(Revision.id == conversation.revision_id,
            Revision.reading_id == job.reading_id, Revision.workspace_id == job.workspace_id))
        paper = db.scalar(select(Paper).where(Paper.id == reading.paper_id, Paper.workspace_id == job.workspace_id)) if reading else None
        connection = db.scalar(select(ModelConnection).where(ModelConnection.id == reading.model_connection_id,
            ModelConnection.workspace_id == job.workspace_id)) if reading else None
        if not all((revision, paper, connection)):
            raise PipelineError("Conversation paper, version or model is unavailable")
        question = db.scalar(select(ChatMessage).where(ChatMessage.id == job.input_snapshot["chat"]["user_message_id"],
            ChatMessage.conversation_id == conversation.id, ChatMessage.workspace_id == job.workspace_id,
            ChatMessage.role == "user", ChatMessage.sequence == message.sequence - 1))
        if question is None:
            raise PipelineError("Conversation question is unavailable")
        rows = list(db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation.id,
            ChatMessage.sequence < question.sequence).order_by(ChatMessage.sequence)))
        # Only complete, verified pairs enter subsequent model context.
        successful = {row.sequence for row in rows if row.role == "assistant" and row.status == "succeeded"}
        history = [{"role": row.role, "content": history_content(row), "sequence": row.sequence} for row in rows
            if row.sequence > conversation.summary_through_sequence and
            ((row.role == "assistant" and row.sequence in successful) or (row.role == "user" and row.sequence + 1 in successful))]
        latest = next((row for row in reversed(rows) if row.role == "assistant" and row.status in {"succeeded", "partial"} and row.artifacts), None)
        saved_sources = deepcopy(latest.artifacts) if latest else []
        model = dict(job.input_snapshot["model"])
        model.setdefault("thinking_format", "default")
        fields = ("id", "name", "base_url", "model", "protocol", "thinking_format", "vision", "context_window", "max_tokens")
        if any(model.get(key) != getattr(connection, key) for key in fields):
            raise PipelineError("Model configuration changed; retry this answer to use current configuration")
        secret = decrypt_secret(connection.encrypted_api_key)
        model.update(api_key=secret, allow_private_models=settings.allow_private_models)
        source_rules = job.input_snapshot.get("source_rules_version", "source-sections-v2")
        workflow_version = job.input_snapshot.get("workflow_version", "guided-v2.3")
        context = {"paper_title": paper.title, "brief_markdown": revision.brief_markdown,
            "full_markdown": revision.full_markdown, "reading_packet": revision.reading_packet,
            "evidence_index": revision.evidence_index}
        _restore_revision_sources(revision, settings.runtime_dir, work_dir)
        cached = {}
        private_root = settings.runtime_dir / "storage" / "workspaces" / job.workspace_id / "conversations" / conversation.id
        for artifact in saved_sources:
            metadata = artifact
            if artifact.get("metadata_path"):
                metadata_file = task_path(private_root, str(settings.runtime_dir / artifact["metadata_path"]), must_exist=True)
                metadata_bytes = metadata_file.read_bytes()
                if hashlib.sha256(metadata_bytes).hexdigest() != artifact.get("metadata_sha256"):
                    raise PipelineError("Retained conversation source metadata changed")
                metadata = json.loads(metadata_bytes)
            document = metadata.get("document")
            if not isinstance(document, dict) or document.get("id") == "main":
                continue
            for entry in metadata.get("files", []):
                _copy_file(settings.runtime_dir, private_root, entry, work_dir, expected=entry.get("sha256"))
            cached[document["id"]] = SourceDocument(**document)
        spec = {"type": "run", "run_id": job.id, "kind": "chat", "skill_id": reading.skill_id,
            "skill_root": str(settings.project_dir / "skills" / reading.skill_id), "work_dir": str(work_dir),
            "model": model, "workflow_mode": "guided", "workflow_version": workflow_version,
            "source_rules_version": source_rules,
            "paper": {"id": paper.id, "title": paper.title, "source_type": paper.source_type, "source_url": paper.source_url,
                "storage_path": str(settings.runtime_dir / paper.storage_path) if paper.storage_path else None},
            "template": job.input_snapshot["template"], "limits": {"timeout_seconds": 300, "max_tool_calls": 48, "max_related_papers": 3},
            "chat": {"operation": "answer", "input": {"question": question.content, "history": history,
                "summary": conversation.summary, "summary_through_sequence": conversation.summary_through_sequence,
                "reading_context": context, "section_index": [], "source_sections": []}}}
    return spec, secret, cached


def history_content(message):
    if message.role != "assistant" or not message.evidence_index:
        return message.content
    locators = [{key: item[key] for key in ("id", "source_id", "section_id", "page") if key in item}
                for item in message.evidence_index[:60]]
    return (message.content + "\n\n[此轮引用定位仅供回查，不代替原文证据]\n" +
            json.dumps({"message_sequence": message.sequence, "citations": locators}, ensure_ascii=False))


def _cache_bytes(target, data=None, original=None, *, expected):
    """Immutable, content-addressed cache survives an interrupted DB commit."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise PipelineError("Private source cache integrity check failed")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            if original is not None:
                with original.open("rb") as source:
                    shutil.copyfileobj(source, output)
            else:
                output.write(data)
        temporary.replace(target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _source_artifacts(pipeline, target):
    runtime = get_settings().runtime_dir
    documents = pipeline.retained_documents()
    if isinstance(documents, dict):
        documents = documents.values()
    artifacts = []
    for document in documents:
        entries = [{"path": document.path, "sha256": document.sha256},
            *document.coverage.get("representations", [])]
        files = []
        for entry in entries:
            original = task_path(pipeline.work_dir, entry["path"], must_exist=True)
            raw_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            if raw_hash != entry["sha256"]:
                raise PipelineError("Original source changed during the conversation")
            destination = task_path(target, raw_hash + "/source")
            _cache_bytes(destination, original=original, expected=raw_hash)
            files.append({"name": entry["path"], "path": str(destination.relative_to(runtime)), "sha256": raw_hash})
        first = files[0]
        metadata = json.dumps({"document": vars(document), "files": files}, ensure_ascii=False, sort_keys=True).encode()
        metadata_hash = hashlib.sha256(metadata).hexdigest()
        metadata_file = task_path(target, document.sha256 + "/metadata-" + metadata_hash + ".json")
        _cache_bytes(metadata_file, data=metadata, expected=metadata_hash)
        artifacts.append({"source_id": document.id, "name": document.path, "path": first["path"],
            "mime_type": mimetypes.guess_type(document.path)[0] or "application/octet-stream", "sha256": document.sha256,
            "metadata_path": str(metadata_file.relative_to(runtime)), "metadata_sha256": metadata_hash})
    return artifacts


def publish_chat_result(job_id, token, result, pipeline, usage):
    from .worker import _owned_job
    from .agent_state import ENGINE, stop_task_clock
    settings = get_settings()
    # Apply the same host-issued, task-bound submission gate as reading
    # publication. A runner response alone cannot authorize a formal answer.
    with SessionLocal() as mode_db:
        candidate = mode_db.get(Job, job_id)
        is_agent = bool(candidate and candidate.execution_engine == ENGINE)
    if is_agent:
        from .agent_tools import AgentTools
        tools = getattr(pipeline, "agent_tools", None)
        if (not isinstance(tools, AgentTools) or tools.pipeline is not pipeline
                or getattr(tools.store, "job_id", None) != job_id
                or getattr(tools.store, "lease_token", None) != token):
            raise PipelineError("Agent answer publication requires authentic task-bound host tools")
        trusted = tools.final_result(getattr(pipeline, "agent_submission", None))
        if trusted != result or trusted.get("status") != "succeeded":
            raise PipelineError("Agent answer changed after the host-issued final submission")
    with SessionLocal() as db, db.begin():
        candidate = db.get(Job, job_id)
        if candidate is None:
            raise TaskCancelled("Chat task is unavailable")
        conversation = _conversation(db, candidate, lock=True)
        job = _owned_job(db, job_id, token)
        message = _message(db, job)
        if result.get("status") not in {"succeeded", "partial"} or not isinstance(result.get("content"), str):
            raise PipelineError("Chat workflow did not return a valid answer")
        target = task_path(settings.runtime_dir / "storage" / "workspaces" / job.workspace_id,
            "conversations/" + conversation.id + "/sources")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifacts = _source_artifacts(pipeline, target)
        if job.lease_expires_at <= now():
            raise TaskCancelled("Lease expired while storing the answer")
        message.content, message.evidence_index = result["content"], result["evidence_index"]
        message.verification, message.artifacts = result["verification"], artifacts
        message.status = job.status = result["status"]
        message.error = job.error = None
        message.usage = job.usage = deepcopy(usage)
        message.updated_at = conversation.updated_at = job.updated_at = now()
        if result.get("summary") is not None and result.get("summary_through_sequence", 0) > conversation.summary_through_sequence:
            conversation.summary = result["summary"]
            conversation.summary_through_sequence = result["summary_through_sequence"]
        if is_agent:
            stop_task_clock(db, job)
        job.stage, job.progress, job.lease_token, job.lease_expires_at = job.status, 100, None, None
        db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type="replace",
            data={"message_id": message.id, "content": message.content, "attempt": job.attempt}))
        db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type="completed", data={"message_id": message.id}))


def run_chat_job(worker, job):
    from .chat_workflow import ChatPipeline, ChatWorkflow
    from .worker import (finish_failure, heartbeat, record_usage, release_for_shutdown,
                         run_runner_process, _safe_log_value)
    settings = get_settings()
    work_dir = task_path(settings.runtime_dir / "tmp", f"{job.id}/{job.attempt}")
    work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cancelled, done = threading.Event(), threading.Event()
    worker.current_cancel = cancelled
    deadline = time.monotonic() + 300
    token, secret, published = job.lease_token, "", False
    usage = deepcopy(job.usage or {})
    stream = None

    def check_budget():
        if cancelled.is_set():
            raise TaskCancelled("Chat cancelled or lease lost")
        if time.monotonic() >= deadline:
            raise BudgetExhausted("Chat answer exceeded its 300 second budget")

    def renew():
        while not done.wait(min(2, settings.lease_seconds / 3)):
            try:
                if not heartbeat(job.id, token):
                    cancelled.set(); return
            except Exception:
                cancelled.set(); return

    def usage_delta(delta):
        nonlocal usage
        usage = record_usage(job.id, token, _safe_log_value(delta, secret))

    guard = threading.Thread(target=renew, daemon=True, name="chat-lease-heartbeat")
    guard.start()
    try:
        # Recovery starts from immutable original/private sources and finalized
        # messages. Discard only this attempt's disposable work copies.
        if (work_dir / "source_corpus.json").exists():
            for entry in list(work_dir.iterdir()):
                if entry.is_symlink():
                    raise PipelineError("Chat work directory contains a symbolic link")
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        spec, secret, cached = build_chat_spec(job, work_dir)
        pipeline = ChatPipeline(work_dir, Path(spec["skill_root"]), spec["paper"], spec["skill_id"], job.workspace_id,
            cancelled, settings.project_dir, bool(spec["model"].get("vision")), max_tool_calls=48,
            workflow_mode="guided", rules_version=spec["source_rules_version"])
        pipeline.cached_documents.update(cached)
        stream = ChatStream(job.id, token, secret)
        stream.emit({"type": "replace", "content": ""})
        stream.emit({"type": "progress", "stage": "answer", "message": "正在阅读论文并回答追问"})
        pipeline.chat_emit = stream.emit
        pipeline.chat_flush = stream.flush

        def call_model(operation, inputs, timeout, tool_cap):
            check_budget()
            request = deepcopy(spec)
            request["chat"] = {"operation": operation, "input": inputs}
            request["limits"].update(timeout_seconds=max(1, min(timeout, deadline - time.monotonic())),
                                     max_tool_calls=min(tool_cap, 48 - pipeline.tool_calls))
            request["model"]["max_tokens"] = min(request["model"]["max_tokens"], 6000 if operation == "answer" else 3000)
            if operation == "answer":
                stream.start_answer()
            result = run_runner_process(job, token, request, pipeline, cancelled, deadline, secret, usage_delta)
            stream.flush(force=True)
            return result

        workflow = ChatWorkflow(pipeline, spec, call_model, stream.emit, check_budget)
        result = workflow.execute()
        stream.flush(force=True)
        check_budget()
        publish_chat_result(job.id, token, result, pipeline, usage)
        published = True
    except TaskCancelled:
        if worker.stopping.is_set():
            release_for_shutdown(job.id, token)
        else:
            finish_failure(job.id, token, "Task cancelled", cancelled=True)
    except Exception as exc:
        if stream:
            try:
                stream.flush(force=True)
            except (TaskCancelled, PipelineError):
                pass
        message = str(exc) if isinstance(exc, (PipelineError, ValueError)) else "Chat execution failed; inspect server diagnostics"
        finish_failure(job.id, token, _safe_log_value(message, secret))
    finally:
        done.set(); guard.join(timeout=3)
        worker.current_cancel = None
        if published:
            shutil.rmtree(work_dir.parent, ignore_errors=True)
        worker.cleanup_terminal_artifacts()
