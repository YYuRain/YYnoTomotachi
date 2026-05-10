"""审计日志：把 bot 运行时的关键事件结构化写到 `data/audit.jsonl`。

每行一个 JSON 事件。不依赖 LLM——只把已经存在的运行时数据收集起来。
失败静默吞（日志写不进去不能挂主流程）。

事件类型：
- `startup` / `shutdown`           进程生命周期
- `user_msg`                        用户消息进来（text / image / quote）
- `assistant_reply`                 bot 实际发出的回复 + 用了哪个模型 + 档位 + latency
- `memory_recall`                   recall query + 命中片段
- `memory_flush`                    flush 多少消息 → 抽出多少 memory_items
- `persona_update`                  trait deltas / 新 observations / milestones / mood
- `persona_consolidate`             每日衰减
- `proactive_decision`              主动搭话决策（should / why / user_probably_doing / opener_angle）
- `proactive_fire`                  实际发出的开场
- `interest_bump`                   话题加热
- `tool_call`                       工具调用（xhs / Exa / read_url 等）

查看：
    tail -f data/audit.jsonl
    grep '"event":"persona_update"' data/audit.jsonl | jq .
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

_path: Path | None = None


def _get_path() -> Path:
    global _path
    if _path is None:
        _path = settings().root / "data" / "audit.jsonl"
        _path.parent.mkdir(parents=True, exist_ok=True)
    return _path


def audit(event: str, **fields: Any) -> None:
    """写一条审计事件。fields 任意 kwargs，会序列化成 JSON。失败不抛。"""
    try:
        record: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
        }
        record.update(fields)
        with _get_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.debug("audit write err: %s", e)
