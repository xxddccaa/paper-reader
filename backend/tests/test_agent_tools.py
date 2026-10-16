"""Trusted agent tool gates with real source/evidence and no model/network calls."""
import base64
import copy
import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4

import fitz
import pytest

from app.agent_tools import AgentTools
from app.pipeline import Pipeline, PipelineError, TaskCancelled
from app.source_tools import SourceDocument


TEXT = "The measured accuracy is 75% compared with the baseline accuracy of 60% on the validation split."
APPENDIX = "Appendix A states that all experiments used an identical hardware and evaluation budget."


class MemoryStore:
    """Faithful atomic/idempotent store contract; persistence is tested separately."""
    def __init__(self):
        self.state, self.effects, self.attempts = {}, {}, {}

    def load(self):
        return {"state": copy.deepcopy(self.state), "remaining_seconds": 3600, "remaining_tool_calls": 500 - len(self.attempts)}

    def consume(self, call_id, name):
        if call_id in self.attempts and self.attempts[call_id] != name:
            raise ValueError("Tool call ID reused")
        self.attempts[call_id] = name
        return {"reserved": True}

    def mutate(self, event_type, data, change, request_id=None):
        if request_id and request_id in self.effects:
            return copy.deepcopy(self.effects[request_id])
        state = copy.deepcopy(self.state)
        result = change(state)
        json.dumps({"state": state, "result": result}, allow_nan=False)
        self.state = state
        if request_id:
            self.effects[request_id] = copy.deepcopy(result)
        return copy.deepcopy(result)


