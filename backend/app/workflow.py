"""Host-owned guided reading phases. Models propose content, never workflow state."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable

from .evidence import packet_evidence, validate_packet
from .evidence_repair import recover_packet
from .pipeline import BudgetExhausted, PipelineError, TaskCancelled

WORKFLOW_VERSION = "guided-v2.3"
PHASE_OUTPUT_CAPS = {"extract": 6000, "packet_repair": 4000, "fact_review": 4000,
                     "compose": 5000, "product_review": 3000, "product_repair": 4000, "revision_plan": 512}
PHASE_TOOL_CAPS = {"extract": 32, "packet_repair": 20, "fact_review": 32,
                   "compose": 8, "product_review": 20, "product_repair": 12, "revision_plan": 3}
PRODUCT_CHECKS = {"coverage", "template", "numeric_conditions", "inference_labels"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def public_model(model: dict) -> dict:
    return {key: value for key, value in model.items() if key not in {"api_key", "encrypted_api_key", "name", "id"}}


def _string(value: Any, limit: int, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value.strip()):
        raise PipelineError(f"Invalid {label}")
    return value


def validate_phase(name: str, output: Any) -> dict:
    """Structural validation only; packet/source and review coverage checks follow."""
    if not isinstance(output, dict) or len(canonical(output).encode()) > 2 * 1024 * 1024:
        raise PipelineError(f"Invalid structured output from {name}")
    if name == "extract":
        if output.get("version") != 1 or not isinstance(output.get("claims"), list):
            raise PipelineError("Extract did not return a version 1 reading packet")
    elif name == "packet_repair":
        if set(output) != {"claims", "remove_claim_ids"} or not isinstance(output["claims"], list) or not isinstance(output["remove_claim_ids"], list):
            raise PipelineError("Invalid packet repair delta")
    elif name == "revision_plan":
        if set(output) != {"scope", "reason"} or output["scope"] not in {"presentation", "facts"}:
            raise PipelineError("Invalid revision scope decision")
        _string(output["reason"], 4000, "revision reason")
    elif name in {"fact_review", "product_review"}:
        if type(output.get("passed")) is not bool or not isinstance(output.get("issues"), list) or len(output["issues"]) > 100:
            raise PipelineError("Invalid independent review report")
        if output["passed"] and output["issues"]:
            raise PipelineError("Review cannot pass with unresolved issues")
        if name == "fact_review":
            checked = output.get("checked_claim_ids")
            suggestions = output.get("suggestions")
            if not isinstance(checked, list) or len(checked) > 20 or any(not isinstance(c, str) for c in checked):
                raise PipelineError("Invalid fact review coverage")
            if len(set(checked)) != len(checked):
                raise PipelineError("Duplicate checked claim IDs")
            if not isinstance(suggestions, list) or len(suggestions) > 100:
                raise PipelineError("Invalid fact review suggestions")
            for item in suggestions:
                _string(item, 8000, "fact review suggestion")
            for issue in output["issues"]:
                if not isinstance(issue, dict) or set(issue) != {"claim_id", "message"}:
                    raise PipelineError("Invalid fact review issue")
                _string(issue["claim_id"], 50, "issue claim ID")
                _string(issue["message"], 8000, "fact review issue")
        else:
            for issue in output["issues"]:
                _string(issue, 8000, "product review issue")
            checks = output.get("checks")
            if not isinstance(checks, dict) or not PRODUCT_CHECKS.issubset(checks) or any(type(checks[k]) is not bool for k in PRODUCT_CHECKS):
                raise PipelineError("Product review omitted required checks")
            if output["passed"] and not all(checks[k] for k in PRODUCT_CHECKS):
                raise PipelineError("Product review cannot pass failed checks")
    elif name == "compose":
        if set(output) != {"brief_markdown", "full_markdown"}:
            raise PipelineError("Compose must return exactly the two reading views")
        _string(output["brief_markdown"], 200000, "brief reading")
        _string(output["full_markdown"], 1000000, "full reading")
    elif name == "product_repair":
        edits = output.get("edits")
        if set(output) != {"edits"} or not isinstance(edits, list) or not 1 <= len(edits) <= 20:
            raise PipelineError("Invalid product patch")
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"field", "old_text", "new_text"} or edit["field"] not in {"brief_markdown", "full_markdown"}:
                raise PipelineError("Invalid product edit")
            _string(edit["old_text"], 200000, "product patch old text")
            _string(edit["new_text"], 200000, "product patch replacement", nonempty=False)
    return deepcopy(output)


def apply_product_patch(product: dict, patch: dict) -> dict:
    validate_phase("compose", product)
    validate_phase("product_repair", patch)
    ranges: dict[str, list[tuple[int, int, str]]] = {key: [] for key in product}
    for edit in patch["edits"]:
        field, old = edit["field"], edit["old_text"]
        start = product[field].find(old)
        if start < 0 or product[field].find(old, start + 1) >= 0:
            raise PipelineError("Product patch must match exactly once in the frozen product")
        end = start + len(old)
        if any(start < b and end > a for a, b, _ in ranges[field]):
            raise PipelineError("Product patch contains overlapping edits")
        ranges[field].append((start, end, edit["new_text"]))
    changed = dict(product)
    for field, edits in ranges.items():
        for start, end, new in sorted(edits, reverse=True):
            changed[field] = changed[field][:start] + new + changed[field][end:]
    return validate_phase("compose", changed)


def check_fact_review(report: dict, packet: dict) -> dict:
    report = validate_phase("fact_review", report)
    claim_ids = {claim["id"] for claim in packet["claims"]}
    if set(report["checked_claim_ids"]) != claim_ids:
        raise PipelineError("Independent fact review did not cover every packet claim")
    if any(issue["claim_id"] not in claim_ids for issue in report["issues"]):
        raise PipelineError("Fact review references an unknown claim")
    return report


class GuidedWorkflow:
    """All checkpoint anchors come from fenced DB rows, not writable model files."""
    def __init__(self, job, spec: dict, pipeline, run_phase: Callable, save_stage: Callable,
                 records: list[dict], deadline: float):
        self.job, self.spec, self.pipeline = job, spec, pipeline
        self.run_phase, self.save_stage = run_phase, save_stage
        self.records = deepcopy(records)
        self.deadline = deadline
        self.root = pipeline.work_dir / "host-workflow"
        self.root.mkdir(exist_ok=True)
        self.outputs: dict[str, dict] = {}
        self.completed: dict[str, dict] = {}
        self.packet: dict | None = None
        self.fact_review: dict | None = None
        self.product_review: dict | None = None
        self.product: dict | None = None
        self.cross_requested = False
        self.finished = False
        guidance_path = Path(spec["skill_root"]) / "SKILL.md"
        self.guidance = guidance_path.read_text()
        pipeline.inspected_skill_files.add("SKILL.md")
        registry_path = pipeline.project_dir / "skills/registry.json"
        registry = json.loads(registry_path.read_text()) if registry_path.is_file() else []
        selected = next((entry for entry in registry if entry.get("id") == spec["skill_id"]), {})
        self.common = {"rules": WORKFLOW_VERSION, "skill": spec["skill_id"], "commit": selected.get("commit"),
                       "adapter": selected.get("adapter_version"), "guidance_hash": digest(self.guidance),
                       "model": public_model(spec["model"])}
        self.source_fingerprint = ""

    def check_budget(self):
        self.pipeline._cancel_check()
        if time.monotonic() >= self.deadline:
            raise BudgetExhausted("Guided workflow exceeded the task time budget")
        if self.pipeline.tool_calls > self.pipeline.max_tool_calls:
            raise BudgetExhausted("Guided workflow exhausted the total tool budget")

    def checkpoint(self, name: str, inputs: Any, compute: Callable[[], dict], *, phase: str | None = None,
                   validator: Callable[[dict], dict] | None = None, allow_reuse: bool = True) -> dict:
        self.check_budget()
        input_hash = digest({"common": self.common, "source": self.source_fingerprint, "name": name, "input": inputs})
        path = self.root / f"{name}.json"
        anchors = [entry for entry in self.records if entry.get("name") == name and entry.get("status") in {"succeeded", "reused"}
                   and entry.get("input_hash") == input_hash]
        if allow_reuse and path.is_file() and not path.is_symlink():
            try:
                saved = json.loads(path.read_text())
                output = saved["output"]
                output_hash = digest(output)
                anchor = next((entry for entry in reversed(anchors) if entry.get("output_hash") == output_hash), None)
                if not anchor or saved["input_hash"] != input_hash or saved["output_hash"] != output_hash:
                    raise ValueError("Unanchored or changed checkpoint")
                source = saved.get("source_checkpoint")
                if not source or saved.get("source_hash") != anchor.get("source_hash") or digest(source) != anchor.get("source_hash"):
                    raise ValueError("Unanchored source checkpoint")
                self.pipeline.library.restore_checkpoint(source, expected_sha256=anchor["source_hash"])
                if phase:
                    output = validate_phase(phase, output)
                if validator:
                    output = validator(output)
                record = self.save_stage(name, "reused", input_hash=input_hash, output=output,
                                         reused_from=anchor.get("reused_from") or {"job_id": anchor.get("job_id", self.job.id), "attempt": anchor.get("attempt")})
                self.records.append(record)
                self.outputs[name], self.completed[name] = deepcopy(output), record
                return output
            except (KeyError, ValueError, OSError, PipelineError):
                # A saved file is never sufficient authority to skip a stage.
                pass
        start = time.monotonic()
        self.save_stage(name, "running", input_hash=input_hash)
        before = self.pipeline.tool_calls
        try:
            output = compute()
            self.check_budget()
            if phase:
                output = validate_phase(phase, output)
            if validator:
                output = validator(output)
            record = self.save_stage(name, "succeeded", input_hash=input_hash, output=output,
                                     duration_ms=round((time.monotonic() - start) * 1000),
                                     usage={**(getattr(self.pipeline, "phase_usage", {}) if phase else {}),
                                            "tool_calls": self.pipeline.tool_calls - before})
            self.records.append(record)
            self.outputs[name], self.completed[name] = deepcopy(output), record
            return output
        except Exception as exc:
            if not isinstance(exc, TaskCancelled):
                self.save_stage(name, "failed", input_hash=input_hash, error=str(exc)[:1000],
                                duration_ms=round((time.monotonic() - start) * 1000))
            raise

    def model_stage(self, name: str, phase: str, inputs: dict, validator=None) -> dict:
        def execute():
            self.check_budget()
            remaining = self.pipeline.max_tool_calls - self.pipeline.tool_calls
            if remaining < 1:
                raise BudgetExhausted("Guided workflow exhausted the total tool budget")
            cap = min(remaining, PHASE_TOOL_CAPS[phase])
            timeout = min(300, max(1, int(self.deadline - time.monotonic())))
            output_cap = min(PHASE_OUTPUT_CAPS[phase], int(self.spec["model"].get("max_tokens", 8192)))
            # One UTF-8 byte per token is a conservative upper-bound estimate.
            # Refuse overflow rather than silently truncate original source coverage.
            budget = int(self.spec["model"].get("context_window", 128000)) - output_cap - 4000
            if len(canonical(inputs).encode()) > max(0, budget):
                raise PipelineError(f"{name}: complete phase input does not fit the configured model context")
            return self.run_phase(phase, inputs, timeout, cap, output_cap)
        return self.checkpoint(name, inputs, execute, phase=phase, validator=validator)

    def source(self) -> list[dict]:
        paper = self.spec["paper"]
        path = paper.get("storage_path")
        source_identity = {"paper_id": paper["id"], "source_type": paper.get("source_type"), "url": paper.get("source_url"),
                           "original_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and paper.get("source_type") == "pdf" else None}
        def prepare():
            # Without a trusted source checkpoint, do not trust a leftover corpus.
            self.pipeline.library.documents.clear()
            self.pipeline.library.prepare_source()
            return self.pipeline.library.export_checkpoint()
        def restore(payload):
            self.pipeline.library.restore_checkpoint(payload, expected_sha256=digest(payload))
            if "main" not in self.pipeline.library.documents:
                raise PipelineError("Source checkpoint has no original paper")
            return payload
        self.checkpoint("source", source_identity, prepare, validator=restore)
        main = self.pipeline.library.documents["main"]
        self.source_fingerprint = digest({"main": vars(main), "source_rules": self.pipeline.library.export_checkpoint()["version"]})
        # A finished review may have acquired related sources after the source
        # stage. Restore only DB-anchored snapshots with this same original.
        for anchor in reversed(self.records):
            if anchor.get("name") == "source" or anchor.get("status") not in {"succeeded", "reused"} or not anchor.get("source_hash"):
                continue
            candidate = self.root / f"{anchor['name']}.json"
            try:
                saved = json.loads(candidate.read_text())
                payload = saved["source_checkpoint"]
                original = next(doc for doc in payload["documents"] if doc["id"] == "main")
                if (digest(saved["output"]) != anchor.get("output_hash") or digest(payload) != anchor["source_hash"]
                        or original["sha256"] != main.sha256):
                    continue
                self.pipeline.library.restore_checkpoint(payload, expected_sha256=anchor["source_hash"])
                break
            except (OSError, ValueError, KeyError, StopIteration):
                continue
        sections = []
        for section in self.pipeline.library.documents["main"].sections:
            sections.append({**section, "source_id": "main"})
        if not sections or not any(section.get("text", "").strip() for section in sections):
            raise PipelineError("Main paper has no usable original sections")
        return sections

    def checked_packet(self, packet: dict) -> dict:
        clean, errors = validate_packet(packet, self.pipeline.library)
        if errors:
            raise PipelineError("Packet evidence validation failed: " + "; ".join(errors[:12]))
        return clean

    def _fact_validator(self, packet):
        return lambda report: check_fact_review(report, packet)

    def repair_packet(self, packet, stage_name, inputs=None, *, review=None):
        return recover_packet(self, packet, stage_name, inputs, review=review)

    def execute(self) -> dict:
        sections = self.source()
        previous = self.spec.get("previous_revision") or {}
        previous_packet = previous.get("reading_packet") or {}
        prior = (previous.get("provenance") or {}).get("workflow", {})
        reusable = False
        previous_clean = None
        if self.spec["kind"] == "revise" and previous_packet:
            try:
                previous_clean = self.checked_packet(previous_packet)
                previous_review = check_fact_review(prior["fact_review"], previous_clean)
                reusable = (prior.get("base_fingerprint") == digest({"common": self.common, "source": self.source_fingerprint})
                            and prior.get("packet_hash") == digest(previous_clean) and previous_review["passed"] is True)
            except (PipelineError, KeyError, ValueError):
                reusable = False
        scope = "facts"
        if self.spec["kind"] == "revise":
            decision = self.model_stage("revision_plan", "revision_plan", {"instruction": self.spec.get("instruction", ""),
                        "previous_packet": previous_packet, "previous_packet_compatible": reusable})
            if reusable:
                scope = decision["scope"]
        if scope == "presentation":
            packet = self.checkpoint("extract", {"verified_parent": previous["id"], "packet_hash": digest(previous_clean)},
                                     lambda: previous_clean, validator=self.checked_packet)
            # This is an authenticated previous fact report, not a manufactured review.
            fact_review = self.checkpoint("fact_review", {"verified_parent": previous["id"], "packet_hash": digest(packet)},
                                          lambda: prior["fact_review"], validator=self._fact_validator(packet))
            for stage in ("extract", "fact_review"):
                record = self.completed[stage]
                record = self.save_stage(stage, "reused", input_hash=record["input_hash"], output=self.outputs[stage],
                                         reused_from={"revision_id": previous["id"]})
                self.records.append(record); self.completed[stage] = record
        else:
            extraction = {"source_sections": sections, "skill_guidance": self.guidance,
                          "instruction": self.spec.get("instruction", ""), "previous_packet": previous_packet}
            packet = self.model_stage("extract", "extract", extraction)
            packet = self.repair_packet(packet, "packet_repair", {"skill_guidance": self.guidance})
            packet = self.checkpoint("packet_validate", {"packet": packet}, lambda: self.checked_packet(packet), validator=self.checked_packet)
            fact_review = self.model_stage("fact_review", "fact_review", {"source_sections": sections, "packet": packet,
                          "instruction": self.spec.get("instruction", "")}, validator=self._fact_validator(packet))
            if not fact_review["passed"]:
                packet = self.repair_packet(packet, "fact_repair", {"instruction": self.spec.get("instruction", "")},
                                            review=fact_review)
                fact_review = self.model_stage("fact_review_final", "fact_review", {"source_sections": sections, "packet": packet,
                              "previous_review": fact_review, "instruction": self.spec.get("instruction", "")},
                              validator=self._fact_validator(packet))
            if not fact_review["passed"]:
                raise PipelineError("Independent fact review still reports unresolved issues after repair")
        packet = self.checked_packet(packet)
        self.cross_requested = bool(packet.get("open_questions") or any(e["source_id"] != "main" for c in packet["claims"] for e in c["evidence"]))
        product_input = {"packet": packet, "template": self.spec["template"], "instruction": self.spec.get("instruction", "")}
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
        self.packet, self.fact_review, self.product_review, self.product = packet, fact_review, product_review, product
        self.finished = True
        # Host-authored data. Never loaded from an arbitrary model result field.
        result = {**product, "reading_packet": packet, "evidence_index": packet_evidence(packet, self.pipeline.library),
                  "verification": {"passed": True, "fact_review": fact_review, "product_review": product_review},
                  "provenance": {"workflow": {"version": WORKFLOW_VERSION,
                       "base_fingerprint": digest({"common": self.common, "source": self.source_fingerprint}),
                       "packet_hash": digest(packet), "fact_review": fact_review,
                       "stages": list(self.completed.values())}}, "artifacts": []}
        self.result_hash = digest(result)
        (self.pipeline.work_dir / "reading-packet.json").write_text(canonical(packet))
        self.pipeline.guided_workflow = self
        return result

    def verify_completion(self, result: dict):
        if not self.finished or digest(result) != self.result_hash:
            raise PipelineError("Guided publication lacks an authentic completed host workflow")
        if not self.fact_review or not self.product_review or not self.fact_review["passed"] or not self.product_review["passed"]:
            raise PipelineError("Guided independent reviews are incomplete")
        self.checked_packet(result["reading_packet"])
        for name, record in self.completed.items():
            path = self.root / f"{name}.json"
            saved = json.loads(path.read_text())
            if (digest(saved["output"]) != record["output_hash"] or saved.get("input_hash") != record["input_hash"]
                    or digest(saved.get("source_checkpoint")) != record.get("source_hash")):
                raise PipelineError("Guided checkpoint changed before publication")
        latest_name = next(reversed(self.completed))
        latest = json.loads((self.root / f"{latest_name}.json").read_text())
        self.pipeline.library.restore_checkpoint(latest["source_checkpoint"], expected_sha256=self.completed[latest_name]["source_hash"])
        self.checked_packet(result["reading_packet"])
        if digest(json.loads((self.pipeline.work_dir / "reading-packet.json").read_text())) != digest(self.packet):
            raise PipelineError("Guided reading packet changed before publication")

    def cross_paper_errors(self) -> list[str]:
        if not self.cross_requested:
            return []
        errors = []
        searches = set(self.pipeline.library.query_log)
        outcomes = getattr(self.pipeline.library, "search_records", {})
        external = {key for key, value in self.pipeline.library.documents.items() if value.kind == "external" and value.coverage.get("full_text") is True}
        for question in self.packet.get("open_questions", []):
            query = question["search_query"].strip()
            if query not in searches:
                errors.append("Requested related-paper check was not searched: " + question["question"][:200])
                continue
            outcome = outcomes.get(query, {})
            if outcome.get("completed") is not True or outcome.get("errors"):
                errors.append("Requested related-paper search was incomplete: " + question["question"][:200])
            matching = {"related:" + str(identity) for identity in outcome.get("candidate_ids", [])}
            if not external.intersection(matching):
                errors.append("Requested related-paper check has no matched full-text source: " + question["question"][:200])
        if not external:
            errors.append("Requested cross-paper verification lacks full-text external sources")
        for claim in self.packet["claims"]:
            if any(item["source_id"] != "main" and item["source_id"] not in external for item in claim["evidence"]):
                errors.append(claim["id"] + ": external evidence lacks full-text verification")
        return errors
