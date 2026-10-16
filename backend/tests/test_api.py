"""Real PostgreSQL API tests. Set an explicitly isolated *_test database URL."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import fitz
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.models import User, Workspace, Membership, Template, TemplateVersion, ModelConnection, Paper, Reading, Revision, Job, JobEvent, SessionRecord, now
from app.security import hash_password, decrypt_secret

PASSWORD = "temporary-test-password"

@pytest.fixture
def env(monkeypatch, tmp_path):
    url = os.getenv("PAPER_READER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PAPER_READER_TEST_DATABASE_URL is required for real PostgreSQL tests")
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    if not name.endswith("_test"):
        pytest.fail("Refusing test database without _test suffix")
    monkeypatch.setenv("PAPER_READER_DATABASE_URL", url)
    monkeypatch.setenv("PAPER_READER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_READER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PAPER_READER_ALLOW_PRIVATE_MODELS", "true")
    monkeypatch.delenv("PAPER_READER_PUBLIC_ORIGIN", raising=False)
    get_settings.cache_clear()
    engine.cache_clear()
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")
    with engine().begin() as connection:
        tables = ", ".join('"' + t.name + '"' for t in reversed(Base.metadata.sorted_tables))
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    from app.builtin import ensure_builtin
    with SessionLocal() as db:
        admin = User(username="admin", password_hash=hash_password(PASSWORD), is_admin=True)
        alice = User(username="alice", password_hash=hash_password(PASSWORD))
        bob = User(username="bob", password_hash=hash_password(PASSWORD))
        db.add_all([admin, alice, bob])
        db.flush()
        alpha, beta = Workspace(name="Alpha"), Workspace(name="Beta")
        db.add_all([alpha, beta])
        db.flush()
        db.add_all([Membership(workspace_id=alpha.id, user_id=admin.id, role="admin"), Membership(workspace_id=alpha.id, user_id=alice.id, role="member"), Membership(workspace_id=beta.id, user_id=bob.id, role="admin")])
        builtin = ensure_builtin(db)
        db.commit()
        ids = {"admin": admin.id, "alice": alice.id, "bob": bob.id, "alpha": alpha.id, "beta": beta.id, "builtin": builtin.id, "runtime": tmp_path}
    from app.main import app
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        ids["client"] = client
        yield ids
    engine().dispose()
    engine.cache_clear()
    get_settings.cache_clear()

def login(client, username="admin"):
    result = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert result.status_code == 200, result.text

def resources(env):
    c, wid = env["client"], env["alpha"]
    login(c)
    model = c.post(f"/api/v1/workspaces/{wid}/models", json={"name": "Test protocol", "base_url": "http://127.0.0.1:19999/v1", "model": "fixture", "api_key": "fixture-not-real-key"})
    assert model.status_code == 201, model.text
    paper = c.post(f"/api/v1/workspaces/{wid}/papers", json={"arxiv": "2401.12345", "title": "Test paper"})
    assert paper.status_code == 201, paper.text
    return model.json(), paper.json()


def test_platform_network_settings_are_admin_only_and_probe_does_not_save(env, monkeypatch):
    from app.source_network import load_source_network
    from app.source_tools import Download
    c = env["client"]
    login(c, "alice")
    settings = {"proxy_enabled": True, "proxy_url": "http://127.0.0.1:10826", "timeout_seconds": 120}
    assert c.get("/api/v1/admin/network").status_code == 403
    assert c.put("/api/v1/admin/network", json=settings).status_code == 403
    assert c.post("/api/v1/admin/network/test", json=settings).status_code == 403
    login(c)
    assert c.get("/api/v1/admin/network").json()["proxy_enabled"] is False
    def download(url, **kwargs):
        assert url == "https://arxiv.org/abs/2106.10199"
        assert kwargs["network"].proxy_url == settings["proxy_url"]
        return Download(b'<meta name="citation_title" content="Test">', url, "text/html")
    monkeypatch.setattr("app.source_tools.download_public", download)
    assert c.post("/api/v1/admin/network/test", json=settings).json()["ok"] is True
    assert not load_source_network().proxy_enabled
    assert c.put("/api/v1/admin/network", json=settings).status_code == 200
    assert c.get("/api/v1/admin/network").json() == settings
    rejected = c.put("/api/v1/admin/network", json={**settings, "proxy_url": "http://user:secret@localhost:10826"})
    assert rejected.status_code == 422
    assert "secret" not in rejected.text

def create_reading(env, monkeypatch):
    model, paper = resources(env)
    monkeypatch.setattr("app.api.skill_catalog", lambda: [{"id": "deeppapernote", "available": True}])
    response = env["client"].post(f"/api/v1/workspaces/{env['alpha']}/readings", json={"paper_id": paper["id"], "model_connection_id": model["id"], "template_id": env["builtin"], "skill_id": "deeppapernote"})
    assert response.status_code == 201, response.text
    return response.json()


def test_legacy_guided_revision_keeps_original_parser_rules(env, monkeypatch):
    created = create_reading(env, monkeypatch)
    with SessionLocal() as db, db.begin():
        job = db.get(Job, created["job_id"])
        reading = db.get(Reading, created["reading_id"])
        assert job.input_snapshot["workflow_version"] == "guided-v3.0"
        assert job.input_snapshot["source_rules_version"] == "source-structured-v3"
        revision = Revision(reading_id=reading.id, workspace_id=reading.workspace_id, number=1,
                            instruction="", brief_markdown="Old reading", full_markdown="Old reading",
                            provenance={"workflow": {"version": "guided-v2.3"}})
        db.add(revision)
        db.flush()
        reading.current_revision_id = revision.id
        job.status = "succeeded"
        revision_id = revision.id
    response = env["client"].post(f"/api/v1/workspaces/{env['alpha']}/readings/{created['reading_id']}/revisions",
                                  json={"base_revision_id": revision_id, "instruction": "Reorganize existing content"})
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        revised = db.get(Job, response.json()["job_id"])
        assert revised.input_snapshot["workflow_version"] == "guided-v2.3"
        assert revised.input_snapshot["source_rules_version"] == "source-sections-v2"

def test_login_origin_session_and_revocation(env):
    c = env["client"]
    assert c.get("/api/v1/auth/me").status_code == 401
    assert c.post("/api/v1/auth/login", headers={"Origin": "http://attacker.test"}, json={"username": "admin", "password": PASSWORD}).status_code == 403
    assert c.post("/api/v1/auth/login", headers={"Origin": "null"}, json={"username": "admin", "password": PASSWORD}).status_code == 403
    login(c)
    assert c.get("/api/v1/auth/me").json()["username"] == "admin"
    token = c.cookies.get("paper_reader_session")
    with SessionLocal() as db:
        assert all(s.id != token for s in db.scalars(select(SessionRecord)))
    assert c.post("/api/v1/auth/logout").status_code == 200
    assert c.get("/api/v1/auth/me").status_code == 401

def test_cross_workspace_access_and_platform_admin(env):
    model, paper = resources(env)
    c, a, b = env["client"], env["alpha"], env["beta"]
    assert c.get(f"/api/v1/workspaces/{b}/papers").status_code == 404
    login(c, "alice")
    assert c.get("/api/v1/admin/users").status_code == 403
    assert c.post("/api/v1/workspaces", json={"name": "No"}).status_code == 403
    assert c.get(f"/api/v1/workspaces/{a}/members").status_code == 403
    assert c.post(f"/api/v1/workspaces/{a}/models", json={"name": "x", "base_url": "https://example.com", "model": "x", "api_key": "x"}).status_code == 403
    login(c, "bob")
    assert c.get(f"/api/v1/workspaces/{a}/papers/{paper['id']}").status_code == 404
    assert c.get(f"/api/v1/workspaces/{b}/papers/{paper['id']}").status_code == 404
    assert c.get(f"/api/v1/workspaces/{b}/papers/{paper['id']}/file").status_code == 404
    assert c.patch(f"/api/v1/workspaces/{b}/models/{model['id']}", json={"name": "Stolen"}).status_code == 404

def test_model_secret_encryption_and_blank_preserve(env):
    model, _ = resources(env)
    assert "api_key" not in model and model["has_api_key"]
    with SessionLocal() as db:
        value = db.get(ModelConnection, model["id"])
        assert value.encrypted_api_key != "fixture-not-real-key"
        assert decrypt_secret(value.encrypted_api_key) == "fixture-not-real-key"
    response = env["client"].patch(f"/api/v1/workspaces/{env['alpha']}/models/{model['id']}", json={"api_key": "", "name": "Updated"})
    assert response.status_code == 200
    with SessionLocal() as db:
        assert decrypt_secret(db.get(ModelConnection, model["id"]).encrypted_api_key) == "fixture-not-real-key"

def test_model_real_http_probe_against_explicit_fixture(env, monkeypatch):
    # A protocol fixture validates network/wire behavior; it is not model quality validation.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"tool_calls": [{"type": "function", "function": {"name": "connection_check", "arguments": "{\"ok\":true}"}}]}}]}).encode())
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        model, _ = resources(env)
        c, wid = env["client"], env["alpha"]
        c.patch(f"/api/v1/workspaces/{wid}/models/{model['id']}", json={"base_url": f"http://127.0.0.1:{server.server_port}/v1"})
        result = c.post(f"/api/v1/workspaces/{wid}/models/{model['id']}/test")
        assert result.json()["ok"] is True
        assert received[0]["max_tokens"] == 512
        assert received[0]["tool_choice"]["function"]["name"] == "connection_check"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

def test_last_admin_and_membership_roles(env):
    c, a = env["client"], env["alpha"]
    login(c)
    assert c.delete(f"/api/v1/workspaces/{a}/members/{env['admin']}").status_code == 409
    assert c.post(f"/api/v1/workspaces/{a}/members", json={"user_id": env["admin"], "role": "member"}).status_code == 409
    assert c.post(f"/api/v1/workspaces/{a}/members", json={"user_id": env["alice"], "role": "admin"}).status_code == 200
    assert c.delete(f"/api/v1/workspaces/{a}/members/{env['admin']}").status_code == 200
    assert c.get(f"/api/v1/workspaces/{a}/papers").status_code == 404

def test_template_versions_and_owner(env):
    c, a = env["client"], env["alpha"]
    login(c, "alice")
    content = {"name": "Table", "brief_markdown": "| Result | Source |", "full_markdown": "# Details", "instructions": "Chinese"}
    created = c.post(f"/api/v1/workspaces/{a}/templates", json=content).json()
    first_id = created["current_version"]["id"]
    content["brief_markdown"] = "# New brief"
    updated = c.put(f"/api/v1/workspaces/{a}/templates/{created['id']}", json=content)
    assert updated.status_code == 200
    assert updated.json()["current_version"]["number"] == 2
    versions = c.get(f"/api/v1/workspaces/{a}/templates/{created['id']}/versions").json()
    assert versions[1]["id"] == first_id and versions[1]["brief_markdown"] == "| Result | Source |"
    assert c.put(f"/api/v1/workspaces/{a}/templates/{env['builtin']}", json=content).status_code == 403

def test_pdf_upload_validation_and_arxiv(env):
    c, a = env["client"], env["alpha"]
    login(c)
    assert c.post(f"/api/v1/workspaces/{a}/papers/upload", files={"file": ("a.pdf", b"not a pdf", "application/pdf")}).status_code == 422
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Original source evidence")
    data = doc.tobytes()
    doc.close()
    uploaded = c.post(f"/api/v1/workspaces/{a}/papers/upload", files={"file": ("a.pdf", data, "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    value = uploaded.json()
    assert value["sha256"] and value["metadata_json"]["pages"] == 1
    assert "storage_path" not in value
    assert c.get(f"/api/v1/workspaces/{a}/papers/{value['id']}/file").content == data
    assert c.post(f"/api/v1/workspaces/{a}/papers", json={"arxiv": "http://localhost/private"}).status_code == 422
    assert c.post(f"/api/v1/workspaces/{a}/papers", json={"arxiv": "https://arxiv.org/pdf/2401.12345v2.pdf"}).json()["source_url"] == "https://arxiv.org/abs/2401.12345v2"

def test_job_snapshot_cancel_retry_and_sse(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, a, jid = env["client"], env["alpha"], ids["job_id"]
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert "api_key" not in json.dumps(job.input_snapshot)
        assert job.input_snapshot["template"]["version_id"]
    cancelled = c.post(f"/api/v1/workspaces/{a}/jobs/{jid}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    events = c.get(f"/api/v1/workspaces/{a}/jobs/{jid}/events")
    assert "event: cancelled" in events.text and "event: progress" in events.text
    retry = c.post(f"/api/v1/workspaces/{a}/jobs/{jid}/retry")
    assert retry.status_code == 201 and retry.json()["job_id"] != jid
    assert c.post(f"/api/v1/workspaces/{a}/jobs/{jid}/retry").status_code == 409
    login(c, "bob")
    assert c.get(f"/api/v1/workspaces/{a}/jobs/{jid}/events").status_code == 404
    assert c.get(f"/api/v1/workspaces/{env['beta']}/jobs/{jid}").status_code == 404

def test_revision_staleness_preserves_old_output_and_artifact_boundaries(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, a, rid = env["client"], env["alpha"], ids["reading_id"]
    with SessionLocal() as db:
        db.get(Job, ids["job_id"]).status = "succeeded"
        rev = Revision(reading_id=rid, workspace_id=a, number=1, brief_markdown="Evidence [E1]", full_markdown="Long result [E1]", evidence_index=[{"id": "E1", "quote": "real quote", "source_title": "Paper"}], artifacts=[{"name": "forbidden.env", "path": "config/app.env"}])
        db.add(rev)
        db.flush()
        db.get(Reading, rid).current_revision_id = rev.id
        db.commit()
        revision_id = rev.id
    (env["runtime"] / "config").mkdir()
    (env["runtime"] / "config" / "app.env").write_text("private")
    assert c.get(f"/api/v1/workspaces/{a}/artifacts/{revision_id}/0").status_code == 404
    assert c.post(f"/api/v1/workspaces/{a}/readings/{rid}/revisions", json={"instruction": "Shorten", "base_revision_id": "stale"}).status_code == 409
    result = c.post(f"/api/v1/workspaces/{a}/readings/{rid}/revisions", json={"instruction": "Shorten", "base_revision_id": revision_id})
    assert result.status_code == 201
    assert c.post(f"/api/v1/workspaces/{a}/readings/{rid}/revisions", json={"instruction": "Again", "base_revision_id": revision_id}).status_code == 409
    exported = c.get(f"/api/v1/workspaces/{a}/readings/{rid}/export?view=brief")
    assert "Evidence [E1]" in exported.text and "real quote" in exported.text
    c.post(f"/api/v1/workspaces/{a}/jobs/{result.json()['job_id']}/cancel")
    detail = c.get(f"/api/v1/workspaces/{a}/readings/{rid}").json()
    assert detail["current_revision_id"] == revision_id and len(detail["revisions"]) == 1

def test_private_model_policy(env, monkeypatch):
    c, a = env["client"], env["alpha"]
    login(c)
    monkeypatch.setenv("PAPER_READER_ALLOW_PRIVATE_MODELS", "false")
    get_settings.cache_clear()
    result = c.post(f"/api/v1/workspaces/{a}/models", json={"name": "Private", "base_url": "http://127.0.0.1/v1", "model": "fixture", "api_key": "fixture"})
    assert result.status_code == 422
    assert "Private model endpoints" in result.text

def test_strict_payloads_and_inactive_session(env):
    model, _ = resources(env)
    c, a = env["client"], env["alpha"]
    assert c.patch(f"/api/v1/workspaces/{a}/models/{model['id']}", json={"name": None}).status_code == 422
    assert c.patch(f"/api/v1/workspaces/{a}/models/{model['id']}", json={"unknown": "field"}).status_code == 422
    assert c.post("/api/v1/admin/users", json={"username": "   ", "password": PASSWORD}).status_code == 422
    with SessionLocal() as db:
        db.get(User, env["admin"]).active = False
        db.commit()
    assert c.get("/api/v1/auth/me").status_code == 401
    assert c.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 401

@pytest.mark.asyncio
async def test_live_sse_stops_after_membership_revocation(env, monkeypatch):
    from app.api import events
    ids = create_reading(env, monkeypatch)
    token = env["client"].cookies.get("paper_reader_session")
    class Request:
        headers = {}
        cookies = {"paper_reader_session": token}
        async def is_disconnected(self):
            return False
    with SessionLocal() as db:
        user = db.get(User, env["admin"])
        response = await events(env["alpha"], ids["job_id"], Request(), after=0, db=db, user=user)
    iterator = response.body_iterator
    first = await anext(iterator)
    assert "event: progress" in first
    with SessionLocal() as db:
        member = db.scalar(select(Membership).where(Membership.workspace_id == env["alpha"], Membership.user_id == env["admin"]))
        db.delete(member)
        db.commit()
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)

def test_error_responses_and_runtime_logs_redact_secrets(env):
    import logging
    from app.logging_config import configure_logging, register_secret
    secret = "deliberately-sensitive-test-value"
    configure_logging("api")
    register_secret(secret)
    logging.getLogger("app.test").warning("registered value=%s bearer credential", secret)
    log = (env["runtime"] / "logs" / "api.log").read_text()
    assert secret not in log and "[REDACTED]" in log


def test_model_response_does_not_expose_service_error_body(env):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'api_key=fixture-not-real-key upstream error')
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        model, _ = resources(env)
        c, a = env["client"], env["alpha"]
        c.patch(f"/api/v1/workspaces/{a}/models/{model['id']}", json={"base_url": f"http://127.0.0.1:{server.server_port}/v1"})
        result = c.post(f"/api/v1/workspaces/{a}/models/{model['id']}/test")
        assert not result.json()["ok"]
        assert "fixture-not-real-key" not in result.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

def test_explicit_retry_refreshes_model_and_preserves_original_snapshot(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, wid = env["client"], env["alpha"]
    with SessionLocal() as db:
        old = db.get(Job, ids["job_id"])
        original = dict(old.input_snapshot)
        model_id = original["model"]["id"]
    result = c.patch(f"/api/v1/workspaces/{wid}/models/{model_id}", json={"base_url": "http://127.0.0.1:19998/v1", "model": "replacement", "api_key": "rotated-fixture-key"})
    assert result.status_code == 200
    c.post(f"/api/v1/workspaces/{wid}/jobs/{ids['job_id']}/cancel")
    retried = c.post(f"/api/v1/workspaces/{wid}/jobs/{ids['job_id']}/retry")
    assert retried.status_code == 201
    with SessionLocal() as db:
        old = db.get(Job, ids["job_id"])
        new = db.get(Job, retried.json()["job_id"])
        assert old.input_snapshot == original
        assert new.input_snapshot["model"]["base_url"] == "http://127.0.0.1:19998/v1"
        assert new.input_snapshot["model"]["model"] == "replacement"
        assert new.input_snapshot["template"] == original["template"]
        assert new.input_snapshot["paper_id"] == original["paper_id"]
        assert "rotated-fixture-key" not in json.dumps(new.input_snapshot)


def test_html_source_uses_correct_mime_as_sandboxed_attachment(env):
    _, paper = resources(env)
    wid, pid, root = env["alpha"], paper["id"], env["runtime"]
    relative = Path("storage") / "workspaces" / wid / "papers" / pid / "source.html"
    destination = root / relative
    destination.parent.mkdir(parents=True)
    destination.write_text('<html><script>alert("untrusted")</script></html>')
    with SessionLocal() as db:
        value = db.get(Paper, pid)
        value.storage_path = str(relative)
        value.metadata_json = {**value.metadata_json, "source_mime_type": "text/html"}
        db.commit()
    response = env["client"].get(f"/api/v1/workspaces/{wid}/papers/{pid}/file")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert env["client"].get(f"/api/v1/workspaces/{wid}/papers/{pid}").json()["metadata_json"]["source_mime_type"] == "text/html"


def test_validation_errors_never_echo_credential_input(env):
    model, _ = resources(env)
    secret = "secret-in-invalid-request"
    response = env["client"].patch(f"/api/v1/workspaces/{env['alpha']}/models/{model['id']}", json={"name": None, "api_key": secret})
    assert response.status_code == 422
    assert secret not in response.text and "input" not in response.json()["detail"][0]


def test_probe_truncation_is_actionable_and_budget_bounded(env, monkeypatch):
    model, _ = resources(env)
    c, wid = env["client"], env["alpha"]
    captured = []
    def truncated_probe(url, *, headers, payload, allow_private):
        captured.append(payload["max_tokens"])
        return 200, {"choices": [{"finish_reason": "length", "message": {"tool_calls": [{"function": {"name": "connection_check", "arguments": '{"ok":'}}]}}]}
    monkeypatch.setattr("app.api.post_model_json", truncated_probe)
    result = c.post(f"/api/v1/workspaces/{wid}/models/{model['id']}/test")
    assert result.status_code == 200 and result.json()["ok"] is False
    assert "512-token output limit" in result.json()["message"]
    assert "fixture-not-real-key" not in result.text
    c.patch(f"/api/v1/workspaces/{wid}/models/{model['id']}", json={"max_tokens": 256})
    result = c.post(f"/api/v1/workspaces/{wid}/models/{model['id']}/test")
    assert "256-token output limit" in result.json()["message"]
    assert captured == [512, 256]


def test_thinking_format_create_and_effective_patch_validation(env):
    c, wid = env["client"], env["alpha"]
    login(c)
    payload = {"name": "Qwen server", "base_url": "http://127.0.0.1:19999/v1", "model": "fixture", "api_key": "fixture-key", "thinking_format": "qwen-chat-template"}
    rejected = c.post(f"/api/v1/workspaces/{wid}/models", json={**payload, "protocol": "anthropic-messages"})
    assert rejected.status_code == 422
    assert c.post(f"/api/v1/workspaces/{wid}/models", json={**payload, "thinking_format": "unknown"}).status_code == 422
    created = c.post(f"/api/v1/workspaces/{wid}/models", json=payload)
    assert created.status_code == 201
    model = created.json()
    assert model["thinking_format"] == "qwen-chat-template" and "api_key" not in model
    endpoint = f"/api/v1/workspaces/{wid}/models/{model['id']}"
    assert c.patch(endpoint, json={"protocol": "anthropic-messages"}).status_code == 422
    with SessionLocal() as db:
        value = db.get(ModelConnection, model["id"])
        assert value.protocol == "openai-completions" and value.thinking_format == "qwen-chat-template"
    changed = c.patch(endpoint, json={"protocol": "anthropic-messages", "thinking_format": "default"})
    assert changed.status_code == 200 and changed.json()["thinking_format"] == "default"
    assert c.patch(endpoint, json={"thinking_format": "qwen-chat-template"}).status_code == 422
    assert c.patch(endpoint, json={"thinking_format": None}).status_code == 422
    assert c.patch(endpoint, json={"protocol": "openai-completions", "thinking_format": "qwen-chat-template"}).status_code == 200
    login(c, "alice")
    assert c.patch(endpoint, json={"thinking_format": "default"}).status_code == 403


@pytest.mark.parametrize("thinking_format", ["default", "qwen-chat-template"])
def test_probe_only_sends_qwen_template_override_when_selected(env, monkeypatch, thinking_format):
    model, _ = resources(env)
    c, wid = env["client"], env["alpha"]
    endpoint = f"/api/v1/workspaces/{wid}/models/{model['id']}"
    assert c.patch(endpoint, json={"thinking_format": thinking_format}).status_code == 200
    captured = []
    def fixture_probe(url, *, headers, payload, allow_private):
        captured.append(payload)
        return 200, {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"function": {"name": "connection_check", "arguments": '{"ok":true}'}}]}}]}
    monkeypatch.setattr("app.api.post_model_json", fixture_probe)
    response = c.post(endpoint + "/test")
    assert response.json()["ok"] is True
    if thinking_format == "qwen-chat-template":
        assert captured[0]["chat_template_kwargs"] == {"enable_thinking": False}
    else:
        assert "chat_template_kwargs" not in captured[0]


def test_thinking_format_snapshot_and_retry_refresh(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, wid = env["client"], env["alpha"]
    with SessionLocal() as db:
        original = db.get(Job, ids["job_id"])
        assert original.input_snapshot["model"]["thinking_format"] == "default"
        model_id = original.input_snapshot["model"]["id"]
    assert c.patch(f"/api/v1/workspaces/{wid}/models/{model_id}", json={"thinking_format": "qwen-chat-template"}).status_code == 200
    c.post(f"/api/v1/workspaces/{wid}/jobs/{ids['job_id']}/cancel")
    retry = c.post(f"/api/v1/workspaces/{wid}/jobs/{ids['job_id']}/retry")
    assert retry.status_code == 201
    with SessionLocal() as db:
        assert db.get(Job, ids["job_id"]).input_snapshot["model"]["thinking_format"] == "default"
        assert db.get(Job, retry.json()["job_id"]).input_snapshot["model"]["thinking_format"] == "qwen-chat-template"


@pytest.mark.parametrize("requested,expected", [(None, "guided"), ("guided", "guided"), ("native", "native")])
def test_workflow_mode_is_defaulted_exposed_and_frozen(env, monkeypatch, requested, expected):
    model, paper = resources(env)
    c, wid = env["client"], env["alpha"]
    monkeypatch.setattr("app.api.skill_catalog", lambda: [{"id": "deeppapernote", "available": True}])
    payload = {"paper_id": paper["id"], "model_connection_id": model["id"], "template_id": env["builtin"], "skill_id": "deeppapernote"}
    if requested is not None:
        payload["workflow_mode"] = requested
    response = c.post(f"/api/v1/workspaces/{wid}/readings", json=payload)
    assert response.status_code == 201
    ids = response.json()
    with SessionLocal() as db:
        assert db.get(Reading, ids["reading_id"]).workflow_mode == expected
        assert db.get(Job, ids["job_id"]).input_snapshot["workflow_mode"] == expected
    detail = c.get(f"/api/v1/workspaces/{wid}/readings/{ids['reading_id']}").json()
    assert detail["workflow_mode"] == expected
    assert detail["jobs"][0]["stage_records"] == []
    assert c.get(f"/api/v1/workspaces/{wid}/readings").json()[0]["workflow_mode"] == expected
    assert c.post(f"/api/v1/workspaces/{wid}/readings", json={**payload, "workflow_mode": "unchecked"}).status_code == 422
    assert c.post(f"/api/v1/workspaces/{wid}/readings", json={**payload, "retry_of_job_id": ids["job_id"]}).status_code == 422


def test_retry_parent_is_server_authorized_and_mode_preserved(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, wid, jid = env["client"], env["alpha"], ids["job_id"]
    c.post(f"/api/v1/workspaces/{wid}/jobs/{jid}/cancel")
    assert c.post(f"/api/v1/workspaces/{wid}/jobs/{jid}/retry", json={"retry_of_job_id": "someone-elses-job"}).status_code == 422
    assert c.post(f"/api/v1/workspaces/{wid}/jobs/{jid}/retry", json={"checkpoint_path": "/private"}).status_code == 422
    retry = c.post(f"/api/v1/workspaces/{wid}/jobs/{jid}/retry")
    assert retry.status_code == 201
    with SessionLocal() as db:
        old, new = db.get(Job, jid), db.get(Job, retry.json()["job_id"])
        assert "retry_of_job_id" not in old.input_snapshot
        assert new.input_snapshot["retry_of_job_id"] == jid
        assert new.input_snapshot["workflow_mode"] == old.input_snapshot["workflow_mode"] == "guided"
        assert new.stage_records == []
    login(c, "bob")
    assert c.post(f"/api/v1/workspaces/{env['beta']}/jobs/{jid}/retry").status_code == 404


def test_retry_of_legacy_snapshot_uses_native_mode(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, wid, jid = env["client"], env["alpha"], ids["job_id"]
    with SessionLocal() as db:
        old = db.get(Job, jid)
        old.input_snapshot = {key: value for key, value in old.input_snapshot.items() if key != "workflow_mode"}
        # Legacy interpretation follows the frozen snapshot, not today's default.
        assert db.get(Reading, ids["reading_id"]).workflow_mode == "guided"
        db.commit()
    c.post(f"/api/v1/workspaces/{wid}/jobs/{jid}/cancel")
    retry = c.post(f"/api/v1/workspaces/{wid}/jobs/{jid}/retry", json={})
    assert retry.status_code == 201
    with SessionLocal() as db:
        new = db.get(Job, retry.json()["job_id"])
        assert new.input_snapshot["workflow_mode"] == "native"
        assert new.input_snapshot["retry_of_job_id"] == jid


def test_packet_and_stage_records_are_visible_only_in_own_workspace(env, monkeypatch):
    ids = create_reading(env, monkeypatch)
    c, wid, rid = env["client"], env["alpha"], ids["reading_id"]
    packet = {"version": 1, "paper_title": "Test paper", "research_question": "What changes?", "claims": [], "open_questions": [], "limitations": ["Fixture, not a published model result"]}
    stage = {"name": "source", "status": "succeeded", "input_hash": "source-hash", "output_hash": "output-hash", "duration_ms": 20}
    with SessionLocal() as db:
        job = db.get(Job, ids["job_id"])
        job.stage_records = [stage]
        job.status = "succeeded"
        revision = Revision(reading_id=rid, workspace_id=wid, number=1, brief_markdown="Fixture", full_markdown="Fixture", reading_packet=packet)
        db.add(revision)
        db.flush()
        db.get(Reading, rid).current_revision_id = revision.id
        db.commit()
    detail = c.get(f"/api/v1/workspaces/{wid}/readings/{rid}").json()
    assert detail["revisions"][0]["reading_packet"] == packet
    assert detail["jobs"][0]["stage_records"] == [stage]
    assert c.get(f"/api/v1/workspaces/{wid}/jobs/{ids['job_id']}").json()["stage_records"] == [stage]
    login(c, "bob")
    assert c.get(f"/api/v1/workspaces/{wid}/readings/{rid}").status_code == 404
    assert c.get(f"/api/v1/workspaces/{env['beta']}/readings/{rid}").status_code == 404
    assert c.get(f"/api/v1/workspaces/{env['beta']}/jobs/{ids['job_id']}").status_code == 404


def test_workflow_migration_backfills_native_and_keeps_new_guided_default(env, monkeypatch):
    from sqlalchemy import inspect
    ids = create_reading(env, monkeypatch)
    with SessionLocal() as db:
        revision = Revision(reading_id=ids["reading_id"], workspace_id=env["alpha"], number=1, brief_markdown="Legacy result", full_markdown="Legacy result")
        db.add(revision)
        db.commit()
        revision_id = revision.id
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.downgrade(cfg, "2e83a0479c61")
    command.upgrade(cfg, "head")
    with SessionLocal() as db:
        assert db.get(Reading, ids["reading_id"]).workflow_mode == "native"
        assert db.get(Job, ids["job_id"]).stage_records == []
        assert db.get(Revision, revision_id).reading_packet == {}
        old = db.get(Reading, ids["reading_id"])
        new = Reading(workspace_id=old.workspace_id, paper_id=old.paper_id, created_by=old.created_by, skill_id=old.skill_id, model_connection_id=old.model_connection_id, template_id=old.template_id)
        db.add(new)
        db.commit()
        assert new.workflow_mode == "guided"
    column = next(column for column in inspect(engine()).get_columns("readings") if column["name"] == "workflow_mode")
    assert "guided" in column["default"] and column["nullable"] is False
    command.check(cfg)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    command.check(cfg)
