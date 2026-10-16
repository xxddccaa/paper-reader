"""Repeat real-provider paper readings; dry-run by default, no quality scores invented.

Runs live_smoke.py in fresh child runtimes, sequentially, using one explicitly
isolated test database. Each child creates its own tenant; no database is reset.
The API key is inherited from the environment and never placed in arguments.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def summarize(rows: list[dict]) -> dict:
    result = {}
    for skill in sorted({row["skill"] for row in rows}):
        selected = [row for row in rows if row["skill"] == skill]
        lengths = [row["brief_length"] for row in selected if isinstance(row.get("brief_length"), int)]
        result[skill] = {
            "attempted": len(selected),
            "succeeded": sum(row.get("status") == "succeeded" for row in selected),
            "partial": sum(row.get("status") == "partial" for row in selected),
            "failed_or_incomplete": sum(row.get("status") not in {"succeeded", "partial"} for row in selected),
            "brief_length_mean": statistics.mean(lengths) if lengths else None,
            "brief_length_stdev": statistics.pstdev(lengths) if lengths else None,
            "human_quality_review": "pending",
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("PAPER_READER_EVAL_DATABASE_URL"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--papers", type=Path, default=Path(__file__).with_name("evaluation-papers.json"))
    parser.add_argument("--skills", nargs="+", choices=["deeppapernote", "ljg-paper"], default=["deeppapernote", "ljg-paper"])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--workflow-mode", choices=["guided", "native"], default="guided")
    parser.add_argument("--model", default="qwen/qwen3-vl-235b-a22b-instruct")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--execute", action="store_true", help="Actually perform the listed provider calls")
    args = parser.parse_args()
    if args.repetitions < 1 or args.timeout < 10:
        parser.error("repetitions must be positive and timeout must be at least 10 seconds")
    papers = json.loads(args.papers.read_text())
    if not isinstance(papers, list) or not papers or any(not isinstance(p, dict) or not p.get("arxiv") for p in papers):
        parser.error("papers must be a nonempty JSON list with arxiv identifiers")
    cases = [{"index": i + 1, "paper": paper["arxiv"], "title": paper.get("title"), "skill": skill, "workflow_mode": args.workflow_mode, "repeat": repeat}
             for i, (paper, skill, repeat) in enumerate((p, s, r) for p in papers for s in args.skills for r in range(1, args.repetitions + 1))]
    print(json.dumps({"cases": cases, "count": len(cases), "model": args.model, "execute": args.execute}, ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        return 0
    if not args.database_url or not urlsplit(args.database_url).path.endswith("_test"):
        parser.error("Set PAPER_READER_EVAL_DATABASE_URL to a dedicated PostgreSQL database ending in '_test'")
    if not (os.environ.get("PAPER_READER_EVAL_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
        parser.error("Set PAPER_READER_EVAL_API_KEY or OPENROUTER_API_KEY")
    runtime = args.runtime.expanduser().resolve()
    if runtime == ROOT or ROOT in runtime.parents:
        parser.error("Keep evaluation runtime outside project source")
    if runtime.exists() and any(runtime.iterdir()):
        parser.error("Use a fresh evaluation runtime directory")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    (runtime / "matrix-plan.json").write_text(json.dumps({"cases": cases, "model": args.model, "vision": args.vision}, ensure_ascii=False, indent=2))
    rows = []
    for case in cases:
        name = f'{case["index"]:02d}-{case["skill"]}-r{case["repeat"]}'
        child = runtime / name
        command = [sys.executable, str(ROOT / "tests/live_smoke.py"), "--database-url", args.database_url,
                   "--runtime", str(child), "--skill", case["skill"], "--workflow-mode", args.workflow_mode, "--paper", case["paper"],
                   "--model", args.model, "--base-url", args.base_url, "--timeout", str(args.timeout)]
        if args.vision:
            command.append("--vision")
        start = time.monotonic()
        # stdout contains sanitized progress/report; retain diagnostics for inspection.
        with (runtime / (name + ".log")).open("w") as stream:
            process = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
        report_path = child / "evaluation.json"
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        row = {**case, "runtime": name, "exit_code": process.returncode,
               "wall_seconds": round(time.monotonic() - start, 2),
               "status": report.get("job", {}).get("status", "incomplete"),
               "brief_length": report.get("brief_length"),
               "evidence_count": report.get("evidence_count"),
               "verified_evidence_count": report.get("verified_evidence_count"),
               "usage": report.get("job", {}).get("usage"), "human_quality_review": None}
        rows.append(row)
        (runtime / "matrix-results.json").write_text(json.dumps({"runs": rows, "summary": summarize(rows)}, ensure_ascii=False, indent=2))
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if all(row["status"] == "succeeded" and row["exit_code"] == 0 for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
