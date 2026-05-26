from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    BigInteger, Column, DateTime, Float, Index, Integer, PrimaryKeyConstraint,
    String, Text, create_engine, event,
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
    """AI 主动发起（开场）的历史。每条带 user_id；用于节流 / 每日上限 / 最近触发时间。

    mode/platform 是 share-discovery 通道（2026-05-21）加的——
    mode='topic_chat'（默认，老路径）|'share_discovery'（带链接分享）；
    platform='xhs'|'bili'|'web'|None；用 (user_id, mode, platform, today) 算独立配额。
    """
    __tablename__ = "proactive_fires"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    why = Column(String, nullable=True)
    user_probably_doing = Column(String, nullable=True)
    opener_angle = Column(String, nullable=True)
    opener_text = Column(String, nullable=True)
    mode = Column(String, nullable=True)        # 'topic_chat' | 'share_discovery'
    platform = Column(String, nullable=True)    # 'xhs' | 'bili' | 'web' | None


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

    trigger_kind = 'passive'（默认）：仅作为指令注入 prompt，靠 bot 在主对话流里
        识别 trigger 关键词。
    trigger_kind = 'active'：除了 passive 注入外，还由 triggered_reach_job 按 cron
        定时扫描；time match 后跑 sonnet 判 condition_prompt 是否成立，成立则主动
        触达（用户在聊就暂存到 PendingReachMessage 让下一轮自然融入；不在聊直接发）。
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
    # active trigger 相关（trigger_kind='passive' 时为空）
    trigger_kind = Column(String, nullable=False, default="passive")  # passive | active
    cron_schedule = Column(String, nullable=True)  # APScheduler cron 表达式（CST），如 "30 17 * * 1-5"
    condition_prompt = Column(Text, nullable=True)  # 给 sonnet 的判定 + 消息生成 prompt
    last_fired_at = Column(DateTime, nullable=True)  # 上次主动触发时间（dedupe 用）
    __table_args__ = (Index("ix_prompt_overrides_user_status", "user_id", "status"),)


class PendingReachMessage(Base):
    """triggered_reach_job 生成的"应当主动告诉对方"的暂存消息。

    场景：trigger 到点 + condition 成立 → sonnet 已生成"等等明天昌平有雨..."这种话
    - 如果 user 正在聊（last_interaction < 5min）→ 暂存这条让下一轮 handle_user_message 融入
    - 如果 user 不在聊 → 直接 send，不进这表
    - 如果暂存超过 5min user 还没新 turn → 兜底 send + status='sent'
    """
    __tablename__ = "pending_reach_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    override_id = Column(Integer, nullable=False)
    message = Column(Text, nullable=False)
    expected_send_after = Column(DateTime, nullable=False)  # 兜底直发时间点
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    status = Column(String, nullable=False, default="pending")  # pending|merged|sent|expired
    __table_args__ = (Index("ix_pending_reach_user_status", "user_id", "status"),)


class UserPromptOverride(Base):
    """每用户对 `prompt/*.md` 的整份覆写。

    与 PromptOverride 区分：
    - PromptOverride 是 feedback agent 沉淀的"system prompt 末尾追加片段"
    - UserPromptOverride 是 admin/user 手动改的"某个 prompt 文件的整份替换内容"

    `name` 是 prompt_loader 用的 stem（不带 .md），如 'system_baseline'、'chat_role_discipline'。
    一个 user 同名只一条；删除该行 = 恢复默认（loader 走文件）。
    """
    __tablename__ = "user_prompt_overrides"
    user_id = Column(BigInteger, nullable=False)
    name = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(BigInteger, nullable=True)  # admin chat_id（admin 改他人时记录）
    __table_args__ = (PrimaryKeyConstraint("user_id", "name"),)


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
        # WAL + busy_timeout：多用户并发写不再 "database is locked"
        # check_same_thread=False 让 SQLAlchemy pool 跨线程复用 connection（asyncio 多 task 安全）
        _engine = create_engine(
            f"sqlite:///{settings().app_db_path}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA synchronous=NORMAL")  # WAL 下 NORMAL 即足够安全且快
            finally:
                cur.close()

        Base.metadata.create_all(_engine)
        _ensure_columns(_engine)
        _seed_skill_creator(_engine)
    return _engine


