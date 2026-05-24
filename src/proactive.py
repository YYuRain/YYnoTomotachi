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
import random
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select

from . import availability, interests, llm, tools
from .audit_log import audit
from .storage import ProactiveFire, session

log = logging.getLogger(__name__)

# 软门参数（2026-05-21 改：硬门→概率门——违规越严重 skip_prob 越高，但永不到 100%
# 保证"最差只是降频，不会完全不主动"）
MIN_GAP_FROM_USER_SEC = 30 * 60        # 对方刚聊过的参考阈值
MIN_GAP_FROM_SELF_SEC = 60 * 60        # 自己上次主动的参考阈值
DAILY_CAP = 6                          # 软上限——超了之后概率快速衰减但仍可能触发
MAX_UNANSWERED_FIRES = 1               # 连续 N 次 proactive 没收到 user 回应 → 降频
SOFT_SKIP_PROB_CAP = 0.97              # 单次 skip_prob 上限——保证 ≥3% 概率突破
SOFT_SKIP_REASONS_MAX_AUDIT = 4        # audit 记几条 violation

# Share-discovery 通道（2026-05-21）：bot 主动上网找有趣的分享给 user
SHARE_PLATFORMS = ("xhs", "bili", "web")   # 支持的平台
SHARE_DAILY_CAP_PER_PLATFORM = {           # 每日单平台上限
    "xhs": 1,
    "bili": 1,
    "web": 99,                             # web 不限（仍计入 DAILY_CAP 总盘）
}


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


def _count_today_share_by_platform(user_id: int, platform: str) -> int:
    """Share-discovery 通道每日单平台已 fire 次数。"""
    start, end = _today_range_utc()
    with session() as sess:
        stmt = select(func.count(ProactiveFire.id)).where(
            ProactiveFire.user_id == user_id,
            ProactiveFire.ts >= start,
            ProactiveFire.ts < end,
            ProactiveFire.mode == "share_discovery",
            ProactiveFire.platform == platform,
        )
        return int(sess.execute(stmt).scalar() or 0)


def _share_quota_remaining(user_id: int) -> list[str]:
    """返回今天还能用的 platform 列表。"""
    out: list[str] = []
    for p in SHARE_PLATFORMS:
        cap = SHARE_DAILY_CAP_PER_PLATFORM.get(p, 1)
        if _count_today_share_by_platform(user_id, p) < cap:
            out.append(p)
    return out


_SEARCH_FN = {
    "xhs": tools.search_xhs,
    "bili": tools.search_bilibili,
    "web": tools.search_web,
}


async def _select_share_item(
    user_id: int, platform: str, query: str, *, recent_topics: list[str],
) -> Optional[dict[str, Any]]:
    """调对应平台搜索 → LLM 看结果挑一条最 fit user 的。

    返回 {platform, title, url, blurb} 或 None（搜索 0 / LLM 弃选 / 解析失败）。
    """
    fn = _SEARCH_FN.get(platform)
    if fn is None:
        return None
    try:
        raw = await fn(query)
    except Exception as e:
        log.info("share search %s err: %s", platform, e)
        audit("proactive_share_search_error",
              user_id=user_id, platform=platform, query=query, error=str(e)[:200])
        return None
    if not raw:
        audit("proactive_share_search_empty",
              user_id=user_id, platform=platform, query=query)
        return None

    # LLM 挑一条（aux tier 便宜 model 即可）
    from . import prompt_loader
    sys_prompt = prompt_loader.load("proactive_share_select")
    user_msg = json.dumps({
        "platform": platform,
        "query": query,
        "recent_topics": recent_topics,
        "results": raw,
    }, ensure_ascii=False, indent=2)
    try:
        data = await llm.chat_json(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=500,
        )
    except Exception as e:
        log.info("share select llm err: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    idx = data.get("idx")
    if not isinstance(idx, int) or idx < 0:
        audit("proactive_share_no_pick",
              user_id=user_id, platform=platform, query=query,
              raw_preview=raw[:300])
        return None
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    blurb = str(data.get("blurb") or "").strip()
    if not (title and url):
        return None
    item = {"platform": platform, "title": title[:200], "url": url[:500], "blurb": blurb[:80]}
    audit("proactive_share_selected",
          user_id=user_id, platform=platform, query=query,
          picked_title=item["title"], picked_url=item["url"], blurb=item["blurb"])
    return item


def record_fire(
    user_id: int,
    *, why: str, user_probably_doing: str, opener_angle: str, opener_text: str,
    mode: str = "topic_chat", platform: Optional[str] = None,
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
                mode=mode,
                platform=platform,
            )
        )
        sess.commit()
    audit("proactive_fire", user_id=user_id, why=why,
          user_probably_doing=user_probably_doing,
          opener_angle=opener_angle, opener_text=opener_text,
          mode=mode, platform=platform)


