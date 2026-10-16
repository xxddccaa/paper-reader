"""Evidence, exact patches, source recovery and native host review boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import threading

import pytest

from app.evidence import PacketError, apply_claim_patch, packet_evidence, validate_packet
from app.pipeline import Pipeline, PipelineError, READER_FIELDS, validate_result
from app.source_tools import SourceDocument, SourceError, SourceLibrary


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def library_fixture(root):
    library = SourceLibrary(root, {"id": "paper", "title": "Measured adaptation"}, "ljg-paper")
    text = "The method improves accuracy from 70 percent to 75 percent on the validation split."
    path = root / "source.txt"
    path.write_text(text)
    library.documents["main"] = SourceDocument("main", "Measured adaptation", "https://example.org/paper", "source.txt",
        [{"id": "p1", "source_id": "main", "page": 1, "section": "Results", "text": text}],
        {"full_text": True}, hashlib.sha256(path.read_bytes()).hexdigest())
    library._save()
    return library


def packet_fixture():
    return {"version": 1, "paper_title": "Measured adaptation", "research_question": "Can the method improve validation accuracy?",
            "claims": [{"id": "C1", "kind": "result", "basis": "observation", "statement": "The method improves accuracy from 70% to 75%.",
                        "conditions": {"model": "", "dataset": "", "split": "validation", "metric": "accuracy", "baseline": "", "budget": ""},
                        "evidence": [{"id": "E1", "source_id": "main", "section_id": "p1", "page": 1,
                                      "quote": "improves accuracy from 70 percent to 75 percent"}],
                        "numeric_comparison": {"value": 75, "baseline_value": 70, "relation": "higher", "unit": "percent"}}],
            "open_questions": [], "limitations": ["The model and training budget are not specified in this excerpt."]}


def pipeline_fixture(root):
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(root, project / "skills/ljg-paper", {"id": "paper", "title": "Measured adaptation"}, "ljg-paper", "team", threading.Event(), project)
    pipeline.library = library_fixture(root)
    return pipeline


def native_fixture(root):
    pipeline = pipeline_fixture(root)
    pipeline.write_artifact("native/note.md", "A note explaining the measured adaptation and its limits.")
    pipeline.write_artifact("native/map.md", "# Research map\n\n## Method\nA specific mechanism.\n\n## 独立阅读检查记录\n\nPending actual independent review.\n\n## Sources\nThe main paper.\n")
    quality = {"passed": True, "issues": [], "checks": {"fixture": True}}
    readability = {"passed": True, "issues": [], "checks": {"fixture": True},
                   "reader_record": {key: "Actual reader answer for " + key for key in READER_FIELDS}}
    return pipeline, quality, readability


def attest(pipeline, quality, readability, policy="model-A/rules-2"):
    return pipeline.host_call("host_native_attest", {"note_path": "native/note.md", "map_path": "native/map.md",
        "note_sha256": hashlib.sha256((pipeline.work_dir / "native/note.md").read_bytes()).hexdigest(),
        "map_sha256": hashlib.sha256((pipeline.work_dir / "native/map.md").read_bytes()).hexdigest(),
        "policy_key": policy, "quality": quality, "readability": readability})


def lookup(pipeline, policy="model-A/rules-2"):
    return pipeline.host_call("host_native_review_lookup", {"note_path": "native/note.md", "map_path": "native/map.md", "policy_key": policy})


def test_packet_exact_evidence_is_host_resolved_without_fabricated_conditions(tmp_path):
    library = library_fixture(tmp_path)
    packet = packet_fixture()
    second = copy.deepcopy(packet["claims"][0]); second.update(id="C2", kind="lesson", basis="inference")
    packet["claims"].append(second)
    clean, errors = validate_packet(packet, library)
    assert errors == [] and clean == packet
    assert clean["claims"][0]["conditions"]["baseline"] == ""
    evidence = packet_evidence(clean, library)
    assert len(evidence) == 1
    assert evidence[0]["verified"] is True and evidence[0]["source_title"] == "Measured adaptation"
    assert evidence[0]["section_id"] == "p1" and evidence[0]["page"] == 1
    assert clean["claims"][1]["basis"] == "inference"  # Quote matching does not upgrade an inference.


def test_packet_search_queries_fit_actual_source_tool_limit(tmp_path):
    library = library_fixture(tmp_path); packet = packet_fixture()
    packet["open_questions"] = [{"question": "Is this comparison robust?", "reason": "Check an explicit counterexample", "search_query": "x" * 500}]
    assert validate_packet(packet, library)[1] == []
    packet["open_questions"][0]["search_query"] += "x"
    assert validate_packet(packet, library)[1]


@pytest.mark.parametrize("change", ["source", "section", "page", "quote", "verified", "relation", "duplicate", "nonfinite", "bool_numeric", "empty_evidence"])
def test_packet_rejects_wrong_identity_quotes_arithmetic_and_schema(tmp_path, change):
    library = library_fixture(tmp_path); packet = packet_fixture()
    claim = packet["claims"][0]; evidence = claim["evidence"][0]
    if change == "source": evidence["source_id"] = "unread"
    if change == "section": evidence["section_id"] = "p2"
    if change == "page": evidence["page"] = 2
    if change == "quote": evidence["quote"] = "The method reaches 99 percent accuracy."
    if change == "verified": evidence["verified"] = True
    if change == "relation": claim["numeric_comparison"]["relation"] = "lower"
    if change == "duplicate": packet["claims"].append(copy.deepcopy(claim))
    if change == "nonfinite": claim["numeric_comparison"]["value"] = float("nan")
    if change == "bool_numeric": claim["numeric_comparison"]["value"] = True
    if change == "empty_evidence": claim["evidence"] = []
    assert validate_packet(packet, library)[1]
    with pytest.raises(PacketError): packet_evidence(packet, library)


def test_shared_evidence_requires_identical_content_and_packet_patch_is_selective(tmp_path):
    packet = packet_fixture()
    second = copy.deepcopy(packet["claims"][0]); second["id"] = "C2"
    packet["claims"].append(second)
    replacement = copy.deepcopy(packet["claims"][0]); replacement["statement"] = "A more carefully limited interpretation."
    updated = apply_claim_patch(packet, {"claims": [replacement], "remove_claim_ids": []})
    assert updated["claims"][1] == packet["claims"][1]
    assert updated["limitations"] == packet["limitations"] and packet["claims"][0] != replacement
    updated["claims"][1]["evidence"][0]["page"] = 2
    assert validate_packet(updated, library_fixture(tmp_path))[1]
    for patch in ({"claims": [], "remove_claim_ids": []}, {"claims": [], "remove_claim_ids": ["C3"]},
                  {"claims": [replacement, replacement], "remove_claim_ids": []},
                  {"claims": [replacement], "remove_claim_ids": ["C1"]},
                  {"claims": [], "remove_claim_ids": ["C1", "C2"]}):
        with pytest.raises(PacketError): apply_claim_patch(packet, patch)


def test_guided_verification_skips_only_native_checks_and_preserves_evidence_gates(tmp_path):
    library = library_fixture(tmp_path)
    result = {"brief_markdown": "Measured result [E1]", "full_markdown": "Measured result and limits [E1]",
              "evidence_index": packet_evidence(packet_fixture(), library), "workflow_mode": "guided"}
    assert any("Native" in error for error in validate_result(result, library)[1])
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=False)
    assert errors == [] and clean["verification"]["cross_paper_status"] == "not_requested"
    assert validate_result(result, library, workflow_mode="guided", cross_paper_requested=True)[1]
    result["evidence_index"][0]["section_id"] = "wrong"
    assert validate_result(result, library, workflow_mode="guided", cross_paper_requested=False)[1]
    result["evidence_index"] = packet_evidence(packet_fixture(), library)
    result["verification"] = {"product_review": {"passed": False}}
    assert any("review" in error for error in validate_result(result, library, workflow_mode="guided")[1])


def test_source_checkpoint_checks_files_payload_identity_and_replaces_state(tmp_path):
    library = library_fixture(tmp_path)
    checkpoint = library.export_checkpoint(); digest = canonical_hash(checkpoint)
    original = library.fingerprint()
    library.documents["unused"] = copy.deepcopy(library.documents["main"])
    library.restore_checkpoint(checkpoint, digest)
    assert set(library.documents) == {"main"} and library.fingerprint() == original
    library.limitations.append("Search metadata does not alter source facts")
    assert library.fingerprint() == original
    wrong = copy.deepcopy(checkpoint); wrong["documents"][0]["sections"][0]["text"] = "Changed checkpoint text"
    with pytest.raises(SourceError, match="hash"): library.restore_checkpoint(wrong, digest)
    wrong = copy.deepcopy(checkpoint); wrong["paper_id"] = "another-paper"
    with pytest.raises(SourceError, match="different"): library.restore_checkpoint(wrong)
    (tmp_path / "source.txt").write_text("tampered source")
    assert library.fingerprint() != original
    with pytest.raises(SourceError, match="integrity"): library.restore_checkpoint(checkpoint, digest)


@pytest.mark.parametrize("fail_second", [False, True])
def test_search_records_bind_query_candidates_and_preserve_failures_on_restore(tmp_path, monkeypatch, fail_second):
    import app.source_tools as source_module
    library = library_fixture(tmp_path)

    def download(url, **kwargs):
        if "export.arxiv.org" in url:
            data = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2106.10199</id><title>BitFit</title><summary>Candidate evidence</summary></entry></feed>'
            return source_module.Download(data, url, "application/xml")
        if fail_second:
            raise SourceError("Explicit fixture provider unavailable")
        return source_module.Download(b'{"data":[]}', url, "application/json")

    monkeypatch.setattr(source_module, "download_public", download)
    query = "bitfit adaptation comparison"
    library.search_papers(query)
    record = library.search_records[query]
    assert record["candidate_ids"] == ["arxiv:2106.10199"]
    assert record["completed"] is (not fail_second)
    assert bool(record["errors"]) is fail_second
    checkpoint = library.export_checkpoint()
    library.search_records = {}
    library.restore_checkpoint(checkpoint, canonical_hash(checkpoint))
    assert library.search_records[query] == record
    resumed = SourceLibrary(tmp_path, library.paper, library.skill_id)
    assert resumed.search_records[query] == record
    tampered = copy.deepcopy(checkpoint)
    tampered["search_records"][query]["candidate_ids"] = ["arxiv:9999.99999"]
    with pytest.raises(SourceError, match="candidates"):
        library.restore_checkpoint(tampered)
    if fail_second:
        tampered = copy.deepcopy(checkpoint); tampered["search_records"][query]["completed"] = True
        with pytest.raises(SourceError, match="outcome"):
            library.restore_checkpoint(tampered)


def test_search_budget_exhaustion_is_recorded_as_failed_attempt(tmp_path):
    library = library_fixture(tmp_path); library.searches = 12
    with pytest.raises(SourceError, match="budget"):
        library.search_papers("explicit unanswered comparison")
    record = library.search_records["explicit unanswered comparison"]
    assert record["completed"] is False and record["candidate_ids"] == [] and record["errors"]
    checkpoint = library.export_checkpoint(); library.restore_checkpoint(checkpoint, canonical_hash(checkpoint))
    assert library.searches == 13


@pytest.mark.parametrize("candidate", ["arxiv:2106.10199", "arxiv:2106.10199v1", "arxiv:2106.10199v9"])
def test_related_fetch_rejects_main_arxiv_across_versions_before_download(tmp_path, monkeypatch, candidate):
    import app.source_tools as source_module
    library = library_fixture(tmp_path)
    library.paper["source_url"] = "https://arxiv.org/abs/2106.10199v2"
    library.documents["main"].url = "https://arxiv.org/html/2106.10199v2"
    monkeypatch.setattr(source_module, "download_public", lambda *a, **k: pytest.fail("Self-source must be rejected before network access"))
    with pytest.raises(SourceError, match="main paper"):
        library.fetch_related(candidate)
    library.candidates["s2:alias"] = {"id": "s2:alias", "url": "https://arxiv.org/pdf/2106.10199v7.pdf"}
    with pytest.raises(SourceError, match="main paper"):
        library.fetch_related("s2:alias")
    assert set(library.documents) == {"main"}


def test_related_identical_pdf_is_rejected_before_source_registration(tmp_path, monkeypatch):
    import fitz
    import app.source_tools as source_module
    library = library_fixture(tmp_path)
    pdf = fitz.open(); page = pdf.new_page(); page.insert_text((50, 50), "The original main paper.")
    path = tmp_path / "source.pdf"; pdf.save(path); pdf.close()
    library.documents["main"].path = "source.pdf"
    library.documents["main"].sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    library.candidates["s2:reupload"] = {"id": "s2:reupload", "title": "A renamed upload", "url": "https://example.org/reupload", "pdf_url": "https://example.org/reupload.pdf"}
    monkeypatch.setattr(source_module, "download_public", lambda url, **kwargs: source_module.Download(path.read_bytes(), url, "application/pdf"))
    monkeypatch.setattr(source_module, "parse_document", lambda *a, **k: pytest.fail("Duplicate bytes must be rejected before parsing"))
    result = library.fetch_related("s2:reupload")
    assert result["coverage"]["full_text"] is False and "identical copy" in result["limitation"]
    assert set(library.documents) == {"main"}
    duplicate = copy.deepcopy(library.documents["main"]); duplicate.id = "related:workspace:other"; duplicate.kind = "external"; duplicate.url = "https://example.org/reupload"
    snapshot = library.export_checkpoint(); snapshot["documents"].append(vars(duplicate))
    with pytest.raises(SourceError, match="main paper as external"):
        library.restore_checkpoint(snapshot, canonical_hash(snapshot))


def test_main_sources_are_excluded_from_public_and_workspace_search_without_db(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import app.db as database
    import app.source_tools as source_module
    pipeline = pipeline_fixture(tmp_path)
    pipeline.library.paper["source_url"] = "https://arxiv.org/abs/2106.10199v2"
    def download(url, **kwargs):
        data = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2106.10199v3</id><title>Main</title></entry><entry><id>https://arxiv.org/abs/2203.05482</id><title>Other research</title></entry></feed>' if "export.arxiv.org" in url else b'{"data":[]}'
        return source_module.Download(data, url, "application/json")
    monkeypatch.setattr(source_module, "download_public", download)
    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scalars(self, statement):
            assert "papers.id !=" in str(statement)
            rows = [SimpleNamespace(id="paper", source_url=None, sha256=None, title="Main", metadata_json={}),
                    SimpleNamespace(id="copy", source_url=None, sha256=pipeline.library.documents["main"].sha256, title="Copy", metadata_json={}),
                    SimpleNamespace(id="other", source_url="https://example.org/different", sha256="other-hash", title="Other", metadata_json={})]
            return SimpleNamespace(all=lambda: rows)
    monkeypatch.setattr(database, "SessionLocal", FakeSession)
    result = pipeline.search_papers("comparison")
    assert {candidate["id"] for candidate in result["candidates"]} == {"arxiv:2203.05482", "workspace:other"}
    assert set(pipeline.library.search_records["comparison"]["candidate_ids"]) == {"arxiv:2203.05482", "workspace:other"}
    monkeypatch.setattr(database, "SessionLocal", lambda: pytest.fail("Main workspace ID must be rejected before DB access"))
    with pytest.raises(PipelineError, match="main paper"):
        pipeline.fetch_related("workspace:paper")


def test_duplicate_main_source_cannot_complete_final_cross_paper_verification(tmp_path):
    library = library_fixture(tmp_path)
    packet = packet_fixture(); packet["claims"][0]["kind"] = "related_work"
    result = {"brief_markdown": "Attributed main-paper comparison [E1]", "full_markdown": "Attributed comparison [E1]", "evidence_index": packet_evidence(packet, library)}
    assert validate_result(result, library, workflow_mode="guided", cross_paper_requested=False)[1] == []
    duplicate = copy.deepcopy(library.documents["main"]); duplicate.id = "related:copy"; duplicate.kind = "external"
    library.documents[duplicate.id] = duplicate
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=True)
    assert clean["verification"]["cross_paper_status"] == "partial"
    assert any("main paper" in error for error in errors)


def test_patch_artifact_cas_atomic_json_and_frozen_nonoverlapping_edits(tmp_path):
    pipeline = pipeline_fixture(tmp_path)
    pipeline.write_artifact("native/plan.json", '{"a":"first","b":"second"}')
    before = pipeline.read_artifact("native/plan.json", limit=5)
    assert before["sha256"] == hashlib.sha256((tmp_path / "native/plan.json").read_bytes()).hexdigest()
    result = pipeline.patch_artifact("native/plan.json", before["sha256"], [{"old_text": "first", "new_text": "second"}, {"old_text": "second", "new_text": "third"}])
    assert json.loads((tmp_path / "native/plan.json").read_text()) == {"a": "second", "b": "third"}
    saved = (tmp_path / "native/plan.json").read_bytes()
    with pytest.raises(PipelineError, match="hash"):
        pipeline.patch_artifact("native/plan.json", before["sha256"], [{"old_text": "third", "new_text": "new"}])
    with pytest.raises(PipelineError, match="valid"):
        pipeline.patch_artifact("native/plan.json", result["sha256"], [{"old_text": '"third"', "new_text": '"unterminated'}])
    with pytest.raises(PipelineError, match="overlap"):
        pipeline.patch_artifact("native/plan.json", result["sha256"], [{"old_text": "second", "new_text": "x"}, {"old_text": '"second"', "new_text": '"y"'}])
    assert (tmp_path / "native/plan.json").read_bytes() == saved
    with pytest.raises(PipelineError, match="valid JSON"):
        pipeline.patch_artifact("native/plan.json", result["sha256"], [{"old_text": '"third"', "new_text": "NaN"}])
    assert (tmp_path / "native/plan.json").read_bytes() == saved


def test_patch_rejects_overlapping_occurrences_of_one_needle(tmp_path):
    pipeline = pipeline_fixture(tmp_path)
    written = pipeline.write_artifact("native/repeated.txt", "aaa")
    with pytest.raises(PipelineError, match="exactly once"):
        pipeline.patch_artifact("native/repeated.txt", written["sha256"], [{"old_text": "aa", "new_text": "x"}])
    assert (tmp_path / "native/repeated.txt").read_text() == "aaa"


@pytest.mark.parametrize("name", ["host-workflow/checkpoint.json", "host-workflow-state.json", "host-guided-review.json", "native/host-workflow/private.json", "reading-packet.json", "source.txt", "native/quality_review.json"])
def test_model_write_and_patch_cannot_change_host_workflow_or_source_files(tmp_path, name):
    pipeline = pipeline_fixture(tmp_path)
    path = tmp_path / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('{"host":true}')
    with pytest.raises(PipelineError, match="owned"): pipeline.write_artifact(name, '{}')
    with pytest.raises(PipelineError, match="owned"): pipeline.patch_artifact(name, hashlib.sha256(path.read_bytes()).hexdigest(), [{"old_text": "true", "new_text": "false"}])
    assert path.read_text() == '{"host":true}'


def test_native_real_reader_records_cache_only_bound_reviews_and_protect_host_section(tmp_path):
    pipeline, quality, readability = native_fixture(tmp_path)
    assert "quality" not in lookup(pipeline)
    response = attest(pipeline, quality, readability)
    assert response["reader_record_written"] is True and response["passed"] is True
    mapping = (tmp_path / "native/map.md").read_text()
    assert "Actual reader answer for mechanism" in mapping and "NARRATIVE_PASS" in mapping
    assert mapping.endswith("## Sources\nThe main paper.\n")
    assert lookup(pipeline)["quality"] == quality and lookup(pipeline)["readability"] == readability
    attest(pipeline, quality, readability)
    assert (tmp_path / "native/map.md").read_text() == mapping
    with pytest.raises(PipelineError, match="reader record"):
        pipeline.write_artifact("native/map.md", mapping.replace("Actual reader answer for narrative", "Forged reader answer"))
    pipeline.patch_artifact("native/map.md", response["map_sha256"], [{"old_text": "A specific mechanism.", "new_text": "A corrected specific mechanism."}])
    cached = lookup(pipeline)
    assert "quality" not in cached and cached["readability"] == readability
    assert "readability" not in lookup(pipeline, "model-B/rules-2")
    pipeline.write_artifact("native/note.md", "Changed factual or explanatory note body.")
    assert "quality" not in lookup(pipeline) and "readability" not in lookup(pipeline)


def test_native_cache_invalidates_changed_source_and_untrusted_reader_section(tmp_path):
    pipeline, quality, readability = native_fixture(tmp_path); attest(pipeline, quality, readability)
    source = tmp_path / "source.txt"; original = source.read_text(); source.write_text(original + " New evidence.")
    assert "quality" not in lookup(pipeline) and "readability" not in lookup(pipeline)
    source.write_text(original)
    mapping = tmp_path / "native/map.md"; mapping.write_text(mapping.read_text().replace("Actual reader answer for narrative", "Tampered external record"))
    assert "quality" not in lookup(pipeline)
    with pytest.raises(PipelineError, match="reader record"):
        pipeline.host_call("host_native_seal", {})


def test_native_failed_or_incomplete_reviews_never_write_pass_or_cache_success(tmp_path):
    pipeline, quality, readability = native_fixture(tmp_path)
    readability["passed"] = False; readability["issues"] = ["Reader cannot reconstruct the mechanism"]
    result = attest(pipeline, quality, readability)
    assert not result["passed"] and not result["reader_record_written"]
    assert "PASS" not in (tmp_path / "native/map.md").read_text()
    assert lookup(pipeline)["quality"] == quality and "readability" not in lookup(pipeline)
    readability.update(passed=True, issues=[]); del readability["reader_record"]["mechanism"]
    with pytest.raises(PipelineError, match="actual structured"):
        attest(pipeline, quality, readability)
    assert "PASS" not in (tmp_path / "native/map.md").read_text()


def test_native_host_record_changes_require_real_validator_on_final_exact_pair(tmp_path):
    pipeline, quality, readability = native_fixture(tmp_path)
    old_hashes = {name: pipeline._fingerprint(tmp_path / name) for name in ("native/note.md", "native/map.md")}
    # Explicit host ledger fixture isolates stale-input fencing, not model quality.
    pipeline.native_checks = [{"script": "validate_note.ts", "exit_code": 0, "semantic_pass": True,
        "args": ["native/note.md", "--map", "native/map.md"], "input_hashes": old_hashes, "output_hashes": {}}]
    (tmp_path / "native-execution-ledger.json").write_text(json.dumps(pipeline.native_checks))
    attest(pipeline, quality, readability)
    with pytest.raises(PipelineError, match="changed after its check"):
        pipeline.host_call("host_native_seal", {})
    result = pipeline.run_skill_step("validate_note.ts", ["native/note.md", "--map", "native/map.md"])
    assert result["semantic_pass"] is False  # Real pinned validator rejects this deliberately incomplete note/map.
    with pytest.raises(PipelineError, match="actual checks"):
        pipeline.host_call("host_native_seal", {})


def test_native_python_script_guard_protects_new_and_existing_host_paths(tmp_path):
    import subprocess
    import sys
    from app.pipeline import PYTHON_FILE_GUARD, _sandbox
    work = tmp_path / "work"; work.mkdir()
    skill = tmp_path / "skill"; skill.mkdir()
    (work / "native").mkdir()
    (work / "host-workflow").mkdir()
    original = work / "host-workflow/checkpoint.json"; original.write_text('{"trusted":true}')
    script = skill / "fixture-probe.py"
    script.write_text("""import os