def _seed_skill_creator(eng) -> None:
    """启动时确保 skills 表有 name='skill_creator' 的特殊 meta-skill。

    feedback_agent 处理 capability_request 时会"调用"这条——把 body 当 sonnet
    prompt template 跑，输出 trigger-based 指令。

    第一次启动时种入；之后 admin 可在 webUI 调教 tab 编辑这条 skill 的 body 调整
    生成质量（虽然现在没 edit UI，但后续可以加）。
    """
    try:
        # 延迟 import 避免循环
        from . import feedback_prompts
    except Exception:
        return
    try:
        # 用一个全 0 dummy embedding 占位（不会被 cosine 召回——本就是 meta，feedback_agent
        # 直接按 name 查不走 embedding 召回）
        # 启动时同步：body / summary 跟代码不一致就 UPDATE。这样改 prompt 重启即生效，
        # 不用手动 SQL。admin 想覆盖可以 SET status='disabled' 让代码默认不被使用，
        # 然后用 admin UI 自己加另一个同名 skill（虽然现在没 edit UI）。
        import json as _json
        with sessionmaker(bind=eng, future=True)() as s:
            existing = s.query(Skill).filter(
                Skill.name == feedback_prompts.SKILL_CREATOR_NAME
            ).first()
            if existing:
                changed = False
                if existing.body != feedback_prompts.SKILL_CREATOR_BODY:
                    existing.body = feedback_prompts.SKILL_CREATOR_BODY
                    changed = True
                if existing.summary != feedback_prompts.SKILL_CREATOR_SUMMARY:
                    existing.summary = feedback_prompts.SKILL_CREATOR_SUMMARY
                    changed = True
                if changed:
                    s.commit()
                return
            s.add(Skill(
                name=feedback_prompts.SKILL_CREATOR_NAME,
                summary=feedback_prompts.SKILL_CREATOR_SUMMARY,
                body=feedback_prompts.SKILL_CREATOR_BODY,
                embedding=_json.dumps([0.0] * 512),
                created_by=0,  # system seed
                created_at=datetime.utcnow(),
                status="active",
            ))
            s.commit()
    except Exception as e:
        # 静默失败——不阻塞主启动；feedback_agent capability_request 路径会跳过
        import logging
        logging.getLogger(__name__).debug("seed skill_creator err: %s", e)


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
    if "prompt_overrides" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("prompt_overrides")}
        with eng.begin() as conn:
            if "trigger_kind" not in cols:
                conn.execute(text("ALTER TABLE prompt_overrides ADD COLUMN trigger_kind TEXT DEFAULT 'passive'"))
            if "cron_schedule" not in cols:
                conn.execute(text("ALTER TABLE prompt_overrides ADD COLUMN cron_schedule TEXT"))
            if "condition_prompt" not in cols:
                conn.execute(text("ALTER TABLE prompt_overrides ADD COLUMN condition_prompt TEXT"))
            if "last_fired_at" not in cols:
                conn.execute(text("ALTER TABLE prompt_overrides ADD COLUMN last_fired_at TIMESTAMP"))
    if "proactive_fires" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("proactive_fires")}
        with eng.begin() as conn:
            if "mode" not in cols:
                conn.execute(text("ALTER TABLE proactive_fires ADD COLUMN mode TEXT"))
            if "platform" not in cols:
                conn.execute(text("ALTER TABLE proactive_fires ADD COLUMN platform TEXT"))


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
    trigger_kind: str = "passive",
    cron_schedule: str | None = None,
    condition_prompt: str | None = None,
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
            trigger_kind=trigger_kind,
            cron_schedule=cron_schedule,
            condition_prompt=condition_prompt,
        )
        s.add(o)
        s.commit()
        return int(o.id)


