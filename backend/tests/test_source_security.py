"""Boundary tests: real parsers/native scripts; remote I/O replaced at transport."""
from pathlib import Path
import json
import socket
import subprocess
import threading

import fitz
import pytest

from app.pipeline import BudgetExhausted, Pipeline, PipelineError, task_path, validate_result
from app.source_tools import (SourceDocument, SourceError, SourceLibrary, normalize_arxiv,
                              parse_html, validate_public_url)


def test_arxiv_normalization_rejects_nonpaper_inputs():
    assert normalize_arxiv("https://arxiv.org/pdf/2106.10199v1.pdf") == "2106.10199v1"
    assert normalize_arxiv("arxiv:cs/9901001") == "cs/9901001"
    for value in ["https://attacker.test/abs/2106.10199", "../../etc/passwd", "2106.10199/extra", "http://localhost/a"]:
        with pytest.raises(SourceError):
            normalize_arxiv(value)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_ssrf_rejects_private_and_mapped_addresses(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(SourceError, match="Private"):
        validate_public_url("https://papers.example/source.pdf")


def test_ssrf_rejects_mixed_dns_answers_and_credentials(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ["8.8.8.8", "127.0.0.1"]])
    with pytest.raises(SourceError):
        validate_public_url("https://papers.example/source.pdf")
    with pytest.raises(SourceError):
        validate_public_url("https://user:password@papers.example/source.pdf")
    with pytest.raises(SourceError):
        validate_public_url("https://papers.example:5432/source.pdf")


