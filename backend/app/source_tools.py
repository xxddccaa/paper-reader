"""Public-network source acquisition and bounded, reproducible document parsing.

Every connection uses the very IP addresses checked by the SSRF policy, including
redirects. Source text is retained; model-facing windows never truncate storage.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from functools import wraps
from pathlib import Path
from typing import Any
from .source_network import SourceNetwork, load_source_network

MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 32 * 1024 * 1024
USER_AGENT = "PaperReader/1.0 (research document reader)"
SOURCE_RULES_VERSION = "source-sections-v2"
STRUCTURED_SOURCE_RULES_VERSION = "source-structured-v3"
ARXIV_PATTERN = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)


class SourceError(ValueError):
    """An actionable source error safe to show without remote response bodies."""


def normalize_arxiv(value: str) -> str:
    value = value.strip()
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(value)
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise SourceError("Use an arXiv identifier or an arxiv.org paper URL")
        value = re.sub(r"^/(?:abs|pdf|html)/", "", parsed.path).removesuffix(".pdf")
    value = value.removeprefix("arxiv:").removeprefix("arXiv:")
    if not ARXIV_PATTERN.fullmatch(value):
        raise SourceError("Invalid arXiv identifier")
    return value


def validate_public_url(url: str) -> tuple[str, int, list[str]]:
    """Return a validated host, port and pinned public addresses, never credentials."""
    if len(url) > 4096 or any(ord(c) < 32 for c in url) or "\\" in url:
        raise SourceError("Invalid source URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SourceError("Invalid source URL") from exc
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise SourceError("Source URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or port not in {80, 443}:
        raise SourceError("Source URL credentials and nonstandard ports are not allowed")
    host = parsed.hostname.rstrip(".")
    if "%" in host or host.lower() in {"localhost", "localhost.localdomain"}:
        raise SourceError("Private source addresses are not allowed")
    try:
        host = host.encode("idna").decode("ascii")
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except (OSError, UnicodeError) as exc:
        raise SourceError("Could not resolve source host") from exc
    if not addresses:
        raise SourceError("Could not resolve source host")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise SourceError("Private source addresses are not allowed")
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped and not ip.ipv4_mapped.is_global:
                raise SourceError("Private source addresses are not allowed")
            if ip.sixtofour and not ip.sixtofour.is_global:
                raise SourceError("Private source addresses are not allowed")
            if ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
                raise SourceError("IPv6 transition source addresses are not allowed")
    return host, port, addresses


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self) -> None:
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


class _ProxyTunnel(http.client.HTTPConnection):
    """CONNECT to the validated target IP; preserve origin Host and TLS SNI."""
    def __init__(self, host: str, port: int, address: str, timeout: float, proxy_url: str, secure: bool):
        super().__init__(host, port, timeout=timeout)
        proxy = urllib.parse.urlsplit(proxy_url)
        self.proxy_address = (proxy.hostname, proxy.port or 80)
        self.secure = secure
        # The proxy must not resolve the untrusted source hostname a second time.
        self.set_tunnel(address, port)

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection(self.proxy_address, self.timeout)
            self._tunnel()
            if self.secure:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise


@dataclass
class Download:
    data: bytes
    url: str
    content_type: str


def download_public(url: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES, timeout: float | None = None,
                    network: SourceNetwork | None = None) -> Download:
    """Stream a bounded body with validated redirects and DNS-rebinding resistance."""
    try:
        network = network if network is not None else load_source_network()
    except ValueError as exc:
        raise SourceError(str(exc)) from None
    timeout = network.timeout_seconds if timeout is None else timeout
    deadline = time.monotonic() + timeout
    current = url
    for _ in range(5):
        host, port, addresses = validate_public_url(current)
        parsed = urllib.parse.urlsplit(current)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("Source download timed out")
        if network.proxy_enabled:
            conn = _ProxyTunnel(host, port, addresses[0], min(remaining, 15), network.proxy_url, parsed.scheme == "https")
        else:
            conn_type = _PinnedHTTPS if parsed.scheme == "https" else _PinnedHTTP
            conn = conn_type(host, port, addresses[0], min(remaining, 15))
        try:
            authority = f"[{host}]" if ":" in host else host
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Host": f"{authority}:{port}"})
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise SourceError("Source redirect has no destination")
                target = urllib.parse.urljoin(current, location)
                if parsed.scheme == "https" and urllib.parse.urlsplit(target).scheme != "https":
                    raise SourceError("Insecure source redirect rejected")
                current = target
                continue
            if response.status != 200:
                raise SourceError(f"Source returned HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise SourceError("Unexpected compressed source response")
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > max_bytes):
                raise SourceError("Source exceeds the download size limit")
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("Source download timed out")
                if conn.sock:
                    conn.sock.settimeout(min(remaining, 15))
                chunk = response.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SourceError("Source exceeds the download size limit")
                chunks.append(chunk)
            return Download(b"".join(chunks), current, response.getheader("Content-Type", "").split(";", 1)[0])
        except SourceError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            message = "Source proxy connection failed or timed out; check Platform settings > Network proxy" if network.proxy_enabled else "Source download failed or timed out"
            raise SourceError(message) from None
        finally:
            conn.close()
    raise SourceError("Too many source redirects")


class _PaperHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0
        self.head = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "footer"}:
            self.suppressed += 1
        if tag == "head":
            self.head += 1
        if tag in {"p", "div", "section", "h1", "h2", "h3", "h4", "tr", "li", "br"}:
            self.parts.append("\n")
        if tag == "img" and not self.suppressed:
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(" " + alt + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer"}:
            self.suppressed = max(0, self.suppressed - 1)
        if tag == "head":
            self.head = max(0, self.head - 1)
        if tag in {"p", "div", "section", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed and not self.head:
            self.parts.append(data)


def parse_html(data: bytes) -> str:
    parser = _PaperHTML()
    parser.feed(data.decode("utf-8", errors="replace"))
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_pdf(path: Path) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Retain page identities without claiming OCR or table/figure verification."""
    import fitz

    sections = []
    warnings = ["PDF figures and table structure require native or visual verification"]
    try:
        with fitz.open(path) as document:
            if document.needs_pass:
                raise SourceError("Encrypted PDF requires an unlocked copy")
            if len(document) > 1000:
                raise SourceError("PDF exceeds the 1000-page limit")
            total = 0
            for index, page in enumerate(document):
                text = page.get_text("text")
                total += len(text.encode("utf-8"))
                if total > MAX_TEXT_BYTES:
                    raise SourceError("Extracted PDF text exceeds the size limit")
                sections.append({"id": f"p{index + 1}", "page": index + 1, "text": text,
                                 "image_count": len(page.get_images()), "text_characters": len(text.strip())})
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("PDF could not be parsed") from exc
    if not any(s["text"].strip() for s in sections):
        warnings.append("PDF is image-only; configured OCR is required")
    return sections, "pymupdf-page-text", warnings


