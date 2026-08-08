import os
os.environ.setdefault("APP_SECRET","test-secret-that-is-at-least-32-characters")
os.environ.setdefault("ENVIRONMENT","test")
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
@pytest.fixture
def db():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False}); Base.metadata.create_all(engine)
    with sessionmaker(engine,expire_on_commit=False)() as session: yield session