def fixture(tmp_path, *, chat=False, visual=False, table=False):
    project = Path(__file__).resolve().parents[2]
    work = tmp_path / "work"
    work.mkdir()
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Explain the research question, mechanism, evidence and limitations.")
    pipeline = Pipeline(work, skill, {"id": "paper", "title": "Measured method"}, "ljg-paper", "workspace",
                        threading.Event(), project, vision=visual, workflow_mode="guided", rules_version="source-structured-v3")
    source = work / "source.html"
    source.write_text(TEXT + "\n" + APPENDIX)
    sections = [{"id": "S1", "source_id": "main", "section": "Results", "kind": "body", "text": TEXT},
                {"id": "A1", "source_id": "main", "section": "Appendix A", "kind": "appendix", "text": APPENDIX},
                {"id": "R1", "source_id": "main", "section": "References", "kind": "reference", "text": "[1] Earlier measured method."}]
    coverage = {"full_text": True, "parser": "html-structured-dom-v3", "figures": [], "tables": [],
                "references": [{"id": "bib1", "section_id": "R1"}], "source_selection": {"format": "html"},
                "representations": [{"format": "html", "path": "source.html", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}
    if visual:
        sections.append({"id": "F1", "section": "Figure 1", "kind": "body", "text": "Figure 1. Validation accuracy."})
        coverage["figures"] = [{"id": "F1", "section_id": "F1", "caption": "Figure 1. Validation accuracy."}]
    if table:
        sections.append({"id": "T1", "section": "Table 1", "kind": "body", "role": "table", "text": "Accuracy | 75 | 60"})
        coverage["tables"] = [{"id": "T1", "section_id": "T1", "caption": "Table 1. Accuracy", "rows": [
            [{"text": "Accuracy", "row": 0, "col": 0, "header": True, "headers": []}],
            [{"text": "75", "row": 1, "col": 0, "header": False, "headers": ["Accuracy", "Model"]}],
            [{"text": "60", "row": 2, "col": 0, "header": False, "headers": ["Accuracy", "Baseline"]}]]}]
    pipeline.library.documents["main"] = SourceDocument("main", "Measured method", "https://arxiv.org/html/2607.05147v1",
        "source.html", sections, coverage, hashlib.sha256(source.read_bytes()).hexdigest(), "paper")
    store = MemoryStore()
    agent = AgentTools(pipeline, store, {"kind": "chat" if chat else "read", "agent": {"goal": "Explain measured accuracy"}},
                       lambda kind, inputs: {"passed": True, "issues": []})
    if visual:
        image_path = work / "figure.png"
        pdf = fitz.open()
        page = pdf.new_page(width=100, height=100)
        page.insert_text((10, 30), "75%")
        page.get_pixmap().save(image_path)
        pdf.close()
        metadata = {"figure_id": "F1", "source_id": "main", "section_id": "F1", "caption": "Figure 1. Validation accuracy.",
                    "path": "figure.png", "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(), "mime_type": "image/png"}
        pipeline.library.prepared_visuals["F1"] = metadata
        pipeline.library.prepare_visual = lambda figure_id: copy.deepcopy(metadata)
    return agent, store, pipeline


def invoke(agent, name, **args):
    return agent.call(name, args, str(uuid4()))


def claim(number=1, *, quote=TEXT, **updates):
    result = {"id": f"C{number}", "kind": "result", "statement": "Validation accuracy improves under the stated evaluation conditions.",
              "basis": "observation", "evidence": [{"id": f"E{number}", "source_id": "main", "section_id": "S1", "quote": quote}]}
    result.update(updates)
    return result


def prepare(agent, *, chat=False, complete=True):
    invoke(agent, "source_index")
    invoke(agent, "read_skill_file", path="SKILL.md")
    if complete:
        invoke(agent, "read_source", limit=32000)
    invoke(agent, "save_findings", claims=[claim()])
    for version, view in enumerate(["answer"] if chat else ["brief", "full"]):
        invoke(agent, "edit_draft", section_id="result", view=view, title="Results",
               content="Validation accuracy improves under matched conditions. [E1](evidence:E1)", expected_version=version)


def reviewer(agent):
    def review(kind, context):
        if kind == "fact":
            offset = 0
            while True:
                batch = invoke(agent, "read_findings", offset=offset)
                if batch["next_offset"] is None:
                    break
                offset = batch["next_offset"]
        if kind == "visual":
            for identity in context["required_visual_ids"]:
                invoke(agent, "read_visual", figure_id=identity)
        if kind == "product":
            offset = 0
            while True:
                batch = invoke(agent, "read_document", offset=offset)
                if batch["next_offset"] is None:
                    break
                offset = batch["next_offset"]
        return {"passed": True, "issues": []}
    return review


def review_all(agent):
    agent.review = reviewer(agent)
    return [invoke(agent, "request_review", kind=kind)["id"] for kind in sorted(agent._required_reviews())]


def test_small_findings_repair_preserves_valid_siblings_and_attempts(tmp_path):
    agent, store, _ = fixture(tmp_path)
    good, bad = claim(), claim(2, quote="The claimed accuracy was actually 99% on the unseen test set.")
    result = invoke(agent, "save_findings", claims=[good, bad])
    assert result["accepted"] == ["C1"]
    assert "exact quote" in result["rejected"][0]["error"]
    assert set(store.state["findings"]) == {"C1"}
    assert invoke(agent, "save_findings", claims=[claim(2)])["accepted"] == ["C2"]
    assert set(store.state["findings"]) == {"C1", "C2"}
    with pytest.raises(PipelineError, match="invalid number"):
        invoke(agent, "save_findings", claims=[claim(index) for index in range(6)])
    with pytest.raises(PipelineError, match="must be array"):
        invoke(agent, "save_findings", claims="[]")
    assert len(store.attempts) == 4
    assert len(store.state["findings"]) == 2


def test_numeric_relation_and_original_value_checks_do_not_accept_fabrication(tmp_path):
    agent, _, _ = fixture(tmp_path)
    numeric = {"value": 75, "baseline_value": 60, "relation": "lower", "unit": "%"}
    result = invoke(agent, "save_findings", claims=[claim(numeric_comparison=numeric)])
    assert "contradicts" in result["rejected"][0]["error"]
    numeric.update(relation="higher", value=95)
    result = invoke(agent, "save_findings", claims=[claim(numeric_comparison=numeric)])
    assert "not present" in result["rejected"][0]["error"]
    numeric.update(value=75)
    assert invoke(agent, "save_findings", claims=[claim(numeric_comparison=numeric)])["accepted"] == ["C1"]


def test_section_selection_binds_actual_original_sha_and_evidence_identity(tmp_path):
    agent, store, pipeline = fixture(tmp_path)
    raw = claim()
    raw["evidence"][0].pop("quote")
    raw["evidence"][0]["selection"] = "section"
    assert invoke(agent, "save_findings", claims=[raw])["accepted"] == ["C1"]
    evidence = store.state["evidence"]["E1"]
    assert evidence["quote"] == TEXT
    assert evidence["source_sha256"] == pipeline.library.documents["main"].sha256
    changed = claim(2)
    changed["evidence"][0].update(id="E1", quote=TEXT[:50])
    assert "different content" in invoke(agent, "save_findings", claims=[changed])["rejected"][0]["error"]
    changed["evidence"][0].update(id="E2", source_sha256="0" * 64)
    assert "hash" in invoke(agent, "save_findings", claims=[changed])["rejected"][0]["error"]


def test_range_coverage_never_counts_index_or_partial_section(tmp_path):
    agent, store, _ = fixture(tmp_path)
    invoke(agent, "source_index")
    assert store.state["coverage"]["body"] == {"read": 0, "total": 1}
    invoke(agent, "search_source", query="accuracy")
    invoke(agent, "read_source", section_id="S1", limit=20)
    assert store.state["coverage"]["body"]["read"] == 0
    invoke(agent, "read_source", section_id="S1", offset=20, limit=32000)
    assert store.state["coverage"]["body"]["read"] == 1
    assert store.state["coverage"]["appendix"]["read"] == 0
    invoke(agent, "read_source", section_id="A1")
    assert store.state["coverage"]["complete"]
    assert store.state["coverage"]["references"] == {"indexed": 1, "total": 1}


def test_draft_versions_idempotence_history_and_invalid_write_preservation(tmp_path):
    agent, store, _ = fixture(tmp_path)
    args = {"section_id": "result", "view": "full", "title": "Result", "content": "Saved draft", "expected_version": 0}
    first = agent.call("edit_draft", args, "draft-one")
    again = agent.call("edit_draft", args, "draft-one")
    assert first == again
    assert store.state["document_version"] == 1
    assert len(store.attempts) == 1
    with pytest.raises(PipelineError, match="version conflict"):
        invoke(agent, "edit_draft", **{**args, "content": "Invalid stale draft"})
    invoke(agent, "edit_draft", **{**args, "content": "Revised draft", "expected_version": 1})
    assert store.state["draft_history"]["1"]["section"]["content"] == "Saved draft"
    assert store.state["drafts"]["full:result"]["content"] == "Revised draft"
    assert store.state["document_version"] == 2


def test_all_required_checks_before_formal_reading_and_resume(tmp_path):
    agent, store, pipeline = fixture(tmp_path)
    prepare(agent)
    assert invoke(agent, "validate_work")["passed"]
    denied = invoke(agent, "submit_final", document_version=2, review_ids=[])
    assert not denied["accepted"]
    assert "Missing current passing" in denied["issues"][-1]
    ids = review_all(agent)
    submitted = invoke(agent, "submit_final", document_version=2, review_ids=ids)
    assert submitted["accepted"]
    result = agent.final_result(submitted)
    assert result["verification"]["passed"]
    assert result["provenance"]["execution_engine"] == "pi-agent-v1"
    assert result["evidence_index"][0]["quote"] == TEXT
    # Guidance reads and trusted work survive a new AgentTools process object.
    pipeline.inspected_skill_files.clear()
    restored = AgentTools(pipeline, store, agent.spec, reviewer(agent))
    assert restored.final_result(submitted)["full_markdown"] == result["full_markdown"]
    assert pipeline.inspected_skill_files == {"SKILL.md"}


def test_stale_or_forged_reviews_cannot_publish_changed_document(tmp_path):
    agent, _, _ = fixture(tmp_path)
    prepare(agent)
    ids = review_all(agent)
    invoke(agent, "edit_draft", section_id="result", view="full", title="Results", content="Revised conclusion [E1](evidence:E1)", expected_version=2)
    result = invoke(agent, "submit_final", document_version=3, review_ids=ids + ["made-up-review"])
    assert not result["accepted"]
    assert any("stale" in issue for issue in result["issues"])
    assert any("Unknown host" in issue for issue in result["issues"])
    with pytest.raises(PipelineError, match="Unknown agent tool"):
        invoke(agent, "submit_review", passed=True, issues=[])


def test_final_gate_rechecks_original_source_bytes_after_approval(tmp_path):
    agent, _, pipeline = fixture(tmp_path)
    prepare(agent)
    submitted = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    (pipeline.work_dir / "source.html").write_text("Overwritten source that the earlier review did not inspect")
    with pytest.raises(PipelineError, match="source bytes changed"):
        agent.final_result(submitted)


def test_final_gate_rechecks_uncited_required_visual_pixels(tmp_path):
    agent, _, pipeline = fixture(tmp_path, visual=True)
    prepare(agent)
    invoke(agent, "read_visual", figure_id="F1")
    submitted = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    assert submitted["accepted"]
    # This required figure is absent from the text evidence, but still belongs
    # to the independent visual verdict and must be intact at publication.
    (pipeline.work_dir / "figure.png").write_bytes(b"changed figure pixels")
    with pytest.raises(PipelineError, match="visual bytes changed"):
        agent.final_result(submitted)


def test_saved_guidance_is_restored_only_when_its_hash_still_matches(tmp_path):
    agent, _, pipeline = fixture(tmp_path)
    prepare(agent)
    submitted = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    pipeline.inspected_skill_files.clear()
    (pipeline.skill_root / "SKILL.md").write_text("Changed writing and verification requirements.")
    restored = AgentTools(pipeline, agent.store, agent.spec, reviewer(agent))
    with pytest.raises(PipelineError, match="guidance changed"):
        restored.final_result(submitted)
    invoke(restored, "read_skill_file", path="SKILL.md")
    denied = invoke(restored, "submit_final", document_version=2, review_ids=submitted["review_ids"])
    assert not denied["accepted"] and any("stale" in issue for issue in denied["issues"])
    assert invoke(restored, "submit_final", document_version=2, review_ids=review_all(restored))["accepted"]


def test_source_structured_cells_cannot_change_under_accepted_identity(tmp_path):
    agent, _, pipeline = fixture(tmp_path, table=True)
    invoke(agent, "source_index")
    pipeline.library.documents["main"].coverage["tables"][0]["rows"][1][0]["text"] = "99"
    checked = invoke(agent, "validate_work")
    assert not checked["passed"]
    assert any("source structure changed" in issue for issue in checked["issues"])


def test_retained_representation_bytes_are_checked_even_before_becoming_a_source(tmp_path):
    agent, _, pipeline = fixture(tmp_path)
    retained = pipeline.work_dir / "retained.pdf"
    retained.write_bytes(b"original retained representation")
    pipeline.library.documents["main"].coverage["representations"].append({
        "format": "pdf", "path": "retained.pdf", "sha256": hashlib.sha256(retained.read_bytes()).hexdigest()})
    prepare(agent)
    submitted = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    assert submitted["accepted"]
    retained.write_bytes(b"replaced retained representation")
    with pytest.raises(PipelineError, match="representation bytes changed"):
        agent.final_result(submitted)


def test_full_paper_cannot_drop_appendix_for_pass(tmp_path):
    agent, store, _ = fixture(tmp_path)
    prepare(agent, complete=False)
    invoke(agent, "read_source", section_id="S1")
    result = invoke(agent, "validate_work")
    assert not result["passed"]
    assert "A1" in result["coverage"]["missing_sections"]
    assert store.state["findings"]["C1"]
    assert not invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))["accepted"]


def test_real_visual_reads_and_independent_visual_inspection(tmp_path):
    agent, store, _ = fixture(tmp_path, visual=True)
    prepare(agent)
    assert not invoke(agent, "validate_work")["passed"]
    image = invoke(agent, "read_visual", figure_id="F1")
    assert image["type"] == "image" and base64.b64decode(image["data"]).startswith(b"\x89PNG")
    assert "data" not in store.state["visual_reads"]["F1"]
    # Main-agent inspection alone cannot satisfy an independent visual check.
    result = invoke(agent, "request_review", kind="visual")
    assert not result["passed"]
    assert "did not inspect actual images" in result["issues"][0]
    agent.review = reviewer(agent)
    assert invoke(agent, "request_review", kind="visual")["passed"]


def test_fact_reviewer_must_read_saved_original_evidence(tmp_path):
    agent, _, _ = fixture(tmp_path)
    prepare(agent)
    result = invoke(agent, "request_review", kind="fact")
    assert not result["passed"]
    assert "did not read saved claims" in result["issues"][0]
    agent.review = reviewer(agent)
    assert invoke(agent, "request_review", kind="fact")["passed"]


def test_review_cannot_mutate_drafts_or_findings(tmp_path):
    agent, store, _ = fixture(tmp_path)
    prepare(agent)
    def malicious(kind, context):
        with pytest.raises(PipelineError, match="read-only"):
            invoke(agent, "save_findings", claims=[claim(2)])
        invoke(agent, "read_findings")
        return {"passed": True, "issues": []}
    agent.review = malicious
    assert invoke(agent, "request_review", kind="fact")["passed"]
    assert set(store.state["findings"]) == {"C1"}


def test_table_read_ranges_and_exact_cell_verification(tmp_path):
    agent, store, _ = fixture(tmp_path, table=True)
    prepare(agent)
    invoke(agent, "read_table", table_id="T1", limit=1)
    assert store.state["coverage"]["tables"]["read"] == 0
    invoke(agent, "read_table", table_id="T1", offset=1, limit=10)
    assert store.state["coverage"]["tables"]["read"] == 1
    result = invoke(agent, "table_statistics", table_id="T1")
    assert result["arithmetic_only"]
    raw = claim(2)
    raw["evidence"] = [{"id": "E2", "source_id": "main", "section_id": "T1", "evidence_type": "table_cell",
                        "table_id": "T1", "row": 1, "column": 0, "quote": "95"}]
    assert "does not match" in invoke(agent, "save_findings", claims=[raw])["rejected"][0]["error"]
    raw["evidence"][0]["quote"] = "75"
    assert invoke(agent, "save_findings", claims=[raw])["accepted"] == ["C2"]


def test_chat_scope_pending_draft_and_only_verified_submission(tmp_path):
    agent, store, _ = fixture(tmp_path, chat=True, visual=True)
    prepare(agent, chat=True, complete=False)
    assert store.state["draft_content"]
    assert invoke(agent, "read_document")["status"] == "pending_verification"
    assert invoke(agent, "validate_work")["passed"]
    assert store.state["coverage"]["scope"] == "question"
    ids = review_all(agent)
    result = invoke(agent, "submit_final", document_version=1, review_ids=ids)
    assert result["accepted"]
    final = agent.final_result(result)
    assert final["status"] == "succeeded"
    assert final["content"] == store.state["draft_content"]
    assert "reading_packet" not in final


def test_wait_retains_drafts_and_missing_vision_does_not_fabricate(tmp_path):
    agent, store, pipeline = fixture(tmp_path, visual=True)
    prepare(agent)
    pipeline.vision = False
    with pytest.raises(PipelineError, match="vision model"):
        invoke(agent, "read_visual", figure_id="F1")
    saved = copy.deepcopy(store.state["drafts"])
    result = invoke(agent, "wait_for_input", reason="A configured vision model is required for the paper figures")
    assert result["status"] == "waiting"
    assert store.state["drafts"] == saved
    assert not invoke(agent, "submit_final", document_version=2, review_ids=[])["accepted"]


def test_cancelled_calls_do_not_execute(tmp_path):
    agent, store, pipeline = fixture(tmp_path)
    pipeline.cancelled.set()
    with pytest.raises(TaskCancelled):
        invoke(agent, "save_findings", claims=[claim()])
    assert not store.state and not store.attempts


def test_call_id_reuse_with_new_arguments_is_rejected(tmp_path):
    agent, store, _ = fixture(tmp_path)
    args = {"section_id": "S1", "limit": 20}
    agent.call("read_source", args, "bound-call")
    with pytest.raises(PipelineError, match="different arguments"):
        agent.call("read_source", {**args, "limit": 40}, "bound-call")
    assert store.state["source_reads"]["main"]["S1"] == [[0, 20]]


def test_review_callback_change_invalidates_its_verdict(tmp_path):
    agent, store, _ = fixture(tmp_path)
    prepare(agent)
    def concurrent_change(kind, context):
        invoke(agent, "read_findings")
        # A host-side concurrent revision, not a permitted reviewer tool.
        store.state["conditions"] = ["A newly discovered evaluation condition"]
        return {"passed": True, "issues": []}
    agent.review = concurrent_change
    record = invoke(agent, "request_review", kind="fact")
    assert not record["passed"]
    assert "changed during review" in record["issues"][-1]


def test_long_product_preview_is_not_full_review(tmp_path):
    agent, _, _ = fixture(tmp_path)
    prepare(agent)
    invoke(agent, "edit_draft", section_id="result", view="full", title="Results",
           content="Additional detail. " * 1000 + " [E1](evidence:E1)", expected_version=2)
    assert not invoke(agent, "request_review", kind="product")["passed"]
    agent.review = reviewer(agent)
    assert invoke(agent, "request_review", kind="product")["passed"]


def test_malformed_independent_verdict_does_not_pass(tmp_path):
    agent, _, _ = fixture(tmp_path)
    prepare(agent)
    agent.review = lambda kind, context: {"passed": "true", "issues": []}
    record = invoke(agent, "request_review", kind="coverage")
    assert not record["passed"]
    assert "invalid verdict" in record["issues"][0]


def test_sandbox_tool_real_registered_inputs_and_idempotent_outputs(tmp_path):
    agent, store, pipeline = fixture(tmp_path)
    code = ("from pathlib import Path\n"
            "source = Path('/inputs/source.html').read_text()\n"
            "Path('/outputs/count.txt').write_text(str(len(source)))\n"
            "print('derived characters:', len(source))\n")
    result = agent.call("run_python", {"code": code, "inputs": ["source.html"]}, "python-one")
    assert result["available"], result.get("error")
    assert result["exit_code"] == 0, result
    assert len(result["files"]) == 1
    output = result["files"][0]["path"]
    assert (pipeline.work_dir / output).read_text() == str(len(TEXT + "\n" + APPENDIX))
    assert output in store.state["derived_files"]
    replay = agent.call("run_python", {"code": code, "inputs": ["source.html"]}, "python-one")
    assert replay == result
    assert len(store.state["sandbox_runs"]) == 1
    assert "original source checks" in result["evidence_status"]
    (pipeline.work_dir / "host-run-spec.json").write_text('{"private":"task control"}')
    with pytest.raises(PipelineError, match="registered source"):
        invoke(agent, "run_python", code="print('no')", inputs=["host-run-spec.json"])


def test_alternate_representation_retains_citation_original(tmp_path, monkeypatch):
    agent, store, pipeline = fixture(tmp_path)
    prepare(agent)
    original = copy.deepcopy(store.state["evidence"]["E1"])
    source_pdf = pipeline.work_dir / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 60), TEXT)
        pdf.save(source_pdf)
    digest = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    pipeline.library.documents["main"].coverage["representations"].append({
        "format": "pdf", "path": "source.pdf", "sha256": digest, "url": "https://arxiv.org/pdf/2607.05147v1"})
    monkeypatch.setattr(pipeline.library, "_save", lambda: None)
    result = invoke(agent, "prepare_source", representation="pdf")
    assert result["source_id"] == "main-pdf-" + digest[:12]
    assert store.state["evidence"]["E1"] == original
    assert pipeline.library.documents["main"].path == "source.html"
    assert pipeline.library.documents[result["source_id"]].kind == "paper"
    assert invoke(agent, "validate_work")["passed"]
    assert (pipeline.work_dir / "source.html").read_text() == TEXT + "\n" + APPENDIX


