"""Title extraction tests: real isolated PostgreSQL, explicitly mocked model I/O."""
from datetime import timedelta
import json

import fitz
import pytest

from app import paper_titles as titles
from app.db import SessionLocal
from app.models import Paper, now
from app.source_tools import Download
from test_api import env, login, resources  # noqa: F401


def model_reply(original="Attention Is All You Need", chinese="注意力就是你所需要的一切"):
    return 200, {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"function": {"name": "save_paper_title", "arguments": json.dumps({"original_title": original, "chinese_title": chinese})}}
    ]}}]}


def upload(env, *, filename="1706.03762.pdf"):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((60, 60), "Attention Is All You Need", fontsize=20)
    page.insert_text((60, 100), "Authors\nAbstract\nA paper about attention and neural networks.")
    document.set_metadata({"title": "Microsoft Word - draft"})
    response = env["client"].post(f"/api/v1/workspaces/{env['alpha']}/papers/upload",
                                files={"file": (filename, document.tobytes(), "application/pdf")})
    assert response.status_code == 201
    document.close()
    return response.json()


def test_pdf_import_extracts_real_title_and_translation_search(env, monkeypatch):
    model, placeholder = resources(env)
    with SessionLocal() as db, db.begin():
        db.get(Paper, placeholder["id"]).metadata_json = {"title_status": "ready"}
    paper = upload(env)
    assert paper["title"] == "Microsoft Word - draft"
    def call(url, **kwargs):
        prompt = str(kwargs["payload"]["messages"])
        assert "Attention Is All You Need" in prompt
        assert "1706.03762.pdf" not in prompt
        return model_reply()
    monkeypatch.setattr(titles, "post_model_json", call)
    assert titles.process_one_title()
    assert not titles.process_one_title()
    result = env["client"].get(f"/api/v1/workspaces/{env['alpha']}/papers?search=注意力").json()
    assert len(result) == 1
    assert result[0]["title"] == "Attention Is All You Need"
    assert result[0]["metadata_json"]["title_zh"] == "注意力就是你所需要的一切"
    assert result[0]["metadata_json"]["pages"] == 1
    assert result[0]["metadata_json"]["title_status"] == "ready"
    assert "fixture-not-real-key" not in json.dumps(result)


def test_legacy_arxiv_gets_official_title_then_resumes_when_model_added(env, monkeypatch):
    login(env["client"])
    response = env["client"].post(f"/api/v1/workspaces/{env['alpha']}/papers", json={"arxiv": "1706.03762"})
    pid = response.json()["id"]
    with SessionLocal() as db, db.begin():
        db.get(Paper, pid).metadata_json = {"arxiv_id": "1706.03762"}  # pre-feature record
    monkeypatch.setattr(titles, "download_public", lambda *a, **kw: Download(
        b'<meta name="citation_title" content="Attention Is All You Need">', "https://arxiv.org/abs/1706.03762", "text/html"))
    assert titles.process_one_title()
    assert not titles.process_one_title()
    with SessionLocal() as db:
        paper = db.get(Paper, pid)
        assert paper.title == "Attention Is All You Need"
        assert paper.metadata_json["title_status"] == "waiting_model"
    resources(env)
    monkeypatch.setattr(titles, "post_model_json", lambda *a, **kw: model_reply())
    assert titles.process_one_title()
    with SessionLocal() as db:
        assert db.get(Paper, pid).metadata_json["title_status"] == "ready"


def test_retries_are_scoped_and_reject_hallucinated_titles(env, monkeypatch):
    _, paper = resources(env)
    pid, wid = paper["id"], env["alpha"]
    with SessionLocal() as db, db.begin():
        record = db.get(Paper, pid)
        record.metadata_json = {"title_status": "failed", "title_zh": "旧译名", "pages": 5}
    route = f"/api/v1/workspaces/{wid}/papers/{pid}/title"
    login(env["client"], "bob")
    assert env["client"].post(route).status_code == 404
    assert env["client"].post(f"/api/v1/workspaces/{env['beta']}/papers/{pid}/title").status_code == 404
    login(env["client"])
    assert env["client"].post(route).status_code == 202
    monkeypatch.setattr(titles, "title_source", lambda *a, **kw: ("", "Attention Is All You Need\nAbstract: A paper about neural networks and attention.", None))
    calls = []
    def hallucination(*args, **kwargs):
        calls.append(True)
        return model_reply("Invented Summary", "编造的标题")
    monkeypatch.setattr(titles, "post_model_json", hallucination)
    assert titles.process_one_title()
    assert calls == [True]
    with SessionLocal() as db, db.begin():
        record = db.get(Paper, pid)
        assert record.title == "Test paper"
        assert record.metadata_json["title_status"] == "failed"
        assert record.metadata_json["title_zh"] == "旧译名"
        assert record.metadata_json["pages"] == 5
        record.metadata_json = {**record.metadata_json, "title_status": "processing", "title_task_token": "old",
                                "title_started_at": (now() - timedelta(minutes=10)).isoformat()}
    # Stale claims recover after a restart; a current claim cannot be superseded.
    response = env["client"].post(route)
    assert response.json()["metadata_json"]["title_task_token"] == "old"
    monkeypatch.setattr(titles, "title_source", lambda *a, **kw: ("Attention Is All You Need", "", None))
    monkeypatch.setattr(titles, "post_model_json", lambda *a, **kw: model_reply())
    assert titles.process_one_title()
    with SessionLocal() as db:
        assert db.get(Paper, pid).metadata_json["title_status"] == "ready"


def test_chinese_original_does_not_call_model_or_duplicate_subtitle(env, monkeypatch):
    _, paper = resources(env)
    monkeypatch.setattr(titles, "title_source", lambda *a, **kw: ("面向语言模型的高效微调方法", "", None))
    monkeypatch.setattr(titles, "post_model_json", lambda *a, **kw: pytest.fail("Chinese original needs no translation"))
    assert titles.process_one_title()
    with SessionLocal() as db:
        result = db.get(Paper, paper["id"])
        assert result.title == "面向语言模型的高效微调方法"
        assert result.metadata_json["title_zh"] == ""
