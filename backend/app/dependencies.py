from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, SessionRecord, Membership, now
from .security import token_hash
from .config import get_settings

COOKIE_NAME = "paper_reader_session"

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Please sign in")
    session = db.get(SessionRecord, token_hash(token))
    if not session or session.expires_at <= now():
        raise HTTPException(401, "Session expired")
    user = db.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(401, "Account unavailable")
    return user

def platform_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Platform administrator required")
    return user

def membership(db: Session, wid: str, user: User, admin: bool = False) -> Membership:
    member = db.scalar(select(Membership).where(Membership.workspace_id == wid, Membership.user_id == user.id))
    if not member:
        raise HTTPException(404, "Workspace not found")
    if admin and member.role != "admin":
        raise HTTPException(403, "Workspace administrator required")
    return member

def require_origin(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    # SameSite alone does not protect against malicious sibling subdomains.
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request rejected")
    origin = request.headers.get("origin")
    expected = get_settings().public_origin or str(request.base_url).rstrip("/")
    if not origin or origin == "null":
        raise HTTPException(403, "Origin header required for state changes")
    def normalized(value):
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname, port
    try:
        matches = normalized(origin) == normalized(expected)
    except ValueError:
        matches = False
    if not matches:
        raise HTTPException(403, "Origin does not match this service")
