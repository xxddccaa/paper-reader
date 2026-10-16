"""Offline typed evidence, actual image reads, forgery and compatibility gates."""
import base64
import copy
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import fitz
import pytest

from app.evidence import PacketError, apply_claim_patch, packet_evidence, validate_packet, validate_visual_source_coverage
from app.pipeline import BudgetExhausted, Pipeline, PipelineError, PYTHON_FILE_GUARD, TaskCancelled, validate_result
from app.source_tools import SourceDocument, SourceLibrary


TEXT = "The measured accuracy is 75%² on the held-out validation split."


def library_fixture(root):
    library = SourceLibrary(root, {"id": "typed-fixture", "title": "Bounded measurements"}, "ljg-paper")
    source = root / "source.txt"
    source.write_text(TEXT)
    headers = ["Validation", "Accuracy (%)", "Model A"]
    cell = {"text": "75%²", "row": 2, "col": 1, "rowspan": 1, "colspan": 2,
            "header": False, "headers": headers}
    table = {"id": "T1", "section_id": "T1", "caption": "Validation accuracy",
             "rows": [[{"text": "Validation", "row": 0, "col": 1, "rowspan": 1,
                        "colspan": 2, "header": True, "headers": []}],
                      [{"text": "Accuracy (%)", "row": 1, "col": 1, "rowspan": 1,
                        "colspan": 2, "header": True, "headers": ["Validation"]}],
                      [{"text": "Model A", "row": 2, "col": 0, "rowspan": 1,
                        "colspan": 1, "header": True, "headers": []}, cell]],
             "footnotes": ["² Validation only."]}
    library.documents["main"] = SourceDocument(
        "main", "Bounded measurements", "https://arxiv.org/html/2607.05147v1", "source.txt",
        [{"id": "S1", "source_id": "main", "section": "Results", "text": TEXT},
         {"id": "T1", "source_id": "main", "section": "Validation table", "kind": "table",
          "text": "Validation | Accuracy (%) | Model A | 75%²", "table_id": "T1"},
         {"id": "F1", "source_id": "main", "section": "Figure", "text": "A validation accuracy chart."}],
        {"full_text": True, "tables": [table], "figures": [{"id": "F1", "section_id": "F1",
          "caption": "A validation accuracy chart.", "images": []}]}, hashlib.sha256(source.read_bytes()).hexdigest())
    library.visual_observations = {}
    library.prepared_visuals = {}
    return library


def evidence(kind="text_quote", **updates):
    item = {"id": "E1", "source_id": "main", "section_id": "S1", "quote": TEXT,
            "evidence_type": kind}
    if kind == "table_cell":
        item.update(section_id="T1", quote="75%²", table_id="T1", row=2, column=1,
                    headers=["Validation", "Accuracy (%)", "Model A"])
    elif kind == "image_observation":
        item.update(section_id="F1", quote="", figure_id="F1", image_sha256="0" * 64, observation_id="V1")
    item.update(updates)
    return item


def packet(item=None, *, version=2, count=1):
    item = evidence() if item is None else item
    return {"version": version, "paper_title": "Bounded measurements", "research_question": "What is measured?",
            "claims": [{"id": f"C{index + 1}", "kind": "result", "basis": "observation",
                        "statement": "Validation accuracy is reported under limited conditions.",
                        "conditions": {key: "" for key in ("model", "dataset", "split", "metric", "baseline", "budget")},
                        "evidence": [copy.deepcopy(item)]} for index in range(count)],
            "open_questions": [], "limitations": ["Only the validation split is measured."]}


def product(items):
    citations = " ".join(f'[{item["id"]}]' for item in items)
    return {"brief_markdown": "Reported observations " + citations,
            "full_markdown": "Reported observations and limited conditions " + citations,
            "evidence_index": items}


