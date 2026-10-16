"""Versioned, offline DOM parsing and bounded source-image rendering.

The legacy parser intentionally lives in source_tools. These helpers are used
only by source-structured-v3; they do not contact a model or a database.
Table coordinates are zero based and identify originating cells, not copies of
spanned values. Header chains include column groups and row labels in DOM order.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .source_tools import MAX_DOWNLOAD_BYTES, MAX_TEXT_BYTES, SourceError, normalize_arxiv, parse_pdf

STRUCTURED_RULES_VERSION = "source-structured-v3"
RENDER_RULES_VERSION = "cairo-svg-v1"
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_EDGE = 4096
MAX_VISUAL_BYTES = 12 * 1024 * 1024
MAX_RENDERED_BYTES = 5 * 1024 * 1024
HEADINGS = {f"h{i}" for i in range(1, 7)}


def _base(identity: str) -> str:
    return re.sub(r"v\d+$", "", identity, flags=re.I).lower()


def resolve_identity(requested_id: str, response_url: str, declared: list[str] = ()) -> str:
    """Bind official response URL and document self-identifiers to one version.

    Bibliography links are never declarations. An unversioned request requires
    an explicit version in the final response URL or the paper's own metadata.
    """
    requested = normalize_arxiv(requested_id)
    response = normalize_arxiv(response_url)
    identities = [response, *(normalize_arxiv(value) for value in declared)]
    if any(_base(value) != _base(requested) for value in identities):
        raise SourceError("Source arXiv identity does not match the requested paper")
    versions = {value.lower() for value in identities if re.search(r"v\d+$", value, re.I)}
    if re.search(r"v\d+$", requested, re.I):
        if versions and versions != {requested.lower()}:
            raise SourceError("Source arXiv version does not match the requested version")
    if len(versions) != 1:
        raise SourceError("Source arXiv version could not be resolved unambiguously")
    return versions.pop()


def html_identity(soup: BeautifulSoup, requested_id: str, response_url: str) -> str:
    declared = []
    for node in soup.select('meta[name="citation_arxiv_id"], meta[name="citation_pdf_url"], link[rel="canonical"]'):
        value = node.get("content") or node.get("href")
        if value:
            declared.append(urllib.parse.urljoin(response_url, value) if value.startswith("/") else value)
    # Current arXiv HTML has no citation meta tags: its site header links are
    # the self-identifiers. Restrict this to chrome outside the paper article.
    for node in soup.select('header a[href], .arxiv-html-header a[href], a.header-button[href]'):
        if node.find_parent("article"):
            continue
        url = urllib.parse.urljoin(response_url, node["href"])
        if re.match(r"/(abs|pdf|html)/", urllib.parse.urlsplit(url).path):
            declared.append(url)
    return resolve_identity(requested_id, response_url, declared)


def pdf_identity(data: bytes, requested_id: str, response_url: str) -> str:
    import fitz

    if not data.startswith(b"%PDF-"):
        raise SourceError("arXiv did not return a PDF")
    try:
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass or not len(pdf):
                raise SourceError("PDF identity is unavailable")
            # Only the first-page arXiv stamp and document metadata are self
            # declarations; cited arXiv papers elsewhere cannot change identity.
            text = pdf[0].get_text() + " " + " ".join(str(v) for v in pdf.metadata.values() if v)
            stamps = re.findall(r"arXiv\s*:\s*((?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})v\d+)", text, re.I)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("PDF identity could not be read") from exc
    return resolve_identity(requested_id, response_url, list(dict.fromkeys(stamps)))


def _text(node) -> str:
    """Choose one math alternative and preserve normal inline punctuation."""
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name in {"script", "style", "annotation", "annotation-xml", "nav", "footer"}:
        return ""
    classes = node.get("class", [])
    if "MathJax_Preview" in classes or "katex-html" in classes or "MJX_Assistive_MathML" in classes:
        return ""
    if node.name == "math":
        annotation = node.find("annotation", attrs={"encoding": re.compile(r"(tex|latex)", re.I)})
        value = node.get("alttext") or (annotation.get_text() if annotation else None)
        if value:
            return value.strip()
    if node.name in {"img", "object"}:
        return node.get("alt", "")
    if node.name == "br":
        return "\n"
    value = "".join(_text(child) for child in node.children)
    return value + ("\n" if node.name in {"p", "div", "li", "tr", "pre", *HEADINGS} else "")


def text_of(node) -> str:
    return " ".join(_text(node).split())


def _span(cell: Tag, name: str, rows_left: int) -> int:
    try:
        value = int(cell.get(name, 1))
    except (ValueError, TypeError) as exc:
        raise SourceError("HTML table has an invalid cell span") from exc
    if name == "rowspan" and value == 0:
        value = rows_left
    if not 1 <= value <= 1000:
        raise SourceError("HTML table cell span exceeds its bound")
    # Browsers clip row spans at the end of their row group (including an
    # implicit tbody). Do not create absent rows or let labels leak into the
    # next group. Validate the declared span before clipping its effective size.
    return min(value, rows_left) if name == "rowspan" else value


def parse_table(table: Tag, table_id: str, caption: str = "", footnotes: list[str] | None = None) -> dict:
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if len(rows) > 10000:
        raise SourceError("HTML table row count exceeds its bound")
    groups = [row.find_parent(["thead", "tbody", "tfoot", "table"]) for row in rows]
    group_ends = [0] * len(rows)
    for r in range(len(rows) - 1, -1, -1):
        # html.parser leaves direct tr children unwrapped. Consecutive rows
        # with the same parent group form the browser's implicit tbody.
        group_ends[r] = r + 1 if r == len(rows) - 1 or groups[r] is not groups[r + 1] else group_ends[r + 1]
    grid, result, nodes = {}, [], {}
    for r, row in enumerate(rows):
        output, col = [], 0
        for node in row.find_all(["td", "th"], recursive=False):
            while (r, col) in grid:
                col += 1
            rowspan = _span(node, "rowspan", group_ends[r] - r)
            colspan = _span(node, "colspan", len(rows) - r)
            if r + rowspan > len(rows) or col + colspan > 1000 or len(grid) + rowspan * colspan > 100000:
                raise SourceError("HTML table grid exceeds its bound")
            cell = {"text": text_of(node), "row": r, "col": col, "rowspan": rowspan,
                    "colspan": colspan, "header": node.name == "th" or node.find_parent("thead") is not None,
                    "headers": []}
            if node.get("id"):
                cell["id"] = node["id"]
            nodes[id(cell)] = node
            for rr in range(r, r + rowspan):
                for cc in range(col, col + colspan):
                    if (rr, cc) in grid:
                        raise SourceError("HTML table has overlapping cell spans")
                    grid[rr, cc] = cell
            output.append(cell)
            col += colspan
        result.append(output)
    cells = [cell for row in result for cell in row]
    # LaTeXML commonly emits td even for multi-tier headers. Infer only the
    # leading label rows before any numeric measurement; retain inference data.
    numeric = re.compile(r"^[\s$({]*[+−\-]?(?:\d|\.\d)|^[<>≤≥≈]", re.U)
    leading_rows = 0
    for row in result:
        if not row or any(numeric.match(cell["text"]) for cell in row if cell["text"]):
            break
        if not all(cell["header"] for cell in row) and leading_rows >= 4:
            break
        leading_rows += 1
    # Only infer column labels when there actually is a body row.
    if leading_rows < len(result):
        for row in result[:leading_rows]:
            for cell in row:
                if not cell["header"]:
                    cell.update(header=True, header_inferred=True)
    column_rows = {r for r, row in enumerate(result) if row and all(
        cell["header"] and nodes[id(cell)].get("scope") not in {"row", "rowgroup"} for cell in row)}
    by_id = {cell["id"]: cell for cell in cells if "id" in cell}
    for cell in cells:
        node = nodes[id(cell)]
        explicit = node.get("headers", [])
        if isinstance(explicit, str):
            explicit = explicit.split()
        if any(value not in by_id for value in explicit):
            raise SourceError("HTML table refers to an unknown header")
        chain = []
        # Include group + leaf column headers, then same-row labels. Rowspans
        # carry model/dataset conditions to every row they cover.
        for header in cells:
            if header is cell or not header["text"]:
                continue
            scope = nodes[id(header)].get("scope", "")
            overlaps_col = header["col"] < cell["col"] + cell["colspan"] and cell["col"] < header["col"] + header["colspan"]
            overlaps_row = header["row"] <= cell["row"] < header["row"] + header["rowspan"]
            column = header["header"] and (scope in {"col", "colgroup"} or header["row"] in column_rows) and header["row"] < cell["row"] and overlaps_col
            label = overlaps_row and header["col"] + header["colspan"] <= cell["col"] and (
                scope in {"row", "rowgroup"} or (not numeric.match(header["text"]) and header["row"] not in column_rows))
            if column or label or header.get("id") in explicit:
                chain.append(header)
        cell["headers"] = [header["text"] for header in chain]
    return {"id": table_id, "section_id": table_id, "caption": caption, "rows": result,
            "footnotes": footnotes or []}


def parse_structured_html(data: bytes, source_id: str = "main", *, url: str | None = None,
                          requested_id: str | None = None) -> tuple[list[dict], dict]:
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise SourceError("HTML source exceeds the size limit")
    soup = BeautifulSoup(data, "html.parser")
    title = text_of(soup.title).lower()
    if re.search(r"\b(error|not found|access denied|captcha|service unavailable|just a moment)\b", title):
        raise SourceError("HTML response is an error or challenge page")
    article = soup.select_one("article.ltx_document, article, main .ltx_document, .ltx_document")
    if article is None or article.find(["p", "section"]) is None:
        raise SourceError("HTML full text has no scholarly article structure")
    if article.name == "article" and not re.search(br"</article\s*>", data, re.I):
        raise SourceError("HTML article is truncated or incomplete")
    body_headings = [node for node in article.find_all(["h2", "h3", "h4", "h5", "h6"])
                     if not re.fullmatch(r"(?:abstract|references|bibliography|keywords|authors)", text_of(node), re.I)]
    if not body_headings:
        raise SourceError("HTML full text has no body section structure beyond its abstract")
    dom_ids = {node["id"] for node in article.find_all(id=True)}
    if any(urllib.parse.unquote(node["href"][1:]) not in dom_ids for node in article.select('a[href^="#bib"]')):
        raise SourceError("HTML bibliography is incomplete: cited reference targets are missing")
    resolved = html_identity(soup, requested_id, url) if requested_id and url else None
    for node in article.select("script, style, nav, footer, form, dialog, .ltx_page_footer, .ltx_page_header"):
        node.decompose()
    sections, inventory, figures, tables, references = [], [], [], [], []
    used, counter = set(), 0

    def identity(node: Tag, prefix: str) -> str:
        nonlocal counter
        counter += 1
        value = node.get("id") or f"{prefix}:{counter}"
        if value in used:
            raise SourceError("HTML source contains duplicate section identities")
        used.add(value)
        return value

    def add(node: Tag, parent: str | None, title: str, kind: str, *, text: str | None = None, **extra) -> dict:
        entry = {"id": identity(node, kind), "source_id": source_id, "text": text_of(node) if text is None else text,
                 "section": title, "kind": kind, "anchors": list(dict.fromkeys(
                     [n["id"] for n in [node, *node.find_all(id=True)] if n.get("id")]))}
        if parent:
            entry["parent_id"] = parent
        entry.update(extra)
        sections.append(entry)
        inventory.append({"id": entry["id"], "title": title or entry["id"], "kind": kind,
                          **({"parent_id": parent} if parent else {})})
        return entry

    def images(node: Tag) -> list[dict]:
        output = []
        for image in ([node] if node.name in {"img", "object"} else node.find_all(["img", "object"])):
            raw = image.get("src") if image.name == "img" else image.get("data")
            if raw:
                output.append({"url": urllib.parse.urljoin(url or "", raw), "alt": image.get("alt", "")})
        return output

    def visit(node: Tag, parent: str | None = None, title: str = "", kind: str = "body") -> None:
        classes = node.get("class", [])
        if node.name == "section" or "ltx_abstract" in classes or "ltx_bibliography" in classes:
            heading = node.find(list(HEADINGS), recursive=False)
            current_title = text_of(heading) if heading else ("Abstract" if "ltx_abstract" in classes else title)
            current_kind = ("reference" if "ltx_bibliography" in classes or re.match(r"^(references|bibliography)$", current_title, re.I)
                            else "appendix" if "ltx_appendix" in classes or re.match(r"^appendi(x|ces)\b", current_title, re.I)
                            else "abstract" if "ltx_abstract" in classes or re.fullmatch(r"Abstract", current_title, re.I) else kind)
            section = add(node, parent, current_title, current_kind, text=current_title, role="heading")
            for child in node.children:
                if isinstance(child, Tag) and child is not heading:
                    visit(child, section["id"], current_title, current_kind)
            return
        if "ltx_bibitem" in classes or (kind == "reference" and node.name == "li"):
            entry = add(node, parent, title, "reference")
            references.append({"id": entry["id"], "text": entry["text"]})
            return
        if "ltx_equation" in classes or "ltx_equationgroup" in classes or (node.name == "math" and node.get("display") == "block"):
            add(node, parent, title, kind, role="equation")
            return
        is_table = node.name == "table" or "ltx_table" in classes
        if is_table:
            if node.name != "table" and len(node.find_all("table")) > 1:
                raise SourceError("HTML table wrapper contains multiple tables; a complete PDF representation is required")
            table = node if node.name == "table" else node.find("table")
            if table is not None:
                caption_node = node.find(["caption", "figcaption"])
                caption = text_of(caption_node)
                notes = [text_of(n) for n in node.select(".ltx_note, .ltx_tablenotes, .table-footnote, tfoot")]
                entry = add(node, parent, title, kind, text="", role="table")
                parsed = parse_table(table, entry["id"], caption, notes)
                entry.update(table_id=entry["id"], table=parsed)
                entry["text"] = "\n".join(filter(None, [caption, *(" | ".join(cell["text"] for cell in row) for row in parsed["rows"]), *notes]))
                tables.append(parsed)
                return
        if node.name == "figure" or "ltx_figure" in classes or node.name in {"img", "object"}:
            caption = text_of(node.find("figcaption"))
            assets = images(node)
            if not assets and (any("algorithm" in value for value in classes) or re.match(r"^Algorithm\s", caption, re.I)):
                add(node, parent, title, kind, role="algorithm")
                return
            entry = add(node, parent, title, kind, text=caption or text_of(node), role="figure")
            figures.append({"id": entry["id"], "section_id": entry["id"], "caption": caption, "images": assets})
            return
        if node.name in HEADINGS:
            add(node, parent, text_of(node), kind, role="heading")
            return
        if node.name in {"p", "pre", "blockquote", "dt", "dd"} or (node.name == "li" and not node.find(["p", "figure", "table"])):
            entry = add(node, parent, title, kind)
            assets = images(node)
            if assets:
                figures.append({"id": entry["id"], "section_id": entry["id"], "caption": entry["text"], "images": assets})
            return
        # Preserve direct prose in generic wrappers (including algorithms and
        # author blocks) without duplicating descendants handled above.
        pending = []
        def flush() -> None:
            if pending:
                text = " ".join("".join(pending).split())
                if text:
                    add(soup.new_tag("span"), parent, title, kind, text=text)
                pending.clear()
        for child in node.children:
            if isinstance(child, NavigableString):
                if not isinstance(child, Comment):
                    pending.append(str(child))
            elif isinstance(child, Tag):
                if child.name in HEADINGS - {"h1"}:
                    flush()
                    title = text_of(child)
                    kind = ("abstract" if re.fullmatch(r"abstract", title, re.I) else "reference" if re.fullmatch(r"references|bibliography", title, re.I)
                            else "appendix" if re.match(r"appendi(x|ces)\b", title, re.I) else "body" if kind in {"abstract", "reference"} else kind)
                    heading = add(child, parent, title, kind, role="heading")
                    parent = heading["id"]
                elif child.name in {"span", "a", "em", "strong", "b", "i", "math", "cite", "sup", "sub"} and not child.find(["p", "table", "figure"]):
                    pending.append(_text(child))
                else:
                    flush()
                    visit(child, parent, title, kind)
        flush()

    visit(article)
    corpus = "\n\n".join(section["text"] for section in sections)
    body_text = "\n".join(section["text"] for section in sections if section["kind"] != "reference")
    substantive_body = "\n".join(section["text"] for section in sections
                                  if section["kind"] in {"body", "appendix"} and section.get("role") != "heading")
    if len(substantive_body.strip()) < 800:
        raise SourceError("HTML full text has insufficient body content beyond the abstract and references")
    if len(body_text.strip()) < 800:
        raise SourceError("HTML full text is unavailable or too short for a complete reading")
    if len(corpus.encode()) > MAX_TEXT_BYTES:
        raise SourceError("HTML extracted text exceeds the size limit")
    lower = corpus.lower()
    coverage = {"full_text": True, "usable_text": True, "status": "complete", "parser": "html-structured-dom-v3",
                "characters": len(corpus), "body_bytes": len(body_text.encode()), "page_count": None,
                "missing_text_pages": [], "text_page_ratio": None, "abstract": "abstract" in lower,
                "related_work": any(s in lower for s in ("related work", "background")),
                "method": any(s in lower for s in ("method", "approach", "algorithm")),
                "results": any(s in lower for s in ("result", "experiment", "evaluation")),
                "warnings": ["Figures require actual visual inspection"] if figures else [],
                "source_selection": {"format": "html", "reason": "Validated structured HTML full text",
                                     "requested_id": requested_id, "resolved_id": resolved},
                "section_inventory": inventory, "figures": figures, "tables": tables, "references": references,
                "representations": [], "document_title": text_of(article.select_one("h1.ltx_title_document") or article.find("h1")) or text_of(soup.title)}
    return sections, coverage


def parse_structured_pdf(path: Path, source_id: str = "main") -> tuple[list[dict], dict]:
    import fitz

    sections, parser, warnings = parse_pdf(path)
    missing, figures = [], []
    reference_starts = [i for i, section in enumerate(sections) if re.search(r"(?mi)^\s*(?:references|bibliography)\s*$", section["text"])]
    reference_start = reference_starts[-1] if reference_starts else None
    appendix_starts = [i for i, section in enumerate(sections) if re.search(r"(?mi)^\s*appendi(?:x|ces)\b", section["text"][:600])]
    appendix_start = next((i for i in appendix_starts if reference_start is None or i > reference_start), None)
    references = []
    with fitz.open(path) as pdf:
        document_title = (pdf.metadata.get("title") or "").strip()
        for section, page in zip(sections, pdf):
            number = section["page"]
            section.update(source_id=source_id, section=f"Page {number}", kind="body", anchors=[])
            if appendix_start is not None and number - 1 >= appendix_start:
                section.update(kind="appendix", section=f"Appendix · Page {number}")
            # Only omit reference-only pages from model extraction when both
            # boundaries are explicit. Ambiguous tails remain fully read.
            if reference_start is not None and appendix_start is not None and reference_start < number - 1 < appendix_start:
                section.update(kind="reference", section=f"References · Page {number}")
            if reference_start is not None and number - 1 >= reference_start and (appendix_start is None or number - 1 < appendix_start):
                reference_text = section["text"]
                if number - 1 == reference_start:
                    match = re.search(r"(?mi)^\s*(?:references|bibliography)\s*$", reference_text)
                    reference_text = reference_text[match.end():]
                references.append({"id": f"pdf-references-{number}", "section_id": section["id"], "page": number,
                                   "text": reference_text, "boundary_explicit": appendix_start is not None})
            if section["text_characters"] < 80:
                missing.append(number)
            # Vector-only charts/tables have no embedded raster image. Drawing
            # paths and caption markers conservatively require a visual check.
            has_visual = bool(section["image_count"] or page.get_drawings() or re.search(r"\b(?:Figure|Table|Fig\.)\s*\d", section["text"]))
            if number in missing or (has_visual and section["kind"] != "reference"):
                figures.append({"id": f"page:{number}", "section_id": section["id"], "caption": f"PDF page {number}",
                                "images": [], "page": number, "text_deficient": number in missing})
    if missing:
        warnings.append("Pages without reliable text: " + ", ".join(map(str, missing)))
    corpus = "\n\n".join(section["text"] for section in sections)
    lower = corpus.lower()
    return sections, {"full_text": not missing, "usable_text": bool(corpus.strip()), "status": "partial" if missing else "complete",
                      "parser": parser + "-v3", "characters": len(corpus), "body_bytes": len(corpus.encode()),
                      "page_count": len(sections), "missing_text_pages": missing,
                      "text_page_ratio": (len(sections) - len(missing)) / len(sections) if sections else 0,
                      "abstract": "abstract" in lower, "related_work": "related work" in lower,
                      "method": any(s in lower for s in ("method", "approach", "algorithm")),
                      "results": any(s in lower for s in ("result", "experiment", "evaluation")), "warnings": warnings,
                      "section_inventory": [{"id": s["id"], "title": s["section"], "kind": s["kind"]} for s in sections],
                      "figures": figures, "tables": [], "references": references, "representations": [], "document_title": document_title}


def figure_pdf_page(path: Path, caption: str) -> int:
    """A caption label at a line start plus caption words must identify one page.

    Mentions in running prose are deliberately insufficient. An ambiguous or
    absent caption is unresolved; page numbers are never guessed from DOM IDs.
    """
    import fitz

    label = re.match(r"\s*(Figure|Fig\.|Table)\s+(\d+[A-Za-z]?)\s*[:.|]\s*(.*)", caption, re.I | re.S)
    if label is None:
        raise SourceError("PDF fallback cannot identify a caption label for this figure")
    prefix = r"(?:Figure|Fig\.)" if label[1].lower().startswith("fig") else "Table"
    pattern = re.compile(r"^\s*" + prefix + r"\s+" + re.escape(label[2]) + r"\s*[:.|]\s*", re.I | re.M)
    expected = re.findall(r"[a-z0-9]+", label[3].lower())[:8]
    if len(expected) < 3:
        raise SourceError("PDF fallback caption has insufficient identifying text")
    candidates = set()
    with fitz.open(path) as pdf:
        for index, page in enumerate(pdf):
            text = page.get_text()
            for match in pattern.finditer(text):
                tail = re.sub(r"[\-\u00ad]\s*\n\s*", "", text[match.end():match.end() + 1200])
                words = re.findall(r"[a-z0-9]+", tail.lower())
                if words[:len(expected)] == expected:
                    candidates.add(index + 1)
    if len(candidates) != 1:
        raise SourceError("PDF fallback could not map the figure caption to one unambiguous page")
    return candidates.pop()


def immutable_write(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    if root.resolve() not in path.resolve().parents or any(p.is_symlink() for p in [path, *path.parents] if p != root):
        raise SourceError("Source asset path escapes the task directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as output:
            output.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise SourceError("Immutable source representation already exists with different bytes")
    return path


def representation(path: Path, root: Path, format: str, url: str | None, arxiv_id: str | None) -> dict:
    return {"format": format, "path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "url": url, "arxiv_id": arxiv_id}


def _render_page(page) -> bytes:
    import fitz

    rect = page.rect
    if not all(math.isfinite(x) and x > 0 for x in (rect.width, rect.height)):
        raise SourceError("Visual has invalid dimensions")
    scale = min(2.0, MAX_IMAGE_EDGE / max(rect.width, rect.height), math.sqrt(MAX_IMAGE_PIXELS / (rect.width * rect.height)))
    for _ in range(4):
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
        data = pixmap.tobytes("png")
        if len(data) <= MAX_RENDERED_BYTES:
            return data
        scale *= 0.7
    raise SourceError("Rendered visual exceeds the image size limit")


def render_pdf_page(path: Path, number: int) -> bytes:
    import fitz

    try:
        with fitz.open(path) as pdf:
            if not 1 <= number <= len(pdf):
                raise SourceError("PDF visual page does not exist")
            return _render_page(pdf[number - 1])
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("PDF visual could not be rendered") from exc


def render_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Decode trusted-size image bytes without browser scripts or remote loads."""
    import fitz

    if len(data) > MAX_VISUAL_BYTES:
        raise SourceError("Original image exceeds the image size limit")
    formats = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF8", "gif"))
    format = next((name for signature, name in formats if data.startswith(signature)), None)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        format = "webp"
    if format is None and (content_type == "image/svg+xml" or b"<svg" in data[:1000]):
        if re.search(br"<!\s*(DOCTYPE|ENTITY)", data, re.I):
            raise SourceError("SVG declarations and external entities are not allowed")
        try:
            root = ET.fromstring(data)
            if root.tag.split("}")[-1] != "svg":
                raise SourceError("Source visual is not an SVG")
            embedded_count, embedded_pixels = 0, 0
            if sum(1 for _ in root.iter()) > 30000:
                raise SourceError("SVG element count exceeds its bound")
            for node in root.iter():
                if node.tag.split("}")[-1].lower() in {"script", "foreignobject", "image", "use"}:
                    if node.tag.split("}")[-1].lower() in {"script", "foreignobject"}:
                        raise SourceError("Active SVG content is not allowed")
                for key, value in node.attrib.items():
                    if key.split("}")[-1].lower().startswith("on"):
                        raise SourceError("SVG external resources are not allowed")
                    if key.split("}")[-1] in {"href", "src"} and not value.startswith("#"):
                        embedded = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,\s*([A-Za-z0-9+/=\s]+)", value)
                        if embedded is None or node.tag.split("}")[-1] != "image":
                            raise SourceError("SVG external resources are not allowed")
                        embedded_count += 1
                        if embedded_count > 64:
                            raise SourceError("SVG embedded image count exceeds its bound")
                        try:
                            decoded = base64.b64decode(re.sub(r"\s+", "", embedded[2]), validate=True)
                        except (ValueError, binascii.Error) as exc:
                            raise SourceError("SVG embedded image is malformed") from exc
                        render_image(decoded, "image/" + embedded[1])
                        from PIL import Image
                        with Image.open(io.BytesIO(decoded)) as embedded_image:
                            embedded_pixels += embedded_image.width * embedded_image.height
                        if embedded_pixels > MAX_IMAGE_PIXELS:
                            raise SourceError("SVG total embedded pixels exceed their bound")
            css_urls = re.findall(br"url\(([^)]*)\)", data, re.I)
            if re.search(br"@import", data, re.I) or any(not value.strip(b" \t\r\n'\"").startswith(b"#") for value in css_urls):
                raise SourceError("SVG external styles are not allowed")
        except ET.ParseError as exc:
            raise SourceError("Source SVG is malformed") from exc
        format = "svg"
    if format is None:
        raise SourceError("Source visual is not a supported image")
    try:
        with fitz.open(stream=data, filetype=format) as image:
            if not len(image):
                raise SourceError("Source visual is empty")
            # Bound raster decompression before rendering; SVG is vector data.
            if format != "svg":
                from PIL import Image
                with Image.open(io.BytesIO(data)) as raster:
                    width, height = raster.size
                    if width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_EDGE * 2:
                        raise SourceError("Source image dimensions exceed the pixel limit")
            if format == "svg":
                import cairosvg
                rect = image[0].rect
                if not all(math.isfinite(v) and v > 0 for v in (rect.width, rect.height)):
                    raise SourceError("SVG has invalid dimensions")
                scale = min(2.0, MAX_IMAGE_EDGE / max(rect.width, rect.height), math.sqrt(MAX_IMAGE_PIXELS / (rect.width * rect.height)))
                rendered = cairosvg.svg2png(bytestring=data, output_width=max(1, int(rect.width * scale)),
                                            output_height=max(1, int(rect.height * scale)),
                                            background_color="white", unsafe=False)
                if len(rendered) > MAX_RENDERED_BYTES:
                    raise SourceError("Rendered SVG exceeds the image size limit")
                return rendered, format
            return _render_page(image[0]), format
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("Source image could not be decoded") from exc


def combine_images(images: list[bytes]) -> bytes:
    """All subpanels of one figure are inspected together, in DOM order."""
    import fitz

    if len(images) == 1:
        return images[0]
    sizes = []
    for data in images:
        with fitz.open(stream=data, filetype="png") as image:
            sizes.append(image[0].rect)
    with fitz.open() as pdf:
        width = max(rect.width for rect in sizes)
        height = sum(rect.height * width / rect.width for rect in sizes)
        page = pdf.new_page(width=width, height=height)
        y = 0
        for data, rect in zip(images, sizes):
            h = rect.height * width / rect.width
            page.insert_image(fitz.Rect(0, y, width, y + h), stream=data)
            y += h
        return _render_page(page)
