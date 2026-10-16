"""Evidence recovery against original text and real workflow stage validators."""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading
import time

import pytest

from app.evidence import validate_packet
from app.evidence_repair import MAX_CONTEXT_BYTES, repair_context
from app.long_workflow import StructuredWorkflow
from app.pipeline import BudgetExhausted, PipelineError, TaskCancelled
from app.workflow import GuidedWorkflow, digest


ORIGINAL = "The method improves accuracy from 70 percent to 75 percent."


def packet_fixture():
    return {"version": 1, "paper_title": "Fixture paper", "research_question": "Does accuracy improve?",
            "claims": [{"id": "C1", "kind": "result", "statement": ORIGINAL, "basis": "observation",
                        "conditions": {key: "" for key in ("model", "dataset", "split", "metric", "baseline", "budget")},
                        "evidence": [{"id": "E1", "source_id": "main", "section_id": "p2", "page": 2, "quote": ORIGINAL}],
                        "numeric_comparison": {"value": 75, "baseline_value": 70, "relation": "higher", "unit": "percent"}}],
            "open_questions": [], "limitations": []}


def host(tmp_path, workflow_type, proposals):
    workflow = workflow_type.__new__(workflow_type)
    workflow.root = tmp_path
    workflow.common = {}
    workflow.source_fingerprint = "fixture"
    workflow.records, workflow.outputs, workflow.completed = [], {}, {}
    workflow.spec = {"model": {"max_tokens": 8192, "context_window": 128000}}
    workflow.deadline = time.monotonic() + 60
    workflow.job = SimpleNamespace(id="fixture")
    workflow.state_lock, workflow.record_lock = threading.RLock(), threading.RLock()
    workflow.pipeline = SimpleNamespace(
        tool_calls=0, max_tool_calls=100, _cancel_check=lambda: None, phase_local=threading.local(),
        library=SimpleNamespace(documents={"main": SimpleNamespace(
            title="Fixture", url=None, kind="paper", coverage={}, sections=[
                {"id": "p1", "page": 1, "text": "An unrelated introductory passage about applications."},
                {"id": "p2", "page": 2, "text": ORIGINAL}])}))
    stages, inputs = [], []

    def save(name, status, **kwargs):
        record = {"name": name, "status": status, **kwargs}
        stages.append(record)
        return record

    def run(phase, input, timeout, tool_cap, output_cap):
        inputs.append(deepcopy(input))
        workflow.pipeline.tool_calls += 1
        proposal = proposals[len(inputs) - 1]
        return proposal(input) if callable(proposal) else deepcopy(proposal)

    workflow.save_stage, workflow.run_phase = save, run
    return workflow, stages, inputs


def replace_claim(packet):
    return {"claims": [deepcopy(packet["claims"][0])], "remove_claim_ids": []}


@pytest.mark.parametrize("workflow_type", [GuidedWorkflow, StructuredWorkflow])
def test_noop_then_source_correction_continues_with_failed_attempt_visible(tmp_path, workflow_type):
    original = packet_fixture()
    bad = deepcopy(original)
    bad["claims"][0]["evidence"][0]["quote"] = ORIGINAL.replace("75", "95")
    host_workflow, stages, inputs = host(tmp_path, workflow_type,
        [{"claims": [], "remove_claim_ids": []}, replace_claim(original)])
    result = host_workflow.repair_packet(bad, "packet_repair")
    assert result == original
    assert len(inputs) == 2
    assert [(r["name"], r["status"]) for r in stages if r["status"] != "running"] == [
        ("packet_repair", "failed"), ("packet_repair_2", "succeeded")]
    context = inputs[1]["repair_context"]
    assert context["round"] == 2 and context["unchanged_errors"]
    assert "actual change" in context["previous_attempt"]["error"]
    assert any(ORIGINAL in s["text"] for s in inputs[1]["source_sections"])
    assert bad["claims"][0]["evidence"][0]["quote"].endswith("95 percent.")


