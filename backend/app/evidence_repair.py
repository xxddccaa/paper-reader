"""Bounded host-guided recovery of proposed evidence, without relaxing validation."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .evidence import PacketError, apply_claim_patch, validate_packet
from .pipeline import BudgetExhausted, PipelineError, TaskCancelled

REPAIR_POLICY_VERSION = "evidence-recovery-v1"
MAX_REPAIR_ROUNDS = 3
MAX_CONTEXT_BYTES = 24000


class EvidenceRepairError(PipelineError):
    """A completed proposal still fails host validation; another strategy is needed."""


def _objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _affected_claims(packet, errors, review=None):
    prefixes = {error.split(":", 1)[0] for error in errors}
    claims = [c for c in _objects(packet.get("claims")) if isinstance(c.get("id"), str)]
    affected = {claim["id"] for claim in claims
                if claim.get("id") in prefixes or any(isinstance(e.get("id"), str) and e["id"] in prefixes
                                                       for e in _objects(claim.get("evidence")))}
    for issue in (review or {}).get("issues", []):
        if isinstance(issue, dict) and issue.get("claim_id"):
            affected.add(issue["claim_id"])
    # Structural errors and some visual/merge reports have no individual IDs.
    return affected or {claim["id"] for claim in claims}


def repair_context(packet, library, errors, *, round_number, previous_errors=(), previous_attempt=None,
                   review=None, check_budget=lambda: None):
    """Offer original passages and locator candidates, never verified evidence.

    Search only retained/read documents. Lexical similarity is used to *suggest*
    a reread location; the existing evidence validator remains the sole judge.
    Character offsets always address the original, unnormalized section text.
    """
    affected = _affected_claims(packet, errors, review)
    documents = list(library.documents.items())
    diagnostics, windows, seen = [], [], set()
    remaining = MAX_CONTEXT_BYTES
    for claim in _objects(packet.get("claims")):
        if not isinstance(claim.get("id"), str) or claim["id"] not in affected:
            continue
        for item in _objects(claim.get("evidence")):
            check_budget()
            if len(diagnostics) >= 8:
                break
            quote = item.get("quote", "")
            quote = quote if isinstance(quote, str) else ""
            tokens = sorted(set(re.findall(r"\w{3,}", quote.casefold())), key=len, reverse=True)[:12]
            candidates = []
            for source_id, document in documents:
                for section_number, section in enumerate(document.sections):
                    if section_number % 32 == 0:
                        check_budget()
                    raw = section.get("text", "")
                    declared = source_id == item.get("source_id") and section.get("id") == item.get("section_id")
                    exact = raw.find(quote) if quote else -1
                    # Unverified lexical anchors are useful even when a model
                    # changed a number, whitespace, or the declared page.
                    folded = raw.casefold()
                    hits = [token for token in tokens if token in folded]
                    score = (1000 if exact >= 0 else 0) + len(hits) * 10 + (25 if declared else 0)
                    if not score:
                        continue
                    anchor = exact
                    if anchor < 0 and hits:
                        # Search raw text for offsets; casefold can change length.
                        match = re.search(re.escape(hits[0]), raw, re.IGNORECASE)
                        anchor = match.start() if match else 0
                    if anchor < 0:
                        anchor = min((round_number - 1) * 1400, max(0, len(raw) - 1600))
                    candidates.append((score, source_id, section, anchor, exact >= 0, declared))
            candidates.sort(key=lambda value: value[0], reverse=True)
            selected = candidates[:2]
            declared_candidate = next((c for c in candidates if c[5]), None)
            if declared_candidate is not None and not any(c is declared_candidate for c in selected):
                selected.append(declared_candidate)
            suggestions = []
            for _, source_id, section, anchor, exact, _ in selected:
                raw = section.get("text", "")
                start = max(0, anchor - 200)
                end = min(len(raw), max(start + 1600, anchor + (len(quote) if exact else 1200) + 200))
                locator = {"source_id": source_id, "section_id": section["id"],
                           **({"page": section["page"]} if section.get("page") is not None else {})}
                suggestions.append({**locator, "source_offset": start, "source_end": end,
                                    "reason": "literal quote found here" if exact else "reread candidate; not verified"})
                key = (source_id, section["id"], start, end)
                if key in seen or remaining <= 200:
                    continue
                window = {**locator, "id": section["id"], "section": section.get("section", section["id"]),
                          "text": raw[start:end], "source_offset": start, "source_end": end,
                          "excerpt": start > 0 or end < len(raw)}
                size = len(json.dumps(window, ensure_ascii=False).encode())
                if size > remaining:
                    window["text"] = raw[start:end].encode()[:max(0, remaining - 500)].decode("utf-8", errors="ignore")
                    window["source_end"] = start + len(window["text"])
                    window["excerpt"] = True
                    size = len(json.dumps(window, ensure_ascii=False).encode())
                if not window["text"] or size > remaining:
                    continue
                windows.append(window)
                seen.add(key)
                remaining -= size
            diagnostics.append({"claim_id": claim["id"], "evidence_id": item.get("id"),
                                "declared_locator": {k: item[k] for k in ("source_id", "section_id", "page", "table_id", "row", "column") if k in item},
                                "errors": [e for e in errors if e.startswith((claim["id"] + ":", str(item.get("id")) + ":"))],
                                "suggested_locations": suggestions})
    unchanged = [error for error in errors if error in previous_errors]
    strategy = ("Reread the authoritative source_sections or use read_source at suggested locations. "
                "Copy a short exact original passage and correct its locator. Check numbers, units, conditions and comparison direction. "
                "Suggestions are unverified. Preserve all unaffected claims. Do not invent quotes or remove claims just to pass validation.")
    if round_number > 1:
        strategy += (" The preceding proposal did not pass. Do not repeat it: use the supplied original bytes, "
                     "relocate evidence if needed, or correct the substantive claim from the original. "
                     "Explicit removal is allowed only when the original cannot support the claim.")
    return {"policy_version": REPAIR_POLICY_VERSION, "round": round_number, "max_rounds": MAX_REPAIR_ROUNDS,
            "errors": list(errors), "unchanged_errors": unchanged, "diagnostics": diagnostics,
            "affected_claim_ids": sorted(affected), "strategy": strategy,
            "previous_attempt": previous_attempt}, windows


def recover_packet(workflow, packet, stage_name, inputs=None, *, review=None):
    """Apply up to three targeted proposals, sharing the workflow's existing budgets.

    Supplying an unsuccessful independent review requires one initial proposal
    even if mechanical evidence validation currently passes. Every proposal is
    validated inside its checkpoint, including reuse, before success is recorded.
    """
    current = deepcopy(packet)
    if not isinstance(current, dict):
        raise PipelineError("Packet evidence recovery requires a structured packet object")
    clean, errors = validate_packet(current, workflow.pipeline.library)
    if not errors and review is None:
        return clean
    previous_errors, previous_attempt = [], None
    for number in range(1, MAX_REPAIR_ROUNDS + 1):
        workflow.check_budget()
        context, sections = repair_context(current, workflow.pipeline.library, errors, round_number=number,
            previous_errors=previous_errors, previous_attempt=previous_attempt, review=review, check_budget=workflow.check_budget)
        allowed = set(context["affected_claim_ids"])
        stage_inputs = {**(inputs or {}), "packet": current, "errors": errors, "source_sections": sections,
                        "repair_context": context}
        if review is not None:
            stage_inputs["review"] = review
        candidate_state = {}

        def validate_proposal(patch):
            candidate_state.clear()
            try:
                candidate = apply_claim_patch(current, patch)
                touched = {c["id"] for c in patch["claims"]} | set(patch["remove_claim_ids"])
                if touched - allowed:
                    raise PacketError("Evidence repair changed unaffected claims: " + ", ".join(sorted(touched - allowed)))
                candidate_clean, candidate_errors = validate_packet(candidate, workflow.pipeline.library)
                candidate_state.update(packet=candidate_clean or candidate, errors=candidate_errors)
                if candidate_errors:
                    raise EvidenceRepairError("Packet evidence validation failed: " + "; ".join(candidate_errors[:12]))
                return patch
            except PacketError as exc:
                raise EvidenceRepairError(str(exc)) from exc

        name = stage_name if number == 1 else f"{stage_name}_{number}"
        try:
            workflow.model_stage(name, "packet_repair", stage_inputs, validator=validate_proposal)
            # The validator ran on either fresh output or an authenticated reuse.
            return candidate_state["packet"]
        except (BudgetExhausted, TaskCancelled):
            raise
        except PipelineError as exc:
            workflow.check_budget()
            previous_errors = errors
            changed_packet = candidate_state.get("packet", current) != current
            current = candidate_state.get("packet", current)
            _, errors = validate_packet(current, workflow.pipeline.library)
            previous_attempt = {"stage": name, "error": str(exc)[:1500],
                                "changed_packet": changed_packet,
                                "remaining_errors": errors[:20]}
    raise PipelineError(f"Packet evidence validation failed after {MAX_REPAIR_ROUNDS} adaptive repair attempts: "
                        + "; ".join((errors or [previous_attempt["error"]])[:12]))
