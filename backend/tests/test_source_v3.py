"""Structured sources use real local parsers; only network transport is mocked.

These tests require neither PostgreSQL nor a configured model connection.
"""
import copy
import base64
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest
from bs4 import BeautifulSoup

from app import source_tools
from app.source_tools import Download, SourceError, SourceLibrary, SOURCE_RULES_VERSION
from app.structured_source import (STRUCTURED_RULES_VERSION, immutable_write, parse_structured_html,
                                   parse_structured_pdf, parse_table, pdf_identity, render_image, figure_pdf_page,
                                   resolve_identity)


PAPER_ID = "2607.05147v1"
PAPER_URL = "https://arxiv.org/html/" + PAPER_ID
BODY = "The method evaluates accuracy under a fixed validation budget. " * 18
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="red"/></svg>'


def html(body="", *, identity=PAPER_ID, metadata="", header=True):
    chrome = f'<header class="arxiv-html-header"><a href="/abs/{identity}">Abstract</a><a href="/pdf/{identity}">PDF</a></header>' if header else ""
    return (f'<!doctype html><html><head><title>A measured method</title>{metadata}</head><body>{chrome}'
            '<article class="ltx_document"><h1>Measured method</h1>'
            f'<div class="ltx_abstract" id="abstract"><h6>Abstract</h6><p id="abstract.p1">{BODY}</p></div>'
            f'<section id="S1"><h2>1 Method</h2><p id="S1.p1">{BODY}</p>{body}</section></article></body></html>').encode()


def library(tmp_path, *, skill="deeppapernote", requested=PAPER_ID, **kwargs):
    return SourceLibrary(tmp_path, {"id": "paper", "title": "Measured method", "source_type": "arxiv",
                                   "source_url": "https://arxiv.org/abs/" + requested}, skill,
                         workflow_mode="guided", rules_version=STRUCTURED_RULES_VERSION, **kwargs)


def install_html(monkeypatch, data=None, *, url=PAPER_URL):
    calls = []
    def download(requested, **kwargs):
        calls.append(requested)
        if "/html/" in requested and not requested.endswith(".svg"):
            return Download(data or html(), url, "text/html")
        if requested.endswith(".svg"):
            return Download(SVG, requested, "image/svg+xml")
        pytest.fail(f"Unexpected network request: {requested}")
    monkeypatch.setattr(source_tools, "download_public", download)
    return calls


def figure_html():
    return html('<figure id="F1"><object data="2607.05147v1/plot.svg" type="image/svg+xml"></object>'
                '<figcaption>Figure 1. Accuracy under the stated budget.</figcaption></figure>')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pdf_bytes(*, text=BODY, gap=False, vector=False):
    with fitz.open() as document:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(30, 30, 560, 800), text)
        if vector:
            page.draw_rect(fitz.Rect(50, 400, 250, 450))
        if gap:
            page = document.new_page()
            page.insert_text((50, 50), "Caption only")
        return document.tobytes()


@pytest.mark.parametrize("skill", ["deeppapernote", "ljg-paper"])
def test_v3_all_guided_skills_use_validated_html_first(tmp_path, monkeypatch, skill):
    calls = install_html(monkeypatch)
    source = library(tmp_path, skill=skill)
    result = source.prepare_source()
    assert calls == [PAPER_URL]
    assert result["source_manifest"]["path"] == "source.html"
    assert result["coverage"]["source_selection"] == {"format": "html", "requested_id": PAPER_ID, "resolved_id": PAPER_ID,
                                                       "reason": "Validated structured HTML full text"}
    assert result["coverage"]["representations"][0]["sha256"] == hashlib.sha256(html()).hexdigest()
    assert any(section["id"] == "S1.p1" for section in source.documents["main"].sections)
    source.prepare_source()
    assert len(calls) == 1


