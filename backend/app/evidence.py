"""Bounded reading packets whose evidence is resolved against retained sources."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

from .source_tools import SourceLibrary

MAX_PACKET_BYTES = 256 * 1024
CONDITIONS = {"model", "dataset", "split", "metric", "baseline", "budget"}
KINDS = {"contribution", "method", "result", "lesson", "limitation", "related_work"}
BASES = {"observation", "author_hypothesis", "inference"}
PDF_LIGATURES = str.maketrans({chr(code): unicodedata.normalize("NFKC", chr(code)) for code in range(0xFB00, 0xFB07)})
# A deliberately small prose vocabulary: arbitrary Latin runs can be formula
# identifiers or functions. Other ambiguous PDF joins require source rereading.
PDF_MATH_PROSE_WORDS = frozenset({
    "input", "inputs", "output", "outputs", "token", "tokens", "position", "positions",
    "step", "steps", "sample", "samples", "iteration", "iterations", "layer", "layers",
    "head", "heads", "parameter", "parameters", "dimension", "dimensions", "block", "blocks",
    "draft", "drafts", "candidate", "candidates",
})
MATH_FUNCTION_WORDS = frozenset({"sin", "cos", "tan", "log", "exp", "sqrt", "min", "max"})
EVIDENCE_METHODS = {"text_quote": "quote_match", "table_cell": "table_cell_match",
                    "image_observation": "visual_observation"}
EVIDENCE_FIELDS = {"id", "source_id", "section_id", "quote", "page", "evidence_type",
                   "table_id", "row", "column", "headers", "figure_id", "image_sha256", "observation_id", "selection"}


class PacketError(ValueError):
    pass


def _glyph_form(value: str) -> str:
    # Broad NFKC would turn exponents/footnotes into ordinary digits (10⁶→106).
    # Only the seven alphabetic PDF ligatures receive compatibility expansion.
    return unicodedata.normalize("NFC", value.translate(PDF_LIGATURES))


def _quote_form(value: str) -> str:
    """Typography only: retain all punctuation, digits and lexical content."""
    return " ".join(_glyph_form(value).casefold().split())


def _is_math_letter(character: str) -> bool:
    return character.isalpha() and unicodedata.name(character, "").startswith(("GREEK ", "MATHEMATICAL "))


def _math_prose_form(value: str) -> tuple[str, list[int], set[int]]:
    """Ignore only the gap between an isolated math letter and known prose.

    Input already has collapsed whitespace. Keep source indices and record
    eligible boundaries even when no gap exists, so a quote cannot create a
    prose boundary inside an otherwise ineligible source formula.
    """
    removed, boundaries = set(), []
    for match in re.finditer(r"([^\x00-\x7f])( ?)([a-z]+)(?=$|[ .,;:!?)])", value):
        symbol, gap, word = match.groups()
        if not _is_math_letter(symbol):
            continue
        if word not in PDF_MATH_PROSE_WORDS:
            continue
        # Never detach a symbol from a coefficient, exponent, subscript,
        # operator, another variable, or an adjoining prose word.
        before = value[:match.start()]
        if before:
            if not before.endswith(" "):
                continue
            previous = before.rstrip().rsplit(" ", 1)[-1].rstrip(",.;:!?")
            if not re.fullmatch(r"[a-z]{2,}", previous) or previous in MATH_FUNCTION_WORDS:
                continue
        after = value[match.end():]
        if after.startswith(" "):
            following = re.match(r"[a-z]{2,}(?=$|[ .,;:!?)])", after[1:])
            if following is None or following.group() in MATH_FUNCTION_WORDS:
                continue
        boundaries.append(match.start() + 1 - len(removed))
        if gap:
            removed.add(match.start() + 1)
    indices = [index for index in range(len(value)) if index not in removed]
    return "".join(value[index] for index in indices), indices, set(boundaries)


def _source_quote(raw: str, quote: str) -> str | None:
    """Resolve a typography-equivalent match to an actual contiguous source slice."""
    if quote in raw:
        return quote
    target = _quote_form(quote)
    if len(target) < 12:
        return None
    characters: list[str] = []
    spans: list[tuple[int, int]] = []

    def emit(value: str, start: int, end: int) -> None:
        for character in value.casefold():
            if character.isspace():
                if not characters:
                    continue
                if characters[-1] == " ":
                    spans[-1] = (spans[-1][0], end)
                    continue
                character = " "
            characters.append(character)
            spans.append((start, end))

    # Keep normalization-dependent characters together, including combining
    # marks and Hangul Jamo. A ligature may expand to several normalized chars;
    # each retains the same original span. No source characters are deleted.
    cluster, normalized, start = "", "", 0
    for index, character in enumerate(raw):
        next_normalized = _glyph_form(character)
        combined = _glyph_form(cluster + character)
        if cluster and not unicodedata.category(character).startswith("M") and combined == normalized + next_normalized:
            emit(normalized, start, index)
            cluster, normalized, start = character, next_normalized, index
        else:
            if not cluster:
                start = index
            cluster += character
            normalized = combined
    if cluster:
        emit(normalized, start, len(raw))
    if characters and characters[-1] == " ":
        characters.pop(); spans.pop()
    text = "".join(characters)
    # Fail closed if a normalization boundary was not represented faithfully.
    if text != _quote_form(raw):
        return None
    offset = text.find(target)
    while offset >= 0:
        end = offset + len(target)
        original = raw[spans[offset][0]:spans[end - 1][1]]
        # Never accept half of an expanded glyph, or drop part of a combining
        # cluster. Persist the real source text, not the model's normalized form.
        if _quote_form(original) == target:
            return original
        offset = text.find(target, offset + 1)
    # PDF extraction sometimes glues an inline symbol to prose (𝛾input).
    # Compare only recognized boundaries, then return the retained raw slice.
    compact_target, _, target_boundaries = _math_prose_form(target)
    if not target_boundaries:
        return None
    compact_text, indices, source_boundaries = _math_prose_form(text)
    offset = compact_text.find(compact_target)
    while offset >= 0:
        end = offset + len(compact_target)
        if all(offset + boundary in source_boundaries for boundary in target_boundaries):
            original = raw[spans[indices[offset]][0]:spans[indices[end - 1]][1]]
            # Prose case folding must not make this new fallback accept a
            # different mathematical letter (for example Γ versus γ).
            source_symbols = [character for character in original if _is_math_letter(character)]
            quote_symbols = [character for character in quote if _is_math_letter(character)]
            if source_symbols == quote_symbols and _math_prose_form(_quote_form(original))[0] == compact_target:
                return original
        offset = compact_text.find(compact_target, offset + 1)
    return None


def _object(value, required: set[str], optional: set[str] | None = None, label="Object") -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise PacketError(f"{label} has missing or unsupported fields")
    return value


def _text(value, maximum: int, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or "\x00" in value:
        raise PacketError(f"{label} must be bounded {'optional ' if empty else ''}text")
    return value


def _array(value, maximum: int, label: str, minimum: int = 0) -> list:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise PacketError(f"{label} must contain {minimum}–{maximum} items")
    return value


def _identifier(value, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[1-9][0-9]{0,5}", value):
        raise PacketError(f"Invalid {prefix} identifier")
    return value


def _evidence_shape(item, version: int) -> dict:
    required, optional = {"id", "source_id", "section_id", "quote"}, {"page"}
    evidence_type = item.get("evidence_type", "text_quote") if isinstance(item, dict) else "text_quote"
    if version == 2:
        optional.add("evidence_type")
        if not isinstance(evidence_type, str) or evidence_type not in EVIDENCE_METHODS:
            raise PacketError("Unsupported evidence type")
        if evidence_type == "table_cell":
            required.remove("quote")
            required.update({"table_id", "row", "column"})
            optional.update({"quote", "headers"})
        elif evidence_type == "image_observation":
            required.update({"figure_id", "image_sha256", "observation_id"})
        else:
            required.remove("quote")
            optional.update({"quote", "selection"})
    _object(item, required, optional, "Evidence")
    eid = _identifier(item["id"], "E")
    for key in ("source_id", "section_id"):
        _text(item[key], 200, eid + " " + key)
    quote = _text(item["quote"], 8000, eid + " quote", empty=evidence_type != "text_quote") if "quote" in item else None
    if evidence_type == "text_quote" and quote is not None and len(" ".join(quote.split())) < 12:
        raise PacketError(eid + ": quote must contain at least 12 meaningful characters")
    if "selection" in item and item["selection"] != "section":
        raise PacketError(eid + ": invalid source selection method")
    if "page" in item and (type(item["page"]) is not int or not 1 <= item["page"] <= 100000):
        raise PacketError(eid + ": invalid evidence page")
    if evidence_type == "table_cell":
        _text(item["table_id"], 200, eid + " table ID")
        for key in ("row", "column"):
            if type(item[key]) is not int or not 0 <= item[key] <= 100000:
                raise PacketError(eid + ": table coordinates must be nonnegative integers")
        for header in _array(item.get("headers", []), 100, eid + " headers"):
            _text(header, 8000, eid + " header", empty=True)
    elif evidence_type == "image_observation":
        for key in ("figure_id", "observation_id"):
            _text(item[key], 200, eid + " " + key)
        if item["source_id"] != "main" or quote != "":
            raise PacketError(eid + ": image observations require the main source and an empty quote")
        if not isinstance(item["image_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["image_sha256"]):
            raise PacketError(eid + ": invalid image SHA-256")
    return item


def _packet_shape(packet) -> dict:
    try:
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise PacketError("Packet must be finite, serializable JSON") from exc
    if len(encoded) > MAX_PACKET_BYTES:
        raise PacketError("Packet exceeds the size limit")
    _object(packet, {"version", "paper_title", "research_question", "claims", "open_questions", "limitations"}, label="Packet")
    if type(packet["version"]) is not int or packet["version"] not in (1, 2):
        raise PacketError("Unsupported packet version")
    _text(packet["paper_title"], 1000, "Paper title")
    _text(packet["research_question"], 4000, "Research question")
    seen, evidence = set(), {}
    for claim in _array(packet["claims"], 80 if packet["version"] == 2 else 20, "Claims", 1):
        _object(claim, {"id", "kind", "statement", "basis", "conditions", "evidence"}, {"numeric_comparison"}, "Claim")
        identity = _identifier(claim["id"], "C")
        if identity in seen:
            raise PacketError("Duplicate claim identifier: " + identity)
        seen.add(identity)
        if claim["kind"] not in KINDS or claim["basis"] not in BASES:
            raise PacketError(identity + ": unsupported claim kind or basis")
        _text(claim["statement"], 4000, identity + " statement")
        _object(claim["conditions"], CONDITIONS, label=identity + " conditions")
        for key, value in claim["conditions"].items():
            _text(value, 1000, identity + " " + key, empty=True)
        local = set()
        for item in _array(claim["evidence"], 6 if packet["version"] == 2 else 3, identity + " evidence", 1):
            _evidence_shape(item, packet["version"])
            eid = item["id"]
            if eid in local or (eid in evidence and item != evidence[eid]):
                raise PacketError(eid + ": repeated evidence IDs must have identical content across claims")
            local.add(eid)
            evidence[eid] = item
        numeric = claim.get("numeric_comparison")
        if numeric is not None:
            _object(numeric, {"value", "baseline_value", "relation", "unit"}, label=identity + " comparison")
            for key in ("value", "baseline_value"):
                if type(numeric[key]) not in (float, int) or (type(numeric[key]) is float and not math.isfinite(numeric[key])):
                    raise PacketError(identity + ": numeric comparison values must be finite numbers")
            if numeric["relation"] not in {"higher", "lower", "equal"}:
                raise PacketError(identity + ": invalid numeric comparison relation")
            _text(numeric["unit"], 100, identity + " unit", empty=True)
    for question in _array(packet["open_questions"], 3, "Open questions"):
        _object(question, {"question", "search_query", "reason"}, label="Open question")
        for key, value in question.items():
            _text(value, 500 if key == "search_query" else 1500, "Open question " + key)
    for value in _array(packet["limitations"], 40, "Limitations"):
        _text(value, 4000, "Limitation")
    return copy.deepcopy(packet)


def _visual_record(item: dict, library: SourceLibrary, claim_id: str | None, *, require_claim_ids: bool = True) -> dict:
    """Bind host acceptance to a prepared source image and bytes actually read."""
    eid = item["id"]
    observations = getattr(library, "visual_observations", {})
    prepared = getattr(library, "prepared_visuals", {})
    record = observations.get(item["observation_id"]) if isinstance(observations, dict) else None
    asset = prepared.get(item["figure_id"]) if isinstance(prepared, dict) else None
    if not isinstance(record, dict) or not isinstance(asset, dict):
        raise PacketError(eid + ": image observation has no host-accepted record or prepared source image")
    if (record.get("id") != item["observation_id"] or record.get("readable") is not True
            or any(record.get(key) != item[key] for key in ("figure_id", "image_sha256", "section_id"))
            or asset.get("source_id") != "main" or asset.get("figure_id") != item["figure_id"]
            or asset.get("section_id") != item["section_id"] or asset.get("sha256") != item["image_sha256"]
            or ("page" in item and asset.get("page") != item["page"])):
        raise PacketError(eid + ": image observation identity or readability does not match the source image")
    _text(record.get("observation"), 8000, eid + " observation")
    _text(record.get("conditions"), 8000, eid + " visual conditions", empty=True)
    for uncertainty in _array(record.get("uncertainties"), 80, eid + " visual uncertainties"):
        _text(uncertainty, 4000, eid + " visual uncertainty")
    claim_ids = _array(record.get("claim_ids"), 80, eid + " visual claim IDs", 1 if require_claim_ids else 0)
    for identity in claim_ids:
        _identifier(identity, "C")
    if claim_id is not None and claim_id not in claim_ids:
        raise PacketError(eid + ": image observation was not accepted for this claim")
    # The JSON ledger is host-owned, records the actual returned image payload,
    # and is copied under the worker's checkpoint/tenant checks on recovery.
    # Merely preparing/downloading an image never satisfies this read check.
    try:
        root = library.work_dir.resolve()
        relative = asset["path"]
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid image path")
        path = root / relative
        ledger_path = root / "host-image-inspections.json"
        if (root not in path.resolve().parents or ledger_path.is_symlink()
                or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root)
                or path.stat().st_size > 5 * 1024 * 1024 or ledger_path.stat().st_size > MAX_PACKET_BYTES):
            raise ValueError("Invalid image ledger path or size")
        ledger = json.loads(ledger_path.read_text())
        if (not isinstance(ledger, dict) or ledger.get(relative) != item["image_sha256"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["image_sha256"]):
            raise ValueError("Unread or changed image")
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise PacketError(eid + ": prepared image bytes do not match the actual host image-read ledger") from exc
    return record


def validate_visual_source_coverage(library: SourceLibrary, recovery: dict) -> bool:
    """Check complete visual recovery without changing the original text coverage.

    Page transcripts are separate host observations, including pages with no
    factual claims. Their acceptance cannot turn image content into text quotes.
    """
    try:
        _object(recovery, {"complete", "recovered_pages", "observation_ids"}, label="Visual source coverage")
        main = library.documents.get("main")
        if (main is None or getattr(library, "rules_version", None) != "source-structured-v3"
                or main.coverage.get("full_text") is not False or recovery["complete"] is not True):
            return False
        missing = main.coverage.get("missing_text_pages")
        pages, observation_ids = recovery["recovered_pages"], recovery["observation_ids"]
        for values in (missing, pages):
            if (not isinstance(values, list) or not values
                    or any(type(page) is not int or not 1 <= page <= 100000 for page in values)
                    or len(set(values)) != len(values)):
                return False
        if set(pages) != set(missing):
            return False
        if (not isinstance(observation_ids, list) or not all(isinstance(value, str) for value in observation_ids)
                or len(observation_ids) != len(pages) or set(observation_ids) != {f"VP{page}" for page in pages}):
            return False
        prepared = getattr(library, "prepared_visuals", {})
        for page in pages:
            asset = prepared.get(f"page:{page}")
            if not isinstance(asset, dict) or asset.get("page") != page:
                return False
            sections = [section for section in main.sections if section.get("id") == asset.get("section_id")]
            if len(sections) != 1 or sections[0].get("page") != page:
                return False
            item = {"id": f"VP{page}", "source_id": "main", "section_id": asset["section_id"], "page": page,
                    "figure_id": f"page:{page}", "image_sha256": asset["sha256"], "observation_id": f"VP{page}"}
            _visual_record(item, library, None, require_claim_ids=False)
    except (PacketError, KeyError, TypeError, ValueError, OSError):
        return False
    return True


def resolve_evidence(item: dict, library: SourceLibrary, *, claim_id: str | None = None, packet_version: int = 2) -> dict:
    """Independently resolve one v2 evidence item for packets or publication."""
    _evidence_shape(item, packet_version)
    item = copy.deepcopy(item)
    source = library.documents.get(item["source_id"])
    if source is None:
        raise PacketError(item["id"] + ": source has not been read")
    sections = [section for section in source.sections if section.get("id") == item["section_id"]]
    html_source = (packet_version == 2 and getattr(library, "rules_version", None) == "source-structured-v3"
                   and str(source.coverage.get("parser", "")).startswith("html-structured"))
    if html_source and len(sections) == 1 and sections[0].get("page") is None:
        # Page numbers are not meaningful locators in an HTML representation.
        # Location metadata comes from the parser; section identity still must
        # match exactly. PDF page constraints remain strict.
        item.pop("page", None)
    if "page" in item:
        sections = [section for section in sections if section.get("page") == item["page"]]
    if len(sections) != 1:
        raise PacketError(item["id"] + ": evidence source/section/page is missing or ambiguous")
    section = sections[0]
    evidence_type = item.get("evidence_type", "text_quote")
    if evidence_type == "text_quote":
        if "quote" not in item or (html_source and item.get("selection") == "section"):
            original = section.get("text", "")
            if (not html_source
                    or section.get("role") in {"heading", "table"}
                    or not 12 <= len(" ".join(original.split())) <= 8000 or len(original) > 8000):
                raise PacketError(item["id"] + ": provide an explicit short quote for PDF pages, tables, headings or oversized sections")
            item.update(quote=original, selection="section")
            item["verification_note"] = "Original paragraph resolved by section identity; no model quotation was treated as a matched quote"
        elif not html_source:
            item.pop("selection", None)
        original = _source_quote(section.get("text", ""), item["quote"])
        if original is None or len(original) > 8000:
            raise PacketError(item["id"] + ": exact quote was not found in the declared source/section/page")
        item["quote"] = original
    elif evidence_type == "table_cell":
        tables = [table for table in source.coverage.get("tables", [])
                  if table.get("id") == item["table_id"] and table.get("section_id") == item["section_id"]]
        cells = [cell for table in tables for row in table.get("rows", []) for cell in row
                 if cell.get("row") == item["row"] and cell.get("col") == item["column"]]
        if len(tables) != 1 or len(cells) != 1:
            raise PacketError(item["id"] + ": table cell was not found at the declared origin coordinates")
        cell = cells[0]
        if ("quote" in item and cell.get("text") != item["quote"]) or ("headers" in item and cell.get("headers") != item["headers"]):
            raise PacketError(item["id"] + ": table cell value or complete header chain does not match")
        item["quote"] = cell["text"]
        item["headers"] = copy.deepcopy(cell["headers"])
    else:
        record = _visual_record(item, library, claim_id)
        item.update({key: copy.deepcopy(record[key]) for key in ("observation", "readable", "conditions", "uncertainties", "claim_ids")})
        item["verification_note"] = "Host-accepted visual observation; not an original text quotation"
    _evidence_shape({key: item[key] for key in EVIDENCE_FIELDS if key in item}, 2)
    item.update({"source_title": source.title, "source_url": source.url, "kind": source.kind,
                 "section": section.get("section", section["id"]), "verified": True,
                 "evidence_type": evidence_type, "verification_method": "section_lookup" if item.get("selection") == "section" else EVIDENCE_METHODS[evidence_type]})
    if section.get("page") is not None:
        item["page"] = section["page"]
    return item


def validate_packet(packet: dict, library: SourceLibrary) -> tuple[dict, list[str]]:
    """Validate exact clauses and arithmetic; semantic inference still needs review."""
    try:
        clean = _packet_shape(packet)
    except (PacketError, TypeError) as exc:
        return {}, [str(exc)]
    errors = []
    for claim in clean["claims"]:
        numeric = claim.get("numeric_comparison")
        if numeric:
            value, baseline = numeric["value"], numeric["baseline_value"]
            actual = "higher" if value > baseline else "lower" if value < baseline else "equal"
            if actual != numeric["relation"]:
                errors.append(claim["id"] + ": numeric comparison relation contradicts its values")
        for item in claim["evidence"]:
            try:
                resolved = resolve_evidence(item, library, claim_id=claim["id"], packet_version=clean["version"])
                if clean["version"] == 2:
                    for key in ("selection", "page"):
                        if key not in resolved:
                            item.pop(key, None)
                    item.update({key: resolved[key] for key in ("quote", "headers", "selection", "page") if key in resolved})
                else:
                    item["quote"] = resolved["quote"]
            except PacketError as exc:
                errors.append(str(exc))
    if clean["version"] == 2 and len(json.dumps(clean, ensure_ascii=False).encode()) > MAX_PACKET_BYTES:
        errors.append("Resolved source excerpts exceed the packet byte bound; select shorter explicit quotes")
    return clean, list(dict.fromkeys(errors))


def packet_evidence(packet: dict, library: SourceLibrary) -> list[dict]:
    clean, errors = validate_packet(packet, library)
    if errors:
        raise PacketError("; ".join(errors))
    resolved = {}
    for claim in clean["claims"]:
        for item in claim["evidence"]:
            if clean["version"] == 2:
                resolved[item["id"]] = resolve_evidence(item, library, claim_id=claim["id"])
                continue
            source = library.documents[item["source_id"]]
            section = next(section for section in source.sections if section["id"] == item["section_id"])
            entry = {**item, "source_title": source.title, "source_url": source.url, "kind": source.kind,
                     "section": section.get("section", section["id"]), "verified": True}
            if section.get("page") is not None:
                entry["page"] = section["page"]
            resolved[item["id"]] = entry
    return list(resolved.values())


def apply_claim_patch(packet: dict, patch: dict) -> dict:
    """Replace existing claims atomically; new IDs and silent removals are errors."""
    clean = _packet_shape(packet)
    _object(patch, {"claims", "remove_claim_ids"}, label="Claim patch")
    maximum = 80 if clean["version"] == 2 else 20
    replacements = _array(patch["claims"], maximum, "Replacement claims")
    removed = _array(patch["remove_claim_ids"], maximum, "Removed claim IDs")
    existing = {claim["id"] for claim in clean["claims"]}
    remove_ids = [_identifier(value, "C") for value in removed]
    replace_ids = [_identifier(value.get("id") if isinstance(value, dict) else None, "C") for value in replacements]
    if len(set(remove_ids)) != len(remove_ids) or len(set(replace_ids)) != len(replace_ids):
        raise PacketError("Claim patch contains duplicate IDs")
    if (set(remove_ids) | set(replace_ids)) - existing:
        raise PacketError("Claim patch may only reference existing claim IDs")
    if set(remove_ids) & set(replace_ids):
        raise PacketError("Claim patch cannot replace and remove the same claim")
    updates = dict(zip(replace_ids, replacements))
    claims = [updates.get(claim["id"], claim) for claim in clean["claims"] if claim["id"] not in remove_ids]
    if claims == clean["claims"]:
        raise PacketError("Claim patch must make an actual change")
    clean["claims"] = claims
    return _packet_shape(clean)
