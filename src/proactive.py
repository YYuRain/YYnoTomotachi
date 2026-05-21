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
MAX_UNANSWERED_FIRES = 1               # 连续 N 次 proactive 没收到 user 回应 → backoff（2026-05-21
                                       # 加，admin 数据清空后 last_interaction=inf + recent 全是
                                       # assistant 自己的 opener，bot 反复发同一句不知道停）


_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

def _decide_system() -> str:
    """从 prompt/proactive_decide.md 加载（2026-05-21 抽出）。"""
    from . import prompt_loader
    return prompt_loader.load("proactive_decide")


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

    # 新硬门（2026-05-21）：连续 MAX_UNANSWERED_FIRES 次 proactive 没等到 user 回应 → backoff
    # 衡量方法：看 _recent_per_user 末尾连续的 assistant 数量（中间没夹任何 user message）。
    # 这样 last_interaction 表是否存在不影响判断——直接看实际对话状态。
    try:
        from .agent import _recent_per_user
        rec_msgs = _recent_per_user.get(str(user_id), [])
    except Exception:
        rec_msgs = []
    consecutive_asst = 0
    for m in reversed(rec_msgs):
        if m.get("role") == "assistant":
            consecutive_asst += 1
        else:
            break
    if consecutive_asst >= MAX_UNANSWERED_FIRES and last_fire is not None:
        audit("proactive_decision", user_id=user_id, should=False,
              why="hard_gate:unanswered_streak",
              ctx={"consecutive_asst_msgs": consecutive_asst,
                   "max_unanswered": MAX_UNANSWERED_FIRES,
                   "last_fire_min_ago": round((now - last_fire).total_seconds() / 60, 1)})
        return None

    score = availability.score(user_id, now.weekday(), now.hour)
    top = [t for t, _ in interests.top(user_id, 6)]

    # 拉最近对话片段——避免 LLM 选一个已聊过/已回答过的话题作为 opener_angle
    recent_history: list[str] = []
    try:
        rec = rec_msgs  # 复用上面已读的
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
                {"role": "system", "content": _decide_system()},
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