def prepare_image(library):
    path = library.work_dir / "host-visuals/figure.png"
    path.parent.mkdir(exist_ok=True)
    pdf = fitz.open()
    page = pdf.new_page(width=100, height=100)
    page.insert_text((10, 30), "75%")
    page.get_pixmap().save(path)
    pdf.close()
    metadata = {"figure_id": "F1", "source_id": "main", "section_id": "F1", "caption": "Validation chart",
                "path": "host-visuals/figure.png", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mime_type": "image/png"}
    library.prepared_visuals["F1"] = metadata
    return metadata


def accepted_visual(library, *, ledger=True):
    metadata = prepare_image(library)
    record = {"id": "V1", "figure_id": "F1", "image_sha256": metadata["sha256"], "section_id": "F1",
              "observation": "A validation value of 75% is readable.", "readable": True,
              "conditions": "Validation split only", "uncertainties": ["No test-set value is shown."], "claim_ids": ["C1"]}
    library.visual_observations["V1"] = record
    if ledger:
        (library.work_dir / "host-image-inspections.json").write_text(json.dumps({metadata["path"]: metadata["sha256"]}))
    return evidence("image_observation", image_sha256=metadata["sha256"])


def pipeline_fixture(root, *, vision=True, **kwargs):
    project = Path(__file__).resolve().parents[2]
    return Pipeline(root, project / "skills/ljg-paper", {"id": "typed-fixture", "title": "Bounded measurements"},
                    "ljg-paper", "team", threading.Event(), project, vision=vision,
                    workflow_mode="guided", rules_version="source-structured-v3", **kwargs)


def test_combined_v2_packet_limit_and_v1_unchanged(tmp_path):
    library = library_fixture(tmp_path)
    clean, errors = validate_packet(packet(count=80), library)
    assert not errors and len(clean["claims"]) == 80
    assert validate_packet(packet(count=81), library)[1]
    old = evidence()
    old.pop("evidence_type")
    incoming = packet(old, version=1, count=20)
    assert validate_packet(incoming, library) == (incoming, [])
    projected = packet_evidence(incoming, library)
    assert "evidence_type" not in projected[0] and "verification_method" not in projected[0]
    assert validate_packet(packet(old, version=1, count=21), library)[1]
    assert validate_packet(packet(evidence(), version=1), library)[1]


def test_v2_defaults_to_text_and_patch_preserves_unlisted_claims(tmp_path):
    library = library_fixture(tmp_path)
    plain = evidence()
    plain.pop("evidence_type")
    incoming = packet(plain, count=80)
    assert packet_evidence(incoming, library)[0]["verification_method"] == "quote_match"
    replacements = copy.deepcopy(incoming["claims"][:21])
    for claim in replacements:
        claim["statement"] = "A more limited observation."
    updated = apply_claim_patch(incoming, {"claims": replacements, "remove_claim_ids": [f"C{i}" for i in range(22, 44)]})
    assert len(updated["claims"]) == 58 and updated["claims"][21:] == incoming["claims"][43:]
    assert validate_packet(updated, library)[1] == [] and incoming["claims"][0] != replacements[0]
    with pytest.raises(PacketError):
        apply_claim_patch(incoming, {"claims": [], "remove_claim_ids": [f"C{i}" for i in range(1, 81)]})


@pytest.mark.parametrize("change", [
    {"evidence_type": "unsupported"}, {"evidence_type": []}, {"row": True}, {"column": -1},
    {"headers": "Validation"}, {"headers": [5]}, {"quote": 75}, {"verified": True},
    {"verification_method": "table_cell_match"}, {"page": True}, {"observation_id": "V1"},
])
def test_typed_schema_rejects_malformed_or_model_verified_evidence(tmp_path, change):
    assert validate_packet(packet(evidence("table_cell", **change)), library_fixture(tmp_path))[1]


def test_table_cell_preserves_short_value_footnote_and_full_headers(tmp_path):
    library = library_fixture(tmp_path)
    incoming = packet(evidence("table_cell"))
    assert validate_packet(incoming, library) == (incoming, [])
    entry = packet_evidence(incoming, library)[0]
    assert entry["quote"] == "75%²" and entry["headers"] == ["Validation", "Accuracy (%)", "Model A"]
    assert entry["verification_method"] == "table_cell_match" and entry["verified"] is True
    clean, errors = validate_result(product([entry]), library, workflow_mode="guided", cross_paper_requested=False)
    assert not errors and clean["evidence_index"][0] == entry
    assert clean["verification"]["server_quote_check"] == "not_requested"
    assert clean["verification"]["server_evidence_check"] == "passed"
    assert validate_packet(packet(evidence(quote="75%²")), library)[1]


@pytest.mark.parametrize("change", [
    {"quote": "75%"}, {"quote": "75%2"}, {"quote": "75"}, {"quote": "75%² "},
    {"headers": ["Accuracy (%)", "Model A"]}, {"headers": ["Model A", "Accuracy (%)", "Validation"]},
    {"headers": ["Validation", "Accuracy (%)", "Model B"]}, {"headers": []},
    {"table_id": "T2"}, {"section_id": "S1"}, {"row": 1}, {"column": 2}, {"page": 1}, {"source_id": "external"},
])
def test_table_forgery_and_spanned_nonorigin_cells_fail(tmp_path, change):
    library = library_fixture(tmp_path)
    item = evidence("table_cell", **change)
    assert validate_packet(packet(item), library)[1]
    clean, errors = validate_result(product([item]), library, workflow_mode="guided", cross_paper_requested=False)
    assert errors and not clean["evidence_index"][0]["verified"]


def test_visual_observation_is_host_resolved_and_never_an_original_quote(tmp_path):
    library = library_fixture(tmp_path)
    item = accepted_visual(library)
    original = copy.deepcopy(library.documents["main"].sections)
    entry = packet_evidence(packet(item), library)[0]
    assert entry["quote"] == "" and entry["evidence_type"] == "image_observation"
    assert entry["verification_method"] == "visual_observation" and entry["verified"]
    assert entry["conditions"] == "Validation split only" and entry["uncertainties"]
    supplied = {**entry, "verification_method": "quote_match", "observation": "Invented observation", "conditions": "Unlimited"}
    clean, errors = validate_result(product([supplied]), library, workflow_mode="guided", cross_paper_requested=False)
    assert not errors and clean["evidence_index"][0] == entry
    assert clean["verification"]["server_quote_check"] == "not_requested"
    assert library.documents["main"].sections == original


@pytest.mark.parametrize("change", ["missing_record", "missing_asset", "missing_ledger", "unreadable", "wrong_record_id",
    "figure", "section", "hash", "asset_hash", "asset_source", "asset_path", "wrong_claim", "changed_bytes",
    "ledger_hash", "ledger_other_path", "symlink", "ledger_symlink", "missing_conditions", "nonempty_quote", "external"])
def test_visual_requires_accepted_identity_prepared_asset_and_actual_read(tmp_path, change):
    library = library_fixture(tmp_path)
    item = accepted_visual(library)
    record = library.visual_observations["V1"]
    asset = library.prepared_visuals["F1"]
    image_path = tmp_path / asset["path"]
    ledger_path = tmp_path / "host-image-inspections.json"
    if change == "missing_record": library.visual_observations.clear()
    if change == "missing_asset": library.prepared_visuals.clear()
    if change == "missing_ledger": ledger_path.unlink()
    if change == "unreadable": record["readable"] = False
    if change == "wrong_record_id": record["id"] = "V2"
    if change == "figure": item["figure_id"] = "F2"
    if change == "section": item["section_id"] = "S1"
    if change == "hash": item["image_sha256"] = "1" * 64
    if change == "asset_hash": asset["sha256"] = "1" * 64
    if change == "asset_source": asset["source_id"] = "external"
    if change == "asset_path": asset["path"] = "../outside.png"
    if change == "wrong_claim": record["claim_ids"] = ["C2"]
    if change == "changed_bytes": image_path.write_bytes(b"changed image")
    if change == "ledger_hash": ledger_path.write_text(json.dumps({asset["path"]: "1" * 64}))
    if change == "ledger_other_path": ledger_path.write_text(json.dumps({"elsewhere.png": asset["sha256"]}))
    if change == "symlink":
        other = tmp_path / "other.png"
        image_path.rename(other)
        image_path.symlink_to(other)
    if change == "ledger_symlink":
        other = tmp_path / "other-ledger.json"
        ledger_path.rename(other)
        ledger_path.symlink_to(other)
    if change == "missing_conditions": record.pop("conditions")
    if change == "nonempty_quote": item["quote"] = TEXT
    if change == "external": item["source_id"] = "external"
    assert validate_packet(packet(item), library)[1]
    with pytest.raises(PacketError):
        packet_evidence(packet(item), library)


def test_visual_cannot_be_reused_for_an_unreviewed_claim(tmp_path):
    library = library_fixture(tmp_path)
    item = accepted_visual(library)
    assert validate_packet(packet(item, count=2), library)[1]
    library.visual_observations["V1"]["claim_ids"].append("C2")
    assert validate_packet(packet(item, count=2), library)[1] == []


def test_typed_checks_do_not_hide_missing_source_coverage_or_requested_external_work(tmp_path):
    library = library_fixture(tmp_path)
    item = accepted_visual(library)
    library.documents["main"].coverage.update(full_text=False, missing_text_pages=[2])
    library.limitations.append("Page 2 still lacks reliable text.")
    clean, errors = validate_result(product(packet_evidence(packet(item), library)), library,
                                    workflow_mode="guided", cross_paper_requested=True)
    assert any("incomplete reading" in error for error in errors)
    assert any("cross-paper" in error for error in errors)
    assert clean["evidence_index"][0]["verified"] and clean["verification"]["server_quote_check"] == "not_requested"
    assert clean["verification"]["coverage"]["missing_text_pages"] == [2]
    assert "Page 2 still lacks reliable text." in clean["verification"]["limitations"]


def test_native_gates_are_not_relaxed_and_guided_quote_typography_stays_strict(tmp_path):
    library = library_fixture(tmp_path)
    items = packet_evidence(packet(accepted_visual(library)), library)
    clean, errors = validate_result(product(items), library)
    assert any("Native skill" in error for error in errors)
    assert not clean["evidence_index"][0]["verified"] and "verification_method" not in clean["evidence_index"][0]
    forged = evidence(quote=TEXT.replace("75%²", "75%2"))
    clean, errors = validate_result(product([forged]), library, workflow_mode="guided")
    assert errors and not clean["evidence_index"][0]["verified"]
    assert clean["verification"]["server_quote_check"] == "partial"


def test_mixed_methods_and_citation_failures_are_reported_independently(tmp_path):
    library = library_fixture(tmp_path)
    visual = accepted_visual(library)
    visual["id"] = "E3"
    incoming = packet()
    incoming["claims"][0]["evidence"] += [evidence("table_cell", id="E2"), visual]
    items = packet_evidence(incoming, library)
    items[1]["headers"] = []
    clean, errors = validate_result(product(items), library, workflow_mode="guided", cross_paper_requested=False)
    assert errors and clean["verification"]["server_quote_check"] == "passed"
    assert clean["verification"]["verification_methods"]["table_cell_match"]["status"] == "partial"
    bad_citation = product(items)
    bad_citation["brief_markdown"] += " [E999]"
    clean, errors = validate_result(bad_citation, library, workflow_mode="guided")
    assert any("unresolved citation" in error for error in errors)
    assert clean["verification"]["server_quote_check"] == "partial"


def recovered_pages_fixture(root):
    library = library_fixture(root)
    library.rules_version = "source-structured-v3"
    main = library.documents["main"]
    main.coverage.update(full_text=False, missing_text_pages=[2, 3])
    metadata = prepare_image(library)
    ledger = {}
    for page in (2, 3):
        section_id = f"p{page}"
        main.sections.append({"id": section_id, "source_id": "main", "text": "", "page": page})
        main.coverage["figures"].append({"id": f"page:{page}", "section_id": section_id, "page": page, "images": [], "caption": "Page without reliable text"})
        asset = {**metadata, "figure_id": f"page:{page}", "section_id": section_id, "page": page}
        library.prepared_visuals[f"page:{page}"] = asset
        ledger[asset["path"]] = asset["sha256"]
        library.visual_observations[f"VP{page}"] = {
            "id": f"VP{page}", "figure_id": f"page:{page}", "section_id": section_id,
            "image_sha256": asset["sha256"], "observation": "The page contains only a readable section heading.",
            "readable": True, "conditions": "Original page rendering", "uncertainties": [], "claim_ids": []}
    (root / "host-image-inspections.json").write_text(json.dumps(ledger))
    return library, {"complete": True, "recovered_pages": [2, 3], "observation_ids": ["VP2", "VP3"]}


def test_visual_page_coverage_allows_no_claims_without_rewriting_original_source(tmp_path):
    library, recovery = recovered_pages_fixture(tmp_path)
    before = copy.deepcopy(library.documents["main"])
    assert validate_visual_source_coverage(library, recovery)
    result = product(packet_evidence(packet(), library))
    result["verification"] = {"source_visual_coverage": recovery}
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=False)
    assert not errors and clean["verification"]["source_visual_coverage"] == recovery
    assert clean["verification"]["coverage"]["full_text"] is False
    assert clean["verification"]["coverage"]["missing_text_pages"] == [2, 3]
    assert library.documents["main"] == before
    # A no-facts page can be covered but cannot supply a claim observation.
    item = evidence("image_observation", figure_id="page:2", section_id="p2", page=2,
                    observation_id="VP2", image_sha256=library.prepared_visuals["page:2"]["sha256"])
    assert validate_packet(packet(item), library)[1]


