"""Real PostgreSQL durability, fencing and allocation accounting."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agent_state import AgentBudgetExhausted, AgentLeaseLost, AgentStore, grant_continuation
from app.db import SessionLocal
from app.models import AgentTask, AgentTaskEvent, Job, now
from test_api import env, create_reading


@pytest.fixture
def running(env, monkeypatch):
    monkeypatch.setenv("PAPER_READER_PI_AGENT_ENABLED", "true")
    from app.config import get_settings
    get_settings.cache_clear()
    created = create_reading(env, monkeypatch)
    with SessionLocal() as db, db.begin():
        job = db.get(Job, created["job_id"])
        job.status = "running"
        job.lease_token = "first-owner"
        job.lease_expires_at = now() + timedelta(minutes=5)
    store = AgentStore(created["job_id"], "first-owner")
    store.start()
    return store


def test_reservation_and_mutation_replay_are_atomic_and_independent(running):
    first = running.consume("call-1", "save_findings")
    assert running.consume("call-1", "save_findings") == first
    with pytest.raises(ValueError, match="another tool"):
        running.consume("call-1", "submit_final")
    count = []
    def change(state):
        count.append(1)
        state["findings"] = ["accepted"]
        return {"accepted": 1}
    assert running.mutate("finding_saved", {}, change, request_id="call-1") == {"accepted": 1}
    assert running.mutate("finding_saved", {}, change, request_id="call-1") == {"accepted": 1}
    assert count == [1]
    assert running.load()["tool_calls"] == 1
    with SessionLocal() as db:
        records = list(db.scalars(select(AgentTaskEvent).where(AgentTaskEvent.job_id == running.job_id).order_by(AgentTaskEvent.sequence)))
        assert [record.sequence for record in records] == list(range(1, len(records) + 1))
        assert sum(record.event_type == "tool_attempt" for record in records) == 1
        assert sum(record.event_type == "finding_saved" for record in records) == 1


def test_failing_mutation_rolls_back_state_and_journal(running):
    def fail(state):
        state["invalid"] = True
        raise ValueError("reject")
    with pytest.raises(ValueError, match="reject"):
        running.mutate("invalid", {}, fail, request_id="bad")
    assert "invalid" not in running.load()["state"]
    with SessionLocal() as db:
        assert not db.scalar(select(AgentTaskEvent.id).where(AgentTaskEvent.job_id == running.job_id, AgentTaskEvent.event_type == "invalid"))


def test_concurrent_replayed_tool_is_charged_once(running):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: running.consume("same-id", "read_source"), range(6)))
    assert all(result == results[0] for result in results)
    assert running.load()["tool_calls"] == 1


@pytest.mark.parametrize("operation", ["load", "start", "tick", "checkpoint", "mutate", "consume", "wait"])
def test_stale_worker_cannot_read_or_write_private_task(running, operation):
    with SessionLocal() as db, db.begin():
        db.get(Job, running.job_id).lease_token = "replacement-owner"
    args = {"checkpoint": ([],), "mutate": ("test", {}, lambda state: None), "consume": ("call", "tool"), "wait": ("stop",)}
    with pytest.raises(AgentLeaseLost):
        getattr(running, operation)(*args.get(operation, ()))


def test_expired_lease_is_fenced_even_before_another_worker_claims(running):
    with SessionLocal() as db, db.begin():
        db.get(Job, running.job_id).lease_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(AgentLeaseLost):
        running.consume("call", "tool")


def test_cancellation_blocks_new_effects_but_allows_trusted_checkpoint(running):
    running.consume("already-reserved", "write_draft")
    with SessionLocal() as db, db.begin():
        db.get(Job, running.job_id).cancel_requested = True
    with pytest.raises(AgentLeaseLost, match="cancellation"):
        running.consume("new-call", "write_draft")
    with pytest.raises(AgentLeaseLost, match="cancellation"):
        running.mutate("draft_saved", {}, lambda state: state.update(draft="too late"), request_id="already-reserved")
    with pytest.raises(AgentLeaseLost, match="cancellation"):
        running.wait("wait must not override cancellation")
    running.checkpoint([{"role": "user", "content": "last accepted boundary"}])
    assert "draft" not in running.load()["state"]


def test_restart_charges_interrupted_wall_time_and_retains_checkpoint(running):
    running.checkpoint([{"role": "user", "content": "Read this"}], "accepted summary")
    running.consume("before-crash", "read_source")
    with SessionLocal() as db, db.begin():
        task = db.get(AgentTask, running.job_id)
        task.clock_at = now() - timedelta(seconds=75)
        job = db.get(Job, running.job_id)
        job.lease_token = "replacement-owner"
    resumed = AgentStore(running.job_id, "replacement-owner").start()
    assert 75 <= resumed["elapsed_seconds"] < 80
    assert 3520 < resumed["remaining_seconds"] <= 3525
    assert resumed["tool_calls"] == 1
    assert resumed["summary"] == "accepted summary"
    assert resumed["messages"] == [{"role": "user", "content": "Read this"}]


def test_wait_and_explicit_continuation_grant_once_without_losing_work(running):
    running.mutate("save", {}, lambda state: state.update(findings=["accepted"]))
    running.consume("read-1", "read_source")
    running.wait("Need another allocation")
    with pytest.raises(AgentLeaseLost):
        running.tick()
    with SessionLocal() as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == running.job_id).with_for_update())
        assert job.status == "waiting" and job.lease_token is None
        task = grant_continuation(db, job)
        assert task.granted_seconds == 7200
        assert task.granted_tool_calls == 1000
        assert task.tool_calls == 1
        assert task.state["findings"] == ["accepted"]
        with pytest.raises(ValueError):
            grant_continuation(db, job)


@pytest.mark.parametrize("budget", ["seconds", "tools"])
def test_exhaustion_commits_accounting_without_reserving_extra_call(running, budget):
    with SessionLocal() as db, db.begin():
        task = db.get(AgentTask, running.job_id)
        if budget == "seconds":
            task.clock_at = now() - timedelta(seconds=3601)
        else:
            task.tool_calls = 500
    with pytest.raises(AgentBudgetExhausted):
        running.consume("over-budget", "tool")
    with SessionLocal() as db:
        task = db.get(AgentTask, running.job_id)
        assert task.elapsed_seconds == 3600 if budget == "seconds" else task.tool_calls == 500
        assert not db.scalar(select(AgentTaskEvent.id).where(AgentTaskEvent.request_id == "attempt:over-budget"))


@pytest.mark.parametrize("message", [
    {"role": "assistant", "stopReason": "length", "content": []},
    {"role": "assistant", "stopReason": "aborted", "content": []},
    {"role": "assistant", "content": [{"type": "toolCall", "arguments": "{incomplete"}]},
])
def test_incomplete_checkpoint_rejected_without_replacing_prior_boundary(running, message):
    running.checkpoint([{"role": "user", "content": "trusted boundary"}])
    with pytest.raises(ValueError):
        running.checkpoint([message])
    assert running.load()["messages"] == [{"role": "user", "content": "trusted boundary"}]
