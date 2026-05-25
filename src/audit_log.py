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

def _get_path_for_today() -> Path:
    """按日切割：data/audit.YYYY-MM-DD.jsonl。

    保留 audit.jsonl 软链/兼容名（如有）但新写入按天滚——避免单文件 GB 级 / admin tail 越来越慢。
    清理：scheduler 的 daily cleanup job 会删 7 天前的旧 audit。
    """
    base = settings().root / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"audit.{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def audit(event: str, **fields: Any) -> None:
    """写一条审计事件。fields 任意 kwargs，会序列化成 JSON。失败不抛。"""
    try:
        record: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
        }
        record.update(fields)
        with _get_path_for_today().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.debug("audit write err: %s", e)
