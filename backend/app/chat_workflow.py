"""Private paper Q&A: immutable sources, bounded recovery and independent review.

Persistence, identity and streaming deltas belong to the worker. The model only
proposes answers; this module decides source matching and final verification.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import time
from typing import Callable

from .evidence import PacketError, resolve_evidence
from .pipeline import BudgetExhausted, Pipeline, PipelineError, TaskCancelled
from .source_tools import SourceError

MAX_REPAIRS = 3
MAX_HISTORY_ENTRIES = 400  # Keep in sync with runner historyEntries().
MAX_SOURCE_BYTES = 24000
CHAT_OUTPUT_CAPS = {"answer": 6000, "review": 2000, "compact": 3000}
# Shared with the actual Pi transcript budget tests in agent-runner/chat.ts.
# Policy reserves bound operation instructions, schemas and message boundaries;
# paper/limit wrappers and the task path are charged separately below.
CHAT_FRAMING_BYTES = 4096
CHAT_POLICY_BYTES = {"answer": 8192, "review": 4608, "compact": 3072}
MAX_ANSWER_CHARACTERS = 100000
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,49}\Z")
STOP_WORDS = frozenset("the a an of to is are was were be for in on with and or this that how what why from it its about please paper explain".split())


def _json_bytes(value) -> int:
    # Include strings, keys, delimiters and JSON escapes in the bound. Keeping
    # Python's spaces is slightly more conservative than JSON.stringify().
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _utf8_prefix(value: str, maximum: int) -> str:
    return value.encode("utf-8")[:max(0, maximum)].decode("utf-8", errors="ignore")


def _fit_window(value: dict, maximum: int) -> dict | None:
    """Bound a serialized excerpt while retaining original character offsets."""
    value = deepcopy(value)
    original = value["text"]
    value["text"] = ""
    value["end_offset"] = value["offset"]
    if _json_bytes(value) > maximum:
        return None
    low, high = 0, len(original)
    while low < high:
        middle = (low + high + 1) // 2
        value.update(text=original[:middle], end_offset=value["offset"] + middle)
        if _json_bytes(value) <= maximum:
            low = middle
        else:
            high = middle - 1
    value.update(text=original[:low], end_offset=value["offset"] + low)
    return value


def _tokens(query: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]{1,2}", query.casefold())
            if word not in STOP_WORDS and len(word) > 1}


def _window(source_id: str, document, section: dict, query: str = "", limit: int = 2400) -> dict:
    """Every offset refers to the retained section, never normalized text."""
    text = section.get("text", "")
    # Lowercasing/casefolding can change string length (İ, ß). Search raw text
    # with IGNORECASE so match.start remains an original character position.
    exact = text.find(query) if query else -1
    matches = ([exact] if exact >= 0 else [match.start() for token in _tokens(query)
               if (match := re.search(re.escape(token), text, re.IGNORECASE))])
    start = max(0, min(matches, default=0) - min(300, limit // 4))
    start = min(start, max(0, len(text) - limit))
    end = min(len(text), start + limit)
    return {"source_id": source_id, "source_title": document.title, "source_url": document.url,
            "section_id": section["id"], "section": section.get("section", section["id"]),
            "page": section.get("page"), "text": text[start:end], "offset": start,
            "end_offset": end, "total_characters": len(text)}


class ChatPipeline(Pipeline):
    """A small allowlist; no artifact writes, shell or native publication RPCs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cached_documents = {key: document for key, document in self.library.documents.items() if key != "main"}
        self.library.documents = {key: document for key, document in self.library.documents.items() if key == "main"}
        self.active_external: set[str] = set()
        self.operation = "answer"
        # Source cache retains acquisition metadata; turn limits and failures do
        # not carry over from a completed prior answer.
        self.library.searches = 0
        self.library.limitations = []

    def retained_documents(self) -> dict:
        return {**self.cached_documents, **self.library.documents}

    def _activate(self, source_id: str) -> None:
        if source_id == "main":
            return
        if source_id not in self.retained_documents():
            raise SourceError("Unknown source; fetch it first")
        if source_id not in self.active_external:
            if len(self.active_external) >= min(self.library.max_related, 3):
                raise SourceError("At most three related papers can be read in one turn")
            self.active_external.add(source_id)
        if source_id not in self.library.documents:
            self.library.documents[source_id] = self.cached_documents[source_id]

    def call(self, name: str, args: dict):
        with self.state_lock:
            self._consume_tool_call()
            if not isinstance(args, dict):
                raise PipelineError("Tool arguments must be an object")
            allowed = {"read_source": self.read_source, "search_source": self.search_source}
            if self.operation == "answer":
                allowed.update(search_papers=self.search_papers, fetch_related=self.fetch_related,
                               read_skill_file=self.read_skill_file)
            if self.operation == "compact" or name not in allowed:
                raise PipelineError("Tool is unavailable in this chat operation")
            try:
                return allowed[name](**args)
            except TypeError as exc:
                raise PipelineError("Invalid chat tool arguments") from exc

    def host_call(self, name: str, args: dict) -> dict:
        with self.state_lock:
            self._consume_tool_call()
            if name != "host_chat_submission" or args != {}:
                raise PipelineError("Unknown internal chat RPC")
            return {"accepted": True}

    def read_source(self, source_id: str = "main", **kwargs) -> dict:
        self._activate(source_id)
        return self.library.read_source(source_id=source_id, **kwargs)

    def search_source(self, query: str, source_id: str | None = None, limit: int = 6) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise PipelineError("Source query must contain 1–2000 characters")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise PipelineError("Source result limit must be between 1 and 10")
        if source_id is not None:
            self._activate(source_id)
            documents = {source_id: self.library.documents[source_id]}
        else:
            documents = self.library.documents
        tokens = _tokens(query)
        candidates = []
        for identity, document in documents.items():
            for index, section in enumerate(document.sections):
                if index % 32 == 0:
                    self._cancel_check()
                text = section.get("text", "")
                if not text.strip():
                    continue
                folded = text.casefold()
                score = sum(min(3, folded.count(word)) for word in tokens)
                if score:
                    candidates.append((score, identity, index, document, section))
        candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
        return {"query": query, "results": [_window(identity, document, section, query)
                for _, identity, _, document, section in candidates[:limit]],
                "instruction": "Offsets are original section character offsets. Cite canonical section_id and copy original text. For Chinese questions about English text, search again using English terms."}

    def _workspace_source_identity(self, identity: str) -> tuple[str, str | None]:
        """Resolve aliases only after checking the tenant record on every call.

        A persisted public arXiv source may be reusable, but a guessed workspace
        identifier must never use the cache to bypass the scoped paper lookup.
        """
        from sqlalchemy import select
        from .db import SessionLocal
        from .models import Paper
        from .source_tools import normalize_arxiv
        with SessionLocal() as db:
            paper = db.scalar(select(Paper).where(Paper.id == identity.removeprefix("workspace:"),
                                                   Paper.workspace_id == self.workspace_id))
            if paper is None:
                raise PipelineError("Related paper is unavailable in this workspace")
            if self.library.is_main_source(identity, paper.source_url, paper.sha256):
                raise PipelineError("The main paper or an identical copy cannot be used as an external source")
            canonical = "arxiv:" + normalize_arxiv(paper.source_url) if paper.source_type == "arxiv" else identity
            return "related:" + canonical, paper.source_url

    def fetch_related(self, id: str, url: str | None = None) -> dict:
        if not isinstance(id, str) or not id or len(id) > 500:
            raise PipelineError("Invalid related source identifier")
        identity, workspace_url = (self._workspace_source_identity(id) if id.startswith("workspace:")
                                   else ("related:" + id, None))
        cached = self.retained_documents().get(identity)
        if cached:
            if url and url not in {cached.url, self.library.candidates.get(id, {}).get("url"),
                                   self.library.candidates.get(id, {}).get("pdf_url"), workspace_url}:
                raise SourceError("Related URL does not match the retained source")
            if self.library.is_main_source(cached.id, cached.url, cached.sha256):
                raise SourceError("The main paper cannot be used as an external source")
            self._activate(identity)
            return {"source_manifest": cached.manifest(), "reused": True,
                    "sections": [_window(identity, cached, section) for section in cached.sections[:2]]}
        if len(self.active_external) >= min(self.library.max_related, 3):
            raise SourceError("At most three related papers can be read in one turn")
        try:
            result = super().fetch_related(id, url)
        except SourceError as exc:
            # Acquisition errors remain visible while local Q&A can continue.
            self.library.limitations.append(f"Full text unavailable for {id}: {exc}")
            raise
        # The inherited workspace fetch canonicalizes arXiv aliases. Count the
        # materialized source identity, not the caller's workspace alias.
        manifest = result.get("source_manifest")
        actual_identity = manifest.get("source_id") if isinstance(manifest, dict) else None
        if isinstance(actual_identity, str) and actual_identity in self.library.documents:
            self._activate(actual_identity)
            self.cached_documents[actual_identity] = self.library.documents[actual_identity]
        if result.get("limitation"):
            self.library.limitations.append(f"Full text unavailable for {id}: {result['limitation']}")
        return result


