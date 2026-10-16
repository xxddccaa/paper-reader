"""Real PostgreSQL tests for worker fencing; never a production model fallback."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import os
import threading

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select, text
from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.models import Job, JobEvent, ModelConnection, Paper, Reading, Revision, Template, User, Workspace, new_id, now
from app.pipeline import Pipeline, PipelineError, TaskCancelled
from app.security import encrypt_secret
from app.source_tools import SourceDocument
from app.worker import claim_job, finish_failure, heartbeat, publish_result, report_progress, LeaseLost


@pytest.fixture
def worker_db(monkeypatch, tmp_path):
    url = os.environ.get("PAPER_READER_WORKER_TEST_DATABASE_URL") or os.environ.get("PAPER_READER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("An explicit PAPER_READER_WORKER_TEST_DATABASE_URL or PAPER_READER_TEST_DATABASE_URL is required")
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("Worker tests require an explicitly isolated *_test database")
    monkeypatch.setenv("PAPER_READER_DATABASE_URL", url)
    monkeypatch.setenv("PAPER_READER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PAPER_READER_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_READER_LEASE_SECONDS", "30")
    monkeypatch.setenv("PAPER_READER_MAX_RUNNING_JOBS", "2")
    monkeypatch.setenv("PAPER_READER_MAX_JOBS_PER_WORKSPACE", "1")
    get_settings.cache_clear()
    engine.cache_clear()
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")
    with engine().begin() as connection:
        tables = ", ".join('"' + table.name + '"' for table in reversed(Base.metadata.sorted_tables))
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield tmp_path
    engine().dispose()
    engine.cache_clear()
    get_settings.cache_clear()


def seed_job(workspace=None):
    with SessionLocal() as db, db.begin():
        user = User(id=new_id(), username=new_id(), password_hash="unused-test-hash")
        db.add(user)
        if workspace is None:
            workspace = Workspace(id=new_id(), name="Worker test")
            db.add(workspace)
        db.flush()
        model = ModelConnection(id=new_id(), workspace_id=workspace.id, name="Fixture connection", base_url="https://example.com/v1",
                                model="fixture-model", protocol="openai-completions", encrypted_api_key=encrypt_secret("test-only"))
        template = Template(id=new_id(), workspace_id=workspace.id, owner_id=user.id, name="Template")
        paper = Paper(id=new_id(), workspace_id=workspace.id, created_by=user.id, title="Test paper", source_type="arxiv", source_url="https://arxiv.org/abs/2106.10199")
        db.add_all([model, template, paper]); db.flush()
        reading = Reading(id=new_id(), workspace_id=workspace.id, paper_id=paper.id, created_by=user.id, skill_id="ljg-paper", model_connection_id=model.id, template_id=template.id)
        db.add(reading); db.flush()
        snapshot = {"skill_id": "ljg-paper", "paper_id": paper.id, "instruction": "", "base_revision_id": None,
                    "template": {"id": template.id, "version_id": new_id(), "brief_markdown": "Brief", "full_markdown": "Full", "instructions": ""},
                    "model": {field: getattr(model, field) for field in ("id", "name", "base_url", "model", "protocol", "vision", "context_window", "max_tokens")}}
        job = Job(id=new_id(), workspace_id=workspace.id, reading_id=reading.id, created_by=user.id, kind="read", status="queued", stage="queued", progress=0, input_snapshot=snapshot)
        db.add(job); db.flush()
        return job, workspace


def test_concurrent_claims_respect_team_limit_and_skip_locked(worker_db):
    first, workspace = seed_job()
    second, _ = seed_job(workspace)
    third, _ = seed_job()
    with ThreadPoolExecutor(max_workers=3) as pool:
        claimed = list(pool.map(lambda _: claim_job(), range(3)))
    values = [j for j in claimed if j]
    assert len(values) == 2
    assert len({j.id for j in values}) == 2
    assert len({j.workspace_id for j in values}) == 2
    assert all(j.attempt == 1 and j.lease_token for j in values)


def test_expired_lease_recovery_and_old_worker_fencing(worker_db):
    original, _ = seed_job()
    claimed = claim_job()
    old_token = claimed.lease_token
    with SessionLocal() as db, db.begin():
        job = db.get(Job, original.id)
        job.lease_expires_at = now() - timedelta(seconds=1)
    assert heartbeat(original.id, old_token) is False
    recovered = claim_job()
    assert recovered.id == original.id and recovered.attempt == 2
    assert recovered.lease_token != old_token
    with pytest.raises(LeaseLost):
        report_progress(original.id, old_token, "stale")
    finish_failure(original.id, old_token, "must not overwrite")
    with SessionLocal() as db:
        assert db.get(Job, original.id).status == "running"


def test_recovery_exhaustion_and_queued_cancellation(worker_db):
    job, _ = seed_job()
    claimed = claim_job()
    with SessionLocal() as db, db.begin():
        row = db.get(Job, job.id)
        row.attempt = 2
        row.lease_expires_at = now() - timedelta(seconds=1)
    assert claim_job() is None
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == "failed"
    second, _ = seed_job()
    with SessionLocal() as db, db.begin():
        db.get(Job, second.id).cancel_requested = True
    assert claim_job() is None
    with SessionLocal() as db:
        assert db.get(Job, second.id).status == "cancelled"


def publication_fixture(job, root):
    work = root / "tmp" / job.id / str(job.attempt)
    work.mkdir(parents=True)
    (work / "source.txt").write_text("The method improves accuracy from 70 percent to 75 percent.")
    pipeline = Pipeline(work, Path(__file__).resolve().parents[2] / "skills/ljg-paper", {"id": job.input_snapshot["paper_id"], "title": "Paper"}, "ljg-paper", job.workspace_id, threading.Event(), Path(__file__).resolve().parents[2])
    pipeline.library.documents["main"] = SourceDocument("main", "Paper", None, "source.txt", [{"id": "p1", "page": 1, "text": (work / "source.txt").read_text()}], {"full_text": True}, "testhash")
    pipeline.library._save()
    pipeline.inspected_skill_files.add("SKILL.md")
    # Explicit isolated transaction harness: native integrity is covered separately.
    pipeline.verify_native_seal = lambda: None
    result = {"brief_markdown": "结果 [E1](evidence:E1)", "full_markdown": "完整结果 [E1](evidence:E1)",
              "evidence_index": [{"id": "E1", "source_id": "main", "quote": "accuracy from 70 percent to 75 percent", "kind": "paper"}],
              "verification": {}, "provenance": {}, "artifacts": [{"path": "source.txt"}]}
    return pipeline, result


def test_publication_immutable_artifacts_and_stale_writer(worker_db):
    job, _ = seed_job()
    job = claim_job()
    pipeline, result = publication_fixture(job, worker_db)
    revision_id = publish_result(job.id, job.lease_token, result, pipeline, {"input_tokens": 12})
    with SessionLocal() as db:
        revision = db.get(Revision, revision_id)
        assert revision.number == 1
        assert revision.evidence_index[0]["verified"] is True
        assert db.get(Job, job.id).status == "partial"  # no native/check-paper claim
        assert db.get(Reading, job.reading_id).current_revision_id == revision_id
        paths = [worker_db / artifact["path"] for artifact in revision.artifacts]
        assert all(path.is_file() for path in paths)
        assert all(job.workspace_id in str(path) for path in paths)
    with pytest.raises(LeaseLost):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert len(db.scalars(select(Revision)).all()) == 1


def test_cancel_prevents_publication_and_preserves_no_result(worker_db):
    job, _ = seed_job(); job = claim_job()
    pipeline, result = publication_fixture(job, worker_db)
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).cancel_requested = True
    assert heartbeat(job.id, job.lease_token) is False
    with pytest.raises(TaskCancelled):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    finish_failure(job.id, job.lease_token, "cancelled", cancelled=True)
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == "cancelled"
        assert db.get(Reading, job.reading_id).current_revision_id is None


def test_result_base_is_serialized_against_other_revision(worker_db):
    job, _ = seed_job(); job = claim_job()
    pipeline, result = publication_fixture(job, worker_db)
    with SessionLocal() as db, db.begin():
        existing = Revision(id=new_id(), reading_id=job.reading_id, workspace_id=job.workspace_id, number=1,
                            brief_markdown="Existing", full_markdown="Existing", evidence_index=[], verification={}, provenance={}, artifacts=[])
        db.add(existing)
        db.get(Reading, job.reading_id).current_revision_id = existing.id
    with pytest.raises(PipelineError, match="changed"):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert len(db.scalars(select(Revision)).all()) == 1


def test_graceful_stop_requeues_with_checkpoint(worker_db):
    from app.worker import release_for_shutdown
    job, _ = seed_job(); job = claim_job()
    release_for_shutdown(job.id, job.lease_token)
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "queued" and row.lease_token is None
    replacement = claim_job()
    assert replacement.id == job.id and replacement.attempt == 2


def protocol_fixture(worker_db, *, wait=False):
    """An explicit test-only JSONL process; never referenced by production code."""
    import fitz
    job, _ = seed_job()
    source = worker_db / "storage" / "workspaces" / job.workspace_id / "papers" / "source.pdf"
    source.parent.mkdir(parents=True)
    pdf = fitz.open(); page = pdf.new_page()
    page.insert_text((50, 50), "The method improves accuracy from 70 percent to 75 percent.\nAbstract and Methods\nAn experiment compares the finetuned baseline and model averaging.")
    pdf.save(source); pdf.close()
    with SessionLocal() as db, db.begin():
        paper = db.get(Paper, job.input_snapshot["paper_id"])
        paper.source_type = "pdf"
        paper.storage_path = str(source.relative_to(worker_db))
    project = worker_db / "protocol-fixture"
    (project / "agent-runner/dist").mkdir(parents=True)
    (project / "skills/ljg-paper").mkdir(parents=True)
    (project / "skills/ljg-paper/SKILL.md").write_text("Test-only protocol fixture guidance")
    if wait:
        script = r'''
const readline=require('readline');const fs=require('fs');const child=require('child_process');
readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);if(m.type==='run'){
const p=child.spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});fs.writeFileSync('child.pid',String(p.pid));
process.stdout.write(JSON.stringify({type:'progress',stage:'fixture_wait',progress:5})+'\n');}});
setInterval(()=>{},1000);
'''
    else:
        script = r'''
const readline=require('readline');let stage=0;
const send=x=>process.stdout.write(JSON.stringify(x)+'\n');
readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);
if(m.type==='run')send({type:'tool_request',id:'source',name:'prepare_source',args:{}});
if(m.type==='tool_response'&&m.id==='source')send({type:'tool_request',id:'skill',name:'read_skill_file',args:{path:'SKILL.md'}});
if(m.type==='tool_response'&&m.id==='skill'){
send({type:'usage',usage:{input_tokens:10,output_tokens:5}});
send({type:'result',result:{brief_markdown:'Result [E1](evidence:E1)',full_markdown:'Detailed [E1](evidence:E1)',evidence_index:[{id:'E1',source_id:'main',quote:'accuracy from 70 percent to 75 percent',kind:'paper'}],verification:{fixture:true},provenance:{fixture:true},artifacts:[]}});process.exit(0);}});
'''
    (project / "agent-runner/dist/index.js").write_text(script)
    get_settings().project_dir = project
    return job


def test_real_jsonl_worker_protocol_harness(worker_db, monkeypatch):
    # Isolate transport/state behavior; no production fallback exists.
    monkeypatch.setattr(Pipeline, "verify_native_seal", lambda self: None)
    from app.worker import Worker
    job = protocol_fixture(worker_db)
    Worker().run(once=True)
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "partial", row.error
        assert row.usage["input_tokens"] == 10
        reading = db.get(Reading, job.reading_id)
        assert reading.current_revision_id
        revision = db.get(Revision, reading.current_revision_id)
        assert revision.evidence_index[0]["verified"]
        assert revision.verification["agent_report"]["fixture"]
        assert "api_key" not in str(revision.provenance)


def test_worker_cancel_terminates_runner_and_child_process(worker_db):
    import time
    from app.worker import Worker
    job = protocol_fixture(worker_db, wait=True)
    get_settings().lease_seconds = 5
    worker = Worker()
    thread = threading.Thread(target=lambda: worker.run(once=True))
    thread.start()
    child_pid = worker_db / "tmp" / job.id / "1" / "child.pid"
    deadline = time.monotonic() + 10
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_pid.exists()
    pid = int(child_pid.read_text())
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).cancel_requested = True
    thread.join(timeout=10)
    assert not thread.is_alive()
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == "cancelled"
    status = Path(f"/proc/{pid}/stat")
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_saturated_tenant_cannot_starve_later_eligible_jobs(worker_db):
    job, workspace = seed_job(); running = claim_job()
    with SessionLocal() as db, db.begin():
        original = db.get(Reading, job.reading_id)
        for _ in range(120):
            reading = Reading(id=new_id(), workspace_id=workspace.id, paper_id=original.paper_id, created_by=original.created_by,
                              skill_id=original.skill_id, model_connection_id=original.model_connection_id, template_id=original.template_id)
            db.add(reading); db.flush()
            db.add(Job(id=new_id(), workspace_id=workspace.id, reading_id=reading.id, created_by=job.created_by,
                       kind="read", status="queued", stage="queued", input_snapshot=job.input_snapshot))
    eligible, _ = seed_job()
    assert claim_job().id == eligible.id


def test_usage_deltas_persist_on_failure_and_across_recovery(worker_db):
    from app.worker import record_usage
    job, _ = seed_job(); job = claim_job()
    record_usage(job.id, job.lease_token, {"input_tokens": 10, "output_tokens": 3})
    record_usage(job.id, job.lease_token, {"input_tokens": 20, "output_tokens": 4})
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).lease_expires_at = now() - timedelta(seconds=1)
    recovered = claim_job()
    result = record_usage(recovered.id, recovered.lease_token, {"input_tokens": 5, "output_tokens": 2})
    assert result["input_tokens"] == 35 and result["output_tokens"] == 9
    assert result["attempts"]["1"]["input_tokens"] == 30
    assert result["attempts"]["2"]["input_tokens"] == 5
    finish_failure(recovered.id, recovered.lease_token, "Provider error")
    with SessionLocal() as db:
        assert db.get(Job, job.id).usage == result
        assert db.get(Job, job.id).status == "failed"


def test_changed_model_is_rejected_before_decryption(worker_db, monkeypatch):
    import app.worker as worker_module
    job, _ = seed_job(); job = claim_job()
    with SessionLocal() as db, db.begin():
        db.get(ModelConnection, job.input_snapshot["model"]["id"]).base_url = "https://changed.example/v1"
    decrypt_calls = []
    monkeypatch.setattr(worker_module, "decrypt_secret", lambda value: decrypt_calls.append(value))
    work = worker_db / "tmp" / job.id / "1"; work.mkdir(parents=True)
    with pytest.raises(PipelineError, match="Model configuration changed"):
        worker_module.build_spec(job, work)
    assert decrypt_calls == []


def test_native_authenticity_failure_is_hard_publication_gate(worker_db):
    job, _ = seed_job(); job = claim_job()
    pipeline, result = publication_fixture(job, worker_db)
    pipeline.verify_native_seal = Pipeline.verify_native_seal.__get__(pipeline, Pipeline)
    with pytest.raises(PipelineError, match="seal"):
        publish_result(job.id, job.lease_token, result, pipeline, {})
    with SessionLocal() as db:
        assert db.get(Reading, job.reading_id).current_revision_id is None
        assert not db.scalars(select(Revision)).all()


def test_terminal_cleanup_preserves_retriable_jobs(worker_db):
    from app.worker import Worker
    active, _ = seed_job()
    finished, _ = seed_job()
    for job in (active, finished):
        directory = worker_db / "tmp" / job.id / "1"; directory.mkdir(parents=True)
        (directory / "checkpoint.txt").write_text("Saved artifact")
    with SessionLocal() as db, db.begin():
        row = db.get(Job, finished.id); row.status = "failed"; row.updated_at = now() - timedelta(days=8)
    Worker().cleanup_terminal_artifacts()
    assert (worker_db / "tmp" / active.id / "1/checkpoint.txt").is_file()
    assert not (worker_db / "tmp" / finished.id).exists()


def test_worker_recovers_sealed_native_checkpoint_without_rerunning_scripts(worker_db):
    import hashlib
    import json
    import fitz
    from app.worker import Worker
    job = protocol_fixture(worker_db)
    first = claim_job()
    work = worker_db / "tmp" / job.id / "1"; work.mkdir(parents=True)
    with SessionLocal() as db:
        paper = db.get(Paper, job.input_snapshot["paper_id"])
        paper_spec = {"id": paper.id, "title": paper.title, "source_type": "pdf", "storage_path": str(worker_db / paper.storage_path)}
    pipeline = Pipeline(work, get_settings().project_dir / "skills/ljg-paper", paper_spec, "ljg-paper", job.workspace_id, threading.Event(), get_settings().project_dir)
    pipeline.library.prepare_source()
    pipeline.write_artifact("native/note.md", "Reviewed checkpoint note")
    pipeline.write_artifact("native/paper-map.md", "Reviewed checkpoint map")
    # Explicit host-state fixture; this regression tests durable recovery, not
    # upstream reading quality or whether a model's native note passes its skill.
    pipeline.native_checks = [{"script": "validate_note.ts", "exit_code": 0, "semantic_pass": True,
                              "args": ["native/note.md", "--map", "native/paper-map.md"],
                              "input_hashes": {name: pipeline._fingerprint(work / name) for name in ("native/note.md", "native/paper-map.md")}, "output_hashes": {}}]
    (work / "native-execution-ledger.json").write_text(json.dumps(pipeline.native_checks))
    pipeline.host_call("host_native_attest", {"note_path": "native/note.md", "note_sha256": hashlib.sha256((work / "native/note.md").read_bytes()).hexdigest(),
                       "map_path": "native/paper-map.md", "map_sha256": hashlib.sha256((work / "native/paper-map.md").read_bytes()).hexdigest(),
                       "quality": {"passed": True, "fixture": True}, "readability": {"passed": True, "fixture": True}})
    pipeline.host_call("host_native_seal", {})
    fixture_script = r'''
const readline=require('readline');const send=x=>process.stdout.write(JSON.stringify(x)+'\n');
readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);
if(m.type==='run')send({type:'tool_request',id:'resume',name:'host_native_status',args:{}});
if(m.type==='tool_response'&&m.id==='resume'){
if(!m.result?.sealed){send({type:'error',message:'Expected verified sealed recovery'});process.exit(2);}
send({type:'tool_request',id:'skill',name:'read_skill_file',args:{path:'SKILL.md'}});}
if(m.type==='tool_response'&&m.id==='skill'){
send({type:'result',result:{brief_markdown:'Result [E1](evidence:E1)',full_markdown:'Detailed [E1](evidence:E1)',evidence_index:[{id:'E1',source_id:'main',quote:'accuracy from 70 percent to 75 percent',kind:'paper'}],verification:{fixture:true},provenance:{native_reused:true,fixture:true},artifacts:[]}});process.exit(0);}});
'''
    (get_settings().project_dir / "agent-runner/dist/index.js").write_text(fixture_script)
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).lease_expires_at = now() - timedelta(seconds=1)
    Worker().run(once=True)
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "partial", row.error
        assert row.attempt == 2
        revision = db.get(Revision, db.get(Reading, job.reading_id).current_revision_id)
        assert revision.provenance["native_reused"] is True
    assert not (worker_db / "tmp" / job.id).exists()


def test_retention_advances_past_missing_first_two_hundred_directories(worker_db):
    from app.worker import Worker
    existing, _ = seed_job()
    with SessionLocal() as db, db.begin():
        original = db.get(Job, existing.id)
        # Ordered IDs make the first page deterministically contain no directories.
        for index in range(205):
            db.add(Job(id=f"00000000-0000-0000-0000-{index:012d}", workspace_id=original.workspace_id,
                       reading_id=original.reading_id, created_by=original.created_by, kind="read", status="failed",
                       stage="failed", input_snapshot=original.input_snapshot, updated_at=now() - timedelta(days=8)))
    stale_id = "00000000-0000-0000-0000-000000000204"
    stale = worker_db / "tmp" / stale_id / "1"; stale.mkdir(parents=True)
    (stale / "diagnostic.txt").write_text("Old retained diagnostic")
    active = worker_db / "tmp" / existing.id / "1"; active.mkdir(parents=True)
    (active / "checkpoint.txt").write_text("Queued checkpoint must survive")
    Worker().cleanup_terminal_artifacts()
    assert not stale.exists()
    assert (active / "checkpoint.txt").is_file()


def test_failed_attempt_pruning_advances_beyond_first_page(worker_db):
    from app.worker import Worker
    existing, _ = seed_job()
    with SessionLocal() as db, db.begin():
        original = db.get(Job, existing.id)
        for index in range(205):
            db.add(Job(id=f"00000000-0000-0000-0001-{index:012d}", workspace_id=original.workspace_id,
                       reading_id=original.reading_id, created_by=original.created_by, kind="read", status="failed",
                       stage="failed", input_snapshot=original.input_snapshot))
    identifier = "00000000-0000-0000-0001-000000000204"
    directory = worker_db / "tmp" / identifier
    for number in (1, 2, 3):
        path = directory / str(number); path.mkdir(parents=True)
        (path / "diagnostic.txt").write_text("Recent failed diagnostic")
    Worker().cleanup_terminal_artifacts()
    assert not (directory / "1").exists()
    assert (directory / "2/diagnostic.txt").is_file()
    assert (directory / "3/diagnostic.txt").is_file()


@pytest.mark.parametrize("limit", [40, 160])
def test_configured_tool_budget_propagates_to_runner_spec(worker_db, monkeypatch, limit):
    from app.worker import build_spec
    monkeypatch.setenv("PAPER_READER_MAX_TOOL_CALLS", str(limit))
    get_settings.cache_clear()
    job, _ = seed_job()
    work = worker_db / "tool-budget"
    work.mkdir()
    spec, _ = build_spec(job, work)
    assert spec["limits"]["max_tool_calls"] == limit
    assert spec["limits"]["timeout_seconds"] == get_settings().job_timeout_seconds
    assert spec["limits"]["max_related_papers"] == 3


@pytest.mark.parametrize("limit,last_tool", [(40, "list_artifacts"), (160, "host_native_status")])
def test_worker_enforces_frozen_spec_budget_and_terminates_on_exhaustion(worker_db, monkeypatch, limit, last_tool):
    import app.worker as worker_module
    job = protocol_fixture(worker_db)
    # A different live settings value proves execution uses the frozen run spec.
    get_settings().max_tool_calls = 10
    build_spec = worker_module.build_spec

    def frozen_spec(*args):
        spec, secret = build_spec(*args)
        spec["limits"]["max_tool_calls"] = limit
        return spec, secret

    monkeypatch.setattr(worker_module, "build_spec", frozen_spec)
    script = r'''
const readline=require('readline');const fs=require('fs');let count=0;let limit=0;
const send=x=>process.stdout.write(JSON.stringify(x)+'\n');
const request=()=>{const next=count+1;send({type:'tool_request',id:String(next),name:next>limit?'LAST_TOOL':next%2?'host_native_status':'list_artifacts',args:{}});};
readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);
if(m.type==='run'){limit=m.limits.max_tool_calls;fs.writeFileSync('runner.pid',String(process.pid));request();}
if(m.type==='tool_response'){
if(m.error){send({type:'error',message:'Budget was incorrectly returned as a recoverable RPC error'});process.exit(2);}
count++;fs.writeFileSync('rpc-count.txt',String(count));
if(count>limit){send({type:'error',message:'Configured frozen budget was not enforced'});process.exit(2);}
request();}});
'''.replace("LAST_TOOL", last_tool)
    (get_settings().project_dir / "agent-runner/dist/index.js").write_text(script)
    worker_module.Worker().run(once=True)
    directory = worker_db / "tmp" / job.id / "1"
    assert (directory / "rpc-count.txt").read_text() == str(limit)
    with SessionLocal() as db:
        row = db.get(Job, job.id)
        assert row.status == "failed"
        assert row.error == f"Tool call budget exhausted ({limit} calls)"
        assert row.lease_token is None
        assert db.get(Reading, job.reading_id).current_revision_id is None
        assert not db.scalars(select(Revision)).all()
    pid = int((directory / "runner.pid").read_text())
    process = Path(f"/proc/{pid}/stat")
    assert not process.exists() or process.read_text().split()[2] == "Z"


def test_legacy_model_snapshot_defaults_thinking_format(worker_db):
    from app.worker import build_spec
    job, _ = seed_job()
    assert "thinking_format" not in job.input_snapshot["model"]
    work = worker_db / "legacy-thinking-format"
    work.mkdir()
    spec, _ = build_spec(job, work)
    assert spec["model"]["thinking_format"] == "default"
    assert "thinking_format" not in job.input_snapshot["model"]


def test_legacy_snapshot_rejects_changed_thinking_format_before_decrypt(worker_db, monkeypatch):
    from app import worker as worker_module
    job, _ = seed_job()
    with SessionLocal() as db, db.begin():
        model = db.get(ModelConnection, job.input_snapshot["model"]["id"])
        model.thinking_format = "qwen-chat-template"
    work = worker_db / "stale-thinking-format"
    work.mkdir()
    monkeypatch.setattr(worker_module, "decrypt_secret", lambda _: pytest.fail("Do not decrypt keys for stale snapshots"))
    with pytest.raises(PipelineError, match="Model configuration changed"):
        worker_module.build_spec(job, work)


def test_selected_thinking_format_propagates_to_runner(worker_db):
    from app.worker import build_spec
    job, _ = seed_job()
    with SessionLocal() as db, db.begin():
        model = db.get(ModelConnection, job.input_snapshot["model"]["id"])
        model.thinking_format = "qwen-chat-template"
        snapshot = {**job.input_snapshot, "model": {**job.input_snapshot["model"], "thinking_format": "qwen-chat-template"}}
        db.get(Job, job.id).input_snapshot = snapshot
        job.input_snapshot = snapshot
    work = worker_db / "selected-thinking-format"
    work.mkdir()
    spec, _ = build_spec(job, work)
    assert spec["model"]["thinking_format"] == "qwen-chat-template"