def test_legacy_options_and_missing_saved_version_preserve_flat_parser(tmp_path, monkeypatch):
    assert SOURCE_RULES_VERSION == "source-sections-v2"
    source = SourceLibrary(tmp_path, {"id": "paper"}, "ljg-paper")
    assert source.workflow_mode == "native" and source.rules_version == SOURCE_RULES_VERSION
    path = tmp_path / "source.html"
    path.write_bytes(html('<p><math alttext="M_{t}"><mi>M</mi><mi>t</mi><annotation>M_{t}</annotation></math></p>'))
    document = source._add("main", "Paper", PAPER_URL, path, "paper")
    assert document.coverage["parser"] == "html-text"
    assert document.sections[0]["id"] == "s1"
    assert "MtM_{t}" in document.sections[0]["text"]
    saved = json.loads((tmp_path / "source_corpus.json").read_text())
    saved.pop("version")
    (tmp_path / "source_corpus.json").write_text(json.dumps(saved))
    resumed = SourceLibrary(tmp_path, source.paper, source.skill_id)
    assert resumed.documents["main"].sections == document.sections
    resumed.restore_checkpoint(source.export_checkpoint())
    with pytest.raises(SourceError, match="parsing rules"):
        SourceLibrary(tmp_path, source.paper, source.skill_id, rules_version=STRUCTURED_RULES_VERSION)


def test_legacy_deep_still_requests_pdf_first(tmp_path, monkeypatch):
    calls = []
    def download(url, **kwargs):
        calls.append(url)
        return Download(pdf_bytes(), url, "application/pdf")
    monkeypatch.setattr(source_tools, "download_public", download)
    source = SourceLibrary(tmp_path, {"id": "paper", "source_url": PAPER_URL}, "deeppapernote", workflow_mode="guided")
    assert source.prepare_source()["coverage"]["parser"] == "pymupdf-page-text"
    assert calls == ["https://arxiv.org/pdf/" + PAPER_ID]


@pytest.mark.parametrize("data", [
    b"<html><title>Access denied</title><article><p>" + BODY.encode() + b"</p></article></html>",
    b"<html><title>Just a moment</title><article><p>" + BODY.encode() + b"</p></article></html>",
    b"<html><h1>arXiv HTML unavailable</h1>" + BODY.encode() * 2 + b"</html>",
    b"<html><article><p>Brief abstract only.</p></article></html>",
])
def test_error_challenge_and_abstract_pages_are_not_full_text(data):
    with pytest.raises(SourceError):
        parse_structured_html(data, url=PAPER_URL, requested_id=PAPER_ID)


@pytest.mark.parametrize("response, declared, match", [
    ("https://arxiv.org/html/2607.05147v2", [], "version"),
    (PAPER_URL, ["2607.05147v2"], "version"),
    (PAPER_URL, ["2106.10199v1"], "identity"),
    ("https://example.com/html/2607.05147v1", [], "arxiv"),
    ("https://arxiv.org/html/2607.05147", [], "version"),
])
def test_identity_and_version_mismatch_is_rejected(response, declared, match):
    with pytest.raises(SourceError, match=match):
        resolve_identity(PAPER_ID, response, declared)


def test_unversioned_resolves_from_header_but_bibliography_is_not_identity():
    data = html('<section class="ltx_bibliography" id="bib"><h2>References</h2>'
                '<li class="ltx_bibitem" id="bib.1"><a href="https://arxiv.org/abs/2106.10199v4">Other paper</a></li></section>')
    sections, coverage = parse_structured_html(data, url="https://arxiv.org/html/2607.05147", requested_id="2607.05147")
    assert coverage["source_selection"]["resolved_id"] == PAPER_ID
    assert coverage["references"] == [{"id": "bib.1", "text": "Other paper"}]
    with pytest.raises(SourceError, match="version"):
        parse_structured_html(html(identity="2607.05147v2"), url=PAPER_URL, requested_id=PAPER_ID)


@pytest.mark.parametrize("metadata", [
    '<meta name="citation_arxiv_id" content="2607.05147v2">',
    '<meta name="citation_pdf_url" content="https://arxiv.org/pdf/2607.05147v2">',
    '<link rel="canonical" href="https://arxiv.org/html/2607.05147v2">',
])
def test_conflicting_html_self_identifiers_fail(metadata):
    with pytest.raises(SourceError, match="version"):
        parse_structured_html(html(metadata=metadata), url=PAPER_URL, requested_id=PAPER_ID)


def test_invalid_html_falls_back_to_pdf_with_explicit_selection(tmp_path, monkeypatch):
    calls = []
    def download(url, **kwargs):
        calls.append(url)
        return Download(b"<title>Service unavailable</title>" if "/html/" in url else pdf_bytes(), url, "text/html")
    monkeypatch.setattr(source_tools, "download_public", download)
    source = library(tmp_path)
    result = source.prepare_source()
    assert calls == [PAPER_URL, "https://arxiv.org/pdf/" + PAPER_ID]
    assert result["coverage"]["source_selection"]["format"] == "pdf"
    assert "error or challenge" in result["coverage"]["source_selection"]["reason"]
    assert not (tmp_path / "source.html").exists()


