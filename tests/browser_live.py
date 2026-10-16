"""Opt-in browser smoke against a real test API/database, with no HTTP fixtures.

Uses an existing live_smoke runtime and its latest reading. Creates a temporary
member, starts the built frontend/API on loopback, checks it with Chromium, then
revokes the temporary account and stops the API. It never starts another job.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--frontend-dir", type=Path, help="Serve an isolated built frontend without replacing preview assets")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if not (runtime / "config/app.env").is_file():
        parser.error("Use an existing live_smoke test runtime")
    os.environ["PAPER_READER_RUNTIME_DIR"] = str(runtime)
    from app.config import get_settings
    settings = get_settings()
    if not urlsplit(settings.database_url).path.endswith("_test"):
        parser.error("Browser verification requires an isolated _test database")
    import httpx
    from sqlalchemy import delete, select
    from app.db import SessionLocal
    from app.models import Job, Membership, SessionRecord, User
    from app.security import hash_password

    username, password = "browser-" + secrets.token_hex(6), secrets.token_urlsafe(24)
    with SessionLocal() as db:
        job = db.scalar(select(Job).order_by(Job.created_at.desc()))
        if not job:
            parser.error("Test runtime has no reading job")
        user = User(username=username, password_hash=hash_password(password), is_admin=False)
        db.add(user)
        db.flush()
        user_id, reading_id = user.id, job.reading_id
        db.add(Membership(user_id=user_id, workspace_id=job.workspace_id, role="member"))
        db.commit()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    output = runtime / "test-runs/browser-live"
    output.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "PAPER_READER_RUNTIME_DIR": str(runtime),
                   "PAPER_READER_DATABASE_URL": settings.database_url,
                   "PAPER_READER_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
                   "PAPER_READER_SECURE_COOKIE": "false"}
    if args.frontend_dir:
        frontend = args.frontend_dir.resolve()
        if not (frontend / "index.html").is_file() or not (frontend / "assets").is_dir():
            parser.error("Use an existing complete frontend build")
        environment["PAPER_READER_FRONTEND_DIR"] = str(frontend)
    process = None
    try:
        with (output / "api.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT / "backend", env=environment, stdout=log, stderr=subprocess.STDOUT)
            base = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    if httpx.get(base + "/api/v1/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("Test API exited before becoming ready")
                time.sleep(0.2)
            else:
                raise RuntimeError("Test API did not become healthy")
            browser_env = {**environment, "BROWSER_SMOKE_BASE": base, "BROWSER_SMOKE_USERNAME": username,
                           "BROWSER_SMOKE_PASSWORD": password, "BROWSER_SMOKE_READING": reading_id,
                           "BROWSER_SMOKE_OUTPUT": str(output),
                           "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(ROOT.parent / "paper-reader-runtime/cache/playwright"))}
            result = subprocess.run(["node", str(ROOT / "tests/browser_live.mjs")], cwd=ROOT, env=browser_env, check=False)
            return result.returncode
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        with SessionLocal() as db:
            db.execute(delete(SessionRecord).where(SessionRecord.user_id == user_id))
            db.execute(delete(Membership).where(Membership.user_id == user_id))
            db.execute(delete(User).where(User.id == user_id))
            db.commit()


if __name__ == "__main__":
    raise SystemExit(main())
