from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from .api import router
from .chat_api import router as chat_router
from .config import PROJECT_DIR
from .db import SessionLocal

@asynccontextmanager
async def lifespan(app):
    from .logging_config import configure_logging
    configure_logging("api")
    logging.getLogger(__name__).info("Paper Reader API started")
    yield

app = FastAPI(lifespan=lifespan, title="Paper Reader", version="0.1.0", description="Evidence-based reading, tenant-scoped skills, durable jobs and revisions.")
app.include_router(router)
app.include_router(chat_router)

@app.exception_handler(RequestValidationError)
async def safe_validation_errors(request: Request, exc: RequestValidationError):
    # Pydantic's default "input" may contain a complete credential-bearing body.
    details = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": details})

@app.middleware("http")
async def secure_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/api/v1/health")
def health():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "Database unavailable")
    return {"ok": True}

_static = Path(os.environ.get("PAPER_READER_FRONTEND_DIR", PROJECT_DIR / "web" / "dist")).expanduser().resolve()
if (_static / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "API route not found")
    candidate = (_static / path).resolve()
    if candidate.is_relative_to(_static.resolve()) and candidate.is_file():
        return FileResponse(candidate)
    if (_static / "index.html").is_file():
        return FileResponse(_static / "index.html")
    raise HTTPException(404, "Frontend has not been built")
