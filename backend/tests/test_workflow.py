"""Guided stages through real JSONL child processes and isolated PostgreSQL.

The deliberately labeled fixture process tests the host orchestration contract;
it never calls or substitutes for a production model/provider.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Job, Reading, Revision, now, new_id
from app.pipeline import PipelineError
from app.worker import Worker, claim_job, record_workflow_stage, LeaseLost
from app.workflow import apply_product_patch, validate_phase
from test_worker_durability import worker_db, protocol_fixture


SCRIPT = r'''
const fs=require('fs'), readline=require('readline');
const home=__dirname;
const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
let pendingResult=null, queue=[], cursor=0;
function finish(){send({type:'usage',usage:{input_tokens:11,output_tokens:7}});send({type:'result',result:pendingResult});process.exit(0);}
function advance(){if(cursor<queue.length){const tool=queue[cursor++];send({type:'tool_request',id:'queued-'+cursor,...tool});}else finish();}
readline.createInterface({input:process.stdin}).on('line',line=>{
 const msg=JSON.parse(line); if(msg.type==='cancel') process.exit(0);
 if(msg.type==='tool_response'&&pendingResult){advance();return;}
 if(msg.type!=='run') return;
 const phase=msg.phase.name, input=msg.phase.input;
 const settings=JSON.parse(fs.readFileSync(home+'/behavior.json','utf8'));
 fs.appendFileSync(home+'/calls.jsonl',JSON.stringify({phase,pid:process.pid,run_id:msg.run_id,has_source:!!input.source_sections,max_tokens:msg.model.max_tokens,limits:msg.limits})+'\n');
 if(settings.fail===phase) {process.exit(7);return;}
 if(settings.wait===phase) {setInterval(()=>{},1000);return;}
 if(settings.forbidden===phase) {send({type:'tool_request',id:'bad',name:'write_artifact',args:{path:'host-workflow/compose.json',content:'forged'}});return;}
 const evidence=()=>{const section=input.source_sections.find(s=>s.text.includes('The method improves accuracy'));return {id:'E1',source_id:'main',section_id:section.id,quote:'The method improves accuracy from 70 percent to 75 percent.',...(section.page?{page:section.page}:{})};};
 let output;
 if(phase==='extract') output={version:1,paper_title:'Fixture paper',research_question:'Does the method improve accuracy?',claims:[{id:'C1',kind:'result',statement:'Accuracy increases from 70 percent to 75 percent.',basis:'observation',conditions:{model:'',dataset:'',split:'',metric:'accuracy',baseline:'',budget:''},evidence:[evidence()],numeric_comparison:{value:75,baseline_value:70,relation:'higher',unit:'percent'}}],open_questions:settings.external?[{question:'Does another paper support this comparison?',search_query:'fixture explicit comparison',reason:'Needs independent evidence'}]:[],limitations:['Model and dataset are not specified in this fixture.']};
 if(phase==='fact_review') output={passed:!settings.fact_issue,issues:settings.fact_issue?[{claim_id:'C1',message:'Unresolved comparison'}]:[],checked_claim_ids:settings.incomplete?[]:input.packet.claims.map(c=>c.id),suggestions:[]};
 if(phase==='compose') output={brief_markdown:'准确率由70%提高到75%。[E1](evidence:E1)',full_markdown:'实验观察到准确率由70%提高到75%；模型和数据集尚未明确。[E1](evidence:E1)'};
 if(phase==='product_review') output={passed:!settings.product_issue,issues:settings.product_issue?['Needs clearer limitations']:[],checks:{coverage:!settings.product_issue,template:true,numeric_conditions:true,inference_labels:true}};
 if(phase==='packet_repair') output={claims:input.packet.claims.map(c=>({...c,statement:c.statement+' Model and dataset were not specified.'})),remove_claim_ids:[]};
 if(phase==='product_repair') output={edits:[{field:'brief_markdown',old_text:input.product.brief_markdown,new_text:input.product.brief_markdown+' 尚需明确实验条件。'}]};
 if(phase==='revision_plan') output={scope:settings.revision_scope||'presentation',reason:'The fixture requests presentation changes only.'};
 if(settings.bad_product&&phase==='compose') output.full_markdown+=' [E999]';
 pendingResult=output;
 if(phase==='extract'&&settings.spend) queue=Array.from({length:settings.spend},()=>({name:'read_source',args:{source_id:'main'}}));
 if(phase==='fact_review'&&settings.external_execute) queue=[{name:'search_papers',args:{query:'fixture explicit comparison'}},{name:'fetch_related',args:{id:'arxiv:1810.04805'}}];
 // Each accounting request precedes local validation; only the final attempt
 // produces the valid phase result, as in the production runner protocol.
 if(settings.submission_rpc) {
   const attempts=phase==='extract'?(settings.invalid_submissions||0)+1:1;
   queue.push(...Array.from({length:attempts},()=>({name:'host_phase_submission',args:{}})));
 }
 advance();
});
'''


def guided_fixture(root, **behavior):
    job = protocol_fixture(root)
    script_dir = get_settings().project_dir / "agent-runner/dist"
    (script_dir / "index.js").write_text(SCRIPT)
    (script_dir / "behavior.json").write_text(json.dumps(behavior))
    with SessionLocal() as db, db.begin():
        stored = db.get(Job, job.id)
        stored.input_snapshot = {**stored.input_snapshot, "workflow_mode": "guided"}
        db.get(Reading, job.reading_id).workflow_mode = "guided"
    return job, script_dir


def calls(script_dir):
    path = script_dir / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def snapshot(job_id):
    with SessionLocal() as db:
        row = db.get(Job, job_id)
        db.expunge(row)
        return row


def retry_job(job_id):
    with SessionLocal() as db, db.begin():
        old = db.get(Job, job_id)
        job = Job(id=new_id(), workspace_id=old.workspace_id, reading_id=old.reading_id, created_by=old.created_by,
                  kind=old.kind, status="queued", stage="queued", progress=0,
                  input_snapshot={**old.input_snapshot, "retry_of_job_id": old.id})
        db.add(job); db.flush()
        return job.id


def revision_job(job_id, *, instruction="Make the summary shorter"):
    with SessionLocal() as db, db.begin():
        old = db.get(Job, job_id)
        reading = db.get(Reading, old.reading_id)
        job = Job(id=new_id(), workspace_id=old.workspace_id, reading_id=old.reading_id, created_by=old.created_by,
                  kind="revise", status="queued", stage="queued", progress=0,
                  input_snapshot={**old.input_snapshot, "instruction": instruction, "base_revision_id": reading.current_revision_id})
        db.add(job); db.flush()
        return job.id


def test_guided_real_jsonl_fresh_phases_and_no_native_claim(worker_db):
    job, fixture = guided_fixture(worker_db)
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "succeeded", row.error
    launched = calls(fixture)
    assert [entry["phase"] for entry in launched] == ["extract", "fact_review", "compose", "product_review"]
    assert len({entry["pid"] for entry in launched}) == 4
    assert [entry["max_tokens"] for entry in launched] == [6000, 4000, 5000, 3000]
    assert [entry["has_source"] for entry in launched] == [True, True, False, False]
    assert all(entry["limits"]["timeout_seconds"] <= 300 for entry in launched)
    assert row.usage["input_tokens"] == 44 and row.usage["tool_calls"] == 4
    assert all(stage["status"] == "succeeded" for stage in row.stage_records)
    with SessionLocal() as db:
        revision = db.scalar(select(Revision).where(Revision.reading_id == job.reading_id))
        assert revision.reading_packet["claims"][0]["id"] == "C1"
        assert revision.verification["workflow"]["native_status"] == "not_requested"
        assert revision.verification["cross_paper_status"] == "not_requested"
        assert revision.verification["fact_review"]["passed"] is True
        assert revision.evidence_index[0]["verified"] is True
    assert not (worker_db / "tmp" / job.id).exists()


def test_failed_phase_retry_reuses_only_finished_hash_anchored_stages(worker_db):
    job, fixture = guided_fixture(worker_db, fail="fact_review")
    Worker().run(once=True)
    failed = snapshot(job.id)
    assert failed.status == "failed", failed.error
    assert next(s for s in failed.stage_records if s["name"] == "extract")["status"] == "succeeded"
    assert next(s for s in failed.stage_records if s["name"] == "fact_review")["status"] == "failed"
    (fixture / "behavior.json").write_text("{}")
    retry_id = retry_job(job.id)
    Worker().run(once=True)
    row = snapshot(retry_id)
    assert row.status == "succeeded", row.error
    assert sum(c["phase"] == "extract" for c in calls(fixture)) == 1
    assert next(s for s in row.stage_records if s["name"] == "extract")["status"] == "reused"
    assert next(s for s in row.stage_records if s["name"] == "fact_review")["status"] == "succeeded"


def test_tampered_checkpoint_reextracts_instead_of_reusing_fake_content(worker_db):
    job, fixture = guided_fixture(worker_db, fail="fact_review")
    Worker().run(once=True)
    path = worker_db / "tmp" / job.id / "1/host-workflow/extract.json"
    saved = json.loads(path.read_text())
    saved["output"]["claims"][0]["statement"] = "Forged accuracy is 100 percent"
    path.write_text(json.dumps(saved))
    (fixture / "behavior.json").write_text("{}")
    retry_id = retry_job(job.id)
    Worker().run(once=True)
    row = snapshot(retry_id)
    assert row.status == "succeeded", row.error
    assert sum(c["phase"] == "extract" for c in calls(fixture)) == 2
    with SessionLocal() as db:
        revision = db.scalar(select(Revision).where(Revision.reading_id == job.reading_id))
        assert "Forged" not in json.dumps(revision.reading_packet)


def test_presentation_revision_reuses_verified_packet_and_fact_review(worker_db):
    job, fixture = guided_fixture(worker_db)
    Worker().run(once=True)
    assert snapshot(job.id).status == "succeeded"
    revision_id = revision_job(job.id)
    Worker().run(once=True)
    row = snapshot(revision_id)
    assert row.status == "succeeded", row.error
    phases = [call["phase"] for call in calls(fixture)]
    assert phases.count("revision_plan") == 1
    assert phases.count("extract") == 1 and phases.count("fact_review") == 1
    assert phases.count("compose") == 2 and phases.count("product_review") == 2
    assert next(s for s in row.stage_records if s["name"] == "fact_review")["status"] == "reused"
    with SessionLocal() as db:
        revisions = db.scalars(select(Revision).where(Revision.reading_id == job.reading_id).order_by(Revision.number)).all()
        assert len(revisions) == 2 and revisions[1].parent_revision_id == revisions[0].id
        assert revisions[0].reading_packet == revisions[1].reading_packet


@pytest.mark.parametrize("behavior,expected", [({"incomplete": True}, "cover every"), ({"fact_issue": True}, "fact review still"),
                                               ({"product_issue": True}, "product review still"), ({"forbidden": "extract"}, "not permitted")])
def test_host_rejects_incomplete_failed_reviews_and_forbidden_tools(worker_db, behavior, expected):
    job, fixture = guided_fixture(worker_db, **behavior)
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "failed" and expected in row.error, row.error
    with SessionLocal() as db:
        assert db.scalar(select(Revision).where(Revision.reading_id == job.reading_id)) is None
    phases = [entry["phase"] for entry in calls(fixture)]
    assert phases.count("packet_repair") <= 1 and phases.count("product_repair") <= 1


def test_model_pass_cannot_attest_unperformed_requested_related_research(worker_db):
    job, _ = guided_fixture(worker_db, external=True)
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "partial", row.error
    with SessionLocal() as db:
        revision = db.scalar(select(Revision).where(Revision.reading_id == job.reading_id))
        assert revision.verification["cross_paper_status"] == "partial"
        assert revision.verification["server_quote_check"] == "passed"
        assert any("not searched" in issue for issue in revision.verification["errors"])


def test_final_citation_gate_stays_independent_of_model_review(worker_db):
    job, _ = guided_fixture(worker_db, bad_product=True)
    Worker().run(once=True)
    assert snapshot(job.id).status == "partial"
    with SessionLocal() as db:
        revision = db.scalar(select(Revision).where(Revision.reading_id == job.reading_id))
        assert any("E999" in issue for issue in revision.verification["errors"])


def test_cancel_real_phase_terminates_without_publication(worker_db):
    job, fixture = guided_fixture(worker_db, wait="fact_review")
    worker = Worker()
    thread = threading.Thread(target=worker.run, kwargs={"once": True})
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(c["phase"] == "fact_review" for c in calls(fixture)):
        time.sleep(0.05)
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).cancel_requested = True
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert snapshot(job.id).status == "cancelled"
    with SessionLocal() as db:
        assert db.scalar(select(Revision).where(Revision.reading_id == job.reading_id)) is None


def test_product_patch_is_frozen_atomic_and_rejects_overlap():
    product = {"brief_markdown": "ABC and DEF", "full_markdown": "A repeated repeated word"}
    patch = {"edits": [{"field": "brief_markdown", "old_text": "ABC", "new_text": "DEF"},
                       {"field": "brief_markdown", "old_text": "DEF", "new_text": "XYZ"}]}
    assert apply_product_patch(product, patch)["brief_markdown"] == "DEF and XYZ"
    assert product["brief_markdown"] == "ABC and DEF"
    with pytest.raises(PipelineError, match="exactly once"):
        apply_product_patch(product, {"edits": [{"field": "full_markdown", "old_text": "repeated", "new_text": "new"}]})
    with pytest.raises(PipelineError, match="exactly once"):
        apply_product_patch({"brief_markdown": "aaa", "full_markdown": "Other"},
                            {"edits": [{"field": "brief_markdown", "old_text": "aa", "new_text": "x"}]})
    with pytest.raises(PipelineError, match="overlapping"):
        apply_product_patch(product, {"edits": [{"field": "brief_markdown", "old_text": "ABC", "new_text": "x"},
                                               {"field": "brief_markdown", "old_text": "ABC and", "new_text": "y"}]})


def test_review_schema_rejects_pass_without_actual_checks():
    with pytest.raises(PipelineError):
        validate_phase("product_review", {"passed": True, "issues": [], "checks": {}})
    with pytest.raises(PipelineError):
        validate_phase("product_review", {"passed": True, "issues": [], "checks": {
            "coverage": False, "template": True, "numeric_conditions": True, "inference_labels": True}})


def test_expired_worker_recovers_finished_stages_without_stale_writes(worker_db):
    job, fixture = guided_fixture(worker_db, wait="fact_review")
    worker = Worker()
    thread = threading.Thread(target=worker.run, kwargs={"once": True})
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(c["phase"] == "fact_review" for c in calls(fixture)):
        time.sleep(0.05)
    old_token = snapshot(job.id).lease_token
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).lease_expires_at = now() - timedelta(seconds=1)
    thread.join(timeout=8)
    assert not thread.is_alive()
    (fixture / "behavior.json").write_text("{}")
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "succeeded" and row.attempt == 2, row.error
    # Limits are per automatic attempt; lifetime provider usage remains summed.
    assert set(row.usage["attempts"]) == {"1", "2"}
    assert row.usage["input_tokens"] == sum(item.get("input_tokens", 0) for item in row.usage["attempts"].values())
    assert sum(c["phase"] == "extract" for c in calls(fixture)) == 1
    assert any(s["name"] == "extract" and s["attempt"] == 2 and s["status"] == "reused" for s in row.stage_records)
    with pytest.raises(LeaseLost):
        record_workflow_stage(job.id, old_token, None, "compose", "running", input_hash="forged")


@pytest.mark.parametrize("spend,limit,message", [(9, 10, "total tool budget"), (40, 160, "Per-phase")])
def test_guided_total_and_per_phase_tool_budgets(worker_db, spend, limit, message):
    job, _ = guided_fixture(worker_db, spend=spend)
    get_settings().max_tool_calls = limit
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "failed" and message in row.error, row.error
    assert row.usage["tool_calls"] <= limit


def test_source_byte_tamper_is_reparsed_from_original_upload(worker_db):
    job, fixture = guided_fixture(worker_db, fail="fact_review")
    Worker().run(once=True)
    work_dir = worker_db / "tmp" / job.id / "1"
    saved = json.loads((work_dir / "host-workflow/source.json").read_text())
    main = next(d for d in saved["output"]["documents"] if d["id"] == "main")
    (work_dir / main["path"]).write_bytes(b"untrusted replacement")
    (fixture / "behavior.json").write_text("{}")
    retry_id = retry_job(job.id)
    Worker().run(once=True)
    row = snapshot(retry_id)
    assert row.status == "succeeded", row.error
    source = next(s for s in row.stage_records if s["name"] == "source")
    assert source["status"] == "succeeded", "changed bytes must not be reused"


def test_retry_checkpoint_source_cannot_cross_workspace(worker_db):
    first, _ = guided_fixture(worker_db, fail="fact_review")
    Worker().run(once=True)
    from test_worker_durability import seed_job
    foreign, _ = seed_job()
    with SessionLocal() as db, db.begin():
        old = db.get(Job, foreign.id)
        old.status = "failed"
        row = db.get(Job, first.id)
        row.status = "queued"
        row.attempt = 0
        row.input_snapshot = {**row.input_snapshot, "retry_of_job_id": foreign.id}
    Worker().run(once=True)
    row = snapshot(first.id)
    assert row.status == "failed" and "reading/workspace" in row.error


def test_presentation_revision_restores_retained_related_sources(worker_db, monkeypatch):
    from app.source_tools import SourceLibrary, SourceDocument
    def search(library, query):
        library.query_log.append(query); library.searches += 1
        library.candidates["arxiv:1810.04805"] = {"id": "arxiv:1810.04805", "title": "Related fixture"}
        library.search_records[query] = {"completed": True, "candidate_ids": ["arxiv:1810.04805"], "errors": []}
        library._save()
        return {"candidates": [{"id": "arxiv:1810.04805", "title": "Related fixture"}]}
    def fetch(library, id, **_):
        text = "The independent fixture corroborates the stated comparison under matching settings."
        path = library.work_dir / "related-fixture.txt"
        path.write_text(text)
        source_id = "related:" + id
        library.documents[source_id] = SourceDocument(source_id, "Related fixture", "https://example.org/fixture", path.name,
                [{"id": "related-s1", "text": text}], {"full_text": True}, hashlib.sha256(path.read_bytes()).hexdigest(), kind="external")
        library._save()
        return {"id": source_id, "sections": library.documents[source_id].sections}
    monkeypatch.setattr(SourceLibrary, "search_papers", search)
    monkeypatch.setattr(SourceLibrary, "fetch_related", fetch)
    job, fixture = guided_fixture(worker_db, external=True, external_execute=True)
    Worker().run(once=True)
    assert snapshot(job.id).status == "succeeded", snapshot(job.id).error
    revised = revision_job(job.id)
    (fixture / "behavior.json").write_text("{}")
    Worker().run(once=True)
    row = snapshot(revised)
    assert row.status == "succeeded", row.error
    assert sum(c["phase"] == "fact_review" for c in calls(fixture)) == 1
    with SessionLocal() as db:
        revision = db.scalar(select(Revision).where(Revision.reading_id == job.reading_id).order_by(Revision.number.desc()))
        assert revision.verification["cross_paper_status"] == "completed"
        assert any(source["source_id"] == "related:arxiv:1810.04805" for source in revision.provenance["source_manifest"])


def test_failed_second_requested_search_cannot_borrow_first_query_source():
    from types import SimpleNamespace
    from app.workflow import GuidedWorkflow
    workflow = object.__new__(GuidedWorkflow)
    workflow.cross_requested = True
    workflow.packet = {"open_questions": [{"question": "First comparison", "search_query": "first"},
                                           {"question": "Second comparison", "search_query": "second"}], "claims": []}
    source = SimpleNamespace(kind="external", coverage={"full_text": True})
    library = SimpleNamespace(query_log=["first", "second"], documents={"related:paper-1": source},
                 search_records={"first": {"completed": True, "candidate_ids": ["paper-1"], "errors": []},
                                 "second": {"completed": False, "candidate_ids": [], "errors": ["provider unavailable"]}})
    workflow.pipeline = SimpleNamespace(library=library)
    errors = workflow.cross_paper_errors()
    assert any("incomplete: Second comparison" in item for item in errors)
    assert any("no matched full-text source: Second comparison" in item for item in errors)
    assert not any("First comparison" in item for item in errors)


def test_main_attributed_related_work_does_not_require_external_search():
    from types import SimpleNamespace
    from app.workflow import GuidedWorkflow
    workflow = object.__new__(GuidedWorkflow)
    workflow.cross_requested = False
    workflow.packet = {"open_questions": [], "claims": [{"kind": "related_work", "evidence": [{"source_id": "main"}]}]}
    workflow.pipeline = SimpleNamespace(library=SimpleNamespace(query_log=[], documents={}))
    assert workflow.cross_paper_errors() == []


def test_final_submission_exactly_at_attempt_tool_limit_succeeds(worker_db):
    job, _ = guided_fixture(worker_db, spend=6)
    get_settings().max_tool_calls = 10
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "succeeded", row.error
    assert row.usage["tool_calls"] == 10


def test_queued_retry_retains_old_parent_checkpoint(worker_db):
    job, _ = guided_fixture(worker_db, fail="fact_review")
    Worker().run(once=True)
    with SessionLocal() as db, db.begin():
        db.get(Job, job.id).updated_at = now() - timedelta(days=8)
    checkpoint = worker_db / "tmp" / job.id / "1/host-workflow/extract.json"
    assert checkpoint.is_file()
    retry = retry_job(job.id)
    Worker().cleanup_terminal_artifacts()
    assert checkpoint.is_file()
    with SessionLocal() as db, db.begin():
        db.get(Job, retry).status = "cancelled"
    Worker().cleanup_terminal_artifacts()
    assert not checkpoint.exists()


def test_executed_invalid_submissions_spend_shared_budget_before_next_phase(worker_db):
    job, fixture = guided_fixture(worker_db, spend=6, submission_rpc=True, invalid_submissions=2)
    get_settings().max_tool_calls = 10
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "failed" and "total tool budget" in row.error, row.error
    assert row.usage["tool_calls"] == 10
    assert [entry["phase"] for entry in calls(fixture)] == ["extract", "fact_review"]
    stages = {entry["name"]: entry for entry in row.stage_records}
    assert stages["extract"]["usage"]["tool_calls"] == 9  # six reads, two invalid submits, one valid
    assert stages["fact_review"]["usage"]["tool_calls"] == 1
    assert stages["compose"]["status"] == "failed"


def test_submission_rpc_final_result_is_not_charged_twice(worker_db):
    job, _ = guided_fixture(worker_db, spend=4, submission_rpc=True, invalid_submissions=2)
    get_settings().max_tool_calls = 10
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "succeeded", row.error
    assert row.usage["tool_calls"] == 10  # four reads + three extract submits + three later submits


def test_submission_rpc_per_phase_exhaustion_is_fatal(worker_db):
    job, fixture = guided_fixture(worker_db, submission_rpc=True, invalid_submissions=40)
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "failed" and "Per-phase" in row.error, row.error
    assert row.usage["tool_calls"] == 32
    assert [entry["phase"] for entry in calls(fixture)] == ["extract"]


def test_submission_accounting_rpc_is_not_available_to_native_runner(worker_db):
    job = protocol_fixture(worker_db)
    script = r"""
const readline=require('readline');readline.createInterface({input:process.stdin}).on('line',line=>{
 const event=JSON.parse(line);if(event.type==='run')process.stdout.write(JSON.stringify({type:'tool_request',id:'reserved',name:'host_phase_submission',args:{}})+'\n');
});
"""
    (get_settings().project_dir / "agent-runner/dist/index.js").write_text(script)
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "failed" and "requires a guided phase" in row.error, row.error
