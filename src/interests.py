"""动态兴趣热度表。

- heat 随每次提及 +1；随时间按 exp(-Δt/τ) 衰减。
- 话题抽取：用 minimax.chat_json 做极简关键词归纳，低温度。
- top/cold 提供给 prompts 和 scheduler。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from sqlalchemy import delete, select

from . import llm
from .audit_log import audit
from .config import settings
from .storage import Interest, session

log = logging.getLogger(__name__)

def _extract_prompt(user_id: int | None = None) -> str:
    """从 prompt/interests_extract.md 加载（2026-05-21 抽出）；user_id 走 per-user 覆写。"""
    from . import prompt_loader
    return prompt_loader.load("interests_extract", user_id=user_id)


async def extract_topics(text: str, user_id: int | None = None) -> list[str]:
    if len(text.strip()) < 2:
        return []
    try:
        # 按当前 provider 走 llm.chat_json。
        data = await llm.chat_json(
            [
                {"role": "system", "content": _extract_prompt(user_id=user_id)},
                {"role": "user", "content": text},
            ],
            max_tokens=2048,
        )
    except Exception as e:
        log.debug("topic extract failed: %s", e)
        return []
    topics = data.get("topics") or []
    out: list[str] = []
    for t in topics:
        if isinstance(t, str):
            t = t.strip()
            if 1 < len(t) <= 12:
                out.append(t)
    return out[:3]


def bump(user_id: int, topics: list[str], delta: float = 1.0) -> None:
    if not topics:
        return
    now = datetime.utcnow()
    after: dict[str, float] = {}
    with session() as sess:
        for t in topics:
            row = sess.get(Interest, (user_id, t))
            if row is None:
                sess.add(Interest(user_id=user_id, topic=t, heat=delta, last_touch=now))
                after[t] = delta
            else:
                row.heat = float(row.heat or 0.0) + delta
                row.last_touch = now
                after[t] = row.heat
        sess.commit()
    audit("interest_bump", user_id=user_id, topics=topics, delta=delta, heat_after=after)


def decay_tick(user_id: int) -> int:
    """对单个用户的兴趣表做一次衰减；淘汰 heat < 0.1 的条目。返回淘汰数。"""
    tau_sec = settings().interest_decay_tau_hours * 3600.0
    now = datetime.utcnow()
    evicted = 0
    with session() as sess:
        rows = sess.execute(
            select(Interest).where(Interest.user_id == user_id)
        ).scalars().all()
        for row in rows:
            dt = (now - row.last_touch).total_seconds()
            row.heat = float(row.heat) * math.exp(-dt / tau_sec)
            row.last_touch = now
            if row.heat < 0.1:
                sess.execute(
                    delete(Interest).where(
                        Interest.user_id == user_id,
                        Interest.topic == row.topic,
                    )
                )
                evicted += 1
        sess.commit()
    if evicted:
        log.info("interests decay uid=%d: evicted %d", user_id, evicted)
    return evicted


def top(user_id: int, n: int = 5) -> list[tuple[str, float]]:
    with session() as sess:
        rows = sess.execute(
            select(Interest)
            .where(Interest.user_id == user_id)
            .order_by(Interest.heat.desc())
            .limit(n)
        ).scalars().all()
        return [(r.topic, float(r.heat)) for r in rows]


def cold(user_id: int, n: int = 5) -> list[tuple[str, float]]:
    """返回 heat 最低的 n 个（用于"最近没聊"的提示）。"""
    with session() as sess:
        rows = sess.execute(
            select(Interest)
            .where(Interest.user_id == user_id)
            .order_by(Interest.heat.asc())
            .limit(n)
        ).scalars().all()
        return [(r.topic, float(r.heat)) for r in rows]
