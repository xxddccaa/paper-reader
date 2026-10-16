"""Typography normalization resolves only exact retained-source quotations."""
import hashlib
from pathlib import Path

import pytest

from app.evidence import packet_evidence, validate_packet
from app.pipeline import validate_result
from app.source_tools import SourceDocument, SourceLibrary


def packet(quote, page=1):
    return {"version": 1, "paper_title": "Typography fixture", "research_question": "What does the original evidence show?",
            "claims": [{"id": "C1", "kind": "result", "basis": "observation", "statement": "The reported observation is bounded by its evidence.",
                        "conditions": {key: "" for key in ("model", "dataset", "split", "metric", "baseline", "budget")},
                        "evidence": [{"id": "E1", "source_id": "main", "section_id": "p1", "page": page, "quote": quote}]}],
            "open_questions": [], "limitations": []}


def retained_library(tmp_path, text):
    library = SourceLibrary(tmp_path, {"id": "typography", "title": "Typography fixture"}, "ljg-paper")
    path = tmp_path / "source.txt"; path.write_text(text)
    library.documents["main"] = SourceDocument("main", "Typography fixture", None, "source.txt",
        [{"id": "p1", "page": 1, "text": text}], {"full_text": True}, hashlib.sha256(path.read_bytes()).hexdigest())
    return library


def test_actual_pdf_ligatures_case_and_line_spacing_resolve_to_retained_raw_slice(tmp_path):
    import fitz
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")
    if not font.is_file():
        pytest.skip("The PDF glyph fixture requires the installed DejaVu Serif font")
    pdf = fitz.open(); page = pdf.new_page()
    page.insert_font(fontname="fixture", fontfile=str(font))
    page.insert_text((30, 50), "In Diﬀ pruning, efﬁcient updates do not improve 75%².\nThe ﬁnal value remains 75% on validation data.", fontname="fixture", fontsize=11)
    path = tmp_path / "source.pdf"; pdf.save(path); pdf.close()
    library = SourceLibrary(tmp_path, {"id": "typography", "title": "Typography fixture"}, "deeppapernote")
    source = library._add("main", "Typography fixture", None, path, "paper")
    raw = source.sections[0]["text"]
    assert "Diﬀ" in raw and "efﬁcient" in raw and "ﬁnal" in raw
    supplied = "in Diff pruning, efficient updates do not improve 75%². The final value remains 75% on validation data."
    incoming = packet(supplied); incoming["claims"][0]["evidence"][0]["section_id"] = source.sections[0]["id"]
    clean, errors = validate_packet(incoming, library)
    assert errors == []
    quoted = clean["claims"][0]["evidence"][0]["quote"]
    assert quoted != supplied and quoted in raw and quoted.encode() in raw.encode()
    assert "Diﬀ" in quoted and "\n" in quoted and "²" in quoted
    assert incoming["claims"][0]["evidence"][0]["quote"] == supplied
    assert packet_evidence(clean, library)[0]["quote"] == quoted


@pytest.mark.parametrize("original,supplied", [
    ("In  Diﬀ pruning,\n efﬁcient updates remain stable.", "in Diff pruning, efficient updates remain stable."),
    ("The cafe\u0301 results remain unchanged.", "the café results remain unchanged."),
    ("The \u1100\u1161 baseline remains unchanged.", "the 가 baseline remains unchanged."),
    ("STRASSE measurements remain unchanged.", "strasse measurements remain unchanged."),
])
def test_typography_match_keeps_contiguous_original_characters(tmp_path, original, supplied):
    library = retained_library(tmp_path, "Context before. " + original + " Context after.")
    clean, errors = validate_packet(packet(supplied), library)
    assert errors == [] and clean["claims"][0]["evidence"][0]["quote"] == original


def test_existing_raw_quote_is_unchanged(tmp_path):
    quote = "In  Diﬀ pruning,\n the original quotation stays exact."
    library = retained_library(tmp_path, "Before. " + quote + " After.")
    incoming = packet(quote)
    assert validate_packet(incoming, library) == (incoming, [])


DSPARK_RAW_QUOTE = (
    "we treat\nthe anchor itself as the first prediction position, so 𝛾input tokens "
    "(anchor + 𝛾−1 masks) yield 𝛾\ndraft logits. This reduces draft computation "
    "while maintaining similar draft quality."
)
DSPARK_MODEL_QUOTE = " ".join(DSPARK_RAW_QUOTE.split()).replace("𝛾input", "𝛾 input")


