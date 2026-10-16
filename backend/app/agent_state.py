"""Transactional logical task storage, independent of worker process attempts.

The Job row is the lock and lease authority. Every private state transition and
journal append commits together. Only host code calls this module; model tool
arguments never select task IDs, grant budgets, or write trusted review records.
"""
from copy import deepcopy
import json
from typing import Any, Callable

from sqlalchemy import select

from .db import SessionLocal
from .models import AgentTask, AgentTaskEvent, ChatMessage, Job, JobEvent, now

ENGINE = "pi-agent-v1"
CONTINUABLE = ("waiting", "cancelled", "failed")


class AgentLeaseLost(RuntimeError):
    pass


class AgentBudgetExhausted(RuntimeError):
    pass


def allocation(kind):
    return (600, 120) if kind == "chat" else (3600, 500)


def agent_snapshot(inputs, kind="read"):
    """Set only engine-owned fields; preserve parser and immutable input identity."""
    inputs = deepcopy(inputs)
    seconds, calls = allocation(kind)
    inputs["execution_engine"] = ENGINE
    inputs["workflow_limits"] = {"timeout_seconds": seconds, "max_tool_calls": calls,
                                  "max_related_papers": 3, "request_timeout_seconds": 300}
    return inputs


def ensure_agent_task(db, job, goal=None):
    if job.execution_engine != ENGINE:
        return None
    task = db.get(AgentTask, job.id)
    if task is None:
        seconds, calls = allocation(job.kind)
        task = AgentTask(job_id=job.id, state={"goal": goal or job.input_snapshot.get("instruction") or "完整解读论文并核验结论与证据",
                                             "current_action": "等待 Agent 开始", "accepted_work": [],
                                             "remaining_work": [], "coverage": {}, "issue": None},
                         messages=[], summary="", granted_seconds=seconds, granted_tool_calls=calls,
                         elapsed_seconds=0, tool_calls=0, allocations=1, sequence=0)
        db.add(task)
        db.flush()
        _append(db, task, "created", {"seconds": seconds, "tool_calls": calls})
    return task


def _append(db, task, event_type, data, result=None, request_id=None):
    task.sequence += 1
    db.add(AgentTaskEvent(job_id=task.job_id, sequence=task.sequence, event_type=event_type,
                         data=deepcopy(data), result=deepcopy(result), request_id=request_id))
    task.updated_at = now()


def _charge(task, at):
    if task.clock_at is not None:
        # A crash gives no reliable later stop time. Conservatively charge the
        # unresolved interval, capped at the allocation, instead of resetting it.
        delta = max(0.0, (at - task.clock_at).total_seconds())
        task.elapsed_seconds = min(task.granted_seconds, task.elapsed_seconds + delta)
        task.clock_at = at


def stop_task_clock(db, job):
    """Worker terminal publication/cancellation hook, inside its fenced transaction."""
    task = db.get(AgentTask, job.id)
    if task:
        _charge(task, now())
        task.clock_at = task.clock_lease_token = None


def grant_continuation(db, job):
    """API-only: caller holds the reading/conversation and Job locks."""
    task = ensure_agent_task(db, job)
    if task is None or job.status not in CONTINUABLE:
        raise ValueError("Only waiting, cancelled or failed Agent tasks can continue")
    if task.clock_at is not None:
        # A terminal worker that predates the new clock hook may leave a clock.
        # Charge through the recorded stop, never the user's paused interval.
        _charge(task, max(task.clock_at, job.updated_at))
    task.clock_at = task.clock_lease_token = None
    seconds, calls = allocation(job.kind)
    task.granted_seconds += seconds
    task.granted_tool_calls += calls
    task.allocations += 1
    task.state = {**task.state, "issue": None, "current_action": "已安排 Agent 继续"}
    job.status, job.stage, job.error = "queued", "queued", None
    job.cancel_requested = False
    job.lease_token = job.lease_expires_at = None
    job.updated_at = now()
    _append(db, task, "continued", {"seconds": seconds, "tool_calls": calls, "allocation": task.allocations})
    db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type="progress",
                    data={"stage": "queued", "message": "Agent 将从已保存的成果继续。", "execution_engine": ENGINE}))
    return task


