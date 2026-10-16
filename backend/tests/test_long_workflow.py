"""Real worker/JSONL/PG checks; model messages below are explicit test fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import hashlib
import threading
import time

import pytest

from app.db import SessionLocal
from app.models import Job, Revision, Paper
from app.long_workflow import StructuredWorkflow, compact_phase_input, segment_sections, combine_packets, validate_review, validate_gap_resolutions, SEGMENT_BYTES
from app.pipeline import Pipeline, PipelineError
from app.source_tools import SourceDocument, SourceError
from app.workflow import canonical, digest
from app.worker import Worker
from test_worker_durability import worker_db
from test_workflow import SCRIPT, guided_fixture, snapshot, retry_job, revision_job, calls


def fixture(root, *, long=False, **behavior):
    job, script = guided_fixture(root, **behavior)
    v3 = SCRIPT.replace("if(phase==='extract') output=", "if(phase==='chunk_extract') output=")
    v3 = v3.replace("if(phase==='fact_review') output=", "if(phase==='fact_review'||phase==='merge_review') output=")
    v3 = v3.replace("pendingResult=output;", "if(phase==='chunk_extract') output={packet:{...output,version:2},checked_section_ids:[...new Set(input.source_sections.map(s=>s.id))],unresolved:[]}; pendingResult=output;")
    v3 = v3.replace("has_source:!!input.source_sections", "has_source:!!input.source_sections,segment_id:input.segment_id")
    v3 = v3.replace("if(settings.fail===phase)", "if(settings.fail===phase && (!settings.segment || input.segment_id===settings.segment))")
    (script / "index.js").write_text(v3)
    with SessionLocal() as db, db.begin():
        row = db.get(Job, job.id)
        row.input_snapshot = {**row.input_snapshot, "workflow_version": "guided-v3.0", "source_rules_version": "source-structured-v3",
                              "workflow_limits": {"timeout_seconds": 60, "max_tool_calls": 100,
                              "long_timeout_seconds": 120, "long_max_tool_calls": 200, "segment_concurrency": 2}}
        if long:
            import fitz
            paper = db.get(Paper, row.input_snapshot["paper_id"])
            path = root / paper.storage_path
            # Many real PDF pages, including a final appendix, make >64KiB.
            document = fitz.open()
            for n in range(85):
                page = document.new_page()
                lines = ["The method improves accuracy from 70 percent to 75 percent.",
                         "Appendix: conditional counterexample." if n == 84 else "Methods and results."]
                lines += ["The evaluation uses a fixed dataset and a fixed hardware budget."] * 16
                page.insert_text((40, 40), "\n".join(lines), fontsize=10)
            document.save(path)
            document.close()
    return job, script


def test_segment_utf8_offsets_and_appendix_coverage():
    text = ("中文并行读取，不丢失原文。\n\n" * 2000)
    sections = [{"id": "body", "text": text, "kind": "body"},
                {"id": "appendix", "text": "Counterexample: selection bias.", "kind": "appendix"},
                {"id": "refs", "text": "Reference list", "kind": "reference"}]
    chunks = segment_sections(sections)
    assert all(chunk["bytes"] <= SEGMENT_BYTES for chunk in chunks)
    body = [s for c in chunks for s in c["sections"] if s["id"] == "body"]
    assert "".join(s["text"] for s in body) == text
    assert body[0]["source_offset"] == 0 and body[-1]["source_end"] == len(text)
    assert any("appendix" in c["section_ids"] for c in chunks)
    assert all("refs" not in c["section_ids"] for c in chunks)


def test_large_table_groups_preserve_spans_headers_and_rows():
    header = [{"text": "Model", "row": 0, "col": 0, "header": True, "rowspan": 2, "colspan": 1},
              {"text": "Metric (%)", "row": 0, "col": 1, "header": True, "rowspan": 1, "colspan": 2}]
    subheader = [{"text": "Validation", "row": 1, "col": 1, "header": True, "rowspan": 1, "colspan": 1},
                 {"text": "Test", "row": 1, "col": 2, "header": True, "rowspan": 1, "colspan": 1}]
    data = [[{"text": str(n) + "x" * 80, "row": n, "col": 0, "header": False,
              "rowspan": 1, "colspan": 1, "headers": ["Model"]}] for n in range(2, 70)]
    table = {"id": "T1", "rows": [header, subheader] + data, "footnotes": ["Same hardware."], "caption": "Results"}
    groups = segment_sections([{"id": "T1", "text": "Results", "role": "table", "table": table}], 1024)
    rows = []
    for segment in groups:
        for section in segment["sections"]:
            assert section["table"]["rows"][:2] == [header, subheader]
            rows.extend(c["row"] for row in section["table"]["rows"][2:] for c in row)
    assert rows == list(range(2, 70))
    assert all(s["bytes"] <= 1024 for s in groups)


def test_v3_real_worker_publishes_typed_packet_and_coverage(worker_db):
    job, script = fixture(worker_db)
    Worker().run(once=True)
    result = snapshot(job.id)
    assert result.status == "succeeded", result.error
    with SessionLocal() as db:
        revision = db.query(Revision).filter_by(reading_id=job.reading_id).one()
        assert revision.reading_packet["version"] == 2
        assert revision.verification["section_coverage"]["complete"] is True
        assert revision.verification["figure_checks"] == {"total": 0, "verified": 0, "complete": True, "items": [], "unresolved": []}
        assert revision.verification["workflow"]["native_status"] == "not_requested"
        assert revision.evidence_index[0]["verification_method"] == "quote_match"
    assert result.usage["tool_calls"] == sum(r.get("usage", {}).get("tool_calls", 0) for r in result.stage_records)


def test_long_parallel_segments_reuse_validated_checkpoints(worker_db):
    job, script = fixture(worker_db, long=True, fail="compose")
    Worker().run(once=True)
    first = snapshot(job.id)
    assert first.status == "failed"
    plan = next(r for r in first.stage_records if r["name"] == "segment_plan")["summary"]
    assert plan["long_document"] is True and plan["concurrency"] == 2
    assert len(plan["segments"]) >= 3
    assert all(s["bytes"] <= SEGMENT_BYTES for s in plan["segments"])
    before = calls(script)
    assert len([c for c in before if c["phase"] == "chunk_extract"]) == len(plan["segments"])
    retry = retry_job(job.id)
    (script / "behavior.json").write_text("{}")
    Worker().run(once=True)
    second = snapshot(retry)
    assert second.status == "succeeded", second.error
    after = calls(script)[len(before):]
    assert [c["phase"] for c in after] == ["compose", "product_review"]
    assert any(r["name"] == "segment_1_extract" and r["status"] == "reused" for r in second.stage_records)


def test_v3_rejects_uncovered_claim_and_retains_failed_summary(worker_db):
    job, _ = fixture(worker_db, incomplete=True)
    Worker().run(once=True)
    result = snapshot(job.id)
    assert result.status == "failed"
    coverage = next(r for r in result.stage_records if r["name"] == "section_coverage")
    assert coverage["summary"]["complete"] is False
    assert coverage["summary"]["segments"][0]["status"] == "failed"
    with SessionLocal() as db:
        assert db.query(Revision).filter_by(reading_id=job.reading_id).count() == 0


def test_v3_presentation_revision_reuses_authenticated_facts(worker_db):
    job, script = fixture(worker_db)
    Worker().run(once=True)
    first = snapshot(job.id)
    assert first.status == "succeeded", first.error
    before = calls(script)
    revised = revision_job(job.id)
    Worker().run(once=True)
    result = snapshot(revised)
    assert result.status == "succeeded", result.error
    assert [c["phase"] for c in calls(script)[len(before):]] == ["revision_plan", "compose", "product_review"]
    assert any(r["name"] == "extract_reused" and r["status"] == "reused" for r in result.stage_records)
    with SessionLocal() as db:
        revisions = db.query(Revision).filter_by(reading_id=job.reading_id).order_by(Revision.number).all()
        assert len(revisions) == 2
        assert revisions[0].reading_packet == revisions[1].reading_packet


@pytest.mark.parametrize("conflict", [False, True])
def test_scanned_pdf_visual_recovery_is_not_a_text_quote(worker_db, conflict):
    import fitz
    from app.models import ModelConnection
    job, script = fixture(worker_db)
    with SessionLocal() as db, db.begin():
        row = db.get(Job, job.id)
        connection = db.get(ModelConnection, row.input_snapshot["model"]["id"])
        connection.vision = True
        row.input_snapshot = {**row.input_snapshot, "model": {**row.input_snapshot["model"], "vision": True}}
        paper = db.get(Paper, row.input_snapshot["paper_id"])
        path = worker_db / paper.storage_path
        original = fitz.open(path)
        image = original[0].get_pixmap().tobytes("png")
        original.close()
        scanned = fitz.open()
        scanned.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=image)
        scanned.save(path)
        scanned.close()
    content = (script / "index.js").read_text()
    content = content.replace("if(msg.type==='tool_response'&&pendingResult){advance();return;}", """
