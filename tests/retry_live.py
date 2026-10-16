"""Retry an existing live_smoke job through the actual API and worker.

Only isolated *_test databases are accepted. No source, checks or prior results
are edited. The encrypted model connection is reused; a temporary test member
is disabled afterward while its job ownership record remains intact.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--max-tool-calls", type=int, default=160)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--revision")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if not (runtime / "config/app.env").is_file() or runtime == ROOT or ROOT in runtime.parents:
        parser.error("Use an existing live_smoke runtime outside project source")
    if not 10 <= args.max_tool_calls <= 500 or args.timeout < 10:
        parser.error("Tool budget must be 10–500 and timeout at least 10 seconds")
    os.environ.update(PAPER_READER_RUNTIME_DIR=str(runtime), PAPER_READER_MAX_TOOL_CALLS=str(args.max_tool_calls),
                      PAPER_READER_JOB_TIMEOUT_SECONDS=str(args.timeout))
    from app.config import get_settings
    if not urlsplit(get_settings().database_url).path.endswith("_test"):
        parser.error("Use an isolated PostgreSQL database ending in _test")
    from fastapi.testclient import TestClient
    from sqlalchemy import delete, select
    from app.db import SessionLocal
    from app.main import app
    from app.models import Job, Membership, SessionRecord, User
    from app.security import hash_password
    from app.worker import Worker

    username, password = "retry-" + secrets.token_hex(6), secrets.token_urlsafe(24)
    with SessionLocal() as db:
        if db.scalar(select(Job.id).where(Job.status.in_({"queued", "running"})).limit(1)):
            parser.error("The isolated test database must have no competing queued or running jobs")
        old = db.get(Job, args.job_id) if args.job_id else db.scalar(select(Job).order_by(Job.created_at.desc()))
        if not old or old.status not in {"failed", "cancelled", "partial"}:
            parser.error("Select a terminal failed, cancelled or partial test job")
        wid, rid, old_id = old.workspace_id, old.reading_id, old.id
        member = User(username=username, password_hash=hash_password(password), is_admin=False)
        db.add(member)
        db.flush()
        uid = member.id
        db.add(Membership(user_id=uid, workspace_id=wid, role="member"))
        db.commit()
    prefix = f"/api/v1/workspaces/{wid}"
    report = {"retry_of": old_id, "reading_id": rid, "workspace_id": wid,
              "configured_limits": {"max_tool_calls": args.max_tool_calls, "timeout_seconds": args.timeout}}
    try:
        with TestClient(app, headers={"Origin": "http://testserver"}) as client:
            def request(method: str, path: str, **kwargs):
                response = client.request(method, path, **kwargs)
                if response.status_code >= 400:
                    raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
                return response.json()

            request("POST", "/api/v1/auth/login", json={"username": username, "password": password})

            def execute(jid: str) -> dict:
                worker = Worker()
                thread = threading.Thread(target=lambda: worker.run(once=True), daemon=True)
                thread.start()
                last = None
                while thread.is_alive():
                    job = request("GET", prefix + "/jobs/" + jid)
                    state = job["status"], job["stage"], job["progress"]
                    if state != last:
                        print(json.dumps({"event": "progress", "job_id": jid, "status": state[0], "stage": state[1], "progress": state[2]}), flush=True)
                        last = state
                    thread.join(timeout=5)
                return request("GET", prefix + "/jobs/" + jid)

            created = request("POST", prefix + "/jobs/" + old_id + "/retry")
            report["job"] = execute(created["job_id"])
            detail = request("GET", prefix + "/readings/" + rid)
            report["revision_count"] = len(detail["revisions"])
            current = next((r for r in detail["revisions"] if r["id"] == detail["current_revision_id"]), None)
            if current:
                original = current
                report.update(verification=current["verification"], evidence_count=len(current["evidence_index"]),
                              verified_evidence_count=sum(e.get("verified") is True for e in current["evidence_index"]),
                              brief_length=len(current["brief_markdown"]), provenance=current["provenance"])
                for name, content in {"retry-brief.md": current["brief_markdown"], "retry-full.md": current["full_markdown"],
                                      "retry-revision.json": json.dumps(current, ensure_ascii=False, indent=2)}.items():
                    (runtime / name).write_text(content)
                if args.revision:
                    revision = request("POST", prefix + "/readings/" + rid + "/revisions", json={"instruction": args.revision, "base_revision_id": current["id"]})
                    report["revision_job"] = execute(revision["job_id"])
                    after = request("GET", prefix + "/readings/" + rid)
                    report["original_revision_immutable"] = next(r for r in after["revisions"] if r["id"] == original["id"]) == original
                    report["revision_count"] = len(after["revisions"])
                    current = next(r for r in after["revisions"] if r["id"] == after["current_revision_id"])
                    if current["id"] != original["id"]:
                        (runtime / "retry-revised-brief.md").write_text(current["brief_markdown"])
                        (runtime / "retry-revised-full.md").write_text(current["full_markdown"])
                        (runtime / "retry-revised-revision.json").write_text(json.dumps(current, ensure_ascii=False, indent=2))
            (runtime / "retry-evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            (runtime / ("retry-evaluation-" + report["job"]["id"] + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
            if current:
                (runtime / ("retry-revision-" + report["job"]["id"] + ".json")).write_text(json.dumps(current, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False), flush=True)
            states = [report["job"]["status"]]
            if args.revision:
                states.append(report.get("revision_job", {}).get("status", "not_run"))
            return 0 if all(s == "succeeded" for s in states) and report.get("original_revision_immutable", True) else 1
    finally:
        with SessionLocal() as db:
            db.execute(delete(SessionRecord).where(SessionRecord.user_id == uid))
            db.execute(delete(Membership).where(Membership.user_id == uid))
            member = db.get(User, uid)
            if member:
                member.active = False
            db.commit()


if __name__ == "__main__":
    raise SystemExit(main())