def test_fully_read_version_bound_alternate_supplies_full_text_without_dropping_figures(tmp_path, monkeypatch):
    agent, store, pipeline = fixture(tmp_path, visual=True)
    main = pipeline.library.documents["main"]
    main.coverage.update(full_text=False, missing_text_pages=[1])
    main.coverage["source_selection"]["resolved_id"] = "2607.05147v1"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 60), TEXT)
        pdf.new_page().insert_text((50, 60), APPENDIX)
        pdf.save(pipeline.work_dir / "source.pdf")
    digest = hashlib.sha256((pipeline.work_dir / "source.pdf").read_bytes()).hexdigest()
    main.coverage["representations"].append({"format": "pdf", "path": "source.pdf", "sha256": digest,
                                            "url": "https://arxiv.org/pdf/2607.05147v1"})
    monkeypatch.setattr(pipeline.library, "_save", lambda: None)
    prepare(agent, complete=False)
    alternate = invoke(agent, "prepare_source", representation="pdf")["source_id"]
    invoke(agent, "read_source", source_id=alternate, offset=0, limit=20)
    assert "alternate_full_text" not in store.state["coverage"]
    invoke(agent, "read_source", source_id=alternate, offset=20, limit=32000)
    coverage = store.state["coverage"]
    assert coverage["alternate_full_text"]["source_id"] == alternate
    assert coverage["alternate_full_text"]["sha256"] == digest
    assert coverage["primary_missing_sections"]
    assert coverage["missing_figures"] == ["F1"] and not coverage["complete"]
    assert not invoke(agent, "validate_work")["passed"]
    invoke(agent, "read_visual", figure_id="F1")
    assert invoke(agent, "validate_work")["passed"]
    submitted = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    assert submitted["accepted"]
    assert pipeline.library.agent_alternate_coverage == {"source_id": alternate, "sha256": digest}
    assert main.path == "source.html" and main.coverage["full_text"] is False
    # Full text from a different arXiv revision cannot attest this paper.
    pipeline.library.documents[alternate].coverage["source_selection"]["resolved_id"] = "2607.05147v2"
    assert not agent._coverage(store.state)["complete"]
    assert agent._alternate_coverage(store.state) is None


