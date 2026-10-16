"""Real PostgreSQL integration for durable Agent claiming and formal publication.

Review callbacks are explicit isolated test fixtures; these tests do not claim
that a real model reviewed a paper. Source/evidence and host submission gates run.
"""
from datetime import timedelta
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.agent_state import AgentLeaseLost, AgentStore, agent_snapshot, ensure_agent_task
from app.db import SessionLocal
from app.models import AgentTask, Job, JobEvent, Reading, Revision, new_id, now
from app.pipeline import PipelineError, TaskCancelled
from app.worker import Worker, claim_job, finish_failure, publish_result, release_for_shutdown, LeaseLost
from test_agent_tools import fixture as tool_fixture, invoke, prepare, review_all
from test_worker_durability import worker_db, seed_job


def seed_agent():
    job, _ = seed_job()
    with SessionLocal() as db, db.begin():
        row = db.get(Job, job.id)
        row.input_snapshot = agent_snapshot({**row.input_snapshot, "workflow_mode": "guided"})
        ensure_agent_task(db, row)
    return job


def active_agent():
    seed_agent()
    job = claim_job()
    store = AgentStore(job.id, job.lease_token)
    store.start()
    return job, store


def ready_publication(job, root):
    fixture_root = root / "fixture"
    fixture_root.mkdir()
    agent, _, pipeline = tool_fixture(fixture_root)
    agent.store = AgentStore(job.id, job.lease_token)
    pipeline.workspace_id = job.workspace_id
    agent.spec["run_id"] = job.id
    prepare(agent)
    submission = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    assert submission["accepted"], submission
    pipeline.agent_tools, pipeline.agent_submission = agent, submission
    pipeline.agent_cross_requested = False
    result = agent.final_result(submission)
    return pipeline, result


def test_engine_dispatch_precedes_chat_legacy_handler(monkeypatch):
    module = ModuleType("app.agent_runtime")
    calls = []
    module.run_agent_job = lambda worker, job: calls.append((worker, job))
    monkeypatch.setitem(sys.modules, "app.agent_runtime", module)
    worker = Worker()
    job = SimpleNamespace(kind="chat", input_snapshot={"execution_engine": "pi-agent-v1"})
    worker.run_job(job)
    assert calls == [(worker, job)]


def test_crash_recovery_bypasses_legacy_attempt_cap_and_preserves_budget(worker_db):
    job, store = active_agent()
    store.mutate("finding_saved", {}, lambda state: state.update(findings={"C1": "accepted"}))
    store.consume("reserved-before-crash", "read_source")
    with SessionLocal() as db, db.begin():
        row = db.get(Job, job.id)
        row.attempt = 99
        row.lease_expires_at = now() - timedelta(seconds=1)
        task = db.get(AgentTask, job.id)
        task.clock_at = now() - timedelta(seconds=80)
        task.elapsed_seconds = 10
        old_clock = task.clock_at
    recovered = claim_job()
    assert recovered.id == job.id and recovered.attempt == 100
    assert recovered.lease_token != job.lease_token
    with SessionLocal() as db:
        task = db.get(AgentTask, job.id)
        assert task.clock_at == old_clock  # Preserve uncertainty until resume.
        assert task.elapsed_seconds == 10
        assert task.tool_calls == 1 and task.allocations == 1
    resumed = AgentStore(job.id, recovered.lease_token).start()
    assert 90 <= resumed["elapsed_seconds"] < 95
    assert resumed["state"]["findings"] == {"C1": "accepted"}
    assert resumed["tool_calls"] == 1
    with pytest.raises(AgentLeaseLost):
        store.load()


@pytest.mark.parametrize("ending", ["failed", "cancelled", "shutdown"])
def test_terminal_and_shutdown_clock_stop_is_fenced(worker_db, ending):
    job, store = active_agent()
    store.consume("call", "read_source")
    with SessionLocal() as db, db.begin():
        db.get(AgentTask, job.id).clock_at = now() - timedelta(seconds=20)
    finish_failure(job.id, "stale-owner", "must not change")
    release_for_shutdown(job.id, "stale-owner")
    with SessionLocal() as db:
        assert db.get(AgentTask, job.id).clock_at is not None
        assert db.get(Job, job.id).status == "running"
    if ending == "shutdown":
        release_for_shutdown(job.id, job.lease_token)
    else:
        finish_failure(job.id, job.lease_token, "fixture ending", cancelled=ending == "cancelled")
    with SessionLocal() as db:
        task, row = db.get(AgentTask, job.id), db.get(Job, job.id)
        assert task.clock_at is None and task.clock_lease_token is None
        assert 20 <= task.elapsed_seconds < 25 and task.tool_calls == 1
        assert row.status == ("queued" if ending == "shutdown" else ending)
    if ending == "shutdown":
        resumed = claim_job()
        assert resumed.id == job.id
        assert AgentStore(job.id, resumed.lease_token).start()["tool_calls"] == 1