@pytest.mark.parametrize("change", ["incomplete", "truthy", "missing_page", "extra_page", "duplicate_page", "boolean_page",
    "missing_id", "duplicate_id", "other_id", "missing_record", "unreadable", "wrong_page", "wrong_section",
    "wrong_figure", "wrong_id", "wrong_hash", "changed_file", "missing_ledger", "wrong_ledger", "no_asset",
    "empty_observation", "legacy_rules", "empty_original_gaps", "original_duplicate_gaps", "unrecognized_fields"])
def test_visual_page_coverage_forgery_cannot_clear_original_text_gaps(tmp_path, change):
    library, recovery = recovered_pages_fixture(tmp_path)
    asset = library.prepared_visuals["page:2"]
    record = library.visual_observations["VP2"]
    if change == "incomplete": recovery["complete"] = False
    if change == "truthy": recovery["complete"] = 1
    if change == "missing_page": recovery["recovered_pages"] = [2]
    if change == "extra_page": recovery["recovered_pages"].append(4)
    if change == "duplicate_page": recovery["recovered_pages"] = [2, 2, 3]
    if change == "boolean_page": recovery["recovered_pages"] = [True, 3]
    if change == "missing_id": recovery["observation_ids"] = ["VP2"]
    if change == "duplicate_id": recovery["observation_ids"] = ["VP2", "VP2"]
    if change == "other_id": recovery["observation_ids"] = ["V2", "V3"]
    if change == "missing_record": library.visual_observations.pop("VP3")
    if change == "unreadable": record["readable"] = False
    if change == "wrong_page": asset["page"] = 3
    if change == "wrong_section": asset["section_id"] = "S1"
    if change == "wrong_figure": record["figure_id"] = "F1"
    if change == "wrong_id": record["id"] = "V2"
    if change == "wrong_hash": record["image_sha256"] = "1" * 64
    if change == "changed_file": (tmp_path / asset["path"]).write_bytes(b"changed")
    if change == "missing_ledger": (tmp_path / "host-image-inspections.json").unlink()
    if change == "wrong_ledger": (tmp_path / "host-image-inspections.json").write_text("{}")
    if change == "no_asset": library.prepared_visuals.pop("page:2")
    if change == "empty_observation": record["observation"] = ""
    if change == "legacy_rules": library.rules_version = "source-sections-v2"
    if change == "empty_original_gaps": library.documents["main"].coverage["missing_text_pages"] = []
    if change == "original_duplicate_gaps": library.documents["main"].coverage["missing_text_pages"] = [2, 2, 3]
    if change == "unrecognized_fields": recovery["verified"] = True
    assert validate_visual_source_coverage(library, recovery) is False
    result = product(packet_evidence(packet(), library))
    result["verification"] = {"source_visual_coverage": recovery}
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=False)
    assert any("incomplete reading" in error for error in errors)
    assert "source_visual_coverage" not in clean["verification"]
    assert clean["verification"]["coverage"]["full_text"] is False