def public_projection(job, task):
    state = task.state if task and isinstance(task.state, dict) else {}
    seconds, calls = allocation(job.kind)
    elapsed = float(task.elapsed_seconds) if task else 0.0
    # This read-only live estimate never grants time; durable ticks own accounting.
    if task and task.clock_at and job.status == "running":
        elapsed = min(task.granted_seconds, elapsed + max(0.0, (now() - task.clock_at).total_seconds()))
    def strings(key):
        return [item for item in state.get(key, []) if isinstance(item, str)] if isinstance(state.get(key), list) else []
    return {"goal": str(state.get("goal") or job.input_snapshot.get("instruction") or "完整解读论文并核验结论与证据"),
            "current_action": str(state.get("current_action") or job.stage or "等待 Agent 开始"),
            "accepted_work": strings("accepted_work"), "issue": str(state.get("issue") or job.error) if state.get("issue") or job.error else None,
            "coverage": deepcopy(state.get("coverage")) if isinstance(state.get("coverage"), dict) else {},
            "remaining_work": strings("remaining_work"), "elapsed_seconds": round(elapsed, 3),
            "remaining_seconds": round(max(0.0, (task.granted_seconds if task else seconds) - elapsed), 3),
            "tool_calls": task.tool_calls if task else 0,
            "remaining_tool_calls": max(0, (task.granted_tool_calls - task.tool_calls) if task else calls),
            "can_continue": job.status in CONTINUABLE}


