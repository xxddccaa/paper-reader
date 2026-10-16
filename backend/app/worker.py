"""Durable PostgreSQL worker with leases, cancellation and fenced publication.

Run with ``python -m app.worker`` (or ``--once`` to claim at most one task).
A worker process handles one runner at a time; multiple processes coordinate
through PostgreSQL. The database, not a browser connection, owns task lifetime.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import logging
import math
import mimetypes
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import aliased

from .agent_state import ENGINE as AGENT_ENGINE, stop_task_clock
from .config import get_settings
from .db import SessionLocal
from .models import Job, JobEvent, ModelConnection, Paper, Reading, Revision, new_id, now
from .pipeline import BudgetExhausted, Pipeline, PipelineError, TaskCancelled, task_path, terminate_group, validate_result
from .security import decrypt_secret
from .source_tools import SourceError

LOG = logging.getLogger("paper_reader.worker")
CLAIM_ADVISORY_LOCK = 742791031
MAX_PROTOCOL_LINE = 4 * 1024 * 1024


class LeaseLost(TaskCancelled):
    pass


def _event(db, job: Job, kind: str, data: dict) -> None:
    if job.kind == "chat":
        from .chat_jobs import sync_chat_job
        sync_chat_job(db, job)
    db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type=kind, data=data))


def claim_job() -> Job | None:
    """Atomically recover abandoned work and claim within global/team limits."""
    settings = get_settings()
    with SessionLocal() as db, db.begin():
        # SKIP LOCKED prevents waiting on individual rows; a short advisory lock
        # serializes capacity decisions across independent worker processes.
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CLAIM_ADVISORY_LOCK})
        moment = now()
        abandoned = db.scalars(select(Job).where(Job.status == "running", Job.lease_expires_at < moment)
                              .with_for_update(skip_locked=True)).all()
        for job in abandoned:
            job.lease_token, job.lease_expires_at, job.updated_at = None, None, moment
            if job.cancel_requested:
                if job.execution_engine == AGENT_ENGINE:
                    stop_task_clock(db, job)
                job.status, job.stage = "cancelled", "cancelled"
                _event(db, job, "cancelled", {"status": "cancelled", "message": "Cancelled after expired worker lease"})
            elif job.execution_engine != AGENT_ENGINE and job.attempt >= settings.max_attempts:
                job.status, job.stage = "failed", "failed"
                job.error = "Worker lease expired; automatic recovery budget exhausted"
                _event(db, job, "failed", {"status": "failed", "message": job.error})
            else:
                job.status, job.stage = "queued", "recovering"
                _event(db, job, "message", {"message": "Previous worker lease expired; resuming saved artifacts"})
        cancelled = db.scalars(select(Job).where(Job.status == "queued", Job.cancel_requested.is_(True))
                              .with_for_update(skip_locked=True)).all()
        for job in cancelled:
            if job.execution_engine == AGENT_ENGINE:
                stop_task_clock(db, job)
            job.status, job.stage, job.updated_at = "cancelled", "cancelled", moment
            _event(db, job, "cancelled", {"status": "cancelled"})
        db.flush()
        active = db.scalars(select(Job).where(Job.status == "running")).all()
        if len(active) >= settings.max_running_jobs:
            return None
        running = aliased(Job)
        team_running = select(func.count(running.id)).where(running.status == "running", running.workspace_id == Job.workspace_id).correlate(Job).scalar_subquery()
        same_reading_running = select(running.id).where(running.status == "running", or_(
            and_(Job.kind == "chat", running.kind == "chat", running.conversation_id == Job.conversation_id),
            and_(Job.kind != "chat", running.kind != "chat", running.reading_id == Job.reading_id),
        )).correlate(Job).exists()
        candidates = db.scalars(select(Job).where(Job.status == "queued", Job.cancel_requested.is_(False),
                                team_running < settings.max_jobs_per_workspace, ~same_reading_running)
                                .order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(100)).all()
        for job in candidates:
            if any((j.kind == "chat" and job.kind == "chat" and j.conversation_id == job.conversation_id) or
                   (j.kind != "chat" and job.kind != "chat" and j.reading_id == job.reading_id) for j in active):
                continue
            if sum(j.workspace_id == job.workspace_id for j in active) >= settings.max_jobs_per_workspace:
                continue
            if job.execution_engine != AGENT_ENGINE and job.attempt >= settings.max_attempts:
                job.status, job.stage, job.error = "failed", "failed", "Automatic attempt budget exhausted"
                _event(db, job, "failed", {"status": "failed", "message": job.error})
                continue
            job.status, job.stage = "running", "starting"
            job.attempt += 1
            job.lease_token = new_id()
            job.lease_expires_at = moment + timedelta(seconds=settings.lease_seconds)
            job.updated_at, job.error = moment, None
            _event(db, job, "progress", {"stage": "starting", "progress": job.progress, "attempt": job.attempt})
            db.flush()
            db.expunge(job)
            return job
    return None


def heartbeat(job_id: str, token: str) -> bool:
    """An expired lease cannot be revived, even before another worker claims it."""
    moment = now()
    with SessionLocal() as db, db.begin():
        result = db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.lease_token == token,
                                             Job.lease_expires_at > moment, Job.cancel_requested.is_(False))
                            .values(lease_expires_at=moment + timedelta(seconds=get_settings().lease_seconds), updated_at=moment))
        return result.rowcount == 1


def record_usage(job_id: str, token: str, delta: dict) -> dict:
    """Persist every provider usage delta before another model request begins."""
    with SessionLocal() as db, db.begin():
        job = _owned_job(db, job_id, token)
        usage = dict(job.usage or {})
        attempts = {key: dict(value) for key, value in usage.get("attempts", {}).items()}
        attempt = attempts.setdefault(str(job.attempt), {})
        for key, value in delta.items():
            if key == "attempts" or not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or not math.isfinite(value):
                continue
            usage[key] = usage.get(key, 0) + value
            attempt[key] = attempt.get(key, 0) + value
        usage["attempts"] = attempts
        job.usage, job.updated_at = usage, now()
        if job.kind == "chat":
            from .chat_jobs import sync_chat_job
            sync_chat_job(db, job)
        return usage

def _owned_job(db, job_id: str, token: str) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None or job.status != "running" or job.lease_token != token or not job.lease_expires_at or job.lease_expires_at <= now():
        raise LeaseLost("Worker lease no longer owns this task")
    if job.cancel_requested:
        raise TaskCancelled("Task cancelled")
    return job


def report_progress(job_id: str, token: str, stage: str, message: str = "", progress: int | None = None) -> None:
    with SessionLocal() as db, db.begin():
        job = _owned_job(db, job_id, token)
        job.stage = str(stage)[:100]
        if progress is not None:
            job.progress = max(job.progress, min(99, max(0, int(progress))))
        job.updated_at = now()
        _event(db, job, "progress", {"stage": job.stage, "message": str(message)[:2000], "progress": job.progress})


def finish_failure(job_id: str, token: str, message: str, *, cancelled: bool = False) -> None:
    with SessionLocal() as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        # Cancellation may have set cancel_requested; ownership, expiry and token
        # are still required to prevent an old process changing a new attempt.
        if not job or job.status != "running" or job.lease_token != token or not job.lease_expires_at or job.lease_expires_at <= now():
            return
        cancelled = cancelled or job.cancel_requested
        if job.execution_engine == AGENT_ENGINE:
            stop_task_clock(db, job)
        job.status = "cancelled" if cancelled else "failed"
        job.stage, job.error = job.status, None if cancelled else message[:2000]
        job.lease_token, job.lease_expires_at, job.updated_at = None, None, now()
        _event(db, job, job.status, {"status": job.status, "message": "Task cancelled" if cancelled else job.error})


def release_for_shutdown(job_id: str, token: str) -> None:
    with SessionLocal() as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job and job.status == "running" and job.lease_token == token and job.lease_expires_at and job.lease_expires_at > now():
            if job.execution_engine == AGENT_ENGINE:
                stop_task_clock(db, job)
            job.status, job.stage = "queued", "recovering"
            job.lease_token, job.lease_expires_at, job.updated_at = None, None, now()
            _event(db, job, "message", {"message": "Worker stopped; saved task artifacts retained for recovery"})

def _safe_log_value(value: Any, api_key: str) -> Any:
    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED]") if api_key else value
    if isinstance(value, dict):
        return {k: _safe_log_value(v, api_key) for k, v in value.items() if k.lower() not in {"api_key", "authorization", "encrypted_api_key"}}
    if isinstance(value, list):
        return [_safe_log_value(v, api_key) for v in value]
    return value


def _rewrite_checkpoint_paths(work_dir: Path, previous_root: Path) -> None:
    if previous_root == work_dir:
        return
    for path in work_dir.rglob("*"):
        if path.is_symlink():
            raise PipelineError("Checkpoint contains a symbolic link")
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md", ".txt", ".org", ".yaml"}:
            if path.stat().st_size <= 32 * 1024 * 1024:
                content = path.read_text(errors="replace")
                if str(previous_root) in content:
                    path.write_text(content.replace(str(previous_root), str(work_dir)))


def build_spec(job: Job, work_dir: Path) -> tuple[dict, str]:
    settings = get_settings()
    with SessionLocal() as db:
        reading = db.scalar(select(Reading).where(Reading.id == job.reading_id, Reading.workspace_id == job.workspace_id))
        if not reading:
            raise PipelineError("Reading is unavailable in the task workspace")
        paper = db.scalar(select(Paper).where(Paper.id == reading.paper_id, Paper.workspace_id == job.workspace_id))
        connection = db.scalar(select(ModelConnection).where(ModelConnection.id == reading.model_connection_id,
                                                             ModelConnection.workspace_id == job.workspace_id))
        if not paper or not connection:
            raise PipelineError("Paper or model connection is unavailable")
        source_path = None
        if paper.storage_path:
            source_path = str(task_path(settings.runtime_dir / "storage" / "workspaces" / job.workspace_id,
                                        str(settings.runtime_dir / paper.storage_path), must_exist=True))
        previous = None
        base_id = job.input_snapshot.get("base_revision_id")
        if base_id:
            revision = db.scalar(select(Revision).where(Revision.id == base_id, Revision.reading_id == reading.id,
                                                        Revision.workspace_id == job.workspace_id))
            if revision is None or reading.current_revision_id != revision.id:
                raise PipelineError("Revision base is stale; refresh the reading before retrying")
            previous = {"id": revision.id, "brief_markdown": revision.brief_markdown, "full_markdown": revision.full_markdown,
                        "evidence_index": revision.evidence_index, "verification": revision.verification,
                        "provenance": revision.provenance, "reading_packet": revision.reading_packet, "artifacts": []}
            if not (work_dir / "source_corpus.json").exists():
                for artifact in revision.artifacts:
                    original = task_path(settings.runtime_dir / "storage" / "workspaces" / job.workspace_id,
                                         str(settings.runtime_dir / artifact["path"]), must_exist=True)
                    relative = artifact.get("name", original.name)
                    target = task_path(work_dir, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
                    previous["artifacts"].append({"name": relative, "path": relative})
                origin = (revision.provenance or {}).get("checkpoint_origin")
                if origin:
                    _rewrite_checkpoint_paths(work_dir, settings.runtime_dir / origin)
            else:
                previous["artifacts"] = [{"name": p.name, "path": str(p.relative_to(work_dir))} for p in work_dir.rglob("*") if p.is_file()]
        skill_id = job.input_snapshot.get("skill_id", reading.skill_id)
        if skill_id not in {"deeppapernote", "ljg-paper"}:
            raise PipelineError("Unsupported skill selection")
        skill_root = settings.project_dir / "skills" / skill_id
        if not (skill_root / "SKILL.md").is_file():
            raise PipelineError("Pinned reading skill is unavailable")
        model = dict(job.input_snapshot["model"])
        # Jobs created before thinking-format support implicitly used the default.
        model.setdefault("thinking_format", "default")
        frozen_fields = ("id", "name", "base_url", "model", "protocol", "thinking_format", "vision", "context_window", "max_tokens")
        if any(model.get(field) != getattr(connection, field) for field in frozen_fields):
            raise PipelineError("Model configuration changed; retry task to use current configuration")
        api_key = decrypt_secret(connection.encrypted_api_key)
        model["allow_private_models"] = settings.allow_private_models
        model["api_key"] = api_key
        mode = job.input_snapshot.get("workflow_mode", "native")
        workflow_version = job.input_snapshot.get("workflow_version", "guided-v2.3")
        source_rules = job.input_snapshot.get("source_rules_version", "source-sections-v2")
        if mode == "guided" and (workflow_version, source_rules) not in {
            ("guided-v2.3", "source-sections-v2"), ("guided-v3.0", "source-structured-v3")
        }:
            raise PipelineError("Frozen workflow or source rules are unsupported")
        spec = {"type": "run", "run_id": job.id, "kind": job.kind, "skill_id": skill_id,
                "skill_root": str(skill_root), "work_dir": str(work_dir), "model": model,
                "workflow_mode": mode, "workflow_version": workflow_version, "source_rules_version": source_rules,
                "paper": {"id": paper.id, "title": paper.title, "source_type": paper.source_type,
                          "source_url": paper.source_url, "storage_path": source_path},
                "template": job.input_snapshot["template"], "instruction": job.input_snapshot.get("instruction", ""),
                "previous_revision": previous,
                "limits": {"timeout_seconds": settings.job_timeout_seconds, "max_tool_calls": settings.max_tool_calls, "max_related_papers": 3}}
        if workflow_version == "guided-v3.0" and mode == "guided":
            spec["limits"].update(job.input_snapshot.get("workflow_limits", {}))
        return spec, api_key


def publish_result(job_id: str, token: str, result: dict, pipeline: Pipeline, usage: dict) -> str:
    settings = get_settings()
    with SessionLocal() as mode_db:
        mode_job = mode_db.get(Job, job_id)
        mode = mode_job.input_snapshot.get("workflow_mode", "native") if mode_job else "native"
        is_agent = bool(mode_job and mode_job.execution_engine == AGENT_ENGINE)
    workflow = getattr(pipeline, "guided_workflow", None)
    if is_agent:
        from .agent_tools import AgentTools
        agent_tools = getattr(pipeline, "agent_tools", None)
        if (not isinstance(agent_tools, AgentTools) or agent_tools.pipeline is not pipeline
                or getattr(agent_tools.store, "job_id", None) != job_id
                or getattr(agent_tools.store, "lease_token", None) != token):
            raise PipelineError("Agent publication requires authentic task-bound host tools")
        trusted = agent_tools.final_result(getattr(pipeline, "agent_submission", None))
        if trusted != result:
            raise PipelineError("Agent product changed after the host-issued final submission")
        cross_requested = getattr(pipeline, "agent_cross_requested", False)
        clean, errors = validate_result(result, pipeline.library, pipeline.native_checks,
                                        workflow_mode="guided", cross_paper_requested=cross_requested)
        deterministic = clean["verification"]
        deterministic.pop("agent_report", None)
        deterministic["source_coverage"] = deterministic.pop("coverage")
        # The result above came from host tools, not a model-authored verdict.
        # Keep full review bindings, coverage and evidence checks at the top level.
        clean["verification"] = {**deepcopy(trusted["verification"]), **deterministic,
                                 "execution_engine": AGENT_ENGINE}
        clean["verification"]["limitations"] = list(dict.fromkeys([
            *trusted["verification"].get("limitations", []), *deterministic.get("limitations", [])]))
    elif mode == "guided":
        if workflow is None or workflow.job.id != job_id:
            raise PipelineError("Guided publication requires an authentic host-owned workflow")
        workflow.verify_completion(result)
        clean, errors = validate_result(result, pipeline.library, pipeline.native_checks,
                                        workflow_mode="guided", cross_paper_requested=workflow.cross_requested)
        errors.extend(workflow.cross_paper_errors())
        if workflow.cross_paper_errors():
            clean["verification"]["cross_paper_status"] = "partial"
        clean["verification"].update({"workflow": {"mode": "guided", "native_status": "not_requested",
                                                  "stages": list(workflow.completed.values())},
                                     "fact_review": workflow.fact_review, "product_review": workflow.product_review})
        if hasattr(workflow, "coverage_projection"):
            clean["verification"].update(workflow.coverage_projection())
    else:
        clean, errors = validate_result(result, pipeline.library, pipeline.native_checks)
        # Native authenticity remains a hard gate, including legacy snapshots.
        pipeline.verify_native_seal()
        clean["verification"]["workflow"] = {"mode": "native", "native_status": "completed", "stages": []}
        if not any(d.kind == "external" for d in pipeline.library.documents.values()):
            clean["verification"]["limitations"].append("Cross-paper full-text verification was not completed")
    if not pipeline.inspected_skill_files and not result.get("provenance", {}).get("native_reused"):
        errors.append("No upstream skill guidance was inspected")
    clean["verification"]["errors"] = list(dict.fromkeys(errors))
    if is_agent and errors:
        raise PipelineError("Agent publication rejected: " + "; ".join(clean["verification"]["errors"]))
    (pipeline.work_dir / "server-verification.json").write_text(json.dumps(clean["verification"], ensure_ascii=False, indent=2))
    files = pipeline.list_artifacts()["files"]
    # Validate even model-requested artifacts that are not otherwise emitted.
    for artifact in clean.get("artifacts", []):
        if not isinstance(artifact, dict) or "path" not in artifact:
            raise PipelineError("Invalid result artifact declaration")
        task_path(pipeline.work_dir, artifact["path"], must_exist=True)
    target = None
    target_created = False
    try:
        with SessionLocal() as db, db.begin():
            job = _owned_job(db, job_id, token)
            reading = db.scalar(select(Reading).where(Reading.id == job.reading_id, Reading.workspace_id == job.workspace_id).with_for_update())
            if reading is None:
                raise PipelineError("Reading was removed")
            base_id = job.input_snapshot.get("base_revision_id")
            if reading.current_revision_id != base_id:
                raise PipelineError("Reading changed during this task; result was not published")
            revision_id = new_id()
            target = settings.runtime_dir / "storage" / "workspaces" / job.workspace_id / "readings" / reading.id / revision_id
            target.mkdir(parents=True, exist_ok=False)
            target_created = True
            artifacts = []
            for item in files:
                if item["path"].startswith((".tmp/", ".cache/")):
                    continue
                original = task_path(pipeline.work_dir, item["path"], must_exist=True)
                destination = task_path(target, item["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
                artifacts.append({"name": item["path"], "path": str(destination.relative_to(settings.runtime_dir)), "mime_type": item["mime_type"]})
            # Copying large artifacts must not make a once-valid lease publishable
            # after expiry. No heartbeat can acquire this row while it is locked.
            if job.lease_expires_at <= now():
                raise LeaseLost("Lease expired while preparing publication")
            number = (db.scalar(select(func.max(Revision.number)).where(Revision.reading_id == reading.id)) or 0) + 1
            provenance = {**clean["provenance"], "job_id": job.id, "attempt": job.attempt, "skill_id": job.input_snapshot["skill_id"],
                          "template_version_id": job.input_snapshot["template"]["version_id"],
                          "model": job.input_snapshot["model"], "usage": usage,
                          "checkpoint_origin": str(pipeline.work_dir.relative_to(settings.runtime_dir)),
                          "inspected_skill_files": sorted(pipeline.inspected_skill_files),
                          "inspected_artifacts": sorted(pipeline.inspected_artifacts),
                          "source_manifest": [d.manifest() for d in pipeline.library.documents.values()]}
            if is_agent:
                provenance["execution_engine"] = AGENT_ENGINE
            revision = Revision(id=revision_id, reading_id=reading.id, workspace_id=job.workspace_id, number=number,
                                parent_revision_id=base_id, instruction=job.input_snapshot.get("instruction", ""),
                                brief_markdown=clean["brief_markdown"], full_markdown=clean["full_markdown"],
                                evidence_index=clean["evidence_index"], verification=clean["verification"],
                                provenance=provenance, artifacts=artifacts,
                                reading_packet=result.get("reading_packet", {}) if is_agent or mode == "guided" else {})
            db.add(revision)
            reading.current_revision_id = revision_id
            if is_agent:
                job.status = "succeeded"
            else:
                job.status = "partial" if errors or clean["verification"]["cross_paper_status"] == "not_completed" else "succeeded"
            job.stage, job.progress, job.usage = job.status, 100, usage
            if is_agent:
                stop_task_clock(db, job)
            job.updated_at, job.lease_token, job.lease_expires_at = now(), None, None
            _event(db, job, "completed", {"status": job.status, "revision_id": revision_id, "progress": 100,
                                         "verification_errors": clean["verification"]["errors"]})
            # Persist the downloaded arXiv original for the authorized file route.
            paper = db.scalar(select(Paper).where(Paper.id == reading.paper_id, Paper.workspace_id == job.workspace_id).with_for_update())
            main = pipeline.library.documents["main"]
            if paper and not paper.storage_path:
                matching = next((a for a in artifacts if a["name"] == main.path), None)
                if matching:
                    paper.storage_path, paper.sha256 = matching["path"], main.sha256
                    paper.metadata_json = {**(paper.metadata_json or {}), "coverage": main.coverage,
                                           "source_mime_type": matching["mime_type"]}
        return revision_id
    except Exception:
        # A connection failure during COMMIT can have an unknown outcome. Keep
        # immutable files rather than delete data that PostgreSQL may reference.
        # An unreferenced directory is unreachable through authenticated APIs.
        raise


def copy_recovery_artifacts(job: Job, token: str, work_dir: Path) -> list[dict]:
    """Only host-authorized same-tenant/same-reading jobs can supply checkpoints."""
    settings = get_settings()
    previous_dir = None
    with SessionLocal() as db, db.begin():
        current = _owned_job(db, job.id, token)
        db.scalar(select(Reading).where(Reading.id == current.reading_id, Reading.workspace_id == current.workspace_id).with_for_update())
        records = [{**entry, "job_id": job.id} for entry in current.stage_records or []]
        if job.attempt > 1:
            previous_dir = work_dir.parent / str(job.attempt - 1)
        elif current.input_snapshot.get("retry_of_job_id"):
            previous = db.scalar(select(Job).where(Job.id == current.input_snapshot["retry_of_job_id"],
                                  Job.workspace_id == current.workspace_id, Job.reading_id == current.reading_id))
            if previous is None or previous.status not in {"failed", "cancelled", "partial"}:
                raise PipelineError("Checkpoint retry source is unavailable in this reading/workspace")
            previous_dir = settings.runtime_dir / "tmp" / previous.id / str(previous.attempt)
            records += [{**entry, "job_id": previous.id} for entry in previous.stage_records or []]
        if previous_dir and previous_dir.is_dir():
            if previous_dir.is_symlink() or any(p.is_symlink() for p in previous_dir.rglob("*")):
                raise PipelineError("Checkpoint contains a symbolic link")
            shutil.copytree(previous_dir, work_dir, dirs_exist_ok=True)
            _rewrite_checkpoint_paths(work_dir, previous_dir)
        if current.lease_expires_at <= now():
            raise LeaseLost("Lease expired while copying workflow checkpoints")
    return records


def record_workflow_stage(job_id: str, token: str, pipeline: Pipeline, name: str, status: str, **values) -> dict:
    # Concurrent segment processes share one source library and fenced job row.
    # Hold the same lock used by RPCs while taking a source checkpoint.
    lock = getattr(pipeline, "state_lock", None)
    if lock is None:
        return _record_workflow_stage(job_id, token, pipeline, name, status, **values)
    with lock:
        return _record_workflow_stage(job_id, token, pipeline, name, status, **values)


def _record_workflow_stage(job_id: str, token: str, pipeline: Pipeline, name: str, status: str, **values) -> dict:
    """Atomically anchor a host checkpoint in the currently owned database row."""
    from .workflow import canonical, digest
    if status not in {"running", "succeeded", "failed", "reused", "skipped"} or not name.replace("_", "").isalnum():
        raise PipelineError("Invalid host workflow stage")
    output = values.pop("output", None)
    with SessionLocal() as db, db.begin():
        job = _owned_job(db, job_id, token)
        rows = deepcopy(job.stage_records or [])
        prior = next((entry for entry in reversed(rows) if entry.get("name") == name and entry.get("attempt") == job.attempt), {})
        moment = now().isoformat()
        record = {**prior, "name": name, "status": status, "attempt": job.attempt,
                  "job_id": job.id, **values}
        if status == "running":
            record["started_at"] = moment
            record.pop("completed_at", None); record.pop("error", None)
        if status in {"succeeded", "reused"}:
            if output is None:
                raise PipelineError("A completed stage must have validated output")
            source = pipeline.library.export_checkpoint()
            record["output_hash"], record["source_hash"] = digest(output), digest(source)
            record["completed_at"] = moment
            if name in {"source_inventory", "segment_plan", "section_coverage", "figure_checks"}:
                record["summary"] = output
            directory = pipeline.work_dir / "host-workflow"
            directory.mkdir(exist_ok=True)
            target = task_path(directory, name + ".json")
            temporary = directory / (name + ".json.tmp")
            temporary.write_text(canonical({"input_hash": record["input_hash"], "output_hash": record["output_hash"],
                                            "output": output, "source_checkpoint": source, "source_hash": record["source_hash"]}))
            temporary.replace(target)
        elif status in {"failed", "skipped"}:
            record["completed_at"] = moment
        rows = [entry for entry in rows if not (entry.get("name") == name and entry.get("attempt") == job.attempt)]
        rows.append(record)
        job.stage_records, job.stage, job.updated_at = rows, name, now()
        milestones = {"source": 10, "revision_plan": 15, "extract": 30, "packet_repair": 33, "packet_validate": 35,
                      "fact_review": 50, "fact_repair": 52, "fact_review_final": 58, "compose": 75,
                      "product_review": 88, "product_repair": 90, "product_review_final": 95,
                      "source_inventory": 10, "segment_plan": 12, "merge_review": 72, "merge_review_final": 73}
        if status in {"succeeded", "reused"}:
            job.progress = max(job.progress, milestones.get(name, job.progress))
            if name == "section_coverage" and output.get("total", 0):
                job.progress = max(job.progress, 12 + int(43 * output["processed"] / output["total"]))
            elif name == "figure_checks":
                total = output.get("total", 0)
                job.progress = max(job.progress, 55 + int(15 * output.get("verified", 0) / total) if total else 70)
        _event(db, job, "progress", {"stage": name, "stage_status": status, "progress": job.progress})
        if job.lease_expires_at <= now():
            raise LeaseLost("Lease expired while saving workflow checkpoint")
        return deepcopy(record)


def run_runner_process(job: Job, token: str, spec: dict, pipeline: Pipeline, cancelled: threading.Event,
                       deadline: float, secret: str, usage_delta) -> dict:
    """One fresh JSONL process; shared host lease, source library and budgets."""
    settings = get_settings()
    runner_path = settings.project_dir / "agent-runner/dist/index.js"
    if not runner_path.is_file():
        raise PipelineError("Agent runner is not built; run the documented build step")
    work_dir = pipeline.work_dir
    phase = (spec.get("phase") or {}).get("name")
    chat = spec.get("kind") == "chat"
    chat_operation = spec.get("chat", {}).get("operation") if chat else None
    phase_deadline = min(deadline, time.monotonic() + spec["limits"]["timeout_seconds"])
    phase_calls = 0
    phase_submission_seen = False
    phase_cap = spec["limits"]["max_tool_calls"]
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8", "NODE_ENV": "production",
                   "TMPDIR": str(work_dir), "XDG_CONFIG_HOME": str(work_dir / ".config"), "XDG_CACHE_HOME": str(work_dir / ".cache")}
    process = subprocess.Popen(["node", str(runner_path)], cwd=work_dir, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
    selector = selectors.DefaultSelector()
    buffer, result, requests_seen = b"", None, set()
    try:
        process.stdin.write((json.dumps(spec, ensure_ascii=False) + "\n").encode()); process.stdin.flush()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while True:
            if cancelled.is_set():
                raise TaskCancelled("Task cancelled or worker lease lost")
            if getattr(pipeline, "segment_abort", threading.Event()).is_set():
                raise PipelineError("Parallel segment stopped after another segment failed; completed checkpoints retained")
            if time.monotonic() >= phase_deadline:
                raise BudgetExhausted("Agent phase exceeded its configured time budget")
            if chat:
                pipeline.chat_flush()
            for key, _ in selector.select(timeout=0.1 if chat else 0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj); continue
                if key.data == "stderr":
                    continue
                buffer += chunk
                if len(buffer) > MAX_PROTOCOL_LINE and b"\n" not in buffer:
                    raise PipelineError("Runner protocol line exceeds size limit")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) > MAX_PROTOCOL_LINE:
                        raise PipelineError("Runner protocol line exceeds size limit")
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as exc:
                        raise PipelineError("Runner emitted invalid JSONL") from exc
                    if not isinstance(event, dict):
                        raise PipelineError("Runner emitted invalid protocol event")
                    kind = event.get("type")
                    if kind == "progress":
                        safe = _safe_log_value(event, secret)
                        if chat:
                            pipeline.chat_emit({"type": "progress", "stage": chat_operation, "message": safe.get("message", "")})
                        else:
                            report_progress(job.id, token, phase or safe.get("stage", "reading"), safe.get("message", ""),
                                            None if phase else safe.get("progress"))
                    elif kind == "chat_delta" and chat and chat_operation == "answer":
                        if not isinstance(event.get("text"), str) or result is not None:
                            raise PipelineError("Invalid chat text event")
                        pipeline.chat_emit({"type": "delta", "text": _safe_log_value(event["text"], secret)})
                    elif kind == "model_output" and spec.get("workflow_version") == "guided-v3.0":
                        # Model messages are diagnostics, never workflow authority.
                        # Retain failed/truncated outputs without recording stdin,
                        # credentials, source-image bytes or request headers.
                        safe = _safe_log_value({"phase": phase, "content": event.get("content"),
                                                "stop_reason": event.get("stop_reason"), "error": event.get("error")}, secret)
                        directory = work_dir / "host-model-output"
                        directory.mkdir(exist_ok=True)
                        stage_name = str(spec.get("phase", {}).get("checkpoint_name", phase or "reading"))
                        if not stage_name.replace("_", "").isalnum():
                            raise PipelineError("Invalid model diagnostic stage")
                        target = task_path(directory, stage_name + ".jsonl")
                        encoded = (json.dumps(safe, ensure_ascii=False) + "\n").encode()
                        if len(encoded) > MAX_PROTOCOL_LINE or (target.stat().st_size if target.exists() else 0) + len(encoded) > 8 * 1024 * 1024:
                            raise PipelineError("Model diagnostic output exceeds its bounded retention quota")
                        with target.open("ab") as output:
                            output.write(encoded)
                    elif kind == "usage":
                        delta = _safe_log_value(event.get("usage", {}), secret)
                        if isinstance(delta, dict):
                            delta.pop("tool_calls", None)
                            usage_delta(delta)
                    elif kind == "tool_request":
                        if result is not None:
                            raise PipelineError("Runner requested a tool after submitting its result")
                        request_id = str(event.get("id", ""))
                        if not request_id or request_id in requests_seen:
                            raise PipelineError("Invalid or repeated tool request identifier")
                        requests_seen.add(request_id)
                        phase_calls += 1
                        if (phase or chat) and phase_calls > phase_cap:
                            raise BudgetExhausted("Per-phase tool call budget exhausted")
                        tool = event.get("name", "")
                        args = event.get("args", {})
                        if not isinstance(args, dict):
                            raise PipelineError("Invalid tool arguments")
                        if tool == "host_phase_submission" and (not phase or args):
                            raise PipelineError("Phase submission accounting requires a guided phase and empty arguments")
                        if tool == "host_chat_submission" and (not chat or args):
                            raise PipelineError("Chat submission accounting requires a chat task and empty arguments")
                        if chat:
                            allowed = {"host_chat_submission"}
                            if chat_operation in {"answer", "review"}:
                                allowed.update({"read_source", "search_source"})
                            if chat_operation == "answer":
                                allowed.update({"search_papers", "fetch_related", "read_skill_file"})
                            if tool not in allowed:
                                raise PipelineError("Tool is not permitted in this chat operation")
                        if phase and tool != "host_phase_submission":
                            allowed = {"read_source", "read_skill_file"}
                            questions = spec["phase"]["input"].get("packet", {}).get("open_questions", [])
                            if phase in {"fact_review", "merge_review"} and questions:
                                allowed.update({"search_papers", "fetch_related"})
                            if phase in {"figure_review", "merge_review", "product_review"} and spec["model"].get("vision"):
                                allowed.add("read_visual")
                                if tool == "read_visual" and args.get("figure_id") not in {
                                    f["id"] for f in spec["phase"]["input"].get("figures", [])
                                }:
                                    raise PipelineError("Visual request is outside this phase's supplied figures")
                            if tool not in allowed:
                                raise PipelineError("Tool is not permitted in this guided phase")
                            if tool == "search_papers" and args.get("query") not in {q["search_query"] for q in questions}:
                                raise PipelineError("Related search must address an explicit packet question")
                        response = {"type": "tool_response", "id": request_id}
                        pipeline.state_lock.acquire()
                        try:
                            if tool in {"host_phase_submission", "host_chat_submission"}:
                                # Trusted runner RPC precedes its local submission
                                # validation, so invalid executed attempts also count.
                                pipeline._cancel_check()
                                if pipeline.tool_calls >= pipeline.max_tool_calls:
                                    raise BudgetExhausted("Structured submission exceeded the total tool budget")
                                pipeline.tool_calls += 1
                                phase_submission_seen = True
                                response["result"] = {"accepted": True,
                                    "remaining_tool_calls": pipeline.max_tool_calls - pipeline.tool_calls}
                            elif not phase and not chat and tool in {"host_native_attest", "host_native_seal", "host_native_status", "host_native_review_lookup"}:
                                response["result"] = pipeline.host_call(tool, args)
                            else:
                                response["result"] = pipeline.call(tool, args)
                        except (TaskCancelled, BudgetExhausted):
                            raise
                        except (PipelineError, SourceError, ValueError, OSError) as exc:
                            response["error"] = _safe_log_value(str(exc)[:2000], secret)
                        finally:
                            pipeline.state_lock.release()
                        usage_delta({"tool_calls": 1})
                        process.stdin.write((json.dumps(response, ensure_ascii=False) + "\n").encode()); process.stdin.flush()
                    elif kind == "result":
                        if result is not None:
                            raise PipelineError("Runner submitted multiple results")
                        if (phase or chat) and not phase_submission_seen:
                            # Older result-only runners/fixtures retain one final
                            # submission charge; RPC-aware runners already paid.
                            with pipeline.state_lock:
                                phase_calls += 1
                                pipeline.tool_calls += 1
                                if phase_calls > phase_cap or pipeline.tool_calls > pipeline.max_tool_calls:
                                    raise BudgetExhausted("Structured submission exceeded the tool call budget")
                            usage_delta({"tool_calls": 1})
                        result = _safe_log_value(event.get("result"), secret)
                    elif kind == "error":
                        raise PipelineError(_safe_log_value(str(event.get("message", "Agent failed")), secret)[:2000])
                    else:
                        raise PipelineError("Unknown runner protocol event")
            if process.poll() is not None and not selector.get_map():
                if buffer.strip():
                    raise PipelineError("Runner ended with incomplete JSONL")
                break
        if process.returncode != 0 or not isinstance(result, dict):
            raise PipelineError("Agent runner exited before returning a complete result")
        return result
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(b'{"type":"cancel"}\n'); process.stdin.flush()
            except (OSError, ValueError):
                pass
        terminate_group(process)
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


class Worker:
    def __init__(self):
        self.stopping = threading.Event()
        self.current_cancel: threading.Event | None = None

    def stop(self, *_: Any) -> None:
        self.stopping.set()
        if self.current_cancel:
            self.current_cancel.set()

    def run_job(self, job: Job) -> None:
        if job.input_snapshot.get("execution_engine") == AGENT_ENGINE:
            from .agent_runtime import run_agent_job
            run_agent_job(self, job)
            return
        if job.kind == "chat":
            from .chat_jobs import run_chat_job
            run_chat_job(self, job)
            return
        settings = get_settings()
        work_dir = settings.runtime_dir / "tmp" / job.id / str(job.attempt)
        work_dir.mkdir(parents=True, exist_ok=True)
        cancelled, heartbeat_done, deadline_exceeded = threading.Event(), threading.Event(), threading.Event()
        self.current_cancel = cancelled
        token = job.lease_token
        secret = ""
        guard = None
        usage: dict = dict(job.usage or {})
        published = False
        started = time.monotonic()
        deadline = started + settings.job_timeout_seconds
        frozen_limits = job.input_snapshot.get("workflow_limits", {})
        if job.input_snapshot.get("workflow_version") == "guided-v3.0":
            deadline = started + max(frozen_limits.get("timeout_seconds", settings.job_timeout_seconds),
                                     frozen_limits.get("long_timeout_seconds", settings.long_job_timeout_seconds))
        usage_lock = threading.Lock()

        def renew() -> None:
            interval = min(2, settings.lease_seconds / 3)
            while not heartbeat_done.wait(interval):
                if time.monotonic() >= deadline:
                    deadline_exceeded.set(); cancelled.set(); return
                try:
                    if not heartbeat(job.id, token):
                        cancelled.set(); return
                except Exception:
                    cancelled.set(); return

        def usage_delta(delta):
            nonlocal usage
            with usage_lock:
                usage = record_usage(job.id, token, _safe_log_value(delta, secret))

        try:
            guard = threading.Thread(target=renew, name="job-lease-heartbeat", daemon=True)
            guard.start()
            anchors = copy_recovery_artifacts(job, token, work_dir)
            spec, secret = build_spec(job, work_dir)
            # Prior published stages are immutable and authorized by build_spec's
            # same-reading/workspace/current-base lookup.
            previous = spec.get("previous_revision") or {}
            anchors += list((previous.get("provenance") or {}).get("workflow", {}).get("stages", []))
            pipeline = Pipeline(work_dir, Path(spec["skill_root"]), spec["paper"], spec["skill_id"], job.workspace_id,
                                cancelled, settings.project_dir, bool(spec["model"].get("vision")),
                                max_tool_calls=spec["limits"]["max_tool_calls"],
                                workflow_mode=spec["workflow_mode"], rules_version=spec["source_rules_version"],
                                recover_invalid_cache=spec["workflow_version"] == "guided-v3.0")
            pipeline.state_lock = getattr(pipeline, "state_lock", threading.RLock())
            pipeline.phase_local = threading.local()
            pipeline.segment_abort = threading.Event()
            def select_budget(long_document):
                nonlocal deadline
                limits = spec["limits"]
                seconds = limits.get("long_timeout_seconds", 2400) if long_document else limits["timeout_seconds"]
                calls = limits.get("long_max_tool_calls", 240) if long_document else limits["max_tool_calls"]
                deadline = started + seconds
                pipeline.max_tool_calls = calls
                return deadline
            pipeline.select_budget = select_budget
            if spec["workflow_mode"] == "guided":
                from .workflow import GuidedWorkflow
                def call_phase(name, inputs, timeout, tool_cap, output_cap):
                    phase_spec = deepcopy(spec)
                    phase_spec["phase"] = {"name": name, "input": inputs}
                    phase_spec["phase"]["checkpoint_name"] = getattr(pipeline.phase_local, "checkpoint_name", name)
                    phase_spec["model"]["max_tokens"] = output_cap
                    phase_spec["limits"] = {**spec["limits"], "timeout_seconds": timeout, "max_tool_calls": tool_cap}
                    local_usage = {}
                    pipeline.phase_local.usage = local_usage
                    pipeline.phase_usage = local_usage  # legacy sequential workflow
                    def phase_usage(delta):
                        for key, value in delta.items():
                            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                                local_usage[key] = local_usage.get(key, 0) + value
                        usage_delta(delta)
                    return run_runner_process(job, token, phase_spec, pipeline, cancelled, deadline, secret, phase_usage)
                def save_stage(name, status, **values):
                    return record_workflow_stage(job.id, token, pipeline, name, status,
                                                 **_safe_log_value(values, secret))
                if spec["workflow_version"] == "guided-v3.0":
                    from .long_workflow import StructuredWorkflow
                    workflow = StructuredWorkflow(job, spec, pipeline, call_phase, save_stage, anchors, deadline)
                else:
                    workflow = GuidedWorkflow(job, spec, pipeline, call_phase, save_stage, anchors, deadline)
                result = workflow.execute()
            else:
                result = run_runner_process(job, token, spec, pipeline, cancelled, deadline, secret, usage_delta)
            publish_result(job.id, token, result, pipeline, usage)
            published = True
        except TaskCancelled:
            if self.stopping.is_set():
                release_for_shutdown(job.id, token)
            elif deadline_exceeded.is_set():
                finish_failure(job.id, token, "Paper reading exceeded its configured time budget")
            else:
                finish_failure(job.id, token, "Task cancelled", cancelled=True)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (PipelineError, SourceError)) else "Task execution failed; inspect server diagnostics"
            finish_failure(job.id, token, _safe_log_value(message, secret))
            LOG.warning("Task %s failed (%s)", job.id, type(exc).__name__)
        finally:
            heartbeat_done.set()
            if guard:
                guard.join(timeout=3)
            self.current_cancel = None
            if published:
                shutil.rmtree(work_dir.parent, ignore_errors=True)
            self.cleanup_terminal_artifacts()

    def cleanup_terminal_artifacts(self) -> None:
        """Retain bounded diagnostics for seven days; never remove queued retries."""
        settings = get_settings()
        cutoff = now() - timedelta(days=7)
        def terminal_rows(statuses: list[str], older_than=None):
            cursor = None
            while True:
                query = select(Job).where(Job.status.in_(statuses))
                if older_than is not None:
                    query = query.where(Job.updated_at < older_than)
                if cursor is not None:
                    query = query.where(Job.id > cursor)
                with SessionLocal() as db:
                    page = db.scalars(query.order_by(Job.id).limit(200)).all()
                if not page:
                    return
                yield from page
                cursor = page[-1].id
        for job in terminal_rows(["failed", "cancelled", "succeeded", "partial"]):
            if job.execution_engine == AGENT_ENGINE and job.status in {"failed", "cancelled"}:
                # Agent continuation refers to retained source/draft files. Its
                # durable artifacts must never be pruned as legacy diagnostics.
                continue
            # The reading lock also serializes API retry creation and checkpoint
            # copying. Active dependents retain every parent checkpoint.
            with SessionLocal() as db, db.begin():
                reading = db.scalar(select(Reading).where(Reading.id == job.reading_id,
                                    Reading.workspace_id == job.workspace_id).with_for_update(skip_locked=True))
                if reading is None:
                    continue
                active = db.scalars(select(Job).where(Job.reading_id == job.reading_id,
                        Job.workspace_id == job.workspace_id, Job.status.in_(["queued", "running", "waiting"]))).all()
                if any(item.id == job.id or item.input_snapshot.get("retry_of_job_id") == job.id
                       or item.input_snapshot.get("retry_from_job_id") == job.id for item in active):
                    continue
                directory = settings.runtime_dir / "tmp" / job.id
                if not directory.is_dir() or directory.is_symlink():
                    continue
                if job.updated_at < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
                    continue
                if job.status not in {"failed", "cancelled"}:
                    continue
                attempts = sorted([p for p in directory.iterdir() if p.is_dir() and p.name.isdigit()], key=lambda p: int(p.name), reverse=True)
                for attempt in attempts[2:]:
                    shutil.rmtree(attempt, ignore_errors=True)
                for attempt in attempts[:2]:
                    for cache in (attempt / ".tmp", attempt / ".cache"):
                        if cache.is_dir() and not cache.is_symlink():
                            shutil.rmtree(cache, ignore_errors=True)
                    files = [path for path in attempt.rglob("*") if path.is_file() and not path.is_symlink()]
                    total = sum(path.stat().st_size for path in files)
                    limit = 192 * 1024 * 1024
                    removed = []
                    for path in sorted(files, key=lambda value: value.stat().st_size, reverse=True):
                        if total <= limit:
                            break
                        size = path.stat().st_size
                        removed.append({"path": str(path.relative_to(attempt)), "bytes": size})
                        path.unlink(); total -= size
                    if removed:
                        (attempt / "diagnostic-retention.json").write_text(json.dumps({"reason": "terminal diagnostics size cap", "removed": removed}, indent=2))

    def run(self, once: bool = False) -> None:
        while not self.stopping.is_set():
            job = claim_job()
            if job is None:
                if once:
                    return
                self.cleanup_terminal_artifacts()
                self.stopping.wait(2)
                continue
            self.run_job(job)
            if once:
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Claim and execute at most one task")
    args = parser.parse_args()
    from .logging_config import configure_logging
    configure_logging("worker")
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    from .paper_titles import process_one_title, run_title_worker
    if args.once:
        process_one_title()
        worker.run(once=True)
    else:
        titles = threading.Thread(target=run_title_worker, args=(worker.stopping,), daemon=True, name="paper-titles")
        titles.start()
        try:
            worker.run()
        finally:
            worker.stopping.set()
            titles.join(timeout=3)


if __name__ == "__main__":
    main()