def test_native_source_gap_and_review_gates_still_apply_after_real_visual_recovery(tmp_path):
    library, recovery = recovered_pages_fixture(tmp_path)
    result = product(packet_evidence(packet(), library))
    result["verification"] = {"source_visual_coverage": recovery, "product_review": {"passed": False}}
    clean, errors = validate_result(result, library)
    assert any("incomplete reading" in error for error in errors)
    assert any("Native skill" in error for error in errors)
    assert "source_visual_coverage" not in clean["verification"]
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=True)
    assert any("review" in error for error in errors) and any("cross-paper" in error for error in errors)
    assert not any("incomplete reading" in error for error in errors)


def test_all_image_pdf_can_publish_only_with_complete_verified_visual_coverage(tmp_path):
    library, recovery = recovered_pages_fixture(tmp_path)
    main = library.documents["main"]
    main.sections = [section for section in main.sections if section.get("page") in (2, 3)]
    library.visual_observations["VP2"]["claim_ids"] = ["C1"]
    item = evidence("image_observation", figure_id="page:2", section_id="p2", page=2,
                    observation_id="VP2", image_sha256=library.prepared_visuals["page:2"]["sha256"])
    result = product(packet_evidence(packet(item), library))
    with pytest.raises(PipelineError, match="usable source text"):
        validate_result(result, library, workflow_mode="guided")
    result["verification"] = {"source_visual_coverage": recovery}
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=False)
    assert not errors and clean["verification"]["server_quote_check"] == "not_requested"
    assert clean["verification"]["coverage"]["full_text"] is False
    with pytest.raises(PipelineError, match="usable source text"):
        validate_result(result, library)


