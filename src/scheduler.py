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

from . import agent_ideas, bot as bot_mod
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
            # 让下一轮 user 回复时上下文能看见这条 opener
            from .agent import record_proactive_message
            record_proactive_message(uid, text)
            share_item = decision.get("share_item") or {}
            proactive.record_fire(
                uid,
                why=decision.get("why", ""),
                user_probably_doing=decision.get("user_probably_doing", ""),
                opener_angle=decision.get("opener_angle", ""),
                opener_text=text,
                mode=decision.get("mode", "topic_chat"),
                platform=share_item.get("platform"),
            )
            log.info("proactive opener sent uid=%d: %s", uid, text[:60])

        await _fan_out(_one)

    async def persona_consolidate_job() -> None:
        async def _one(uid: int):
            persona.consolidate(uid)
        await _fan_out(_one)

    async def auto_dream_job() -> None:
        """PRD v2 / 5.3 + P1-6 + airi-borrow + L4 自治：搭便车 03:13 班车，跑批量整理。

        per-user 五段：
        1. memories 三态判定（auto_dream）
        2. prompt_overrides 冲突整理（auto_dream_overrides）
        3. insight 生成（auto_dream_insights）—— P1-6
        4. **agent_ideas 自主形成**（airi `come_up_ideas` 借鉴）—— bot 凌晨想心事
        5. **agent self iterate**（L4）—— bot 看自己最近聊得咋样、自改 prompt / skill / 写 issue

        全局一段：
        6. skill 库整理（auto_dream_skills）
        7. agent_ideas 过期清理（expire_old_ideas）

        所有反思类 LLM 调用（1/3/4/5/6）走 reflection tier（默认 opus）。
        """
        from . import agent_self
        async def _one(uid: int):
            try:
                await memory.auto_dream(uid)
            except Exception as e:
                log.warning("auto_dream uid=%d err: %s", uid, e)
            try:
                await memory.auto_dream_overrides(uid)
            except Exception as e:
                log.warning("auto_dream_overrides uid=%d err: %s", uid, e)
            try:
                await memory.auto_dream_insights(uid)
            except Exception as e:
                log.warning("auto_dream_insights uid=%d err: %s", uid, e)
            try:
                await agent_ideas.form_ideas(uid)
            except Exception as e:
                log.warning("agent_ideas.form_ideas uid=%d err: %s", uid, e)
            # L4 self iterate—放最后，先有 ideas + insights，再决定要不要改自己
            try:
                await agent_self.auto_dream_self_iterate(uid)
            except Exception as e:
                log.warning("auto_dream_self_iterate uid=%d err: %s", uid, e)
        await _fan_out(_one)
        # skill 库不分 user，跑一次即可
        try:
            await memory.auto_dream_skills()
        except Exception as e:
            log.warning("auto_dream_skills err: %s", e)
        # 全局 idea 过期清理
        try:
            agent_ideas.expire_old_ideas()
        except Exception as e:
            log.warning("agent_ideas expire err: %s", e)

    async def triggered_reach_job() -> None:
        """主动触达 job：每分钟扫所有 active trigger override，cron match 当前时间则跑。

        不走 proactive 冷却（这是 user 明确请求的、有意图的触达）。
        命中后跑 sonnet 判 condition + 生成消息：
          - user 最近 5min 在聊 → 暂存 PendingReachMessage 让下一轮 handle 融入
          - user 不在聊 → 直接 send + record_proactive_message + UPDATE last_fired_at
        """
        try:
            from . import triggered_reach
            await triggered_reach.tick()
        except Exception as e:
            log.warning("triggered_reach_job err: %s", e)

    async def pending_reach_overdue_job() -> None:
        """兜底：pending_reach_messages 里 expected_send_after < now 仍 pending 的，直发。"""
        try:
            from . import triggered_reach
            await triggered_reach.dispatch_overdue()
        except Exception as e:
            log.warning("pending_reach_overdue_job err: %s", e)

    async def daily_cleanup_job() -> None:
        """每日清理：
        - audit.<date>.jsonl 保留 30 天
        - data/wipe_backup_<uid>_<ts>/ 保留 7 天（per MEMORY.md 软规则）
        """
        import os
        import shutil
        import time as _t
        from .config import settings as _settings
        root = _settings().root / "data"
        if not root.exists():
            return
        now_ts = _t.time()
        # audit 30 天
        audit_cutoff = now_ts - 30 * 86400
        for p in root.glob("audit.*.jsonl"):
            try:
                if p.stat().st_mtime < audit_cutoff:
                    p.unlink()
                    log.info("cleanup: removed old audit %s", p.name)
            except Exception as e:
                log.debug("cleanup audit %s err: %s", p, e)
        # wipe_backup 7 天
        backup_cutoff = now_ts - 7 * 86400
        for p in root.glob("wipe_backup_*"):
            try:
                if p.is_dir() and p.stat().st_mtime < backup_cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    log.info("cleanup: removed old wipe_backup %s", p.name)
            except Exception as e:
                log.debug("cleanup wipe_backup %s err: %s", p, e)

    # 防重叠 + misfire 兜底：job 跑超时不要堆积新实例；停机后多个 misfire 合并成一次
    # max_instances=1 → 上次没跑完不开新实例（避免数据库被 50 用户 × 多 job 并发打爆）
    # coalesce=True → 一段时间内多次 misfire 合并成一次
    # misfire_grace_time=600 → 进程暂停 ≤10min 还能补跑（cloudflared 抖动 / 重启窗口够用）
    _COMMON = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 600}
    sched.add_job(decay_job, "interval", hours=1, id="decay", **_COMMON)
    sched.add_job(memu_flush_job, "interval", minutes=15, id="memu_flush", **_COMMON)
    sched.add_job(
        persona_consolidate_job,
        CronTrigger(hour=3, minute=7),
        id="persona_consolidate",
        **_COMMON,
    )
    sched.add_job(
        auto_dream_job,
        CronTrigger(hour=3, minute=13),
        id="auto_dream",
        # auto_dream 是重活儿（多用户 × 三段 LLM 整理），跑超 1h 也不怪——给更长 grace
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    jitter_sec = 10 * 60
    sched.add_job(
        proactive_job,
        IntervalTrigger(minutes=25, jitter=jitter_sec),
        id="proactive",
        **_COMMON,
    )
    sched.add_job(
        triggered_reach_job,
        IntervalTrigger(minutes=1),
        id="triggered_reach",
        # 这条每分钟跑——misfire grace 取小：超 90s 没跑就别补（错过的分钟也没意义）
        max_instances=1, coalesce=True, misfire_grace_time=90,
    )
    sched.add_job(
        pending_reach_overdue_job,
        IntervalTrigger(minutes=1, jitter=15),
        id="pending_reach_overdue",
        max_instances=1, coalesce=True, misfire_grace_time=90,
    )
    sched.add_job(
        daily_cleanup_job,
        CronTrigger(hour=4, minute=23),  # 03:13 auto_dream 之后；避开整点凑热闹
        id="daily_cleanup",
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
    return sched
