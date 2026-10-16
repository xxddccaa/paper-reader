"""The approved ten-run HTML/PDF/short-paper matrix; real providers only.

Each case receives its own PostgreSQL *_test database and fresh runtime. The
runner retains every failure and refuses to mix implementation hashes. This is
an evaluation tool, never imported by production or used as model evidence.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def implementation_hashes():
    paths = sorted((ROOT / "backend/app").glob("*.py")) + sorted((ROOT / "agent-runner/dist").glob("*.js"))
    paths += [ROOT / "skills/registry.json", ROOT / "backend/app/builtin.py", ROOT / "tests/live_smoke.py",
              ROOT / "backend/requirements.lock", ROOT / "agent-runner/package-lock.json"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="Isolated parent *_test database; separate child databases are created")
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--dspark-pdf", required=True, type=Path)
    parser.add_argument("--bitfit-pdf", required=True, type=Path)
    parser.add_argument("--model", default="Qwen3.8-27B")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1/")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    database = urlsplit(args.database_url)
    if not database.path.endswith("_test"):
        parser.error("Use an explicitly isolated *_test database")
    runtime = args.runtime.resolve()
    if runtime == ROOT or ROOT in runtime.parents or (runtime.exists() and any(runtime.iterdir())):
        parser.error("Use a fresh runtime outside source")
    for path in (args.dspark_pdf, args.bitfit_pdf):
        if not path.is_file() or not path.read_bytes().startswith(b"%PDF-"):
            parser.error("Both retained PDF inputs must be real PDF files")
    cases = []
    for skill in ("deeppapernote", "ljg-paper"):
        for repeat in range(1, 4):
            cases.append({"name": f"dspark-html-{skill}-{repeat}", "paper": "2607.05147v1", "skill": skill,
                          "input_kind": "arxiv", "repeat": repeat})
    for paper, label, path in (("2607.05147v1", "dspark", args.dspark_pdf), ("2106.10199v5", "bitfit", args.bitfit_pdf)):
        for skill in ("deeppapernote", "ljg-paper"):
            cases.append({"name": f"{label}-pdf-{skill}", "paper": paper, "skill": skill,
                          "input_kind": "uploaded_pdf", "pdf": str(path.resolve()), "repeat": 1})
    frozen = implementation_hashes()
    plan = {"cases": cases, "model": args.model, "base_url": args.base_url, "thinking_format": "qwen-chat-template",
            "enable_thinking": False, "vision": True, "context_window": 262144, "max_tokens": 12000,
            "temperature": "omitted; provider default", "seed": "omitted",
            "timeout_seconds": 2400, "max_tool_calls": 240, "parallel_jobs": 1,
            "implementation_sha256": frozen, "input_sha256": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
                                                                for p in (args.dspark_pdf, args.bitfit_pdf)}}
    print(json.dumps({"count": len(cases), "cases": cases, "execute": args.execute}, ensure_ascii=False), flush=True)
    if not args.execute:
        return 0
    runtime.mkdir(parents=True, mode=0o700)
    (runtime / "matrix-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    # Creating an existing database is rejected. No DROP, reset or table cleanup.
    dsns = {}
    with psycopg.connect(args.database_url.replace("postgresql+psycopg:", "postgresql:"), autocommit=True) as connection:
        for index, case in enumerate(cases, 1):
            name = database.path.lstrip("/").removesuffix("_test") + f"_{index:02d}_test"
            if len(name.encode()) > 63:
                parser.error("Database prefix is too long")
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            dsns[case["name"]] = urlunsplit(database._replace(path="/" + name))
    def run(case):
        if implementation_hashes() != frozen:
            return {**case, "status": "not_run", "error": "Implementation changed after matrix freeze"}
        child = runtime / case["name"]
        command = [sys.executable, str(ROOT / "tests/live_smoke.py"), "--database-url", dsns[case["name"]],
                   "--runtime", str(child), "--skill", case["skill"], "--paper", case["paper"],
                   "--model", args.model, "--base-url", args.base_url, "--allow-private-models", "--no-auth",
                   "--thinking-format", "qwen-chat-template", "--vision", "--timeout", "2400", "--max-tool-calls", "240"]
        if case.get("pdf"):
            command += ["--pdf", case["pdf"]]
        start = time.monotonic()
        with (runtime / (case["name"] + ".log")).open("w") as stream:
            result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
        output = child / "evaluation.json"
        report = json.loads(output.read_text()) if output.is_file() else {}
        job = report.get("job", {})
        return {**case, "status": job.get("status", "failed_before_report"), "exit_code": result.returncode,
                "elapsed_seconds": round(time.monotonic() - start, 2), "job_id": job.get("id"),
                "error": job.get("error"), "usage": job.get("usage", {}),
                "implementation_unchanged": implementation_hashes() == frozen,
                "source_selection": report.get("verification", {}).get("source_selection") or report.get("source_selection"),
                "source_sha256": report.get("retained_source_sha256"),
                "section_coverage": report.get("verification", {}).get("section_coverage"),
                "figure_checks": report.get("verification", {}).get("figure_checks"),
                "brief_length": report.get("brief_length"), "content_review": "pending"}
    rows = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = [pool.submit(run, case) for case in cases]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            report = {"planned": 10, "finished": len(rows), "succeeded": sum(r["status"] == "succeeded" for r in rows),
                      "rows": sorted(rows, key=lambda r: next(i for i, c in enumerate(cases) if c["name"] == r["name"])),
                      "content_review": "pending", "general_stability_claim": False}
            (runtime / "matrix-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({k: row[k] for k in ("name", "status", "elapsed_seconds", "error")}), flush=True)
    return 0 if all(r["status"] == "succeeded" and r["implementation_unchanged"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