def test_pipeline_forwards_frozen_versions_and_preserves_native_defaults(tmp_path):
    guided = pipeline_fixture(tmp_path / "guided")
    assert guided.library.workflow_mode == "guided" and guided.library.rules_version == "source-structured-v3"
    with guided.state_lock:
        with guided.state_lock:
            assert guided.tool_calls == 0
    project = Path(__file__).resolve().parents[2]
    native = Pipeline(tmp_path / "native", project / "skills/ljg-paper", {"id": "native"}, "ljg-paper",
                      "team", threading.Event(), project)
    assert native.library.workflow_mode == "native" and native.library.rules_version == "source-sections-v2"
    with pytest.raises(PipelineError, match="Unknown"):
        native.call("read_visual", {"figure_id": "F1"})


def test_pipeline_read_visual_returns_actual_payload_identity_and_single_budget_charge(tmp_path, monkeypatch):
    pipeline = pipeline_fixture(tmp_path)
    library = library_fixture(tmp_path)
    pipeline.library.documents = library.documents
    metadata = prepare_image(pipeline.library)
    calls = []
    def prepare(figure_id):
        calls.append(figure_id)
        return copy.deepcopy(metadata)
    monkeypatch.setattr(pipeline.library, "prepare_visual", prepare)
    returned = pipeline.call("read_visual", {"figure_id": "F1"})
    assert calls == ["F1"] and pipeline.tool_calls == 1
    assert returned["type"] == "image" and returned["figure_id"] == "F1" and returned["section_id"] == "F1"
    assert returned["source_id"] == "main" and returned["caption"] == metadata["caption"]
    assert base64.b64decode(returned["data"]) == (tmp_path / metadata["path"]).read_bytes()
    assert returned["sha256"] == pipeline.inspected_images[metadata["path"]]
    assert json.loads((tmp_path / "host-image-inspections.json").read_text()) == pipeline.inspected_images
    assert not pipeline.library.visual_observations  # Reading alone is not accepted review.
    with pytest.raises(PipelineError, match="Unknown"):
        pipeline.call("accept_visual_observation", {"readable": True})
    with pytest.raises(PipelineError, match="owned"):
        pipeline.write_artifact("host-image-inspections.json", "{}")


