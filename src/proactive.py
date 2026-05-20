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
MIN_GAP_FROM_USER_SEC = 30 * 60        # 对方刚聊过 30min 内不主动（缩短自 1h，2026-05-14）
MIN_GAP_FROM_SELF_SEC = 60 * 60        # 自己上次主动 60min 内不连续（缩短自 90min，2026-05-14）
DAILY_CAP = 6                          # 每天最多主动 6 条


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

**关于 recent_history（最近对话片段）和 active_overrides（用户偏好）——非常重要**：
- `recent_history` 是你跟对方刚聊过的话。**选 opener_angle 时要避开里面已经覆盖的话题**——
  比如 history 里已经聊完"昨天没下雨/伞在家没事"，就不要选"问昨天淋雨没"这种重复角度
- `active_overrides` 是用户表达过的偏好/触发指令。如果其中某条已经能覆盖你想说的事
  （比如用户已请求"下雨提醒带伞"，主动通道会自动管这件事），你就别再凑这个角度
- 选 opener_angle 时优先**没在 recent_history 出现过的新话题** / 用户感兴趣但近期没聊的事
- 如果 recent_history 显示对方刚有过情绪倾诉（累/烦躁），且话题没自然结束 → 一般 should=false
  （让对方先消化），除非你想接着上一条情绪做软回应

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


def _count_today(user_id: int) -> int:
    start, end = _today_range_utc()
    with session() as sess:
        stmt = select(func.count(ProactiveFire.id)).where(
            ProactiveFire.user_id == user_id,
            ProactiveFire.ts >= start,
            ProactiveFire.ts < end,
        )
        return int(sess.execute(stmt).scalar() or 0)


def _last_fire_ts(user_id: int) -> Optional[datetime]:
    with session() as sess:
        row = sess.execute(
            select(ProactiveFire)
            .where(ProactiveFire.user_id == user_id)
            .order_by(ProactiveFire.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        return row.ts if row else None


def record_fire(
    user_id: int,
    *, why: str, user_probably_doing: str, opener_angle: str, opener_text: str,
) -> None:
    with session() as sess:
        sess.add(
            ProactiveFire(
                user_id=user_id,
                ts=datetime.now(),
                why=why[:80],
                user_probably_doing=user_probably_doing[:80],
                opener_angle=opener_angle[:80],
                opener_text=opener_text[:500],
            )
        )
        sess.commit()
    audit("proactive_fire", user_id=user_id, why=why,
          user_probably_doing=user_probably_doing,
          opener_angle=opener_angle, opener_text=opener_text)


async def decide(user_id: int, now: datetime | None = None) -> Optional[dict[str, Any]]:
    """返回 None = 不主动；否则返回 {why, user_probably_doing, opener_angle}。"""
    now = now or datetime.now()

    # 硬门（夜间不再过滤——交给下面的软门 LLM 看 user_active_score_now 判断）。
    # 每个硬门拦截都打 audit，方便事后排查"为啥这段时间没主动发"。
    idle_sec = availability.seconds_since_last_interaction(user_id)
    today_count = _count_today(user_id)
    last_fire = _last_fire_ts(user_id)
    if idle_sec < MIN_GAP_FROM_USER_SEC:
        audit("proactive_decision", user_id=user_id, should=False,
              why="hard_gate:user_cooldown",
              ctx={"idle_min": round(idle_sec / 60, 1),
                   "min_gap_min": MIN_GAP_FROM_USER_SEC // 60})
        return None
    if last_fire and (now - last_fire).total_seconds() < MIN_GAP_FROM_SELF_SEC:
        audit("proactive_decision", user_id=user_id, should=False,
              why="hard_gate:self_cooldown",
              ctx={"since_last_fire_min": round((now - last_fire).total_seconds() / 60, 1),
                   "min_gap_min": MIN_GAP_FROM_SELF_SEC // 60})
        return None
    if today_count >= DAILY_CAP:
        audit("proactive_decision", user_id=user_id, should=False,
              why="hard_gate:daily_cap",
              ctx={"opens_today": today_count, "cap": DAILY_CAP})
        return None

    score = availability.score(user_id, now.weekday(), now.hour)
    top = [t for t, _ in interests.top(user_id, 6)]

    # 拉最近对话片段——避免 LLM 选一个已聊过/已回答过的话题作为 opener_angle
    recent_history: list[str] = []
    try:
        from .agent import _recent_per_user
        rec = _recent_per_user.get(str(user_id), [])
        for m in rec[-12:]:
            role = "user" if m.get("role") == "user" else "asst"
            content = (m.get("content") or "").strip().replace("\r", "")
            if content:
                recent_history.append(f"{role}: {content[:200]}")
    except Exception as e:
        log.debug("proactive decide load recent err uid=%s: %s", user_id, e)

    # 拉 active overrides——LLM 选 angle 时要尊重用户已表达的偏好
    active_overrides: list[str] = []
    try:
        from . import storage as _storage
        for o in _storage.list_active_overrides(user_id)[:8]:
            active_overrides.append(o.text[:200])
    except Exception as e:
        log.debug("proactive decide load overrides err uid=%s: %s", user_id, e)

    ctx: dict[str, Any] = {
        "now": now.strftime("%H:%M"),
        "weekday": _WEEKDAYS[now.weekday()],
        "hours_since_user_last_msg": round(idle_sec / 3600, 1),
        "user_active_score_now": round(score, 2),
        "recent_topics": top,
        "recent_history": recent_history,
        "active_overrides": active_overrides,
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
        audit("proactive_decision", user_id=user_id, should=False,
              why=f"llm_error:{type(e).__name__}",
              ctx={**ctx, "error": str(e)[:200]})
        return None

    if not isinstance(data, dict) or not data.get("should"):
        why = (data or {}).get("why", "") if isinstance(data, dict) else ""
        if why:
            log.info("proactive skip uid=%d: %s", user_id, why)
        audit("proactive_decision", user_id=user_id, should=False, why=why, ctx=ctx)
        return None

    decision = {
        "why": str(data.get("why") or "")[:80],
        "user_probably_doing": str(data.get("user_probably_doing") or "")[:80],
        "opener_angle": str(data.get("opener_angle") or "")[:80],
        "recent_topics": top,
    }
    log.info(
        "proactive GO uid=%d: why=%r doing=%r angle=%r",
        user_id, decision["why"], decision["user_probably_doing"], decision["opener_angle"],
    )
    audit("proactive_decision", user_id=user_id, should=True, ctx=ctx, **decision)
    return decision
