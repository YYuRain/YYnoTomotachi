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


class PromptOverride(Base):
    """每用户 prompt 末尾的追加指令（feedback agent 沉淀的偏好）。

    PRD：用户独立 prompt + Feedback Sub-Agent。装配 system prompt 时在 dynamic block
    后面追加所有 status='active' 的 text。
    """
    __tablename__ = "prompt_overrides"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    text = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    source_user_msg = Column(Text, nullable=True)
    source_skill_id = Column(Integer, nullable=True)  # 复用 skill 库时指向 skills.id
    risk_level = Column(String, nullable=False, default="low")
    status = Column(String, nullable=False, default="active")  # pending|active|disabled|rejected
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    approved_by = Column(BigInteger, nullable=True)  # admin chat_id；自动 active 时填 0
    approved_at = Column(DateTime, nullable=True)
    __table_args__ = (Index("ix_prompt_overrides_user_status", "user_id", "status"),)


class Skill(Base):
    """通用化的 prompt 片段，跨用户复用。embedding 存 JSON list[float]。"""
    __tablename__ = "skills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    embedding = Column(Text, nullable=False)  # JSON list[float]，bge-small-zh 512 维
    created_by = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    usage_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default="active")  # active | disabled


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


# ============ PromptOverride / Skill helpers ============

def list_active_overrides(user_id: int) -> list[PromptOverride]:
    """该用户当前生效（status='active'）的 prompt overrides，按 created_at 升序。"""
    with session() as s:
        return list(
            s.query(PromptOverride)
            .filter(PromptOverride.user_id == user_id, PromptOverride.status == "active")
            .order_by(PromptOverride.created_at.asc())
            .all()
        )


def list_overrides(
    user_id: int | None = None, status: str | None = None, limit: int = 200
) -> list[PromptOverride]:
    with session() as s:
        q = s.query(PromptOverride)
        if user_id is not None:
            q = q.filter(PromptOverride.user_id == user_id)
        if status:
            q = q.filter(PromptOverride.status == status)
        return list(q.order_by(PromptOverride.created_at.desc()).limit(limit).all())


def add_override(
    *,
    user_id: int,
    text: str,
    reason: str | None = None,
    source_user_msg: str | None = None,
    source_skill_id: int | None = None,
    risk_level: str = "low",
    status: str = "active",
    approved_by: int | None = None,
) -> int:
    now = datetime.utcnow()
    with session() as s:
        o = PromptOverride(
            user_id=user_id,
            text=text,
            reason=reason,
            source_user_msg=source_user_msg,
            source_skill_id=source_skill_id,
            risk_level=risk_level,
            status=status,
            created_at=now,
            updated_at=now,
            approved_by=approved_by if status == "active" else None,
            approved_at=now if status == "active" else None,
        )
        s.add(o)
        s.commit()
        return int(o.id)


def set_override_status(override_id: int, status: str, *, approved_by: int | None = None) -> bool:
    """改 status：pending → active|rejected, active → disabled."""
    if status not in ("pending", "active", "disabled", "rejected"):
        raise ValueError(f"bad status: {status}")
    now = datetime.utcnow()
    with session() as s:
        o = s.query(PromptOverride).filter(PromptOverride.id == override_id).first()
        if o is None:
            return False
        o.status = status
        o.updated_at = now
        if status == "active":
            o.approved_by = approved_by or 0
            o.approved_at = now
        s.commit()
        return True


# ----- skills -----

def add_skill(
    *,
    name: str,
    summary: str,
    body: str,
    embedding: list[float],
    created_by: int,
) -> int:
    import json as _json
    with session() as s:
        sk = Skill(
            name=name,
            summary=summary,
            body=body,
            embedding=_json.dumps(embedding),
            created_by=created_by,
            created_at=datetime.utcnow(),
        )
        s.add(sk)
        s.commit()
        return int(sk.id)


def list_skills(*, status: str | None = "active", limit: int = 200) -> list[Skill]:
    with session() as s:
        q = s.query(Skill)
        if status:
            q = q.filter(Skill.status == status)
        return list(q.order_by(Skill.usage_count.desc(), Skill.created_at.desc()).limit(limit).all())


def get_skill(skill_id: int) -> Skill | None:
    with session() as s:
        return s.query(Skill).filter(Skill.id == skill_id).first()


def bump_skill_usage(skill_id: int) -> None:
    now = datetime.utcnow()
    with session() as s:
        sk = s.query(Skill).filter(Skill.id == skill_id).first()
        if sk is None:
            return
        sk.usage_count = int(sk.usage_count or 0) + 1
        sk.last_used_at = now
        s.commit()


def set_skill_status(skill_id: int, status: str) -> bool:
    if status not in ("active", "disabled"):
        raise ValueError(f"bad status: {status}")
    with session() as s:
        sk = s.query(Skill).filter(Skill.id == skill_id).first()
        if sk is None:
            return False
        sk.status = status
        s.commit()
        return True


def top_skills_by_embedding(query_vec: list[float], k: int = 3) -> list[tuple[Skill, float]]:
    """全表 cosine top-k（量小够用）。返回 [(skill, similarity), ...] 降序。"""
    import json as _json, math
    if not query_vec:
        return []
    qnorm = math.sqrt(sum(x * x for x in query_vec)) or 1.0
    out: list[tuple[Skill, float]] = []
    with session() as s:
        rows = s.query(Skill).filter(Skill.status == "active").all()
        for sk in rows:
            try:
                v = _json.loads(sk.embedding)
            except Exception:
                continue
            if not v or len(v) != len(query_vec):
                continue
            dot = sum(a * b for a, b in zip(query_vec, v))
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            sim = dot / (qnorm * vn)
            out.append((sk, sim))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:k]