def test_expired_cancelled_agent_stops_clock_without_new_attempt(worker_db):
    job, _ = active_agent()
    with SessionLocal() as db, db.begin():
        row = db.get(Job, job.id)
        row.cancel_requested = True
        row.lease_expires_at = now() - timedelta(seconds=1)
        db.get(AgentTask, job.id).clock_at = now() - timedelta(seconds=10)
    assert claim_job() is None
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == "cancelled"
        assert db.get(AgentTask, job.id).clock_at is None
        assert db.get(AgentTask, job.id).elapsed_seconds >= 10


def test_agent_publication_keeps_reviews_and_stops_clock_atomically(worker_db):
    job, store = active_agent()
    pipeline, result = ready_publication(job, worker_db)
    assert not hasattr(pipeline, "guided_workflow")
    revision_id = publish_result(job.id, job.lease_token, result, pipeline, {"input_tokens": 42})
    with SessionLocal() as db:
        revision = db.get(Revision, revision_id)
        row, task = db.get(Job, job.id), db.get(AgentTask, job.id)
        assert row.status == "succeeded"
        assert task.clock_at is None and task.tool_calls > 0
        assert revision.provenance["execution_engine"] == "pi-agent-v1"
        assert revision.reading_packet == result["reading_packet"]
        assert revision.verification["evidence_check"] == {"passed": True, "issues": []}
        assert revision.verification["reviews"] == result["verification"]["reviews"]
        assert revision.verification["coverage"] == result["verification"]["coverage"]
        assert revision.verification["server_evidence_check"] == "passed"
        assert revision.verification["cross_paper_status"] == "not_requested"
        assert revision.evidence_index[0]["verified"]
        assert db.get(Reading, job.reading_id).current_revision_id == revision_id
        assert len(list(db.scalars(select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.event_type == "completed")))) == 1
    with pytest.raises((AgentLeaseLost, LeaseLost)):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Revision.id))) == 1


@pytest.mark.parametrize("invalid", ["fake_tools", "different_job", "changed_result", "missing_skill", "cross_required"])
def test_agent_invalid_publication_never_creates_partial_revision(worker_db, invalid):
    job, _ = active_agent()
    pipeline, result = ready_publication(job, worker_db)
    if invalid == "fake_tools":
        pipeline.agent_tools = SimpleNamespace(final_result=lambda _: result, pipeline=pipeline)
    elif invalid == "different_job":
        pipeline.agent_tools.store.job_id = new_id()
    elif invalid == "changed_result":
        result = {**result, "full_markdown": result["full_markdown"] + "\nChanged after review."}
    elif invalid == "missing_skill":
        # Durable task state is authoritative: a restarted process legitimately
        # has no in-memory inspected_skill_files, so the real missing-guidance
        # case removes the recorded guidance read itself.
        pipeline.agent_tools.store.mutate("guidance_removed", {}, lambda state: state.get("skill_reads", {}).pop("SKILL.md", None))
        pipeline.inspected_skill_files.clear()
    else:
        pipeline.agent_cross_requested = True
    with pytest.raises(PipelineError):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Revision.id))) == 0
        assert db.get(Reading, job.reading_id).current_revision_id is None
        assert db.get(Job, job.id).status == "running"


def test_saved_review_cannot_publish_changed_document(worker_db):
    job, _ = active_agent()
    pipeline, result = ready_publication(job, worker_db)
    invoke(pipeline.agent_tools, "edit_draft", section_id="result", view="full", title="Results",
           content="Changed explanation [E1](evidence:E1)", expected_version=2)
    with pytest.raises(PipelineError, match="stale|changed|version"):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Revision.id))) == 0


def test_revision_base_conflict_retains_task_without_publication(worker_db):
    job, _ = active_agent()
    pipeline, result = ready_publication(job, worker_db)
    with SessionLocal() as db, db.begin():
        revision = Revision(id=new_id(), reading_id=job.reading_id, workspace_id=job.workspace_id,
                            number=1, brief_markdown="Concurrent revision", full_markdown="Concurrent revision")
        db.add(revision)
        db.get(Reading, job.reading_id).current_revision_id = revision.id
        existing_id = revision.id
    with pytest.raises(PipelineError, match="changed"):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert db.get(Reading, job.reading_id).current_revision_id == existing_id
        assert db.scalar(select(func.count(Revision.id))) == 1
        assert db.get(AgentTask, job.id).state["submission"]["accepted"]


def test_cancel_after_verified_submission_cannot_publish(worker_db):
    job, _ = active_agent()
    pipeline, result = ready_publication(job, worker_db)
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).cancel_requested = True
    with pytest.raises(TaskCancelled):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    finish_failure(job.id, job.lease_token, "cancelled", cancelled=True)
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Revision.id))) == 0
        assert db.get(AgentTask, job.id).clock_at is None


def test_cleanup_retains_resumable_agent_source_files(worker_db):
    job, _ = active_agent()
    path = worker_db / "tmp" / job.id / "1" / "accepted-source.txt"
    path.parent.mkdir(parents=True)
    path.write_text("Source needed by saved citations")
    finish_failure(job.id, job.lease_token, "saved for continuation")
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).updated_at = now() - timedelta(days=30)
    Worker().cleanup_terminal_artifacts()
    assert path.read_text() == "Source needed by saved citations"