def test_ssrf_pins_validated_ip_and_checks_redirect(monkeypatch):
    from app import source_tools
    seen = []
    class Response:
        status = 302
        def getheader(self, name, default=None):
            return "http://127.0.0.1/secret" if name == "Location" else default
    class Connection:
        def __init__(self, host, port, address, timeout):
            seen.append((host, address))
        def request(self, *a, **kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1" if host == "127.0.0.1" else "8.8.8.8", 80))])
    monkeypatch.setattr(source_tools, "_PinnedHTTP", Connection)
    with pytest.raises(SourceError):
        source_tools.download_public("http://papers.example/start")
    assert seen == [("papers.example", "8.8.8.8")]


def test_source_body_and_actual_received_byte_limit(monkeypatch):
    from app import source_tools
    class Response:
        status = 200
        def getheader(self, name, default=None): return default
        def read(self, count): return b"X" * count
    class Connection:
        sock = None
        def __init__(self, *a): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(source_tools, "validate_public_url", lambda u: ("paper.example", 443, ["8.8.8.8"]))
    monkeypatch.setattr(source_tools, "_PinnedHTTPS", Connection)
    with pytest.raises(SourceError, match="size limit"):
        source_tools.download_public("https://paper.example/a", max_bytes=32)


def test_html_excludes_script_and_preserves_math_alt():
    assert parse_html(b'<html><head>metadata</head><p>Paper <img alt="x + y"></p><script>bad()</script></html>') == 'Paper x + y'


def test_paths_reject_traversal_and_symlinks(tmp_path):
    outside = tmp_path.parent / "outside-paper-secret"
    outside.write_text("private")
    (tmp_path / "link").symlink_to(outside)
    for value in ["../outside-paper-secret", str(outside), "link"]:
        with pytest.raises(PipelineError):
            task_path(tmp_path, value, must_exist=True)


def make_library(tmp_path):
    library = SourceLibrary(tmp_path, {"id": "test", "title": "Paper"}, "ljg-paper")
    library.documents["main"] = SourceDocument("main", "Real title", "https://arxiv.org/abs/2106.10199", "source.pdf",
        [{"id": "p1", "page": 1, "text": "The method improves accuracy from 70 percent to 75 percent."}],
        {"full_text": True}, "abc")
    return library


def test_quote_verification_ignores_model_verified_and_checks_page(tmp_path):
    library = make_library(tmp_path)
    result = {"brief_markdown": "提高准确率 [E1](evidence:E1)", "full_markdown": "实验结果 [E1] [E2]",
              "evidence_index": [{"id": "E1", "source_id": "main", "quote": "accuracy from 70 percent to 75 percent", "verified": False, "page": 1},
                                 {"id": "E2", "source_id": "main", "quote": "The method reaches 99 percent", "verified": True, "page": 1}]}
    clean, errors = validate_result(result, library, [{"script": "validate", "exit_code": 0}])
    assert clean["evidence_index"][0]["verified"] is True
    assert clean["evidence_index"][0]["source_title"] == "Real title"
    assert clean["evidence_index"][1]["verified"] is False
    assert any("E2" in error for error in errors)
    result["evidence_index"][0]["page"] = 2
    clean, errors = validate_result(result, library)
    assert clean["evidence_index"][0]["verified"] is False


def test_entire_source_is_accessible_after_initial_window(tmp_path):
    library = make_library(tmp_path)
    library.documents["main"].sections.append({"id": "p2", "page": 2, "text": "z" * 50000 + "FINAL EVIDENCE"})
    result = library.read_source(section_id="p2", offset=50000)
    assert result["text"] == "FINAL EVIDENCE"
    assert result["next_offset"] is None


def test_related_cap_counts_distinct_sources(tmp_path):
    library = make_library(tmp_path)
    for index in range(3):
        library.documents[str(index)] = SourceDocument(str(index), "External", None, "p.pdf", [], {}, "hash", "external")
    with pytest.raises(SourceError, match="three"):
        library.fetch_related("arxiv:2203.05482")


def test_native_real_local_acquisition_and_path_guards(tmp_path):
    project = Path(__file__).resolve().parents[2]
    root = project / "skills" / "deeppapernote"
    if not root.is_dir():
        pytest.skip("Pinned upstream skill is not installed")
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((50, 50), "A Study of Task Vectors\nAbstract\nWe compare task vectors and model merging.\nMethods\nThe training only changes a small parameter subset.\nResults\nAccuracy improves from 70 to 75 percent.")
    pdf.save(tmp_path / "source.pdf")
    pdf.close()
    pipeline = Pipeline(tmp_path, root, {"id": "test", "title": "A Study of Task Vectors"}, "deeppapernote", "test", threading.Event(), project)
    steps = [
        ("resolve_paper.py", ["--input", "source.pdf", "--zotero-mode", "off", "--output", "native/resolve.json"]),
        ("collect_metadata.py", ["--input", "native/resolve.json", "--output", "native/metadata.json"]),
        ("build_identity_contract.py", ["--input", "native/metadata.json", "--resolve", "native/resolve.json", "--trace-output", "native/trace.json", "--output", "native/identity.json"]),
        ("fetch_pdf.py", ["--input", "native/metadata.json", "--identity", "native/identity.json", "--reference", "source.pdf", "--dest-dir", "native/pdfs", "--output", "native/fetch.json"]),
        ("extract_source_text.py", ["--input", "native/fetch.json", "--output", "native/source_manifest.json", "--raw-sections-output", "native/raw.jsonl", "--full-text-output", "native/full.md"]),
    ]
    for script, args in steps:
        result = pipeline.run_skill_step(script, args)
        assert result["exit_code"] == 0, result["stderr"]
    assert "Task Vectors" in (tmp_path / "native/full.md").read_text()
    with pytest.raises(PipelineError):
        pipeline.run_skill_step("resolve_paper.py", ["--input", "/etc/passwd", "--zotero-mode", "off"])
    with pytest.raises(PipelineError):
        pipeline.run_skill_step("run_pipeline.py", ["--input", "source.pdf"])
    with pytest.raises(PipelineError):
        pipeline.write_artifact("source_corpus.json", "{}")


def test_native_python_guard_blocks_outside_files_and_sockets(tmp_path):
    import sys
    from app.pipeline import PYTHON_FILE_GUARD, _sandbox
    work = tmp_path / "work"; work.mkdir()
    skill = tmp_path / "skill"; skill.mkdir()
    script = skill / "probe.py"
    script.write_text("import socket\ntry:\n open('/etc/passwd').read()\n raise RuntimeError('read escaped')\nexcept PermissionError: pass\ntry:\n socket.socket()\n raise RuntimeError('network escaped')\nexcept PermissionError: pass\nprint('boundaries held')\n")
    result = subprocess.run([sys.executable, "-c", PYTHON_FILE_GUARD, str(work), str(skill), str(script), "[]"], cwd=work,
                            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=10,
                            preexec_fn=lambda: _sandbox(str(work), str(skill)))
    assert result.returncode == 0, result.stderr
    assert "boundaries held" in result.stdout


def integrity_pipeline(tmp_path):
    """Test-only host attestations isolate hash/seal invariants from model quality."""
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/ljg-paper", {"id": "p", "title": "Title"}, "ljg-paper", "team", threading.Event(), project)
    (tmp_path / "native").mkdir(exist_ok=True)
    (tmp_path / "native/note.md").write_text("A reviewed fixture note")
    (tmp_path / "native/paper-map.md").write_text("A reviewed fixture map")
    hashes = {path: pipeline._fingerprint(tmp_path / path) for path in ["native/note.md", "native/paper-map.md"]}
    pipeline.native_checks = [{"script": "validate_note.ts", "exit_code": 0, "semantic_pass": True, "input_hashes": hashes, "output_hashes": {}, "args": ["native/note.md", "--map", "native/paper-map.md"]}]
    (tmp_path / "native-execution-ledger.json").write_text(json.dumps(pipeline.native_checks))
    import hashlib
    pipeline.host_call("host_native_attest", {"note_path": "native/note.md", "note_sha256": hashlib.sha256((tmp_path / "native/note.md").read_bytes()).hexdigest(),
                         "map_path": "native/paper-map.md", "map_sha256": hashlib.sha256((tmp_path / "native/paper-map.md").read_bytes()).hexdigest(),
                         "quality": {"passed": True, "fixture": True}, "readability": {"passed": True, "fixture": True}})
    return pipeline


def test_model_cannot_forge_checks_reviews_or_internal_calls(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    for path in ["native/lint.json", "native/grounding.json", "native/quality_review.json", "native/readability_review.json", "host-native-attestation.json", "native-execution-ledger.json"]:
        with pytest.raises(PipelineError, match="owned"):
            pipeline.write_artifact(path, '{"passed":true}')
    with pytest.raises(PipelineError, match="Unknown"):
        pipeline.call("host_native_attest", {})


def test_native_note_and_map_changes_invalidate_attestation(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    pipeline.write_artifact("native/paper-map.md", "Changed after independent review")
    with pytest.raises(PipelineError, match="changed"):
        pipeline.host_call("host_native_seal", {"note_path": "native/note.md", "map_path": "native/paper-map.md"})


def test_seal_freezes_native_and_revalidates_tampering(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    assert pipeline.host_call("host_native_seal", {"note_path": "native/note.md", "map_path": "native/paper-map.md"})["sealed"]
    with pytest.raises(PipelineError, match="sealed"):
        pipeline.write_artifact("native/note.md", "Changed by product phase")
    with pytest.raises(PipelineError, match="sealed"):
        pipeline.run_skill_step("validate_note.ts", ["native/note.md", "--map", "native/paper-map.md"])
    pipeline.write_artifact("product/brief.md", "Allowed product draft")
    pipeline.library._save()  # trusted related-source index updates stay possible
    pipeline.verify_native_seal()
    (tmp_path / "native/note.md").write_text("Tampered outside authorized tools")
    with pytest.raises(PipelineError, match="integrity"):
        pipeline.verify_native_seal()


def test_native_semantic_failure_prevents_seal_even_exit_zero(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    pipeline.native_checks[0]["semantic_pass"] = False
    with pytest.raises(PipelineError, match="actual checks"):
        pipeline.host_call("host_native_seal", {})


def test_actual_native_lint_failure_cannot_be_forged(tmp_path):
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/deeppapernote", {"id": "p", "title": "Title"}, "deeppapernote", "team", threading.Event(), project)
    pipeline.write_artifact("native/note.md", "An intentionally incomplete native note.")
    result = pipeline.run_skill_step("lint_note.py", ["--input", "native/note.md", "--language", "zh-CN", "--output", "native/lint.json"])
    assert result["semantic_pass"] is False
    with pytest.raises(PipelineError):
        pipeline.write_artifact("native/lint.json", '{"passes_format":true}')


def test_visual_claim_requires_actual_image_tool_read(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    (tmp_path / "native/figure_decisions.json").write_text(json.dumps({"items": [{"visual_review": {"reviewed_asset_sha256": "f" * 64}}]}))
    with pytest.raises(PipelineError, match="never inspected"):
        pipeline.host_call("host_native_seal", {})


def test_seal_survives_trusted_checkpoint_relocation(tmp_path):
    import shutil
    from app.worker import _rewrite_checkpoint_paths
    original = tmp_path / "first"; original.mkdir()
    pipeline = integrity_pipeline(original)
    pipeline.host_call("host_native_seal", {})
    restored = tmp_path / "restored"
    shutil.copytree(original, restored)
    _rewrite_checkpoint_paths(restored, original)
    resumed = Pipeline(restored, pipeline.skill_root, {"id": "p", "title": "Title"}, "ljg-paper", "team", threading.Event(), pipeline.project_dir)
    assert resumed.host_call("host_native_seal", {}) == {"sealed": True, "reused": True}
    (restored / "native-execution-ledger.json").write_text("[]")
    with pytest.raises(PipelineError, match="ledger integrity"):
        resumed.verify_native_seal()


def test_mixed_scanned_pdf_is_partial_not_full_text(tmp_path):
    from app.source_tools import parse_document
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((50, 50), "Abstract and method: " + "The evaluation measures transfer across domains. " * 4)
    page = pdf.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pix.clear_with(255)
    page.insert_image(fitz.Rect(0, 0, 500, 700), pixmap=pix)
    path = tmp_path / "mixed.pdf"; pdf.save(path); pdf.close()
    sections, coverage = parse_document(path, "main")
    assert coverage["full_text"] is False
    assert coverage["status"] == "partial"
    assert coverage["missing_text_pages"] == [2]
    assert coverage["text_page_ratio"] == 0.5


def test_configured_ocr_attempt_precedes_image_only_rejection(tmp_path, monkeypatch):
    from app import source_tools
    pdf = fitz.open(); page = pdf.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False); pix.clear_with(255)
    page.insert_image(fitz.Rect(0, 0, 500, 700), pixmap=pix)
    path = tmp_path / "scan.pdf"; pdf.save(path); pdf.close()
    with pytest.raises(SourceError, match="image-only"):
        source_tools.parse_document(path, "main")
    monkeypatch.setenv("PAPER_READER_DOCLING_ENABLED", "1")
    # Availability interface test, not a claim that actual OCR models were run.
    monkeypatch.setattr(source_tools, "parse_docling", lambda p: ([{"id": "ocr1", "page": 1, "text": "Recovered optical text. " * 8}], "docling-layout-ocr"))
    _, coverage = source_tools.parse_document(path, "main", prefer_docling=True)
    assert coverage["full_text"] is True and coverage["parser"] == "docling-layout-ocr"


def test_successful_validator_for_other_note_cannot_seal_reviewed_note(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    pipeline.write_artifact("native/other-note.md", "A different note")
    pipeline.write_artifact("native/other-map.md", "A different map")
    entry = pipeline.native_checks[0]
    entry["args"] = ["native/other-note.md", "--map", "native/other-map.md"]
    entry["input_hashes"] = {path: pipeline._fingerprint(tmp_path / path) for path in ("native/other-note.md", "native/other-map.md")}
    (tmp_path / "native-execution-ledger.json").write_text(json.dumps(pipeline.native_checks))
    with pytest.raises(PipelineError, match="different note/map"):
        pipeline.host_call("host_native_seal", {"note_path": "native/note.md", "map_path": "native/paper-map.md"})


def test_internal_native_status_exposes_only_verified_resume_paths(tmp_path):
    pipeline = integrity_pipeline(tmp_path)
    assert pipeline.host_call("host_native_status", {}) == {"sealed": False}
    pipeline.host_call("host_native_seal", {})
    status = pipeline.host_call("host_native_status", {})
    assert status["sealed"] is True
    assert status["note_path"] == "native/note.md"
    assert status["map_path"] == "native/paper-map.md"
    assert status["reviews"]["passed"] is True
    assert not Path(status["note_path"]).is_absolute()


def test_native_tool_envelope_bounds_context_and_preserves_attested_full_logs(tmp_path):
    import hashlib
    project = tmp_path / "project"
    validator = project / "agent-runner/dist/validate-ljg.js"
    validator.parent.mkdir(parents=True)
    # Explicit trusted-process fixture tests the tool envelope, not skill quality.
    validator.write_text("const fs=require('fs');fs.writeFileSync('native/changed.json',JSON.stringify({changed:true}));process.stdout.write(JSON.stringify({ok:true,padding:'x'.repeat(20000)}));process.stderr.write('diagnostic '.repeat(1000));")
    work = tmp_path / "work"; (work / "native").mkdir(parents=True)
    (work / "native/note.md").write_text("Fixture note")
    (work / "native/map.md").write_text("Fixture map")
    (work / "unrelated.txt").write_text("Existing source must not repeat in response")
    pipeline = Pipeline(work, project / "skills/ljg-paper", {"id": "p", "title": "Title"}, "ljg-paper", "team", threading.Event(), project)
    result = pipeline.run_skill_step("validate_note.ts", ["native/note.md", "--map", "native/map.md"])
    assert result["semantic_pass"] is True  # full JSON, not truncated preview, parsed
    assert len(result["stdout"]) == 2000 and result["stdout_truncated"]
    assert len(result["stderr"]) == 4000 and result["stderr_truncated"]
    assert len(json.loads((work / result["stdout_log"]).read_text())["padding"]) == 20000
    assert (work / result["stderr_log"]).stat().st_size == 11000
    assert [item["path"] for item in result["files"]] == ["native/changed.json"]
    assert result["changed_file_count"] == 1
    assert result["checks"][0]["ok"] is True
    assert set(pipeline.native_checks[-1]["log_hashes"]) == {result["stdout_log"], result["stderr_log"]}
    assert pipeline.read_artifact(result["stdout_log"], limit=30000)["content"].endswith('"}')
    with pytest.raises(PipelineError, match="owned"):
        pipeline.write_artifact(result["stdout_log"], "Forged check output")
    pipeline.host_call("host_native_attest", {"note_path": "native/note.md", "note_sha256": hashlib.sha256((work / "native/note.md").read_bytes()).hexdigest(),
                       "map_path": "native/map.md", "map_sha256": hashlib.sha256((work / "native/map.md").read_bytes()).hexdigest(),
                       "quality": {"passed": True, "fixture": True}, "readability": {"passed": True, "fixture": True}})
    pipeline.host_call("host_native_seal", {})
    (work / result["stdout_log"]).write_text("Tampered output")
    with pytest.raises(PipelineError, match="integrity"):
        pipeline.verify_native_seal()


@pytest.mark.parametrize("coverages,expected", [([], "not_completed"), ([True], "completed"), ([False], "partial"), ([True, False], "partial")])
def test_cross_paper_completion_requires_complete_external_text(tmp_path, coverages, expected):
    library = make_library(tmp_path)
    for index, complete in enumerate(coverages):
        identity = f"related:{index}"
        library.documents[identity] = SourceDocument(identity, f"Related paper {index}", None, f"related-{index}.pdf",
            [{"id": "p1", "page": 1, "text": "The retained first page is readable, while a later page may be scanned."}],
            {"full_text": complete, "missing_text_pages": [] if complete else [2]}, "related-hash", "external")
    result = {"brief_markdown": "结果 [E1](evidence:E1)", "full_markdown": "完整结果 [E1](evidence:E1)",
              "evidence_index": [{"id": "E1", "source_id": "main", "quote": "accuracy from 70 percent to 75 percent", "kind": "paper"}]}
    clean, errors = validate_result(result, library, [{"script": "validate_note.ts", "exit_code": 0}])
    assert clean["verification"]["cross_paper_status"] == expected
    if expected == "partial":
        assert any("without complete source text" in error for error in errors)
        assert any("missing reliable text on pages 2" in limitation for limitation in clean["verification"]["limitations"])
        assert clean["verification"]["server_quote_check"] == "passed"
    else:
        assert errors == []
    assert clean["evidence_index"][0]["verified"] is True


def test_pinned_figure_recrop_accepts_review_decisions_and_rejects_outside_paths(tmp_path):
    import hashlib
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/deeppapernote", {"id": "p", "title": "Figure fixture"}, "deeppapernote", "team", threading.Event(), project, vision=True)
    (tmp_path / "native/assets").mkdir(parents=True)
    pdf = fitz.open(); page = pdf.new_page(width=240, height=240)
    page.insert_text((25, 50), "Figure 1: a bounded crop fixture")
    page.draw_rect(fitz.Rect(30, 70, 200, 200), color=(0.1, 0.2, 0.3))
    page.get_pixmap().save(tmp_path / "native/assets/candidate.png")
    pdf.save(tmp_path / "source.pdf"); pdf.close()
    before_pdf = hashlib.sha256((tmp_path / "source.pdf").read_bytes()).hexdigest()
    before_image = hashlib.sha256((tmp_path / "native/assets/candidate.png").read_bytes()).hexdigest()
    pipeline.read_artifact("native/assets/candidate.png")
    review = {"status": "ok", "script": "plan_figure_table_decisions.py", "output_language": "zh-CN", "paper_id": "fixture:p", "asset_subdir": "images",
              "decisions": [{"source_id": "Figure 1", "decision": "review_pending", "source_image_path": "native/assets/candidate.png",
                             "source_image_sha256": before_image, "relative_markdown_embed": "![Figure 1](images/candidate.png)",
                             "review_evidence": {"source_pdf_path": "source.pdf", "source_page": 1},
                             "visual_review": {"status": "repair_requested", "reviewed_asset_sha256": before_image,
                                               "failure_reason": "insufficient_safety_margin", "repair_attempts": 0,
                                               "revised_bbox": [0.08, 0.12, 0.92, 0.92]}}]}
    pipeline.write_artifact("native/review.json", json.dumps(review))
    result = pipeline.run_skill_step("plan_figure_table_decisions.py", ["--review-decisions", "native/review.json", "--language", "zh-CN", "--output", "native/repaired.json"])
    assert result["exit_code"] == 0, result["stderr"]
    assert result["semantic_pass"] is True
    repaired = json.loads((tmp_path / "native/repaired.json").read_text())["decisions"][0]
    assert repaired["visual_review"]["status"] == "pending"
    assert repaired["visual_review"]["repair_attempts"] == 1
    assert repaired["decision"] == "review_pending"
    assert repaired["source_image_sha256"] != before_image
    assert (tmp_path / repaired["source_image_path"]).is_file()
    assert hashlib.sha256((tmp_path / "source.pdf").read_bytes()).hexdigest() == before_pdf
    assert "native/review.json" in pipeline.native_checks[-1]["input_hashes"]
    with pytest.raises(PipelineError, match="escapes"):
        pipeline.run_skill_step("plan_figure_table_decisions.py", ["--review-decisions", "/etc/passwd", "--output", "native/no.json"])
    review["decisions"][0]["review_evidence"]["source_pdf_path"] = "/etc/passwd"
    pipeline.write_artifact("native/escape.json", json.dumps(review))
    with pytest.raises(PipelineError, match="escapes"):
        pipeline.run_skill_step("plan_figure_table_decisions.py", ["--review-decisions", "native/escape.json", "--output", "native/no.json"])
    with pytest.raises(PipelineError, match="escapes"):
        pipeline.run_skill_step("plan_figure_table_decisions.py", ["--review-decisions", "native/review.json", "--output", "../outside.json"])


def test_malformed_json_write_preserves_previous_valid_artifact(tmp_path):
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/deeppapernote", {"id": "p", "title": "Title"}, "deeppapernote", "team", threading.Event(), project)
    initial = '{"decisions": [{"source_id": "Figure 1", "decision": "review_pending"}]}'
    pipeline.write_artifact("native/figure_decisions.json", initial)
    target = tmp_path / "native/figure_decisions.json"
    before = target.read_bytes()
    with pytest.raises(PipelineError, match="complete and valid; existing file was preserved"):
        pipeline.write_artifact("native/figure_decisions.json", '{"decisions": [{"source_image_path": "native/assets/fig')
    assert target.read_bytes() == before
    replacement = '{"decisions": [{"source_id": "Figure 1", "decision": "insert"}]}'
    pipeline.write_artifact("native/figure_decisions.json", replacement)
    assert target.read_text() == replacement
    assert not list(target.parent.glob(".figure_decisions.json.*.tmp"))
    with pytest.raises(PipelineError, match="owned"):
        pipeline.write_artifact("native/lint.json", '{"passes_')
    assert not (tmp_path / "native/lint.json").exists()


def test_pipeline_default_budget_allows_call_81_and_rejects_161(tmp_path):
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/ljg-paper", {"id": "p", "title": "Title"}, "ljg-paper", "team", threading.Event(), project)
    for _ in range(160):
        assert pipeline.call("list_artifacts", {}) == {"files": []}
    assert pipeline.tool_calls == 160
    with pytest.raises(BudgetExhausted, match=r"160 calls"):
        pipeline.call("list_artifacts", {})


@pytest.mark.parametrize("last_host", [False, True])
def test_pipeline_host_and_model_rpcs_share_exact_configured_budget(tmp_path, last_host):
    project = Path(__file__).resolve().parents[2]
    pipeline = Pipeline(tmp_path, project / "skills/ljg-paper", {"id": "p", "title": "Title"}, "ljg-paper", "team", threading.Event(), project, max_tool_calls=10)
    for _ in range(5):
        assert pipeline.call("list_artifacts", {}) == {"files": []}
        assert pipeline.host_call("host_native_status", {}) == {"sealed": False}
    assert pipeline.tool_calls == 10
    call = pipeline.host_call if last_host else pipeline.call
    with pytest.raises(BudgetExhausted, match=r"10 calls"):
        call("host_native_status" if last_host else "list_artifacts", {})


@pytest.mark.parametrize("limit", [9, 501, True, "160"])
def test_pipeline_rejects_invalid_tool_budget(tmp_path, limit):
    project = Path(__file__).resolve().parents[2]
    with pytest.raises(PipelineError, match="integer between 10 and 500"):
        Pipeline(tmp_path, project / "skills/ljg-paper", {"id": "p", "title": "Title"}, "ljg-paper", "team", threading.Event(), project, max_tool_calls=limit)
