"""Small, durable tools for the Pi task owner.

The model can edit findings and drafts, but cannot edit source snapshots, issue a
passing review, or publish an unchecked document.  Tool failures are observations
for the agent; accepted sibling findings and document versions remain intact.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
from decimal import Decimal, InvalidOperation
from typing import Callable

from .evidence import (BASES, CONDITIONS, EVIDENCE_FIELDS, KINDS, PacketError, resolve_evidence,
                       validate_packet, validate_visual_source_coverage)
from .pipeline import PipelineError, TaskCancelled, task_path, validate_result
from .source_tools import SourceError
from .table_math import table_statistics


ENGINE = "pi-agent-v1"
REVIEW_KINDS = {"fact", "visual", "coverage", "product"}
READ_ONLY = {"source_index", "read_source", "search_source", "read_visual", "read_table",
             "table_statistics", "read_skill_file", "read_findings", "read_document", "task_status"}
MAX_CLAIMS = 400


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value, label, maximum=4000, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise PipelineError(f"{label} must be {'0' if empty else '1'}–{maximum} characters")
    return value


def _obj(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def _str(maximum=4000, **kwargs):
    return {"type": "string", "maxLength": maximum, **kwargs}


def _int(minimum=0, maximum=100000):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _arr(items, maximum=100):
    return {"type": "array", "items": items, "maxItems": maximum}


def _schema_check(value, schema, path="arguments"):
    """Validate again on the trusted side, including rejected attempt accounting."""
    expected = schema.get("type")
    checks = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
              "string": lambda: isinstance(value, str), "integer": lambda: type(value) is int,
              "number": lambda: type(value) in (int, float) and math.isfinite(value),
              "boolean": lambda: type(value) is bool}
    if expected in checks and not checks[expected]():
        raise PipelineError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise PipelineError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        extra = value.keys() - properties.keys() if schema.get("additionalProperties") is False else set()
        if missing or extra:
            raise PipelineError(f"{path}: missing fields {sorted(missing)}, unknown fields {sorted(extra)}")
        for key, item in value.items():
            if key in properties:
                _schema_check(item, properties[key], path + "." + key)
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", 100000) or len(value) < schema.get("minItems", 0):
            raise PipelineError(f"{path} has an invalid number of entries")
        for index, item in enumerate(value):
            _schema_check(item, schema.get("items", {}), f"{path}[{index}]")
    elif isinstance(value, str) and len(value) > schema.get("maxLength", 1000000):
        raise PipelineError(f"{path} exceeds its character limit")
    elif type(value) in (int, float) and not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
        raise PipelineError(f"{path} is outside its permitted range")


class AgentTools:
    def __init__(self, pipeline, store, spec: dict, review: Callable[[str, dict], dict]):
        self.pipeline, self.store, self.spec, self.review = pipeline, store, spec, review
        self.library = pipeline.library
        self.chat = spec.get("kind") == "chat"
        self._review_local = threading.local()
        self._call_local = threading.local()

    def definitions(self) -> list[dict]:
        evidence = _obj({"id": _str(50), "source_id": _str(200), "source_sha256": _str(64),
            "section_id": _str(200), "quote": _str(8000), "selection": _str(20, enum=["section"]),
            "page": _int(1), "evidence_type": _str(30, enum=["text_quote", "table_cell", "image_observation"]),
            "table_id": _str(200), "row": _int(), "column": _int(), "headers": _arr(_str(8000)),
            "figure_id": _str(200), "image_sha256": _str(64), "observation_id": _str(200)},
            ["id", "source_id", "section_id"])
        claim = _obj({"id": _str(50), "kind": _str(30, enum=sorted(KINDS)), "statement": _str(),
            "basis": _str(30, enum=sorted(BASES)), "conditions": _obj({key: _str(1000) for key in sorted(CONDITIONS)}),
            "evidence": _arr(evidence, 6), "numeric_comparison": _obj({"value": {"type": "number"},
                "baseline_value": {"type": "number"}, "relation": _str(10, enum=["higher", "lower", "equal"]),
                "unit": _str(100)}, ["value", "baseline_value", "relation", "unit"])},
            ["id", "kind", "statement", "basis", "evidence"])
        entries = [
            ("task_status", "Read accepted progress, remaining work, document version and valid review IDs.", _obj()),
            ("prepare_source", "Acquire the authoritative paper. HTML/PDF choices retain every original separately. Returns a compact inventory; use source_index and read_source to read the full paper.",
                _obj({"representation": _str(10, enum=["auto", "html", "pdf"])})),
            ("source_index", "Page through canonical source section IDs, figures, tables and reference index. Does not count as reading section text.",
                _obj({"source_id": _str(200), "offset": _int(), "limit": _int(1, 100)})),
            ("read_source", "Read original text in bounded ranges. Use canonical section IDs and exact quotes or HTML selection:section. Continue at next_offset until done; complete ranges count toward coverage.",
                _obj({"source_id": _str(200), "section_id": _str(200), "page": _int(1), "offset": _int(), "limit": _int(1, 32000)})),
            ("search_source", "Search retained original text with English terms when the paper is English. Matches return canonical IDs and exact original windows; search is not full section coverage.",
                _obj({"query": _str(2000), "source_id": _str(200), "limit": _int(1, 12)}, ["query"])),
            ("read_visual", "Inspect actual paper figure/page pixels, not merely captions. Requires configured vision. Missing capability is recoverable waiting, never a fabricated observation.",
                _obj({"figure_id": _str(200)}, ["figure_id"])),
            ("read_table", "Read original table cells and header chains in row ranges; complete ranges count as table inspection.",
                _obj({"table_id": _str(200), "source_id": _str(200), "offset": _int(), "limit": _int(1, 40)}, ["table_id"])),
            ("table_statistics", "Calculate exact original table arithmetic. Header alignment does not by itself establish scientific comparability.",
                _obj({"table_id": _str(200), "source_id": _str(200)}, ["table_id"])),
            ("search_papers", "Search scholarly providers. An unavailable provider is an observation; decide whether the user's goal requires external evidence.", _obj({"query": _str(500)}, ["query"])),
            ("fetch_related", "Fetch and retain a related paper through the protected downloader; cannot access arbitrary local files or private network targets.", _obj({"id": _str(500), "url": _str(2000)}, ["id"])),
            ("read_skill_file", "Read the selected guidance, starting with SKILL.md; respect its writing requirements.", _obj({"path": _str(1024)}, ["path"])),
            ("save_plan", "Save or update your approach, research question, remaining work, conditions and unresolved questions. This does not mark content verified.",
                _obj({"plan": _arr(_str()), "research_question": _str(), "remaining_work": _arr(_str()),
                    "open_questions": _arr(_str()), "conditions": _arr(_str()), "limitations": _arr(_str())})),
            ("save_findings", "Save at most FIVE claims at a time. Each independently checks exact source evidence and numeric comparisons; valid siblings survive rejected claims. Use C1/E1-style stable IDs. Conditions are model/dataset/split/metric/baseline/budget. Omit quote with selection:section only for original HTML paragraphs. Update a claim by its existing ID; no silent removal.",
                _obj({"claims": _arr(claim, 5)}, ["claims"])),
            ("save_visual_observations", "Save at most five UNTRUSTED visual observations after read_visual. They become evidence only after an independent visual review approves their actual images. Use VP<n> for a full transcript of a text-deficient PDF page n; preserve all readable material and uncertainties. Other IDs may be V1, V2. Cite accepted records using evidence_type:image_observation, quote:'', observation_id, figure_id, image_sha256. claim_ids binds the observation to planned C1-style claims.",
                _obj({"observations": _arr(_obj({"id": _str(100), "figure_id": _str(200), "observation": _str(8000),
                    "readable": {"type": "boolean"}, "conditions": _str(8000), "uncertainties": _arr(_str(), 80),
                    "claim_ids": _arr(_str(50), 80)}, ["id", "figure_id", "observation", "readable", "conditions", "uncertainties", "claim_ids"]), 5)}, ["observations"])),
            ("read_findings", "Read saved findings/evidence in pages for drafting or independent checking.",
                _obj({"offset": _int(), "limit": _int(1, 10), "claim_ids": _arr(_str(50), 10)})),
            ("edit_draft", "Save one draft section, preserving all others. expected_version is the current GLOBAL document version; each accepted change increments it. Full/brief are required for readings, answer for chat. Cite [E1](evidence:E1). Chat drafts remain pending verification. order controls section order.",
                _obj({"section_id": _str(100), "view": _str(10, enum=["answer"] if self.chat else ["brief", "full"]),
                    "title": _str(300), "content": _str(24000), "expected_version": _int(), "order": _int(0, 10000)},
                    ["section_id", "view", "title", "content", "expected_version"])),
            ("read_document", "Read saved document sections, current version and pending-verification status. Page sections with offset/limit.",
                _obj({"view": _str(10, enum=["brief", "full", "answer"]), "section_id": _str(100), "offset": _int(), "limit": _int(1, 10)})),
            ("validate_work", "Run deterministic evidence, arithmetic, citation, source-hash and coverage checks. Returns precise repair observations without deleting accepted work.", _obj()),
            ("request_review", "Request a fresh independent fact/visual/coverage/product review of saved work. Host binds verdict to all current hashes. Fix issues and request again. Readings require all four kinds; chat fact/product are scoped to the question. Changing drafts or findings invalidates old reviews.",
                _obj({"kind": _str(20, enum=sorted(REVIEW_KINDS))}, ["kind"])),
            ("run_python", "Run serial Python in a networkless isolated sandbox. Read registered inputs at /inputs/<relative path>; write /outputs. Available: stdlib, fitz/PyMuPDF, PIL, bs4; no numpy or pypdf. Threads/fork are disabled. 60 seconds/1 GiB/50 MiB outputs. Output is derived material, never original evidence. No host fallback.",
                _obj({"code": _str(32000), "inputs": _arr(_str(1024), 30)}, ["code", "inputs"])),
            ("submit_final", "Submit the SAVED global document_version and host-issued passing review_ids. All required checks must pass; returns an opaque accepted submission reference. Never send the entire paper here.",
                _obj({"document_version": _int(), "review_ids": _arr(_str(100), 20)}, ["document_version", "review_ids"])),
            ("wait_for_input", "Preserve accepted work and wait when the remaining goal needs missing capability, unavailable evidence or another allocation. Explain the concrete blocker and next action.",
                _obj({"reason": _str(2000)}, ["reason"])),
        ]
        return [{"name": name, "description": description, "parameters": parameters} for name, description, parameters in entries]

    def _state(self):
        return self.store.load().get("state", {})

    def _change(self, kind, change, *, request_id=None):
        def apply(state):
            result = change(state)
            self._project(state)
            return result
        return self.store.mutate(kind, {"tool": getattr(self._call_local, "name", kind)}, apply,
                                 request_id=request_id)

    def _cancel(self):
        if self.pipeline.cancelled.is_set():
            raise TaskCancelled("Agent task cancelled or lease lost")

    def call(self, name, args, call_id):
        self._cancel()
        _text(call_id, "Tool call ID", 180)
        self.store.consume(call_id, name)
        schema = next((item["parameters"] for item in self.definitions() if item["name"] == name), None)
        if schema is None:
            raise PipelineError("Unknown agent tool")
        digest = _hash(args)  # Reject non-finite JSON before any domain effect.
        binding = self.store.mutate("tool_arguments_bound", {"name": name, "sha256": digest},
                                    lambda state: {"name": name, "sha256": digest}, request_id="args:" + _hash(call_id))
        if binding != {"name": name, "sha256": digest}:
            raise PipelineError("Tool call ID already belongs to different arguments; use a fresh call ID")
        _schema_check(args, schema)
        if getattr(self._review_local, "activity", None) is not None and name not in READ_ONLY:
            raise PipelineError("Independent reviewers have read-only tools")
        method = getattr(self, "_tool_" + name)
        previous_name = getattr(self._call_local, "name", None)
        self._call_local.name = name
        try:
            result = method(**args, _call_id=call_id)
            self._cancel()
            return result
        finally:
            self._call_local.name = previous_name

    def _sync_sources(self, state):
        sources = state.setdefault("sources", {})
        for identity, document in self.library.documents.items():
            previous = sources.get(identity)
            record = {"source_id": identity, "sha256": document.sha256, "path": document.path,
                      "title": document.title, "url": document.url, "kind": document.kind,
                      "sections_sha256": _hash([{key: section.get(key) for key in ("id", "text", "kind", "role")}
                                                for section in document.sections]),
                      # Page numbers/visual fallback diagnostics can be added by
                      # the protected renderer. Text structure and cell values
                      # remain an immutable part of the parsed source identity.
                      "structure_sha256": _hash({
                          "sections": [{key: section.get(key) for key in ("id", "parent_id", "anchors")}
                                       for section in document.sections],
                          "tables": document.coverage.get("tables", []),
                          "figures": [{key: figure.get(key) for key in ("id", "section_id", "caption", "images", "text_deficient")}
                                      for figure in document.coverage.get("figures", [])],
                          "references": document.coverage.get("references", []),
                          "full_text": document.coverage.get("full_text"),
                          "missing_text_pages": document.coverage.get("missing_text_pages", [])}),
                      "representations": copy.deepcopy(document.coverage.get("representations", []))}
            if previous and (previous["sha256"] != record["sha256"] or previous["sections_sha256"] != record["sections_sha256"]):
                raise PipelineError("Source version changed behind existing identity; retain it under a new source ID")
            if previous and previous.get("structure_sha256", record["structure_sha256"]) != record["structure_sha256"]:
                raise PipelineError("Original source structure changed behind existing identity: " + identity)
            if previous and any(rep not in record["representations"] for rep in previous.get("representations", [])):
                raise PipelineError("Original source representation changed or disappeared: " + identity)
            sources[identity] = record

    def _check_sources(self, state):
        self._sync_sources(state)
        for record in state.get("sources", {}).values():
            path = task_path(self.pipeline.work_dir, record["path"], must_exist=True)
            if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                raise PipelineError("Original source bytes changed: " + record["source_id"])
            for representation in record.get("representations", []):
                path = task_path(self.pipeline.work_dir, representation["path"], must_exist=True)
                if hashlib.sha256(path.read_bytes()).hexdigest() != representation["sha256"]:
                    raise PipelineError("Original source representation bytes changed: " + representation["path"])
        # Required figures must retain the pixels actually read, including those
        # that do not appear in the final evidence index.
        for identity, read in state.get("visual_reads", {}).items():
            prepared = self.library.prepared_visuals.get(identity)
            if not prepared or any(read.get(key) != prepared.get(key) for key in
                                   ("figure_id", "source_id", "section_id", "path", "sha256")):
                raise PipelineError("Inspected visual identity changed: " + identity)
            path = task_path(self.pipeline.work_dir, read["path"], must_exist=True)
            if hashlib.sha256(path.read_bytes()).hexdigest() != read["sha256"]:
                raise PipelineError("Inspected visual bytes changed: " + identity)
        for identity, observation in state.get("accepted_visual_observations", {}).items():
            if self.library.visual_observations.get(identity) != observation:
                raise PipelineError("Accepted visual observation changed: " + identity)

    @staticmethod
    def _merge_ranges(ranges, start, end):
        merged = []
        for left, right in sorted([*ranges, [start, end]]):
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        return merged

    @staticmethod
    def _covered(ranges, length):
        return not length or bool(ranges and ranges[0][0] == 0 and ranges[0][1] >= length)

    def _coverage(self, state):
        main = self.library.documents.get("main")
        coverage = {"scope": "question" if self.chat else "paper", "body": {"read": 0, "total": 0},
                    "appendix": {"read": 0, "total": 0}, "figures": {"read": 0, "total": 0},
                    "tables": {"read": 0, "total": 0}, "references": {"indexed": 0, "total": 0},
                    "complete": False, "missing_sections": [], "missing_figures": [], "missing_tables": []}
        if main is None:
            return coverage
        reads = state.get("source_reads", {}).get("main", {})
        for section in main.sections:
            if section.get("kind") == "reference" or section.get("role") == "heading" or not section.get("text", "").strip():
                continue
            kind = "appendix" if section.get("kind") == "appendix" else "body"
            coverage[kind]["total"] += 1
            if self._covered(reads.get(section["id"], []), len(section["text"])):
                coverage[kind]["read"] += 1
            else:
                coverage["missing_sections"].append(section["id"])
        for figure in main.coverage.get("figures", []):
            coverage["figures"]["total"] += 1
            if figure["id"] in state.get("visual_reads", {}):
                coverage["figures"]["read"] += 1
            else:
                coverage["missing_figures"].append(figure["id"])
        for table in main.coverage.get("tables", []):
            coverage["tables"]["total"] += 1
            if self._covered(state.get("table_reads", {}).get("main:" + table["id"], []), len(table.get("rows", []))):
                coverage["tables"]["read"] += 1
            else:
                coverage["missing_tables"].append(table["id"])
        references = main.coverage.get("references", [])
        coverage["references"] = {"indexed": len(references), "total": len(references)}
        alternate = self._alternate_coverage(state)
        if alternate:
            coverage["primary_missing_sections"] = coverage["missing_sections"]
            coverage["missing_sections"] = []
            coverage["body"], coverage["appendix"] = alternate["body"], alternate["appendix"]
            coverage["alternate_full_text"] = alternate
        recovery = self._visual_recovery()
        if recovery:
            coverage["visual_recovered_pages"] = recovery["recovered_pages"]
        coverage["complete"] = bool((main.coverage.get("full_text") is True or recovery or alternate) and not any(
            coverage[key] for key in ("missing_sections", "missing_figures", "missing_tables")))
        if self.chat:
            coverage["complete"] = bool(state.get("findings"))
        return coverage

    def _alternate_coverage(self, state):
        """A separately retained representation can supply missing full text.

        Never infer equivalence from a model-authored title or URL. Both parses
        must have the same protected, versioned arXiv identity, and the alternate
        must have been acquired through prepare_source under its immutable ID.
        Primary figures/tables remain independently required by _coverage.
        """
        main = self.library.documents.get("main")
        if not main:
            return None
        resolved = main.coverage.get("source_selection", {}).get("resolved_id")
        if not isinstance(resolved, str) or not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v[1-9]\d*", resolved):
            return None
        for identity, document in self.library.documents.items():
            if (identity == "main" or document.kind != "paper" or
                    identity not in state.get("alternate_sources", {}) or
                    state["alternate_sources"][identity] != document.sha256 or
                    document.coverage.get("full_text") is not True or
                    document.coverage.get("source_selection", {}).get("resolved_id") != resolved):
                continue
            counts = {"body": {"read": 0, "total": 0}, "appendix": {"read": 0, "total": 0}}
            reads = state.get("source_reads", {}).get(identity, {})
            for section in document.sections:
                if section.get("kind") == "reference" or section.get("role") == "heading" or not section.get("text", "").strip():
                    continue
                kind = "appendix" if section.get("kind") == "appendix" else "body"
                counts[kind]["total"] += 1
                if self._covered(reads.get(section["id"], []), len(section["text"])):
                    counts[kind]["read"] += 1
            if counts["body"]["total"] and all(row["read"] == row["total"] for row in counts.values()):
                return {"source_id": identity, "sha256": document.sha256, "resolved_id": resolved,
                        "reads_sha256": _hash(reads), **counts}
        return None

    def _visual_recovery(self):
        main = self.library.documents.get("main")
        pages = main.coverage.get("missing_text_pages", []) if main else []
        if not pages:
            return None
        recovery = {"complete": True, "recovered_pages": list(pages), "observation_ids": [f"VP{page}" for page in pages]}
        return recovery if validate_visual_source_coverage(self.library, recovery) else None

    def _project(self, state):
        state.setdefault("goal", self.spec.get("agent", {}).get("goal") or self.spec.get("instruction") or
                         ("回答论文追问" if self.chat else "完整解读论文并核验依据"))
        state["coverage"] = self._coverage(state)
        state["accepted_work"] = [f"已保存 {len(state.get('findings', {}))} 条有原文依据的结论",
                                  f"已保存 {len(state.get('drafts', {}))} 节草稿（版本 {state.get('document_version', 0)}）"]
        hashes = self._bindings(state)
        passed = [record["kind"] for record in state.get("reviews", {}).values()
                  if record.get("passed") and record.get("bindings") == hashes]
        if passed:
            state["accepted_work"].append("当前版本已通过独立检查：" + "、".join(sorted(set(passed))))
        state.setdefault("remaining_work", [])
        state.setdefault("issue", None)

    def _bindings(self, state):
        return {"sources_sha256": _hash(state.get("sources", {})),
                "findings_sha256": _hash({"findings": state.get("findings", {}), "evidence": state.get("evidence", {}),
                                         "conditions": state.get("conditions", []), "limitations": state.get("limitations", []),
                                         "open_questions": state.get("open_questions", []),
                                         "visual_proposals": state.get("visual_proposals", {}),
                                         "accepted_visual_observations": state.get("accepted_visual_observations", {})}),
                "document_sha256": _hash({"drafts": state.get("drafts", {}), "version": state.get("document_version", 0)}),
                "visuals_sha256": _hash(state.get("visual_reads", {})),
                "guidance_sha256": _hash(state.get("skill_reads", {})),
                "coverage_sha256": _hash(self._coverage(state))}

    def _tool_task_status(self, _call_id):
        state = self._state()
        self._project(state)
        hashes = self._bindings(state)
        return {key: state.get(key) for key in ("goal", "plan", "research_question", "accepted_work", "issue", "remaining_work", "coverage", "document_version")} | {
            "reviews": [{"id": record["id"], "kind": record["kind"], "passed": record["passed"],
                         "current": record["bindings"] == hashes, "issues": record["issues"]}
                        for record in state.get("reviews", {}).values()],
            "sources": list(state.get("sources", {}).values())}

    def _tool_prepare_source(self, representation="auto", _call_id=None):
        if representation == "auto" or not self.library.documents.get("main"):
            self.library.prepare_source()
        source_id = "main"
        main = self.library.documents["main"]
        current_format = main.coverage.get("source_selection", {}).get("format")
        if representation in {"pdf", "html"} and current_format != representation:
            from .source_tools import download_public, normalize_arxiv
            from .structured_source import immutable_write, parse_structured_html, parse_structured_pdf
            resolved = main.coverage.get("source_selection", {}).get("resolved_id")
            if representation == "pdf":
                path = self.library._ensure_pdf_representation()
                source_id = "main-pdf-" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                parsed = parse_structured_pdf(path, source_id)
                url = next(rep.get("url") for rep in main.coverage["representations"] if rep["format"] == "pdf")
            else:
                requested = resolved or normalize_arxiv(self.library.paper.get("source_url", ""))
                downloaded = download_public("https://arxiv.org/html/" + requested)
                source_id = "main-html-" + hashlib.sha256(downloaded.data).hexdigest()[:12]
                parsed = parse_structured_html(downloaded.data, source_id, url=downloaded.url, requested_id=requested)
                path = immutable_write(self.pipeline.work_dir, "source-versions/" + source_id + ".html", downloaded.data)
                url = downloaded.url
                from .structured_source import representation as source_representation
                if not any(rep.get("sha256") == hashlib.sha256(downloaded.data).hexdigest()
                           for rep in main.coverage.get("representations", [])):
                    main.coverage.setdefault("representations", []).append(source_representation(
                        path, self.pipeline.work_dir, "html", url, resolved))
            if source_id not in self.library.documents:
                parsed[1]["source_selection"] = {"format": representation, "reason": "Agent-requested alternate immutable representation",
                                                 "requested_id": resolved, "resolved_id": resolved}
                self.library._add(source_id, main.title, url, path, "paper", structured=parsed)
        def update(state):
            self._sync_sources(state)
            if source_id != "main":
                state.setdefault("alternate_sources", {})[source_id] = self.library.documents[source_id].sha256
            state["current_action"] = "已准备原文，Agent 可选择章节、检索或图表继续阅读"
            return {"source_id": source_id, "source_sha256": self.library.documents[source_id].sha256,
                    "title": main.title, "section_count": len(self.library.documents[source_id].sections),
                    "coverage": self._coverage(state), "representations": list(state["sources"].values()),
                    "next_action": "Use source_index for canonical locators and read_source in ranges. No original is overwritten."}
        return self._change("source_prepared", update, request_id=_call_id)

    def _tool_source_index(self, source_id="main", offset=0, limit=80, _call_id=None):
        document = self._document(source_id)
        rows = [{key: section[key] for key in ("id", "section", "kind", "role", "parent_id", "page") if key in section}
                | {"characters": len(section.get("text", ""))} for section in document.sections]
        def update(state):
            self._sync_sources(state)
            return {"source_id": source_id, "source_sha256": document.sha256, "sections": rows[offset:offset + limit],
                    "next_offset": offset + limit if offset + limit < len(rows) else None, "total_sections": len(rows),
                    "figures": [{key: figure[key] for key in ("id", "section_id", "caption", "page", "text_deficient") if key in figure}
                                for figure in document.coverage.get("figures", [])],
                    "tables": [{key: table[key] for key in ("id", "section_id", "caption") if key in table}
                               | {"rows": len(table.get("rows", []))} for table in document.coverage.get("tables", [])],
                    "references": [{key: entry[key] for key in ("id", "section_id", "page") if key in entry}
                                   for entry in document.coverage.get("references", [])]}
        return self._change("source_indexed", update, request_id=_call_id)

    def _document(self, source_id):
        if hasattr(self.pipeline, "_activate"):
            self.pipeline._activate(source_id)
        document = self.library.documents.get(source_id)
        if document is None:
            raise SourceError("Unknown source; prepare or fetch it first")
        return document

    def _tool_read_source(self, source_id="main", section_id=None, page=None, offset=0, limit=16000, _call_id=None):
        document = self._document(source_id)
        result = self.library.read_source(source_id, section_id, page, offset, limit)
        # Reconstruct exact spans for aliases/legacy parsers too; a heading may
        # contain descendants, so never mark the requested heading as all read.
        spans = result.get("section_spans")
        if spans is None:
            selected = document.sections
            if section_id is not None:
                selected = [section for section in selected if section["id"] == section_id]
                if not selected:
                    anchored = [section for section in document.sections if section_id in section.get("anchors", [])]
                    if anchored:
                        selected = [min(anchored, key=lambda section: (section.get("role") == "heading", len(section["text"])))]
                if selected and selected[0].get("role") == "heading":
                    identities = {selected[0]["id"]}
                    while True:
                        children = {section["id"] for section in document.sections if section.get("parent_id") in identities}
                        if children <= identities:
                            break
                        identities.update(children)
                    selected = [section for section in document.sections if section["id"] in identities]
            if page is not None:
                selected = [section for section in selected if section.get("page") == page]
            spans, position = [], 0
            for section in selected:
                spans.append({"section_id": section["id"], "start": position, "end": position + len(section["text"])})
                position += len(section["text"]) + 2
        def update(state):
            self._sync_sources(state)
            reads = state.setdefault("source_reads", {}).setdefault(source_id, {})
            for span in spans:
                left, right = max(offset, span["start"]), min(offset + len(result["text"]), span["end"])
                if right > left:
                    identity = span["section_id"]
                    reads[identity] = self._merge_ranges(reads.get(identity, []), left - span["start"], right - span["start"])
            state["current_action"] = "正在阅读原文：" + (section_id or source_id)
            return {**result, "source_sha256": document.sha256, "section_spans": spans}
        return self._change("source_read", update, request_id=_call_id)

    def _tool_search_source(self, query, source_id=None, limit=6, _call_id=None):
        _text(query, "Search query", 2000)
        documents = {source_id: self._document(source_id)} if source_id else self.library.documents
        tokens = set(re.findall(r"[\w-]+", query.casefold()))
        candidates = []
        for identity, document in documents.items():
            for section in document.sections:
                text = section.get("text", "")
                score = sum(min(3, text.casefold().count(token)) for token in tokens if len(token) > 1)
                if score:
                    locations = [text.casefold().find(token) for token in tokens if token in text.casefold()]
                    start = max(0, min(locations, default=0) - 300)
                    candidates.append((score, {"source_id": identity, "source_sha256": document.sha256,
                        "section_id": section["id"], "page": section.get("page"), "offset": start,
                        "text": text[start:start + 2400], "total_characters": len(text)}))
        candidates.sort(key=lambda item: -item[0])
        return {"query": query, "results": [result for _, result in candidates[:limit]],
                "instruction": "Read surrounding original ranges before asserting conditions; citation section IDs above are canonical."}

    def _tool_read_visual(self, figure_id, _call_id=None):
        image = self.pipeline.read_visual(figure_id)
        activity = getattr(self._review_local, "activity", None)
        if activity is not None:
            activity["visual_ids"].add(figure_id)
        metadata = {key: value for key, value in image.items() if key not in {"data", "base64", "content"}}
        def update(state):
            self._sync_sources(state)
            state.setdefault("visual_reads", {})[figure_id] = metadata
            state["current_action"] = "正在核对原始图像：" + figure_id
            return metadata
        # Never persist megabytes of image base64 in the task event journal.
        self._change("visual_read", update, request_id=_call_id)
        return image

    def _tool_read_table(self, table_id, source_id="main", offset=0, limit=20, _call_id=None):
        document = self._document(source_id)
        table = next((item for item in document.coverage.get("tables", []) if item["id"] == table_id), None)
        if table is None:
            raise SourceError("Unknown original structured table")
        rows = table.get("rows", [])
        def update(state):
            self._sync_sources(state)
            reads = state.setdefault("table_reads", {})
            identity = source_id + ":" + table_id
            reads[identity] = self._merge_ranges(reads.get(identity, []), min(offset, len(rows)), min(offset + limit, len(rows)))
            return {**{key: table[key] for key in ("id", "section_id", "caption", "footnotes") if key in table},
                    "source_id": source_id, "source_sha256": document.sha256, "rows": rows[offset:offset + limit],
                    "next_offset": offset + limit if offset + limit < len(rows) else None, "total_rows": len(rows)}
        return self._change("table_read", update, request_id=_call_id)

    def _tool_table_statistics(self, table_id, source_id="main", _call_id=None):
        self._document(source_id)
        return table_statistics(self.library, table_id, source_id)

    def _tool_search_papers(self, query, _call_id=None):
        return self.pipeline.search_papers(query)

    def _tool_fetch_related(self, id, url=None, _call_id=None):
        result = self.pipeline.fetch_related(id, url)
        def update(state):
            self._sync_sources(state)
            return result
        return self._change("related_source_prepared", update, request_id=_call_id)

    def _tool_read_skill_file(self, path, _call_id=None):
        result = self.pipeline.read_skill_file(path)
        def update(state):
            state.setdefault("skill_reads", {})[result["path"]] = _hash(result["content"])
            return result
        return self._change("guidance_read", update, request_id=_call_id)

    def _tool_save_plan(self, _call_id=None, **values):
        def update(state):
            for key, value in values.items():
                state[key] = copy.deepcopy(value)
            state["current_action"] = "Agent 已更新阅读计划"
            return {"saved": True, **values}
        return self._change("plan_updated", update, request_id=_call_id)

    def _claim(self, raw, state):
        claim = copy.deepcopy(raw)
        claim["conditions"] = {key: claim.get("conditions", {}).get(key, "") for key in sorted(CONDITIONS)}
        for item in claim["evidence"]:
            document = self._document(item["source_id"])
            if item.get("source_sha256", document.sha256) != document.sha256:
                raise PipelineError(item["id"] + ": evidence source hash does not match the original")
            item.pop("source_sha256", None)
        packet = {"version": 2, "paper_title": self.library.documents.get("main").title if self.library.documents.get("main") else "Paper",
                  "research_question": state.get("research_question") or "Explain the paper's claims and evidence",
                  "claims": [claim], "open_questions": [], "limitations": []}
        clean, errors = validate_packet(packet, self.library)
        if errors:
            raise PipelineError("; ".join(errors))
        claim = clean["claims"][0]
        numeric = claim.get("numeric_comparison")
        if numeric:
            original = " ".join(self.library.visual_observations.get(item.get("observation_id"), {}).get("observation", "")
                                if item.get("evidence_type") == "image_observation" else item.get("quote", "")
                                for item in claim["evidence"])
            numbers = set()
            for token in re.findall(r"(?<![\w.])[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\w.])", original.replace(",", "")):
                try:
                    numbers.add(Decimal(token.replace("−", "-")))
                except InvalidOperation:
                    pass
            if any(Decimal(str(numeric[key])) not in numbers for key in ("value", "baseline_value")):
                raise PipelineError(claim["id"] + ": numeric comparison values are not present in its original evidence; cite exact values and units")
        for item in claim["evidence"]:
            item["source_sha256"] = self.library.documents[item["source_id"]].sha256
        return claim

    def _tool_save_findings(self, claims, _call_id=None):
        if not claims:
            raise PipelineError("Provide 1–5 findings; accepted findings are never cleared by an empty batch")
        def update(state):
            self._check_sources(state)
            findings = state.setdefault("findings", {})
            accepted, rejected = [], []
            for raw in claims:
                try:
                    claim = self._claim(raw, state)
                    if claim["id"] not in findings and len(findings) >= MAX_CLAIMS:
                        raise PipelineError("Saved finding limit reached; refine existing findings")
                    other = {item["id"]: item for cid, old in findings.items() if cid != claim["id"] for item in old["evidence"]}
                    for item in claim["evidence"]:
                        if item["id"] in other and item != other[item["id"]]:
                            raise PipelineError(item["id"] + ": this evidence ID already has different content in another accepted claim")
                    findings[claim["id"]] = claim
                    accepted.append(claim["id"])
                except (PipelineError, PacketError, SourceError, ValueError, TypeError, KeyError) as exc:
                    rejected.append({"id": raw.get("id", "unknown"), "error": str(exc)[:3000]})
            state["evidence"] = {item["id"]: item for claim in findings.values() for item in claim["evidence"]}
            state["current_action"] = "已保存并逐条核验结论与证据"
            state["issue"] = rejected[0]["error"] if rejected else None
            return {"accepted": accepted, "rejected": rejected, "total_findings": len(findings),
                    "instruction": "Repair only rejected findings using original locators. Accepted siblings remain saved; continue reading all required content."}
        return self._change("findings_saved", update, request_id=_call_id)

    def _tool_save_visual_observations(self, observations, _call_id=None):
        if not observations:
            raise PipelineError("Provide 1–5 visual observations")
        def update(state):
            proposals = state.setdefault("visual_proposals", {})
            accepted, rejected = [], []
            for raw in observations:
                try:
                    identity, figure_id = raw["id"], raw["figure_id"]
                    if not re.fullmatch(r"V(?:P)?[1-9]\d{0,6}", identity):
                        raise PipelineError("Visual observation ID must be V1-style, or VP1 for text-deficient PDF page 1")
                    _text(raw["observation"], "Visual observation", 8000)
                    if any(not re.fullmatch(r"C[1-9]\d{0,6}", cid) for cid in raw["claim_ids"]):
                        raise PipelineError("Visual claim IDs must use C1-style identities")
                    visual = self.library.prepared_visuals.get(figure_id)
                    if not visual or figure_id not in state.get("visual_reads", {}):
                        raise PipelineError("Read the actual source image before proposing its observation")
                    if identity.startswith("VP") and (figure_id != "page:" + identity[2:] or visual.get("page") != int(identity[2:])):
                        raise PipelineError("Page recovery observation must match its exact PDF page")
                    record = {**copy.deepcopy(raw), "section_id": visual["section_id"], "image_sha256": visual["sha256"]}
                    if identity in self.library.visual_observations and self.library.visual_observations[identity] != record:
                        raise PipelineError("Accepted visual observations are immutable; choose a new observation ID")
                    proposals[identity] = record
                    accepted.append(identity)
                except (PipelineError, KeyError, ValueError) as exc:
                    rejected.append({"id": raw.get("id"), "error": str(exc)})
            state["current_action"] = "已保存待独立核验的图像观察"
            return {"saved_proposals": accepted, "rejected": rejected, "verified": False,
                    "next_action": "Request independent visual review. These proposals are not evidence until the host accepts that review."}
        return self._change("visual_observations_proposed", update, request_id=_call_id)

    def _tool_read_findings(self, offset=0, limit=5, claim_ids=None, _call_id=None):
        state = self._state()
        findings = list(state.get("findings", {}).values())
        if claim_ids is not None:
            findings = [claim for claim in findings if claim["id"] in claim_ids]
        selected = []
        for claim in findings[offset:offset + limit]:
            if selected and len(json.dumps([*selected, claim], ensure_ascii=False)) > 48000:
                break
            selected.append(claim)
        activity = getattr(self._review_local, "activity", None)
        if activity is not None:
            activity["claim_ids"].update(row["id"] for row in selected)
        end = offset + len(selected)
        observations = {item["observation_id"]: self.library.visual_observations.get(item["observation_id"])
                        for row in selected for item in row["evidence"] if item.get("evidence_type") == "image_observation"}
        return {"claims": selected, "visual_observations": observations, "total": len(findings),
                "next_offset": end if end < len(findings) else None}

    def _tool_edit_draft(self, section_id, view, title, content, expected_version, order=100, _call_id=None):
        _text(section_id, "Draft section ID", 100)
        if not re.fullmatch(r"[\w.-]+", section_id):
            raise PipelineError("Draft section ID must contain letters, digits, underscores, dots or hyphens")
        _text(content, "Draft content", 24000)
        def update(state):
            version = state.get("document_version", 0)
            if expected_version != version:
                raise PipelineError(f"Document version conflict: expected {expected_version}, current {version}; read_document then retry without losing other sections")
            drafts = state.setdefault("drafts", {})
            key = view + ":" + section_id
            section = {"section_id": section_id, "view": view, "title": title, "content": content, "order": order}
            if drafts.get(key) != section:
                if key not in drafts and len(drafts) >= 150:
                    raise PipelineError("Document section limit reached")
                if sum(len(row["content"]) for identity, row in drafts.items() if identity != key) + len(content) > 800000:
                    raise PipelineError("Document exceeds the total character limit")
                drafts[key] = section
                state["document_version"] = version + 1
                state.setdefault("draft_history", {})[str(version + 1)] = {"key": key, "section": copy.deepcopy(section)}
                if len(json.dumps(state["draft_history"], ensure_ascii=False).encode()) > 16 * 1024 * 1024:
                    raise PipelineError("Saved draft history exceeds the task size limit")
            state["current_action"] = "正在编写待核验草稿：" + (title or section_id)
            state["issue"] = None
            if self.chat:
                state["draft_content"] = self._render(state, "answer")
            return {"saved": True, "section_id": section_id, "view": view,
                    "document_version": state.get("document_version", version), "status": "pending_verification"}
        return self._change("draft_saved", update, request_id=_call_id)

    @staticmethod
    def _sections(state, view=None):
        return sorted([row for row in state.get("drafts", {}).values() if view is None or row["view"] == view],
                      key=lambda row: (row["order"], row["section_id"]))

    def _render(self, state, view):
        return "\n\n".join(("## " + row["title"].strip() + "\n\n" if row["title"].strip() else "") + row["content"].strip()
                           for row in self._sections(state, view))

    def _tool_read_document(self, view=None, section_id=None, offset=0, limit=5, _call_id=None):
        state = self._state()
        sections = [row for row in self._sections(state, view) if section_id is None or row["section_id"] == section_id]
        selected = []
        for section in sections[offset:offset + limit]:
            if selected and sum(len(row["content"]) for row in [*selected, section]) > 48000:
                break
            selected.append(section)
        activity = getattr(self._review_local, "activity", None)
        if activity is not None:
            activity["draft_ids"].update(row["view"] + ":" + row["section_id"] for row in selected)
        end = offset + len(selected)
        return {"document_version": state.get("document_version", 0), "status": "pending_verification",
                "sections": selected, "total": len(sections), "next_offset": end if end < len(sections) else None}

    def _validate(self, state):
        errors, evidence = [], []
        try:
            self._check_sources(state)
        except (PipelineError, SourceError, OSError) as exc:
            errors.append(str(exc))
        # A restarted process has no in-memory inspected_skill_files. Restore
        # only the host-recorded reads whose selected guidance still matches;
        # changed guidance requires another read and fresh independent reviews.
        for path, digest in state.get("skill_reads", {}).items():
            try:
                result = self.pipeline.read_skill_file(path)
                if result["path"] != path or _hash(result["content"]) != digest:
                    errors.append("Selected guidance changed since it was read: " + path)
            except (PipelineError, OSError) as exc:
                errors.append("Saved guidance is unavailable: " + path + ": " + str(exc))
        if not state.get("findings"):
            errors.append("No saved, source-supported findings")
        for claim in state.get("findings", {}).values():
            try:
                self._claim(claim, state)
            except (PipelineError, PacketError, SourceError, ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
        for item in state.get("evidence", {}).values():
            try:
                document = self.library.documents.get(item["source_id"])
                if document is None or item.get("source_sha256") != document.sha256:
                    raise PipelineError(item["id"] + ": evidence is bound to a different source version")
                resolved = resolve_evidence({key: value for key, value in item.items() if key in EVIDENCE_FIELDS}, self.library)
                resolved["source_sha256"] = item["source_sha256"]
                section = next(row for row in document.sections if row["id"] == resolved["section_id"])
                if resolved.get("quote"):
                    resolved["offset"] = section["text"].find(resolved["quote"])
                    resolved["end_offset"] = resolved["offset"] + len(resolved["quote"])
                evidence.append(resolved)
            except (PipelineError, PacketError, SourceError, ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
        ids = {item["id"] for item in evidence}
        for view in (["answer"] if self.chat else ["brief", "full"]):
            text = self._render(state, view)
            if not text.strip():
                errors.append("Missing saved " + view + " document")
                continue
            citations = set(re.findall(r"\]\(\s*evidence:([^)]*)\)", text))
            citations.update(re.findall(r"\[(E\d+)\](?!\()", text))
            if not citations:
                errors.append(view + ": no original evidence citations")
            errors.extend(view + ": unknown or malformed evidence citation " + identity for identity in sorted(citations - ids))
        coverage = self._coverage(state)
        if not self.chat:
            if not coverage["complete"]:
                for key in ("missing_sections", "missing_figures", "missing_tables"):
                    if coverage[key]:
                        errors.append(key + ": " + ", ".join(coverage[key][:30]))
                main = self.library.documents.get("main")
                if main is None or (main.coverage.get("full_text") is not True and not self._visual_recovery() and not self._alternate_coverage(state)):
                    errors.append("Main source does not have verified full-text coverage")
            if "SKILL.md" not in state.get("skill_reads", {}):
                errors.append("Read the selected guidance SKILL.md before formal delivery")
        return {"passed": not errors, "issues": list(dict.fromkeys(errors)), "evidence_index": evidence,
                "coverage": coverage, "bindings": self._bindings(state), "document_version": state.get("document_version", 0)}

    def _tool_validate_work(self, _call_id=None):
        def update(state):
            result = self._validate(state)
            state["validation"] = {key: value for key, value in result.items() if key != "evidence_index"}
            state["issue"] = result["issues"][0] if result["issues"] else None
            state["current_action"] = "已执行证据、数值、引文与覆盖范围校验"
            return {key: value for key, value in result.items() if key != "evidence_index"}
        return self._change("work_validated", update, request_id=_call_id)

    def review_context(self, kind):
        if kind not in REVIEW_KINDS:
            raise PipelineError("Unknown independent review kind")
        state = self._state()
        self._check_sources(state)
        sections = self._sections(state)
        main = self.library.documents.get("main")
        return {"kind": kind, "scope": "current question only" if self.chat else "entire paper including appendix, figures, tables, references",
            "goal": state.get("goal"), "question": self.spec.get("chat", {}).get("input", self.spec.get("chat", {})).get("question"),
            "document_version": state.get("document_version", 0), "bindings": self._bindings(state),
            "coverage": self._coverage(state), "research_question": state.get("research_question"),
            "conditions": state.get("conditions", []), "limitations": state.get("limitations", []),
            "open_questions": state.get("open_questions", []),
            "findings": [{"id": row["id"], "statement": row["statement"], "basis": row["basis"],
                "evidence_ids": [item["id"] for item in row["evidence"]]} for row in state.get("findings", {}).values()],
            "document_sections": [{key: value for key, value in row.items() if key != "content"}
                | {"characters": len(row["content"])} for row in sections],
            "document_preview": {view: self._render(state, view)[:16000] for view in (["answer"] if self.chat else ["brief", "full"])},
            "sources": list(state.get("sources", {}).values()),
            "visual_observation_proposals": list(state.get("visual_proposals", {}).values()),
            "accepted_visual_observations": list(state.get("accepted_visual_observations", {}).values()),
            "required_visual_ids": [row["id"] for row in main.coverage.get("figures", [])] if main and not self.chat else [],
            "deterministic_checks": {key: value for key, value in self._validate(state).items() if key != "evidence_index"},
            "reading_instructions": "This is an independent check, not the author's self-assessment. A FACT review must call read_findings until all saved claims and their original evidence/conditions have been read. A PRODUCT review must read_document until all sections omitted or truncated by the previews have been read. Verify against read_source/read_table as needed. A VISUAL review must call read_visual on every required visual AND every visual_observation_proposal figure; verify each proposal against actual pixels, conditions and uncertainty. VPn proposals must faithfully cover all readable material on text-deficient page n, not just selected claims. You may review visual proposals before the draft exists; checks irrelevant to your review kind need not pass yet. Return concrete issues and do not approve unchecked required content."}

    def _tool_request_review(self, kind, _call_id=None):
        if self.chat and kind == "coverage":
            raise PipelineError("Chat requires question-scoped fact/product checks; optional visual review can attest image evidence")
        state = self._state()
        existing = state.get("reviews", {}).get("review:" + _call_id)
        if existing:
            return existing
        context = self.review_context(kind)
        # No DB transaction is held during a model request. The session can read
        # through host tools, and an intervening edit will invalidate its result.
        self._review_local.activity = {"visual_ids": set(), "claim_ids": set(), "draft_ids": set()}
        try:
            report = self.review(kind, context)
            activity = self._review_local.activity
            actual_visuals = sorted(activity["visual_ids"])
        finally:
            self._review_local.activity = None
        self._cancel()
        if (not isinstance(report, dict) or type(report.get("passed")) is not bool or
                not isinstance(report.get("issues"), list) or len(report["issues"]) > 100 or
                any(not isinstance(issue, str) or not issue.strip() or len(issue) > 4000 for issue in report["issues"]) or
                (report["passed"] and report["issues"])):
            report = {"passed": False, "issues": ["Independent reviewer returned an invalid verdict"]}
        else:
            report = {"passed": report["passed"], "issues": list(report["issues"])}
        if kind == "visual":
            missing = (set(context["required_visual_ids"]) | {row["figure_id"] for row in context["visual_observation_proposals"]}) - set(actual_visuals)
            if missing:
                report = {"passed": False, "issues": [*report["issues"], "Independent visual reviewer did not inspect actual images: " + ", ".join(sorted(missing))]}
        if kind == "fact":
            missing = set(state.get("findings", {})) - activity["claim_ids"]
            if missing:
                report = {"passed": False, "issues": [*report["issues"], "Independent fact reviewer did not read saved claims and original evidence: " + ", ".join(sorted(missing))]}
        if kind == "product":
            # Complete small previews count as reading. Longer views require
            # explicit bounded reads so a truncation cannot receive a pass.
            missing = {row["view"] + ":" + row["section_id"] for row in self._sections(state)
                       if len(self._render(state, row["view"])) > 16000} - activity["draft_ids"]
            if missing:
                report = {"passed": False, "issues": [*report["issues"], "Independent product reviewer did not read full draft sections: " + ", ".join(sorted(missing))]}
        if not report["passed"] and not report["issues"]:
            report["issues"] = ["Independent reviewer did not approve this version"]
        def update(current):
            self._check_sources(current)
            if self._bindings(current) != context["bindings"]:
                report["passed"] = False
                report["issues"].append("Work changed during review; review the current saved version")
            if kind == "visual" and report["passed"]:
                for proposal in context["visual_observation_proposals"]:
                    if proposal["readable"] is True:
                        self.library.visual_observations[proposal["id"]] = copy.deepcopy(proposal)
                        current.setdefault("accepted_visual_observations", {})[proposal["id"]] = copy.deepcopy(proposal)
                if context["visual_observation_proposals"]:
                    self.library._save()
            record = {"id": "review:" + _call_id, "kind": kind, **report, "bindings": context["bindings"],
                      "document_version": context["document_version"], "inspected_visual_ids": actual_visuals,
                      "issuer": "host-independent-pi-session"}
            if kind == "visual" and report["passed"]:
                # Only host acceptance changed: bind the review to the exact
                # newly attested observations and validated recovery coverage.
                record["bindings"] = self._bindings(current)
            current.setdefault("reviews", {})[record["id"]] = record
            current["current_action"] = "独立检查已完成：" + kind
            current["issue"] = report["issues"][0] if report["issues"] else None
            return record
        return self._change("independent_review_completed", update, request_id=_call_id)

    def _tool_run_python(self, code, inputs, _call_id=None):
        from .agent_sandbox import run_python
        prior = self._state().get("sandbox_runs", {}).get(_call_id)
        if prior is not None:
            return copy.deepcopy(prior)
        inputs = [str(task_path(self.pipeline.work_dir, value, must_exist=True).relative_to(self.pipeline.work_dir))
                  for value in inputs]
        # Scripts receive only source/artifact files, never checkpoints or host
        # execution metadata even when such files live beneath the task root.
        allowed = {document.path for document in self.library.documents.values()}
        allowed.update(rep["path"] for document in self.library.documents.values() for rep in document.coverage.get("representations", []))
        allowed.update(row["path"] for row in self.library.prepared_visuals.values())
        allowed.update(self._state().get("derived_files", []))
        if any(str(task_path(self.pipeline.work_dir, value).relative_to(self.pipeline.work_dir)) not in allowed for value in inputs):
            raise PipelineError("Sandbox inputs must be registered source, visual or prior derived files")
        result = run_python(self.pipeline.work_dir, code, inputs, self.pipeline.cancelled)
        def update(state):
            files = state.setdefault("derived_files", [])
            for row in result.get("files", []):
                path = row.get("path") if isinstance(row, dict) else row
                if isinstance(path, str) and path not in files:
                    task_path(self.pipeline.work_dir, path, must_exist=True)
                    files.append(path)
            saved = {**result, "evidence_status": "Derived material; original source checks are still required"}
            state.setdefault("sandbox_runs", {})[_call_id] = saved
            return saved
        return self._change("isolated_python_completed", update, request_id=_call_id)

    def _required_reviews(self):
        return {"fact", "product"} if self.chat else REVIEW_KINDS

    def _gate(self, state, document_version, review_ids):
        checked = self._validate(state)
        issues = list(checked["issues"])
        if document_version != state.get("document_version", 0):
            issues.append("Submission document version is stale")
        kinds = set()
        for identity in review_ids:
            record = state.get("reviews", {}).get(identity)
            if not record or record.get("issuer") != "host-independent-pi-session":
                issues.append("Unknown host review record: " + identity)
            elif not record.get("passed"):
                issues.append("Review has unresolved issues: " + identity)
            elif record.get("bindings") != checked["bindings"]:
                issues.append("Review is stale after source/findings/document changes: " + identity)
            else:
                kinds.add(record["kind"])
        missing = self._required_reviews() - kinds
        if missing:
            issues.append("Missing current passing independent reviews: " + ", ".join(sorted(missing)))
        # This private in-memory attestation is consumed only by the existing
        # host publication validator. It is never accepted from model output.
        alternate = self._alternate_coverage(state) if not issues and not self.chat else None
        self.library.agent_alternate_coverage = ({"source_id": alternate["source_id"], "sha256": alternate["sha256"]}
                                                 if alternate else None)
        return checked, list(dict.fromkeys(issues))

    def _tool_submit_final(self, document_version, review_ids, _call_id=None):
        def update(state):
            checked, issues = self._gate(state, document_version, review_ids)
            if issues:
                state["issue"] = issues[0]
                return {"accepted": False, "issues": issues, "document_version": state.get("document_version", 0),
                        "instruction": "Continue from saved work. Repair or obtain missing checks; no formal reading/answer was published."}
            submission = {"accepted": True, "submission_id": "submission:" + _call_id,
                          "document_version": document_version, "review_ids": review_ids, "bindings": checked["bindings"]}
            state["submission"] = submission
            state["current_action"] = "全部必要检查已通过，正在交付"
            state["issue"], state["remaining_work"] = None, []
            return submission
        return self._change("final_submission_checked", update, request_id=_call_id)

    def final_result(self, submission):
        self._cancel()
        state = self._state()
        saved = state.get("submission")
        if not isinstance(submission, dict) or not saved or submission != saved or not saved.get("accepted"):
            raise PipelineError("Formal publication requires the exact host-issued saved submission")
        checked, issues = self._gate(state, saved["document_version"], saved["review_ids"])
        if issues or saved["bindings"] != checked["bindings"]:
            raise PipelineError("Final publication gate: " + "; ".join(issues or ["Saved work changed after final submission"]))
        reviews = {state["reviews"][identity]["kind"]: state["reviews"][identity] for identity in saved["review_ids"]}
        verification = {"passed": True, "execution_engine": ENGINE, "evidence_check": {"passed": True, "issues": []},
                        "reviews": reviews, "coverage": checked["coverage"], "issues": [],
                        "limitations": state.get("limitations", []), "product_review": reviews["product"],
                        "fact_review": reviews["fact"], "server_evidence_check": "passed",
                        **({kind + "_review": reviews[kind] for kind in ("coverage", "visual")} if not self.chat else {})}
        recovery = self._visual_recovery()
        if recovery:
            verification["source_visual_coverage"] = recovery
        if self.chat:
            verification.update(quote_match={"passed": True, "issues": []}, independent_review=reviews["product"])
            return {"status": "succeeded", "content": self._render(state, "answer"),
                    "evidence_index": checked["evidence_index"], "verification": verification,
                    "execution_engine": ENGINE}
        result = {"brief_markdown": self._render(state, "brief"), "full_markdown": self._render(state, "full"),
                  "evidence_index": checked["evidence_index"], "verification": verification,
                  "provenance": {"execution_engine": ENGINE, "document_version": saved["document_version"],
                                 "submission_id": saved["submission_id"], "bindings": saved["bindings"]}, "artifacts": [],
                  "reading_packet": {"version": 2, "paper_title": self.library.documents["main"].title,
                    "research_question": state.get("research_question") or state.get("goal", "Paper reading"),
                    "claims": [{**claim, "evidence": [{key: value for key, value in item.items() if key in EVIDENCE_FIELDS}
                                                       for item in claim["evidence"]]} for claim in state.get("findings", {}).values()],
                    "open_questions": [], "limitations": state.get("limitations", [])}}
        _, errors = validate_result(result, self.library, workflow_mode="guided", cross_paper_requested=False)
        if errors:
            raise PipelineError("Final existing evidence validator rejected publication: " + "; ".join(errors))
        return result

    def _tool_wait_for_input(self, reason, _call_id=None):
        _text(reason, "Waiting reason", 2000)
        def update(state):
            state["issue"] = reason
            state["current_action"] = "任务已保存，待继续"
            return {"status": "waiting", "reason": reason, "accepted_work_saved": True}
        # The runtime releases the lease after checkpointing the tool response;
        # doing it here would fence out that final durable checkpoint.
        return self._change("agent_wait_requested", update, request_id=_call_id)
