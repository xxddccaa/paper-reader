"""Private, revision-bound follow-up conversations and replayable answer streams."""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import schemas as S
from .api import ACTIVE, TERMINAL, owned, snapshot, model_out
from .agent_state import ENGINE, agent_snapshot, ensure_agent_task, grant_continuation
from .config import get_settings
from .db import get_db, SessionLocal
from .dependencies import COOKIE_NAME, current_user, membership, require_origin
from .models import ChatConversation, ChatMessage, Job, JobEvent, Membership, ModelConnection, Reading, Revision, SessionRecord, User, now
from .security import token_hash

router = APIRouter(prefix="/api/v1/workspaces/{wid}", dependencies=[Depends(require_origin)])


def private_conversation(db, wid, cid, user, *, lock=False):
    membership(db, wid, user)
    stmt = select(ChatConversation).where(ChatConversation.id == cid, ChatConversation.workspace_id == wid, ChatConversation.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    conversation = db.scalar(stmt)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    return conversation


def conversation_message(db, conversation, mid):
    message = db.scalar(select(ChatMessage).where(ChatMessage.id == mid, ChatMessage.conversation_id == conversation.id, ChatMessage.workspace_id == conversation.workspace_id))
    if not message:
        raise HTTPException(404, "Message not found")
    return message


def reject_active_response(db, conversation):
    # Consult both durable records so a partially recovered job cannot race a send.
    active_message = db.scalar(select(ChatMessage.id).where(ChatMessage.conversation_id == conversation.id, ChatMessage.workspace_id == conversation.workspace_id, ChatMessage.role == "assistant", ChatMessage.status.in_(ACTIVE)).limit(1))
    active_job = db.scalar(select(Job.id).where(Job.conversation_id == conversation.id, Job.workspace_id == conversation.workspace_id, Job.status.in_(ACTIVE)).limit(1))
    if active_message or active_job:
        raise HTTPException(409, "This conversation already has an active answer")


def enqueue_answer(db, conversation, user_message, assistant_message, user, *, retry_of=None):
    reading = owned(db, Reading, conversation.reading_id, conversation.workspace_id)
    revision = owned(db, Revision, conversation.revision_id, conversation.workspace_id)
    if revision.reading_id != reading.id:
        raise HTTPException(404, "Revision not found")
    inputs = snapshot(db, reading, base_revision_id=revision.id)
    inputs["chat"] = {"conversation_id": conversation.id, "message_id": assistant_message.id, "user_message_id": user_message.id}
    if get_settings().pi_agent_enabled:
        inputs = agent_snapshot(inputs, "chat")
    else:
        inputs["workflow_limits"] = {"timeout_seconds": 300, "max_tool_calls": 48, "max_related_papers": 3}
    if retry_of:
        inputs["retry_of_job_id"] = retry_of
    job = Job(workspace_id=conversation.workspace_id, reading_id=reading.id, conversation_id=conversation.id, created_by=user.id, kind="chat", input_snapshot=inputs)
    db.add(job)
    db.flush()
    ensure_agent_task(db, job, goal=user_message.content)
    assistant_message.job_id = job.id
    db.add(JobEvent(job_id=job.id, workspace_id=conversation.workspace_id, event_type="progress", data={"message_id": assistant_message.id, "stage": "queued", "message": "追问已排队，等待生成回答。"}))
    conversation.updated_at = now()
    return job


@router.get("/readings/{rid}/conversations", response_model=list[S.ConversationOut])
def conversations(wid: str, rid: str, limit: int = Query(default=100, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    owned(db, Reading, rid, wid)
    return list(db.scalars(select(ChatConversation).where(ChatConversation.workspace_id == wid, ChatConversation.reading_id == rid, ChatConversation.user_id == user.id).order_by(ChatConversation.updated_at.desc(), ChatConversation.id).limit(limit)))


@router.post("/readings/{rid}/conversations", response_model=S.ConversationOut, status_code=201)
def create_conversation(wid: str, rid: str, data: S.ConversationIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    membership(db, wid, user)
    owned(db, Reading, rid, wid)
    revision = owned(db, Revision, data.revision_id, wid)
    if revision.reading_id != rid:
        raise HTTPException(404, "Revision not found")
    conversation = ChatConversation(workspace_id=wid, reading_id=rid, user_id=user.id, revision_id=revision.id, title=data.title.strip() or "新对话")
    db.add(conversation)
    db.commit()
    return conversation


@router.get("/conversations/{cid}", response_model=S.ConversationDetail)
def conversation_detail(wid: str, cid: str, before: int | None = Query(default=None, ge=1), limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user)
    stmt = select(ChatMessage).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid)
    if before is not None:
        stmt = stmt.where(ChatMessage.sequence < before)
    rows = list(db.scalars(stmt.order_by(ChatMessage.sequence.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    messages = list(reversed(rows[:limit]))
    return {"conversation": conversation, "messages": messages, "next_before": messages[0].sequence if has_more else None}


@router.post("/conversations/{cid}/messages", response_model=S.ChatMessageCreated, status_code=202)
def send_message(wid: str, cid: str, data: S.ChatMessageIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user, lock=True)
    previous = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid, ChatMessage.request_id == str(data.client_request_id)))
    if previous:
        if previous.role != "user" or previous.content != data.content:
            raise HTTPException(409, "This request ID was already used for another question")
        answer = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid, ChatMessage.sequence == previous.sequence + 1, ChatMessage.role == "assistant"))
        if not answer or not answer.job_id:
            raise HTTPException(409, "The original answer is unavailable")
        return {"user_message": previous, "assistant_message": answer, "job_id": answer.job_id}
    reject_active_response(db, conversation)
    sequence = (db.scalar(select(func.max(ChatMessage.sequence)).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid)) or 0) + 1
    question = ChatMessage(conversation_id=cid, workspace_id=wid, role="user", sequence=sequence, content=data.content, status="succeeded", request_id=str(data.client_request_id))
    answer = ChatMessage(conversation_id=cid, workspace_id=wid, role="assistant", sequence=sequence + 1, status="queued")
    db.add_all([question, answer])
    db.flush()
    job = enqueue_answer(db, conversation, question, answer, user)
    if sequence == 1 and conversation.title == "新对话":
        conversation.title = data.content.strip()[:80]
    db.commit()
    return {"user_message": question, "assistant_message": answer, "job_id": job.id}


@router.post("/conversations/{cid}/messages/{mid}/cancel", response_model=S.ChatMessageOut)
def cancel_message(wid: str, cid: str, mid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user, lock=True)
    message = conversation_message(db, conversation, mid)
    if message.role != "assistant" or not message.job_id:
        raise HTTPException(409, "Only assistant answers can be cancelled")
    job = db.scalar(select(Job).where(Job.id == message.job_id, Job.workspace_id == wid, Job.conversation_id == cid, Job.kind == "chat").with_for_update())
    if not job:
        raise HTTPException(404, "Answer task not found")
    db.refresh(message)
    if job.status in ACTIVE:
        job.cancel_requested = True
        if job.status == "queued":
            job.status, job.stage = "cancelled", "cancelled"
            message.status = "cancelled"
            message.error = "回答已停止。"
            db.flush()
            db.add(JobEvent(job_id=job.id, workspace_id=wid, event_type="cancelled", data={"message": S.ChatMessageOut.model_validate(message).model_dump(mode="json")}))
        else:
            db.add(JobEvent(job_id=job.id, workspace_id=wid, event_type="progress", data={"message_id": mid, "stage": "cancelling", "message": "正在停止回答。"}))
        conversation.updated_at = now()
        db.commit()
    return message


@router.post("/conversations/{cid}/messages/{mid}/retry", response_model=S.ChatRetryCreated, status_code=202)
def retry_message(wid: str, cid: str, mid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user, lock=True)
    message = conversation_message(db, conversation, mid)
    if message.role != "assistant" or message.status not in ("failed", "cancelled", "partial"):
        raise HTTPException(409, "Only failed, cancelled or partial answers can be retried")
    reject_active_response(db, conversation)
    latest = db.scalar(select(func.max(ChatMessage.sequence)).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid))
    if message.sequence != latest:
        raise HTTPException(409, "Only the latest answer can be retried")
    question = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid, ChatMessage.sequence == message.sequence - 1, ChatMessage.role == "user"))
    if not question:
        raise HTTPException(409, "Original question is unavailable")
    old_job_id = message.job_id
    message.status, message.content, message.error = "queued", "", None
    message.evidence_index, message.verification, message.usage, message.artifacts = [], {}, {}, []
    job = enqueue_answer(db, conversation, question, message, user, retry_of=old_job_id)
    db.commit()
    return {"assistant_message": message, "job_id": job.id}