@pytest.mark.parametrize("workflow_type", [GuidedWorkflow, StructuredWorkflow])
def test_wrong_page_relocated_using_unverified_original_candidate(tmp_path, workflow_type):
    original = packet_fixture()
    bad = deepcopy(original)
    bad["claims"][0]["evidence"][0].update(section_id="p1", page=1)
    workflow, _, inputs = host(tmp_path, workflow_type, [replace_claim(original)])
    result = workflow.repair_packet(bad, "segment_1_repair")
    suggestions = inputs[0]["repair_context"]["diagnostics"][0]["suggested_locations"]
    assert suggestions[0]["section_id"] == "p2" and suggestions[0]["page"] == 2
    assert "verified" not in suggestions[0]
    assert not validate_packet(result, workflow.pipeline.library)[1]


@pytest.mark.parametrize("workflow_type", [GuidedWorkflow, StructuredWorkflow])
def test_three_semantic_failures_exhaust_without_success_or_nested_retries(tmp_path, workflow_type):
    bad = packet_fixture()
    bad["claims"][0]["numeric_comparison"]["relation"] = "lower"

    def superficial(input):
        patch = replace_claim(input["packet"])
        patch["claims"][0]["statement"] += " Still uncorrected."
        return patch

    workflow, stages, inputs = host(tmp_path, workflow_type, [superficial] * 3)
    with pytest.raises(PipelineError, match="after 3 adaptive repair attempts.*numeric comparison"):
        workflow.repair_packet(bad, "packet_repair")
    assert len(inputs) == 3
    assert all(stage["status"] != "succeeded" for stage in stages)
    assert [stage["name"] for stage in stages if stage["status"] == "failed"] == [
        "packet_repair", "packet_repair_2", "packet_repair_3"]


@pytest.mark.parametrize("workflow_type", [GuidedWorkflow, StructuredWorkflow])
@pytest.mark.parametrize("abort", [TaskCancelled, BudgetExhausted])
def test_recovery_obeys_cancellation_and_shared_budget(tmp_path, workflow_type, abort):
    bad = packet_fixture()
    bad["claims"][0]["numeric_comparison"]["relation"] = "lower"
    workflow, _, inputs = host(tmp_path, workflow_type, [])

    def stop(*args):
        raise abort("stop the existing task")

    workflow.run_phase = stop
    with pytest.raises(abort, match="stop the existing task"):
        workflow.repair_packet(bad, "packet_repair")
    assert not inputs


def test_valid_packet_never_calls_repair_model(tmp_path):
    workflow, stages, inputs = host(tmp_path, GuidedWorkflow, [])
    assert workflow.repair_packet(packet_fixture(), "packet_repair") == packet_fixture()
    assert stages == inputs == []


def test_repair_preserves_unaffected_claims(tmp_path):
    bad = packet_fixture()
    second = deepcopy(bad["claims"][0])
    second["id"], second["evidence"][0]["id"] = "C2", "E2"
    bad["claims"].append(second)
    bad["claims"][0]["evidence"][0]["quote"] = ORIGINAL.replace("75", "95")
    original = packet_fixture()
    illegal = {"claims": [deepcopy(second)], "remove_claim_ids": []}
    illegal["claims"][0]["statement"] = "An unrelated rewrite."
    workflow, _, inputs = host(tmp_path, GuidedWorkflow, [illegal, replace_claim(original)])
    result = workflow.repair_packet(bad, "packet_repair")
    assert "unaffected claims" in inputs[1]["repair_context"]["previous_attempt"]["error"]
    assert result["claims"][1] == second


