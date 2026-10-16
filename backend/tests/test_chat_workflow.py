"""Host chat decisions against retained sources, without a live model or DB."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from app.chat_workflow import ChatPipeline, ChatWorkflow, MAX_REPAIRS
from app.pipeline import BudgetExhausted, Pipeline, PipelineError, TaskCancelled
from app.source_tools import SourceDocument, SourceError


ORIGINAL = "The method improves accuracy from 70 percent to 75 percent, but does not improve latency."
PROJECT = Path(__file__).resolve().parents[2]


def document(identity="main", text=ORIGINAL, page=2):
    return SourceDocument(id=identity, title="Actual original title", url="https://arxiv.org/abs/2401.00001",
                          path="source.pdf", sections=[{"id": "p2", "page": page, "text": text}],
                          coverage={"full_text": True, "parser": "pdf"}, sha256="a" * 64,
                          kind="paper" if identity == "main" else "external")


def pipeline(tmp_path):
    value = ChatPipeline(tmp_path, PROJECT / "skills", {"id": "paper", "title": "Paper", "source_type": "pdf"},
                         "deeppapernote", "workspace", threading.Event(), PROJECT, max_tool_calls=48)
    value.library.documents["main"] = document()
    return value


def answer(quote=ORIGINAL, **overrides):
    return {"answer_markdown": "Accuracy improves; latency does not. [E1](evidence:E1)",
            "evidence": [{"id": "E1", "source_id": "main", "section_id": "p2", "page": 2, "quote": quote}],
            "limitations": [], **overrides}


def workflow(tmp_path, responses, inputs=None, check_budget=None):
    pipe = pipeline(tmp_path)
    calls, events = [], []
    iterator = iter(responses)
    spec = {"limits": {"timeout_seconds": 300, "max_tool_calls": 48, "max_related_papers": 3},
            "model": {"context_window": 128000, "max_tokens": 4096},
            "chat": {"input": {"question": "What changed in accuracy?", "history": [], "summary": "",
                               "reading_context": {"paper_title": "Paper"}, **(inputs or {})}}}
    def call(operation, payload, timeout, tool_cap):
        calls.append({"operation": operation, "input": deepcopy(payload), "timeout": timeout, "tool_cap": tool_cap})
        value = next(iterator)
        if callable(value):
            value = value(operation, payload, pipe)
        pipe.host_call("host_chat_submission", {})
        return deepcopy(value)
    flow = ChatWorkflow(pipe, spec, call, events.append, check_budget or (lambda: None))
    return flow, pipe, calls, events


def test_verified_answer_uses_original_titles_offsets_and_independent_review(tmp_path):
    proposed = answer()
    proposed["evidence"][0].update(verified=True, source_title="Model-invented title", source_url="https://evil.invalid")
    flow, pipe, calls, events = workflow(tmp_path, [proposed, {"passed": True, "issues": []}])
    result = flow.execute()
    assert result["status"] == "succeeded"
    assert [call["operation"] for call in calls] == ["answer", "review"]
    assert "history" not in calls[-1]["input"] and "summary" not in calls[-1]["input"]
    evidence = result["evidence_index"][0]
    assert evidence["source_title"] == "Actual original title"
    assert evidence["source_url"] == pipe.library.documents["main"].url
    assert evidence["verified"] and evidence["quote"] == ORIGINAL
    assert evidence["offset"] == 0 and evidence["end_offset"] == len(ORIGINAL)
    assert events[-1] == {"type": "replace", "content": result["content"]}
    assert result["verification"]["repair_attempts"] == []


@pytest.mark.parametrize("quote", [ORIGINAL.replace("75", "95"), ORIGINAL.replace("does not", "does"),
                                    ORIGINAL.replace("70", "70⁶")])
def test_substantive_source_changes_never_pass_after_noop_repairs(tmp_path, quote):
    bad = answer(quote)
    flow, _, calls, _ = workflow(tmp_path, [bad] * 4)
    result = flow.execute()
    assert result["status"] == "partial"
    assert not result["evidence_index"][0]["verified"]
    assert not result["verification"]["quote_match"]["passed"]
    assert result["verification"]["independent_review"]["status"] == "not_run"
    assert len(calls) == 1 + MAX_REPAIRS
    assert "Accuracy improves" in result["content"] and "待核验" in result["content"]
    assert all(attempt["status"] == "failed" for attempt in result["verification"]["repair_attempts"])


def test_noop_repair_then_corrected_quote_reuses_specific_feedback(tmp_path):
    bad = answer(ORIGINAL.replace("75", "95"))
    flow, _, calls, _ = workflow(tmp_path, [bad, bad, answer(), {"passed": True, "issues": []}])
    result = flow.execute()
    assert result["status"] == "succeeded"
    first, second = calls[1]["input"]["repair_context"], calls[2]["input"]["repair_context"]
    assert first["round"] == 1 and second["round"] == 2 and second["max_rounds"] == 3
    assert second["unchanged_errors"] and "no actual change" in second["previous_attempt"]["error"]
    assert first["diagnostics"][0]["suggested_locations"][0]["text"] == ORIGINAL
    assert [attempt["status"] for attempt in result["verification"]["repair_attempts"]] == ["failed", "succeeded"]


def test_semantic_review_noop_cannot_reask_reviewer_for_a_different_verdict(tmp_path):
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": False, "issues": ["Result lacks comparison conditions"]},
                                           answer(), answer(), answer()])
    result = flow.execute()
    assert result["status"] == "partial"
    assert sum(call["operation"] == "review" for call in calls) == 1
    assert result["verification"]["independent_review"]["issues"] == ["Result lacks comparison conditions"]
    assert len(result["verification"]["repair_attempts"]) == 3


def test_changed_semantic_repair_gets_a_fresh_review(tmp_path):
    changed = answer(answer_markdown="With the stated experiment conditions, accuracy improves. [E1](evidence:E1)")
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": False, "issues": ["Qualify the experimental conditions"]},
                                           changed, {"passed": True, "issues": []}])
    result = flow.execute()
    assert result["status"] == "succeeded"
    assert [call["operation"] for call in calls] == ["answer", "review", "answer", "review"]
    assert calls[2]["input"]["repair_context"]["errors"] == ["Qualify the experimental conditions"]


def test_malformed_repair_preserves_last_useful_answer_and_repairs_again(tmp_path):
    bad = answer(ORIGINAL.replace("75", "95"))
    flow, _, calls, _ = workflow(tmp_path, [bad, {"answer_markdown": ""}, answer(), {"passed": True, "issues": []}])
    assert flow.execute()["status"] == "succeeded"
    assert calls[2]["input"]["repair_context"]["previous_answer"] == bad
    assert "bounded text" in calls[2]["input"]["repair_context"]["previous_attempt"]["error"]


@pytest.mark.parametrize("citation", ["[E2](evidence:E2)", "[bad](evidence:E 1)"])
def test_unknown_or_malformed_citation_is_not_silently_accepted(tmp_path, citation):
    bad = answer(answer_markdown="Some assertion. " + citation)
    flow, _, _, _ = workflow(tmp_path, [bad] * 4)
    result = flow.execute()
    assert result["status"] == "partial"
    assert any("evidence" in issue for issue in result["verification"]["issues"])


def test_zero_evidence_general_explanation_still_requires_independent_review(tmp_path):
    general = answer(answer_markdown="一般背景解释：请先说明要比较的方法和指标。", evidence=[])
    flow, _, calls, _ = workflow(tmp_path, [general, {"passed": True, "issues": []}])
    result = flow.execute()
    assert result["status"] == "succeeded" and result["evidence_index"] == []
    assert calls[-1]["operation"] == "review"


def test_external_failure_keeps_useful_answer_with_honest_coverage_limit(tmp_path):
    def external_failure(operation, payload, pipe):
        pipe.library.limitations.append("arXiv search unavailable")
        return answer()
    flow, _, _, _ = workflow(tmp_path, [external_failure, {"passed": True, "issues": []}])
    result = flow.execute()
    assert result["status"] == "partial"
    assert result["verification"]["quote_match"]["passed"]
    assert result["verification"]["independent_review"]["passed"]
    assert "arXiv search unavailable" in result["content"]
    assert "Accuracy improves" in result["content"]


def test_math_prose_spacing_uses_conservative_shared_matcher(tmp_path):
    original = "We therefore use so 𝛾input tokens for the draft positions."
    proposed = answer(original.replace("𝛾input", "𝛾 input"))
    flow, pipe, _, _ = workflow(tmp_path, [proposed, {"passed": True, "issues": []}])
    pipe.library.documents["main"] = document(text=original)
    assert flow.execute()["evidence_index"][0]["quote"] == original


def test_declared_page_mismatch_stays_invalid_even_if_quote_exists(tmp_path):
    proposed = answer()
    proposed["evidence"][0]["page"] = 1
    flow, _, _, _ = workflow(tmp_path, [proposed] * 4)
    result = flow.execute()
    assert result["status"] == "partial" and not result["evidence_index"][0]["verified"]
    assert "missing or ambiguous" in result["verification"]["issues"][0]


def test_search_windows_are_real_offsets_even_after_casefold_expansion(tmp_path):
    pipe = pipeline(tmp_path)
    raw = "İ ß " * 400 + "needle " + "tail " * 500
    pipe.library.documents["main"] = document(text=raw)
    result = pipe.call("search_source", {"query": "needle", "limit": 1})["results"][0]
    assert result["offset"] > 0
    assert result["text"] == raw[result["offset"]:result["end_offset"]]
    assert "needle" in result["text"] and result["section_id"] == "p2"


def test_chinese_question_gets_original_intro_and_english_search_guidance(tmp_path):
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}], inputs={"question": "解释一下这个方法"})
    flow.execute()
    assert calls[0]["input"]["source_sections"][0]["text"] == ORIGINAL


@pytest.mark.parametrize("tool", ["write_artifact", "run_skill_step", "read_artifact", "prepare_source", "host_native_seal"])
def test_chat_rejects_artifact_native_and_unlisted_model_tools(tmp_path, tool):
    pipe = pipeline(tmp_path)
    with pytest.raises(PipelineError, match="unavailable"):
        pipe.call(tool, {})
    assert pipe.tool_calls == 1


def test_review_and_compaction_cannot_inherit_answer_tools(tmp_path):
    pipe = pipeline(tmp_path)
    pipe.operation = "review"
    with pytest.raises(PipelineError, match="unavailable"):
        pipe.call("search_papers", {"query": "anything"})
    assert pipe.call("read_source", {"source_id": "main"})["text"] == ORIGINAL
    pipe.operation = "compact"
    with pytest.raises(PipelineError, match="unavailable"):
        pipe.call("read_source", {})
    with pytest.raises(PipelineError, match="Unknown internal"):
        pipe.host_call("host_native_status", {})


def test_cached_sources_activate_without_download_and_cap_three(tmp_path, monkeypatch):
    pipe = pipeline(tmp_path)
    for index in range(4):
        identity = f"related:arxiv:2501.0000{index}"
        pipe.cached_documents[identity] = document(identity)
    monkeypatch.setattr(pipe.library, "is_main_source", lambda *args: False)
    monkeypatch.setattr(pipe.library, "fetch_related", lambda **kwargs: pytest.fail("cached source redownloaded"))
    for index in range(3):
        result = pipe.call("fetch_related", {"id": f"arxiv:2501.0000{index}"})
        assert result["reused"]
    with pytest.raises(SourceError, match="three related"):
        pipe.call("read_source", {"source_id": "related:arxiv:2501.00003"})
    assert len(pipe.active_external) == 3
    assert len(pipe.retained_documents()) == 5
    assert pipe.call("read_source", {"source_id": "related:arxiv:2501.00000"})["text"] == ORIGINAL


def test_cached_evidence_activates_before_validation_and_is_discoverable(tmp_path):
    proposed = answer()
    proposed["evidence"][0]["source_id"] = "related:arxiv:2501.00001"
    flow, pipe, calls, _ = workflow(tmp_path, [proposed, {"passed": True, "issues": []}])
    pipe.cached_documents["related:arxiv:2501.00001"] = document("related:arxiv:2501.00001")
    result = flow.execute()
    assert result["status"] == "succeeded"
    indexed = next(row for row in calls[0]["input"]["section_index"] if row["source_id"] != "main")
    assert indexed["cached"] and indexed["source_title"] == "Actual original title"
    assert result["evidence_index"][0]["kind"] == "external"


def test_workspace_arxiv_fetch_counts_canonical_source_before_activating_cached_papers(tmp_path, monkeypatch):
    pipe = pipeline(tmp_path)
    canonical = "related:arxiv:2501.00001"
    fetches = []
    monkeypatch.setattr(pipe, "_workspace_source_identity", lambda identity: (canonical, "https://arxiv.org/abs/2501.00001"))
    monkeypatch.setattr(pipe.library, "is_main_source", lambda *args: False)
    def inherited_fetch(self, id, url=None):
        fetches.append(id)
        self.library.documents[canonical] = document(canonical)
        return {"source_manifest": self.library.documents[canonical].manifest()}
    monkeypatch.setattr(Pipeline, "fetch_related", inherited_fetch)
    for index in range(2, 5):
        identity = f"related:arxiv:2501.0000{index}"
        pipe.cached_documents[identity] = document(identity)
    result = pipe.call("fetch_related", {"id": "workspace:team-paper"})
    assert result["source_manifest"]["source_id"] == canonical
    assert canonical in pipe.active_external and canonical in pipe.cached_documents
    pipe.call("read_source", {"source_id": "related:arxiv:2501.00002"})
    pipe.call("fetch_related", {"id": "arxiv:2501.00003"})
    with pytest.raises(SourceError, match="three related"):
        pipe.call("read_source", {"source_id": "related:arxiv:2501.00004"})
    assert len(pipe.active_external) == 3 and len(pipe.library.documents) == 4
    # Even at the limit, the workspace alias must resolve to the active public
    # original; it must not be treated as a fourth fetch or redownloaded.
    assert pipe.call("fetch_related", {"id": "workspace:team-paper"})["reused"]
    assert fetches == ["workspace:team-paper"]


def test_workspace_alias_rechecks_scope_before_reusing_cached_public_arxiv(tmp_path, monkeypatch):
    from app import db as db_module
    from types import SimpleNamespace
    pipe = pipeline(tmp_path)
    canonical = "related:arxiv:2501.00001"
    pipe.cached_documents[canonical] = document(canonical)
    monkeypatch.setattr(pipe.library, "is_main_source", lambda *args: False)
    monkeypatch.setattr(Pipeline, "fetch_related", lambda *args, **kwargs: pytest.fail("canonical cache was redownloaded"))
    statements = []
    paper = SimpleNamespace(source_type="arxiv", source_url="https://arxiv.org/abs/2501.00001", sha256="b" * 64)
    class ScopedSession:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def scalar(self, statement):
            statements.append(statement)
            return paper
    monkeypatch.setattr(db_module, "SessionLocal", ScopedSession)
    for _ in range(2):
        result = pipe.call("fetch_related", {"id": "workspace:team-paper", "url": paper.source_url})
        assert result["reused"] and result["source_manifest"]["source_id"] == canonical
    assert len(statements) == 2  # No previously resolved alias bypasses access.
    for statement in statements:
        compiled = statement.compile()
        assert compiled.params["id_1"] == "team-paper"
        assert compiled.params["workspace_id_1"] == pipe.workspace_id
        assert "papers.workspace_id =" in str(compiled)


def test_unknown_workspace_alias_cannot_reuse_an_unscoped_cache(tmp_path, monkeypatch):
    from app import db as db_module
    pipe = pipeline(tmp_path)
    identity = "related:workspace:foreign-paper"
    pipe.cached_documents[identity] = document(identity)
    monkeypatch.setattr(Pipeline, "fetch_related", lambda *args, **kwargs: pytest.fail("unknown workspace source fetched"))
    statements = []
    class ScopedSession:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def scalar(self, statement):
            statements.append(statement)
            return None
    monkeypatch.setattr(db_module, "SessionLocal", ScopedSession)
    with pytest.raises(PipelineError, match="unavailable in this workspace"):
        pipe.call("fetch_related", {"id": "workspace:foreign-paper"})
    assert statements[0].compile().params["workspace_id_1"] == pipe.workspace_id
    assert pipe.active_external == set()


def test_compaction_preserves_recent_turns_and_returns_cursor_without_mutating_raw_history(tmp_path):
    history = [{"role": "user" if index % 2 else "assistant", "content": (f"message {index} " * 1000), "sequence": index}
               for index in range(1, 13)]
    original = deepcopy(history)
    def output(operation, payload, pipe):
        if operation == "compact":
            return {"summary": "User compares methods A and B; preserve E1 and unresolved speed question."}
        return answer() if operation == "answer" else {"passed": True, "issues": []}
    flow, _, calls, _ = workflow(tmp_path, [output] * 20, inputs={"history": history})
    # The complete Pi policy/tools also occupy context; leave enough room for
    # the two large recent pairs after reserving their actual fixed overhead.
    flow.spec["model"]["context_window"] = 80000
    result = flow.execute()
    assert calls[0]["operation"] == "compact"
    answer_call = next(call for call in calls if call["operation"] == "answer")
    assert len(answer_call["input"]["history"]) == 4
    assert answer_call["input"]["history"][0]["content"] == history[-4]["content"]
    assert result["summary_through_sequence"] == 8
    assert "E1" in result["summary"] and history == original


def test_small_context_compaction_final_payload_fits_and_cursor_keeps_raw_recent_pair_when_possible(tmp_path):
    history = [{"role": "user" if index % 2 else "assistant", "content": f"message {index} " * 400, "sequence": index}
               for index in range(1, 13)]
    def output(operation, payload, pipe):
        if operation == "compact":
            return {"summary": "Older dialogue discussed accuracy and latency; E1 is the original evidence."}
        return answer() if operation == "answer" else {"passed": True, "issues": []}
    flow, _, calls, _ = workflow(tmp_path, [output] * 30, inputs={"history": history})
    flow.spec["model"]["context_window"] = 32000
    result = flow.execute()
    answer_call = next(call for call in calls if call["operation"] == "answer")
    assert len(answer_call["input"]["history"]) == 2
    assert answer_call["input"]["history"][-1]["content"] == history[-1]["content"]
    assert result["summary_through_sequence"] == 10
    assert all(len(json.dumps(call["input"], ensure_ascii=False).encode("utf-8")) <= flow._context_bytes(call["operation"]) for call in calls)


def test_large_structured_reading_context_is_bounded_without_modifying_original_input(tmp_path):
    original = {"paper_title": "Large paper", "brief_markdown": "summary " * 4000,
                "full_markdown": "full reading " * 30000,
                "reading_packet": {"version": 2, "claims": [{"id": f"C{i}", "statement": "finding " * 10000} for i in range(40)]},
                "evidence_index": [{"id": f"E{i}", "quote": "original quotation " * 1000} for i in range(80)]}
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}], inputs={"reading_context": original})
    result = flow.execute()
    assert result["status"] == "succeeded"
    assert len(json.dumps(calls[0]["input"], ensure_ascii=False).encode("utf-8")) <= flow._context_bytes()
    assert len(original["reading_packet"]["claims"]) == 40 and len(original["evidence_index"]) == 80


def test_original_quote_window_anchors_full_quote_not_an_earlier_common_word(tmp_path):
    raw = "accuracy " * 900 + ORIGINAL + " ending text " * 400
    flow, pipe, _, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}])
    pipe.library.documents["main"] = document(text=raw)
    windows = flow._source_sections("accuracy", answer()["evidence"])
    assert ORIGINAL in windows[0]["text"]
    assert windows[0]["text"] == raw[windows[0]["offset"]:windows[0]["end_offset"]]


def test_chinese_history_below_entry_limit_compacts_by_serialized_utf8_bytes(tmp_path):
    history = [{"role": "user" if index % 2 else "assistant", "content": f"第{index}轮：" + '解释速度与精度😃\\"\n' * 300,
                "sequence": index} for index in range(1, 13)]
    original = deepcopy(history)
    def output(operation, payload, pipe):
        if operation == "compact":
            return {"summary": "用户询问速度与精度的条件，E1是原文依据，尚需解释对比实验。"}
        return answer() if operation == "answer" else {"passed": True, "issues": []}
    flow, _, calls, _ = workflow(tmp_path, [output] * 20, inputs={"history": history})
    flow.spec["model"]["context_window"] = 64000
    # The previous Unicode-character rule would not compact this dialogue.
    assert len(json.dumps(history, ensure_ascii=False)) < (64000 - 4096 - 6000) * 1.5
    assert len(json.dumps(history, ensure_ascii=False).encode("utf-8")) > flow._context_bytes()
    result = flow.execute()
    assert calls[0]["operation"] == "compact"
    assert result["status"] == "succeeded" and result["summary_through_sequence"] == 8
    answer_call = next(call for call in calls if call["operation"] == "answer")
    assert answer_call["input"]["history"] == [{"role": row["role"], "content": row["content"]} for row in history[-4:]]
    compacted_text = "".join(row["content"] for call in calls if call["operation"] == "compact" for row in call["input"]["history"])
    assert compacted_text == "".join(row["content"] for row in history[:-4])
    assert all(call["input"]["max_summary_bytes"] == flow._summary_bytes() for call in calls if call["operation"] == "compact")
    assert all(len(json.dumps(call["input"], ensure_ascii=False).encode("utf-8")) <= flow._context_bytes(call["operation"]) for call in calls)
    assert history == original


def test_large_chinese_message_compaction_splits_without_dropping_unicode_or_escapes(tmp_path):
    huge = '保留条件和否定词😃\\"\n' * 4000
    history = [{"role": "user", "content": huge, "sequence": 1},
               {"role": "assistant", "content": "这些是尚未验证的上下文线索。", "sequence": 2},
               {"role": "user", "content": "接着解释实验。", "sequence": 3},
               {"role": "assistant", "content": "需要参照E1。", "sequence": 4},
               {"role": "user", "content": "之后再比较速度。", "sequence": 5},
               {"role": "assistant", "content": "仍需保留实验条件。", "sequence": 6}]
    def output(operation, payload, pipe):
        if operation == "compact":
            return {"summary": "保留用户关于实验条件、否定词与E1的约束。"}
        return answer() if operation == "answer" else {"passed": True, "issues": []}
    flow, _, calls, _ = workflow(tmp_path, [output] * 30, inputs={"history": history})
    flow.spec["model"]["context_window"] = 32000
    result = flow.execute()
    chunks = [row["content"] for call in calls if call["operation"] == "compact" for row in call["input"]["history"] if row["role"] == "user"]
    assert len(chunks) > 1 and "".join(chunks) == huge
    assert result["summary_through_sequence"] == 2
    for call in calls:
        assert len(json.dumps(call["input"], ensure_ascii=False).encode("utf-8")) <= flow._context_bytes(call["operation"])


@pytest.mark.parametrize("context_window", [4096, 8192])
def test_small_context_never_gets_raised_to_unsafe_minimum(tmp_path, context_window):
    flow, _, calls, _ = workflow(tmp_path, [])
    flow.spec["model"]["context_window"] = context_window
    assert flow._context_bytes() == 0
    with pytest.raises(PipelineError, match="context|model"):
        flow.execute()
    assert calls == []


@pytest.mark.parametrize("operation", ["answer", "review", "compact", "repair"])
def test_every_operation_rejects_required_utf8_payload_that_only_fits_character_estimate(tmp_path, operation):
    flow, _, calls, _ = workflow(tmp_path, [])
    flow.spec["model"]["context_window"] = 32000
    text = "中" * 11000
    actual_operation = "answer" if operation == "repair" else operation
    if operation == "compact":
        payload = {"summary": "", "history": [{"role": "user", "content": text}]}
    elif operation == "review":
        payload = {"question": "核对事实。", "answer_markdown": text, "evidence": [], "source_sections": [], "limitations": []}
    else:
        payload = {"question": "修复引文。" if operation == "repair" else text, "reading_context": {}, "section_index": [], "source_sections": []}
        if operation == "repair":
            payload["repair_context"] = {"errors": ["引文未匹配"], "previous_answer": {"answer_markdown": text}}
    assert len(json.dumps(payload, ensure_ascii=False)) < flow._context_bytes(actual_operation)
    with pytest.raises(PipelineError, match="context"):
        flow._call(actual_operation, payload)
    assert calls == []


def test_chinese_source_windows_and_diagnostics_use_byte_caps_with_real_offsets(tmp_path):
    from app.chat_workflow import _fit_window
    flow, pipe, _, _ = workflow(tmp_path, [])
    raw = '中文原文需要保留否定和数字😃\\"\n' * 3000
    pipe.library.documents["main"] = document(text=raw)
    windows = flow._source_sections("中文原文")
    assert len(json.dumps(windows, ensure_ascii=False).encode("utf-8")) <= min(24000, flow._context_bytes() // 3)
    for window in windows:
        assert window["text"] == raw[window["offset"]:window["end_offset"]]
    # Full JSON serialization, not only UTF-8 string length, is bounded.
    excerpt = _fit_window(windows[0], 600)
    assert excerpt and len(json.dumps(excerpt, ensure_ascii=False).encode("utf-8")) <= 600
    assert excerpt["text"] == raw[excerpt["offset"]:excerpt["end_offset"]]
    diagnostics = flow._diagnostics([{"id": "E1", "quote": "中文原文", "verification_note": "未匹配原文。"}], ["未匹配"])
    assert len(json.dumps(diagnostics, ensure_ascii=False).encode("utf-8")) <= min(12000, flow._context_bytes() // 4)


def test_chinese_reading_context_is_safely_reduced_without_mutating_pinned_revision(tmp_path):
    reading = {"paper_title": "中文标题😃", "brief_markdown": '中文概要😃\\"\n' * 3000,
               "full_markdown": "完整解读及限制😃" * 10000,
               "reading_packet": {"version": 2, "claims": [{"id": f"C{i}", "statement": "有条件的结论😃" * 300} for i in range(30)]},
               "evidence_index": [{"id": f"E{i}", "quote": "原文证据保留条件和数字。" * 100} for i in range(50)]}
    original = deepcopy(reading)
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}], inputs={"reading_context": reading})
    flow.spec["model"]["context_window"] = 32000
    result = flow.execute()
    assert result["status"] == "succeeded" and reading == original
    for call in calls:
        encoded = json.dumps(call["input"], ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= flow._context_bytes(call["operation"])
        assert json.loads(encoded.decode("utf-8")) == call["input"]


def test_compaction_advertises_byte_limit_and_never_silently_truncates_oversize_summary(tmp_path):
    flow, _, calls, _ = workflow(tmp_path, [{"summary": "重要约束" * 3000}])
    with pytest.raises(PipelineError, match="bounded summary"):
        flow._compact("", [{"role": "user", "content": "保留全部必要约束。"}])
    assert calls[0]["input"]["max_summary_bytes"] == flow._summary_bytes()
    assert flow.summary_update == {}


def _runner_initial_input_capacity(spec, operation):
    """The separately verified SDK first-request reserve, including metadata."""
    output = min(spec["model"]["max_tokens"], {"answer": 6000, "review": 2000, "compact": 3000}[operation])
    wrapper = {"kind": "chat", "operation": operation, "paper": {"id": spec["paper"]["id"], "title": spec["paper"]["title"]},
               "input": {}, "limits": {**spec["limits"], "max_output_tokens": output}}
    return (spec["model"]["context_window"] - output - 4096 - {"answer": 8192, "review": 4608, "compact": 3072}[operation]
            - (len(json.dumps(wrapper, ensure_ascii=False).encode("utf-8")) - 2) - len(spec["work_dir"].encode("utf-8")))


def test_full_compact_fragments_fit_actual_runner_envelope_with_unicode_metadata(tmp_path):
    def output(operation, payload, pipe):
        assert operation == "compact"
        assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= _runner_initial_input_capacity(flow.spec, operation)
        return {"summary": "保留实验条件、原文E1及尚未解决的问题。"}
    flow, _, calls, _ = workflow(tmp_path, [output] * 5)
    flow.spec.update(paper={"id": "00000000-0000-0000-0000-000000000001", "title": '😃中文标题\\"\n' * 100},
                     work_dir="/tmp/" + "中文目录😃" * 30)
    capacity = _runner_initial_input_capacity(flow.spec, "compact")
    # ASCII filler lets the first chunk reach the byte boundary exactly; the
    # Unicode metadata and previous summary consume their real encoded space.
    original = "x" * (capacity * 2)
    summary = flow._compress_history("", [{"role": "user", "content": original}])
    assert summary.startswith("保留实验条件")
    sizes = [len(json.dumps(call["input"], ensure_ascii=False).encode("utf-8")) for call in calls]
    assert len(sizes) == 3 and sizes[0] == capacity and sizes[1] == capacity
    assert "".join(row["content"] for call in calls for row in call["input"]["history"]) == original


def test_400_unicode_history_messages_fit_runner_envelope_without_spurious_compaction(tmp_path):
    history = [{"role": "user" if index % 2 else "assistant", "content": "中文条件" * 15, "sequence": index}
               for index in range(1, 401)]
    def output(operation, payload, pipe):
        assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= _runner_initial_input_capacity(flow.spec, operation)
        return answer() if operation == "answer" else {"passed": True, "issues": []}
    flow, _, calls, _ = workflow(tmp_path, [output] * 2, inputs={"history": history})
    flow.spec.update(paper={"id": "00000000-0000-0000-0000-000000000001", "title": "😃" * 1000},
                     work_dir="/tmp/中文任务目录")
    result = flow.execute()
    assert result["status"] == "succeeded"
    assert [call["operation"] for call in calls] == ["answer", "review"]
    assert len(calls[0]["input"]["history"]) == 400
    assert "summary_through_sequence" not in result


def test_context_capacity_charges_long_title_and_workdir_outside_fixed_policy_margin(tmp_path):
    flow, _, _, _ = workflow(tmp_path, [])
    flow.spec.update(paper={"id": "paper", "title": "Short"}, work_dir="/tmp/task")
    before = {operation: flow._context_bytes(operation) for operation in ("answer", "review", "compact")}
    flow.spec["paper"]["title"] += '中文😃\\"\n' * 100
    flow.spec["work_dir"] += "/资料" * 30
    for operation, previous in before.items():
        assert flow._context_bytes(operation) == _runner_initial_input_capacity(flow.spec, operation)
        assert previous - flow._context_bytes(operation) == 1810


def test_small_history_does_not_compact_and_failed_drafts_are_not_restored_as_facts(tmp_path):
    history = [{"role": "user", "content": "Explain this", "sequence": 1},
               {"role": "assistant", "content": "Unverified draft", "status": "failed", "sequence": 2}]
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}], inputs={"history": history})
    result = flow.execute()
    assert all(call["operation"] != "compact" for call in calls)
    assert calls[0]["input"]["history"] == [{"role": "user", "content": "Explain this"}]
    assert "summary" not in result


@pytest.mark.parametrize("message_count,compact_counts", [(402, [398]), (1004, [400, 400, 200])])
def test_many_short_messages_compact_below_runner_count_limit(tmp_path, message_count, compact_counts):
    history = [{"role": "user" if index % 2 else "assistant", "content": f"Turn {index}", "sequence": index}
               for index in range(1, message_count + 1)]
    original = deepcopy(history)
    def output(operation, payload, pipe):
        # Exercise the runner's real structural limit, even though these short
        # messages fit comfortably inside the configured character budget.
        assert len(payload.get("history", [])) <= 400
        if operation == "compact":
            return {"summary": "Earlier questions concern the same method and source E1."}
        return answer() if operation == "answer" else {"passed": True, "issues": []}
    flow, _, calls, _ = workflow(tmp_path, [output] * 8, inputs={"history": history})
    result = flow.execute()
    assert result["status"] == "succeeded"
    compact_calls = [call for call in calls if call["operation"] == "compact"]
    assert [len(call["input"]["history"]) for call in compact_calls] == compact_counts
    answer_call = next(call for call in calls if call["operation"] == "answer")
    assert answer_call["input"]["history"] == [{"role": row["role"], "content": row["content"]} for row in history[-4:]]
    assert result["summary_through_sequence"] == message_count - 4
    assert history == original


def test_exact_runner_count_limit_does_not_add_compaction_call(tmp_path):
    history = [{"role": "user" if index % 2 else "assistant", "content": f"Turn {index}", "sequence": index}
               for index in range(1, 401)]
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}], inputs={"history": history})
    result = flow.execute()
    assert len(calls[0]["input"]["history"]) == 400
    assert [call["operation"] for call in calls] == ["answer", "review"]
    assert "summary_through_sequence" not in result


def test_summary_cursor_excludes_already_compacted_messages(tmp_path):
    history = [{"role": "user" if index % 2 else "assistant", "content": f"message {index}", "sequence": index}
               for index in range(1, 7)]
    flow, _, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}],
                                 inputs={"history": history, "summary": "Existing summary", "summary_through_sequence": 4})
    flow.execute()
    assert calls[0]["input"]["history"] == [{"role": "user", "content": "message 5"}, {"role": "assistant", "content": "message 6"}]
    assert calls[0]["input"]["summary"] == "Existing summary"


@pytest.mark.parametrize("failure", [TaskCancelled("cancelled"), BudgetExhausted("budget exhausted")])
def test_fatal_shared_limits_propagate_and_keep_emitted_draft(tmp_path, failure):
    def abort_review(operation, payload, pipe):
        raise failure
    flow, _, calls, events = workflow(tmp_path, [answer(), abort_review])
    with pytest.raises(type(failure)):
        flow.execute()
    assert events[-2]["type"] == "replace" and events[-2]["content"] == answer()["answer_markdown"]
    assert len(calls) == 2


def test_model_calls_share_decreasing_tool_budget_and_deadline(tmp_path):
    flow, pipe, calls, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": []}])
    flow.execute()
    assert calls[1]["tool_cap"] == calls[0]["tool_cap"] - 1
    assert calls[1]["timeout"] <= calls[0]["timeout"] <= 300
    flow.deadline = time.monotonic() - 1
    with pytest.raises(BudgetExhausted, match="time budget"):
        flow.check_budget()
    pipe.cancelled.set()
    with pytest.raises(TaskCancelled):
        flow.check_budget()


def test_invalid_review_pass_with_issues_never_counts_as_success(tmp_path):
    changed = answer(answer_markdown="Reworded answer. [E1](evidence:E1)")
    flow, _, _, _ = workflow(tmp_path, [answer(), {"passed": True, "issues": ["Actually unsupported"]},
                                       changed, {"passed": True, "issues": ["Still unsupported"]}, changed, changed])
    result = flow.execute()
    assert result["status"] == "partial"
    assert result["verification"]["independent_review"]["passed"] is False
