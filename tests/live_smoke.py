"""Opt-in real-provider end-to-end check; never used by production execution.

Uses an explicitly named test PostgreSQL database and a fresh runtime. The API
key comes from PAPER_READER_EVAL_API_KEY or OPENROUTER_API_KEY and is never
printed. All requests exercise the actual API; Worker runs the actual Pi SDK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--skill", choices=["deeppapernote", "ljg-paper"], default="ljg-paper")
    parser.add_argument("--workflow-mode", choices=["guided", "native"], default="guided")
    parser.add_argument("--paper", default="2106.10199")
    parser.add_argument("--pdf", type=Path, help="Upload a real retained PDF instead of downloading arXiv again")
    parser.add_argument("--model", default="qwen/qwen3-coder")
    parser.add_argument("--vision", action="store_true", help="Declare a provider/model that accepts image tool results")
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--allow-private-models", action="store_true", help="Allow the explicitly selected private model endpoint in this isolated runtime")
    parser.add_argument("--no-auth", action="store_true", help="Use a non-secret SDK placeholder for a server that ignores Authorization; never use ambient API keys")
    parser.add_argument("--thinking-format", choices=["default", "qwen-chat-template"], default="default", help="Explicit model compatibility setting; qwen-chat-template sends the runner's disabled-thinking setting to Qwen/vLLM")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--max-tool-calls", type=int, default=160, help="Shared tool budget across all phases of one attempt (10–500)")
    parser.add_argument("--revision", help="Also run a natural-language revision after a completed read")
    args = parser.parse_args()
    implementation_files = [ROOT / "backend/app" / name for name in
                            ("worker.py", "workflow.py", "long_workflow.py", "pipeline.py", "source_tools.py", "structured_source.py", "table_math.py", "evidence.py", "api.py", "config.py")]
    implementation_files += sorted((ROOT / "agent-runner/dist").glob("*.js"))
    implementation_hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in implementation_files if path.is_file()}
    if not 10 <= args.max_tool_calls <= 500:
        parser.error("--max-tool-calls must be between 10 and 500")
    if not urlsplit(args.database_url).path.endswith("_test"):
        parser.error("Use an isolated PostgreSQL database ending in '_test'")
    key = "unused-no-auth" if args.no_auth else os.environ.get("PAPER_READER_EVAL_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        parser.error("Set PAPER_READER_EVAL_API_KEY or OPENROUTER_API_KEY")
    runtime = args.runtime.expanduser().resolve()
    if runtime == ROOT or ROOT in runtime.parents:
        parser.error("Keep evaluation runtime outside project source")
    if runtime.exists() and any(runtime.iterdir()):
        parser.error("Runtime must be a fresh directory")
    from cryptography.fernet import Fernet
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    (runtime / "config").mkdir(mode=0o700)
    settings = {
        "PAPER_READER_DATABASE_URL": args.database_url,
        "PAPER_READER_RUNTIME_DIR": str(runtime),
        "PAPER_READER_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PAPER_READER_JOB_TIMEOUT_SECONDS": str(args.timeout),
        "PAPER_READER_MAX_TOOL_CALLS": str(args.max_tool_calls),
        "PAPER_READER_ALLOW_PRIVATE_MODELS": str(args.allow_private_models).lower(),
    }
    config_file = runtime / "config" / "app.env"
    config_file.write_text("\n".join(f"{k}={v}" for k, v in settings.items()) + "\n")
    config_file.chmod(0o600)
    os.environ.update(settings)
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from app.db import SessionLocal
    from app.models import User
    from app.security import hash_password
    from app.builtin import ensure_builtin
    from app.main import app
    from app.worker import Worker

    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    command.upgrade(cfg, "head")
    username, password = "eval-" + secrets.token_hex(5), secrets.token_urlsafe(24)
    with SessionLocal() as db:
        db.add(User(username=username, password_hash=hash_password(password), is_admin=True))
        ensure_builtin(db)
        db.commit()
    client = TestClient(app, headers={"Origin": "http://testserver"})

    def request(method: str, path: str, **kwargs):
        response = client.request(method, "/api/v1" + path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
        return response.json()

    request("POST", "/auth/login", json={"username": username, "password": password})
    workspace = request("POST", "/workspaces", json={"name": "Live evaluation " + args.skill})
    prefix = "/workspaces/" + workspace["id"]
    model = request("POST", prefix + "/models", json={
        "name": "Evaluation provider", "base_url": args.base_url, "model": args.model,
        "api_key": key, "vision": args.vision, "context_window": 262144, "max_tokens": 12000,
        "thinking_format": args.thinking_format,
    })
    probe = request("POST", prefix + "/models/" + model["id"] + "/test")
    print(json.dumps({"event": "model_probe", "result": probe}), flush=True)
    if not probe["ok"]:
        return 2
    template = next(x for x in request("GET", prefix + "/templates") if x["is_builtin"])
    if args.pdf:
        with args.pdf.expanduser().resolve().open("rb") as document:
            paper = request("POST", prefix + "/papers/upload", files={"file": (args.pdf.name, document, "application/pdf")})
    else:
        paper = request("POST", prefix + "/papers", json={"arxiv": args.paper})
    created = request("POST", prefix + "/readings", json={
        "paper_id": paper["id"], "skill_id": args.skill,
        "workflow_mode": args.workflow_mode,
        "model_connection_id": model["id"], "template_id": template["id"],
    })

    elapsed_seconds = {}

    def execute(job_id: str) -> dict:
        started = time.monotonic()
        worker = Worker()
        thread = threading.Thread(target=lambda: worker.run(once=True), daemon=True)
        thread.start()
        last = None
        while thread.is_alive():
            job = request("GET", prefix + "/jobs/" + job_id)
            records = job.get("stage_records", [])
            state = (job["status"], job["stage"], job["progress"],
                     tuple((r["name"], r["status"]) for r in records))
            if state != last:
                print(json.dumps({"event": "progress", "job_id": job_id, "status": state[0], "stage": state[1], "progress": state[2], "stages": [{"name": r["name"], "status": r["status"]} for r in records]}), flush=True)
                last = state
            thread.join(timeout=5)
        elapsed_seconds[job_id] = round(time.monotonic() - started, 2)
        return request("GET", prefix + "/jobs/" + job_id)

    job = execute(created["job_id"])
    detail = request("GET", prefix + "/readings/" + created["reading_id"])
    paper = request("GET", prefix + "/papers/" + paper["id"])
    retained_sources = []
    for manifest in runtime.glob("tmp/*/*/source_manifest.json"):
        retained_sources.extend(json.loads(manifest.read_text()).get("sources", []))
    if detail["revisions"]:
        retained_sources = detail["revisions"][0].get("provenance", {}).get("source_manifest", retained_sources)
    main_source = next((s for s in retained_sources if s.get("source_id") == "main"), {})
    report = {"skill": args.skill, "workflow_mode": args.workflow_mode, "paper": args.paper, "model": args.model,
              "base_url": args.base_url, "auth_mode": "non_secret_placeholder" if args.no_auth else "api_key",
              "thinking_format": args.thinking_format, "implementation_sha256": implementation_hashes,
              "model_parameters": {"vision": args.vision, "context_window": 262144, "max_tokens": 12000,
                                   "temperature": "omitted; provider default", "seed": "omitted",
                                   "enable_thinking": False if args.thinking_format == "qwen-chat-template" else None},
              "configured_limits": {"timeout_seconds": args.timeout, "max_tool_calls": args.max_tool_calls},
              "input_kind": "uploaded_pdf" if args.pdf else "arxiv", "source_sha256": paper.get("sha256"),
              "source_metadata": paper.get("metadata_json", {}),
              "retained_source_manifest": retained_sources,
              "source_selection": main_source.get("coverage", {}).get("source_selection"),
              "retained_source_sha256": main_source.get("sha256"),
              "workspace_id": workspace["id"], "reading_id": created["reading_id"],
              "job": job, "elapsed_seconds": elapsed_seconds,
              "revision_count": len(detail["revisions"])}
    (runtime / "stages.json").write_text(json.dumps(job.get("stage_records", []), ensure_ascii=False, indent=2))
    if detail["revisions"]:
        revision = detail["revisions"][0]
        original_revision = dict(revision)
        report["verification"] = revision["verification"]
        report["evidence_count"] = len(revision["evidence_index"])
        report["verified_evidence_count"] = sum(e.get("verified") is True for e in revision["evidence_index"])
        report["brief_length"] = len(revision["brief_markdown"])
        report["native_checks"] = revision["verification"].get("native_checks")
        report["native_review"] = revision["verification"].get("agent_report", {}).get("native_skill")
        report["provenance"] = revision["provenance"]
        packet = revision.get("reading_packet", {})
        report["claim_count"] = len(packet.get("claims", []))
        report["open_question_count"] = len(packet.get("open_questions", []))
        (runtime / "reading-packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2))
        (runtime / "revision.json").write_text(json.dumps(revision, ensure_ascii=False, indent=2))
        (runtime / "brief.md").write_text(revision["brief_markdown"])
        (runtime / "full.md").write_text(revision["full_markdown"])
        if args.revision and job["status"] == "succeeded":
            # Preserve the first real outcome even if revision execution fails.
            (runtime / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            revised = request("POST", prefix + "/readings/" + created["reading_id"] + "/revisions", json={"instruction": args.revision, "base_revision_id": detail["current_revision_id"]})
            report["revision_job"] = execute(revised["job_id"])
            revision_stages = report["revision_job"].get("stage_records", [])
            report["revision_reused_stages"] = [r["name"] for r in revision_stages if r["status"] == "reused"]
            (runtime / "revised-stages.json").write_text(json.dumps(revision_stages, ensure_ascii=False, indent=2))
            after = request("GET", prefix + "/readings/" + created["reading_id"])
            retained = next(r for r in after["revisions"] if r["id"] == original_revision["id"])
            report["original_revision_immutable"] = retained == original_revision
            report["revision_count"] = len(after["revisions"])
            current = next((r for r in after["revisions"] if r["id"] == after["current_revision_id"]), None)
            if current and current["id"] != original_revision["id"]:
                report["revision_packet_unchanged"] = current.get("reading_packet", {}) == packet
                (runtime / "revised-brief.md").write_text(current["brief_markdown"])
                (runtime / "revised-full.md").write_text(current["full_markdown"])
                (runtime / "revised-revision.json").write_text(json.dumps(current, ensure_ascii=False, indent=2))
    (runtime / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    statuses = [job["status"]]
    if args.revision:
        statuses.append(report.get("revision_job", {}).get("status", "not_run"))
    # Partial is a useful retained result, but not a fully passing evaluation.
    return 0 if all(status == "succeeded" for status in statuses) and report.get("original_revision_immutable", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
