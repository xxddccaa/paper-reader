"""Lease-fenced host for the persistent Pi task owner.

There is no phase scheduler here. Pi chooses tools, reading scopes, repairs and
reviews; the host provides durability, capabilities, budgets and publication.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import threading
import time

from sqlalchemy import select

from .agent_state import AgentBudgetExhausted, AgentLeaseLost, AgentStore, ENGINE
from .agent_tools import AgentTools, READ_ONLY
from .config import get_settings
from .db import SessionLocal
from .models import Job
from .pipeline import BudgetExhausted, Pipeline, PipelineError, TaskCancelled, task_path, terminate_group
from .source_tools import SourceError

LOG = logging.getLogger(__name__)
MAX_LINE = 40 * 1024 * 1024  # Complete checkpoint, bounded again by AgentStore.


def cross_paper_requested(goal: str) -> bool:
    return bool(re.search(r"跨论文|相关论文|其他论文|其它论文|对比.*(?:论文|工作)|比较.*(?:论文|工作)|"
                          r"(?:compare|comparison|versus).{0,100}(?:paper|work|approach)|related papers?", goal, re.I))


def _copy_legacy_sources(job, token, work_dir):
    """Import only host-authorized originals/checkpoints, never an old verdict."""
    from .worker import _owned_job
    origin_id = job.input_snapshot.get("retry_from_job_id") or job.input_snapshot.get("retry_of_job_id")
    if not origin_id:
        return []
    settings = get_settings()
    with SessionLocal() as db:
        current = _owned_job(db, job.id, token)
        previous = db.scalar(select(Job).where(Job.id == origin_id, Job.workspace_id == current.workspace_id,
            Job.reading_id == current.reading_id, Job.conversation_id == current.conversation_id))
        if previous is None or previous.status not in {"failed", "cancelled", "partial", "waiting"}:
            raise PipelineError("Agent takeover source is not available in this task scope")
        for key in ("paper_id", "skill_id", "template", "base_revision_id", "source_rules_version"):
            if previous.input_snapshot.get(key) != current.input_snapshot.get(key):
                return []  # Inputs changed; fresh acquisition is still permitted.
        origin = settings.runtime_dir / "tmp" / previous.id / str(previous.attempt)
        corpus = origin / "source_corpus.json"
        if not corpus.is_file() or corpus.is_symlink():
            return []
        payload = json.loads(corpus.read_text())
        if payload.get("version") != current.input_snapshot.get("source_rules_version"):
            return []
        if not (work_dir / "source_corpus.json").is_file():
            for name, digest in payload.get("file_sha256", {}).items():
                source = task_path(origin, name, must_exist=True)
                if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                    raise SourceError("Legacy source hash changed; cannot import it")
                target = task_path(work_dir, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            shutil.copyfile(corpus, work_dir / "source_corpus.json")
            ledger = origin / "host-image-inspections.json"
            if ledger.is_file() and not ledger.is_symlink():
                shutil.copyfile(ledger, work_dir / ledger.name)
        old_checks = origin / "host-workflow"
        if old_checks.is_dir() and not old_checks.is_symlink():
            target = work_dir / "host-workflow"
            target.mkdir(exist_ok=True)
            for source in old_checks.glob("*.json"):
                if source.is_file() and not source.is_symlink() and source.stat().st_size <= 32 * 1024 * 1024:
                    shutil.copyfile(source, target / source.name)
        return deepcopy(previous.stage_records or [])


class AgentHost:
    """One logical task, one main session, fresh independent review sessions."""
    def __init__(self, job, token, spec, pipeline, store, cancelled, deadline, secret, usage_delta):
        self.job, self.token, self.spec, self.pipeline, self.store = job, token, spec, pipeline, store
        self.cancelled, self.deadline, self.secret, self.usage_delta = cancelled, deadline, secret, usage_delta
        self.tools = AgentTools(pipeline, store, spec, self.review)
        pipeline.agent_tools = self.tools
        pipeline.agent_cross_requested = bool(spec["agent"].get("cross_paper_requested"))
        self._auxiliary = threading.BoundedSemaphore(2)
        self._last_draft = None

    def check(self):
        if self.cancelled.is_set():
            raise TaskCancelled("Agent task cancelled or lease lost")
        if time.monotonic() >= self.deadline:
            raise AgentBudgetExhausted("Agent time allocation exhausted; saved work can continue")

    def sync_draft(self):
        if self.job.kind != "chat":
            return
        content = self.store.load()["state"].get("draft_content")
        if isinstance(content, str) and content != self._last_draft:
            from .chat_jobs import persist_chat_event
            persist_chat_event(self.job.id, self.token, {"type": "replace", "content": content})
            self._last_draft = content

    def observation(self, message):
        from .worker import _safe_log_value, report_progress
        message = _safe_log_value(str(message)[:2000], self.secret)
        self.store.mutate("agent_observation", {"message": message},
                          lambda state: state.update(current_action=message) or {"saved": True})
        report_progress(self.job.id, self.token, "agent", message)

    def request_spec(self, operation="run", context=None):
        saved = self.store.load()
        spec = deepcopy(self.spec)
        spec["limits"] = {**spec["limits"], "timeout_seconds": max(1, min(saved["remaining_seconds"], self.deadline-time.monotonic())),
                          "max_tool_calls": max(1, saved["remaining_tool_calls"])}
        spec["agent"].update(operation=operation, state=saved["state"] if operation == "run" else {},
                             messages=saved["messages"] if operation == "run" else [],
                             summary=saved["summary"] if operation == "run" else "",
                             tools=[definition for definition in self.tools.definitions()
                                    if operation == "run" or definition["name"] in READ_ONLY])
        if operation != "run":
            spec["agent"]["review_input"] = context
            spec.pop("previous_revision", None)
            spec.pop("chat", None)
        return spec

    def review(self, kind, context):
        self.check()
        if not self._auxiliary.acquire(blocking=False):
            return {"passed": False, "issues": ["Two auxiliary sessions are already active; retry after they finish"]}
        try:
            # Deliberately synchronous: actual-read tracking belongs to the same
            # AgentTools review scope/thread. A failure returns to the main Pi.
            return self.run_process(self.request_spec("review", context))
        except (AgentBudgetExhausted, AgentLeaseLost, TaskCancelled):
            raise
        except (PipelineError, SourceError, OSError, ValueError) as exc:
            from .worker import _safe_log_value
            return {"passed": False, "issues": ["Independent review interrupted: " + _safe_log_value(str(exc), self.secret)[:1800]]}
        finally:
            self._auxiliary.release()

    def rpc(self, name, args, operation, reservations):
        self.check()
        if not isinstance(args, dict):
            raise PipelineError("Invalid Agent RPC arguments")
        if name == "host_agent_attempt":
            call_id, tool = args.get("call_id"), args.get("name")
            allowed = {item["name"] for item in self.tools.definitions()}
            if operation != "run":
                allowed &= READ_ONLY
                allowed.add("submit_review" if operation == "review" else "submit_subtask")
            before = self.store.load()["tool_calls"]
            result = self.store.consume(call_id, tool)
            difference = result["tool_calls"] - before
            if difference > 0:
                self.usage_delta({"tool_calls": difference})
            if tool not in allowed:
                raise PipelineError("Tool is not permitted in this Agent session")
            reservations[call_id] = tool
            return result
        if name == "host_agent_action":
            call_id, tool = args.get("call_id"), args.get("name")
            if reservations.get(call_id) != tool:
                raise PipelineError("Tool execution has no matching durable reservation")
            if operation != "run" and tool not in READ_ONLY:
                raise PipelineError("Independent Agent sessions have read-only tools")
            value = self.tools.call(tool, args.get("args"), call_id)
            if operation == "run":
                self.sync_draft()
            return value
        if name == "host_agent_checkpoint" and operation == "run":
            from .worker import _safe_log_value
            clean = _safe_log_value(args, self.secret)
            return self.store.checkpoint(clean.get("messages"), clean.get("summary"))
        if name == "host_agent_observation" and operation == "run":
            self.observation(args.get("message", ""))
            return {"saved": True}
        raise PipelineError("Unknown or forbidden internal Agent RPC")

    def run_process(self, spec):
        """JSONL transport only; the Pi process controls all reasoning and steps."""
        from .worker import _safe_log_value
        self.check()
        runner = get_settings().project_dir / "agent-runner/dist/index.js"
        if not runner.is_file():
            raise PipelineError("Pi Agent runner is not built")
        node = shutil.which("node")
        if not node:
            raise PipelineError("Node runtime is unavailable")
        work_dir = self.pipeline.work_dir
        environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8",
                       "NODE_ENV": "production", "TMPDIR": str(work_dir),
                       "XDG_CONFIG_HOME": str(work_dir / ".config"), "XDG_CACHE_HOME": str(work_dir / ".cache")}
        process = subprocess.Popen([node, str(runner)], cwd=work_dir, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        selector, buffer, output = selectors.DefaultSelector(), b"", None
        seen, reservations = set(), {}
        operation = spec["agent"]["operation"]
        try:
            process.stdin.write((json.dumps(spec, ensure_ascii=False, allow_nan=False) + "\n").encode())
            process.stdin.flush()
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while True:
                self.check()
                for key, _ in selector.select(timeout=.15):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        continue  # No raw model or transport diagnostics in logs.
                    buffer += chunk
                    if len(buffer) > MAX_LINE and b"\n" not in buffer:
                        raise PipelineError("Agent protocol message exceeds size bound")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if len(line) > MAX_LINE:
                            raise PipelineError("Agent protocol message exceeds size bound")
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError) as exc:
                            raise PipelineError("Agent emitted invalid JSONL") from exc
                        if not isinstance(event, dict):
                            raise PipelineError("Invalid Agent protocol event")
                        kind = event.get("type")
                        if kind == "tool_request":
                            identity = event.get("id")
                            if output is not None or not isinstance(identity, str) or not identity or identity in seen:
                                raise PipelineError("Repeated or invalid Agent request ID")
                            seen.add(identity)
                            response = {"type": "tool_response", "id": identity}
                            try:
                                response["result"] = self.rpc(event.get("name"), event.get("args"), operation, reservations)
                            except (AgentBudgetExhausted, AgentLeaseLost, TaskCancelled):
                                raise
                            except (PipelineError, SourceError, ValueError, TypeError, OSError) as exc:
                                response["error"] = _safe_log_value(str(exc)[:2000], self.secret)
                            process.stdin.write((json.dumps(response, ensure_ascii=False, allow_nan=False)+"\n").encode())
                            process.stdin.flush()
                        elif kind == "usage":
                            usage = event.get("usage")
                            if not isinstance(usage, dict):
                                raise PipelineError("Invalid usage event")
                            usage.pop("tool_calls", None)
                            self.usage_delta(_safe_log_value(usage, self.secret))
                        elif kind == "progress":
                            if operation == "run":
                                self.observation(event.get("message", "Agent 正在处理论文"))
                        elif kind == "result":
                            if output is not None or not isinstance(event.get("result"), dict):
                                raise PipelineError("Invalid or repeated Agent result")
                            output = _safe_log_value(event["result"], self.secret)
                        elif kind == "error":
                            raise PipelineError(_safe_log_value(str(event.get("message", "Agent process failed")), self.secret))
                        else:
                            raise PipelineError("Unsupported Agent protocol event")
                if process.poll() is not None and not selector.get_map():
                    break
            if buffer.strip() or output is None:
                raise PipelineError("Pi process stopped before a complete result; saved checkpoint is retained")
            if process.returncode != 0:
                raise PipelineError("Pi process exited unsuccessfully; saved checkpoint is retained")
            return output
        finally:
            terminate_group(process)
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def run_agent_job(worker, job):
    from .chat_jobs import build_chat_spec, publish_chat_result
    from .chat_workflow import ChatPipeline
    from .worker import (build_spec, finish_failure, heartbeat, publish_result, record_usage,
                         release_for_shutdown, _safe_log_value)
    settings, token = get_settings(), job.lease_token
    store = AgentStore(job.id, token)
    cancelled, done, exhausted = threading.Event(), threading.Event(), threading.Event()
    worker.current_cancel = cancelled
    secret, guard, host, published = "", None, None, False
    work_dir = task_path(settings.runtime_dir / "tmp", f"{job.id}/agent")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    usage = deepcopy(job.usage or {})
    deadline = time.monotonic()

    def usage_delta(delta):
        nonlocal usage
        usage = record_usage(job.id, token, _safe_log_value(delta, secret))

    def renew():
        while not done.wait(min(2, settings.lease_seconds / 3)):
            try:
                if not heartbeat(job.id, token):
                    cancelled.set()
                    return
                remaining = store.tick()
                if remaining["remaining_seconds"] <= 0 or time.monotonic() >= deadline:
                    exhausted.set()
                    cancelled.set()
                    return
            except Exception:
                cancelled.set()
                return

    try:
        saved = store.start()
        deadline = time.monotonic() + saved["remaining_seconds"]
        if saved["remaining_seconds"] <= 0 or saved["remaining_tool_calls"] <= 0:
            raise AgentBudgetExhausted("本次分配已用完，已保存成果；继续任务可追加一份额度。")
        guard = threading.Thread(target=renew, name="pi-task-lease", daemon=True)
        guard.start()
        anchors = _copy_legacy_sources(job, token, work_dir)
        if job.kind == "chat":
            spec, secret, cached = build_chat_spec(job, work_dir)
            goal = spec["chat"]["input"]["question"]
            pipeline_type = ChatPipeline
        else:
            spec, secret = build_spec(job, work_dir)
            cached = {}
            goal = spec.get("instruction") or "完整解读这篇论文：解释研究问题、方法、实验、结论、局限，覆盖正文、附录与图表并核验全部证据。"
            pipeline_type = Pipeline
        spec["execution_engine"] = ENGINE
        spec.pop("phase", None)
        spec["limits"] = {"timeout_seconds": saved["remaining_seconds"], "max_tool_calls": saved["remaining_tool_calls"], "max_related_papers": 3}
        spec["agent"] = {"operation": "run", "goal": goal, "cross_paper_requested": cross_paper_requested(goal),
                         "state": saved["state"], "messages": saved["messages"], "summary": saved["summary"], "tools": []}
        store.mutate("goal_loaded", {}, lambda state: state.update(goal=goal) or {"saved": True})
        pipeline = pipeline_type(work_dir, Path(spec["skill_root"]), spec["paper"], spec["skill_id"],
            job.workspace_id, cancelled, settings.project_dir, bool(spec["model"].get("vision")),
            max_tool_calls=500, workflow_mode="guided", rules_version=spec["source_rules_version"])
        if job.kind == "chat":
            pipeline.cached_documents.update(cached)
            # Retained previous answer sources remain available by their exact IDs.
            # Their activation still checks the existing three-related-source cap.
        host = AgentHost(job, token, spec, pipeline, store, cancelled, deadline, secret, usage_delta)
        if anchors and hasattr(host.tools, "import_legacy"):
            host.tools.import_legacy(anchors)
        host.sync_draft()
        saved_submission = store.load()["state"].get("submission")
        if saved_submission and saved_submission.get("accepted"):
            output = {"status": "completed", "submission": saved_submission}
        else:
            output = host.run_process(host.request_spec())
        if output.get("status") != "completed":
            store.wait(output.get("reason") or "Agent 已保存进度，任务需要继续。")
            return
        pipeline.agent_submission = output.get("submission")
        result = host.tools.final_result(pipeline.agent_submission)
        host.check()
        if job.kind == "chat":
            publish_chat_result(job.id, token, result, pipeline, usage)
        else:
            publish_result(job.id, token, result, pipeline, usage)
        published = True
    except (AgentBudgetExhausted, BudgetExhausted) as exc:
        try:
            store.wait(_safe_log_value(str(exc), secret))
        except AgentLeaseLost:
            finish_failure(job.id, token, "Task cancelled", cancelled=True)
    except (TaskCancelled, AgentLeaseLost):
        if worker.stopping.is_set():
            release_for_shutdown(job.id, token)
        elif exhausted.is_set():
            try:
                store.wait("本次时间额度已用完，成果已保存，可以继续。")
            except AgentLeaseLost:
                finish_failure(job.id, token, "Task cancelled", cancelled=True)
        else:
            finish_failure(job.id, token, "Task cancelled", cancelled=True)
    except Exception as exc:
        message = str(exc) if isinstance(exc, (PipelineError, SourceError, ValueError, OSError)) else "Agent 执行中断；已保存成果，等待继续并查看服务诊断。"
        try:
            store.wait(_safe_log_value(message, secret)[:2000])
        except AgentLeaseLost:
            finish_failure(job.id, token, "Task cancelled", cancelled=True)
        LOG.warning("Pi task %s interrupted (%s)", job.id, type(exc).__name__)
    finally:
        done.set()
        if guard:
            guard.join(timeout=3)
        worker.current_cancel = None
        if published:
            shutil.rmtree(work_dir.parent, ignore_errors=True)
        worker.cleanup_terminal_artifacts()