if(msg.type==='tool_response'&&pendingResult){
 if(msg.result?.type==='image'&&pendingResult.observations){const o=pendingResult.observations[0];o.image_sha256=msg.result.sha256;o.section_id=msg.result.section_id;}
 advance();return;
}
""")
    content = content.replace("return {id:'E1',source_id:'main',section_id:section.id,quote:",
                              "if(section.source_method==='vision')return {id:'E1',source_id:'main',quote:'',evidence_type:'image_observation',...section.visual_evidence};return {id:'E1',source_id:'main',section_id:section.id,quote:")
    content = content.replace("pendingResult=output;", """
if(phase==='figure_review'){
 const f=input.figures[0];output={passed:true,issues:[],observations:[{figure_id:f.id,image_sha256:'',section_id:f.section_id,
 observation:'The method improves accuracy from 70 percent to 75 percent. Abstract and Methods. An experiment compares the finetuned baseline and model averaging.',
 readable:true,conditions:'Fixture source image',uncertainties:[],claim_ids:input.packet.claims.map(c=>c.id)}]};
 queue=[{name:'read_visual',args:{figure_id:f.id}}];
}
pendingResult=output;
""")
    (script / "index.js").write_text(content)
    if conflict:
        content = content.replace("pendingResult=output;", """
if(phase==='figure_review'&&input.purpose!=='page_transcription'&&!input.packet.claims[0].statement.includes('Visually clarified')){
 output.passed=false;output.issues=[{claim_id:input.packet.claims[0].id,message:'Clarify the fixture visual condition'}];
}
if(phase==='packet_repair'&&input.visual_observations?.length){
 const observation=input.visual_observations[0],claim=input.packet.claims[0];
 output={claims:[{...claim,statement:claim.statement+' Visually clarified.',evidence:[...claim.evidence,{id:'E8',source_id:'main',section_id:observation.section_id,quote:'',evidence_type:'image_observation',figure_id:observation.figure_id,image_sha256:observation.image_sha256,observation_id:observation.id}]}],remove_claim_ids:[]};
}
pendingResult=output;
""")
        (script / "index.js").write_text(content)
    Worker().run(once=True)
    result = snapshot(job.id)
    assert result.status == "succeeded", result.error
    if conflict:
        assert any(r["name"] == "figure_1_repair" and r["status"] == "succeeded" for r in result.stage_records)
    with SessionLocal() as db:
        revision = db.query(Revision).filter_by(reading_id=job.reading_id).one()
        assert revision.verification["coverage"]["full_text"] is False
        assert revision.verification["source_visual_coverage"]["complete"] is True
        assert revision.verification["source_visual_coverage"]["recovered_pages"] == [1]
        assert revision.verification["server_quote_check"] == "not_requested"
        assert all(e["evidence_type"] == "image_observation" and e["quote"] == "" for e in revision.evidence_index)
        source = next(a for a in revision.artifacts if a["name"] == "source_corpus.json")
        corpus = json.loads((worker_db / source["path"]).read_text())
        assert corpus["documents"][0]["sections"][0]["text"] == ""
    before = calls(script)
    revised = revision_job(job.id)
    Worker().run(once=True)
    revision_result = snapshot(revised)
    assert revision_result.status == "succeeded", revision_result.error
    assert [c["phase"] for c in calls(script)[len(before):]] == ["revision_plan", "compose", "product_review"]


def workflow_probe(root):
    work, skill = root / "work", root / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Explicit test fixture guidance")
    paper = {"id": "fixture", "title": "Fixture", "source_type": "pdf"}
    pipeline = Pipeline(work, skill, paper, "ljg-paper", "tenant", threading.Event(), root,
                        workflow_mode="guided", rules_version="source-structured-v3")
    pipeline.phase_local = threading.local()
    source = work / "source.txt"
    source.write_text("Original retained source.")
    pipeline.library.documents["main"] = SourceDocument("main", "Fixture", None, "source.txt",
        [{"id": "p1", "source_id": "main", "text": source.read_text()}],
        {"full_text": True, "figures": [], "representations": [{"format": "html", "path": "source.txt",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "url": None, "arxiv_id": None}]}, hashlib.sha256(source.read_bytes()).hexdigest())
    spec = {"skill_root": str(skill), "skill_id": "ljg-paper", "paper": paper, "kind": "read",
            "source_rules_version": "source-structured-v3", "model": {"context_window": 128000}, "limits": {}}
    def save(name, status, **values):
        output = values.pop("output", None)
        record = {"name": name, "status": status, **values}
        if output is not None:
            checkpoint = pipeline.library.export_checkpoint()
            record.update(output_hash=digest(output), source_hash=digest(checkpoint))
            path = work / "host-workflow" / (name + ".json")
            path.write_text(canonical({"input_hash": record["input_hash"], "output_hash": record["output_hash"],
                                      "output": output, "source_hash": record["source_hash"], "source_checkpoint": checkpoint}))
        return record
    return StructuredWorkflow(SimpleNamespace(id="job"), spec, pipeline, lambda *a: None, save, [], time.monotonic() + 60)


def test_cached_stage_preserves_newer_searches_and_representations(tmp_path):
    workflow = workflow_probe(tmp_path)
    workflow.checkpoint("cached", {}, lambda: {"ok": True})
    library = workflow.pipeline.library
    library.candidates["new"] = {"id": "new", "title": "New candidate"}
    library.query_log = ["new query"]
    library.searches = 1
    library.search_records["new query"] = {"candidate_ids": ["new"], "errors": [], "completed": True}
    library.limitations.append("Newer authenticated limitation")
    extra = library.work_dir / "source-extra.pdf"
    extra.write_bytes(b"%PDF-fixture-representation")
    library.documents["main"].coverage["representations"].append({"format": "pdf", "path": extra.name,
        "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(), "url": None, "arxiv_id": None})
    before = library.export_checkpoint()
    assert workflow.checkpoint("cached", {}, lambda: pytest.fail("Unchanged stage should reuse")) == {"ok": True}
    assert library.export_checkpoint() == before


def test_large_visual_context_stays_readable_by_observation_identity(tmp_path):
    workflow = workflow_probe(tmp_path)
    workflow.sections = [{"id": f"p{n}", "source_id": "main", "text": "visible page content " * 400,
                          "source_method": "vision", "visual_evidence": {"observation_id": f"VP{n}"}}
                         for n in range(1, 10)]
    packet = {"claims": [{"evidence": [{"source_id": "main", "section_id": f"p{n}", "quote": "",
                                       "evidence_type": "image_observation"}]} for n in range(1, 10)]}
    context = workflow._packet_context(packet)
    assert len(context) == 9 and all(s["text"] and s["source_method"] == "vision" for s in context)
    assert all(s["excerpt"] is True and s["visual_evidence"] for s in context)


def test_short_packet_preserves_nonsequential_ids(worker_db):
    job, script = fixture(worker_db)
    content = (script / "index.js").read_text().replace("'C1'", "'C7'").replace("E1", "E9")
    (script / "index.js").write_text(content)
    Worker().run(once=True)
    result = snapshot(job.id)
    assert result.status == "succeeded", result.error
    with SessionLocal() as db:
        revision = db.query(Revision).filter_by(reading_id=job.reading_id).one()
        assert revision.reading_packet["claims"][0]["id"] == "C7"
        assert revision.verification["fact_review"]["checked_claim_ids"] == ["C7"]


def test_multiple_tables_cannot_be_silently_omitted():
    from app.structured_source import parse_structured_html
    html = '<article class="ltx_document"><section><h2>Method</h2><p>' + 'Real source prose. ' * 80 + '</p><figure class="ltx_table"><table><tr><td>A</td></tr></table><table><tr><td>B</td></tr></table></figure></section></article>'
    with pytest.raises(SourceError, match="multiple tables"):
        parse_structured_html(html.encode())


def test_structured_heading_reads_descendants_with_canonical_quote_spans(tmp_path):
    workflow = workflow_probe(tmp_path)
    library = workflow.pipeline.library
    library.documents["main"].sections = [
        {"id": "S1", "text": "Methods", "role": "heading", "anchors": ["S1", "original-dom-anchor"]},
        {"id": "P1", "text": "A concrete method and condition.", "parent_id": "S1", "anchors": ["original-dom-anchor"]},
        {"id": "S2", "text": "Appendix", "role": "heading"}]
    value = library.read_source(section_id="S1")
    assert value["text"] == "Methods\n\nA concrete method and condition."
    assert [s["section_id"] for s in value["section_spans"]] == ["S1", "P1"]
    alias = library.read_source(section_id="original-dom-anchor")
    assert alias["section_id"] == "P1" and alias["text"] == "A concrete method and condition."


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_v3_retry_recreates_damaged_attempt_source_from_original_upload(worker_db, damage):
    job, script = fixture(worker_db, fail="compose")
    Worker().run(once=True)
    assert snapshot(job.id).status == "failed"
    cached = worker_db / "tmp" / job.id / "1" / "source.pdf"
    if damage == "missing":
        cached.unlink()
    else:
        cached.write_bytes(b"%PDF-damaged disposable copy")
    retry = retry_job(job.id)
    (script / "behavior.json").write_text("{}")
    Worker().run(once=True)
    result = snapshot(retry)
    assert result.status == "succeeded", result.error
    inventory = next(r for r in result.stage_records if r["name"] == "source_inventory")["summary"]
    assert inventory["cache_recovery"]["quarantined_files"]


def test_visual_paging_reaches_later_observations_in_same_section(tmp_path):
    workflow = workflow_probe(tmp_path)
    library = workflow.pipeline.library
    library.visual_observations = {identity: {"id": identity, "section_id": "p1", "figure_id": "page:1",
        "readable": True, "observation": text} for identity, text in [("VP1", "A" * 6000), ("V1", "B" * 100)]}
    cursor, recovered = 0, {"VP1": "", "V1": ""}
    while cursor is not None:
        value = library.read_source(section_id="p1", offset=cursor, limit=2000)
        for observation in value["visual_observations"]:
            recovered[observation["id"]] += observation["observation"]
        cursor = value["visual_next_offset"]
    assert recovered == {"VP1": "A" * 6000, "V1": "B" * 100}


def test_table_math_separates_percentage_of_means_and_mean_percentages(tmp_path):
    from app.table_math import table_statistics
    from decimal import Decimal
    workflow = workflow_probe(tmp_path)
    table = {"id": "T", "rows": [[{"row": r, "col": c, "text": str(value), "header": False,
        "headers": ["Dataset", str(c), "Model", label]} for c, value in enumerate(values)]
        for r, label, values in [(0, "Baseline", [1, 9]), (1, "Method", [2, 9])]]}
    workflow.pipeline.library.documents["main"].coverage["tables"] = [table]
    stats = table_statistics(workflow.pipeline.library, "T")
    assert stats["rows"][0]["sum"] == "10" and stats["rows"][1]["sum"] == "11"
    comparison = next(c for c in stats["comparisons_with_matching_header_prefixes"] if c["baseline_row"] == 0)
    assert Decimal(comparison["relative_change_of_mean_percent"]) == 10
    assert Decimal(comparison["mean_relative_change_percent"]) == 50


def test_svg_pattern_is_clipped_to_its_shape():
    import io
    from PIL import Image, ImageChops
    from app.structured_source import render_image
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><defs>
    <pattern id="h" patternUnits="userSpaceOnUse" width="8" height="8"><rect width="8" height="8" fill="white"/><path d="M-2 2L2 -2M0 8L8 0M6 10L10 6" stroke="blue"/></pattern>
    </defs><circle cx="50" cy="50" r="10" fill="url(#h)" stroke="green"/></svg>'''
    data, format = render_image(svg, "image/svg+xml")
    image = Image.open(io.BytesIO(data)).convert("RGB")
    box = ImageChops.difference(image, Image.new("RGB", image.size, "white")).getbbox()
    assert format == "svg" and box is not None
    assert box[0] >= image.width * 0.35 and box[2] <= image.width * 0.65
    assert box[1] >= image.height * 0.35 and box[3] <= image.height * 0.65


def test_phase_retry_retains_failure_usage_without_changing_original_inputs(tmp_path):
    workflow = workflow_probe(tmp_path)
    calls = []
    def run(phase, inputs, timeout, tools, output_cap):
        calls.append(deepcopy(inputs))
        workflow.pipeline.tool_calls += 1
        workflow.pipeline.phase_local.usage = {"tool_calls": 1, "output_tokens": len(calls)}
        assert timeout <= 300
        if len(calls) == 1:
            raise PipelineError("Synthetic transient protocol failure")
        return {"brief_markdown": "Brief", "full_markdown": "Full"}
    workflow.run_phase = run
    original = {"instruction": "Preserve facts"}
    assert workflow.model_stage("compose", "compose", original)["brief_markdown"] == "Brief"
    assert original == {"instruction": "Preserve facts"} and len(calls) == 2
    failed = [r for r in workflow.records if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["usage"]["output_tokens"] == 1
    assert workflow.completed["compose"]["usage"]["output_tokens"] == 2


def test_terminal_phase_retry_retains_both_call_usage_records(tmp_path):
    workflow = workflow_probe(tmp_path)
    saved, counter = [], [0]
    original_save = workflow.save_stage
    def save(name, status, **values):
        saved.append({"name": name, "status": status, **values})
        return original_save(name, status, **values)
    workflow.save_stage = save
    def fail(*args):
        counter[0] += 1
        workflow.pipeline.phase_local.usage = {"tool_calls": 1, "output_tokens": counter[0]}
        raise PipelineError("Synthetic repeated failure")
    workflow.run_phase = fail
    with pytest.raises(PipelineError, match="repeated failure"):
        workflow.model_stage("compose", "compose", {})
    failures = [s for s in saved if s["status"] == "failed"]
    assert [f["usage"]["output_tokens"] for f in failures] == [1, 2]


def test_v2_six_evidence_items_are_all_checked_and_v1_limit_is_unchanged(tmp_path):
    from app.evidence import validate_packet
    workflow = workflow_probe(tmp_path)
    quotes = [f"Original retained evidence clause number {i}." for i in range(6)]
    workflow.pipeline.library.documents["main"].sections[0]["text"] = "\n".join(quotes)
    evidence = [{"id": f"E{i+1}", "source_id": "main", "section_id": "p1", "quote": text} for i, text in enumerate(quotes)]
    packet = {"version": 2, "paper_title": "Fixture", "research_question": "Question", "claims": [
        {"id": "C1", "kind": "result", "statement": "Six observations", "basis": "observation",
         "conditions": {k: "" for k in ["model", "dataset", "split", "metric", "baseline", "budget"]}, "evidence": evidence}],
         "open_questions": [], "limitations": []}
    assert not validate_packet(packet, workflow.pipeline.library)[1]
    assert validate_packet({**packet, "version": 1}, workflow.pipeline.library)[1]
    packet["claims"][0]["evidence"][-1]["quote"] = "This final evidence clause is invented."
    assert any("E6" in e for e in validate_packet(packet, workflow.pipeline.library)[1])


def test_pdf_reference_index_preserves_explicit_appendix_and_skips_reference_art(tmp_path):
    import fitz
    from app.structured_source import parse_structured_pdf
    path = tmp_path / "indexed.pdf"
    doc = fitz.open()
    pages = ["Methods\n" + "Original method and experiment. " * 5 + "\nReferences\nA. Author. Paper, 2024.",
             "A. Author. Reference title, 2024.\n" * 8,
             "Appendices\nA. Counterexample\n" + "The removed stopping condition introduces selection bias. " * 4]
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 700), text, fontsize=10)
        page.draw_line((40, 720), (500, 720))
    doc.save(path)
    doc.close()
    sections, coverage = parse_structured_pdf(path)
    assert [s["kind"] for s in sections] == ["body", "reference", "appendix"]
    assert len(coverage["references"]) == 2
    assert {f["page"] for f in coverage["figures"]} == {1, 3}
    groups = segment_sections(sections)
    assert "p3" in {sid for g in groups for sid in g["section_ids"]}


def test_html_locator_copies_original_and_never_claims_a_model_quote_match(tmp_path):
    from app.evidence import resolve_evidence, PacketError
    workflow = workflow_probe(tmp_path)
    library = workflow.pipeline.library
    library.documents["main"].coverage["parser"] = "html-structured-dom-v3"
    locator = {"id": "E1", "source_id": "main", "section_id": "p1"}
    evidence = resolve_evidence(locator, library)
    assert evidence["quote"] == "Original retained source."
    assert evidence["selection"] == "section" and evidence["verification_method"] == "section_lookup"
    # Explicit section selection resolves host source text; a supplied caption
    # string is not used as a purportedly matched original quotation.
    selected = resolve_evidence({**locator, "selection": "section", "quote": "A model paraphrase that is not the original.", "page": 99}, library)
    assert selected["quote"] == evidence["quote"] and "page" not in selected
    with pytest.raises(PacketError, match="exact quote"):
        resolve_evidence({**locator, "quote": "A model paraphrase that is not the original."}, library)
    with pytest.raises(PacketError):
        resolve_evidence({**locator, "section_id": "another-tenant-source"}, library)
    with pytest.raises(PacketError):
        resolve_evidence(locator, library, packet_version=1)
    library.documents["main"].sections[0]["role"] = "heading"
    with pytest.raises(PacketError, match="explicit short quote"):
        resolve_evidence(locator, library)
    library.documents["main"].sections[0].pop("role")
    library.documents["main"].coverage["parser"] = "pymupdf-page-text-v3"
    with pytest.raises(PacketError, match="explicit short quote"):
        resolve_evidence(locator, library)


def test_table_locator_materializes_exact_cell_and_rejects_conflicting_supplied_headers(tmp_path):
    from app.evidence import resolve_evidence, PacketError
    workflow = workflow_probe(tmp_path)
    library = workflow.pipeline.library
    library.documents["main"].coverage["tables"] = [{"id": "T", "section_id": "p1", "rows": [[
        {"row": 2, "col": 3, "text": "5.14", "headers": ["Math", "Metric", "Model", "Baseline"]}]]}]
    locator = {"id": "E1", "source_id": "main", "section_id": "p1", "evidence_type": "table_cell", "table_id": "T", "row": 2, "column": 3}
    result = resolve_evidence(locator, library)
    assert result["quote"] == "5.14" and result["headers"] == ["Math", "Metric", "Model", "Baseline"]
    with pytest.raises(PacketError, match="header chain"):
        resolve_evidence({**locator, "headers": ["Wrong"]}, library)
    with pytest.raises(PacketError, match="cell value"):
        resolve_evidence({**locator, "quote": "5.15"}, library)


def test_prompt_excerpt_compaction_is_lossless_and_never_changes_patch_packets():
    evidence = {"id": "E1", "source_id": "main", "section_id": "s1", "quote": "Complete original paragraph."}
    packet = {"claims": [{"id": "C1", "evidence": [evidence]}, {"id": "C2", "evidence": [{**evidence, "id": "E2"}]}]}
    inputs = {"packet": packet}
    compact = compact_phase_input("compose", inputs)
    assert compact["source_excerpts"] == [{"source_id": "main", "section_id": "s1", "text": evidence["quote"], "evidence_ids": ["E1", "E2"]}]
    assert all("quote" not in c["evidence"][0] for c in compact["packet"]["claims"])
    assert inputs["packet"]["claims"][0]["evidence"][0]["quote"] == evidence["quote"]
    assert compact_phase_input("packet_repair", inputs) == inputs


def test_merge_pending_questions_require_evidence_and_explicit_resolution():
    packet = {"claims": [{"id": "C1", "evidence": [{"id": "E1"}]}]}
    gaps = [{"id": "G1", "question": "Appendix pending"}]
    report = {"passed": True, "checked_claim_ids": ["C1"], "issues": [], "suggestions": []}
    with pytest.raises(PipelineError, match="every pending"):
        validate_gap_resolutions(report, packet, gaps, {})
    entry = {"id": "G1", "status": "resolved", "explanation": "Appendix counterexample now read and cited.", "evidence_ids": ["E1"], "figure_ids": []}
    assert validate_gap_resolutions({**report, "gap_resolutions": [entry]}, packet, gaps, {})["passed"]
    for changed in [{**entry, "evidence_ids": []}, {**entry, "evidence_ids": ["E99"]},
                    {**entry, "status": "unresolved"}, {**entry, "figure_ids": ["F1"]}]:
        with pytest.raises(PipelineError):
            validate_gap_resolutions({**report, "gap_resolutions": [changed]}, packet, gaps, {})
