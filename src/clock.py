"""时间感知小工具。

LLM 不知道"现在"是几点。直接喂 ISO 时间会让回复带播报腔（"现在是下午 2 点"），
所以这里把时间包成中文体感字符串：`周三 14:32（工作日下午）`。

注入位置在 `agent._build_turn` —— 拼到用户消息前缀（不进 system，避免破 prompt cache，
且 MiniMax 链路上 user 前缀比 system 末尾稳定）。
"""
from __future__ import annotations

from datetime import datetime

_WEEKDAY_ZH = "一二三四五六日"


def _hour_phase(h: int) -> str:
    if 0 <= h < 5:
        return "深夜"
    if 5 <= h < 8:
        return "清早"
    if 8 <= h < 11:
        return "上午"
    if 11 <= h < 13:
        return "午饭点"
    if 13 <= h < 17:
        return "下午"
    if 17 <= h < 19:
        return "晚饭点"
    if 19 <= h < 22:
        return "晚上"
    return "深夜"


def now_signal(dt: datetime | None = None) -> str:
    """`2026-05-08 周五（工作日） 14:32 下午` 风格的中文时间感字符串。

    设计说明：日期、weekday、工作日/周末、时段四件信息**分开**摆，
    避免被模型打包成一个 chunk 误读（实测 MiniMax-M2.7 会把
    "周五（工作日午饭点）"里的"周五"弱化成"周末午饭点"语境）。"""
    dt = dt or datetime.now()
    weekday_zh = "周" + _WEEKDAY_ZH[dt.weekday()]
    daytype = "周末" if dt.weekday() >= 5 else "工作日"
    return f"{dt.strftime('%Y-%m-%d')} {weekday_zh}（{daytype}） {dt.strftime('%H:%M')} {_hour_phase(dt.hour)}"


def since_phrase(seconds: float) -> str:
    """把"距上次互动多少秒"翻译成体感词。"""
    if seconds == float("inf") or seconds < 0:
        return "很久"
    s = int(seconds)
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{s // 60} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    d = s // 86400
    if d == 1:
        return "1 天前"
    if d < 7:
        return f"{d} 天前"
    w = s // (86400 * 7)
    if w < 5:
        return f"{w} 周前"
    return f"{d} 天前"
