"""Task-scoped tool RPC and server-side result verification.

The runner gets no direct shell/file tools. Native skill scripts execute in a
Linux Landlock + seccomp sandbox with no network and only task-directory writes.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import hashlib
import json
import mimetypes
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any
from uuid import uuid4

from .evidence import EVIDENCE_FIELDS, EVIDENCE_METHODS, PacketError, resolve_evidence, validate_visual_source_coverage
from .source_tools import SourceError, SourceLibrary, section_preview

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 192 * 1024 * 1024
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".org", ".csv"}
# Acquisition scripts receive local, verified PDFs only and cannot use sockets.
SCRIPT_FLAGS = {
    "resolve_paper.py": {"--input", "--output", "--zotero-mode"},
    "collect_metadata.py": {"--input", "--output"},
    "build_identity_contract.py": {"--input", "--resolve", "--trace-output", "--output"},
    "fetch_pdf.py": {"--input", "--identity", "--dest-dir", "--output", "--reference"},
    "extract_source_text.py": {"--input", "--output", "--raw-sections-output", "--full-text-output", "--max-pages"},
    "extract_evidence.py": {"--input", "--source-manifest", "--output", "--max-pages", "--max-chunks-per-section"},
    "extract_pdf_assets.py": {"--input", "--output", "--assets-dir", "--max-pages", "--min-searchable-chars", "--ocr-dpi", "--figure-dpi"},
    "plan_figures.py": {"--input", "--evidence", "--assets", "--output", "--paper-id", "--language", "--max-items"},
    "plan_figure_table_decisions.py": {"--source-manifest", "--figures", "--assets", "--review-decisions", "--language", "--output", "--paper-id"},
    "build_synthesis_bundle.py": {"--metadata", "--evidence", "--figures", "--assets", "--source-manifest", "--figure-table-decisions", "--figure-decisions", "--language", "--output"},
    "materialize_figure_asset.py": {"--source-image", "--input", "--title", "--save-mode", "--subdir", "--filename", "--asset-subdir", "--label", "--output"},
    "lint_note.py": {"--input", "--plan-file", "--output", "--paper-id", "--language"},
    "lint_grounding.py": {"--note-plan", "--source-manifest", "--synthesis-bundle", "--input", "--output", "--language", "--figure-table-decisions", "--synthesis-file", "--content-file", "--bundle-json", "--figure-decisions"},
    "write_obsidian_note.py": {"--input", "--content-file", "--note-plan", "--plan-file", "--output", "--save-mode", "--subdir", "--filename", "--asset-subdir", "--target-directory", "--paper-id", "--language", "--title", "--synthesis-bundle", "--source-manifest", "--figure-table-decisions", "--figure-decisions", "--lint-json"},
}
SCALAR_FLAGS = {"--max-pages", "--max-chunks-per-section", "--max-items", "--min-searchable-chars", "--ocr-dpi", "--figure-dpi", "--zotero-mode", "--save-mode", "--language", "--paper-id", "--title", "--label"}
READER_FIELDS = {
    "missing_premises": "缺失前提与处理", "narrative": "不依赖分数的全文复述",
    "first_memory": "读者首先记住的解释", "chapter_links": "读者对章节联系的解释",
    "research_question": "研究问题", "contribution": "作者的主要贡献",
    "mechanism": "输入→组件／关系→输出", "experiment": "中心实验问题／固定项／比较者／结果／意义",
    "limitations": "证据的适用范围与限制", "understanding": "三句认识变化复述", "reconstruction": "结果解释",
}


class PipelineError(ValueError):
    pass


class TaskCancelled(PipelineError):
    pass


class BudgetExhausted(PipelineError):
    """Fatal attempt limit, never a recoverable model tool error."""


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite constant is not valid JSON: " + value)


def task_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(character) < 32 for character in value) or value.startswith("~"):
        raise PipelineError("Invalid artifact path")
    raw = Path(value)
    if ".." in raw.parts:
        raise PipelineError("Artifact path escapes the task directory")
    candidate = raw if raw.is_absolute() else root / raw
    root = root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PipelineError("Artifact path escapes the task directory")
    # Symlinks are never artifacts, even if their current target happens to be safe.
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise PipelineError("Symbolic links are not allowed in artifacts")
    if must_exist and not resolved.is_file():
        raise PipelineError("Artifact file does not exist")
    return resolved


# Fixed trusted scripts only: this guard is defense in depth around Python file
# APIs on hosts predating Landlock. Native extension arguments are additionally
# validated by the parent. It is not an arbitrary-code execution sandbox.
PYTHON_FILE_GUARD = r"""
import os, sys, runpy
root, skill, script, protected_json = sys.argv[1:5]
import json
protected = set(json.loads(protected_json))
read_roots = [root, skill, sys.prefix, sys.base_prefix, '/usr', '/lib', '/lib64', '/etc/fonts', '/dev/null', '/dev/urandom']
def inside(path, roots):
    if isinstance(path, int): return True
    if not isinstance(path, (str, bytes, os.PathLike)): return False
    path = os.path.realpath(os.fsdecode(path))
    return any(path == r or path.startswith(r.rstrip('/') + '/') for r in roots)
def host_owned(path):
    if isinstance(path, int): return False
    resolved = os.path.realpath(os.fsdecode(path))
    parts = os.path.relpath(resolved, root).split(os.sep)
    return resolved in protected or any(part.startswith('host-') or part in {'reading-packet.json', 'source-assets'} for part in parts)
def guard(event, args):
    if event == 'open':
        path, mode, flags = args
        write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if not inside(path, [root] if write else read_roots): raise PermissionError('Native script file access denied')
        if write and host_owned(path): raise PermissionError('Host-owned artifact cannot be overwritten')
    elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.truncate'}:
        if not inside(args[0], [root]) or host_owned(args[0]): raise PermissionError('Native script write denied')
    elif event in {'os.rename', 'os.link', 'os.symlink'}:
        if not all(inside(p, [root]) and not host_owned(p) for p in args[:2]): raise PermissionError('Native script path mutation denied')
    elif event in {'os.system', 'subprocess.Popen', 'os.exec', 'os.spawn'}:
        raise PermissionError('Native subprocess execution denied; use platform tools')
sys.addaudithook(guard)
sys.path.insert(0, skill + '/scripts')
sys.argv = [script, *sys.argv[5:]]
runpy.run_path(script, run_name='__main__')
"""

def _sandbox(work_dir: str, skill_root: str) -> None:
    """Fail closed if Linux filesystem/network restrictions are unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    # Landlock filesystem ABI1; handled bits 0..12 cover execute/read/write and
    # all creation/removal operations. Cross-directory rename remains forbidden.
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]
    handled = (1 << 13) - 1
    attr = Ruleset(handled)
    fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0:
        fd = None  # Python audit guard below covers older kernels.
    read = (1 << 0) | (1 << 2) | (1 << 3)
    roots = {"/usr", "/lib", "/lib64", "/bin", sys.prefix, skill_root,
             "/etc/fonts", "/etc/localtime", "/dev/null", "/dev/urandom"}
    try:
        for path, access in ([(p, read) for p in roots] + [(work_dir, handled)]) if fd is not None else []:
            if not os.path.exists(path):
                continue
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if not os.path.isdir(path):
                    access &= (1 << 0) | (1 << 1) | (1 << 2)
                rule = Beneath(access, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) < 0:
                    os._exit(126)
            finally:
                os.close(path_fd)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or (fd is not None and libc.syscall(446, fd, 0) != 0):
            os._exit(126)
    finally:
        if fd is not None:
            os.close(fd)
    # Deny sockets entirely. Native acquisition can only reuse the verified file.
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2")
        seccomp.seccomp_init.restype = ctypes.c_void_p
        seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
        seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        ctx = seccomp.seccomp_init(0x7FFF0000)
        if not ctx:
            os._exit(126)
        for name in [b"socket", b"socketpair"]:
            number = seccomp.seccomp_syscall_resolve_name(name)
            if seccomp.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0) < 0:
                os._exit(126)
        if seccomp.seccomp_load(ctx) < 0:
            os._exit(126)
        seccomp.seccomp_release(ctx)
    except Exception:
        os._exit(126)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TOTAL_ARTIFACT_BYTES, MAX_TOTAL_ARTIFACT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))