@pytest.mark.parametrize("change", ["no_vision", "changed_bytes", "missing_prepared", "identity", "page", "text", "cancelled"])
def test_pipeline_visual_rejects_missing_capability_identity_or_changed_image(tmp_path, monkeypatch, change):
    pipeline = pipeline_fixture(tmp_path, vision=change != "no_vision")
    pipeline.library.documents = library_fixture(tmp_path).documents
    metadata = prepare_image(pipeline.library)
    def prepare(figure_id):
        if change == "no_vision": pytest.fail("No image should be fetched without vision capability")
        return copy.deepcopy(metadata)
    monkeypatch.setattr(pipeline.library, "prepare_visual", prepare)
    if change == "changed_bytes": (tmp_path / metadata["path"]).write_bytes(b"different image")
    if change == "missing_prepared": pipeline.library.prepared_visuals.clear()
    if change == "identity": metadata["source_id"] = "external"
    if change == "page": metadata["page"] = 2
    if change == "text":
        metadata.update(path="source.txt", sha256=hashlib.sha256((tmp_path / "source.txt").read_bytes()).hexdigest(), mime_type="text/plain")
    if change == "cancelled": pipeline.cancelled.set()
    with pytest.raises((PipelineError, TaskCancelled)):
        pipeline.call("read_visual", {"figure_id": "F1"})


