"""Agent default routing, continuation permissions and private projections."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, inspect, select

from app.agent_state import AgentStore
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import AgentTask, ChatMessage, Job, JobEvent, ModelConnection, User, now
from test_api import env, create_reading, login, resources
from test_chat_api import chat, send, finish


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("PAPER_READER_PI_AGENT_ENABLED", "true")
    get_settings.cache_clear()


def stop_job(jid, *, accepted=True):
    with SessionLocal() as db, db.begin():
        job = db.get(Job, jid)
        job.status, job.lease_token = "running", "test-owner"
        job.lease_expires_at = now() + timedelta(minutes=5)
    store = AgentStore(jid, "test-owner")
    store.start()
    if accepted:
        store.mutate("finding_saved", {}, lambda state: state.update(accepted_work=["Checked E1"], findings=[{"private": "never in public projection"}]))
    store.consume("accepted-call", "read_source")
    store.wait("预算耗尽，已有成果已保存")


def test_new_guided_defaults_agent_and_feature_flag_preserves_native(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    with SessionLocal() as db:
        job = db.get(Job, created["job_id"])
        assert job.execution_engine == "pi-agent-v1"
        assert job.input_snapshot["workflow_limits"]["timeout_seconds"] == 3600
        assert job.input_snapshot["workflow_limits"]["max_tool_calls"] == 500
        assert db.get(AgentTask, job.id)
    monkeypatch.setenv("PAPER_READER_PI_AGENT_ENABLED", "false")
    get_settings.cache_clear()
    other = create_reading(env, monkeypatch)
    with SessionLocal() as db:
        assert db.get(Job, other["job_id"]).execution_engine == "legacy"
        assert db.get(AgentTask, other["job_id"]) is None
    model, paper = resources(env)
    response = env["client"].post(f"/api/v1/workspaces/{env['alpha']}/readings", json={"paper_id": paper["id"], "model_connection_id": model["id"], "template_id": env["builtin"], "skill_id": "deeppapernote", "workflow_mode": "native"})
    with SessionLocal() as db:
        assert db.get(Job, response.json()["job_id"]).execution_engine == "legacy"


def test_public_projection_continuation_and_sse_replay(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    stop_job(created["job_id"])
    url = f"/api/v1/workspaces/{env['alpha']}/jobs/{created['job_id']}"
    c = env["client"]
    public = c.get(url).json()
    assert public["status"] == "waiting"
    assert public["execution_engine"] == "pi-agent-v1"
    assert public["agent_task"]["can_continue"]
    assert public["agent_task"]["accepted_work"] == ["Checked E1"]
    assert "never in public projection" not in str(public)
    assert "event: waiting" in c.get(url + "/events?after=999999999").text
    response = c.post(url + "/continue")
    assert response.status_code == 202 and response.json()["job_id"] == created["job_id"]
    assert c.post(url + "/continue").status_code == 409
    public = c.get(url).json()
    assert public["agent_task"]["remaining_tool_calls"] == 999
    with SessionLocal() as db:
        assert db.get(AgentTask, created["job_id"]).state["findings"] == [{"private": "never in public projection"}]


def test_concurrent_continue_grants_one_allocation(env, monkeypatch):
    from app.main import app
    created = create_reading(env, monkeypatch)
    stop_job(created["job_id"])
    token = env["client"].cookies.get("paper_reader_session")
    url = f"/api/v1/workspaces/{env['alpha']}/jobs/{created['job_id']}/continue"
    def continue_once(_):
        with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver", "Cookie": f"paper_reader_session={token}"}) as client:
            return client.post(url).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(continue_once, range(4)))
    assert sorted(statuses) == [202, 409, 409, 409]
    with SessionLocal() as db:
        assert db.get(AgentTask, created["job_id"]).allocations == 2


@pytest.mark.parametrize("previous_status", ["waiting", "failed", "cancelled"])
async def test_reading_continuation_stream_ignores_old_terminal_events(env, monkeypatch, previous_status):
    from app.api import events
    created = create_reading(env, monkeypatch)
    stop_job(created["job_id"])
    with SessionLocal() as db, db.begin():
        job = db.get(Job, created["job_id"])
        job.status = previous_status
        db.add(JobEvent(job_id=job.id, workspace_id=env["alpha"], event_type=previous_status,
                        data={"status": previous_status}))
    url = f"/api/v1/workspaces/{env['alpha']}/jobs/{created['job_id']}"
    assert env["client"].post(url + "/continue").status_code == 202
    token = env["client"].cookies.get("paper_reader_session")
    class Request:
        headers = {}
        cookies = {"paper_reader_session": token}
        polls = 0
        async def is_disconnected(self):
            self.polls += 1
            return self.polls > 1
    with SessionLocal() as db:
        response = await events(env["alpha"], created["job_id"], Request(), after=0,
                                db=db, user=db.get(User, env["admin"]))
        chunks = [chunk async for chunk in response.body_iterator]
    text = "".join(chunks)
    assert "event: progress" in text
    assert all(f"event: {status}" not in text for status in ("waiting", "failed", "cancelled", "completed"))
    assert env["client"].get(url + "/events", headers={"Last-Event-ID": "-1"}).status_code == 422


def test_reading_repeated_wait_replay_uses_latest_allocation(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    stop_job(created["job_id"])
    url = f"/api/v1/workspaces/{env['alpha']}/jobs/{created['job_id']}"
    assert env["client"].post(url + "/continue").status_code == 202
    with SessionLocal() as db, db.begin():
        job = db.get(Job, created["job_id"])
        job.status, job.lease_token = "running", "second-owner"
        job.lease_expires_at = now() + timedelta(minutes=5)
    store = AgentStore(created["job_id"], "second-owner")
    store.start()
    store.mutate("finding_saved", {}, lambda state: state.update(accepted_work=["Checked E1", "Checked E2"]))
    store.wait("Second allocation is saved")
    replay = env["client"].get(url + "/events").text
    assert replay.count("event: waiting") == 1
    assert "Checked E2" in replay and "Second allocation is saved" in replay


def test_legacy_takeover_creates_job_preserves_history_and_refreshes_model(env, monkeypatch):
    monkeypatch.setenv("PAPER_READER_PI_AGENT_ENABLED", "false")
    get_settings.cache_clear()
    created = create_reading(env, monkeypatch)
    with SessionLocal() as db, db.begin():
        job = db.get(Job, created["job_id"])
        job.status = "failed"
        frozen = dict(job.input_snapshot)
        db.get(ModelConnection, job.input_snapshot["model"]["id"]).model = "updated-model"
    response = env["client"].post(f"/api/v1/workspaces/{env['alpha']}/jobs/{created['job_id']}/continue")
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] != created["job_id"]
    with SessionLocal() as db:
        old = db.get(Job, created["job_id"])
        new = db.get(Job, response.json()["job_id"])
        assert old.input_snapshot == frozen and old.status == "failed"
        assert new.input_snapshot["retry_from_job_id"] == old.id
        assert new.input_snapshot["model"]["model"] == "updated-model"
        assert new.execution_engine == "pi-agent-v1"


def test_continue_workspace_isolation_and_revision_conflict(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    stop_job(created["job_id"])
    c, url = env["client"], f"/api/v1/workspaces/{env['alpha']}/jobs/{created['job_id']}/continue"
    login(c, "bob")
    assert c.post(url).status_code == 404
    assert c.post(url.replace(env["alpha"], env["beta"])).status_code == 404
    login(c)
    with SessionLocal() as db, db.begin():
        job = db.get(Job, created["job_id"])
        job.kind = "revise"
        job.input_snapshot = {**job.input_snapshot, "base_revision_id": "outdated"}
    assert c.post(url).status_code == 409


def test_chat_same_job_continue_keeps_pair_draft_and_private_artifacts(chat):
    answer = send(chat)
    mid, jid = answer["assistant_message"]["id"], answer["job_id"]
    stop_job(jid)
    with SessionLocal() as db, db.begin():
        db.get(ChatMessage, mid).content = "待核验草稿"
    url = f"{chat['url']}/messages/{mid}"
    stream = chat["client"].get(url + "/events")
    assert "event: waiting" in stream.text and "待核验草稿" in stream.text
    response = chat["client"].post(url + "/continue")
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == jid
    assert response.json()["assistant_message"]["content"] == "待核验草稿"
    assert response.json()["assistant_message"]["agent_task"]["remaining_tool_calls"] == 239
    assert response.json()["assistant_message"]["agent_task"]["goal"] == "How does this method work?"
    with SessionLocal() as db:
        assert db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == chat["cid"])) == 2
        assert db.scalar(select(func.count(Job.id)).where(Job.conversation_id == chat["cid"])) == 1
    assert chat["client"].post(f"{chat['root']}/jobs/{jid}/continue").status_code == 404
    login(chat["client"], "admin")
    assert chat["client"].post(url + "/continue").status_code == 404


def test_chat_continuation_latest_answer_only(chat):
    first = send(chat)
    stop_job(first["job_id"])
    second = send(chat, "A new question")
    stop_job(second["job_id"])
    assert chat["client"].post(f"{chat['url']}/messages/{first['assistant_message']['id']}/continue").status_code == 409


def test_chat_continuation_replay_reports_current_outcome(chat):
    answer = send(chat)
    mid = answer["assistant_message"]["id"]
    finish(answer, status="failed", content="Earlier unverified draft")
    url = f"{chat['url']}/messages/{mid}"
    assert chat["client"].post(url + "/continue").status_code == 202
    finish(answer, content="Accepted continued answer")
    stream = chat["client"].get(url + "/events").text
    assert "event: completed" in stream and "Accepted continued answer" in stream
    assert "event: failed" not in stream


def test_legacy_chat_handover_retains_question_and_history(chat):
    answer = send(chat)
    finish(answer, status="failed", content="Historical draft")
    with SessionLocal() as db, db.begin():
        old = db.get(Job, answer["job_id"])
        old.input_snapshot = {key: value for key, value in old.input_snapshot.items() if key != "execution_engine"}
    response = chat["client"].post(f"{chat['url']}/messages/{answer['assistant_message']['id']}/continue")
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] != answer["job_id"]
    assert response.json()["assistant_message"]["content"] == "Historical draft"
    with SessionLocal() as db:
        new = db.get(Job, response.json()["job_id"])
        assert new.execution_engine == "pi-agent-v1"
        assert new.input_snapshot["retry_from_job_id"] == answer["job_id"]
        assert db.get(Job, answer["job_id"]).status == "failed"
        assert db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == chat["cid"])) == 2


def test_migration_roundtrip_preserves_existing_jobs(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.downgrade(cfg, "4c7b35a912df")
    assert "agent_tasks" not in inspect(engine()).get_table_names()
    with SessionLocal() as db:
        assert db.get(Job, created["job_id"])
    command.upgrade(cfg, "head")
    assert "agent_task_events" in inspect(engine()).get_table_names()
