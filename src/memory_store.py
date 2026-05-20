"""自搭记忆栈的 schema + ORM + engine。

只有一张表 `memories`：
- 一条事实 = 一行
- 不再分 categories / resources 这些 memU 的辅助实体
- pgvector RAG（cosine 相似度）召回

启动时 `engine()` 会：
- CREATE EXTENSION IF NOT EXISTS vector
- 建表 + 索引
- 老库（之前用过 memU）安全无侵入——不动 memory_items / memory_categories 等表
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Column, DateTime, Float, Index, Integer, String, Text, create_engine, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

log = logging.getLogger(__name__)

EMBED_DIM = 512  # bge-small-zh-v1.5 输出维度


class Base(DeclarativeBase):
    pass


class Memory(Base):
    """一条记忆事实。"""
    __tablename__ = "memories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, nullable=False, index=True)
    summary = Column(Text, nullable=False)
    memory_type = Column(String(32), nullable=False, default="profile")  # profile | event
    embedding = Column(Vector(EMBED_DIM), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    evidence_ref = Column(Text, nullable=True)

    # PRD v2 字段（5.1 写入冲突检测 / 5.2 召回反验证 / 5.3 Auto Dream 用）
    status = Column(String(16), nullable=False, default="confirmed")  # confirmed | to_verify | stale
    confidence = Column(Float, nullable=False, default=1.0)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    depends_on = Column(ARRAY(UUID(as_uuid=True)), nullable=True)  # 上游 memory id 列表

    # P0-4（2026-05-20，Graphiti episodes 借鉴）：来源 episode 反查
    # 抽这条 memory 时 buffer 的原始 turns 落在 episodes 表，这里反查用
    source_episode_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_memories_user_created", "user_id", "created_at"),
        Index("ix_memories_user_status", "user_id", "status"),
    )


class Episode(Base):
    """一次 flush 的原始对话片段——抽出来的 memory 都 trace 回这里。

    设计：每次 maybe_flush 把当前 buffer 的 raw turns 整体写一行，拿到 episode_id 后
    再调 LLM 抽 profile/event；落库时 memories.source_episode_id 指过来。
    用途：(1) 5.2 反验证 / 5.3 Auto Dream LLM 拿原始上下文判断；(2) admin UI 点条目跳"当时聊了啥"。
    """
    __tablename__ = "episodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(BigInteger, nullable=False, index=True)
    raw_turns = Column(JSONB, nullable=False)  # list[{role, content, ts}]
    turn_count = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_episodes_user_ended", "user_id", "ended_at"),
    )


_engine = None
_Session = None


def _db_url() -> str:
    """复用 .env::MEMU_DB_URL 这个老 key，部署时不用改。"""
    s = settings()
    if not s.memu_db_url:
        raise RuntimeError("MEMU_DB_URL 未设——自搭记忆栈仍依赖 postgres，请配置")
    return s.memu_db_url


def engine():
    """单例 SQLAlchemy engine。第一次调用会建表 + 启用 pgvector + 建 ivfflat 索引。"""
    global _engine
    if _engine is not None:
        return _engine
    _engine = create_engine(_db_url(), future=True)
    with _engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(_engine)
    _ensure_v2_columns()
    _ensure_vector_index()
    log.info("memory_store engine ready (memories table)")
    return _engine


def _ensure_v2_columns() -> None:
    """老库已经有 memories 表但没 PRD v2 字段——一次性加列。

    `Base.metadata.create_all` 只建不存在的表，已经存在的表它不会改 schema。所以新加的
    status / confidence / last_verified_at / depends_on 必须自己 ALTER。
    """
    ddls = [
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS status VARCHAR(16) "
        "NOT NULL DEFAULT 'confirmed'",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION "
        "NOT NULL DEFAULT 1.0",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ NULL",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS depends_on UUID[] NULL",
        "CREATE INDEX IF NOT EXISTS ix_memories_user_status "
        "ON memories (user_id, status)",
        # P0-4 episodes（2026-05-20）
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_episode_id UUID NULL",
        "CREATE INDEX IF NOT EXISTS ix_memories_source_episode "
        "ON memories (source_episode_id)",
    ]
    try:
        with _engine.begin() as conn:  # type: ignore[union-attr]
            for ddl in ddls:
                conn.execute(text(ddl))
    except Exception as e:
        log.warning("memories v2 列升级失败：%s", e)


def _ensure_vector_index() -> None:
    """ivfflat cosine 索引；表里 0 行时建索引会报 lists 参数问题，所以仅在
    至少有一行时建。少量数据时不建索引也能跑（线性扫够快）。
    """
    try:
        with _engine.begin() as conn:  # type: ignore[union-attr]
            count = conn.execute(text("SELECT count(*) FROM memories")).scalar() or 0
            if count < 100:
                # 数据量小时 seqscan 反而更快，不建索引
                return
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_memories_embedding_cosine "
                "ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
            ))
    except Exception as e:
        log.debug("ivfflat 索引创建跳过：%s", e)


def session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _Session()


# ============ Episodes (P0-4) ============

def add_episode(
    user_id: int,
    raw_turns: list[dict],
    *,
    started_at: datetime,
    ended_at: datetime,
) -> str:
    """落 episode 行，返回新 episode_id（str）。raw_turns: list[{role, content, ...}]。"""
    eng = engine()
    new_id = str(uuid.uuid4())
    import json as _json
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO episodes (id, user_id, raw_turns, turn_count, "
                "started_at, ended_at, created_at) "
                "VALUES (CAST(:id AS uuid), :user_id, CAST(:raw_turns AS jsonb), "
                ":turn_count, :started_at, :ended_at, :created_at)"
            ),
            {
                "id": new_id,
                "user_id": user_id,
                "raw_turns": _json.dumps(raw_turns, ensure_ascii=False),
                "turn_count": len(raw_turns) // 2,
                "started_at": started_at,
                "ended_at": ended_at,
                "created_at": datetime.utcnow(),
            },
        )
    return new_id


def get_episode(episode_id: str) -> Optional[dict]:
    """读一条 episode（admin UI / 反验证用）。"""
    eng = engine()
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id::text, user_id, raw_turns, turn_count, "
                "started_at, ended_at, created_at FROM episodes "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": episode_id},
        ).first()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "raw_turns": row[2],
        "turn_count": row[3],
        "started_at": row[4],
        "ended_at": row[5],
        "created_at": row[6],
    }


def reset_engine_for_tests() -> None:
    """单元测试用——清单例。生产代码不调。"""
    global _engine, _Session
    _engine = None
    _Session = None