def parse_docling(path: Path) -> tuple[list[dict], str]:
    """Use only explicitly provisioned local Docling layout models."""
    from .config import get_settings
    settings = get_settings()
    artifacts = Path(os.environ.get("PAPER_READER_DOCLING_ARTIFACTS_PATH", settings.runtime_dir / "cache" / "docling" / "models"))
    if not artifacts.is_dir() or not any(artifacts.iterdir()):
        raise SourceError("Docling models have not been prepared by the deployer")
    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
    except ImportError as exc:
        raise SourceError("Docling optional dependency is not installed") from exc
    ocr_enabled = os.environ.get("PAPER_READER_DOCLING_OCR_ENABLED", "").lower() in {"1", "true", "yes"}
    options = PdfPipelineOptions(artifacts_path=artifacts, enable_remote_services=False, do_ocr=ocr_enabled)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    # Offline by default, including model dependencies. No first-user downloads.
    previous_offline = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        converted = converter.convert(path)
        sections = []
        total = 0
        for item, _ in converted.document.iterate_items():
            text = getattr(item, "text", "")
            if not text:
                continue
            total += len(text.encode())
            if total > MAX_TEXT_BYTES:
                raise SourceError("Docling output exceeds the text size limit")
            provenance = getattr(item, "prov", [])
            entry = {"id": "d" + str(len(sections) + 1), "text": text}
            if provenance:
                entry["page"] = provenance[0].page_no
            sections.append(entry)
        if not sections:
            raise SourceError("Docling returned no text")
        return sections, "docling-layout-ocr" if ocr_enabled else "docling-layout-no-ocr"
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("Docling conversion failed; inspect deployment model availability") from exc
    finally:
        if previous_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous_offline

def parse_document(path: Path, source_id: str, *, prefer_docling: bool = False) -> tuple[list[dict], dict]:
    raw = path.read_bytes()
    page_count = 0
    missing_pages: list[int] = []
    if raw.startswith(b"%PDF-"):
        # Extract a page inventory first, but let provisioned Docling/OCR recover
        # empty page text before deciding that an image-only paper is unusable.
        fallback, parser, warnings = parse_pdf(path)
        sections = fallback
        page_count = len(fallback)
        if prefer_docling:
            if os.environ.get("PAPER_READER_DOCLING_ENABLED", "").lower() in {"1", "true", "yes"}:
                try:
                    sections, parser = parse_docling(path)
                except SourceError as exc:
                    warnings.append(str(exc) + "; page-text fallback used")
            else:
                warnings.append("Docling layout/OCR model is not enabled; page-text fallback used")
        text_per_page = {number: 0 for number in range(1, page_count + 1)}
        for section in sections:
            if section.get("page") in text_per_page:
                text_per_page[section["page"]] += len(section["text"].strip())
        missing_pages = [page["page"] for page in fallback
                         if text_per_page[page["page"]] < 80 and (page.get("image_count", 0) > 0 or text_per_page[page["page"]] == 0)]
        if not any(section["text"].strip() for section in sections):
            raise SourceError("PDF is image-only and no usable OCR text is available; complete reading is unsupported")
        if missing_pages:
            warnings.append("Pages without reliable text: " + ", ".join(str(number) for number in missing_pages))
    else:
        text = parse_html(raw)
        if len(text) < 800:
            raise SourceError("HTML full text is unavailable or too short for a complete reading")
        sections = [{"id": f"s{i // 12000 + 1}", "text": text[i:i + 12000]} for i in range(0, len(text), 12000)]
        parser, warnings = "html-text", ["Figures require separate visual verification"]
    for section in sections:
        section["source_id"] = source_id
    corpus = "\n\n".join(s["text"] for s in sections).lower()
    coverage = {
        "full_text": not missing_pages, "usable_text": bool(corpus.strip()),
        "status": "partial" if missing_pages else "complete", "parser": parser, "characters": len(corpus),
        "page_count": page_count or None, "missing_text_pages": missing_pages,
        "text_page_ratio": (page_count - len(missing_pages)) / page_count if page_count else None,
        "abstract": "abstract" in corpus,
        "related_work": any(s in corpus for s in ("related work", "background")),
        "method": any(s in corpus for s in ("method", "approach", "algorithm")),
        "results": any(s in corpus for s in ("result", "experiment", "evaluation")),
        "warnings": warnings,
    }
    return sections, coverage


def section_preview(sections: list[dict], count: int = 4, max_characters: int = 24000) -> list[dict]:
    result = []
    remaining = max_characters
    for section in sections[:count]:
        if remaining <= 0:
            break
        text = section["text"]
        excerpt = text[:remaining]
        result.append({**section, "text": excerpt, "total_characters": len(text), "truncated": len(excerpt) < len(text)})
        remaining -= len(excerpt)
    return result


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call

@dataclass
class SourceDocument:
    id: str
    title: str
    url: str | None
    path: str
    sections: list[dict]
    coverage: dict
    sha256: str
    kind: str = "paper"

    def manifest(self) -> dict:
        return {"source_id": self.id, "title": self.title, "url": self.url, "path": self.path,
                "sha256": self.sha256, "kind": self.kind, "coverage": self.coverage,
                "section_count": len(self.sections)}


