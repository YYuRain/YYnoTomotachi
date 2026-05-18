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
    BigInteger, Column, DateTime, Index, String, Text, create_engine, text,
)
from sqlalchemy.dialects.postgresql import UUID
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
    evidence_ref = Column(Text, nullable=True)  # 来源对话文件路径，调试用

    __table_args__ = (
        Index("ix_memories_user_created", "user_id", "created_at"),
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
    _ensure_vector_index()
    log.info("memory_store engine ready (memories table)")
    return _engine


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


def reset_engine_for_tests() -> None:
    """单元测试用——清单例。生产代码不调。"""
    global _engine, _Session
    _engine = None
    _Session = None
