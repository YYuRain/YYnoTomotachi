"""主动搭话决策。

核心问题：朋友不是"定点问候"——该开口的时候开口，不该的时候别打扰。
做法是两层门：
1. **硬门**（便宜 & 一定要对的）：
   - 距用户最近一次回复太近（<1h）不打扰。
   - 距上次 AI 主动搭话太近（<90min）不重复。
   - 每日上限（默认 6 条）。
   - **不再禁夜间**——夜间是否合适交给软门 LLM 判断（看用户作息：很多用户夜里更活跃）。
2. **软门**（LLM 判断）：
   时间/weekday、最近话题、idle 时长、今日已发次数 喂给 LLM，
   让它像朋友那样判断"现在要不要顺口说一句"，包含夜间是否打扰的判断。
   返回 should + 猜对方在做什么 + 想切入的角度。

决策通过后，交给 `agent.generate_opener(context=...)` 生成实际消息，
写 `ProactiveFire` 表记录。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select

from . import availability, interests, llm
from .audit_log import audit
from .storage import ProactiveFire, session

log = logging.getLogger(__name__)

# 硬门参数（夜间不再硬门——交给软门判断）
MIN_GAP_FROM_USER_SEC = 1 * 3600       # 对方刚聊过 1h 内不主动（缩短自 2h）
MIN_GAP_FROM_SELF_SEC = 90 * 60        # 自己上次主动 90min 内不连续（缩短自 3h）
DAILY_CAP = 6                          # 每天最多主动 6 条（提高自 3）


_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

_DECIDE_SYSTEM = """你要决定：现在这个时间点，作为一个普通朋友，要不要主动发一条消息过去。

**你不是助手，不是提醒功能。** 不要"定期问候"。不要"嘘寒问暖"。你只在真的顺口想说点什么的时候才发。

判断的倾向：
- 不要太克制。朋友之间随手发一句的频率是 OK 的——一天 3-6 条没问题，关键是"想起来才发"。
- 以下情况可以发：
  - 真的想起某件和对方有关的事（参考"最近聊过的话题"）
  - 刚"看到/听到/遇到"某件有意思的小事，想分享
  - 很久没聊了（idle 长）想随口起个话头
  - 当前时间正好是对方平时活跃的时段（看 user_active_score_now）
- 以下情况不发：
  - 没什么特别想说的，纯"问候"心态 → 不发
  - 对方很可能在忙的时段（工作日白天上班、深度睡觉时段）且没特别理由发

**关于夜间**：23:00–07:00 不是一刀切的禁区，但要看情况——
  - 如果 `user_active_score_now` 这个时段历史上很高（说明用户经常这个点活跃），且 idle 也不算很长，可以发。
  - 如果是凌晨 2-5 点这种"绝大多数人都在睡"的时段，没特别想说就别发。
  - 周末晚 23-1 点比工作日凌晨宽松得多。
  - 当作"朋友会不会这个点给我发微信"来判断。

输出严格 JSON：
{
  "should": true|false,
  "why": "10 字内解释判断理由",
  "user_probably_doing": "根据时间/weekday 猜对方此刻大概在做什么，20 字内",
  "opener_angle": "如果 should=true：你想用什么角度开口（想起某事/分享见闻/随口吐槽/关心一件具体的事），20 字内；should=false 时留空"
}

JSON 之外不要输出任何内容。"""


def _today_range_utc() -> tuple[datetime, datetime]:
    # 按本地时间算"今天"，但 ProactiveFire.ts 存的是 utcnow；用 local 转 UTC 的粗略近似：
    # 直接按 local 的今日起止，SQLite 存的时间戳比较能对得上（我们历史上都用 utcnow 存 ts，
    # 差一个时区，但作为"粗略当日"够用）。
    now_local = datetime.now()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local


def _count_today() -> int:
    start, end = _today_range_utc()
    with session() as sess:
        stmt = select(func.count(ProactiveFire.id)).where(
            ProactiveFire.ts >= start, ProactiveFire.ts < end
        )
        return int(sess.execute(stmt).scalar() or 0)


def _last_fire_ts() -> Optional[datetime]:
    with session() as sess:
        row = sess.execute(
            select(ProactiveFire).order_by(ProactiveFire.ts.desc()).limit(1)
        ).scalar_one_or_none()
        return row.ts if row else None


def record_fire(
    *, why: str, user_probably_doing: str, opener_angle: str, opener_text: str
) -> None:
    with session() as sess:
        sess.add(
            ProactiveFire(
                ts=datetime.now(),
                why=why[:80],
                user_probably_doing=user_probably_doing[:80],
                opener_angle=opener_angle[:80],
                opener_text=opener_text[:500],
            )
        )
        sess.commit()
    audit("proactive_fire", why=why, user_probably_doing=user_probably_doing,
          opener_angle=opener_angle, opener_text=opener_text)


async def decide(now: datetime | None = None) -> Optional[dict[str, Any]]:
    """返回 None = 不主动；否则返回 {why, user_probably_doing, opener_angle}。"""
    now = now or datetime.now()

    # 硬门（夜间不再过滤——交给下面的软门 LLM 看 user_active_score_now 判断）
    idle_sec = availability.seconds_since_last_interaction()
    if idle_sec < MIN_GAP_FROM_USER_SEC:
        return None
    last_fire = _last_fire_ts()
    if last_fire and (now - last_fire).total_seconds() < MIN_GAP_FROM_SELF_SEC:
        return None
    today_count = _count_today()
    if today_count >= DAILY_CAP:
        return None

    score = availability.score(now.weekday(), now.hour)
    top = [t for t, _ in interests.top(6)]

    ctx: dict[str, Any] = {
        "now": now.strftime("%H:%M"),
        "weekday": _WEEKDAYS[now.weekday()],
        "hours_since_user_last_msg": round(idle_sec / 3600, 1),
        "user_active_score_now": round(score, 2),
        "recent_topics": top,
        "opens_today": today_count,
        "daily_cap": DAILY_CAP,
    }
    user_msg = json.dumps(ctx, ensure_ascii=False, indent=2)

    try:
        data = await llm.chat_json(
            [
                {"role": "system", "content": _DECIDE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=800,
        )
    except Exception as e:
        log.debug("proactive decide failed: %s", e)
        return None

    if not isinstance(data, dict) or not data.get("should"):
        why = (data or {}).get("why", "") if isinstance(data, dict) else ""
        if why:
            log.info("proactive skip: %s", why)
        audit("proactive_decision", should=False, why=why, ctx=ctx)
        return None

    decision = {
        "why": str(data.get("why") or "")[:80],
        "user_probably_doing": str(data.get("user_probably_doing") or "")[:80],
        "opener_angle": str(data.get("opener_angle") or "")[:80],
        "recent_topics": top,
    }
    log.info(
        "proactive GO: why=%r doing=%r angle=%r",
        decision["why"], decision["user_probably_doing"], decision["opener_angle"],
    )
    audit("proactive_decision", should=True, ctx=ctx, **decision)
    return decision