def test_visual_proposals_are_untrusted_until_actual_independent_review(tmp_path, monkeypatch):
    agent, store, pipeline = fixture(tmp_path, visual=True)
    monkeypatch.setattr(pipeline.library, "_save", lambda: None)
    invoke(agent, "read_visual", figure_id="F1")
    proposed = {"id": "V1", "figure_id": "F1", "observation": "The figure displays validation accuracy of 75%.",
                "readable": True, "conditions": "Validation only", "uncertainties": [], "claim_ids": ["C1"]}
    assert invoke(agent, "save_visual_observations", observations=[proposed])["saved_proposals"] == ["V1"]
    assert not pipeline.library.visual_observations
    raw = claim()
    raw["evidence"] = [{"id": "E1", "source_id": "main", "section_id": "F1", "evidence_type": "image_observation",
                        "quote": "", "figure_id": "F1", "observation_id": "V1",
                        "image_sha256": pipeline.library.prepared_visuals["F1"]["sha256"]}]
    assert "no host-accepted" in invoke(agent, "save_findings", claims=[raw])["rejected"][0]["error"]
    agent.review = reviewer(agent)
    assert invoke(agent, "request_review", kind="visual")["passed"]
    assert pipeline.library.visual_observations["V1"] == store.state["accepted_visual_observations"]["V1"]
    assert invoke(agent, "save_findings", claims=[raw])["accepted"] == ["C1"]
    changed = {**proposed, "observation": "Actually, 95%"}
    result = invoke(agent, "save_visual_observations", observations=[changed])
    assert "immutable" in result["rejected"][0]["error"]
    assert pipeline.library.visual_observations["V1"]["observation"] == proposed["observation"]


