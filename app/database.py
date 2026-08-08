from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.core.config import get_settings

class Base(DeclarativeBase): pass
engine = create_engine(get_settings().database_url, connect_args={"check_same_thread": False} if get_settings().database_url.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)
def get_db():
    with SessionLocal() as db: yield db
