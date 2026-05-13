from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    BigInteger, Column, DateTime, Float, Index, Integer, PrimaryKeyConstraint,
    String, Text, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Interest(Base):
    __tablename__ = "interests"
    user_id = Column(BigInteger, nullable=False)
    topic = Column(String, nullable=False)
    heat = Column(Float, nullable=False, default=0.0)
    last_touch = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (PrimaryKeyConstraint("user_id", "topic"),)


class ReplySample(Base):
    __tablename__ = "reply_samples"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    weekday = Column(Integer, nullable=False)  # 0=Mon..6=Sun
    hour = Column(Integer, nullable=False)     # 0..23
    replied_within_sec = Column(Integer, nullable=True)
    __table_args__ = (Index("ix_reply_samples_user_wd_h", "user_id", "weekday", "hour"),)


class LastInteraction(Base):
    """每个用户一行；user_id 是主键。"""
    __tablename__ = "last_interaction"
    user_id = Column(BigInteger, primary_key=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)


class ProactiveFire(Base):
    """AI 主动发起（开场）的历史。每条带 user_id；用于节流 / 每日上限 / 最近触发时间。"""
    __tablename__ = "proactive_fires"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    why = Column(String, nullable=True)
    user_probably_doing = Column(String, nullable=True)
    opener_angle = Column(String, nullable=True)
    opener_text = Column(String, nullable=True)


class PersonaSnapshot(Base):
    """每个用户独立的 persona 动态层快照流（事件流，最新一行 = 当前状态）。"""
    __tablename__ = "persona_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow)
    payload_json = Column(Text, nullable=False)
    __table_args__ = (Index("ix_persona_snapshots_user_ts", "user_id", "ts"),)


class User(Base):
    """已注册用户。chat_id 即 user_id；status 用 'active' 标识。"""
    __tablename__ = "users"
    chat_id = Column(BigInteger, primary_key=True)
    status = Column(String, nullable=False, default="active")  # active | banned (预留)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    note = Column(String, nullable=True)  # admin 备注用
    # webUI 登录密码（明文，每用户一个；admin 走 env 不用这个字段）
    webui_password = Column(String, nullable=True)


class InviteCode(Base):
    """邀请码：admin 生成，未使用前 used_by/used_at 为空。"""
    __tablename__ = "invite_codes"
    code = Column(String, primary_key=True)
    created_by = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    used_by = Column(BigInteger, nullable=True)
    used_at = Column(DateTime, nullable=True)


_engine = None
_Session = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{settings().app_db_path}", future=True)
        Base.metadata.create_all(_engine)
        _ensure_columns(_engine)
    return _engine


def _ensure_columns(eng) -> None:
    """SQLite-friendly ALTER TABLE：给已有数据库补上模型新增的 nullable 列。
    Base.metadata.create_all 只建缺的表，不动已有表的列。"""
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    if "users" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("users")}
        if "webui_password" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN webui_password TEXT"))


def session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _Session()