class AgentStore:
    def __init__(self, job_id: str, lease_token: str):
        self.job_id = job_id
        self.lease_token = lease_token

    def _locked(self, db):
        job = db.scalar(select(Job).where(Job.id == self.job_id).with_for_update())
        if (not job or job.execution_engine != ENGINE or job.status != "running"
                or not self.lease_token or job.lease_token != self.lease_token
                or job.lease_expires_at is None or job.lease_expires_at <= now()):
            raise AgentLeaseLost("Agent task lease is no longer owned")
        return job, ensure_agent_task(db, job)

    @staticmethod
    def _value(task):
        return {"state": deepcopy(task.state), "messages": deepcopy(task.messages), "summary": task.summary,
                "remaining_seconds": max(0.0, task.granted_seconds - task.elapsed_seconds),
                "remaining_tool_calls": max(0, task.granted_tool_calls - task.tool_calls),
                "elapsed_seconds": task.elapsed_seconds, "tool_calls": task.tool_calls}

    def load(self) -> dict:
        with SessionLocal() as db, db.begin():
            _, task = self._locked(db)
            return self._value(task)

    def start(self) -> dict:
        with SessionLocal() as db, db.begin():
            _, task = self._locked(db)
            previous = task.clock_lease_token
            _charge(task, now())
            task.clock_at, task.clock_lease_token = now(), self.lease_token
            _append(db, task, "resumed" if previous and previous != self.lease_token else "started",
                    {"elapsed_seconds": task.elapsed_seconds})
            return self._value(task)

    def tick(self) -> dict:
        with SessionLocal() as db, db.begin():
            _, task = self._locked(db)
            _charge(task, now())
            _append(db, task, "budget_tick", {"elapsed_seconds": task.elapsed_seconds})
            return self._value(task)

    def mutate(self, event_type: str, data: dict, change: Callable[[dict], Any], request_id=None) -> Any:
        if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 180):
            raise ValueError("Invalid request ID")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 80:
            raise ValueError("Invalid event type")
        with SessionLocal() as db, db.begin():
            job, task = self._locked(db)
            if job.cancel_requested:
                raise AgentLeaseLost("Agent task cancellation was requested")
            if request_id:
                existing = db.scalar(select(AgentTaskEvent).where(AgentTaskEvent.job_id == self.job_id,
                                                                 AgentTaskEvent.request_id == "effect:" + request_id))
                if existing:
                    if existing.event_type != event_type:
                        raise ValueError("Request ID already belongs to another operation")
                    return deepcopy(existing.result)
            state = deepcopy(task.state)
            result = change(state)
            # Validate before SQLAlchemy binding, so no partial journal/state writes.
            json.dumps({"state": state, "data": data, "result": result}, allow_nan=False)
            task.state = state
            _append(db, task, event_type, data, result, "effect:" + request_id if request_id else None)
            return deepcopy(result)

    def consume(self, call_id: str, name: str) -> dict:
        if not isinstance(call_id, str) or not call_id or len(call_id) > 180 or not isinstance(name, str) or not name:
            raise ValueError("Tool reservation needs a valid call ID and name")
        exhausted = False
        with SessionLocal() as db, db.begin():
            job, task = self._locked(db)
            if job.cancel_requested:
                raise AgentLeaseLost("Agent task cancellation was requested")
            existing = db.scalar(select(AgentTaskEvent).where(AgentTaskEvent.job_id == self.job_id,
                                                             AgentTaskEvent.request_id == "attempt:" + call_id))
            if existing:
                if existing.data.get("name") != name:
                    raise ValueError("Tool call ID already reserved for another tool")
                return deepcopy(existing.result)
            _charge(task, now())
            if task.elapsed_seconds >= task.granted_seconds or task.tool_calls >= task.granted_tool_calls:
                exhausted = True
            else:
                task.tool_calls += 1
                result = {"reserved": True, "call_id": call_id, "name": name,
                          "remaining_seconds": max(0.0, task.granted_seconds - task.elapsed_seconds),
                          "tool_calls": task.tool_calls, "remaining_tool_calls": task.granted_tool_calls - task.tool_calls}
                _append(db, task, "tool_attempt", {"name": name, "call_id": call_id}, result, "attempt:" + call_id)
        if exhausted:
            raise AgentBudgetExhausted("Agent allocation exhausted; saved work can be continued")
        return result

    def checkpoint(self, messages, summary=None) -> dict:
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise ValueError("Checkpoint messages must be complete message objects")
        for message in messages:
            if message.get("role") == "assistant" and message.get("stopReason") in {"length", "error", "aborted"}:
                raise ValueError("Incomplete assistant messages cannot enter a trusted checkpoint")
            for item in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if isinstance(item, dict) and item.get("type") == "toolCall" and not isinstance(item.get("arguments"), dict):
                    raise ValueError("Incomplete tool arguments cannot enter a trusted checkpoint")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("Checkpoint summary must be text")
        if len(json.dumps(messages, allow_nan=False).encode()) > 32 * 1024 * 1024:
            raise ValueError("Checkpoint exceeds storage limit; compact the context")
        with SessionLocal() as db, db.begin():
            _, task = self._locked(db)
            task.messages = deepcopy(messages)
            if summary is not None:
                task.summary = summary
            _append(db, task, "checkpoint", {"message_count": len(messages), "compacted": summary is not None})
            return self._value(task)

    def wait(self, reason: str) -> None:
        reason = str(reason)[:2000]
        with SessionLocal() as db, db.begin():
            job, task = self._locked(db)
            if job.cancel_requested:
                raise AgentLeaseLost("Agent task cancellation was requested")
            _charge(task, now())
            task.clock_at = task.clock_lease_token = None
            task.state = {**task.state, "issue": reason, "current_action": "待继续"}
            job.status, job.stage, job.error = "waiting", "waiting", reason
            job.lease_token = job.lease_expires_at = None
            job.updated_at = now()
            if job.kind == "chat":
                message = db.scalar(select(ChatMessage).where(ChatMessage.job_id == job.id, ChatMessage.role == "assistant"))
                if message:
                    message.status, message.error = "waiting", reason
            _append(db, task, "waiting", {"reason": reason})
            db.add(JobEvent(job_id=job.id, workspace_id=job.workspace_id, event_type="waiting",
                            data={"status": "waiting", "message": reason, "execution_engine": ENGINE,
                                  "agent_task": public_projection(job, task)}))
