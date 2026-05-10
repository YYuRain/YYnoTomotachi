from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column, DateTime, Float, Integer, String, Text, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Interest(Base):
    __tablename__ = "interests"
    topic = Column(String, primary_key=True)
    heat = Column(Float, nullable=False, default=0.0)
    last_touch = Column(DateTime, nullable=False, default=datetime.utcnow)


class ReplySample(Base):
    __tablename__ = "reply_samples"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    weekday = Column(Integer, nullable=False)  # 0=Mon..6=Sun
    hour = Column(Integer, nullable=False)     # 0..23
    replied_within_sec = Column(Integer, nullable=True)


class LastInteraction(Base):
    __tablename__ = "last_interaction"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)


class ProactiveFire(Base):
    """AI 主动发起（开场）的历史，用于节流 / 每日上限 / 记录最近触发时间。"""
    __tablename__ = "proactive_fires"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    why = Column(String, nullable=True)
    user_probably_doing = Column(String, nullable=True)
    opener_angle = Column(String, nullable=True)
    opener_text = Column(String, nullable=True)


# 为未来的人格演化预留
class PersonaSnapshot(Base):
    __tablename__ = "persona_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    payload_json = Column(Text, nullable=False)


_engine = None
_Session = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{settings().app_db_path}", future=True)
        Base.metadata.create_all(_engine)
    return _engine


def session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _Session()
