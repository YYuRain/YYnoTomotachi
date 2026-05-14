"""后台定时任务（多用户版）：兴趣衰减 + memU flush + 人格 consolidate + 主动搭话。

每个 job 内部 `asyncio.gather` 遍历活跃用户，`Semaphore(5)` 兜底并发。
proactive_job 每用户独立 send/typing 闭包；硬门 SQL 拦掉绝大多数，软门 LLM 调用量很小。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import bot as bot_mod
from . import interests, memory, persona, proactive, test_bot, users
from .agent import generate_opener
from .rhythm import deliver

# 虚拟 user_id 起点——test_bot 给虚拟身份分配的 uid 都在这之上
_TEST_UID_BASE = 9_000_000_000


def _resolve_send_typing(uid: int):
    """决定 proactive 怎么把消息发给 user_id；返回 (send, typing) 或 None（无法路由就跳过）。

    - 真 telegram chat_id（< _TEST_UID_BASE）：走 prod bot
    - 虚拟 test 身份：必须能在 test_bot._identity 里反查到当前 real chat_id；
      否则跳过（test bot 没启 / 用户没 /become / bot 重启了 _identity 丢）。
      防止给虚拟 chat_id 发 telegram 消息抛异常浪费 LLM 调用。
    """
    if uid < _TEST_UID_BASE:
        return bot_mod.make_send_and_typing(uid)
    real = test_bot.real_chat_id_for(uid)
    if real is None:
        return None
    try:
        return test_bot.make_send_and_typing(real)
    except RuntimeError:
        return None

log = logging.getLogger(__name__)

# 限制每个 job 内部并发（防止 OpenRouter 短期被打爆）
_PER_JOB_SEMAPHORE = 5


async def _fan_out(coro_factory, sem_n: int = _PER_JOB_SEMAPHORE) -> None:
    """对所有活跃用户 fan-out。coro_factory(uid) 返回 awaitable。"""
    user_ids = users.list_active()
    if not user_ids:
        return
    sem = asyncio.Semaphore(sem_n)

    async def _wrap(uid: int):
        async with sem:
            try:
                await coro_factory(uid)
            except Exception as e:
                log.exception("job err uid=%d: %s", uid, e)

    await asyncio.gather(*(_wrap(u) for u in user_ids), return_exceptions=True)


def build() -> AsyncIOScheduler:
    # 用中国时区，cron 表达式（persona_consolidate 03:07）就是中国时间凌晨
    sched = AsyncIOScheduler(timezone="Asia/Shanghai")

    async def decay_job() -> None:
        async def _one(uid: int):
            interests.decay_tick(uid)
        await _fan_out(_one)

    async def memu_flush_job() -> None:
        # memory.maybe_flush(None) 内部已遍历所有 buffer 非空的用户
        try:
            await memory.maybe_flush(None)
        except Exception as e:
            log.debug("memu flush err: %s", e)

    async def proactive_job() -> None:
        async def _one(uid: int):
            decision = await proactive.decide(uid)
            if decision is None:
                return
            # 先确认能不能发；不能发就别浪费 LLM 调用 generate_opener
            route = _resolve_send_typing(uid)
            if route is None:
                log.info("proactive skip uid=%d: no telegram route (test 身份无 _identity 反查)", uid)
                return
            send, typing = route
            text = await generate_opener(uid, context=decision)
            if not text:
                return
            await deliver(text, send, typing)
            proactive.record_fire(
                uid,
                why=decision.get("why", ""),
                user_probably_doing=decision.get("user_probably_doing", ""),
                opener_angle=decision.get("opener_angle", ""),
                opener_text=text,
            )
            log.info("proactive opener sent uid=%d: %s", uid, text[:60])

        await _fan_out(_one)

    async def persona_consolidate_job() -> None:
        async def _one(uid: int):
            persona.consolidate(uid)
        await _fan_out(_one)

    sched.add_job(decay_job, "interval", hours=1, id="decay")
    sched.add_job(memu_flush_job, "interval", minutes=15, id="memu_flush")
    sched.add_job(
        persona_consolidate_job,
        CronTrigger(hour=3, minute=7),
        id="persona_consolidate",
    )
    jitter_sec = 10 * 60
    sched.add_job(
        proactive_job,
        IntervalTrigger(minutes=25, jitter=jitter_sec),
        id="proactive",
    )
    return sched