@dataclass
class SourceLibrary:
    work_dir: Path
    paper: dict
    skill_id: str
    max_related: int = 3
    documents: dict[str, SourceDocument] = field(default_factory=dict)
    candidates: dict[str, dict] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    searches: int = 0
    query_log: list[str] = field(default_factory=list)
    search_records: dict[str, dict] = field(default_factory=dict)
    workflow_mode: str = field(default="native", kw_only=True)
    rules_version: str = field(default=SOURCE_RULES_VERSION, kw_only=True)
    recover_invalid_cache: bool = field(default=False, kw_only=True)
    visual_observations: dict[str, dict] = field(default_factory=dict, kw_only=True)
    prepared_visuals: dict[str, dict] = field(default_factory=dict, kw_only=True)
    visual_assets: dict[str, dict] = field(default_factory=dict, kw_only=True)
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.rules_version not in {SOURCE_RULES_VERSION, STRUCTURED_SOURCE_RULES_VERSION}:
            raise SourceError("Unsupported source parsing rules version")
        if self.workflow_mode not in {"native", "guided"}:
            raise SourceError("Unsupported source workflow mode")
        self.work_dir = self.work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        # Resumed attempts get this trusted checkpoint copied by the worker.
        checkpoint = self.work_dir / "source_corpus.json"
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION and self.recover_invalid_cache:
            try:
                if checkpoint.is_symlink():
                    raise SourceError("Cached corpus is a symbolic link")
                if checkpoint.is_file():
                    self.restore_checkpoint(json.loads(checkpoint.read_text()))
                elif any((self.work_dir / name).exists() for name in ("source.pdf", "source.html", "source-assets")):
                    raise SourceError("Interrupted source materialization has no validated corpus")
            except (SourceError, OSError, ValueError, TypeError, KeyError) as exc:
                # Only disposable, attempt-local copies are moved. The original
                # tenant upload and immutable published revision are untouched.
                rejected = self.work_dir.parent / ("host-rejected-source-" + uuid.uuid4().hex)
                rejected.mkdir(mode=0o700)
                names = ("source.pdf", "source.html", "source_corpus.json", "source_corpus.json.tmp",
                         "source_manifest.json", "host-image-inspections.json", "source-assets")
                moved = []
                for name in names:
                    path = self.work_dir / name
                    if path.exists() or path.is_symlink():
                        path.rename(rejected / name)
                        moved.append(name)
                self.documents.clear()
                self.candidates.clear()
                self.limitations.clear()
                self.query_log.clear()
                self.search_records.clear()
                self.visual_observations.clear()
                self.prepared_visuals.clear()
                self.visual_assets.clear()
                self.searches = 0
                self.cache_recovery = {"reason": str(exc)[:500], "quarantined_files": moved}
            return
        if checkpoint.is_file():
            payload = json.loads(checkpoint.read_text())
            if payload.get("version", SOURCE_RULES_VERSION) != self.rules_version:
                raise SourceError("Source checkpoint belongs to different parsing rules")
            if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION and payload.get("documents"):
                self.restore_checkpoint(payload)
                return
            for value in payload.get("documents", []):
                document = SourceDocument(**value)
                self.documents[document.id] = document
            self.candidates = payload.get("candidates", {})
            self.limitations = payload.get("limitations", [])
            self.searches = int(payload.get("searches", 0))
            self.query_log = payload.get("query_log", [])
            self.search_records = payload.get("search_records", {})
            self.visual_observations = payload.get("visual_observations", {})
            self.prepared_visuals = payload.get("prepared_visuals", {})
            self.visual_assets = payload.get("visual_assets", {})

    @_locked
    def _save(self) -> None:
        payload = {"documents": [vars(d) for d in self.documents.values()], "candidates": self.candidates,
                   "limitations": self.limitations, "searches": self.searches, "query_log": self.query_log,
                   "search_records": self.search_records, "version": self.rules_version,
                   "visual_observations": self.visual_observations, "prepared_visuals": self.prepared_visuals,
                   "visual_assets": self.visual_assets}
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
            payload = self.export_checkpoint()
        temporary = self.work_dir / "source_corpus.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False))
        temporary.replace(self.work_dir / "source_corpus.json")
        (self.work_dir / "source_manifest.json").write_text(json.dumps(
            {"sources": [d.manifest() for d in self.documents.values()], "limitations": self.limitations},
            ensure_ascii=False, indent=2))

    def _checkpoint_file(self, name: str) -> Path:
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise SourceError("Source checkpoint path escapes the task directory")
        path = self.work_dir / name
        if self.work_dir not in path.resolve().parents or any(part.is_symlink() for part in [path, *path.parents] if part != self.work_dir):
            raise SourceError("Source checkpoint path escapes the task directory")
        if not path.is_file() or path.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise SourceError("Source checkpoint file is missing or exceeds its bound")
        return path

    def _source_files(self, documents: list[dict]) -> dict[str, str]:
        expected = {}
        for document in documents:
            entries = [{"path": document["path"], "sha256": document["sha256"]}]
            if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
                entries.extend(document["coverage"].get("representations", []))
            for entry in entries:
                name, digest = entry["path"], entry["sha256"]
                if name in expected and expected[name] != digest:
                    raise SourceError("Source representation hashes disagree")
                expected[name] = digest
        for entry in [*self.visual_assets.values(), *self.prepared_visuals.values()]:
            name, digest = entry["path"], entry["sha256"]
            if name in expected and expected[name] != digest:
                raise SourceError("Source visual hashes disagree")
            expected[name] = digest
        return expected

    @_locked
    def export_checkpoint(self) -> dict:
        """Snapshot payload plus actual source-file hashes; the worker anchors its hash."""
        documents = [vars(self.documents[key]) for key in sorted(self.documents)]
        file_hashes = {name: hashlib.sha256(self._checkpoint_file(name).read_bytes()).hexdigest()
                       for name in self._source_files(documents)}
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION and file_hashes != self._source_files(documents):
            raise SourceError("Immutable source or visual integrity check failed")
        payload = {"version": self.rules_version, "paper_id": self.paper["id"], "skill_id": self.skill_id,
                   "documents": documents, "candidates": self.candidates, "limitations": self.limitations,
                   "searches": self.searches, "query_log": self.query_log, "search_records": self.search_records,
                   "file_sha256": file_hashes}
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION or self.prepared_visuals or self.visual_observations:
            payload.update(visual_observations=self.visual_observations, prepared_visuals=self.prepared_visuals,
                           visual_assets=self.visual_assets)
        return json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))

    @_locked
    def restore_checkpoint(self, payload: dict, expected_sha256: str | None = None) -> None:
        """Validate fully before replacing state; source bytes must match the snapshot."""
        try:
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if len(serialized) > MAX_TEXT_BYTES or (expected_sha256 is not None and hashlib.sha256(serialized).hexdigest() != expected_sha256):
                raise SourceError("Source checkpoint payload hash or size is invalid")
            payload = json.loads(serialized)
            if payload["version"] != self.rules_version or payload["paper_id"] != self.paper["id"] or payload["skill_id"] != self.skill_id:
                raise SourceError("Source checkpoint belongs to different paper, skill or parsing rules")
            if not isinstance(payload["documents"], list) or not 1 <= len(payload["documents"]) <= self.max_related + 1:
                raise SourceError("Source checkpoint document count is invalid")
            restored = {}
            paths = set()
            for raw in payload["documents"]:
                document = SourceDocument(**raw)
                if document.id in restored or not isinstance(document.sections, list) or not document.sections or not isinstance(document.coverage, dict):
                    raise SourceError("Source checkpoint contains invalid or duplicate documents")
                sections = set()
                for section in document.sections:
                    if not isinstance(section, dict) or not isinstance(section.get("id"), str) or not isinstance(section.get("text"), str) or section["id"] in sections:
                        raise SourceError("Source checkpoint sections are invalid")
                    sections.add(section["id"])
                digest = hashlib.sha256(self._checkpoint_file(document.path).read_bytes()).hexdigest()
                if digest != payload["file_sha256"].get(document.path) or digest != document.sha256:
                    raise SourceError("Source checkpoint file integrity check failed")
                paths.add(document.path)
                if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
                    selection = document.coverage.get("source_selection", {})
                    resolved = selection.get("resolved_id")
                    representations = document.coverage.get("representations", [])
                    if not representations or not any(rep.get("path") == document.path and rep.get("sha256") == document.sha256 for rep in representations):
                        raise SourceError("Source checkpoint is missing its original representation")
                    for rep in representations:
                        if rep.get("arxiv_id") != resolved:
                            raise SourceError("Source checkpoint representation version mismatch")
                        actual = hashlib.sha256(self._checkpoint_file(rep["path"]).read_bytes()).hexdigest()
                        if actual != rep["sha256"] or actual != payload["file_sha256"].get(rep["path"]):
                            raise SourceError("Source checkpoint representation integrity check failed")
                        paths.add(rep["path"])
                restored[document.id] = document
            assets = payload.get("visual_assets", {})
            visuals = payload.get("prepared_visuals", {})
            observations = payload.get("visual_observations", {})
            if not all(isinstance(value, dict) for value in (assets, visuals, observations)):
                raise SourceError("Source checkpoint visual state is invalid")
            main = restored.get("main")
            figure_index = {figure["id"]: figure for figure in main.coverage.get("figures", [])} if main else {}
            for name, asset in assets.items():
                if not isinstance(asset, dict) or name != asset.get("path"):
                    raise SourceError("Source checkpoint visual asset is invalid")
                if main and asset.get("arxiv_id") != main.coverage.get("source_selection", {}).get("resolved_id"):
                    raise SourceError("Source checkpoint visual asset version mismatch")
                digest = hashlib.sha256(self._checkpoint_file(name).read_bytes()).hexdigest()
                if digest != asset.get("sha256") or digest != payload["file_sha256"].get(name):
                    raise SourceError("Source checkpoint visual asset integrity check failed")
                paths.add(name)
            for identity, visual in visuals.items():
                if not isinstance(visual, dict) or visual.get("figure_id") != identity or visual.get("source_id") != "main" or visual.get("mime_type") != "image/png":
                    raise SourceError("Source checkpoint visual identity is invalid")
                figure = figure_index.get(identity)
                if not figure or visual.get("section_id") != figure["section_id"]:
                    raise SourceError("Source checkpoint visual section is invalid")
                digest = hashlib.sha256(self._checkpoint_file(visual["path"]).read_bytes()).hexdigest()
                if digest != visual.get("sha256") or digest != payload["file_sha256"].get(visual["path"]):
                    raise SourceError("Source checkpoint visual integrity check failed")
                paths.add(visual["path"])
            for identity, observation in observations.items():
                if not isinstance(observation, dict) or observation.get("id") != identity:
                    raise SourceError("Source checkpoint visual observation is invalid")
                visual = visuals.get(observation.get("figure_id"))
                if not visual or observation.get("image_sha256") != visual["sha256"] or observation.get("section_id") != visual["section_id"]:
                    raise SourceError("Source checkpoint visual observation hash or identity is invalid")
                if type(observation.get("readable")) is not bool or not isinstance(observation.get("observation"), str) or not isinstance(observation.get("conditions"), str):
                    raise SourceError("Source checkpoint visual observation content is invalid")
                if not all(isinstance(observation.get(key), list) and all(isinstance(value, str) for value in observation[key]) for key in ("uncertainties", "claim_ids")):
                    raise SourceError("Source checkpoint visual observation content is invalid")
            if "main" not in restored or set(payload["file_sha256"]) != paths:
                raise SourceError("Source checkpoint is missing its main source or file hashes")
            for document in restored.values():
                if document.kind == "external" and self.is_main_source(document.id, document.url, document.sha256, main_document=restored["main"]):
                    raise SourceError("Source checkpoint incorrectly treats the main paper as external")
            if not isinstance(payload["candidates"], dict) or not isinstance(payload["limitations"], list) or not all(isinstance(value, str) for value in payload["limitations"]):
                raise SourceError("Source checkpoint search state is invalid")
            if type(payload["searches"]) is not int or not 0 <= payload["searches"] <= 1000000 or not isinstance(payload["query_log"], list) or not all(isinstance(value, str) for value in payload["query_log"]):
                raise SourceError("Source checkpoint search state is invalid")
            records = payload.get("search_records", {})
            if not isinstance(records, dict):
                raise SourceError("Source checkpoint search records are invalid")
            for query, record in records.items():
                if not isinstance(query, str) or not query or len(query) > 500 or query not in payload["query_log"] or not isinstance(record, dict) or set(record) != {"candidate_ids", "errors", "completed"}:
                    raise SourceError("Source checkpoint search record is invalid")
                if not isinstance(record["candidate_ids"], list) or not all(isinstance(value, str) and value in payload["candidates"] for value in record["candidate_ids"]):
                    raise SourceError("Source checkpoint search candidates are invalid")
                if not isinstance(record["errors"], list) or not all(isinstance(value, str) for value in record["errors"]) or type(record["completed"]) is not bool or (record["completed"] and record["errors"]):
                    raise SourceError("Source checkpoint search outcome is invalid")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            if isinstance(exc, SourceError):
                raise
            raise SourceError("Invalid source checkpoint") from exc
        self.documents = restored
        self.candidates = json.loads(json.dumps(payload["candidates"]))
        self.limitations = list(payload["limitations"])
        self.searches = payload["searches"]
        self.query_log = list(payload["query_log"])
        self.search_records = json.loads(json.dumps(records))
        self.visual_observations = json.loads(json.dumps(observations))
        self.prepared_visuals = json.loads(json.dumps(visuals))
        self.visual_assets = json.loads(json.dumps(assets))
        self._save()

    @_locked
    def fingerprint(self) -> str:
        """Semantic source identity includes real bytes and full retained sections."""
        sources = []
        for identity in sorted(self.documents):
            document = self.documents[identity]
            sources.append({"id": identity, "title": document.title, "url": document.url, "kind": document.kind,
                            "sha256": document.sha256, "sections": document.sections, "coverage": document.coverage,
                            "file_sha256": hashlib.sha256(self._checkpoint_file(document.path).read_bytes()).hexdigest()})
        value = {"rules": self.rules_version, "skill_id": self.skill_id, "paper_id": self.paper["id"], "sources": sources}
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
            value["representation_sha256"] = {rep["path"]: hashlib.sha256(self._checkpoint_file(rep["path"]).read_bytes()).hexdigest()
                                               for document in self.documents.values() for rep in document.coverage.get("representations", [])}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @_locked
    def _add(self, source_id: str, title: str, url: str | None, path: Path, kind: str, *, structured: tuple | None = None) -> SourceDocument:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if kind == "external" and self.is_main_source(source_id, url, digest):
            raise SourceError("The main paper or an identical copy cannot be used as an external source")
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
            from .structured_source import parse_structured_html, parse_structured_pdf, representation
            if structured is not None:
                sections, coverage = structured
            elif path.read_bytes().startswith(b"%PDF-"):
                sections, coverage = parse_structured_pdf(path, source_id)
            else:
                sections, coverage = parse_structured_html(path.read_bytes(), source_id, url=url)
            format = "pdf" if path.read_bytes().startswith(b"%PDF-") else "html"
            coverage.setdefault("source_selection", {"format": format, "reason": "Locally supplied source",
                                                      "requested_id": None, "resolved_id": None})
            coverage["representations"] = [representation(path, self.work_dir, format, url, coverage["source_selection"].get("resolved_id"))]
            actual_title = coverage.get("document_title", "").strip()
            placeholder = title.lower() in {"untitled paper", "untitled", ""} or title.lower().endswith(".pdf")
            try:
                normalize_arxiv(title.removeprefix("arXiv "))
                placeholder = True
            except SourceError:
                pass
            if actual_title and placeholder:
                title = actual_title
        else:
            sections, coverage = parse_document(path, source_id, prefer_docling=self.skill_id == "ljg-paper")
        document = SourceDocument(source_id, title, url, str(path.relative_to(self.work_dir)), sections, coverage,
                                  digest, kind)
        self.documents[source_id] = document
        self._save()
        return document

    def is_main_source(self, source_id: str, url: str | None = None, sha256: str | None = None, *,
                       main_document: SourceDocument | None = None) -> bool:
        """Identify the current paper across workspace IDs, arXiv versions and bytes."""
        main = main_document or self.documents.get("main")
        identity = str(source_id).removeprefix("related:")
        if identity in {"main", str(self.paper["id"]), "workspace:" + str(self.paper["id"])}:
            return True
        references = [self.paper.get("source_url"), self.paper.get("id"), main.url if main else None]
        def canonical(value):
            try:
                return re.sub(r"v\d+$", "", normalize_arxiv(value), flags=re.I).casefold() if isinstance(value, str) else None
            except SourceError:
                return None
        main_arxiv = {normalized for value in references if (normalized := canonical(value)) is not None}
        if any(normalized in main_arxiv for value in (identity, url) if (normalized := canonical(value)) is not None):
            return True
        if url and url in {self.paper.get("source_url"), main.url if main else None}:
            return True
        return bool(sha256 and main and (sha256 == main.sha256 or any(
            sha256 == rep.get("sha256") for rep in main.coverage.get("representations", []))))

    @_locked
    def prepare_source(self) -> dict:
        if "main" not in self.documents and self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
            self._prepare_structured_source()
        if "main" not in self.documents:
            source_path = self.paper.get("storage_path")
            title = self.paper.get("title") or "Untitled paper"
            source_url = self.paper.get("source_url")
            if self.paper.get("source_type") == "pdf":
                if not source_path or not Path(source_path).is_file():
                    raise SourceError("Uploaded PDF is unavailable")
                data = Path(source_path).read_bytes()
                if len(data) > MAX_DOWNLOAD_BYTES or not data.startswith(b"%PDF-"):
                    raise SourceError("Uploaded source is not a supported PDF")
                path = self.work_dir / "source.pdf"
                path.write_bytes(data)
                self._add("main", title, source_url, path, "paper")
            else:
                arxiv_id = normalize_arxiv(source_url or self.paper.get("id", ""))
                path = self.work_dir / "source.pdf"
                if self.skill_id == "ljg-paper":
                    try:
                        html = download_public(f"https://arxiv.org/html/{arxiv_id}")
                        path = self.work_dir / "source.html"
                        path.write_bytes(html.data)
                        self._add("main", title, html.url, path, "paper")
                    except SourceError as exc:
                        self.limitations.append(f"arXiv HTML unavailable: {exc}")
                if "main" not in self.documents:
                    pdf = download_public(f"https://arxiv.org/pdf/{arxiv_id}")
                    if not pdf.data.startswith(b"%PDF-"):
                        raise SourceError("arXiv did not return a PDF")
                    path = self.work_dir / "source.pdf"
                    path.write_bytes(pdf.data)
                    self._add("main", title, pdf.url, path, "paper")
        document = self.documents["main"]
        return {"paper_id": self.paper["id"], "title": document.title,
                "source_manifest": document.manifest(), "sections": section_preview(document.sections),
                "section_index": [{k: v for k, v in s.items() if k != "text"} for s in document.sections],
                "files": [{"name": Path(document.path).name, "path": document.path},
                          {"name": "source_manifest.json", "path": "source_manifest.json"}],
                "coverage": document.coverage}

    def _prepare_structured_source(self) -> None:
        from .structured_source import immutable_write, parse_structured_html, parse_structured_pdf, pdf_identity

        title = self.paper.get("title") or "Untitled paper"
        source_url = self.paper.get("source_url")
        if self.paper.get("source_type") == "pdf":
            source_path = self.paper.get("storage_path")
            if not source_path or not Path(source_path).is_file() or Path(source_path).stat().st_size > MAX_DOWNLOAD_BYTES:
                raise SourceError("Uploaded PDF is unavailable or exceeds the size limit")
            data = Path(source_path).read_bytes()
            if not data.startswith(b"%PDF-"):
                raise SourceError("Uploaded source is not a supported PDF")
            path = immutable_write(self.work_dir, "source.pdf", data)
            self._add("main", title, source_url, path, "paper")
            return
        requested = normalize_arxiv(source_url or self.paper.get("id", ""))
        reason = "HTML not available"
        try:
            html = download_public(f"https://arxiv.org/html/{requested}")
            parsed = parse_structured_html(html.data, "main", url=html.url, requested_id=requested)
        except SourceError as exc:
            reason = f"arXiv HTML unavailable: {exc}"
            self.limitations.append(reason)
        else:
            path = immutable_write(self.work_dir, "source.html", html.data)
            self._add("main", title, html.url, path, "paper", structured=parsed)
            return
        pdf = download_public(f"https://arxiv.org/pdf/{requested}")
        resolved = pdf_identity(pdf.data, requested, pdf.url)
        path = immutable_write(self.work_dir, "source.pdf", pdf.data)
        sections, coverage = parse_structured_pdf(path)
        coverage["source_selection"] = {"format": "pdf", "reason": reason + "; validated PDF fallback",
                                        "requested_id": requested, "resolved_id": resolved}
        self._add("main", title, pdf.url, path, "paper", structured=(sections, coverage))

    def _ensure_pdf_representation(self) -> Path:
        from .structured_source import immutable_write, pdf_identity, representation

        document = self.documents["main"]
        for entry in document.coverage.get("representations", []):
            if entry["format"] == "pdf":
                path = self._checkpoint_file(entry["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                    raise SourceError("PDF representation integrity check failed")
                return path
        resolved = document.coverage.get("source_selection", {}).get("resolved_id")
        if not resolved:
            raise SourceError("PDF visual fallback requires a resolved arXiv version")
        pdf = download_public(f"https://arxiv.org/pdf/{resolved}")
        pdf_identity(pdf.data, resolved, pdf.url)
        path = immutable_write(self.work_dir, "source.pdf", pdf.data)
        document.coverage["representations"].append(representation(path, self.work_dir, "pdf", pdf.url, resolved))
        self._save()
        return path

    def _visual_pdf_fallback(self, figure: dict, reason: str) -> bytes:
        from .structured_source import figure_pdf_page, render_pdf_page

        if not re.match(r"\s*(?:Figure|Fig\.|Table)\s+\d+[A-Za-z]?\s*[:.|]\s*\S", figure.get("caption", ""), re.I):
            raise SourceError("PDF fallback cannot identify a caption label for this figure")
        path = self._ensure_pdf_representation()
        page = figure_pdf_page(path, figure.get("caption", ""))
        rendered = render_pdf_page(path, page)
        document = self.documents["main"]
        figure.update(page=page, visual_fallback=reason)
        for section in document.sections:
            if section["id"] == figure["section_id"]:
                section["page"] = page
        document.coverage["warnings"].append(f"{figure['id']}: {reason}; inspecting matching PDF page {page}")
        return rendered

    def _visual_images(self, figure: dict) -> bytes:
        from .structured_source import (MAX_VISUAL_BYTES, combine_images, immutable_write,
                                        render_image, representation)

        resolved = self.documents["main"].coverage.get("source_selection", {}).get("resolved_id")
        images = figure.get("images", [])
        if not images or len(images) > 16:
            raise SourceError("Figure has no supported original images or too many subpanels")
        panels = []
        for image in images:
            url = image["url"]
            if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
                raise SourceError("Figure image must use a public HTTP source")
            versions = re.findall(r"(?:\d{4}\.\d{4,5})v\d+", urllib.parse.unquote(url))
            if resolved and any(value != resolved for value in versions):
                raise SourceError("Figure image arXiv version does not match its paper")
            downloaded = download_public(url, max_bytes=MAX_VISUAL_BYTES)
            versions = re.findall(r"(?:\d{4}\.\d{4,5})v\d+", urllib.parse.unquote(downloaded.url))
            if resolved and any(value != resolved for value in versions):
                raise SourceError("Figure image redirect arXiv version does not match its paper")
            png, format = render_image(downloaded.data, downloaded.content_type)
            digest = hashlib.sha256(downloaded.data).hexdigest()
            original = immutable_write(self.work_dir, f"source-assets/{digest}.{format}", downloaded.data)
            asset = representation(original, self.work_dir, format, downloaded.url, resolved)
            self.visual_assets[asset["path"]] = asset
            panels.append(png)
        return combine_images(panels)

    @_locked
    def prepare_visual(self, figure_id: str) -> dict:
        """Materialize one actual main-source image; observation acceptance is host-owned."""
        from .structured_source import immutable_write, render_pdf_page

        if not isinstance(figure_id, str) or len(figure_id) > 1000:
            raise SourceError("Invalid source figure identity")
        self.prepare_source()
        document = self.documents["main"]
        figure = next((entry for entry in document.coverage.get("figures", []) if entry["id"] == figure_id), None)
        if figure is None:
            raise SourceError("Unknown main-source figure or PDF page")
        from .structured_source import RENDER_RULES_VERSION
        if figure_id in self.prepared_visuals and self.prepared_visuals[figure_id].get("render_rules") != RENDER_RULES_VERSION:
            self.prepared_visuals.pop(figure_id)
            self.visual_observations = {key: value for key, value in self.visual_observations.items() if value.get("figure_id") != figure_id}
        if figure_id in self.prepared_visuals:
            visual = self.prepared_visuals[figure_id]
            if hashlib.sha256(self._checkpoint_file(visual["path"]).read_bytes()).hexdigest() != visual["sha256"]:
                raise SourceError("Immutable visual integrity check failed")
            return dict(visual)
        if figure.get("page"):
            pdf = next((entry for entry in document.coverage.get("representations", []) if entry["format"] == "pdf"), None)
            if pdf is None:
                raise SourceError("No matching PDF representation is available for this visual")
            path = self._checkpoint_file(pdf["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != pdf["sha256"]:
                raise SourceError("PDF representation integrity check failed")
            rendered = render_pdf_page(path, figure["page"])
        else:
            try:
                rendered = self._visual_images(figure)
            except SourceError as exc:
                try:
                    rendered = self._visual_pdf_fallback(figure, str(exc))
                except SourceError as fallback:
                    raise SourceError(f"Figure visual unresolved: {exc}; PDF fallback: {fallback}") from fallback
        digest = hashlib.sha256(rendered).hexdigest()
        path = immutable_write(self.work_dir, f"source-assets/{digest}.png", rendered)
        visual = {"figure_id": figure_id, "source_id": "main", "section_id": figure["section_id"],
                  "caption": figure.get("caption", ""), "path": str(path.relative_to(self.work_dir)),
                  "sha256": digest, "mime_type": "image/png", "render_rules": RENDER_RULES_VERSION}
        if figure.get("page"):
            visual["page"] = figure["page"]
        self.prepared_visuals[figure_id] = visual
        self._save()
        return dict(visual)

    @_locked
    def read_source(self, source_id: str = "main", section_id: str | None = None, page: int | None = None,
                    offset: int = 0, limit: int = 16000) -> dict:
        if source_id not in self.documents:
            raise SourceError("Unknown source; prepare or fetch it first")
        document = self.documents[source_id]
        sections = document.sections
        if section_id is not None:
            sections = [s for s in sections if s["id"] == section_id]
            if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
                if not sections:
                    anchored = [s for s in document.sections if section_id in s.get("anchors", [])]
                    if anchored:
                        sections = [min(anchored, key=lambda s: (s.get("role") == "heading", len(s["text"])))]
                if sections and sections[0].get("role") == "heading":
                    identities = {sections[0]["id"]}
                    while True:
                        children = {s["id"] for s in document.sections if s.get("parent_id") in identities}
                        if children <= identities:
                            break
                        identities.update(children)
                    sections = [s for s in document.sections if s["id"] in identities]
        if page is not None:
            sections = [s for s in sections if s.get("page") == page]
        if not sections:
            raise SourceError("Source section or page does not exist")
        offset, limit = max(0, int(offset)), max(1, min(32000, int(limit)))
        text = "\n\n".join(s["text"] for s in sections)
        result = {"source_id": source_id, "title": document.title, "url": document.url,
                "section_id": section_id, "page": page, "text": text[offset:offset + limit],
                "offset": offset, "next_offset": offset + limit if offset + limit < len(text) else None,
                "total_characters": len(text)}
        if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION and source_id == "main":
            position = 0
            spans = []
            for section in sections:
                end = position + len(section["text"])
                if end > offset and position < offset + limit:
                    spans.append({"section_id": section["id"], "start": position, "end": end,
                                  **({"page": section["page"]} if section.get("page") else {})})
                position = end + 2
            result["section_spans"] = spans
            result["citation_instruction"] = "Use the canonical section_id in section_spans for each exact quote; a heading request includes its descendant paragraphs."
            if len(sections) == 1:
                result["section_id"] = sections[0]["id"]
            section_ids = {section["id"] for section in sections}
            observations = [self.visual_observations[key] for key in sorted(self.visual_observations)
                            if (record := self.visual_observations[key])
                            if record.get("section_id") in section_ids and record.get("readable") is True]
            # Supplementary observations remain explicitly distinct from the
            # immutable original text and can be paged by the same character
            # offset without turning them into matchable original quotations.
            result["visual_observation_index"] = [{"id": record["id"], "section_id": record["section_id"],
                "figure_id": record["figure_id"], "total_characters": len(record["observation"])} for record in observations]
            position = 0
            excerpts = []
            for record in observations:
                end = position + len(record["observation"])
                if end > offset and position < offset + limit:
                    begin_local, end_local = max(0, offset - position), min(len(record["observation"]), offset + limit - position)
                    excerpts.append({**record, "observation": record["observation"][begin_local:end_local],
                                     "source_method": "vision", "observation_offset": begin_local,
                                     "stream_start": position, "stream_end": end})
                position = end + 2
            result["visual_observations"] = excerpts
            result["visual_total_characters"] = max(0, position - 2)
            result["visual_next_offset"] = offset + limit if offset + limit < max(0, position - 2) else None
        return result

    @_locked
    def search_papers(self, query: str) -> dict:
        query = str(query).strip()
        if not query or len(query) > 500:
            raise SourceError("Search query must contain 1–500 characters")
        self.searches += 1
        self.query_log.append(query)
        self.search_records[query] = {"candidate_ids": [], "errors": ["Search did not complete"], "completed": False}
        if self.searches > 12:
            self.search_records[query]["errors"] = ["Related-paper search budget exhausted"]
            self._save()
            raise SourceError("Related-paper search budget exhausted")
        self._save()
        results: list[dict] = []
        failures = []
        try:
            url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"search_query": "all:" + query, "max_results": 6})
            data = download_public(url, max_bytes=2 * 1024 * 1024, timeout=20)
            root = ET.fromstring(data.data)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("a:entry", ns):
                paper_url = entry.findtext("a:id", "", ns).replace("http://", "https://")
                arxiv_id = normalize_arxiv(paper_url)
                results.append({"id": "arxiv:" + arxiv_id, "title": " ".join(entry.findtext("a:title", "", ns).split()),
                                "url": f"https://arxiv.org/abs/{arxiv_id}",
                                "abstract": entry.findtext("a:summary", "", ns), "source": "arxiv"})
        except (SourceError, ET.ParseError):
            failures.append("arXiv search unavailable")
        try:
            url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(
                {"query": query, "limit": 6, "fields": "title,abstract,url,externalIds,openAccessPdf"})
            payload = json.loads(download_public(url, max_bytes=2 * 1024 * 1024, timeout=20).data)
            for entry in payload.get("data", []):
                arxiv_id = (entry.get("externalIds") or {}).get("ArXiv")
                identity = "arxiv:" + arxiv_id if arxiv_id else "s2:" + entry["paperId"]
                pdf_url = (entry.get("openAccessPdf") or {}).get("url")
                results.append({"id": identity, "title": entry.get("title", ""), "abstract": entry.get("abstract") or "",
                                "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else entry.get("url"),
                                "pdf_url": pdf_url, "source": "semantic-scholar"})
        except (SourceError, ValueError, KeyError, TypeError):
            failures.append("Semantic Scholar search unavailable")
        results = [entry for entry in results if not self.is_main_source(entry["id"], entry.get("url"))]
        for entry in results:
            self.candidates[entry["id"]] = entry
        self.search_records[query] = {"candidate_ids": list(dict.fromkeys(entry["id"] for entry in results)),
                                      "errors": list(failures), "completed": not failures}
        self.limitations.extend(failures)
        self._save()
        return {"candidates": list({e["id"]: e for e in results}.values()), "limitations": failures}

    @_locked
    def fetch_related(self, id: str, url: str | None = None) -> dict:
        candidate = self.candidates.get(id)
        if self.is_main_source(id, url) or (candidate and self.is_main_source(id, candidate.get("url"))):
            raise SourceError("The main paper cannot be used as an external source")
        identity = "related:" + str(id)
        if identity in self.documents:
            document = self.documents[identity]
            if self.is_main_source(document.id, document.url, document.sha256):
                raise SourceError("The main paper or an identical copy cannot be used as an external source")
            return {"source_manifest": document.manifest(), "sections": section_preview(document.sections, 2, 16000)}
        if len([d for d in self.documents.values() if d.kind == "external"]) >= min(self.max_related, 3):
            raise SourceError("At most three related papers can be read in one task")
        if not candidate and id.startswith("arxiv:"):
            arxiv_id = normalize_arxiv(id)
            candidate = {"id": id, "title": f"arXiv {arxiv_id}", "url": f"https://arxiv.org/abs/{arxiv_id}"}
        if not candidate:
            raise SourceError("Related paper must come from a search result or an arXiv identifier")
        if url and url not in {candidate.get("url"), candidate.get("pdf_url")}:
            raise SourceError("Related URL does not match the selected search result")
        filename = "related-" + hashlib.sha256(id.encode()).hexdigest()[:16]
        try:
            structured = None
            if id.startswith("arxiv:"):
                arxiv_id = normalize_arxiv(id)
                try:
                    downloaded = download_public(f"https://arxiv.org/html/{arxiv_id}")
                    if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
                        from .structured_source import parse_structured_html
                        structured = parse_structured_html(downloaded.data, identity, url=downloaded.url, requested_id=arxiv_id)
                    elif len(parse_html(downloaded.data)) < 800:
                        raise SourceError("HTML full text unavailable")
                    extension = ".html"
                except SourceError:
                    downloaded = download_public(f"https://arxiv.org/pdf/{arxiv_id}")
                    extension = ".pdf"
            elif candidate.get("pdf_url"):
                downloaded = download_public(candidate["pdf_url"])
                extension = ".pdf"
            else:
                raise SourceError("No publicly accessible full text was found")
            if extension == ".pdf" and not downloaded.data.startswith(b"%PDF-"):
                raise SourceError("Related-paper source did not return a PDF")
            path = self.work_dir / (filename + extension)
            if self.rules_version == STRUCTURED_SOURCE_RULES_VERSION:
                from .structured_source import immutable_write, parse_structured_pdf, pdf_identity
                if extension == ".pdf" and id.startswith("arxiv:"):
                    resolved = pdf_identity(downloaded.data, arxiv_id, downloaded.url)
                    immutable_write(self.work_dir, filename + extension, downloaded.data)
                    sections, coverage = parse_structured_pdf(path, identity)
                    coverage["source_selection"] = {"format": "pdf", "reason": "Related HTML unavailable; validated PDF fallback",
                                                    "requested_id": arxiv_id, "resolved_id": resolved}
                    structured = sections, coverage
                else:
                    immutable_write(self.work_dir, filename + extension, downloaded.data)
            else:
                path.write_bytes(downloaded.data)
            document = self._add(identity, candidate["title"], downloaded.url, path, "external", structured=structured)
            return {"source_manifest": document.manifest(), "sections": section_preview(document.sections, 2, 16000), "coverage": document.coverage}
        except SourceError as exc:
            self.limitations.append(f"Full text unavailable for {id}: {exc}")
            self._save()
            return {"id": id, "title": candidate["title"], "abstract": candidate.get("abstract", ""),
                    "coverage": {"full_text": False}, "limitation": str(exc)}
