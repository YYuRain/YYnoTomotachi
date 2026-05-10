"""学"用户什么时候有空"。

非常朴素的做法：
- 每次用户主动发来消息 → 记一条 reply_samples(weekday, hour)，权重 = 1。
- 每次 agent 主动开场并得到用户回应 → 再记一条 weighted 更高（说明这个时段确实在线）。
- score(weekday, hour) = 该 (weekday, hour) 附近 ±1 小时的样本数 / 全表样本数的归一化。
- 冷启动期样本少 → score 接近均匀，让 scheduler 也不过度保守。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .storage import LastInteraction, ReplySample, session

log = logging.getLogger(__name__)


def record(replied_within_sec: int | None = None, now: datetime | None = None) -> None:
    # 用 local time 存 weekday/hour，和 proactive 的判断口径对齐
    now = now or datetime.now()
    with session() as sess:
        sess.add(
            ReplySample(
                ts=now,
                weekday=now.weekday(),
                hour=now.hour,
                replied_within_sec=replied_within_sec,
            )
        )
        row = sess.get(LastInteraction, 1)
        if row is None:
            sess.add(LastInteraction(id=1, ts=now))
        else:
            row.ts = now
        sess.commit()


def seconds_since_last_interaction() -> float:
    with session() as sess:
        row = sess.get(LastInteraction, 1)
        if row is None:
            return float("inf")
        return (datetime.now() - row.ts).total_seconds()


def _count_near(weekday: int, hour: int) -> int:
    hours = {(hour - 1) % 24, hour, (hour + 1) % 24}
    with session() as sess:
        stmt = select(func.count(ReplySample.id)).where(
            ReplySample.weekday == weekday,
            ReplySample.hour.in_(hours),
        )
        return int(sess.execute(stmt).scalar() or 0)


def _total_samples() -> int:
    with session() as sess:
        return int(sess.execute(select(func.count(ReplySample.id))).scalar() or 0)


def score(weekday: int, hour: int) -> float:
    total = _total_samples()
    if total < 10:
        # 冷启动：白天高、凌晨低的简单先验
        if 0 <= hour < 8:
            return 0.15
        if 8 <= hour < 12:
            return 0.55
        if 12 <= hour < 18:
            return 0.5
        if 18 <= hour < 24:
            return 0.7
    near = _count_near(weekday, hour)
    # 该时段在全部样本中占比 → 乘以一个缩放让峰值靠近 1
    ratio = near / max(total, 1)
    return min(1.0, ratio * 24.0)


def best_slot_in_next_hours(n: int = 6) -> datetime | None:
    now = datetime.now()
    best: tuple[float, datetime] | None = None
    for dh in range(n + 1):
        t = now + timedelta(hours=dh)
        s = score(t.weekday(), t.hour)
        if best is None or s > best[0]:
            best = (s, t)
    return best[1] if best else None
