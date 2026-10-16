"""Versioned structured-source workflow with bounded, independently durable reads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import re
import threading
import time

from .evidence import packet_evidence, validate_packet
from .pipeline import BudgetExhausted, PipelineError, TaskCancelled
from .table_math import all_table_statistics
from .workflow import (GuidedWorkflow, PHASE_OUTPUT_CAPS, PHASE_TOOL_CAPS,
                       apply_product_patch, canonical, digest, validate_phase)

VERSION = "guided-v3.0"
LONG_BYTES = 64 * 1024
SEGMENT_BYTES = 32 * 1024
CAPS = {**PHASE_OUTPUT_CAPS, "chunk_extract": 7500, "figure_review": 3500,
        "merge_review": 5000, "compose": 12000, "packet_repair": 7000, "fact_review": 6000}
TOOLS = {**PHASE_TOOL_CAPS, "chunk_extract": 16, "figure_review": 10, "merge_review": 30}


def text_size(sections):
    return sum(len(s.get("text", "").encode("utf-8")) for s in sections)


def _split_text(text: str, maximum: int) -> list[str]:
    """Split without losing a single original character; prefer paragraph ends."""
    parts = []
    while len(text.encode()) > maximum:
        end = len(text.encode()[:maximum].decode("utf-8", errors="ignore"))
        boundary = text.rfind("\n\n", 0, end)
        if boundary > end // 2:
            end = boundary + 2
        else:
            boundary = text.rfind("\n", 0, end)
            if boundary > end // 2:
                end = boundary + 1
        if not end:
            raise PipelineError("Source block cannot fit the segment budget")
        parts.append(text[:end])
        text = text[end:]
    if text:
        parts.append(text)
    return parts


def segment_sections(sections: list[dict], maximum: int = SEGMENT_BYTES) -> list[dict]:
    """Paragraph IDs remain evidence anchors; large blocks retain source offsets."""
    if maximum < 1024 or maximum > SEGMENT_BYTES:
        raise PipelineError("Invalid original-text segment budget")
    blocks = []
    for section in sections:
        if section.get("kind") in {"references", "reference", "bibliography"}:
            continue
        text = section.get("text", "")
        if not text.strip():
            continue
        if section.get("role") == "table" and isinstance(section.get("table"), dict):
            table = section["table"]
            rows = table.get("rows", [])
            headers = [row for row in rows if row and all(c.get("header") for c in row)]
            data_rows = [row for row in rows if row not in headers]
            caption = table.get("caption", "")
            header_text = "\n".join(" | ".join(c["text"] for c in row) for row in headers)
            prefix = "\n".join(filter(None, [caption, header_text]))
            prefix_bytes = len(prefix.encode()) + 1
            if prefix_bytes >= maximum:
                raise PipelineError("Table headers exceed segment budget; full headers cannot be retained")
            group, group_bytes = [], prefix_bytes
            def append_group():
                selected = headers + group
                body = "\n".join(" | ".join(c["text"] for c in row) for row in group)
                blocks.append({**section, "text": "\n".join(filter(None, [prefix, body])),
                               "table": {**table, "rows": selected}, "continued": len(data_rows) != len(group),
                               "row_group": [c["row"] for row in group for c in row]})
            for row in data_rows:
                length = len(" | ".join(c["text"] for c in row).encode()) + 1
                if prefix_bytes + length > maximum:
                    raise PipelineError("A table row and its complete headers exceed the segment budget")
                if group and group_bytes + length > maximum:
                    append_group()
                    group, group_bytes = [], prefix_bytes
                group.append(row)
                group_bytes += length
            if group or not data_rows:
                append_group()
            # Footnotes are source evidence too, associated with the same table.
            notes = "\n".join(table.get("footnotes", []))
            if notes:
                for part in _split_text(notes, maximum):
                    blocks.append({**section, "text": part, "table": {**table, "rows": headers},
                                   "continued": True, "role": "table_footnotes"})
            continue
        if section.get("role") in {"equation", "figure"} and len(text.encode()) > maximum:
            raise PipelineError("An indivisible equation or figure caption exceeds the segment budget")
        offset = 0
        for part in _split_text(text, maximum):
            blocks.append({**section, "text": part, "source_offset": offset,
                           "source_end": offset + len(part), "continued": len(part) != len(text)})
            offset += len(part)
    segments, current, size = [], [], 0
    for block in blocks:
        length = len(block["text"].encode())
        if current and size + length > maximum:
            segments.append({"id": f"segment_{len(segments) + 1}", "sections": current,
                             "section_ids": list(dict.fromkeys(s["id"] for s in current)), "bytes": size})
            current, size = [], 0
        current.append(block)
        size += length
    if current:
        segments.append({"id": f"segment_{len(segments) + 1}", "sections": current,
                         "section_ids": list(dict.fromkeys(s["id"] for s in current)), "bytes": size})
    if not segments:
        raise PipelineError("No body or appendix sections are available")
    if len(segments) > 80:
        raise PipelineError("Paper exceeds the bounded 80-segment workflow; full coverage cannot be claimed")
    return segments


def combine_packets(packets: list[dict]) -> dict:
    """Lossless combination; never drop an appendix finding to reach a claim cap."""
    result = {"version": 2, "paper_title": packets[0]["paper_title"],
              "research_question": packets[0]["research_question"], "claims": [],
              "open_questions": [], "limitations": []}
    evidence_counter = 0
    for packet in packets:
        eids = {}
        for raw in packet["claims"]:
            claim = deepcopy(raw)
            claim["id"] = f"C{len(result['claims']) + 1}"
            for item in claim["evidence"]:
                if item["id"] not in eids:
                    evidence_counter += 1
                    eids[item["id"]] = f"E{evidence_counter}"
                item["id"] = eids[item["id"]]
            result["claims"].append(claim)
        for field in ("limitations", "open_questions"):
            for entry in packet[field]:
                if entry not in result[field]:
                    result[field].append(deepcopy(entry))
    if len(result["claims"]) > 80 or len(result["limitations"]) > 40 or len(result["open_questions"]) > 3:
        raise PipelineError("Combined findings exceed the fact-packet bound; segment findings retained without silent omissions")
    return result


def validate_review(report: dict, packet: dict) -> dict:
    if (not isinstance(report, dict) or type(report.get("passed")) is not bool
            or not isinstance(report.get("issues"), list) or not isinstance(report.get("suggestions"), list)):
        raise PipelineError("Invalid independent fact review")
    ids = {c["id"] for c in packet["claims"]}
    checked = report.get("checked_claim_ids", [])
    if not isinstance(checked, list) or len(checked) != len(ids) or set(checked) != ids:
        raise PipelineError("Independent review did not cover every retained claim")
    for issue in report["issues"]:
        if (not isinstance(issue, dict) or issue.get("claim_id") not in ids
                or not isinstance(issue.get("message"), str) or not issue["message"].strip()):
            raise PipelineError("Review contains an invalid claim issue")
    if report["passed"] and report["issues"]:
        raise PipelineError("Review cannot pass with unresolved issues")
    return deepcopy(report)


def compact_phase_input(phase: str, inputs: dict) -> dict:
    """Move duplicate original quotations to a shared prompt index, losslessly.

    Extraction and claim patches keep the complete packet schema. Review and
    composition outputs do not return packets, so their input can reference
    shared excerpts without duplicating paragraphs in every claim.
    """
    if phase in {"extract", "chunk_extract", "packet_repair"}:
        return inputs
    output = deepcopy(inputs)
    excerpts = {}
    for key in ("packet", "previous_packet"):
        packet = output.get(key)
        if not isinstance(packet, dict):
            continue
        for claim in packet.get("claims", []):
            for evidence in claim.get("evidence", []):
                if evidence.get("evidence_type", "text_quote") != "text_quote" or not evidence.get("quote"):
                    continue
                text = evidence.pop("quote")
                identity = (evidence["source_id"], evidence["section_id"], evidence.get("page"), text)
                if identity not in excerpts:
                    excerpts[identity] = {"source_id": evidence["source_id"], "section_id": evidence["section_id"],
                                          "text": text, "evidence_ids": [], **({"page": evidence["page"]} if "page" in evidence else {})}
                if evidence["id"] not in excerpts[identity]["evidence_ids"]:
                    excerpts[identity]["evidence_ids"].append(evidence["id"])
    if excerpts:
        output["source_excerpts"] = list(excerpts.values())
        if phase == "merge_review":
            # Every supporting original passage is already present in the
            # excerpt index. Merge also receives the full section index and can
            # reread neighboring original paragraphs on demand. Do not copy
            # whole table metadata into each overlapping quote window again.
            output["source_sections"] = [
                {key: section[key] for key in ("id", "source_id", "section", "page", "kind", "text", "source_method", "visual_evidence", "excerpt", "source_offset", "source_end") if key in section}
                for section in output.get("source_sections", []) if section.get("source_method") == "vision"]
    return output


def validate_gap_resolutions(report: dict, packet: dict, gaps: list[dict], figures: dict) -> dict:
    report = validate_review(report, packet)
    resolutions = report.get("gap_resolutions", [])
    if not isinstance(resolutions, list) or len(resolutions) != len(gaps) or {r.get("id") for r in resolutions} != {g["id"] for g in gaps}:
        raise PipelineError("Merge review must explicitly account for every pending segment question")
    evidence_ids = {e["id"] for c in packet["claims"] for e in c["evidence"]}
    verified_figures = {f["figure_id"] for f in figures.get("items", []) if f.get("status") == "verified"}
    for resolution in resolutions:
        if (resolution.get("status") not in {"resolved", "retained_limitation", "unresolved"}
                or not isinstance(resolution.get("explanation"), str) or not resolution["explanation"].strip()
                or len(resolution["explanation"]) > 2000
                or not isinstance(resolution.get("evidence_ids"), list) or set(resolution["evidence_ids"]) - evidence_ids
                or not isinstance(resolution.get("figure_ids"), list) or set(resolution["figure_ids"]) - verified_figures):
            raise PipelineError("Invalid gap resolution or unverified supporting evidence")
        if resolution["status"] != "unresolved" and not (resolution["evidence_ids"] or resolution["figure_ids"]):
            raise PipelineError("Closing a segment question requires retained evidence or a verified figure")
        if report["passed"] and resolution["status"] == "unresolved":
            raise PipelineError("Merge cannot pass with an unresolved required source question")
    return report


class StructuredWorkflow(GuidedWorkflow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.common["rules"] = VERSION
        self.common["source_rules"] = self.spec.get("source_rules_version")
        from .structured_source import RENDER_RULES_VERSION
        self.common["render_rules"] = RENDER_RULES_VERSION
        self.common["limits"] = self.spec.get("limits")
        self.state_lock = self.pipeline.state_lock
        self.record_lock = threading.RLock()
        self.sections = []
        self.segments = []
        self.segment_status = {}
        self.figure_items = []
        self.unresolved = []
        self.gap_resolutions = []
        self.long_document = False
        self.source_visual_coverage = {"complete": False, "recovered_pages": [], "observation_ids": []}

    def source(self):
        try:
            return super().source()
        except PipelineError as exc:
            main = self.pipeline.library.documents.get("main")
            if str(exc) == "Main paper has no usable original sections" and main and main.coverage.get("missing_text_pages"):
                return [{**s, "source_id": "main"} for s in main.sections]
            raise

    def checkpoint(self, name, inputs, compute, *, phase=None, validator=None, allow_reuse=True):
        """Cache recovery and stage writes share source-state locking with tool RPCs."""
        self.check_budget()
        input_hash = digest({"common": self.common, "source": self.source_fingerprint, "name": name, "input": inputs})
        path = self.root / f"{name}.json"
        with self.state_lock, self.record_lock:
            anchors = [r for r in self.records if r.get("name") == name and r.get("status") in {"succeeded", "reused"}
                       and r.get("input_hash") == input_hash]
            if allow_reuse and path.is_file() and not path.is_symlink():
                try:
                    saved = json.loads(path.read_text())
                    output = saved["output"]
                    anchor = next(r for r in reversed(anchors) if r.get("output_hash") == digest(output))
                    if saved["input_hash"] != input_hash or saved["output_hash"] != anchor["output_hash"]:
                        raise ValueError("Changed checkpoint")
                    source = saved["source_checkpoint"]
                    if digest(source) != anchor.get("source_hash") or saved.get("source_hash") != anchor["source_hash"]:
                        raise ValueError("Changed source snapshot")
                    # Validate a cached snapshot without rolling back newer
                    # authenticated searches, observations or representations.
                    library = self.pipeline.library
                    current_source = library.export_checkpoint() if name != "source" else None
                    library.restore_checkpoint(source, expected_sha256=anchor["source_hash"])
                    if current_source is not None:
                        library.restore_checkpoint(current_source, expected_sha256=digest(current_source))
                    if validator:
                        output = validator(output)
                    record = self.save_stage(name, "reused", input_hash=input_hash, output=output,
                                             reused_from=anchor.get("reused_from") or {"job_id": anchor.get("job_id", self.job.id),
                                                                                     "attempt": anchor.get("attempt")})
                    self.records.append(record)
                    self.outputs[name], self.completed[name] = deepcopy(output), record
                    return output
                except (KeyError, StopIteration, ValueError, OSError, PipelineError):
                    pass
        started = time.monotonic()
        self.save_stage(name, "running", input_hash=input_hash)
        try:
            output = compute()
            self.check_budget()
            with self.state_lock, self.record_lock:
                if validator:
                    output = validator(output)
                usage = deepcopy(getattr(self.pipeline.phase_local, "usage", {})) if phase else {}
                record = self.save_stage(name, "succeeded", input_hash=input_hash, output=output,
                                         duration_ms=getattr(self.pipeline.phase_local, "call_duration_ms", round((time.monotonic() - started) * 1000)) if phase else round((time.monotonic() - started) * 1000),
                                         total_duration_ms=round((time.monotonic() - started) * 1000), usage=usage)
                self.records.append(record)
                self.outputs[name], self.completed[name] = deepcopy(output), record
            return output
        except Exception as exc:
            if not isinstance(exc, TaskCancelled):
                self.save_stage(name, "failed", input_hash=input_hash, error=str(exc)[:1000],
                                duration_ms=getattr(self.pipeline.phase_local, "call_duration_ms", round((time.monotonic() - started) * 1000)) if phase else round((time.monotonic() - started) * 1000),
                                total_duration_ms=round((time.monotonic() - started) * 1000),
                                usage=deepcopy(getattr(self.pipeline.phase_local, "usage", {})) if phase else {})
            raise

    def model_stage(self, name, phase, inputs, validator=None):
        def execute():
            output_cap = min(CAPS[phase], int(self.spec["model"].get("max_tokens", 8192)))
            budget = int(self.spec["model"].get("context_window", 128000)) - output_cap - 6000
            attempt_inputs = compact_phase_input(phase, inputs)
            # Evidence proposals have their own diagnostic-driven, three-round
            # recovery loop; do not multiply it by the generic transport retry.
            attempts = 1 if phase == "packet_repair" else 2
            for attempt in range(1, attempts + 1):
                self.check_budget()
                with self.state_lock:
                    remaining = self.pipeline.max_tool_calls - self.pipeline.tool_calls
                if remaining < 1:
                    raise BudgetExhausted("Shared tool budget exhausted; completed segments retained")
                if len(canonical(attempt_inputs).encode()) > max(0, budget):
                    sizes = ", ".join(f"{key}={len(canonical(value).encode())}" for key, value in attempt_inputs.items())
                    raise PipelineError(f"{name}: complete bounded input exceeds safe context budget ({budget} bytes; {sizes}); retained findings were not truncated")
                self.pipeline.phase_local.checkpoint_name = name if attempt == 1 else name + "_retry"
                started = time.monotonic()
                try:
                    output = self.run_phase(phase, attempt_inputs, min(300, max(1, int(self.deadline - time.monotonic()))),
                                            min(remaining, TOOLS[phase]), output_cap)
                    self.pipeline.phase_local.call_duration_ms = round((time.monotonic() - started) * 1000)
                    return validate(output)
                except PipelineError as exc:
                    self.pipeline.phase_local.call_duration_ms = round((time.monotonic() - started) * 1000)
                    abort = getattr(self.pipeline, "segment_abort", None)
                    if attempt == attempts or isinstance(exc, (TaskCancelled, BudgetExhausted)) or (abort is not None and abort.is_set()):
                        raise
                    self.check_budget()
                    record = self.save_stage(name + "_failed_call", "failed", input_hash=digest(attempt_inputs),
                        error=str(exc)[:1000], duration_ms=round((time.monotonic() - started) * 1000),
                        usage=deepcopy(getattr(self.pipeline.phase_local, "usage", {})))
                    with self.record_lock:
                        self.records.append(record)
                    attempt_inputs = {**compact_phase_input(phase, inputs), "instruction": inputs.get("instruction", "") +
                        "\nRecovery of this phase only: the previous call failed (" + str(exc)[:300] +
                        "). Use the supplied original context and return only the required structured submission. "
                        "Keep required findings and conditions; use concise wording and no narrated analysis."}
        def validate(output):
            if not isinstance(output, dict) or len(canonical(output).encode()) > 2 * 1024 * 1024:
                raise PipelineError("Invalid bounded phase result")
            if phase in {"compose", "product_review", "product_repair", "revision_plan", "packet_repair"}:
                output = validate_phase(phase, output)
            return validator(output) if validator else output
        return self.checkpoint(name, inputs, execute, phase=phase, validator=validate)

    def _source_inventory(self):
        main = self.pipeline.library.documents["main"]
        inventory = main.coverage.get("section_inventory") or [
            {"id": s["id"], "title": s.get("section", s["id"]), "kind": s.get("kind", "page")} for s in self.sections]
        return {"source_selection": main.coverage.get("source_selection", {}), "section_inventory": inventory,
                "figures": main.coverage.get("figures", []), "references": main.coverage.get("references", []),
                "cache_recovery": getattr(self.pipeline.library, "cache_recovery", None)}

    def _coverage(self):
        rows = []
        for section in self.sections:
            related = [seg["id"] for seg in self.segments if section["id"] in seg["section_ids"]]
            is_reference = section.get("kind") in {"references", "reference", "bibliography"}
            statuses = [self.segment_status.get(s, "pending") for s in related]
            status = "indexed" if is_reference else ("succeeded" if statuses and all(s in {"succeeded", "reused"} for s in statuses)
                                                     else "failed" if "failed" in statuses else "pending")
            rows.append({"id": section["id"], "title": section.get("section", section["id"]),
                         "kind": section.get("kind", "page"), "status": status, "segment_ids": related})
        processed = sum(r["status"] in {"succeeded", "reused", "indexed"} for r in rows)
        return {"total": len(rows), "processed": processed, "complete": bool(rows) and processed == len(rows),
                "sections": rows, "segments": [{"id": s["id"], "section_ids": s["section_ids"],
                "status": self.segment_status.get(s["id"], "pending")} for s in self.segments], "unresolved": list(self.unresolved),
                "gap_resolutions": deepcopy(self.gap_resolutions)}

    def _figure_projection(self):
        verified = sum(item["status"] == "verified" for item in self.figure_items)
        return {"total": len(self.figure_items), "verified": verified, "complete": verified == len(self.figure_items),
                "items": deepcopy(self.figure_items), "unresolved": [f"{i['figure_id']}: {u}"
                for i in self.figure_items if i["status"] != "verified" for u in i.get("uncertainties", ["not verified"])]}

    def coverage_projection(self):
        return {"source_selection": self._source_inventory()["source_selection"],
                "section_coverage": self._coverage(), "figure_checks": self._figure_projection(),
                "source_visual_coverage": deepcopy(self.source_visual_coverage)}

    def _recover_pdf_text(self):
        """Visual transcripts are reading inputs, never substituted source quotes."""
        main = self.pipeline.library.documents["main"]
        missing = main.coverage.get("missing_text_pages", [])
        if not missing:
            return
        if not self.spec["model"].get("vision"):
            raise PipelineError("PDF text coverage has missing pages and the configured model cannot visually recover them")
        empty = {"version": 2, "paper_title": main.title, "research_question": "Visual source recovery",
                 "claims": [], "open_questions": [], "limitations": []}
        recovered, observation_ids = [], []
        for page in missing:
            figure = next((f for f in main.coverage.get("figures", []) if f.get("page") == page), None)
            if not figure:
                raise PipelineError(f"Missing PDF page {page} has no renderable source representation")
            report = self.model_stage(f"page_{page}_transcription", "figure_review",
                    {"figures": [figure], "packet": empty, "source_sections": [], "purpose": "page_transcription"},
                    self._visual_validator([figure], empty))
            if not report["passed"] or not report["observations"][0]["readable"]:
                raise PipelineError(f"PDF page {page} remains unreadable after actual visual recovery")
            observation = report["observations"][0]
            oid = f"VP{page}"
            with self.state_lock:
                prior = self.pipeline.library.visual_observations.get(oid, {})
                if all(prior.get(key) == observation.get(key) for key in ("image_sha256", "section_id", "observation", "conditions", "readable", "uncertainties")):
                    observation = {**observation, "claim_ids": list(prior.get("claim_ids", []))}
                self.pipeline.library.visual_observations[oid] = {"id": oid, **observation}
                self.pipeline.library._save()
            self.sections = [{**s, "text": observation["observation"], "source_method": "vision",
                              "visual_evidence": {"figure_id": figure["id"], "image_sha256": observation["image_sha256"],
                                                  "observation_id": oid, "section_id": s["id"]}}
                             if s.get("page") == page else s for s in self.sections]
            recovered.append(page)
            observation_ids.append(oid)
        self.source_visual_coverage = {"complete": set(recovered) == set(missing), "recovered_pages": recovered,
                                       "observation_ids": observation_ids}
        self.checkpoint("source_visual_recovery", self.source_visual_coverage, lambda: self.source_visual_coverage)

    def _associate_visual_evidence(self, packet, *, replace=False):
        """Record claim linkage proposed by extraction; independent reviews follow."""
        links = {}
        for claim in packet.get("claims", []):
            for evidence in claim.get("evidence", []):
                if evidence.get("evidence_type") == "image_observation":
                    links.setdefault(evidence.get("observation_id"), []).append(claim["id"])
        with self.state_lock:
            for oid, ids in links.items():
                record = self.pipeline.library.visual_observations.get(oid)
                if record and oid in self.source_visual_coverage["observation_ids"]:
                    record["claim_ids"] = list(dict.fromkeys(([] if replace else record.get("claim_ids", [])) + ids))

    def _save_coverage(self):
        with self.state_lock:
            output = self._coverage()
            self.checkpoint("section_coverage", output, lambda: output, allow_reuse=False)

    def _packet_context(self, packet, *, full_sections=False):
        if full_sections:
            return self.sections
        ids = {e["section_id"] for c in packet["claims"] for e in c["evidence"] if e["source_id"] == "main"}
        chosen = [s for s in self.sections if s["id"] in ids]
        # Original passages are retained and available through read_source even
        # when evidence spans alone must be passed to a large merge/review.
        if text_size(chosen) > 40000:
            windows = []
            for section in chosen:
                if section.get("source_method") == "vision":
                    # Image evidence has an intentionally empty quote. Keep its
                    # explicitly labelled transcript context and identity, with
                    # complete observations still available through read_source.
                    text = section["text"]
                    windows.append({**section, "text": text[:2000], "source_offset": 0,
                                    "source_end": min(2000, len(text)), "excerpt": len(text) > 2000})
                    continue
                quotes = [e["quote"] for c in packet["claims"] for e in c["evidence"]
                          if e["source_id"] == "main" and e["section_id"] == section["id"] and e.get("quote")]
                ranges = []
                for quote in quotes:
                    pos = section["text"].find(quote)
                    if pos >= 0:
                        start, end = max(0, pos - 200), min(len(section["text"]), pos + len(quote) + 200)
                        if not any(a <= start and end <= b for a, b in ranges):
                            ranges.append((start, end))
                for start, end in ranges:
                    windows.append({**section, "text": section["text"][start:end], "source_offset": start,
                                    "source_end": end, "excerpt": True})
            return windows
        return chosen

    def _extract_segment(self, segment):
        sid = segment["id"]
        self.segment_status[sid] = "running"
        inputs = {"source_sections": segment["sections"], "skill_guidance": self.guidance,
                  "segment_id": sid, "instruction": self.spec.get("instruction", ""),
                  "source_selection": self._source_inventory()["source_selection"],
                  "paper_title": self.pipeline.library.documents["main"].title}
        def check_extract(output):
            if (not isinstance(output.get("packet"), dict) or output["packet"].get("version") != 2
                    or not isinstance(output.get("unresolved"), list)
                    or set(output.get("checked_section_ids", [])) != set(segment["section_ids"])):
                raise PipelineError("Segment extraction omitted supplied source sections")
            if len(output["packet"].get("claims", [])) > 20:
                raise PipelineError("Segment extraction exceeds its claim bound")
            return output
        try:
            extracted = self.model_stage(sid + "_extract", "chunk_extract", inputs, check_extract)
            packet = extracted["packet"]
            self._associate_visual_evidence(packet)
            packet = self.repair_packet(packet, sid + "_repair", inputs)
            packet = self.checkpoint(sid + "_validate", {"packet": packet}, lambda: self.checked_packet(packet),
                                     validator=self.checked_packet)
            review_input = {"source_sections": segment["sections"], "packet": packet,
                            "table_statistics": all_table_statistics(self.pipeline.library),
                            "instruction": self.spec.get("instruction", "")}
            review = self.model_stage(sid + "_review", "fact_review", review_input, lambda x: validate_review(x, packet))
            if not review["passed"]:
                packet = self.repair_packet(packet, sid + "_fact_repair", review_input, review=review)
                review = self.model_stage(sid + "_review_final", "fact_review", {**review_input, "packet": packet,
                                          "previous_review": review}, lambda x: validate_review(x, packet))
            if not review["passed"]:
                raise PipelineError(f"{sid}: factual issues remain after bounded repair")
            if extracted["unresolved"]:
                self.unresolved.extend(f"{sid}: {issue}" for issue in extracted["unresolved"])
            self.segment_status[sid] = "succeeded"
            self._save_coverage()
            return {"segment_id": sid, "packet": packet, "review": review,
                    "checked_section_ids": extracted["checked_section_ids"], "unresolved": extracted["unresolved"]}
        except Exception:
            self.segment_status[sid] = "failed"
            # The failing phase record remains the authoritative failure. Other
            # completed source/segment checkpoints remain safe to reuse.
            raise

    def _visual_validator(self, figures, packet):
        def validate(output):
            observations, issues = output.get("observations"), output.get("issues")
            if type(output.get("passed")) is not bool or not isinstance(observations, list) or not isinstance(issues, list):
                raise PipelineError("Invalid visual review output")
            expected = {f["id"]: f for f in figures}
            if len(observations) != len(expected) or {o.get("figure_id") for o in observations} != set(expected):
                raise PipelineError("Visual review omitted or duplicated a required figure")
            claim_ids = {c["id"] for c in packet.get("claims", [])}
            library = self.pipeline.library
            for observation in observations:
                fid = observation["figure_id"]
                asset = library.prepared_visuals.get(fid)
                if (not asset or asset["sha256"] != observation.get("image_sha256")
                        or self.pipeline.inspected_images.get(asset["path"]) != asset["sha256"]
                        or observation.get("section_id") != asset["section_id"]):
                    raise PipelineError("Visual review lacks a matching actually read source image")
                if (type(observation.get("readable")) is not bool or not isinstance(observation.get("observation"), str)
                        or not observation["observation"].strip() or not isinstance(observation.get("uncertainties"), list)
                        or not isinstance(observation.get("conditions"), str)
                        or not isinstance(observation.get("claim_ids"), list)
                        or set(observation["claim_ids"]) - claim_ids):
                    raise PipelineError("Visual observation has invalid conditions or claim associations")
                if output["passed"] and not observation["readable"]:
                    raise PipelineError("Visual review cannot pass an unreadable required image")
            for issue in issues:
                if not isinstance(issue, dict) or issue.get("claim_id") not in claim_ids or not issue.get("message"):
                    raise PipelineError("Visual review has an invalid claim issue")
            if output["passed"] and issues:
                raise PipelineError("Visual review cannot pass a text/image conflict")
            return output
        return validate

    def _check_figures(self, packet):
        figures = self.pipeline.library.documents["main"].coverage.get("figures", [])
        if not figures:
            return packet
        self.figure_items = [{"figure_id": f["id"], "section_id": f["section_id"], "caption": f.get("caption", ""),
                              "status": "pending", "uncertainties": ["not yet inspected"], "claim_ids": []} for f in figures]
        if not self.spec["model"].get("vision"):
            self.checkpoint("figure_checks", self._figure_projection(), self._figure_projection, allow_reuse=False)
            raise PipelineError("Required figure verification needs a vision-enabled model; source and segment checkpoints retained")
        for number, figure in enumerate(figures, 1):
            sources = [s for s in self.sections if s["id"] == figure["section_id"]]
            if not sources:
                sources = self._packet_context(packet)
            # Prose evidence may depend on this plot without citing its caption
            # ID. Every claim remains visible to the independent figure reader.
            candidates = packet
            inputs = {"figures": [figure], "packet": candidates, "source_sections": sources,
                      "instruction": self.spec.get("instruction", "")}
            report = self.model_stage(f"figure_{number}_review", "figure_review", inputs,
                                      self._visual_validator([figure], candidates))
            fid, oid = figure["id"], "V" + str(number)
            def accept_observation(value):
                record = {"id": oid, **value["observations"][0]}
                with self.state_lock:
                    self.pipeline.library.visual_observations[oid] = record
                    self.pipeline.library._save()
                return record
            # A readable image observation can support repair of a conflicting
            # claim. Acceptance here records the actual read, not a passing
            # claim verdict; publication still requires the final review below.
            record = accept_observation(report)
            if not report["passed"] and report["issues"]:
                packet = self.repair_packet(packet, f"figure_{number}_repair", {
                        "source_sections": self._packet_context(packet), "figure_checks": report,
                        "visual_observations": [record]}, review=report)
                inputs["packet"] = packet
                report = self.model_stage(f"figure_{number}_review_final", "figure_review", inputs,
                                          self._visual_validator([figure], packet))
                record = accept_observation(report)
            observation = report["observations"][0]
            next_evidence = max((int(e["id"][1:]) for c in packet["claims"] for e in c["evidence"]), default=0) + 1
            for claim in packet["claims"]:
                if (claim["id"] in observation["claim_ids"] and len(claim["evidence"]) < 6 and report["passed"]
                        and not any(e.get("observation_id") == oid for e in claim["evidence"])):
                    claim["evidence"].append({"id": f"E{next_evidence}", "source_id": "main", "section_id": observation["section_id"],
                        "quote": "", "evidence_type": "image_observation", "figure_id": fid,
                        "image_sha256": observation["image_sha256"], "observation_id": oid})
                    next_evidence += 1
            self.figure_items[number - 1] = {"caption": figure.get("caption", ""), **observation,
                                             "status": "verified" if report["passed"] and observation["readable"] else "unresolved"}
            self.checkpoint("figure_checks", self._figure_projection(), self._figure_projection, allow_reuse=False)
            if not report["passed"] or not observation["readable"]:
                raise PipelineError(f"{fid}: required figure observation remains unresolved")
        return packet

    def execute(self):
        self.sections = self.source()
        inventory = self._source_inventory()
        self.checkpoint("source_inventory", inventory, lambda: inventory)
        self._recover_pdf_text()
        full_input = {"source_sections": self.sections, "skill_guidance": self.guidance,
                      "instruction": self.spec.get("instruction", "")}
        safe = int(self.spec["model"].get("context_window", 128000)) - CAPS["chunk_extract"] - 10000
        self.long_document = text_size([s for s in self.sections if s.get("kind") not in {"reference", "references", "bibliography"}]) > LONG_BYTES or len(canonical(full_input).encode()) > safe
        self.deadline = self.pipeline.select_budget(self.long_document)
        # Keep JSON extraction + coverage acknowledgement comfortably inside a
        # five-minute phase on the selected deployment. 32 KiB remains the hard
        # source-text ceiling; 16 KiB is the normal working size.
        max_chunk = min(16 * 1024, max(1024, safe - len(self.guidance.encode()) - 10000))
        if not self.long_document:
            # A short paper keeps one extraction/review pair. v3 typed evidence
            # and required visual inspection still apply.
            self.segments = [{"id": "segment_1", "sections": [s for s in self.sections if s.get("kind") not in {"reference", "references", "bibliography"}],
                              "section_ids": [s["id"] for s in self.sections if s.get("kind") not in {"reference", "references", "bibliography"}],
                              "bytes": text_size(self.sections)}]
        else:
            self.segments = segment_sections(self.sections, max_chunk)
        plan = {"long_document": self.long_document, "normalized_bytes": text_size(self.sections),
                "segments": [{k: v for k, v in s.items() if k != "sections"} for s in self.segments],
                "sections": self._coverage()["sections"], "concurrency": min(2, self.spec["limits"].get("segment_concurrency", 2)),
                "timeout_seconds": self.spec["limits"].get("long_timeout_seconds", 2400) if self.long_document else self.spec["limits"]["timeout_seconds"],
                "max_tool_calls": self.pipeline.max_tool_calls}
        self.checkpoint("segment_plan", plan, lambda: plan)
        previous = self.spec.get("previous_revision") or {}
        if self.spec["kind"] == "revise" and previous.get("reading_packet"):
            prior = previous.get("provenance", {}).get("workflow", {})
            compatible = False
            try:
                old_packet = self.checked_packet(previous["reading_packet"])
                old_review = validate_review(prior["fact_review"], old_packet)
                old_verification = previous.get("verification", {})
                compatible = (prior.get("base_fingerprint") == digest({"common": self.common, "source": self.source_fingerprint})
                              and prior.get("packet_hash") == digest(old_packet) and old_review["passed"]
                              and old_verification.get("section_coverage", {}).get("complete") is True
                              and old_verification.get("figure_checks", {}).get("complete") is True)
            except (PipelineError, KeyError, ValueError):
                pass
            decision = self.model_stage("revision_plan", "revision_plan", {"previous_packet": previous["reading_packet"],
                                        "previous_packet_compatible": compatible, "instruction": self.spec.get("instruction", "")})
            if compatible and decision["scope"] == "presentation":
                for stage, output in (("extract_reused", old_packet), ("fact_review_reused", old_review)):
                    self.checkpoint(stage, {"revision_id": previous["id"], "output": output}, lambda output=output: output)
                    record = self.completed[stage]
                    record = self.save_stage(stage, "reused", input_hash=record["input_hash"], output=output,
                                             reused_from={"revision_id": previous["id"]})
                    self.records.append(record)
                    self.completed[stage] = record
                self.segment_status = {s["id"]: "reused" for s in self.segments}
                self.figure_items = deepcopy(old_verification["figure_checks"]["items"])
                self.source_visual_coverage = deepcopy(old_verification.get("source_visual_coverage", self.source_visual_coverage))
                self.gap_resolutions = deepcopy(old_verification.get("section_coverage", {}).get("gap_resolutions", []))
                self._save_coverage()
                return self._compose_result(old_packet, old_review)
        completed = {}
        try:
            with ThreadPoolExecutor(max_workers=plan["concurrency"], thread_name_prefix="paper-segment") as pool:
                pending = {pool.submit(self._extract_segment, s): s["id"] for s in self.segments}
                for future in as_completed(pending):
                    try:
                        completed[pending[future]] = future.result()
                    except Exception:
                        self.pipeline.segment_abort.set()
                        for other in pending:
                            other.cancel()
                        raise
        except Exception:
            # Keep the full, honest coverage summary even when time/tools expire.
            with self.state_lock:
                summary = self._coverage()
                self.save_stage("section_coverage", "failed", input_hash=digest(summary), summary=summary,
                                error="Some required segments did not complete")
            raise
        packets = [completed[s["id"]]["packet"] for s in self.segments]
        packet = deepcopy(packets[0]) if len(packets) == 1 else combine_packets(packets)
        self._associate_visual_evidence(packet, replace=True)
        packet = self.checked_packet(packet)
        packet = self.checked_packet(self._check_figures(packet))
        self.checkpoint("combined_packet", {"packet": packet}, lambda: packet, validator=self.checked_packet)
        gaps = [{"id": f"G{index + 1}", "question": question} for index, question in enumerate(dict.fromkeys(self.unresolved))]
        merged_input = {"packet": packet, "segment_summaries": [
            {"segment_id": s["id"], "section_ids": s["section_ids"], "unresolved": completed[s["id"]]["unresolved"],
             "findings": [c["statement"] for c in completed[s["id"]]["packet"]["claims"]]} for s in self.segments],
            "section_index": [{key: s[key] for key in ("id", "section", "kind", "parent_id") if key in s}
                              for s in self.sections if s.get("role") == "heading" or s.get("kind") == "appendix"],
            "source_sections": self._packet_context(packet), "figure_checks": self._figure_projection(),
            "figures": self.pipeline.library.documents["main"].coverage.get("figures", []),
            "table_statistics": all_table_statistics(self.pipeline.library),
            "open_gaps": gaps,
            "instruction": self.spec.get("instruction", "")}
        # Short text without visual changes already has an independent complete
        # source review. Long papers require the additional cross-segment check.
        if not self.long_document and not self.figure_items and not gaps:
            review = completed[self.segments[0]["id"]]["review"]
        else:
            review = self.model_stage("merge_review", "merge_review", merged_input,
                                      lambda x: validate_gap_resolutions(x, packet, gaps, self._figure_projection()))
        if not review["passed"]:
            packet = self.repair_packet(packet, "merge_repair", {
                                     "source_sections": self._packet_context(packet), "figure_checks": self._figure_projection(),
                                     "table_statistics": all_table_statistics(self.pipeline.library)}, review=review)
            review = self.model_stage("merge_review_final", "merge_review", {**merged_input, "packet": packet,
                                      "source_sections": self._packet_context(packet), "previous_review": review},
                                      lambda x: validate_gap_resolutions(x, packet, gaps, self._figure_projection()))
        self.gap_resolutions = [{**next(g for g in gaps if g["id"] == r["id"]), **r} for r in review.get("gap_resolutions", [])]
        self.unresolved = [r["question"] for r in self.gap_resolutions if r["status"] == "unresolved"]
        self._save_coverage()
        if not review["passed"] or self.unresolved:
            raise PipelineError("Full-paper merge still has unresolved factual or source gaps; segment findings retained")
        return self._compose_result(packet, review)

    def _compose_result(self, packet, review):
        packet = self.checked_packet(packet)
        review = validate_review(review, packet)
        self.cross_requested = bool(packet["open_questions"] or any(e["source_id"] != "main" for c in packet["claims"] for e in c["evidence"]))
        product_input = {"packet": packet, "template": self.spec["template"], "instruction": self.spec.get("instruction", ""),
                         "figure_checks": self._figure_projection(),
                         "gap_resolutions": self.gap_resolutions,
                         "figures": self.pipeline.library.documents["main"].coverage.get("figures", [])}
        product = self.model_stage("compose", "compose", product_input)
        review_input = {**product_input, "product": product}
        product_review = self.model_stage("product_review", "product_review", review_input)
        if not product_review["passed"]:
            patch = self.model_stage("product_repair", "product_repair", {**review_input, "review": product_review})
            product = apply_product_patch(product, patch)
            product_review = self.model_stage("product_review_final", "product_review", {**product_input, "product": product,
                                               "previous_review": product_review})
        if not product_review["passed"]:
            raise PipelineError("Independent product review still reports unresolved issues after repair")
        self.packet, self.fact_review, self.product_review, self.product = packet, review, product_review, product
        self.finished = True
        result = {**product, "reading_packet": packet, "evidence_index": packet_evidence(packet, self.pipeline.library),
                  "verification": {"passed": True, "fact_review": review, "product_review": product_review, **self.coverage_projection()},
                  "provenance": {"workflow": {"version": VERSION, "source_rules_version": self.spec.get("source_rules_version"),
                       "base_fingerprint": digest({"common": self.common, "source": self.source_fingerprint}),
                       "packet_hash": digest(packet), "fact_review": review, "long_document": self.long_document,
                       "stages": list(self.completed.values())}}, "artifacts": []}
        self.result_hash = digest(result)
        (self.pipeline.work_dir / "reading-packet.json").write_text(canonical(packet))
        self.pipeline.guided_workflow = self
        return result

    def verify_completion(self, result):
        super().verify_completion(result)
        if not self._coverage()["complete"] or not self._figure_projection()["complete"] or self.unresolved:
            raise PipelineError("Structured workflow has incomplete required section or figure checks")