def test_original_windows_are_bounded_and_offsets_are_exact(tmp_path):
    workflow, _, _ = host(tmp_path, GuidedWorkflow, [])
    sections = workflow.pipeline.library.documents["main"].sections
    sections[1]["text"] = "很长的导言。" * 10000 + ORIGINAL + "很长的结尾。" * 10000
    packet = packet_fixture()
    for number in range(2, 12):
        claim = deepcopy(packet["claims"][0])
        claim["id"], claim["evidence"][0]["id"] = f"C{number}", f"E{number}"
        packet["claims"].append(claim)
    context, windows = repair_context(packet, workflow.pipeline.library, ["Structural issue"], round_number=1)
    assert len(context["diagnostics"]) <= 8
    assert sum(len(json.dumps(w, ensure_ascii=False).encode()) for w in windows) <= MAX_CONTEXT_BYTES
    for window in windows:
        source = next(s for s in sections if s["id"] == window["id"])
        assert window["text"] == source["text"][window["source_offset"]:window["source_end"]]
    assert any(ORIGINAL in w["text"] for w in windows)


def test_invalid_fact_patch_gets_evidence_recovery_before_independent_rereview(tmp_path):
    original = packet_fixture()
    bad_patch = replace_claim(original)
    bad_patch["claims"][0]["evidence"][0]["page"] = 99
    corrected = replace_claim(original)
    corrected["claims"][0]["statement"] += " The dataset was not specified."
    workflow, stages, inputs = host(tmp_path, GuidedWorkflow, [bad_patch, corrected])
    result = workflow.repair_packet(original, "fact_repair", review={
        "passed": False, "issues": [{"claim_id": "C1", "message": "Clarify experiment conditions"}]})
    assert len(inputs) == 2 and inputs[1]["errors"]
    assert result["claims"][0]["evidence"][0]["page"] == 2
    assert stages[1]["status"] == "failed"


def test_review_noop_is_failed_before_next_targeted_proposal(tmp_path):
    original = packet_fixture()
    correction = replace_claim(original)
    correction["claims"][0]["statement"] += " The dataset was not specified."
    workflow, stages, inputs = host(tmp_path, GuidedWorkflow,
        [{"claims": [], "remove_claim_ids": []}, correction])
    report = {"passed": False, "issues": [{"claim_id": "C1", "message": "State missing dataset conditions"}]}
    result = workflow.repair_packet(original, "fact_repair", review=report)
    assert stages[1]["status"] == "failed"
    assert inputs[1]["review"] == report
    assert "actual change" in inputs[1]["repair_context"]["previous_attempt"]["error"]
    assert result["claims"][0]["statement"].endswith("The dataset was not specified.")


@pytest.mark.parametrize("workflow_type", [GuidedWorkflow, StructuredWorkflow])
def test_cached_structural_only_repair_is_revalidated_before_reuse(tmp_path, workflow_type):
    original = packet_fixture()
    bad = deepcopy(original)
    bad["claims"][0]["evidence"][0]["quote"] = ORIGINAL.replace("75", "95")
    workflow, stages, inputs = host(tmp_path, workflow_type, [replace_claim(original)])
    _, errors = validate_packet(bad, workflow.pipeline.library)
    context, sections = repair_context(bad, workflow.pipeline.library, errors, round_number=1)
    repair_input = {"packet": bad, "errors": errors, "source_sections": sections, "repair_context": context}
    input_hash = digest({"common": workflow.common, "source": workflow.source_fingerprint,
                         "name": "packet_repair", "input": repair_input})
    cached_patch = replace_claim(bad)
    cached_patch["claims"][0]["statement"] += " A structurally valid but uncorrected patch."
    source = {"version": "fixture", "documents": []}
    saved = {"input_hash": input_hash, "output_hash": digest(cached_patch), "output": cached_patch,
             "source_hash": digest(source), "source_checkpoint": source}
    (tmp_path / "packet_repair.json").write_text(json.dumps(saved))
    workflow.records.append({"name": "packet_repair", "status": "succeeded", "input_hash": input_hash,
                             "output_hash": digest(cached_patch), "source_hash": digest(source)})
    workflow.pipeline.library.restore_checkpoint = lambda *args, **kwargs: None
    workflow.pipeline.library.export_checkpoint = lambda: source
    result = workflow.repair_packet(bad, "packet_repair")
    assert result == original and len(inputs) == 1
    assert not any(stage["status"] == "reused" for stage in stages)