def test_text_deficient_pdf_recovers_with_separate_verified_page_observation(tmp_path, monkeypatch):
    agent, store, pipeline = fixture(tmp_path, visual=True)
    main = pipeline.library.documents["main"]
    metadata = pipeline.library.prepared_visuals.pop("F1")
    metadata.update(figure_id="page:1", section_id="page-1", page=1)
    pipeline.library.prepared_visuals["page:1"] = metadata
    source = pipeline.work_dir / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=100, height=100)
        page.insert_image(page.rect, filename=str(pipeline.work_dir / "figure.png"))
        pdf.save(source)
    main.path, main.sha256 = "source.pdf", hashlib.sha256(source.read_bytes()).hexdigest()
    main.sections = [{"id": "page-1", "source_id": "main", "section": "Page 1", "page": 1, "kind": "body", "text": ""}]
    main.coverage.update(full_text=False, parser="pymupdf-v3", missing_text_pages=[1],
                         figures=[{"id": "page:1", "section_id": "page-1", "page": 1, "text_deficient": True}],
                         references=[], source_selection={"format": "pdf"},
                         representations=[{"format": "pdf", "path": "source.pdf", "sha256": main.sha256}])
    monkeypatch.setattr(pipeline.library, "_save", lambda: None)
    invoke(agent, "read_skill_file", path="SKILL.md")
    invoke(agent, "read_visual", figure_id="page:1")
    assert not store.state["coverage"]["complete"]
    proposed = {"id": "VP1", "figure_id": "page:1", "observation": "The scanned original page contains the visible value 75%.",
                "readable": True, "conditions": "Only this value is shown; experimental conditions are absent.",
                "uncertainties": ["No validation or test split is stated on the page."], "claim_ids": ["C1"]}
    invoke(agent, "save_visual_observations", observations=[proposed])
    agent.review = reviewer(agent)
    assert invoke(agent, "request_review", kind="visual")["passed"]
    assert store.state["coverage"]["visual_recovered_pages"] == [1]
    assert store.state["coverage"]["complete"]
    assert main.coverage["full_text"] is False
    assert main.sections[0]["text"] == ""
    raw = claim()
    raw["evidence"] = [{"id": "E1", "source_id": "main", "section_id": "page-1", "page": 1,
                        "evidence_type": "image_observation", "quote": "", "figure_id": "page:1",
                        "image_sha256": metadata["sha256"], "observation_id": "VP1"}]
    assert invoke(agent, "save_findings", claims=[raw])["accepted"] == ["C1"]
    for version, view in enumerate(["brief", "full"]):
        invoke(agent, "edit_draft", section_id="value", view=view, title="Visible value",
               content="The scanned page displays 75%; its evaluation conditions are not stated. [E1](evidence:E1)", expected_version=version)
    assert invoke(agent, "validate_work")["passed"]
    submission = invoke(agent, "submit_final", document_version=2, review_ids=review_all(agent))
    assert submission["accepted"]
    final = agent.final_result(submission)
    assert final["verification"]["source_visual_coverage"]["recovered_pages"] == [1]
    assert final["evidence_index"][0]["quote"] == ""
    assert final["evidence_index"][0]["observation"] == proposed["observation"]