@router.post("/conversations/{cid}/messages/{mid}/continue", response_model=S.ChatRetryCreated, status_code=202)
def continue_message(wid: str, cid: str, mid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user, lock=True)
    message = conversation_message(db, conversation, mid)
    if message.role != "assistant" or not message.job_id:
        raise HTTPException(409, "Only stopped assistant answers can continue")
    old = db.scalar(select(Job).where(Job.id == message.job_id, Job.workspace_id == wid,
                                     Job.conversation_id == cid, Job.created_by == user.id, Job.kind == "chat").with_for_update())
    if not old:
        raise HTTPException(404, "Answer task not found")
    db.refresh(message)
    allowed = ("waiting", "failed", "cancelled") if old.execution_engine == ENGINE else ("failed", "partial", "cancelled")
    if message.status not in allowed or old.status not in allowed:
        raise HTTPException(409, "Only a stopped answer can continue")
    reject_active_response(db, conversation)
    latest = db.scalar(select(func.max(ChatMessage.sequence)).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid))
    if message.sequence != latest:
        raise HTTPException(409, "Only the latest answer can continue")
    question = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid,
                                                  ChatMessage.sequence == message.sequence - 1, ChatMessage.role == "user"))
    if not question:
        raise HTTPException(409, "Original question is unavailable")
    reading = owned(db, Reading, conversation.reading_id, wid)
    if old.reading_id != reading.id or old.input_snapshot.get("base_revision_id") != conversation.revision_id:
        raise HTTPException(409, "Answer revision no longer matches the conversation")
    model = owned(db, ModelConnection, reading.model_connection_id, wid)
    if old.execution_engine == ENGINE:
        inputs = agent_snapshot(old.input_snapshot, "chat")
        inputs["model"] = {k: v for k, v in model_out(model).items() if k != "has_api_key"}
        old.input_snapshot = inputs
        grant_continuation(db, old)
        job = old
    else:
        # Preserve the original Job and pair. A pending draft is not verified history.
        inputs = agent_snapshot(old.input_snapshot, "chat")
        inputs["retry_from_job_id"] = old.id
        inputs["retry_of_job_id"] = old.id
        inputs["model"] = {k: v for k, v in model_out(model).items() if k != "has_api_key"}
        job = Job(workspace_id=wid, reading_id=reading.id, conversation_id=cid, created_by=user.id,
                  kind="chat", input_snapshot=inputs)
        db.add(job)
        db.flush()
        ensure_agent_task(db, job, goal=question.content)
        message.job_id = job.id
        db.add(JobEvent(job_id=job.id, workspace_id=wid, event_type="progress",
                        data={"message_id": mid, "stage": "queued", "message": "已交给 Agent 继续。"}))
    message.status, message.error = "queued", None
    conversation.updated_at = now()
    db.commit()
    return {"assistant_message": message, "job_id": job.id}