def terminate_group(process: subprocess.Popen, grace: float = 2) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _normalize_quote(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).replace("\u00ad", "")
    value = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", value)
    return " ".join(value.split()).casefold()


def validate_result(result: dict, library: SourceLibrary, native_checks: list[dict] | None = None, *,
                    workflow_mode: str = "native", cross_paper_requested: bool | None = None) -> tuple[dict, list[str]]:
    """Never accept the model's `verified` field as evidence of correctness."""
    if not isinstance(result, dict):
        raise PipelineError("Runner returned an invalid result")
    if workflow_mode not in {"native", "guided"} or (cross_paper_requested is not None and type(cross_paper_requested) is not bool):
        raise PipelineError("Invalid trusted workflow verification options")
    brief, full = result.get("brief_markdown"), result.get("full_markdown")
    if not isinstance(brief, str) or not isinstance(full, str) or not brief.strip() or not full.strip():
        raise PipelineError("Runner did not produce both required reading views")
    if len(brief) > 200000 or len(full) > 1000000:
        raise PipelineError("Reading exceeds the output size limit")
    recovery = (result.get("verification") or {}).get("source_visual_coverage")
    visual_source_complete = (workflow_mode == "guided" and getattr(library, "rules_version", None) == "source-structured-v3"
                              and validate_visual_source_coverage(library, recovery))
    if "main" not in library.documents or not (visual_source_complete or any(section.get("text", "").strip() for section in library.documents["main"].sections)):
        raise PipelineError("Main paper usable source text was not acquired")
    errors, evidence, seen = [], [], set()
    if not library.documents["main"].coverage.get("full_text") and not visual_source_complete:
        errors.append("Main paper contains pages without reliable text; this is an incomplete reading")
    quote_error_start = len(errors)
    method_counts = {method: 0 for method in EVIDENCE_METHODS.values()}
    method_failures = set()
    items = result.get("evidence_index", [])
    evidence_limit = 480 if workflow_mode == "guided" and getattr(library, "rules_version", None) == "source-structured-v3" else 300
    if not isinstance(items, list) or len(items) > evidence_limit:
        raise PipelineError("Invalid evidence index")
    for raw in items:
        if not isinstance(raw, dict):
            errors.append("Malformed evidence item")
            continue
        item = {k: raw[k] for k in ("id", "source_id", "quote", "section_id", "section", "page", "kind") if k in raw}
        if workflow_mode == "guided":
            item.update({key: raw[key] for key in EVIDENCE_FIELDS if key in raw})
        identity = str(item.get("id", ""))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,49}", identity) or identity in seen:
            errors.append("Invalid or duplicate evidence identifier")
            continue
        seen.add(identity)
        item["verified"] = False
        source = library.documents.get(item.get("source_id"))
        if item.get("kind") == "inference":
            item["source_title"] = source.title if source else "System inference"
            item["source_url"] = source.url if source else None
            item["verification_note"] = "Inference is not a paper observation"
        elif workflow_mode == "guided":
            evidence_type = item.get("evidence_type", "text_quote")
            method = EVIDENCE_METHODS.get(evidence_type) if isinstance(evidence_type, str) else None
            if evidence_type == "text_quote" and (item.get("selection") == "section" or "quote" not in item):
                method = "section_lookup"
            if method:
                method_counts.setdefault(method, 0)
                method_counts[method] += 1
            try:
                item = resolve_evidence({key: item[key] for key in EVIDENCE_FIELDS if key in item}, library)
            except (PacketError, TypeError, ValueError) as exc:
                errors.append(f"{identity}: evidence verification failed: {exc}")
                if method:
                    method_failures.add(method)
                item["source_title"] = source.title if source else "Unresolved source"
                item["source_url"] = source.url if source else None
                item["evidence_type"] = evidence_type
                item["verification_method"] = method
                if source:
                    item["kind"] = source.kind
        elif source:
            item["source_title"], item["source_url"] = source.title, source.url
            item["kind"] = source.kind
            quote = _normalize_quote(str(item.get("quote", "")))
            candidates = source.sections
            if item.get("section_id") is not None:
                candidates = [s for s in candidates if s.get("id") == item["section_id"]]
            if item.get("page") is not None:
                candidates = [s for s in candidates if str(s.get("page")) == str(item["page"])]
            matches = [s for s in candidates if len(quote) >= 12 and quote in _normalize_quote(s["text"])]
            if matches:
                item["verified"] = True
                if matches[0].get("page") is not None:
                    item["page"] = matches[0]["page"]
                item["section"] = matches[0].get("section", matches[0]["id"])
            else:
                errors.append(f"{identity}: quoted evidence was not found in the identified source/page")
        else:
            item["source_title"] = "Unresolved source"
            errors.append(f"{identity}: source has not been read")
        evidence.append(item)
    if not any(e.get("verified") and e.get("kind") == "paper" for e in evidence):
        errors.append("No verified evidence from the main paper")
    citation_error_start = len(errors)
    for label, text in (("brief", brief), ("full", full)):
        citations = set(re.findall(r"\[[^\]]+\]\(evidence:([A-Za-z][A-Za-z0-9_-]*)\)", text))
        citations.update(re.findall(r"\[(E\d+)\](?!\()", text))
        if not citations:
            errors.append(f"{label}: no evidence citations")
        for citation in citations - seen:
            errors.append(f"{label}: unresolved citation {citation}")
    quote_status = "partial" if len(errors) > quote_error_start else "passed"
    evidence_status = quote_status
    if workflow_mode == "guided":
        quote_status = ("not_requested" if not method_counts["quote_match"] else
                        "partial" if "quote_match" in method_failures or len(errors) > citation_error_start else "passed")
    external_sources = [document for document in library.documents.values() if document.kind == "external"]
    duplicate_external = [document for document in external_sources if library.is_main_source(document.id, document.url, document.sha256)]
    incomplete_external = [document for document in external_sources if document.coverage.get("full_text") is not True]
    limitations = list(library.limitations)
    cross_paper_status = "completed" if external_sources else "not_completed"
    if duplicate_external:
        cross_paper_status = "partial"
        errors.append("The main paper or an identical copy was incorrectly identified as an external source")
    if workflow_mode == "guided" and not external_sources:
        if cross_paper_requested is False:
            cross_paper_status = "not_requested"
        elif cross_paper_requested is True:
            errors.append("Requested cross-paper verification has no retained related full text")
    if incomplete_external:
        cross_paper_status = "partial"
        errors.append("Cross-paper verification includes related papers without complete source text")
        for document in incomplete_external:
            limitation = f"Related paper {document.id} has incomplete full-text coverage"
            missing_pages = document.coverage.get("missing_text_pages", [])
            if missing_pages:
                limitation += "; missing reliable text on pages " + ", ".join(str(page) for page in missing_pages)
            limitations.append(limitation)
    verification = dict(result.get("verification") or {})
    # Agent/native assertions are preserved as reports, never presented as the
    # independent server result. The exact script ledger comes from execution.
    verification = {"agent_report": verification, "native_checks": native_checks or [],
                    "server_quote_check": quote_status,
                    "errors": errors, "limitations": list(dict.fromkeys(limitations)),
                    "coverage": library.documents["main"].coverage,
                    "cross_paper_status": cross_paper_status}
    if workflow_mode == "guided":
        verification["server_evidence_check"] = evidence_status
        if visual_source_complete:
            verification["source_visual_coverage"] = {"complete": True, "recovered_pages": list(recovery["recovered_pages"]),
                                                      "observation_ids": list(recovery["observation_ids"])}
        verification["verification_methods"] = {
            method: {"count": count, "status": "partial" if method in method_failures else "passed" if count else "not_requested"}
            for method, count in method_counts.items()}
    latest = {c.get("script"): c for c in native_checks or []}
    required = ({"resolve_paper.py", "collect_metadata.py", "build_identity_contract.py", "fetch_pdf.py",
                 "extract_source_text.py", "extract_evidence.py", "extract_pdf_assets.py", "plan_figures.py",
                 "plan_figure_table_decisions.py", "build_synthesis_bundle.py", "lint_grounding.py",
                 "lint_note.py", "write_obsidian_note.py"} if library.skill_id == "deeppapernote" else {"validate_note.ts"})
    missing = required - latest.keys()
    failed = [name for name in required & latest.keys() if latest[name].get("exit_code") != 0]
    if workflow_mode == "native" and (missing or failed):
        errors.append("Native skill completion has missing or failed checks: " + ", ".join(sorted(missing | set(failed))))
    agent_verification = result.get("verification") or {}
    if agent_verification.get("passed") is False or (agent_verification.get("product_review") or {}).get("passed") is False:
        errors.append("Independent product review reported unresolved issues")
    clean = {"brief_markdown": brief, "full_markdown": full, "evidence_index": evidence,
             "verification": verification, "provenance": dict(result.get("provenance") or {}),
             "artifacts": result.get("artifacts", [])}
    return clean, errors