def test_dspark_inline_math_gap_resolves_to_exact_page_five_source(tmp_path):
    library = retained_library(tmp_path, "Preceding paragraph. " + DSPARK_RAW_QUOTE + " Following paragraph.")
    section = library.documents["main"].sections[0]
    section.update(id="p5", page=5)
    incoming = packet(DSPARK_MODEL_QUOTE, page=5)
    incoming["claims"][0]["evidence"][0]["section_id"] = "p5"
    clean, errors = validate_packet(incoming, library)
    assert errors == []
    assert clean["claims"][0]["evidence"][0]["quote"] == DSPARK_RAW_QUOTE
    assert packet_evidence(clean, library)[0]["quote"] == DSPARK_RAW_QUOTE
    assert incoming["claims"][0]["evidence"][0]["quote"] == DSPARK_MODEL_QUOTE
    incoming["claims"][0]["evidence"][0]["page"] = 4
    assert validate_packet(incoming, library)[1]
    incoming["claims"][0]["evidence"][0].update(page=5, section_id="p4")
    assert validate_packet(incoming, library)[1]


@pytest.mark.parametrize("original,supplied", [
    ("We use γinput tokens in each block.", "We use γ input tokens in each block."),
    ("We use 𝑘tokens in each block.", "We use 𝑘 tokens in each block."),
    ("We use 𝛾 input tokens in each block.", "We use 𝛾input tokens in each block."),
    ("We use 𝛾input tokens and 𝑘output tokens.", "We use 𝛾 input tokens and 𝑘 output tokens."),
    ("We use 𝛾\n input tokens and 𝑘output tokens.", "We use 𝛾input tokens and 𝑘 output tokens."),
    ("Diﬀ pruning uses 𝛾input tokens and retains 75%².", "diff pruning uses 𝛾 input tokens and retains 75%²."),
])
def test_known_math_prose_gaps_preserve_original_glyphs_and_offsets(tmp_path, original, supplied):
    library = retained_library(tmp_path, "Before. " + original + " After.")
    clean, errors = validate_packet(packet(supplied), library)
    assert errors == []
    assert clean["claims"][0]["evidence"][0]["quote"] == original


@pytest.mark.parametrize("old,new", [
    ("𝛾−1", "𝛾−2"),
    ("𝛾−1", "𝛾+1"),
    ("𝛾−1", "𝛾1"),
    ("𝛾 input", "𝜏 input"),
    ("𝛾 input", "γ input"),
    ("reduces", "does not reduce"),
    ("input tokens", "inputtokens"),
    ("draft computation", "draftcomputation"),
    ("similar", "identical"),
])
def test_math_prose_fallback_does_not_hide_substantive_edits(tmp_path, old, new):
    supplied = DSPARK_MODEL_QUOTE.replace(old, new)
    assert supplied != DSPARK_MODEL_QUOTE
    assert validate_packet(packet(supplied), retained_library(tmp_path, DSPARK_RAW_QUOTE))[1]


@pytest.mark.parametrize("raw,supplied", [
    ("The formula uses γx for the estimate.", "The formula uses γ x for the estimate."),
    ("The formula uses γabc for the estimate.", "The formula uses γ abc for the estimate."),
    ("The formula uses γsin for the estimate.", "The formula uses γ sin for the estimate."),
    ("The formula uses xinput for the estimate.", "The formula uses x input for the estimate."),
    ("The formula uses ∑input for the estimate.", "The formula uses ∑ input for the estimate."),
    ("The formula uses 2γinput tokens.", "The formula uses 2γ input tokens."),
    ("The formula uses 2 γinput tokens.", "The formula uses 2 γ input tokens."),
    ("The formula uses x γinput tokens.", "The formula uses x γ input tokens."),
    ("The formula uses x+γinput tokens.", "The formula uses x+γ input tokens."),
    ("The formula uses x + γinput tokens.", "The formula uses x + γ input tokens."),
    ("The formula uses sin γinput tokens.", "The formula uses sin γ input tokens."),
    ("The formula uses γinput + x.", "The formula uses γ input + x."),
    ("The formula uses γinput x.", "The formula uses γ input x."),
    ("The formula uses γinput sin x.", "The formula uses γ input sin x."),
    ("We use Γinput tokens in each block.", "We use γ input tokens in each block."),
    ("We use ϕinput tokens in each block.", "We use φ input tokens in each block."),
    ("The formula uses γinput² tokens.", "The formula uses γ input² tokens."),
    ("The formula uses γinput₂ tokens.", "The formula uses γ input₂ tokens."),
    ("The formula uses γ²input tokens.", "The formula uses γ² input tokens."),
    ("The formula uses γ input¹ tokens.", "The formula uses γinput¹ tokens."),
    ("The formula uses x=γinput tokens.", "γ input tokens."),
    ("The formula uses x=γ input tokens.", "γinput tokens."),
])
def test_ambiguous_math_and_footnote_boundaries_are_not_equivalent(tmp_path, raw, supplied):
    assert validate_packet(packet(supplied), retained_library(tmp_path, raw))[1]