def public_event(event, message):
    """Drop internal host fields, tool arguments and reasoning from the public feed."""
    data = event.data if isinstance(event.data, dict) else {}
    if event.event_type in {"completed", "failed", "cancelled", "waiting"}:
        outcome = message.status if message.status in {"failed", "cancelled", "waiting"} else "completed" if message.status in TERMINAL else None
        if event.event_type != outcome:
            return None
        return {"message": S.ChatMessageOut.model_validate(message).model_dump(mode="json")}
    if event.event_type in {"delta", "replace"}:
        key = "text" if event.event_type == "delta" else "content"
        if not isinstance(data.get(key), str):
            return None
        attempt = data.get("attempt", 0)
        return {"message_id": message.id, key: data[key], "attempt": attempt if isinstance(attempt, int) else 0}
    if event.event_type == "progress":
        return {"message_id": message.id, "stage": str(data.get("stage", "running")), "message": str(data.get("message", ""))}
    return None


@router.get("/conversations/{cid}/messages/{mid}/events")
async def message_events(wid: str, cid: str, mid: str, request: Request, after: int = Query(default=0, ge=0), db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user)
    message = conversation_message(db, conversation, mid)
    if message.role != "assistant" or not message.job_id:
        raise HTTPException(404, "Answer task not found")
    try:
        header_cursor = int(request.headers.get("last-event-id", "0"))
        if header_cursor < 0:
            raise ValueError
        cursor = max(after, header_cursor)
    except ValueError:
        raise HTTPException(422, "Invalid Last-Event-ID") from None
    user_id, session_id, jid = user.id, token_hash(request.cookies[COOKIE_NAME]), message.job_id
    db.rollback()

    async def stream():
        nonlocal cursor
        sent_terminal = False
        while not await request.is_disconnected():
            with SessionLocal() as session:
                login = session.get(SessionRecord, session_id)
                account = session.get(User, user_id)
                member = session.scalar(select(Membership.id).where(Membership.workspace_id == wid, Membership.user_id == user_id))
                owner = session.scalar(select(ChatConversation.id).where(ChatConversation.id == cid, ChatConversation.workspace_id == wid, ChatConversation.user_id == user_id))
                if not login or login.user_id != user_id or login.expires_at <= now() or not account or not account.active or not member or not owner:
                    return
                answer = session.scalar(select(ChatMessage).where(ChatMessage.id == mid, ChatMessage.conversation_id == cid, ChatMessage.workspace_id == wid, ChatMessage.job_id == jid))
                job = session.scalar(select(Job).where(Job.id == jid, Job.workspace_id == wid, Job.conversation_id == cid, Job.created_by == user_id, Job.kind == "chat"))
                if not answer or not job:
                    return
                records = list(session.scalars(select(JobEvent).where(JobEvent.job_id == jid, JobEvent.workspace_id == wid, JobEvent.id > cursor).order_by(JobEvent.id).limit(100)))
                terminal = job.status in TERMINAL and answer.status in TERMINAL
                for event in records:
                    cursor = event.id
                    payload = public_event(event, answer)
                    if payload is None:
                        continue
                    if event.event_type in {"completed", "failed", "cancelled", "waiting"}:
                        sent_terminal = True
                    yield f"id: {event.id}\nevent: {event.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if terminal and len(records) < 100:
                    if not sent_terminal:
                        # A cursor may already be past the terminal record; still
                        # deliver the final snapshot so reconnect always converges.
                        kind = answer.status if answer.status in {"failed", "cancelled", "waiting"} else "completed"
                        payload = {"message": S.ChatMessageOut.model_validate(answer).model_dump(mode="json")}
                        yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    return
            if not records:
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations/{cid}/messages/{mid}/sources/{source_id:path}")
def message_source(wid: str, cid: str, mid: str, source_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    conversation = private_conversation(db, wid, cid, user)
    message = conversation_message(db, conversation, mid)
    item = next((entry for entry in message.artifacts if isinstance(entry, dict) and entry.get("source_id") == source_id), None)
    if not item or not isinstance(item.get("path"), str):
        raise HTTPException(404, "Source file not found")
    runtime = get_settings().runtime_dir.resolve()
    expected = runtime / "storage" / "workspaces" / wid / "conversations" / cid
    relative = Path(item["path"])
    path = (runtime / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(expected) or not path.is_file():
        raise HTTPException(404, "Source file not found")
    mime_type = item.get("mime_type")
    if mime_type not in {"application/pdf", "text/html", "text/plain"}:
        mime_type = "application/octet-stream"
    return FileResponse(path, media_type=mime_type, filename=Path(item.get("name") or path.name).name, content_disposition_type="inline" if mime_type == "application/pdf" else "attachment", headers={"Content-Security-Policy": "sandbox; default-src 'none'"})
