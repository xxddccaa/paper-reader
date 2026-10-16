"""Private chat API invariants against an explicitly isolated PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, inspect, select

from app.chat_api import message_events
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import AgentTask, ChatConversation, ChatMessage, Job, JobEvent, Membership, ModelConnection, Reading, Revision, SessionRecord, User
from app.security import token_hash
from test_api import env, login, create_reading


@pytest.fixture
def chat(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    c, wid = env["client"], env["alpha"]
    with SessionLocal() as db, db.begin():
        reading = db.get(Reading, created["reading_id"])
        original = db.get(Job, created["job_id"])
        original.status = "succeeded"
        revision = Revision(reading_id=reading.id, workspace_id=wid, number=1, brief_markdown="Original brief", full_markdown="Original full", provenance={"workflow": {"version": "guided-v2.3"}})
        latest = Revision(reading_id=reading.id, workspace_id=wid, number=2, brief_markdown="Latest brief", full_markdown="Latest full", provenance={"workflow": {"version": "guided-v3.0"}})
        db.add_all([revision, latest])
        db.flush()
        reading.current_revision_id = latest.id
        ids = {**env, **created, "revision_id": revision.id, "current_revision_id": latest.id, "model_id": reading.model_connection_id}
    login(c, "alice")
    root = f"/api/v1/workspaces/{wid}"
    response = c.post(f"{root}/readings/{created['reading_id']}/conversations", json={"revision_id": ids["revision_id"]})
    assert response.status_code == 201, response.text
    ids.update(root=root, cid=response.json()["id"])
    ids["url"] = f"{root}/conversations/{ids['cid']}"
    return ids


def send(chat, content="How does this method work?", request_id=None):
    response = chat["client"].post(f"{chat['url']}/messages", json={"content": content, "client_request_id": request_id or str(uuid4())})
    assert response.status_code == 202, response.text
    return response.json()


def finish(answer, status="succeeded", content="Supported answer"):
    with SessionLocal() as db, db.begin():
        message = db.get(ChatMessage, answer["assistant_message"]["id"])
        job = db.get(Job, answer["job_id"])
        message.status = job.status = status
        message.content = content
        job.stage = status
        db.flush()
        db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type=status if status in {"failed", "cancelled"} else "completed", data={"message_id": message.id, "internal": "never expose this"}))


def test_historical_version_pinned_and_fresh_public_snapshot(chat):
    answer = send(chat)
    assert answer["user_message"]["sequence"] == 1
    assert answer["assistant_message"]["sequence"] == 2
    assert answer["user_message"]["status"] == "succeeded"
    assert answer["assistant_message"]["status"] == "queued"
    assert not {"artifacts", "request_id", "workspace_id"} & set(answer["assistant_message"])
    with SessionLocal() as db:
        job = db.get(Job, answer["job_id"])
        reading = db.get(Reading, chat["reading_id"])
        assert job.kind == "chat" and job.conversation_id == chat["cid"]
        assert job.input_snapshot["base_revision_id"] == chat["revision_id"]
        assert job.input_snapshot["workflow_version"] == "guided-v2.3"
        assert job.input_snapshot["source_rules_version"] == "source-sections-v2"
        assert job.input_snapshot["workflow_limits"] == {"timeout_seconds": 600, "max_tool_calls": 120, "max_related_papers": 3, "request_timeout_seconds": 300}
        assert job.input_snapshot["chat"] == {"conversation_id": chat["cid"], "message_id": answer["assistant_message"]["id"], "user_message_id": answer["user_message"]["id"]}
        assert "api_key" not in json.dumps(job.input_snapshot)
        assert reading.current_revision_id == chat["current_revision_id"]
        assert db.scalar(select(func.count(Revision.id)).where(Revision.reading_id == reading.id)) == 2
    detail = chat["client"].get(chat["url"]).json()
    assert detail["conversation"]["revision_id"] == chat["revision_id"]
    assert detail["conversation"]["title"] == "How does this method work?"


def test_legacy_chat_limits_without_agent_engine(chat, monkeypatch):
    monkeypatch.setenv("PAPER_READER_PI_AGENT_ENABLED", "false")
    get_settings.cache_clear()
    answer = send(chat)
    with SessionLocal() as db:
        job = db.get(Job, answer["job_id"])
        assert job.execution_engine == "legacy"
        assert job.input_snapshot["workflow_limits"] == {"timeout_seconds": 300, "max_tool_calls": 48, "max_related_papers": 3}
        assert db.get(AgentTask, job.id) is None


def test_send_idempotency_conflict_and_one_active_answer(chat):
    request_id = str(uuid4())
    first = send(chat, request_id=request_id)
    assert send(chat, request_id=request_id) == first
    conflict = chat["client"].post(f"{chat['url']}/messages", json={"content": "Changed question", "client_request_id": request_id})
    assert conflict.status_code == 409
    active = chat["client"].post(f"{chat['url']}/messages", json={"content": "Another question", "client_request_id": str(uuid4())})
    assert active.status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == chat["cid"])) == 2
        assert db.scalar(select(func.count(Job.id)).where(Job.conversation_id == chat["cid"])) == 1


def test_concurrent_idempotent_sends_allocate_one_pair(chat):
    from app.main import app
    token = chat["client"].cookies.get("paper_reader_session")
    request_id = str(uuid4())
    def request(_):
        with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver", "Cookie": f"paper_reader_session={token}"}) as client:
            response = client.post(f"{chat['url']}/messages", json={"content": "Concurrent question", "client_request_id": request_id})
            return response.status_code, response.json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(4)))
    assert all(code == 202 for code, _ in results)
    assert len({value["job_id"] for _, value in results}) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == chat["cid"])) == 2


@pytest.mark.parametrize("other", ["admin", "bob"])
def test_private_conversation_hidden_even_from_same_workspace_admin(chat, other):
    answer = send(chat)
    mid = answer["assistant_message"]["id"]
    c = chat["client"]
    login(c, other)
    assert c.get(chat["url"]).status_code == 404
    assert c.post(f"{chat['url']}/messages", json={"content": "Hijack", "client_request_id": str(uuid4())}).status_code == 404
    assert c.get(f"{chat['url']}/messages/{mid}/events").status_code == 404
    assert c.get(f"{chat['url']}/messages/{mid}/sources/main").status_code == 404
    assert c.post(f"{chat['url']}/messages/{mid}/cancel").status_code == 404
    assert c.post(f"{chat['url']}/messages/{mid}/retry").status_code == 404
    response = c.get(f"{chat['root']}/readings/{chat['reading_id']}/conversations")
    assert response.status_code == (200 if other == "admin" else 404)
    if other == "admin":
        assert response.json() == []


def test_wrong_workspace_and_cross_reading_revision_rejected(chat):
    c = chat["client"]
    assert c.get(chat["url"].replace(chat["alpha"], chat["beta"])).status_code == 404
    with SessionLocal() as db, db.begin():
        base = db.get(Reading, chat["reading_id"])
        second = Reading(workspace_id=base.workspace_id, paper_id=base.paper_id, created_by=base.created_by, skill_id=base.skill_id, model_connection_id=base.model_connection_id, template_id=base.template_id)
        db.add(second)
        db.flush()
        rid = second.id
    assert c.post(f"{chat['root']}/readings/{rid}/conversations", json={"revision_id": chat["revision_id"]}).status_code == 404
    login(c, "bob")
    assert c.post(f"/api/v1/workspaces/{chat['beta']}/readings/{chat['reading_id']}/conversations", json={"revision_id": chat["revision_id"]}).status_code == 404


@pytest.mark.parametrize("username", ["alice", "admin"])
def test_generic_jobs_and_reading_routes_never_expose_chat(chat, username):
    answer = send(chat)
    c = chat["client"]
    login(c, username)
    jid = answer["job_id"]
    assert all(job["id"] != jid for job in c.get(f"{chat['root']}/jobs").json())
    assert all(job["id"] != jid for job in c.get(f"{chat['root']}/readings/{chat['reading_id']}").json()["jobs"])
    item = next(row for row in c.get(f"{chat['root']}/readings").json() if row["id"] == chat["reading_id"])
    assert item["latest_job"]["id"] == chat["job_id"]
    for suffix in ("", "/events"):
        assert c.get(f"{chat['root']}/jobs/{jid}{suffix}").status_code == 404
    for action in ("cancel", "retry"):
        assert c.post(f"{chat['root']}/jobs/{jid}/{action}", json={}).status_code == 404
    with SessionLocal() as db:
        assert not db.get(Job, jid).cancel_requested


def test_chat_does_not_block_formal_revision(chat):
    send(chat)
    response = chat["client"].post(f"{chat['root']}/readings/{chat['reading_id']}/revisions", json={"instruction": "Improve explanation", "base_revision_id": chat["current_revision_id"]})
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        assert db.get(Job, response.json()["job_id"]).kind == "revise"


def test_queued_cancel_synchronizes_message_job_and_terminal_event(chat):
    answer = send(chat)
    mid = answer["assistant_message"]["id"]
    response = chat["client"].post(f"{chat['url']}/messages/{mid}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    with SessionLocal() as db:
        job = db.get(Job, answer["job_id"])
        assert job.status == "cancelled" and job.cancel_requested
        event = db.scalar(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id.desc()))
        assert event.event_type == "cancelled"
    stream = chat["client"].get(f"{chat['url']}/messages/{mid}/events")
    assert "event: cancelled" in stream.text
    assert '"artifacts"' not in stream.text
    assert chat["client"].post(f"{chat['url']}/messages/{mid}/cancel").json()["status"] == "cancelled"


def test_running_cancel_is_request_until_host_finalizes(chat):
    answer = send(chat)
    mid = answer["assistant_message"]["id"]
    with SessionLocal() as db, db.begin():
        db.get(Job, answer["job_id"]).status = "running"
        db.get(ChatMessage, mid).status = "running"
    response = chat["client"].post(f"{chat['url']}/messages/{mid}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "running"
    with SessionLocal() as db:
        assert db.get(Job, answer["job_id"]).cancel_requested
        assert db.get(ChatMessage, mid).status == "running"


@pytest.mark.parametrize("status", ["failed", "partial", "cancelled"])
def test_retry_replaces_job_preserves_pair_and_refreshes_model(chat, status):
    answer = send(chat)
    finish(answer, status=status)
    mid = answer["assistant_message"]["id"]
    with SessionLocal() as db, db.begin():
        db.get(ModelConnection, chat["model_id"]).model = "updated-model"
        message = db.get(ChatMessage, mid)
        message.evidence_index = [{"id": "old"}]
        message.verification = {"passed": False}
        message.error = "Previous failure"
    response = chat["client"].post(f"{chat['url']}/messages/{mid}/retry")
    assert response.status_code == 202, response.text
    retried = response.json()
    assert retried["job_id"] != answer["job_id"]
    assert retried["assistant_message"]["id"] == mid
    assert retried["assistant_message"]["content"] == ""
    assert retried["assistant_message"]["status"] == "queued"
    assert retried["assistant_message"]["error"] is None
    with SessionLocal() as db:
        job = db.get(Job, retried["job_id"])
        assert job.input_snapshot["model"]["model"] == "updated-model"
        assert job.input_snapshot["base_revision_id"] == chat["revision_id"]
        assert job.input_snapshot["retry_of_job_id"] == answer["job_id"]
        assert db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == chat["cid"])) == 2
    assert chat["client"].post(f"{chat['url']}/messages/{mid}/retry").status_code == 409


def test_retry_requires_latest_terminal_assistant(chat):
    first = send(chat)
    assert chat["client"].post(f"{chat['url']}/messages/{first['user_message']['id']}/retry").status_code == 409
    assert chat["client"].post(f"{chat['url']}/messages/{first['assistant_message']['id']}/retry").status_code == 409
    finish(first, status="failed")
    second = send(chat, "Next question")
    finish(second)
    assert chat["client"].post(f"{chat['url']}/messages/{first['assistant_message']['id']}/retry").status_code == 409
    assert chat["client"].post(f"{chat['url']}/messages/{second['assistant_message']['id']}/retry").status_code == 409


def test_history_pagination_and_title_order(chat):
    for index in range(3):
        finish(send(chat, f"Question {index}"))
    c = chat["client"]
    newest = c.get(f"{chat['url']}?limit=3").json()
    assert [message["sequence"] for message in newest["messages"]] == [4, 5, 6]
    assert newest["next_before"] == 4
    oldest = c.get(f"{chat['url']}?limit=3&before=4").json()
    assert [message["sequence"] for message in oldest["messages"]] == [1, 2, 3]
    assert oldest["next_before"] is None
    assert c.get(f"{chat['url']}?limit=201").status_code == 422


def test_source_mapping_path_containment_and_html_sandbox(chat, tmp_path):
    answer = send(chat)
    mid = answer["assistant_message"]["id"]
    folder = Path("storage") / "workspaces" / chat["alpha"] / "conversations" / chat["cid"]
    runtime = get_settings().runtime_dir
    (runtime / folder).mkdir(parents=True)
    (runtime / folder / "source.html").write_text("<h1>Original</h1>")
    outside = runtime / "private.txt"
    outside.write_text("private data")
    (runtime / folder / "link.txt").symlink_to(outside)
    paths = {"main": str(folder / "source.html"), "other": "private.txt", "traversal": str(folder / "../../../../private.txt"), "absolute": str(runtime / folder / "source.html"), "link": str(folder / "link.txt"), "other-conversation": str(folder.parent / "someone-else" / "source.pdf")}
    with SessionLocal() as db, db.begin():
        db.get(ChatMessage, mid).artifacts = [{"source_id": key, "path": path, "mime_type": "text/html", "document": {"internal": "hidden"}} for key, path in paths.items()]
    with SessionLocal() as db, db.begin():
        message = db.get(ChatMessage, mid)
        message.artifacts = message.artifacts + [{"source_id": "related:arxiv:hep-th/9901001", "path": str(folder / "source.html"), "mime_type": "text/html"}]
    source = f"{chat['url']}/messages/{mid}/sources"
    assert chat["client"].get(f"{source}/related%3Aarxiv%3Ahep-th%2F9901001").status_code == 200
    response = chat["client"].get(f"{source}/main")
    assert response.status_code == 200 and response.text == "<h1>Original</h1>"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"].startswith("sandbox")
    for key in set(paths) - {"main"} | {"missing"}:
        assert chat["client"].get(f"{source}/{key}").status_code == 404
    assert "artifacts" not in chat["client"].get(chat["url"]).text


def test_sse_replay_final_snapshot_and_field_allowlist(chat):
    answer = send(chat)
    mid, jid = answer["assistant_message"]["id"], answer["job_id"]
    with SessionLocal() as db, db.begin():
        first = JobEvent(job_id=jid, workspace_id=chat["alpha"], event_type="delta", data={"text": "First", "attempt": 1, "api_key": "hidden-token"})
        second = JobEvent(job_id=jid, workspace_id=chat["alpha"], event_type="delta", data={"text": " second", "attempt": 1, "reasoning": "hidden-reasoning"})
        db.add_all([first, second])
        db.flush()
        first_id = first.id
        db.add(JobEvent(job_id=jid, workspace_id=chat["alpha"], event_type="tool_request", data={"secret": "hidden-tool"}))
    finish(answer)
    c, url = chat["client"], f"{chat['url']}/messages/{mid}/events"
    response = c.get(url, headers={"Last-Event-ID": str(first_id)})
    assert response.status_code == 200
    assert '"text": "First"' not in response.text
    assert '"text": " second"' in response.text
    assert 'event: completed' in response.text and 'Supported answer' in response.text
    assert all(value not in response.text for value in ("hidden-token", "hidden-reasoning", "hidden-tool", "never expose", "artifacts"))
    replay = c.get(f"{url}?after=999999999")
    assert "event: completed" in replay.text
    for cursor in ("bad", "-1"):
        assert c.get(url, headers={"Last-Event-ID": cursor}).status_code == 422


@pytest.mark.parametrize("revocation", ["session", "membership", "owner", "account"])
async def test_sse_rechecks_private_access_on_each_poll(chat, monkeypatch, revocation):
    answer = send(chat)
    token = chat["client"].cookies.get("paper_reader_session")
    class Request:
        headers = {}
        cookies = {"paper_reader_session": token}
        async def is_disconnected(self):
            return False
    async def no_sleep(_):
        pass
    monkeypatch.setattr("app.chat_api.asyncio.sleep", no_sleep)
    with SessionLocal() as db:
        user = db.get(User, chat["alice"])
        response = await message_events(chat["alpha"], chat["cid"], answer["assistant_message"]["id"], Request(), after=0, db=db, user=user)
        iterator = response.body_iterator
        first = await anext(iterator)
        assert "event: progress" in first
        # Consume the empty next poll's heartbeat, then revoke before it repeats.
        assert "keep-alive" in await anext(iterator)
        with SessionLocal() as update, update.begin():
            if revocation == "session":
                update.delete(update.get(SessionRecord, token_hash(token)))
            elif revocation == "membership":
                update.delete(update.scalar(select(Membership).where(Membership.workspace_id == chat["alpha"], Membership.user_id == chat["alice"])))
            elif revocation == "owner":
                update.get(ChatConversation, chat["cid"]).user_id = chat["admin"]
            else:
                update.get(User, chat["alice"]).active = False
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)


def test_input_validation_and_origin(chat):
    c, url = chat["client"], f"{chat['url']}/messages"
    for content, request_id in (("", str(uuid4())), ("  \n", str(uuid4())), ("x" * 10001, str(uuid4())), ("Question", "invalid")):
        assert c.post(url, json={"content": content, "client_request_id": request_id}).status_code == 422
    assert c.post(url, headers={"Origin": "http://evil.test"}, json={"content": "Question", "client_request_id": str(uuid4())}).status_code == 403


def test_chat_migration_roundtrip_preserves_readings(chat):
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.downgrade(cfg, "3f9b52d018e7")
    assert "chat_messages" not in inspect(engine()).get_table_names()
    with SessionLocal() as db:
        # Raw table inspection avoids the temporarily unavailable chat job column.
        assert db.get(Reading, chat["reading_id"]).current_revision_id == chat["current_revision_id"]
    command.upgrade(cfg, "head")
    assert "chat_messages" in inspect(engine()).get_table_names()
    assert "conversation_id" in {column["name"] for column in inspect(engine()).get_columns("jobs")}
    with SessionLocal() as db:
        assert db.get(Job, chat["job_id"]).conversation_id is None