@pytest.mark.parametrize("supplied", [
    "in Diff pruning, efficient updates do not improve 85%².",
    "in Diff pruning, efficient updates do improve 75%².",
    "in Diff pruning, efficient updates do not improve 75%.",
    "in Diff pruning efficient updates do not improve 75%².",
    "in Diff pruning, efficient updates always improve 75%².",
])
def test_number_negation_footnote_punctuation_and_qualifier_changes_fail(tmp_path, supplied):
    library = retained_library(tmp_path, "In Diﬀ pruning, efﬁcient updates do not improve 75%².")
    assert validate_packet(packet(supplied), library)[1]


@pytest.mark.parametrize("raw,supplied", [
    ("Runtime scales as 10⁶ operations.", "runtime scales as 106 operations."),
    ("The reported accuracy is 95¹ percent.", "the reported accuracy is 951 percent."),
    ("The parameter x₁ controls the shared bias.", "the parameter x1 controls the shared bias."),
    ("Footnotes¹ explain the methods.", "footnotes1 explain the methods."),
    ("The reported accuracy is ９５ percent.", "the reported accuracy is 95 percent."),
])
def test_numeric_and_footnote_glyphs_are_not_compatibility_normalized(tmp_path, raw, supplied):
    library = retained_library(tmp_path, raw)
    assert validate_packet(packet(supplied), library)[1]
    assert validate_packet(packet(raw.lower()), library)[1] == []


def test_normalization_cannot_cross_declared_page_or_invent_table_sentence(tmp_path):
    library = retained_library(tmp_path, "Model | Accuracy\nA | 75\nB | 70")
    assert validate_packet(packet("Model A achieves accuracy 75"), library)[1]
    library.documents["main"].sections.append({"id": "p2", "page": 2, "text": "In Diﬀ pruning, efﬁcient updates remain stable."})
    assert validate_packet(packet("in Diff pruning, efficient updates remain stable."), library)[1]
    assert validate_packet(packet("Model | Accuracy\nA | 75", page=2), library)[1]


@pytest.mark.parametrize("raw,supplied", [
    ("The fine-tuning\nprocedure stays unchanged.", "the finetuning procedure stays unchanged."),
    ("The fine-\ntuning procedure stays unchanged.", "the fine-tuning procedure stays unchanged."),
    ("ﬃ baseline remains unchanged.", "fi baseline remains unchanged."),
])
def test_no_dehyphenation_or_partial_expanded_glyph_match(tmp_path, raw, supplied):
    assert validate_packet(packet(supplied), retained_library(tmp_path, raw))[1]


def test_valid_quotes_stay_passed_when_requested_external_check_is_missing(tmp_path):
    library = retained_library(tmp_path, "The measured result remains stable under these conditions.")
    evidence = packet_evidence(packet("The measured result remains stable under these conditions."), library)
    result = {"brief_markdown": "Evidence [E1]", "full_markdown": "Evidence and conditions [E1]", "evidence_index": evidence}
    clean, errors = validate_result(result, library, workflow_mode="guided", cross_paper_requested=True)
    assert errors and clean["verification"]["cross_paper_status"] == "not_completed"
    assert clean["verification"]["server_quote_check"] == "passed"
    assert all(item["verified"] for item in clean["evidence_index"])
    library.documents["main"].coverage["full_text"] = False
    clean, errors = validate_result(result, library, workflow_mode="native")
    assert any("incomplete reading" in error for error in errors) and any("Native skill" in error for error in errors)
    assert clean["verification"]["server_quote_check"] == "passed"
    result["full_markdown"] = "An unresolved reference [E2]"
    clean, errors = validate_result(result, library, workflow_mode="guided")
    assert errors and clean["verification"]["server_quote_check"] == "partial"