def test_pdf_version_stamp_checked_even_when_response_url_matches():
    data = pdf_bytes(text="arXiv:2607.05147v2\n" + BODY)
    with pytest.raises(SourceError, match="version"):
        pdf_identity(data, PAPER_ID, "https://arxiv.org/pdf/" + PAPER_ID)
    assert pdf_identity(data, "2607.05147", "https://arxiv.org/pdf/2607.05147") == "2607.05147v2"


def test_math_has_one_alternative_and_equations_are_not_tables():
    content = ('<p id="math.p">The target <math alttext="M_{t}"><semantics><msub><mi>M</mi><mi>t</mi></msub>'
               '<annotation encoding="application/x-tex">M_{t}</annotation></semantics></math> uses '
               '<span class="katex"><span class="katex-mathml"><math><semantics><mi>x</mi>'
               '<annotation encoding="application/x-tex">x_{1}</annotation></semantics></math></span>'
               '<span class="katex-html">x1</span></span>.</p>'
               '<table class="ltx_equation" id="eq1"><tr><td><math alttext="x=1"><mi>x</mi><mo>=</mo><mn>1</mn></math></td><td>(1)</td></tr></table>')
    sections, coverage = parse_structured_html(html(content))
    assert next(s["text"] for s in sections if s["id"] == "math.p") == "The target M_{t} uses x_{1}."
    assert not coverage["tables"]
    assert next(s for s in sections if s["id"] == "eq1")["role"] == "equation"


def test_appendix_headings_paragraph_ids_anchors_and_references_are_retained():
    content = ('<section id="A1" class="ltx_appendix"><h2>Appendix A Counterexample</h2>'
               '<p id="A1.p1">The selection policy fails <span id="qualifier">without early stopping</span>.</p>'
               '<section id="A1.1"><h3>Proof</h3><p id="A1.1.p1">Conditions cannot be discarded.</p></section></section>'
               '<section class="ltx_bibliography" id="bib"><h2>References</h2><ul><li id="bib.1">Original reference.</li></ul></section>')
    sections, coverage = parse_structured_html(html(content))
    indexed = {s["id"]: s for s in sections}
    assert indexed["A1.p1"]["kind"] == indexed["A1.1.p1"]["kind"] == "appendix"
    assert indexed["A1.1.p1"]["parent_id"] == "A1.1"
    assert indexed["A1.p1"]["anchors"] == ["A1.p1", "qualifier"]
    assert indexed["bib.1"]["kind"] == "reference"
    assert coverage["references"] == [{"id": "bib.1", "text": "Original reference."}]
    assert len(coverage["section_inventory"]) == len(sections)


def table_markup(tag="td"):
    return (f'<table><tr><{tag} rowspan="2">Target</{tag}><{tag} rowspan="2">Drafter</{tag}><{tag} colspan="2">Math</{tag}></tr>'
            f'<tr><{tag}>GSM8K</{tag}><{tag}>MATH</{tag}></tr>'
            '<tr><td rowspan="2">Qwen3-4B</td><td>DFlash</td><td>5.40</td><td>4.85</td></tr>'
            '<tr><td>DSpark</td><td>6.11</td><td>5.70</td></tr></table>')


@pytest.mark.parametrize("tag", ["td", "th"])
def test_table_multilevel_headers_and_rowspan_conditions(tag):
    parsed = parse_table(BeautifulSoup(table_markup(tag), "html.parser").table, "T1")
    cell = next(c for c in parsed["rows"][3] if c["col"] == 2)
    assert cell["text"] == "6.11" and cell["row"] == 3
    assert cell["headers"] == ["Math", "GSM8K", "Qwen3-4B", "DSpark"]
    assert parsed["rows"][2][0]["rowspan"] == 2 and len(parsed["rows"][3]) == 3
    assert parsed["rows"][0][2]["colspan"] == 2
    assert all(c["header"] for row in parsed["rows"][:2] for c in row)