class Pipeline:
    def __init__(self, work_dir: Path, skill_root: Path, paper: dict, skill_id: str,
                 workspace_id: str, cancelled: threading.Event, project_dir: Path, vision: bool = False,
                 max_tool_calls: int = 160, *, workflow_mode: str = "native", rules_version: str = "source-sections-v2",
                 recover_invalid_cache: bool = False):
        if type(max_tool_calls) is not int or not 10 <= max_tool_calls <= 500:
            raise PipelineError("Tool call budget must be an integer between 10 and 500")
        self.work_dir, self.skill_root = work_dir.resolve(), skill_root.resolve()
        self.workspace_id, self.skill_id = workspace_id, skill_id
        self.vision = vision
        self.state_lock = threading.RLock()
        self.cancelled, self.project_dir = cancelled, project_dir
        self.library = SourceLibrary(self.work_dir, paper, skill_id, workflow_mode=workflow_mode, rules_version=rules_version,
                                     recover_invalid_cache=recover_invalid_cache)
        self.native_checks: list[dict] = []
        self.tool_calls = 0
        self.max_tool_calls = max_tool_calls
        self.inspected_artifacts: set[str] = set()
        self.inspected_skill_files: set[str] = set()
        self.inspected_images: dict[str, str] = {}
        self.sealed = (self.work_dir / "host-native-seal.json").is_file()
        image_ledger = self.work_dir / "host-image-inspections.json"
        if image_ledger.is_file():
            self.inspected_images = json.loads(image_ledger.read_text())
        ledger = self.work_dir / "native-execution-ledger.json"
        if ledger.is_file():
            self.native_checks = json.loads(ledger.read_text())

    def _fingerprint(self, path: Path) -> str:
        data = path.read_bytes()
        if path.suffix.lower() in TEXT_EXTENSIONS:
            data = data.replace(str(self.work_dir).encode(), b"@PAPER_READER_WORKDIR@")
        return hashlib.sha256(data).hexdigest()

    def _fingerprints(self) -> dict[str, str]:
        return {entry["path"]: self._fingerprint(task_path(self.work_dir, entry["path"], must_exist=True))
                for entry in self.list_artifacts()["files"]
                if not entry["path"].startswith((".tmp/", ".cache/", "host-")) and entry["path"] != "native-execution-ledger.json"}

    def _reserved_paths(self) -> set[str]:
        reserved = {"source_corpus.json", "source_manifest.json", "native-execution-ledger.json", "server-verification.json",
                    "reading-packet.json",
                    "host-native-attestation.json", "host-native-seal.json", "host-image-inspections.json",
                    "native/quality_review.json", "native/readability_review.json", "native/grounding.json", "native/lint.json"}
        reserved.update(document.path for document in self.library.documents.values())
        for document in self.library.documents.values():
            reserved.update(item["path"] for item in document.coverage.get("representations", []) if item.get("path"))
        reserved.update(item["path"] for item in getattr(self.library, "prepared_visuals", {}).values() if item.get("path"))
        reserved.update(item["path"] for item in getattr(self.library, "visual_assets", {}).values() if item.get("path"))
        # Canonical acquisition/check products can be regenerated only by their
        # producing script. Authored plans and notes remain editable before seal.
        protected_scripts = {"resolve_paper.py", "collect_metadata.py", "build_identity_contract.py", "fetch_pdf.py",
                             "extract_source_text.py", "extract_evidence.py", "extract_pdf_assets.py", "lint_note.py", "lint_grounding.py"}
        for entry in self.native_checks:
            reserved.update(entry.get("log_hashes", {}))
            if entry.get("script") in protected_scripts:
                reserved.update(entry.get("output_hashes", {}))
        return reserved

    def host_call(self, name: str, args: dict) -> dict:
        """Internal runner-only RPCs: never registered as model tools."""
        with self.state_lock:
            return self._host_call(name, args)

    def _host_call(self, name: str, args: dict) -> dict:
        self._consume_tool_call()
        if name == "host_native_status":
            if not self.sealed:
                return {"sealed": False}
            self.verify_native_seal()
            attestation = json.loads((self.work_dir / "host-native-attestation.json").read_text())
            note_path = str(task_path(self.work_dir, attestation["note_path"], must_exist=True).relative_to(self.work_dir))
            map_path = attestation.get("map_path")
            if map_path:
                map_path = str(task_path(self.work_dir, map_path, must_exist=True).relative_to(self.work_dir))
            return {"sealed": True, "note_path": note_path, "map_path": map_path,
                    "reviews": {"passed": attestation.get("passed") is True,
                                "quality": attestation["quality"], "readability": attestation["readability"]}}
        if name == "host_native_attest":
            return self._attest_native(**args)
        if name == "host_native_review_lookup":
            return self._native_review_lookup(**args)
        if name == "host_native_seal":
            return self._seal_native(**args)
        raise PipelineError("Unknown internal host RPC")

    @staticmethod
    def _reader_section(text: str) -> tuple[int, int] | None:
        headings = list(re.finditer(r"^## 独立阅读检查记录[ \t]*\r?$", text, re.M))
        if len(headings) != 1:
            return None
        start = headings[0].start()
        following = re.search(r"^## ", text[headings[0].end():], re.M)
        end = headings[0].end() + following.start() if following else len(text)
        return start, end

    def _reader_record(self) -> dict | None:
        path = self.work_dir / "host-native-reader-record.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def _trusted_reader_section(self, path: Path, text: str) -> tuple[int, int] | None:
        record = self._reader_record()
        section = self._reader_section(text)
        if record and section and record.get("map_path") == str(path.relative_to(self.work_dir)):
            if hashlib.sha256(text[section[0]:section[1]].encode()).hexdigest() == record.get("sha256"):
                return section
        return None

    def _protect_reader_record(self, path: Path, replacement: str) -> None:
        if path.is_file():
            original = path.read_text(errors="replace")
            section = self._trusted_reader_section(path, original)
            if section:
                new_section = self._reader_section(replacement)
                if not new_section or replacement[new_section[0]:new_section[1]] != original[section[0]:section[1]]:
                    raise PipelineError("Host-owned independent reader record cannot be overwritten")

    def _review_keys(self, note: Path, mapping: Path | None, policy_key: str) -> dict:
        if not isinstance(policy_key, str) or not policy_key.strip() or len(policy_key) > 4096:
            raise PipelineError("Native review requires a bounded policy key")
        common = {"note": hashlib.sha256(note.read_bytes()).hexdigest(), "source": self.library.fingerprint(),
                  "policy": policy_key, "skill": self.skill_id, "rules": "native-review-cache-v2"}
        map_text = mapping.read_text() if mapping else ""
        section = self._trusted_reader_section(mapping, map_text) if mapping else None
        if section:
            map_text = map_text[:section[0]] + map_text[section[1]:]
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return {"quality": digest({**common, "semantic_map": map_text}), "readability": digest(common)}

    @staticmethod
    def _review_passed(report: dict) -> bool:
        return isinstance(report, dict) and report.get("passed") is True and report.get("issues", []) == []

    def _native_review_lookup(self, note_path: str, policy_key: str, map_path: str | None = None) -> dict:
        note = task_path(self.work_dir, note_path, must_exist=True)
        mapping = task_path(self.work_dir, map_path, must_exist=True) if map_path else None
        keys = self._review_keys(note, mapping, policy_key)
        result = {"note_sha256": hashlib.sha256(note.read_bytes()).hexdigest()}
        if mapping:
            result["map_sha256"] = hashlib.sha256(mapping.read_bytes()).hexdigest()
        path = self.work_dir / "host-native-review-cache.json"
        cache = json.loads(path.read_text()) if path.is_file() else {}
        for kind in ("quality", "readability"):
            entry = cache.get(kind, {})
            if entry.get("key") == keys[kind] and self._review_passed(entry.get("report")):
                result[kind] = entry["report"]
        return result

    def _attest_native(self, note_path: str, note_sha256: str, quality: dict, readability: dict,
                       map_path: str | None = None, map_sha256: str | None = None, policy_key: str | None = None) -> dict:
        if self.sealed:
            raise PipelineError("Native artifacts have already been sealed")
        note = task_path(self.work_dir, note_path, must_exist=True)
        if hashlib.sha256(note.read_bytes()).hexdigest() != note_sha256:
            raise PipelineError("Native note changed during independent review")
        if not isinstance(quality, dict) or not isinstance(readability, dict):
            raise PipelineError("Independent native reports must be objects")
        if policy_key is not None:
            for report in (quality, readability):
                if type(report.get("passed")) is not bool or not isinstance(report.get("issues"), list) or not all(isinstance(issue, str) for issue in report["issues"]):
                    raise PipelineError("Independent native report lacks a real verdict and issues")
                if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode()) > 256 * 1024:
                    raise PipelineError("Independent native report exceeds its bound")
        hashes = {str(note.relative_to(self.work_dir)): self._fingerprint(note)}
        mapping = None
        if map_path:
            mapping = task_path(self.work_dir, map_path, must_exist=True)
            if not map_sha256 or hashlib.sha256(mapping.read_bytes()).hexdigest() != map_sha256:
                raise PipelineError("Native map changed during independent review")
        reader_written = False
        if policy_key is not None:
            self._review_keys(note, mapping, policy_key)  # Validate policy/source before any host write.
            if self.skill_id == "ljg-paper" and self._review_passed(quality) and self._review_passed(readability):
                reader = readability.get("reader_record")
                if not isinstance(reader, dict) or set(reader) != set(READER_FIELDS) or not all(isinstance(value, str) and value.strip() and len(value) <= 8000 for value in reader.values()):
                    raise PipelineError("Passing ljg readability requires all actual structured reader answers")
                text = mapping.read_text() if mapping else ""
                section = self._reader_section(text)
                if not section:
                    raise PipelineError("ljg map requires exactly one existing independent reader record section")
                lines = ["## 独立阅读检查记录", "", "以下为独立阅读者的实际回答，由 host 根据已完成的检查写入。", ""]
                lines.extend("- " + label + "：" + " ".join(reader[key].split()) for key, label in READER_FIELDS.items())
                lines.extend(["- 全文理解判定：NARRATIVE_PASS", "- 问题与贡献判定：PAPER_IDENTITY_PASS",
                              "- 认识变化判定：UNDERSTANDING_PASS", "- 结果解释判定：RECONSTRUCTION_PASS", "", ""])
                replacement = "\n".join(lines)
                self._atomic_text(mapping, text[:section[0]] + replacement + text[section[1]:])
                record = {"map_path": str(mapping.relative_to(self.work_dir)), "sha256": hashlib.sha256(replacement.encode()).hexdigest()}
                self._atomic_text(self.work_dir / "host-native-reader-record.json", json.dumps(record, sort_keys=True))
                reader_written = True
            keys = self._review_keys(note, mapping, policy_key)
            cache = {kind: {"key": keys[kind], "report": report} for kind, report in (("quality", quality), ("readability", readability))
                     if self._review_passed(report)}
            self._atomic_text(self.work_dir / "host-native-review-cache.json", json.dumps(cache, ensure_ascii=False, sort_keys=True))
        if mapping:
            hashes[str(mapping.relative_to(self.work_dir))] = self._fingerprint(mapping)
        payload = {"hashes": hashes, "note_path": str(note.relative_to(self.work_dir)), "map_path": map_path,
                   "quality": quality, "readability": readability,
                   "passed": self._review_passed(quality) and self._review_passed(readability),
                   "policy_key": policy_key, "reader_record_written": reader_written}
        for filename, content in (("host-native-attestation.json", payload), ("native/quality_review.json", quality), ("native/readability_review.json", readability)):
            target = task_path(self.work_dir, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_text(target, json.dumps(content, ensure_ascii=False, indent=2))
        return {"recorded": True, "passed": payload["passed"], "reader_record_written": reader_written,
                **({"map_sha256": hashlib.sha256(mapping.read_bytes()).hexdigest()} if mapping else {})}

    def _visual_claims_verified(self) -> None:
        decisions = self.work_dir / "native" / "figure_decisions.json"
        if not decisions.is_file():
            return
        observed = set(self.inspected_images.values())
        def visit(value):
            if isinstance(value, dict):
                claimed = value.get("reviewed_asset_sha256")
                if claimed and claimed not in observed:
                    raise PipelineError("Native visual review claims an image that was never inspected")
                for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)
        visit(json.loads(decisions.read_text()))

    def _seal_native(self, note_path: str | None = None, map_path: str | None = None) -> dict:
        if self.sealed:
            self.verify_native_seal()
            return {"sealed": True, "reused": True}
        attestation_path = self.work_dir / "host-native-attestation.json"
        if not attestation_path.is_file():
            raise PipelineError("Independent native reviews have not been attested by the host")
        attestation = json.loads(attestation_path.read_text())
        if not attestation.get("passed"):
            raise PipelineError("Independent native review did not pass")
        if attestation.get("reader_record_written"):
            mapping = task_path(self.work_dir, attestation["map_path"], must_exist=True)
            if not self._trusted_reader_section(mapping, mapping.read_text()):
                raise PipelineError("Host-owned independent reader record is missing or changed")
        if note_path and attestation.get("note_path") != note_path:
            raise PipelineError("Native submission does not match the independently reviewed note")
        if map_path and attestation.get("map_path") != map_path:
            raise PipelineError("Native submission does not match the independently reviewed map")
        latest = {item["script"]: item for item in self.native_checks}
        required = ({"resolve_paper.py", "collect_metadata.py", "build_identity_contract.py", "fetch_pdf.py", "extract_source_text.py",
                     "extract_evidence.py", "extract_pdf_assets.py", "plan_figures.py", "plan_figure_table_decisions.py",
                     "build_synthesis_bundle.py", "lint_note.py", "lint_grounding.py", "write_obsidian_note.py"}
                    if self.skill_id == "deeppapernote" else {"validate_note.ts"})
        for name in required:
            entry = latest.get(name)
            if not entry or entry.get("exit_code") != 0 or entry.get("semantic_pass") is not True:
                raise PipelineError("Native script has not passed its actual checks: " + name)
        def relative(value: str | None) -> str:
            if not value:
                raise PipelineError("Native check is missing a required input/output binding")
            return str(task_path(self.work_dir, value).relative_to(self.work_dir))
        def arguments(script: str) -> dict[str, str]:
            values = latest[script].get("args", [])
            return dict(zip(values[::2], values[1::2]))
        if self.skill_id == "ljg-paper":
            values = latest["validate_note.ts"].get("args", [])
            if len(values) != 3 or values[1] != "--map" or not attestation.get("map_path"):
                raise PipelineError("ljg validator must bind the reviewed note and map")
            if relative(values[0]) != relative(attestation["note_path"]) or relative(values[2]) != relative(attestation["map_path"]):
                raise PipelineError("ljg validator checked a different note/map than the independent review")
        else:
            def bind(consumer: str, flag: str, producer: str, output_flag: str = "--output") -> None:
                if relative(arguments(consumer).get(flag)) != relative(arguments(producer).get(output_flag)):
                    raise PipelineError("Native stage inputs are not bound to the checked source: " + consumer + " " + flag)
            for consumer, flag, producer in [
                ("collect_metadata.py", "--input", "resolve_paper.py"),
                ("build_identity_contract.py", "--input", "collect_metadata.py"),
                ("build_identity_contract.py", "--resolve", "resolve_paper.py"),
                ("fetch_pdf.py", "--identity", "build_identity_contract.py"),
                ("fetch_pdf.py", "--input", "collect_metadata.py"),
                ("extract_source_text.py", "--input", "fetch_pdf.py"),
                ("extract_evidence.py", "--input", "fetch_pdf.py"),
                ("extract_evidence.py", "--source-manifest", "extract_source_text.py"),
                ("extract_pdf_assets.py", "--input", "fetch_pdf.py"),
                ("plan_figures.py", "--evidence", "extract_evidence.py"),
                ("plan_figures.py", "--assets", "extract_pdf_assets.py"),
                ("plan_figure_table_decisions.py", "--source-manifest", "extract_source_text.py"),
                ("plan_figure_table_decisions.py", "--figures", "plan_figures.py"),
                ("plan_figure_table_decisions.py", "--assets", "extract_pdf_assets.py"),
                ("build_synthesis_bundle.py", "--metadata", "collect_metadata.py"),
                ("build_synthesis_bundle.py", "--evidence", "extract_evidence.py"),
                ("build_synthesis_bundle.py", "--source-manifest", "extract_source_text.py"),
                ("build_synthesis_bundle.py", "--figure-decisions", "plan_figure_table_decisions.py"),
                ("lint_grounding.py", "--source-manifest", "extract_source_text.py"),
                ("lint_grounding.py", "--bundle-json", "build_synthesis_bundle.py"),
                ("lint_grounding.py", "--figure-decisions", "plan_figure_table_decisions.py"),
                ("write_obsidian_note.py", "--lint-json", "lint_note.py"),
                ("write_obsidian_note.py", "--source-manifest", "extract_source_text.py"),
                ("write_obsidian_note.py", "--figure-decisions", "plan_figure_table_decisions.py"),
                ("write_obsidian_note.py", "--input", "collect_metadata.py"),
            ]:
                bind(consumer, flag, producer)
            if relative(arguments("lint_note.py").get("--input")) != relative(attestation["note_path"]):
                raise PipelineError("Native lint checked a different note than the independent review")
            if relative(arguments("write_obsidian_note.py").get("--content-file")) != relative(attestation["note_path"]):
                raise PipelineError("Native save used a different note than the independent review")
            if relative(arguments("lint_note.py").get("--plan-file")) != relative(arguments("lint_grounding.py").get("--note-plan")):
                raise PipelineError("Native formatting and grounding checks used different note plans")
        # Check immutable acquisition artifacts and final check inputs/outputs.
        critical = {"resolve_paper.py", "collect_metadata.py", "build_identity_contract.py", "fetch_pdf.py", "extract_source_text.py",
                    "extract_evidence.py", "extract_pdf_assets.py", "lint_note.py", "lint_grounding.py", "write_obsidian_note.py", "validate_note.ts"}
        for name in required & critical:
            entry = latest[name]
            for relative, digest in {**entry.get("input_hashes", {}), **entry.get("output_hashes", {})}.items():
                path = task_path(self.work_dir, relative, must_exist=True)
                if self._fingerprint(path) != digest:
                    raise PipelineError("Native input or script output changed after its check: " + relative)
        for relative, digest in attestation["hashes"].items():
            if self._fingerprint(task_path(self.work_dir, relative, must_exist=True)) != digest:
                raise PipelineError("Native note/map changed after independent review")
        self._visual_claims_verified()
        fingerprints = self._fingerprints()
        native = {name: digest for name, digest in fingerprints.items()
                  if name.startswith("native/") or name in self._reserved_paths()
                  or any(name in entry.get("output_hashes", {}) for entry in self.native_checks)}
        # source_corpus is a host-owned evolving index, updated by related fetches.
        native.pop("source_corpus.json", None)
        native.pop("source_manifest.json", None)
        native["host-native-attestation.json"] = self._fingerprint(attestation_path)
        if attestation.get("reader_record_written"):
            native["host-native-reader-record.json"] = self._fingerprint(self.work_dir / "host-native-reader-record.json")
        for entry in self.native_checks:
            for relative, digest in entry.get("log_hashes", {}).items():
                if self._fingerprint(task_path(self.work_dir, relative, must_exist=True)) != digest:
                    raise PipelineError("Native execution log changed after its script: " + relative)
                native[relative] = digest
        seal = {"version": 1, "skill_id": self.skill_id, "artifacts": native, "images": self.inspected_images,
                "execution_ledger_hash": self._fingerprint(self.work_dir / "native-execution-ledger.json")}
        (self.work_dir / "host-native-seal.json").write_text(json.dumps(seal, indent=2))
        self.sealed = True
        return {"sealed": True, "artifact_count": len(native)}

    def verify_native_seal(self) -> None:
        seal_path = self.work_dir / "host-native-seal.json"
        if not seal_path.is_file():
            raise PipelineError("Native artifacts lack a host-verified immutable seal")
        seal = json.loads(seal_path.read_text())
        if seal.get("skill_id") != self.skill_id:
            raise PipelineError("Native seal belongs to a different skill")
        for relative, digest in seal["artifacts"].items():
            if self._fingerprint(task_path(self.work_dir, relative, must_exist=True)) != digest:
                raise PipelineError("Sealed native artifact integrity check failed: " + relative)
        if self._fingerprint(self.work_dir / "native-execution-ledger.json") != seal["execution_ledger_hash"]:
            raise PipelineError("Native execution ledger integrity check failed")
        self.sealed = True

    def _cancel_check(self) -> None:
        if self.cancelled.is_set():
            raise TaskCancelled("Task was cancelled or its lease was lost")

    def _consume_tool_call(self) -> None:
        """Count each inbound RPC once, including internal runner operations."""
        with self.state_lock:
            self._cancel_check()
            self.tool_calls += 1
            if self.tool_calls > self.max_tool_calls:
                raise BudgetExhausted(f"Tool call budget exhausted ({self.max_tool_calls} calls)")

    def call(self, name: str, args: dict) -> Any:
        with self.state_lock:
            return self._call(name, args)

    def _call(self, name: str, args: dict) -> Any:
        self._consume_tool_call()
        if self.sealed:
            self.verify_native_seal()
        if not isinstance(args, dict):
            raise PipelineError("Tool arguments must be an object")
        methods = {"prepare_source": self.library.prepare_source, "read_source": self.library.read_source,
                   "search_papers": self.search_papers, "fetch_related": self.fetch_related,
                   "read_skill_file": self.read_skill_file, "run_skill_step": self.run_skill_step, "read_native_contract": self.read_native_contract,
                   "list_artifacts": self.list_artifacts, "read_artifact": self.read_artifact,
                   "write_artifact": self.write_artifact, "patch_artifact": self.patch_artifact}
        # The worker owns phase allowlists; this adapter cannot select a phase
        # or accept figure-review records on the model's behalf.
        if self.library.workflow_mode == "guided":
            methods["read_visual"] = self.read_visual
        if name not in methods:
            raise PipelineError("Unknown paper tool")
        try:
            return methods[name](**args)
        except TypeError as exc:
            raise PipelineError("Invalid tool arguments") from exc

    def read_native_contract(self) -> dict:
        path = task_path(self.work_dir, "native/bundle.json", must_exist=True)
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise PipelineError("Native synthesis bundle exceeds the size limit")
        payload = json.loads(path.read_text())
        self.inspected_artifacts.add("native/bundle.json")
        return {key: payload.get(key) for key in ("paper_id", "output_language", "coverage", "source_index", "writing_contract")}

    def read_skill_file(self, path: str) -> dict:
        file = task_path(self.skill_root, path, must_exist=True)
        if file.stat().st_size > MAX_ARTIFACT_BYTES or file.suffix.lower() not in TEXT_EXTENSIONS | {".py", ".ts", ".js"}:
            raise PipelineError("Skill file type or size is not readable")
        relative = str(file.relative_to(self.skill_root))
        self.inspected_skill_files.add(relative)
        return {"path": relative, "content": file.read_text()}

    def list_artifacts(self) -> dict:
        entries = []
        for path in sorted(self.work_dir.rglob("*")):
            if path.is_symlink():
                raise PipelineError("Symbolic-link artifact rejected")
            if path.is_file():
                entries.append({"name": path.name, "path": str(path.relative_to(self.work_dir)),
                                "size": path.stat().st_size, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"})
        if len(entries) > 1000 or sum(e["size"] for e in entries) > MAX_TOTAL_ARTIFACT_BYTES:
            raise PipelineError("Task artifact quota exceeded")
        return {"files": entries}

    def read_artifact(self, path: str, offset: int = 0, limit: int = 20000) -> dict:
        file = task_path(self.work_dir, path, must_exist=True)
        if file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            if not self.vision:
                raise PipelineError("Visual inspection requires a configured vision model")
            if file.stat().st_size > 5 * 1024 * 1024:
                raise PipelineError("Image exceeds the vision payload limit")
            with self.state_lock:
                data = file.read_bytes()
                relative = str(file.relative_to(self.work_dir))
                digest = hashlib.sha256(data).hexdigest()
                self.inspected_artifacts.add(relative)
                self.inspected_images[relative] = digest
                self._atomic_text(self.work_dir / "host-image-inspections.json", json.dumps(self.inspected_images, indent=2))
                return {"path": relative, "data": base64.b64encode(data).decode(), "sha256": digest,
                        "mime_type": mimetypes.guess_type(file.name)[0], "type": "image"}
        if file.suffix.lower() not in TEXT_EXTENSIONS:
            raise PipelineError("Use source tools to read PDF; only text artifacts can be read here")
        if file.stat().st_size > MAX_TOTAL_ARTIFACT_BYTES:
            raise PipelineError("Artifact is too large")
        text = file.read_text(errors="replace")
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 40000))
        relative = str(file.relative_to(self.work_dir))
        self.inspected_artifacts.add(relative)
        return {"path": relative, "content": text[offset:offset + limit], "total_characters": len(text),
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "next_offset": offset + limit if offset + limit < len(text) else None}

    def read_visual(self, figure_id: str) -> dict:
        """Return actual image content with the prepared main-source identity."""
        with self.state_lock:
            self._cancel_check()
            if not isinstance(figure_id, str) or not figure_id.strip() or len(figure_id) > 200:
                raise PipelineError("Invalid figure identifier")
            if not self.vision:
                raise PipelineError("Visual inspection requires a configured vision model")
            metadata = self.library.prepare_visual(figure_id)
            prepared = self.library.prepared_visuals.get(figure_id)
            if (not isinstance(metadata, dict) or metadata != prepared or metadata.get("figure_id") != figure_id
                    or metadata.get("source_id") != "main" or not isinstance(metadata.get("section_id"), str)
                    or not isinstance(metadata.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])):
                raise PipelineError("Prepared visual identity is invalid")
            main = self.library.documents.get("main")
            sections = [section for section in main.sections if section.get("id") == metadata["section_id"]] if main else []
            if len(sections) != 1 or ("page" in metadata and metadata["page"] != sections[0].get("page")):
                raise PipelineError("Prepared visual section/page does not match the main source")
            self._cancel_check()
            image = self.read_artifact(metadata["path"])
            if (image.get("type") != "image" or image.get("path") != metadata["path"]
                    or image.get("sha256") != metadata["sha256"] or image.get("mime_type") != metadata.get("mime_type")
                    or self.inspected_images.get(image["path"]) != metadata["sha256"]):
                raise PipelineError("Prepared visual does not match the actual image read")
            self._cancel_check()
            return {**image, **{key: metadata[key] for key in ("figure_id", "source_id", "section_id", "caption", "page") if key in metadata}}

    def _model_write_path(self, path: str) -> Path:
        file = task_path(self.work_dir, path)
        relative = str(file.relative_to(self.work_dir))
        if relative in self._reserved_paths() or any(part.startswith("host-") or part in {"reading-packet.json", "source-assets"} for part in Path(relative).parts):
            raise PipelineError("Platform-owned artifact cannot be overwritten")
        if self.sealed and not relative.startswith("product/"):
            raise PipelineError("Native/source artifacts are sealed; product writes must stay under product/")
        return file

    @staticmethod
    def _atomic_text(file: Path, content: str) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=file.parent,
                                             prefix="." + file.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(file)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def write_artifact(self, path: str, content: str) -> dict:
        file = self._model_write_path(path)
        if file.suffix.lower() not in TEXT_EXTENSIONS or not isinstance(content, str) or len(content.encode()) > MAX_ARTIFACT_BYTES:
            raise PipelineError("Artifact must be bounded text or JSON")
        if file.suffix.lower() == ".json":
            try:
                json.loads(content, parse_constant=_reject_json_constant)
            except ValueError as exc:
                raise PipelineError("JSON artifact must be complete and valid; existing file was preserved: " + str(exc)) from exc
        self._protect_reader_record(file, content)
        self._atomic_text(file, content)
        self.list_artifacts()
        return {"path": str(file.relative_to(self.work_dir)), "bytes": file.stat().st_size,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}

    def patch_artifact(self, path: str, expected_sha256: str, edits: list[dict]) -> dict:
        file = self._model_write_path(path)
        if file.suffix.lower() not in TEXT_EXTENSIONS or not file.is_file() or file.stat().st_size > MAX_ARTIFACT_BYTES:
            raise PipelineError("Patch requires a bounded existing text artifact")
        before = file.read_bytes()
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or hashlib.sha256(before).hexdigest() != expected_sha256:
            raise PipelineError("Artifact changed since it was read; patch hash does not match")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise PipelineError("Patch requires 1–50 exact edits")
        text = before.decode("utf-8")
        replacements = []
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"old_text", "new_text"} or not all(isinstance(value, str) for value in edit.values()):
                raise PipelineError("Patch edits require only old_text and new_text")
            old, new = edit["old_text"], edit["new_text"]
            start = text.find(old) if old else -1
            if start < 0 or len(new.encode()) > MAX_ARTIFACT_BYTES or text.find(old, start + 1) >= 0:
                raise PipelineError("Patch old_text must match exactly once in the frozen artifact")
            replacements.append((start, start + len(old), new))
        replacements.sort()
        if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:])):
            raise PipelineError("Patch edits overlap in the frozen artifact")
        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
        if file.read_bytes() != before:
            raise PipelineError("Artifact changed during patch preparation")
        return self.write_artifact(path, text)

    def _validate_embedded_paths(self, value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                self._validate_embedded_paths(child_value, child_key)
        elif isinstance(value, list):
            for child in value:
                self._validate_embedded_paths(child, key)
        elif isinstance(value, str) and value:
            if value.startswith(("http://", "https://")):
                return  # The OS denies network regardless of embedded URLs.
            if (value.startswith(("/", "~")) or ".." in Path(value).parts or key.endswith(("_path", "_file", "_dir")) or key in {"path", "file", "directory"}):
                task_path(self.work_dir, value)

    def run_skill_step(self, script: str, args: list[str]) -> dict:
        self._cancel_check()
        if self.sealed:
            raise PipelineError("Native artifacts are sealed; scripts cannot run during product writing")
        name = Path(script).name
        before = self._fingerprints()
        input_paths: set[str] = set()
        output_paths: set[str] = set()
        if self.skill_id == "ljg-paper" and name in {"validate-ljg.js", "validate", "validate_note.ts"}:
            if not isinstance(args, list) or len(args) != 3 or args[1] != "--map":
                raise PipelineError("ljg validator requires a note and --map path")
            path = task_path(self.work_dir, args[0], must_exist=True)
            map_path = task_path(self.work_dir, args[2], must_exist=True)
            input_paths.update([str(path.relative_to(self.work_dir)), str(map_path.relative_to(self.work_dir))])
            command = ["node", str(self.project_dir / "agent-runner" / "dist" / "validate-ljg.js"), str(path), "--map", str(map_path)]
            sandbox_root = str(self.project_dir / "agent-runner")
        else:
            if self.skill_id != "deeppapernote" or name not in SCRIPT_FLAGS or script not in {name, "scripts/" + name}:
                raise PipelineError("Script is not in the selected skill allowlist")
            if not isinstance(args, list) or len(args) > 40 or len(args) % 2:
                raise PipelineError("Native scripts require explicit flag/value argument pairs")
            flags = {}
            for flag, value in zip(args[::2], args[1::2]):
                if flag not in SCRIPT_FLAGS[name] or not isinstance(value, str) or len(value) > 1024 or flag in flags:
                    raise PipelineError("Script argument is not allowed")
                flags[flag] = value
                if flag in SCALAR_FLAGS:
                    if flag == "--zotero-mode" and value != "off":
                        raise PipelineError("Zotero probing must be disabled")
                    if flag == "--save-mode" and value != "workspace":
                        raise PipelineError("Native output must use workspace mode")
                    if flag == "--language" and value not in {"zh-CN", "en"}:
                        raise PipelineError("Unsupported native output language")
                    if flag.startswith(("--max-", "--min-", "--ocr-", "--figure-dpi")) and (not value.isdigit() or not 0 <= int(value) <= 1000):
                        raise PipelineError("Native resource bound is invalid")
                else:
                    path = task_path(self.work_dir, value)
                    relative = str(path.relative_to(self.work_dir))
                    if flag in {"--output", "--raw-sections-output", "--full-text-output", "--trace-output", "--assets-dir", "--dest-dir"}:
                        if "source-assets" in Path(relative).parts:
                            raise PipelineError("Source assets are host-owned and cannot be native script outputs")
                        if not relative.startswith("native/"):
                            raise PipelineError("Native generated files must be under native/")
                        if flag not in {"--assets-dir", "--dest-dir"}:
                            output_paths.add(relative)
                    elif path.is_file():
                        input_paths.add(relative)
                    if path.suffix == ".json" and path.is_file():
                        if path.stat().st_size > MAX_ARTIFACT_BYTES:
                            raise PipelineError("Native input artifact is too large")
                        self._validate_embedded_paths(json.loads(path.read_text()))
            if name == "resolve_paper.py":
                if flags.get("--zotero-mode") != "off" or flags.get("--input") != "source.pdf":
                    raise PipelineError("Resolve must use verified source.pdf with Zotero disabled")
            script_path = task_path(self.skill_root, "scripts/" + name, must_exist=True)
            protected = self._reserved_paths()
            for entry in self.native_checks:
                if entry.get("script") == name:
                    protected.difference_update(entry.get("output_hashes", {}))
            # Only the actual checker can write its result, including first run.
            if name == "lint_grounding.py": protected.discard("native/grounding.json")
            if name == "lint_note.py": protected.discard("native/lint.json")
            command = [sys.executable, "-c", PYTHON_FILE_GUARD, str(self.work_dir), str(self.skill_root), str(script_path),
                       json.dumps([str(self.work_dir / value) for value in protected]), *args]
            sandbox_root = str(self.skill_root)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "DEEPPAPERNOTE_DISABLE_SHELL_CONFIG": "1", "DEEPPAPERNOTE_SAVE_MODE": "workspace",
               "DEEPPAPERNOTE_OUTPUT_LANGUAGE": "zh-CN", "DEEPPAPERNOTE_CONFIG_PATH": str(self.work_dir / "native-config.json"),
               "TMPDIR": str(self.work_dir / ".tmp"), "XDG_CACHE_HOME": str(self.work_dir / ".cache")}
        (self.work_dir / ".tmp").mkdir(exist_ok=True)
        (self.work_dir / ".cache").mkdir(exist_ok=True)
        started = time.monotonic()
        try:
            process = subprocess.Popen(command, cwd=self.work_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, preexec_fn=lambda: _sandbox(str(self.work_dir), sandbox_root))
        except (OSError, subprocess.SubprocessError) as exc:
            raise PipelineError("Native script sandbox could not start") from exc
        try:
            while True:
                self._cancel_check()
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started > 180:
                        raise PipelineError("Native script exceeded the 180-second timeout")
            if process.returncode == 126:
                raise PipelineError("Native script sandbox unavailable; Linux libseccomp is required")
            stdout_text, stderr_text = stdout.decode(errors="replace"), stderr.decode(errors="replace")
            after = self._fingerprints()
            produced = {relative: digest for relative, digest in after.items() if before.get(relative) != digest or relative in output_paths}
            # Preserve complete diagnostics outside model context. These are
            # host-owned, hash-attested artifacts and remain readable in chunks.
            log_id = f"{len(self.native_checks) + 1:04d}-{uuid4().hex[:10]}"
            log_hashes = {}
            log_paths = {}
            for channel, body in (("stdout", stdout), ("stderr", stderr)):
                relative = f"host-step-logs/{log_id}.{channel}.txt"
                path = task_path(self.work_dir, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                log_paths[channel] = relative
                log_hashes[relative] = self._fingerprint(path)
            inventory = self.list_artifacts()["files"]
            changed_files = [item for item in inventory if item["path"] in produced]
            result = {"script": name, "stdout": stdout_text[:2000], "stderr": stderr_text[-4000:],
                      "stdout_truncated": len(stdout_text) > 2000, "stderr_truncated": len(stderr_text) > 4000,
                      "stdout_characters": len(stdout_text), "stderr_characters": len(stderr_text),
                      "stdout_log": log_paths["stdout"], "stderr_log": log_paths["stderr"],
                      "exit_code": process.returncode, "files": changed_files[:32],
                      "changed_file_count": len(changed_files), "files_truncated": len(changed_files) > 32}
            semantic_pass = process.returncode == 0
            checks = []
            for relative in output_paths:
                path = task_path(self.work_dir, relative)
                if path.is_file() and path.suffix == ".json":
                    try:
                        payload = json.loads(path.read_text())
                        if isinstance(payload, dict):
                            checks.append((relative, payload))
                    except ValueError:
                        semantic_pass = False
            # Parse the complete stream before bounding the model-facing preview.
            try:
                stdout_payload = json.loads(stdout_text)
                if isinstance(stdout_payload, dict):
                    checks.append((log_paths["stdout"], stdout_payload))
            except ValueError:
                stdout_payload = None
            if name == "validate_note.ts":
                semantic_pass = semantic_pass and isinstance(stdout_payload, dict) and stdout_payload.get("ok") is True
            summaries = []
            for check_path, check in checks:
                flags = {key: value is True for key, value in check.items() if key.startswith("passes_")}
                if check.get("status") in {"error", "failed", "blocked"} or check.get("run_status") in {"error", "failed", "blocked"}:
                    semantic_pass = False
                if flags and not all(flags.values()):
                    semantic_pass = False
                messages = check.get("errors") or check.get("issues") or []
                if not isinstance(messages, list):
                    messages = [messages]
                if flags or messages or name == "validate_note.ts":
                    summaries.append({"path": check_path, "status": str(check.get("status", ""))[:100],
                                      "run_status": str(check.get("run_status", ""))[:100], "flags": flags,
                                      "ok": check.get("ok") if isinstance(check.get("ok"), bool) else None,
                                      "messages": [str(value)[:300] for value in messages[:5]], "message_count": len(messages)})
            if name in {"lint_grounding.py", "lint_note.py"} and not any(any(key.startswith("passes_") for key in check) for _, check in checks):
                semantic_pass = False
            entry = {"script": name, "exit_code": process.returncode, "semantic_pass": semantic_pass,
                     "elapsed_seconds": round(time.monotonic() - started, 2), "args": args,
                     "input_hashes": {relative: before[relative] for relative in input_paths if relative in before},
                     "output_hashes": produced, "log_hashes": log_hashes}
            result["semantic_pass"] = semantic_pass
            result["checks"] = summaries[:4]
            self.native_checks.append(entry)
            (self.work_dir / "native-execution-ledger.json").write_text(json.dumps(self.native_checks, indent=2))
            return result
        finally:
            terminate_group(process)

    def search_papers(self, query: str) -> dict:
        from sqlalchemy import select
        from .db import SessionLocal
        from .models import Paper
        result = self.library.search_papers(query)
        with SessionLocal() as db:
            papers = db.scalars(select(Paper).where(Paper.workspace_id == self.workspace_id,
                                                    Paper.id != self.library.paper["id"],
                                                    Paper.title.ilike("%" + query[:150] + "%")).limit(6)).all()
            for paper in papers:
                if self.library.is_main_source("workspace:" + paper.id, paper.source_url, paper.sha256):
                    continue
                entry = {"id": "workspace:" + paper.id, "title": paper.title, "url": paper.source_url,
                         "source": "workspace", "abstract": (paper.metadata_json or {}).get("abstract", "")}
                result["candidates"].append(entry)
                self.library.candidates[entry["id"]] = entry
                record = self.library.search_records.get(str(query).strip())
                if record is not None and entry["id"] not in record["candidate_ids"]:
                    record["candidate_ids"].append(entry["id"])
        self.library._save()
        return result

    def fetch_related(self, id: str, url: str | None = None) -> dict:
        if self.library.is_main_source(id, url):
            raise PipelineError("The main paper cannot be used as an external source")
        if not id.startswith("workspace:"):
            return self.library.fetch_related(id=id, url=url)
        from .db import SessionLocal
        from .models import Paper
        from .config import get_settings
        from sqlalchemy import select
        identity = "related:" + id
        if identity in self.library.documents:
            document = self.library.documents[identity]
            if self.library.is_main_source(document.id, document.url, document.sha256):
                raise PipelineError("The main paper or an identical copy cannot be used as an external source")
            return {"source_manifest": document.manifest()}
        if sum(d.kind == "external" for d in self.library.documents.values()) >= 3:
            raise PipelineError("At most three related papers can be read in one task")
        with SessionLocal() as db:
            paper = db.scalar(select(Paper).where(Paper.id == id.removeprefix("workspace:"), Paper.workspace_id == self.workspace_id))
            if paper is None:
                raise PipelineError("Related paper is unavailable in this workspace")
            if self.library.is_main_source(id, paper.source_url, paper.sha256):
                raise PipelineError("The main paper or an identical copy cannot be used as an external source")
            if paper.source_type == "arxiv":
                from .source_tools import normalize_arxiv
                canonical_id = "arxiv:" + normalize_arxiv(paper.source_url)
                # The tenant record and public arXiv ID name the same checked
                # paper. Retain that host-resolved alias in query provenance.
                self.library.candidates.setdefault(canonical_id, {"id": canonical_id, "title": paper.title, "url": paper.source_url})
                for record in self.library.search_records.values():
                    if id in record["candidate_ids"] and canonical_id not in record["candidate_ids"]:
                        record["candidate_ids"].append(canonical_id)
                self.library._save()
                return self.library.fetch_related(canonical_id)
            tenant_root = get_settings().runtime_dir / "storage" / "workspaces" / self.workspace_id
            source = task_path(tenant_root, str(get_settings().runtime_dir / paper.storage_path), must_exist=True)
            path = self.work_dir / ("related-" + paper.id + ".pdf")
            path.write_bytes(source.read_bytes())
            document = self.library._add(identity, paper.title, paper.source_url, path, "external")
            return {"source_manifest": document.manifest(), "sections": section_preview(document.sections, 2, 16000), "coverage": document.coverage}
