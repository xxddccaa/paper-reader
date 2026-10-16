from functools import lru_cache
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session
from .config import get_settings

class Base(DeclarativeBase):
    pass

@lru_cache
def engine():
    url = get_settings().database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)

def SessionLocal() -> Session:
    return Session(engine(), expire_on_commit=False)

def get_db():
    with SessionLocal() as session:
        yield session