@pytest.mark.parametrize("claims", [None, [None], [{"id": [], "evidence": [{}]}]])
def test_malformed_packet_fails_honestly_without_diagnostic_attribute_error(tmp_path, claims):
    malformed = packet_fixture()
    malformed["claims"] = claims
    workflow, _, inputs = host(tmp_path, GuidedWorkflow, [{"claims": [], "remove_claim_ids": []}] * 3)
    with pytest.raises(PipelineError, match="after 3 adaptive repair attempts"):
        workflow.repair_packet(malformed, "packet_repair")
    assert len(inputs) == 3


def test_shared_tool_budget_stops_before_second_repair_call(tmp_path):
    bad = packet_fixture()
    bad["claims"][0]["numeric_comparison"]["relation"] = "lower"
    workflow, _, inputs = host(tmp_path, GuidedWorkflow, [{"claims": [], "remove_claim_ids": []}])
    workflow.pipeline.max_tool_calls = 1
    with pytest.raises(BudgetExhausted, match="total tool budget"):
        workflow.repair_packet(bad, "packet_repair")
    assert len(inputs) == 1


# These fixtures launch a labelled test-only JSONL child process through Worker;
# no provider output is mocked as a production reading result.
from test_worker_durability import worker_db  # noqa: E402,F401


@pytest.mark.parametrize("structured", [False, True])
def test_real_worker_recovers_noop_then_exact_original_and_publishes(worker_db, structured):
    from app.db import SessionLocal
    from app.models import Revision
    from app.worker import Worker
    from test_workflow import guided_fixture, snapshot, calls
    from test_long_workflow import fixture as structured_fixture

    job, script_dir = (structured_fixture if structured else guided_fixture)(worker_db)
    script_path = script_dir / "index.js"
    injected = r'''
 if(phase==='extract'||phase==='chunk_extract') {
   const packet=output.packet||output;
   packet.claims[0].evidence[0].quote=packet.claims[0].evidence[0].quote.replace('75','95');
 }
 if(phase==='packet_repair') {
   fs.appendFileSync(home+'/repair-inputs.jsonl',JSON.stringify(input)+'\n');
   if(input.repair_context.round===1) output={claims:[],remove_claim_ids:[]};
   else {
     const claim=JSON.parse(JSON.stringify(input.packet.claims[0]));
     const source=input.source_sections.find(s=>s.text.includes('The method improves accuracy from 70 percent to 75 percent.'));
     claim.evidence[0]={...claim.evidence[0],source_id:source.source_id,section_id:source.id,
       quote:'The method improves accuracy from 70 percent to 75 percent.',...(source.page?{page:source.page}:{})};
     output={claims:[claim],remove_claim_ids:[]};
   }
 }
'''
    script_path.write_text(script_path.read_text().replace("pendingResult=output;", injected + "\npendingResult=output;"))
    Worker().run(once=True)
    row = snapshot(job.id)
    assert row.status == "succeeded", row.error
    repairs = [c for c in calls(script_dir) if c["phase"] == "packet_repair"]
    assert len(repairs) == 2
    name = "segment_1_repair" if structured else "packet_repair"
    assert any(s["name"] == name and s["status"] == "failed" for s in row.stage_records)
    assert any(s["name"] == name + "_2" and s["status"] == "succeeded" for s in row.stage_records)
    retained_inputs = [json.loads(line) for line in (script_dir / "repair-inputs.jsonl").read_text().splitlines()]
    assert retained_inputs[1]["repair_context"]["previous_attempt"]["changed_packet"] is False
    with SessionLocal() as db:
        revision = db.query(Revision).filter_by(reading_id=job.reading_id).one()
        assert revision.reading_packet["claims"][0]["evidence"][0]["quote"] == ORIGINAL
        assert revision.evidence_index[0]["verified"] is True