def test_table_caption_footnotes_and_short_cell_original_value():
    content = '<figure class="ltx_table" id="T1"><figcaption>Table 1. Validation only.</figcaption>' + table_markup() + '<div class="table-footnote">* Fixed training budget.</div></figure>'
    sections, coverage = parse_structured_html(html(content))
    table = coverage["tables"][0]
    assert table["id"] == table["section_id"] == "T1"
    assert table["caption"] == "Table 1. Validation only."
    assert table["footnotes"] == ["* Fixed training budget."]
    section = next(s for s in sections if s["id"] == "T1")
    assert section["table"] == table and section["table_id"] == "T1"
    assert "6.11" in section["text"] and "Fixed training budget" in section["text"]


@pytest.mark.parametrize("wrapper", ["", "tbody"])
@pytest.mark.parametrize("span", ["0", "16"])
def test_rowspan_clips_to_existing_rows_without_inventing_values(wrapper, span):
    # Reduced from DeepSeek 2609.19969v1 table S5.T3: Agentic declares 16
    # rows but only 15 remain in its tbody. Browsers end it at the last row.
    rows = (f'<tr><td rowspan="{span}">Agentic</td><td>Terminal-Bench 2.1</td><td>90.6</td></tr>'
            '<tr><td>ZeroBench-main</td><td>49.0</td></tr>')
    markup = f'<table><{wrapper}>{rows}</{wrapper}></table>' if wrapper else f'<table>{rows}</table>'
    table = parse_table(BeautifulSoup(markup, "html.parser").table, "T")
    assert len(table["rows"]) == 2
    assert table["rows"][0][0]["rowspan"] == 2
    assert [(c["col"], c["text"]) for c in table["rows"][1]] == [(1, "ZeroBench-main"), (2, "49.0")]
    assert table["rows"][1][-1]["headers"] == ["Agentic", "ZeroBench-main"]


@pytest.mark.parametrize("span", ["0", "8"])
def test_rowspan_stays_within_each_explicit_row_group(span):
    markup = ('<table><thead><tr><th>Category</th><th>Score</th></tr></thead>'
              f'<tbody><tr><th scope="rowgroup" rowspan="{span}">Agentic</th><td>90.6</td></tr>'
              '<tr><td>49.0</td></tr></tbody>'
              f'<tbody><tr><th scope="rowgroup" rowspan="{span}">Reasoning</th><td>65.6</td></tr>'
              '<tr><td>90.9</td></tr></tbody>'
              f'<tfoot><tr><th scope="rowgroup" rowspan="{span}">Summary</th><td>80.0</td></tr></tfoot></table>')
    table = parse_table(BeautifulSoup(markup, "html.parser").table, "T")
    assert [table["rows"][r][0]["rowspan"] for r in [1, 3, 5]] == [2, 2, 1]
    assert table["rows"][2][0]["headers"] == ["Score", "Agentic"]
    assert table["rows"][4][0]["headers"] == ["Score", "Reasoning"]
    assert [(c["col"], c["text"]) for c in table["rows"][5]] == [(0, "Summary"), (1, "80.0")]
    assert table["rows"][5][1]["headers"] == ["Score", "Summary"]


def test_rowspan_does_not_join_separate_implicit_row_groups():
    markup = ('<table><tr><td rowspan="0">Before</td><td>1</td></tr>'
              '<tbody><tr><td>Middle</td><td>2</td></tr></tbody>'
              '<tr><td>After</td><td>3</td></tr></table>')
    table = parse_table(BeautifulSoup(markup, "html.parser").table, "T")
    assert table["rows"][0][0]["rowspan"] == 1
    assert all(row[0]["col"] == 0 for row in table["rows"])
    assert table["rows"][2][1]["headers"] == ["After"]


def test_html_rowspan_overrun_keeps_html_and_table_evidence(tmp_path, monkeypatch):
    content = ('<figure class="ltx_table" id="T3"><figcaption>Agentic benchmarks.</figcaption>'
               '<table><tbody><tr><th>Category</th><th>Benchmark</th><th>DS-V4.1-Flash</th></tr>'
               '<tr><td rowspan="16">Agentic</td><td>Terminal-Bench 2.1</td><td>90.6</td></tr>'
               '<tr><td>ZeroBench-main</td><td>49.0</td></tr></tbody></table></figure>')
    calls = install_html(monkeypatch, html(content))
    source = library(tmp_path)
    result = source.prepare_source()
    assert calls == [PAPER_URL]
    assert result["source_manifest"]["path"] == "source.html"
    table = result["coverage"]["tables"][0]
    assert len(table["rows"]) == 3
    assert table["rows"][2][-1]["headers"] == ["DS-V4.1-Flash", "Agentic", "ZeroBench-main"]
    assert "49.0" in source.read_source(section_id="T3")["text"]