def list_active_triggers() -> list[PromptOverride]:
    """所有 active 且 trigger_kind='active' 的 override。triggered_reach_job 用。"""
    with session() as s:
        return list(
            s.query(PromptOverride)
            .filter(
                PromptOverride.status == "active",
                PromptOverride.trigger_kind == "active",
                PromptOverride.cron_schedule.isnot(None),
            )
            .all()
        )


def mark_override_fired(override_id: int) -> None:
    with session() as s:
        o = s.query(PromptOverride).filter(PromptOverride.id == override_id).first()
        if o is None:
            return
        o.last_fired_at = datetime.utcnow()
        s.commit()


# ----- pending_reach_messages -----

def add_pending_reach(
    *, user_id: int, override_id: int, message: str, expected_send_after,
) -> int:
    with session() as s:
        p = PendingReachMessage(
            user_id=user_id, override_id=override_id, message=message,
            expected_send_after=expected_send_after,
            created_at=datetime.utcnow(), status="pending",
        )
        s.add(p)
        s.commit()
        return int(p.id)


def pop_pending_reach_for_merge(user_id: int) -> list[PendingReachMessage]:
    """取该 user 所有 status='pending' 的暂存消息，标 merged 返回。
    handle_user_message 入口调，把暂存内容融入下一轮 system prompt。"""
    now = datetime.utcnow()
    with session() as s:
        rows = list(
            s.query(PendingReachMessage)
            .filter(
                PendingReachMessage.user_id == user_id,
                PendingReachMessage.status == "pending",
            )
            .all()
        )
        for r in rows:
            r.status = "merged"
            r.updated_at = now if hasattr(r, "updated_at") else None
        s.commit()
        return rows


def list_overdue_pending_reach() -> list[PendingReachMessage]:
    """超过 expected_send_after 仍 pending 的——兜底直发用。"""
    now = datetime.utcnow()
    with session() as s:
        return list(
            s.query(PendingReachMessage)
            .filter(
                PendingReachMessage.status == "pending",
                PendingReachMessage.expected_send_after <= now,
            )
            .all()
        )


def mark_pending_reach_status(reach_id: int, status: str) -> None:
    if status not in ("pending", "merged", "sent", "expired"):
        raise ValueError(f"bad status: {status}")
    with session() as s:
        p = s.query(PendingReachMessage).filter(PendingReachMessage.id == reach_id).first()
        if p is None:
            return
        p.status = status
        s.commit()


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


# ----- user_prompt_overrides -----

def get_user_prompt_override(user_id: int, name: str) -> str | None:
    """返该用户对该 prompt 名的整份覆写内容；没改过返 None。"""
    with session() as s:
        row = s.query(UserPromptOverride).filter(
            UserPromptOverride.user_id == user_id,
            UserPromptOverride.name == name,
        ).first()
        return row.content if row else None


def set_user_prompt_override(
    user_id: int, name: str, content: str, *, updated_by: int | None = None,
) -> None:
    """upsert（user_id, name）→ content。content 可以是空串（视为"故意覆写为空"）。"""
    now = datetime.utcnow()
    with session() as s:
        row = s.query(UserPromptOverride).filter(
            UserPromptOverride.user_id == user_id,
            UserPromptOverride.name == name,
        ).first()
        if row is None:
            s.add(UserPromptOverride(
                user_id=user_id, name=name, content=content,
                updated_at=now, updated_by=updated_by,
            ))
        else:
            row.content = content
            row.updated_at = now
            row.updated_by = updated_by
        s.commit()


def delete_user_prompt_override(user_id: int, name: str) -> bool:
    """删该用户对该 prompt 名的覆写。返回是否真删了（False=本来就没这行）。"""
    with session() as s:
        row = s.query(UserPromptOverride).filter(
            UserPromptOverride.user_id == user_id,
            UserPromptOverride.name == name,
        ).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def list_user_prompt_overrides(user_id: int) -> list[UserPromptOverride]:
    """该用户所有覆写过的 prompt 行。admin UI 列表用。"""
    with session() as s:
        return list(
            s.query(UserPromptOverride)
            .filter(UserPromptOverride.user_id == user_id)
            .order_by(UserPromptOverride.name.asc())
            .all()
        )


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