def test_parallel_rpc_budget_is_shared_and_serializes_host_state(tmp_path, monkeypatch):
    pipeline = pipeline_fixture(tmp_path, max_tool_calls=10)
    seen = []
    def read_source(**args):
        seen.append(pipeline.tool_calls)
        return {"text": "bounded source text"}
    monkeypatch.setattr(pipeline.library, "read_source", read_source)
    def call(_):
        try:
            pipeline.call("read_source", {})
            return "ok"
        except BudgetExhausted:
            return "exhausted"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(16)))
    assert results.count("ok") == 10 and results.count("exhausted") == 6
    assert pipeline.tool_calls == 16 and seen == list(range(1, 11))


@pytest.mark.parametrize("path", ["source-assets", "source-assets/future.txt", "source-assets/nested/future.json",
                                  "native/source-assets/future.txt"])
def test_model_cannot_write_future_source_asset_paths(tmp_path, path):
    pipeline = pipeline_fixture(tmp_path)
    assert not pipeline.sealed and not pipeline.library.visual_assets
    with pytest.raises(PipelineError, match="owned"):
        pipeline.write_artifact(path, "{}")
    assert not (tmp_path / path).exists()


def test_original_svg_is_reserved_and_cannot_be_written_or_patched(tmp_path):
    pipeline = pipeline_fixture(tmp_path)
    path = tmp_path / "source-assets/original.svg"
    path.parent.mkdir()
    original = '<svg xmlns="http://www.w3.org/2000/svg"><text>Original measurement</text></svg>'
    path.write_text(original)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pipeline.library.visual_assets["source-assets/original.svg"] = {
        "path": "source-assets/original.svg", "format": "svg", "sha256": digest}
    assert "source-assets/original.svg" in pipeline._reserved_paths()
    assert not pipeline.sealed
    with pytest.raises(PipelineError, match="owned"):
        pipeline.write_artifact("source-assets/original.svg", "changed")
    with pytest.raises(PipelineError, match="owned"):
        pipeline.patch_artifact("source-assets/original.svg", digest,
                                [{"old_text": "Original measurement", "new_text": "Invented measurement"}])
    assert path.read_text() == original


@pytest.mark.parametrize("path", ["source-assets/future.json", "native/source-assets/future.json"])
def test_native_output_arguments_cannot_target_source_assets(tmp_path, monkeypatch, path):
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/deeppapernote", {"id": "asset-guard"}, "deeppapernote",
                        "team", threading.Event(), project)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("Reject the output path before script execution"))
    with pytest.raises(PipelineError, match="host-owned"):
        pipeline.run_skill_step("resolve_paper.py", ["--input", "source.pdf", "--zotero-mode", "off", "--output", path])
    assert not (tmp_path / path).exists()


def test_native_script_guard_protects_source_asset_subtree_without_enumeration(tmp_path):
    work = tmp_path / "work"
    skill = tmp_path / "skill"
    (work / "source-assets/nested").mkdir(parents=True)
    skill.mkdir()
    original = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    (work / "source-assets/original.svg").write_bytes(original)
    (work / "replacement.txt").write_text("replacement")
    script = skill / "asset_guard_probe.py"
    script.write_text("""from pathlib import Path
import os

asset = Path('source-assets/original.svg')
assert asset.read_bytes().startswith(b'<svg')
operations = [
    lambda: asset.write_text('changed'),
    lambda: Path('source-assets/future.txt').write_text('future'),
    lambda: Path('source-assets/nested/future.json').write_text('{}'),
    lambda: Path('source-assets/future-dir').mkdir(),
    lambda: os.replace('replacement.txt', asset),
    lambda: os.rename('source-assets', 'moved-assets'),
    lambda: asset.unlink(),
]
for operation in operations:
    try:
        operation()
    except PermissionError:
        continue
    raise AssertionError('Source asset mutation was allowed')
Path('allowed.txt').write_text('ordinary native output still works')
print('All source asset mutations blocked')
""")
    # An empty enumerated list proves protection applies to the whole subtree.
    result = subprocess.run([sys.executable, "-c", PYTHON_FILE_GUARD, str(work), str(skill), str(script), "[]"],
                            cwd=work, capture_output=True, text=True, timeout=10,
                            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert "All source asset mutations blocked" in result.stdout
    assert (work / "source-assets/original.svg").read_bytes() == original
    assert sorted(path.name for path in (work / "source-assets").iterdir()) == ["nested", "original.svg"]
    assert not list((work / "source-assets/nested").iterdir())
    assert (work / "allowed.txt").read_text() == "ordinary native output still works"