@pytest.mark.parametrize("markup", [
    '<table><tr><td rowspan="100000000">X</td></tr></table>',
    '<table><tbody><tr><td rowspan="1001">X</td></tr></tbody></table>',
    '<table><tr><td colspan="-1">X</td></tr></table>',
    '<table><tr><td rowspan="bogus">X</td></tr></table>',
    '<table><tr><td>X</td><td rowspan="2">Y</td></tr><tr><td colspan="2">Z</td></tr></table>',
    '<table><tr><td headers="missing">1</td></tr></table>',
])
def test_malformed_or_unbounded_table_fails_closed(markup):
    with pytest.raises(SourceError):
        parse_table(BeautifulSoup(markup, "html.parser").table, "T")


@pytest.mark.parametrize("markup", [
    '<table><tr><td colspan="1000">X</td><td>Y</td></tr></table>',
    '<table><tr><td rowspan="101" colspan="1000">X</td></tr>' + '<tr></tr>' * 100 + '</table>',
])
def test_rowspan_clipping_preserves_grid_resource_bounds(markup):
    with pytest.raises(SourceError, match="grid exceeds"):
        parse_table(BeautifulSoup(markup, "html.parser").table, "T")


def test_svg_objects_and_relative_images_are_main_source_visuals(tmp_path, monkeypatch):
    calls = install_html(monkeypatch, figure_html())
    source = library(tmp_path)
    result = source.prepare_source()
    figure = result["coverage"]["figures"][0]
    assert figure["images"][0]["url"] == "https://arxiv.org/html/2607.05147v1/plot.svg"
    visual = source.prepare_visual("F1")
    assert visual["source_id"] == "main" and visual["section_id"] == "F1"
    assert visual["mime_type"] == "image/png"
    image = (tmp_path / visual["path"]).read_bytes()
    assert image.startswith(b"\x89PNG") and hashlib.sha256(image).hexdigest() == visual["sha256"]
    assert len(source.documents) == 1 and len(source.visual_assets) == 1
    assert not source.visual_observations
    assert source.prepare_visual("F1") == visual and len(calls) == 2
    assert len(source.export_checkpoint()["file_sha256"]) == 3
    assert "Accuracy under" in source.read_source(section_id="F1")["text"]


@pytest.mark.parametrize("bad", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://127.0.0.1/a"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect onload="alert(1)"/></svg>',
    b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com/a";</style></svg>',
    b'<html><title>Error</title></html>',
])
def test_visual_parser_rejects_active_svg_and_error_images(bad):
    with pytest.raises(SourceError):
        render_image(bad, "image/svg+xml")


def test_image_dimensions_are_bounded():
    data = b'<svg xmlns="http://www.w3.org/2000/svg" width="1000000" height="1000000"><rect width="1000000" height="1000000"/></svg>'
    png, format = render_image(data, "image/svg+xml")
    assert format == "svg"
    with fitz.open(stream=png, filetype="png") as image:
        assert image[0].rect.width <= 4096 and image[0].rect.height <= 4096


def test_multiple_image_panels_are_all_fetched_and_composed(tmp_path, monkeypatch):
    data = html('<figure id="F1"><img src="2607.05147v1/a.svg"><img src="2607.05147v1/b.svg"><figcaption>Both panels</figcaption></figure>')
    calls = install_html(monkeypatch, data)
    source = library(tmp_path)
    visual = source.prepare_visual("F1")
    assert len(calls) == 3
    with fitz.open(tmp_path / visual["path"]) as image:
        assert image[0].rect.width == image[0].rect.height


@pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "file:///etc/passwd", "https://arxiv.org/html/2607.05147v2/plot.svg"])
def test_bad_image_scheme_or_version_rejected_before_network(tmp_path, monkeypatch, url):
    install_html(monkeypatch, html(f'<figure id="F1"><img src="{url}"></figure>'))
    source = library(tmp_path)
    source.prepare_source()
    monkeypatch.setattr(source_tools, "download_public", lambda *args, **kwargs: pytest.fail("Rejected before network"))
    with pytest.raises(SourceError):
        source.prepare_visual("F1")