def _compute_soft_gate_skip(
    *, idle_sec: float, last_fire: Optional[datetime], now: datetime,
    today_count: int, consecutive_asst: int,
) -> tuple[float, list[dict[str, Any]]]:
    """把所有"门违规"转成 skip_prob——取最大那个决定是否跳过。

    设计：违规越深，skip_prob 越大但永远 ≤ SOFT_SKIP_PROB_CAP（0.97）。
    保证最差也有 ≥3% 概率发出去——"频率降低但不会完全不主动"。
    """
    violations: list[dict[str, Any]] = []

    # 1) 对方刚聊过——0 idle 时 0.95，到达 MIN_GAP 时 0.5，超过线性衰减到 0
    if idle_sec < MIN_GAP_FROM_USER_SEC:
        ratio = max(0.0, idle_sec / MIN_GAP_FROM_USER_SEC)
        prob = 0.95 - 0.45 * ratio  # 0.95 → 0.5
        violations.append({"reason": "user_cooldown", "prob": round(prob, 3),
                           "idle_min": round(idle_sec / 60, 1)})

    # 2) 自己上次主动太近——同形状
    if last_fire is not None:
        since_self = (now - last_fire).total_seconds()
        if since_self < MIN_GAP_FROM_SELF_SEC:
            ratio = max(0.0, since_self / MIN_GAP_FROM_SELF_SEC)
            prob = 0.95 - 0.45 * ratio  # 0.95 → 0.5
            violations.append({"reason": "self_cooldown", "prob": round(prob, 3),
                               "since_min": round(since_self / 60, 1)})

    # 3) 每日上限——刚到 0.85，每超 1 条 +0.04，封顶 cap
    if today_count >= DAILY_CAP:
        over = today_count - DAILY_CAP + 1
        prob = min(SOFT_SKIP_PROB_CAP, 0.85 + 0.04 * over)
        violations.append({"reason": "daily_cap", "prob": round(prob, 3),
                           "opens_today": today_count, "cap": DAILY_CAP})

    # 4) 连续没回——1 条 0.7，2 条 0.85，3+ 0.95（仍有 5% 概率突破）
    if consecutive_asst >= MAX_UNANSWERED_FIRES and last_fire is not None:
        extras = consecutive_asst - MAX_UNANSWERED_FIRES
        prob = min(SOFT_SKIP_PROB_CAP, 0.70 + 0.15 * extras)
        violations.append({"reason": "unanswered_streak", "prob": round(prob, 3),
                           "consecutive_asst": consecutive_asst})

    if not violations:
        return 0.0, []
    skip_prob = max(v["prob"] for v in violations)
    return min(SOFT_SKIP_PROB_CAP, skip_prob), violations


async def decide(user_id: int, now: datetime | None = None) -> Optional[dict[str, Any]]:
    """返回 None = 不主动；否则返回 {why, user_probably_doing, opener_angle}。

    门策略（2026-05-21 改）：所有"硬门"软化为概率跳过。违规越严重 skip_prob 越高
    （封顶 0.97），但永远不会 100% 拦死——保证"频率降低但不会完全不主动"。
    """
    now = now or datetime.now()

    idle_sec = availability.seconds_since_last_interaction(user_id)
    today_count = _count_today(user_id)
    last_fire = _last_fire_ts(user_id)

    # 连续 N 条没回判定：看 _recent_per_user 末尾连续 assistant 数量
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

    skip_prob, violations = _compute_soft_gate_skip(
        idle_sec=idle_sec, last_fire=last_fire, now=now,
        today_count=today_count, consecutive_asst=consecutive_asst,
    )
    if skip_prob > 0:
        roll = random.random()
        if roll < skip_prob:
            audit("proactive_decision", user_id=user_id, should=False,
                  why=f"soft_gate:{violations[0]['reason']}",
                  ctx={"skip_prob": round(skip_prob, 3), "roll": round(roll, 3),
                       "violations": violations[:SOFT_SKIP_REASONS_MAX_AUDIT]})
            return None
        # 突破了——继续走软门 LLM；audit 留个痕迹方便观察
        audit("proactive_soft_gate_passed", user_id=user_id,
              skip_prob=round(skip_prob, 3), roll=round(roll, 3),
              violations=violations[:SOFT_SKIP_REASONS_MAX_AUDIT])

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

    share_quota_remaining = _share_quota_remaining(user_id)

    # 把最近 3 条自己发过的 proactive opener 摘出来——让软门 LLM 看到"我反复戳过这些"
    # 单独抽是因为 recent_history 是混杂的对话流，LLM 不容易辨认"哪些是我主动发的没回应的"
    recent_assistant_openers: list[str] = []
    if consecutive_asst > 0:
        for m in reversed(rec_msgs):
            if m.get("role") != "assistant":
                break
            content = (m.get("content") or "").strip()
            if content:
                recent_assistant_openers.append(content[:200])
            if len(recent_assistant_openers) >= 3:
                break

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
        "share_quota_remaining": share_quota_remaining,
        "consecutive_asst_no_reply": consecutive_asst,
        "recent_assistant_openers": recent_assistant_openers,
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

    decision: dict[str, Any] = {
        "why": str(data.get("why") or "")[:80],
        "user_probably_doing": str(data.get("user_probably_doing") or "")[:80],
        "opener_angle": str(data.get("opener_angle") or "")[:80],
        "recent_topics": top,
        "mode": "topic_chat",
        "share_item": None,
    }

    # Share-discovery 分支：LLM 输出了 share_intent 且配额够 → 调 search + LLM 挑
    share_intent = data.get("share_intent") if isinstance(data, dict) else None
    if isinstance(share_intent, dict):
        platform = str(share_intent.get("platform") or "").strip()
        query = str(share_intent.get("query") or "").strip()
        if platform in SHARE_PLATFORMS and query and platform in share_quota_remaining:
            item = await _select_share_item(
                user_id, platform, query, recent_topics=top,
            )
            if item:
                decision["mode"] = "share_discovery"
                decision["share_item"] = item
            # 选不出来就 silent 降级到 topic_chat（_select_share_item 内部已 audit）

    log.info(
        "proactive GO uid=%d: why=%r doing=%r angle=%r mode=%s",
        user_id, decision["why"], decision["user_probably_doing"],
        decision["opener_angle"], decision["mode"],
    )
    audit("proactive_decision", user_id=user_id, should=True, ctx=ctx, **decision)
    return decision