for path in ['host-workflow/checkpoint.json', 'host-guided-new.json', 'reading-packet.json', 'native/host-private.json']:
    try:
        open(path, 'w').write('{}')
        raise RuntimeError('host file write escaped')
    except PermissionError: pass
try:
    os.rename('host-workflow/checkpoint.json', 'stolen.json')
    raise RuntimeError('host file rename escaped')
except PermissionError: pass
print('host boundaries held')
""")
    result = subprocess.run([sys.executable, "-c", PYTHON_FILE_GUARD, str(work), str(skill), str(script), "[]"],
        cwd=work, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
        timeout=10, preexec_fn=lambda: _sandbox(str(work), str(skill)))
    assert result.returncode == 0, result.stderr
    assert "host boundaries held" in result.stdout and original.read_text() == '{"trusted":true}'


def test_native_cache_recovers_and_seal_keeps_reader_record_immutable(tmp_path):
    pipeline, quality, readability = native_fixture(tmp_path); attest(pipeline, quality, readability)
    resumed = Pipeline(tmp_path, pipeline.skill_root, pipeline.library.paper, pipeline.skill_id, "team", threading.Event(), pipeline.project_dir)
    assert lookup(resumed)["quality"] == quality and lookup(resumed)["readability"] == readability
    # Explicit ledger fixture: source_security separately executes the real validator.
    resumed.native_checks = [{"script": "validate_note.ts", "exit_code": 0, "semantic_pass": True,
        "args": ["native/note.md", "--map", "native/map.md"],
        "input_hashes": {name: resumed._fingerprint(tmp_path / name) for name in ("native/note.md", "native/map.md")}, "output_hashes": {}}]
    (tmp_path / "native-execution-ledger.json").write_text(json.dumps(resumed.native_checks))
    assert resumed.host_call("host_native_seal", {})["sealed"]
    old = resumed.read_artifact("native/note.md")
    with pytest.raises(PipelineError, match="sealed"):
        resumed.patch_artifact("native/note.md", old["sha256"], [{"old_text": "measured", "new_text": "changed"}])
    resumed.write_artifact("product/brief.md", "Presentation before")
    brief = resumed.read_artifact("product/brief.md")
    assert resumed.patch_artifact("product/brief.md", brief["sha256"], [{"old_text": "before", "new_text": "after"}])["sha256"] != brief["sha256"]
    (tmp_path / "host-native-reader-record.json").unlink()
    with pytest.raises(PipelineError): resumed.verify_native_seal()