def test_pdf_gap_and_vector_charts_get_renderable_page_ids(tmp_path):
    path = tmp_path / "input.pdf"
    path.write_bytes(pdf_bytes(gap=True, vector=True))
    sections, coverage = parse_structured_pdf(path)
    assert len(sections) == 2 and coverage["missing_text_pages"] == [2]
    assert coverage["full_text"] is False and coverage["text_page_ratio"] == 0.5
    assert [f["id"] for f in coverage["figures"]] == ["page:1", "page:2"]
    source = SourceLibrary(tmp_path / "work", {"id": "upload", "source_type": "pdf", "storage_path": str(path)},
                           "deeppapernote", workflow_mode="guided", rules_version=STRUCTURED_RULES_VERSION)
    visual = source.prepare_visual("page:2")
    assert visual["page"] == 2 and visual["section_id"] == "p2"
    assert source.documents["main"].coverage["full_text"] is False
    assert source.read_source(section_id="p2")["text"].strip() == "Caption only"


def test_image_only_pdf_is_honestly_partial_and_can_be_inspected(tmp_path):
    path = tmp_path / "blank.pdf"
    path.write_bytes(pdf_bytes(text=""))
    sections, coverage = parse_structured_pdf(path)
    assert not coverage["full_text"] and not coverage["usable_text"]
    assert coverage["missing_text_pages"] == [1]
    assert coverage["figures"][0]["id"] == "page:1"


def accepted_observation(visual):
    return {"id": "O1", "figure_id": visual["figure_id"], "image_sha256": visual["sha256"], "section_id": visual["section_id"],
            "observation": "The panel contains a red rectangle.", "readable": True,
            "conditions": "Visual fixture only", "uncertainties": [], "claim_ids": ["C1"]}


def test_checkpoint_relocation_restores_visual_hashes_and_separate_observations(tmp_path, monkeypatch):
    install_html(monkeypatch, figure_html())
    source = library(tmp_path / "first")
    visual = source.prepare_visual("F1")
    source.visual_observations["O1"] = accepted_observation(visual)
    original_text = source.read_source()["text"]
    source._save()
    checkpoint = source.export_checkpoint()
    shutil.copytree(source.work_dir, tmp_path / "resumed")
    resumed = library(tmp_path / "resumed")
    resumed.restore_checkpoint(checkpoint, digest(checkpoint))
    assert resumed.visual_observations == source.visual_observations
    assert resumed.prepared_visuals == source.prepared_visuals
    assert resumed.read_source()["text"] == original_text
    assert "red rectangle" not in original_text
    assert resumed.fingerprint() == source.fingerprint()
    assert resumed.prepare_visual("F1") == visual


@pytest.mark.parametrize("what", ["png", "svg", "html", "observation_hash", "observation_section", "unknown_figure", "extra_file", "version"])
def test_restore_checks_all_representations_visuals_and_observation_identity(tmp_path, monkeypatch, what):
    install_html(monkeypatch, figure_html())
    source = library(tmp_path)
    visual = source.prepare_visual("F1")
    source.visual_observations["O1"] = accepted_observation(visual)
    checkpoint = source.export_checkpoint()
    changed = copy.deepcopy(checkpoint)
    if what in {"png", "svg", "html"}:
        path = next(name for name in changed["file_sha256"] if name.endswith("." + what))
        (tmp_path / path).write_bytes(b"tampered")
    if what == "observation_hash": changed["visual_observations"]["O1"]["image_sha256"] = "f" * 64
    if what == "observation_section": changed["visual_observations"]["O1"]["section_id"] = "S1.p1"
    if what == "unknown_figure": changed["prepared_visuals"]["F1"]["figure_id"] = "F2"
    if what == "extra_file": changed["file_sha256"]["unreferenced"] = "f" * 64
    if what == "version": changed["documents"][0]["coverage"]["representations"][0]["arxiv_id"] = "2607.05147v2"
    previous = source.visual_observations.copy()
    with pytest.raises(SourceError):
        source.restore_checkpoint(changed)
    assert source.visual_observations == previous