class ChatWorkflow:
    def __init__(self, pipeline: ChatPipeline, spec: dict, call_model: Callable,
                 emit: Callable, check_budget: Callable):
        self.pipeline, self.spec = pipeline, spec
        self.call_model, self.emit, self.outer_check_budget = call_model, emit, check_budget
        self.deadline = time.monotonic() + min(300, spec.get("limits", {}).get("timeout_seconds", 300))
        self.input = deepcopy(spec.get("chat", {}).get("input", {}))
        self.summary_update: dict = {}
        self.pipeline.library.max_related = min(3, spec.get("limits", {}).get("max_related_papers", 3))

    def check_budget(self) -> None:
        self.outer_check_budget()
        self.pipeline._cancel_check()
        if time.monotonic() >= self.deadline:
            raise BudgetExhausted("Chat exceeded the task time budget")
        if self.pipeline.tool_calls > self.pipeline.max_tool_calls:
            raise BudgetExhausted("Chat exhausted the total tool budget")

    def _call(self, operation: str, inputs: dict) -> dict:
        self.check_budget()
        if self.pipeline.tool_calls >= self.pipeline.max_tool_calls:
            raise BudgetExhausted("Chat exhausted the total tool budget")
        inputs = deepcopy(inputs)
        budget = self._context_bytes(operation)
        # Optional context may be reduced; the question, proposed answer,
        # required evidence and validation errors are never silently truncated.
        if operation == "answer":
            self._bound_context(inputs, target=budget)
        elif operation == "review":
            while _json_bytes(inputs) > budget and inputs.get("source_sections"):
                self._shrink_source_context(inputs["source_sections"])
        if _json_bytes(inputs) > budget:
            raise PipelineError("Chat context exceeds the configured model window after compaction; choose a larger context window or a shorter question")
        previous = self.pipeline.operation
        self.pipeline.operation = operation
        try:
            result = self.call_model(operation, deepcopy(inputs),
                                     max(0.001, self.deadline - time.monotonic()),
                                     self.pipeline.max_tool_calls - self.pipeline.tool_calls)
            self.check_budget()
            return result
        finally:
            self.pipeline.operation = previous

    def _progress(self, stage: str, message: str) -> None:
        self.emit({"type": "progress", "stage": stage, "message": message})

    def _section_index(self) -> list[dict]:
        result, size = [], 2
        documents = self.pipeline.retained_documents()
        # Give every retained paper an entry before filling the main paper's
        # directory, so a long original cannot hide cached comparison sources.
        ordered = [(identity, document, document.sections[:1]) for identity, document in documents.items()]
        ordered += [(identity, document, document.sections[1:]) for identity, document in documents.items()]
        for identity, document, sections in ordered:
            for index, section in enumerate(sections):
                if index % 32 == 0:
                    self.check_budget()
                row = {"source_id": identity, "source_title": document.title[:200],
                       "section_id": section["id"], "page": section.get("page"),
                       "title": str(section.get("section", section["id"]))[:160],
                       "characters": len(section.get("text", "")),
                       "cached": identity not in self.pipeline.library.documents}
                length = _json_bytes(row) + 2
                if size + length > 18000:
                    break
                result.append(row)
                size += length
        return result

    def _source_sections(self, question: str, evidence: list | None = None) -> list[dict]:
        windows, seen = [], set()
        maximum = min(MAX_SOURCE_BYTES, self._context_bytes() // 3)
        def add(identity, document, section, query):
            key = identity, section["id"]
            remaining = maximum - _json_bytes(windows) - 2
            if key in seen or remaining <= 0:
                return
            value = _fit_window(_window(identity, document, section, query, 4000), remaining)
            if not value or not value["text"]:
                return
            seen.add(key)
            windows.append(value)
        for item in evidence or []:
            self.check_budget()
            if not isinstance(item, dict):
                continue
            identity = item.get("source_id")
            if not isinstance(identity, str):
                continue
            document = self.pipeline.library.documents.get(identity)
            if document:
                for section in document.sections:
                    if section["id"] == item.get("section_id"):
                        add(identity, document, section, str(item.get("quote", "")))
        results = self.pipeline.search_source(question[:2000], limit=8)["results"]
        for found in results:
            document = self.pipeline.library.documents[found["source_id"]]
            section = next(value for value in document.sections if value["id"] == found["section_id"])
            add(found["source_id"], document, section, question)
        # A Chinese query can have no lexical overlap with an English paper.
        # Supply actual opening sections, and let Pi reformulate its tool query.
        if not windows:
            main = self.pipeline.library.documents.get("main")
            if main:
                for section in main.sections[:4]:
                    add("main", main, section, "")
        return windows

    def _context_bytes(self, operation: str = "answer") -> int:
        # Match the reading workflows: one UTF-8 byte per token is conservative
        # across English, Chinese, math and emoji. A small context never gets
        # raised to an arbitrary positive minimum after reserving output/tools.
        context = int(self.spec.get("model", {}).get("context_window", 128000))
        output = min(int(self.spec.get("model", {}).get("max_tokens", 4096)), CHAT_OUTPUT_CAPS[operation])
        paper = self.spec.get("paper", {})
        # This is the runner's outer prompt, excluding the separately measured
        # input object. Dynamic titles and Unicode paths cannot spend the fixed
        # policy margin. Python JSON separators conservatively include spaces.
        wrapper = {"kind": "chat", "operation": operation,
                   "paper": {"id": paper.get("id", ""), "title": paper.get("title", "")},
                   "input": {}, "limits": {**self.spec.get("limits", {}), "max_output_tokens": output}}
        envelope_bytes = _json_bytes(wrapper) - 2
        path_bytes = len(str(self.spec.get("work_dir", "")).encode("utf-8"))
        return max(0, context - output - CHAT_FRAMING_BYTES - CHAT_POLICY_BYTES[operation] - envelope_bytes - path_bytes)

    @staticmethod
    def _shrink_source_context(sections: list[dict]) -> None:
        if len(sections) > 1:
            sections.pop()
        elif sections and len(sections[0].get("text", "").encode("utf-8")) > 256:
            value = sections[0]
            value["text"] = _utf8_prefix(value["text"], len(value["text"].encode("utf-8")) // 2)
            value["end_offset"] = value["offset"] + len(value["text"])
        elif sections:
            sections.pop()

    def _bound_context(self, base: dict, *, target: int | None = None) -> None:
        """Reading summaries are context, never the authoritative source store."""
        reading = base["reading_context"]
        allowed = {"paper_title", "brief_markdown", "full_markdown", "reading_packet", "evidence_index"}
        for key in list(reading):
            if key not in allowed:
                del reading[key]
        for field, cap in (("paper_title", 500), ("brief_markdown", 12000), ("full_markdown", 24000)):
            if isinstance(reading.get(field), str) and len(reading[field].encode("utf-8")) > cap:
                reading[field] = _utf8_prefix(reading[field], cap) + "\n[上下文已截短，请用原文工具核对细节。]"
        # Trim whole structured units rather than turn excerpts into false full
        # evidence. Full original retrieval remains available to the agent.
        target = self._context_bytes() // 2 if target is None else target
        while _json_bytes(base) > target:
            self.check_budget()
            if reading.get("full_markdown"):
                reading.pop("full_markdown")
            elif isinstance(reading.get("reading_packet"), dict) and reading["reading_packet"].get("claims"):
                reading["reading_packet"]["claims"].pop()
            elif reading.get("reading_packet"):
                reading.pop("reading_packet")
            elif reading.get("evidence_index"):
                reading["evidence_index"].pop()
            elif len(base.get("section_index", [])) > 4:
                base["section_index"].pop()
            elif base.get("source_sections"):
                self._shrink_source_context(base["source_sections"])
            elif len(reading.get("brief_markdown", "").encode("utf-8")) > 1000:
                reading["brief_markdown"] = _utf8_prefix(reading["brief_markdown"], 1000)
            elif reading.get("brief_markdown"):
                reading.pop("brief_markdown")
            elif base.get("section_index"):
                base["section_index"].pop()
            elif base.get("repair_context", {}).get("diagnostics"):
                base["repair_context"]["diagnostics"].pop()
            else:
                break

    def _history(self, base: dict) -> tuple[list[dict], str]:
        history = self.input.get("history", [])
        if not isinstance(history, list):
            raise PipelineError("Invalid chat history")
        summary = self.input.get("summary", "")
        cursor = self.input.get("summary_through_sequence", 0)
        history = [deepcopy(row) for row in history if isinstance(row, dict)
                   and row.get("role") in {"user", "assistant"} and isinstance(row.get("content"), str)
                   and (not isinstance(row.get("sequence"), int) or row["sequence"] > cursor)]
        # The DB keeps failed drafts. They are deliberately not statements the
        # assistant can rely on when restoring a completed dialogue.
        history = [row for row in history if row["role"] != "assistant" or row.get("status", "succeeded") == "succeeded"]
        budget = self._context_bytes()
        visible = lambda rows: [{"role": row["role"], "content": row["content"]} for row in rows]
        total = _json_bytes({**base, "history": visible(history), "summary": summary})
        if (total > budget or len(history) > MAX_HISTORY_ENTRIES) and len(history) >= 2:
            # Preserve the latest two complete user/assistant turns. The cursor
            # advances only to a completed assistant with trusted DB sequence.
            split = max(0, len(history) - 4)
            while split > 0 and history[split - 1]["role"] != "assistant":
                split -= 1
            # Keep the latest complete pair if two long pairs would overflow.
            # Reserve the full allowed summary including its serialized form.
            # Count role keys, escapes and separators, not just message text.
            recent_budget = budget - _json_bytes({**base, "history": [], "summary": ""}) - self._summary_bytes()
            while split < len(history) - 2 and _json_bytes(visible(history[split:])) > recent_budget:
                split += 1
                while split < len(history) - 2 and history[split - 1]["role"] != "assistant":
                    split += 1
            if _json_bytes(visible(history[split:])) > recent_budget and history[-1]["role"] == "assistant":
                # Only when even the newest pair cannot fit, preserve it in
                # the memory summary. Raw messages remain untouched in DB.
                split = len(history)
            old, recent = history[:split], history[split:]
            if old and all(type(row.get("sequence")) is int for row in old):
                self._progress("compact", "正在整理较早的对话上下文，保留近期追问和引用线索。")
                summary = self._compress_history(summary, old)
                history = recent
                self.summary_update = {"summary": summary, "summary_through_sequence": old[-1]["sequence"]}
        restored = visible(history)
        if len(restored) > MAX_HISTORY_ENTRIES:
            raise PipelineError("Chat history exceeds the runner entry limit after compaction")
        if _json_bytes({**base, "history": restored, "summary": summary}) > budget:
            raise PipelineError("Chat context cannot fit this model after preserving the question and compacting prior turns")
        return restored, summary

    def _summary_bytes(self) -> int:
        return min(6000, self._context_bytes() // 4)

    def _compact_input(self, summary: str, history: list[dict]) -> dict:
        return {"summary": summary, "history": history, "max_summary_bytes": self._summary_bytes()}

    def _compress_history(self, summary: str, history: list[dict]) -> str:
        """Split only at Unicode boundaries, fitting the full compact payload.

        A long message may span batches; all original text is supplied in order
        and prior compaction output carries its continuity into the next batch.
        """
        budget = self._context_bytes("compact")
        batch = []
        for row in history:
            offset, first = 0, True
            while first or offset < len(row["content"]):
                self.check_budget()
                first = False
                if len(batch) >= MAX_HISTORY_ENTRIES:
                    summary = self._compact(summary, batch)
                    batch = []
                remaining = row["content"][offset:]
                fragment = {"role": row["role"], "content": remaining}
                if _json_bytes(self._compact_input(summary, [*batch, fragment])) <= budget:
                    batch.append(fragment)
                    break
                if batch:
                    summary = self._compact(summary, batch)
                    batch = []
                    continue
                # A single long turn is split by the serialized JSON size, so
                # Chinese glyphs, emoji, quotes and control escapes all count.
                low, high = 0, len(remaining)
                while low < high:
                    middle = (low + high + 1) // 2
                    fragment["content"] = remaining[:middle]
                    if _json_bytes(self._compact_input(summary, [fragment])) <= budget:
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    raise PipelineError("Chat compaction cannot fit its summary and a history fragment in the configured context")
                fragment["content"] = remaining[:low]
                summary = self._compact(summary, [fragment])
                offset += low
        if batch:
            summary = self._compact(summary, batch)
        return summary

    def _compact(self, summary: str, history: list[dict]) -> str:
        result = self._call("compact", self._compact_input(summary, history))
        if (not isinstance(result, dict) or not isinstance(result.get("summary"), str)
                or not result["summary"].strip() or _json_bytes(result["summary"]) > self._summary_bytes()):
            raise PipelineError("Chat context compaction did not return a bounded summary")
        return result["summary"]

    @staticmethod
    def _answer(value) -> dict:
        if not isinstance(value, dict):
            raise PipelineError("Chat answer must be an object")
        text, evidence, limitations = value.get("answer_markdown"), value.get("evidence"), value.get("limitations")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_ANSWER_CHARACTERS or "\x00" in text:
            raise PipelineError("Chat answer must contain bounded text")
        if not isinstance(evidence, list) or len(evidence) > 80:
            raise PipelineError("Invalid chat evidence list")
        if (not isinstance(limitations, list) or len(limitations) > 50
                or any(not isinstance(row, str) or len(row) > 2000 for row in limitations)):
            raise PipelineError("Invalid chat limitations")
        return {"answer_markdown": text, "evidence": deepcopy(evidence), "limitations": list(limitations)}

    def _verify(self, answer: dict) -> tuple[list[dict], list[str]]:
        evidence, errors, seen = [], [], set()
        for index, raw in enumerate(answer["evidence"]):
            self.check_budget()
            if not isinstance(raw, dict):
                errors.append(f"Evidence {index + 1} is not an object")
                continue
            item = {key: raw[key] for key in ("id", "source_id", "section_id", "quote")
                    if isinstance(raw.get(key), str)}
            if type(raw.get("page")) is int:
                item["page"] = raw["page"]
            identity = item.get("id")
            if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity) or identity in seen:
                errors.append("Invalid or duplicate chat evidence identifier")
                continue
            seen.add(identity)
            item["verified"] = False
            source_id = item.get("source_id")
            if isinstance(source_id, str) and source_id in self.pipeline.cached_documents:
                try:
                    self.pipeline._activate(source_id)
                except SourceError as exc:
                    item["verification_note"] = str(exc)
                    errors.append(str(exc))
            document = self.pipeline.library.documents.get(source_id) if isinstance(source_id, str) else None
            item.update(source_title=document.title if document else "Unknown source",
                        source_url=document.url if document else None, kind=document.kind if document else "paper")
            try:
                payload = {key: raw[key] for key in ("source_id", "section_id", "quote", "page") if key in raw}
                payload["id"] = "E" + str(index + 1)
                resolved = resolve_evidence(payload, self.pipeline.library)
                resolved["id"] = identity
                # The original quote and its offsets come solely from the source.
                section = next(row for row in document.sections if row["id"] == resolved["section_id"])
                resolved["offset"] = section["text"].find(resolved["quote"])
                resolved["end_offset"] = resolved["offset"] + len(resolved["quote"])
                item = resolved
            except (PacketError, TypeError, KeyError, ValueError) as exc:
                message = str(exc).replace("E" + str(index + 1) + ":", identity + ":", 1)
                item["verification_note"] = message
                errors.append(message)
            evidence.append(item)
        for match in re.finditer(r"\]\(\s*evidence:([^\s)]+)\s*\)", answer["answer_markdown"]):
            if match[1] not in seen:
                errors.append("Answer cites unknown evidence: " + match[1][:100])
        # Catch malformed evidence destinations as well as well-formed IDs.
        linked = re.findall(r"\]\(\s*evidence:([^)]*)\)", answer["answer_markdown"])
        if any(not IDENTIFIER.fullmatch(value.strip()) for value in linked):
            errors.append("Answer contains an invalid evidence citation")
        return evidence, list(dict.fromkeys(errors))

    def _review(self, question: str, answer: dict, evidence: list) -> dict:
        self._progress("review", "正在独立复核回答、原文依据和推论边界。")
        report = self._call("review", {"question": question, "answer_markdown": answer["answer_markdown"],
                            "evidence": evidence, "source_sections": self._source_sections(question, evidence),
                            "limitations": answer["limitations"]})
        if (not isinstance(report, dict) or type(report.get("passed")) is not bool
                or not isinstance(report.get("issues"), list) or len(report["issues"]) > 80
                or any(not isinstance(issue, str) or not issue.strip() or len(issue) > 4000 for issue in report["issues"])
                or (report["passed"] and report["issues"])):
            return {"passed": False, "issues": ["Independent review returned an invalid verdict"]}
        if not report["passed"] and not report["issues"]:
            return {"passed": False, "issues": ["Independent review did not approve this answer"]}
        return {"passed": report["passed"], "issues": list(report["issues"])}

    def _diagnostics(self, evidence: list, errors: list[str]) -> list[dict]:
        result = []
        maximum = min(12000, self._context_bytes() // 4)
        for item in evidence:
            self.check_budget()
            if maximum - _json_bytes(result) < 100 or len(result) >= 10:
                break
            if not isinstance(item, dict) or item.get("verified") is True:
                continue
            query = str(item.get("quote", ""))[:2000]
            suggestions = self.pipeline.search_source(query, limit=3)["results"] if query.strip() else []
            row = {"id": item.get("id"), "error": item.get("verification_note", "Invalid evidence"),
                   "suggested_locations": []}
            for suggestion in suggestions:
                remaining = maximum - _json_bytes([*result, row]) - 2
                if remaining <= 0:
                    break
                bounded = _fit_window(suggestion, remaining)
                if bounded and bounded["text"]:
                    row["suggested_locations"].append(bounded)
            if _json_bytes([*result, row]) <= maximum:
                result.append(row)
        return result

    def execute(self) -> dict:
        self.check_budget()
        question = self.input.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 10000:
            raise PipelineError("Chat question must contain 1–10000 characters")
        self._progress("source", "正在准备当前论文原文和已保留的对话资料。")
        if "main" not in self.pipeline.library.documents:
            self.pipeline.library.prepare_source()
        self.check_budget()
        base = {"question": question, "reading_context": deepcopy(self.input.get("reading_context", {})),
                "section_index": self._section_index(), "source_sections": self._source_sections(question)}
        self._bound_context(base)
        history, summary = self._history(base)
        base.update(history=history, summary=summary)
        self._progress("answer", "正在阅读原文并回答追问，引用将在生成后核验。")
        answer = self._answer(self._call("answer", base))
        self.emit({"type": "replace", "content": answer["answer_markdown"]})
        attempts, previous_errors, previous_attempt = [], [], None
        report = {"passed": False, "issues": [], "status": "not_run"}
        for round_number in range(MAX_REPAIRS + 1):
            prior_report = report
            evidence, quote_errors = self._verify(answer)
            report = {"passed": False, "issues": [], "status": "not_run"}
            if not quote_errors:
                if previous_attempt is not None and not previous_attempt["changed"]:
                    report = prior_report
                else:
                    report = self._review(question, answer, evidence)
            errors = quote_errors + report["issues"]
            if attempts:
                attempts[-1].update(status="succeeded" if not errors and report["passed"] else "failed",
                                    validation_errors=deepcopy(errors))
            if not errors and report["passed"]:
                break
            if round_number == MAX_REPAIRS:
                break
            self._progress("repair", f"正在自动修复回答与证据（第 {round_number + 1} 轮），然后重新校验。")
            repair = {**base, "source_sections": self._source_sections(question, evidence),
                      "repair_context": {"round": round_number + 1, "max_rounds": MAX_REPAIRS,
                          "errors": errors, "unchanged_errors": [error for error in errors if error in previous_errors],
                          "previous_answer": deepcopy(answer), "previous_attempt": previous_attempt,
                          "diagnostics": self._diagnostics(evidence, errors),
                          "strategy": "Reread authoritative original excerpts. Correct claims, exact quotes or locators; label unsupported inferences. Do not repeat a failed proposal or remove necessary support solely to pass."}}
            previous_errors = errors
            try:
                proposed = self._answer(self._call("answer", repair))
                changed = proposed != answer
                previous_attempt = {"round": round_number + 1, "changed": changed,
                                    "error": "" if changed else "Repair made no actual change; prior issues remain"}
                answer = proposed
                self.emit({"type": "replace", "content": answer["answer_markdown"]})
            except (TaskCancelled, BudgetExhausted):
                raise
            except (PipelineError, ValueError) as exc:
                # Preserve the preceding answer if a repair submission is
                # malformed. Its old errors remain authoritative next round.
                previous_attempt = {"round": round_number + 1, "changed": False, "error": str(exc)[:1000]}
            attempts.append(previous_attempt)
        coverage = list(dict.fromkeys(str(value)[:2000] for value in self.pipeline.library.limitations if value))
        main = self.pipeline.library.documents.get("main")
        if main is None or not any(section.get("text", "").strip() for section in main.sections):
            coverage.append("The main paper has no usable retained original text")
        elif main.coverage.get("full_text") is False:
            coverage.append("Some main-paper content lacks reliable extracted text")
        limitations = list(dict.fromkeys(answer["limitations"] + coverage))
        all_errors = list(dict.fromkeys(errors + coverage))
        succeeded = not all_errors and report["passed"]
        content = answer["answer_markdown"]
        # Network/source gaps are host-observed; never rely on the model to
        # disclose them. Keep useful text even when verification is incomplete.
        if not succeeded:
            notices = all_errors or ["回答尚未完成独立核验。"]
            content += "\n\n> 本次回答仍有待核验或资料缺口：\n" + "\n".join(
                "> - " + " ".join(value.split()) for value in notices[:12])
        self.emit({"type": "replace", "content": content})
        return {"content": content, "evidence_index": evidence,
                "verification": {"quote_match": {"passed": not quote_errors, "issues": quote_errors},
                                 "independent_review": report, "limitations": limitations,
                                 "issues": all_errors, "repair_attempts": attempts,
                                 "verified_evidence_count": sum(item.get("verified") is True for item in evidence)},
                "status": "succeeded" if succeeded else "partial", **self.summary_update}
