"""后台定时任务：兴趣衰减 + 主动搭话。

- interests.decay_tick 每小时跑一次。
- proactive_tick 每 10 分钟检查一次：满足 idle 阈值 + 此刻 availability.score 够高 → 发一句开场。
  发出后会记一条 availability（视作一次尝试时段）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Awaitable, Callable

import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import availability, interests, memory, persona, proactive
from .agent import generate_opener
from .config import settings
from .rhythm import deliver

log = logging.getLogger(__name__)

Send = Callable[[str], Awaitable[None]]
Typing = Callable[[], Awaitable[None]] | None


def build(send: Send, typing: Typing) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")

    async def decay_job() -> None:
        try:
            interests.decay_tick()
        except Exception as e:
            log.exception("decay_tick failed: %s", e)

    async def memu_flush_job() -> None:
        try:
            await memory.maybe_flush()
        except Exception as e:
            log.debug("memu flush err: %s", e)

    async def proactive_job() -> None:
        try:
            decision = await proactive.decide()
            if decision is None:
                return
            text = await generate_opener(context=decision)
            if not text:
                return
            await deliver(text, send, typing)
            # 记录 AI 主动开场（用于节流/每日上限/事后检查）
            proactive.record_fire(
                why=decision.get("why", ""),
                user_probably_doing=decision.get("user_probably_doing", ""),
                opener_angle=decision.get("opener_angle", ""),
                opener_text=text,
            )
            log.info("proactive opener sent: %s", text[:60])
        except Exception as e:
            log.exception("proactive_job failed: %s", e)

    async def persona_consolidate_job() -> None:
        try:
            persona.consolidate()
        except Exception as e:
            log.exception("persona consolidate failed: %s", e)

    sched.add_job(decay_job, "interval", hours=1, id="decay")
    sched.add_job(memu_flush_job, "interval", minutes=15, id="memu_flush")
    # 每日本地凌晨 03:07 跑 consolidate（trait 朝中性衰减、清旧 observations）
    sched.add_job(
        persona_consolidate_job,
        CronTrigger(hour=3, minute=7),
        id="persona_consolidate",
    )
    # proactive 检查：平均每 25 分钟一次 + ±10 分钟 jitter
    # （硬门兜底：用户冷却 1h、自冷却 90min、每日 6 条；多出来的检查只是给软门更多机会）
    jitter_sec = 10 * 60
    sched.add_job(
        proactive_job,
        IntervalTrigger(minutes=25, jitter=jitter_sec),
        id="proactive",
    )
    return sched