def test_checkpoint_payload_hash_and_rules_are_bound(tmp_path, monkeypatch):
    install_html(monkeypatch)
    source = library(tmp_path)
    source.prepare_source()
    checkpoint = source.export_checkpoint()
    changed = copy.deepcopy(checkpoint)
    changed["documents"][0]["sections"][0]["text"] = "Forged"
    with pytest.raises(SourceError, match="hash"):
        source.restore_checkpoint(changed, digest(checkpoint))
    with pytest.raises(SourceError, match="different parsing rules"):
        SourceLibrary(tmp_path, source.paper, source.skill_id)


def test_immutable_assets_refuse_overwrite_and_symlink(tmp_path):
    first = immutable_write(tmp_path, "source-assets/original.bin", b"original")
    assert immutable_write(tmp_path, "source-assets/original.bin", b"original") == first
    with pytest.raises(SourceError, match="Immutable"):
        immutable_write(tmp_path, "source-assets/original.bin", b"replacement")
    (tmp_path / "alias").symlink_to(tmp_path / "source-assets", target_is_directory=True)
    with pytest.raises(SourceError, match="escapes"):
        immutable_write(tmp_path, "alias/out.bin", b"data")
    with pytest.raises(SourceError, match="escapes"):
        immutable_write(tmp_path, "../outside", b"data")


def test_concurrent_preparation_is_single_download_and_atomic_checkpoint(tmp_path, monkeypatch):
    calls = install_html(monkeypatch, figure_html())
    source = library(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: source.prepare_visual("F1"), range(2)))
    assert results[0] == results[1] and len(calls) == 2
    resumed = library(tmp_path)
    assert resumed.export_checkpoint() == source.export_checkpoint()


@pytest.mark.parametrize("supplied,expected", [("arxiv:" + PAPER_ID, "Measured method"), ("arXiv " + PAPER_ID, "Measured method"),
                                               (PAPER_ID, "Measured method"), ("Curated user title", "Curated user title")])
def test_document_html_title_replaces_only_placeholders(tmp_path, monkeypatch, supplied, expected):
    install_html(monkeypatch)
    source = library(tmp_path)
    source.paper["title"] = supplied
    assert source.prepare_source()["title"] == expected


def test_pdf_metadata_title_replaces_upload_filename(tmp_path):
    with fitz.open(stream=pdf_bytes(), filetype="pdf") as pdf:
        pdf.set_metadata({"title": "Measured PDF paper title"})
        pdf.save(tmp_path / "upload.pdf")
    source = SourceLibrary(tmp_path / "work", {"id": "upload", "title": "upload.pdf", "source_type": "pdf",
                                              "storage_path": str(tmp_path / "upload.pdf")}, "deeppapernote",
                           rules_version=STRUCTURED_RULES_VERSION)
    assert source.prepare_source()["title"] == "Measured PDF paper title"


def test_algorithm_figure_preserves_pseudocode_without_requiring_visual():
    data = html('<figure id="alg1" class="ltx_float ltx_float_algorithm"><figcaption>Algorithm 1 Scheduler</figcaption>'
                '<div>For each candidate prefix, retain the accepted token.</div><div>Return the selected prefix.</div></figure>')
    sections, coverage = parse_structured_html(data)
    algorithm = next(s for s in sections if s["id"] == "alg1")
    assert algorithm["role"] == "algorithm" and "Return the selected prefix" in algorithm["text"]
    assert coverage["figures"] == []


def test_svg_allows_bounded_embedded_raster_and_local_clip_references():
    png, _ = render_image(SVG, "image/svg+xml")
    data = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><defs><clipPath id="clip"><rect width="40" height="20"/></clipPath></defs>'
            '<image width="40" height="20" style="clip-path:url(#clip)" href="data:image/png;base64,'
            + base64.b64encode(png).decode() + '"/></svg>').encode()
    rendered, format = render_image(data, "image/svg+xml")
    assert format == "svg" and rendered.startswith(b"\x89PNG")


def test_explicit_row_header_is_not_applied_to_later_data_rows():
    table = BeautifulSoup('<table><tr><th>Method</th><th>Accuracy</th></tr>'
                          '<tr><th>A</th><td>75</td></tr><tr><th>B</th><td>80</td></tr></table>', 'html.parser').table
    rows = parse_table(table, "T")["rows"]
    assert rows[1][1]["headers"] == ["Accuracy", "A"]
    assert rows[2][1]["headers"] == ["Accuracy", "B"]


def test_original_image_failure_uses_same_version_pdf_caption_page(tmp_path, monkeypatch):
    calls = []
    data = html('<figure id="F7"><img src="2607.05147v1/missing.png"><figcaption>Figure 7: Throughput vs. TPS. Measured traffic only.</figcaption></figure>')
    pdf = pdf_bytes(text="Figure 7 | Throughput vs. TPS. Measured traffic only.\n" + BODY)
    def download(url, **kwargs):
        calls.append(url)
        if url.endswith("missing.png"):
            raise SourceError("Source returned HTTP 404")
        return Download(data if "/html/" in url else pdf, url, "text/html" if "/html/" in url else "application/pdf")
    monkeypatch.setattr(source_tools, "download_public", download)
    source = library(tmp_path)
    visual = source.prepare_visual("F7")
    assert calls[-1] == "https://arxiv.org/pdf/" + PAPER_ID
    assert visual["figure_id"] == visual["section_id"] == "F7" and visual["page"] == 1
    document = source.documents["main"]
    assert document.path == "source.html" and source.read_source(section_id="F7")["text"].startswith("Figure 7:")
    assert next(s for s in document.sections if s["id"] == "F7")["page"] == 1
    assert {r["format"] for r in document.coverage["representations"]} == {"html", "pdf"}
    assert {r["arxiv_id"] for r in document.coverage["representations"]} == {PAPER_ID}
    assert len(source.documents) == 1 and source.is_main_source("copy", "https://example.com/copied.pdf", hashlib.sha256(pdf).hexdigest())
    source.restore_checkpoint(source.export_checkpoint())


def test_fallback_pdf_wrong_version_fails_before_persistence(tmp_path, monkeypatch):
    def download(url, **kwargs):
        if url.endswith(".svg"):
            raise SourceError("Source returned HTTP 404")
        if "/html/" in url:
            return Download(figure_html(), url, "text/html")
        return Download(pdf_bytes(text="arXiv:2607.05147v2\n" + BODY), url, "application/pdf")
    monkeypatch.setattr(source_tools, "download_public", download)
    source = library(tmp_path)
    with pytest.raises(SourceError, match="version"):
        source.prepare_visual("F1")
    assert not source.prepared_visuals and not (tmp_path / "source.pdf").exists()


@pytest.mark.parametrize("text", ["As noted in Figure 7: Throughput vs. TPS. Measured traffic only.", "Figure 7 | Entirely different figure caption."])
def test_fallback_does_not_guess_from_inline_reference_or_caption_number(tmp_path, text):
    path = tmp_path / "source.pdf"
    path.write_bytes(pdf_bytes(text=text + "\n" + BODY))
    with pytest.raises(SourceError, match="unambiguous"):
        figure_pdf_page(path, "Figure 7: Throughput vs. TPS. Measured traffic only.")


def test_restore_detaches_input_payload(tmp_path, monkeypatch):
    install_html(monkeypatch)
    source = library(tmp_path)
    source.prepare_source()
    checkpoint = source.export_checkpoint()
    source.restore_checkpoint(checkpoint)
    checkpoint["documents"][0]["sections"][0]["text"] = "Mutated caller data"
    assert source.documents["main"].sections[0]["text"] != "Mutated caller data"


@pytest.mark.parametrize("body", [
    f'<h2>Abstract</h2><p>{BODY * 4}</p>',
    f'<section><h2>Abstract</h2><p>{BODY * 4}</p></section><section><h2>Introduction</h2><p>Body missing.</p></section>',
    f'<h2>Abstract</h2><p>{BODY * 4}</p><h2>Introduction</h2><p>Body missing.</p>',
    f'<section class="ltx_bibliography"><h2>References</h2><p>{BODY * 4}</p></section>',
])
def test_long_abstract_or_bibliography_does_not_substitute_for_body(body):
    with pytest.raises(SourceError, match="body"):
        parse_structured_html(f'<article><h1>A scholarly title</h1>{body}</article>'.encode())


def test_missing_cited_bibliography_or_truncated_article_is_incomplete():
    with pytest.raises(SourceError, match="bibliography"):
        parse_structured_html(html('<p>Prior research <a href="#bib.missing">[1]</a>.</p>'))
    with pytest.raises(SourceError, match="truncated"):
        parse_structured_html(html().replace(b"</article></body></html>", b""))
