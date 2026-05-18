"""一次性脚本：给现有 profile 集合补 conflict-check 历史。

PRD v2 / 5.1 加 status + depends_on 之前迁过来的 memory，全部 confirmed / deps=NULL。
直接看 graph 是孤岛。本脚本按时间顺序"重放"这些 profile：每条 profile P 出现时，
对**比它早**的 profile 做一次 conflict check：
  - LLM 拿 top-5 语义最近的早 profile 作为候选
  - 判 still_valid / to_verify / stale
  - 后两者：UPDATE 早 profile 的 status / confidence / depends_on（追加 P 的 id）

跟 src.memory._check_conflicts_for_one 的实时逻辑等价，只是这里限定候选 created_at < new.created_at
来还原"那个时刻应该看到的旧条目"。

幂等：脚本完整跑完一遍，再跑一次会因为某些 profile 已经有 deps（被覆盖）；保守做法是只对
当前 deps 为空的 profile 跑——本脚本默认这么干。覆盖跑请加 --force。

用法：
  .venv/bin/python -m scripts.backfill_conflict_check --user-id 8058993786 [--dry] [--force]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("backfill")


def _setup_env() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "backfill")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.minimaxi.com"
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


CONFLICT_TOPK = 5

_VERDICT_RE = re.compile(
    r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"verdict"\s*:\s*"(still_valid|to_verify|stale)"\s*\}',
    re.DOTALL,
)


def _parse_verdicts(raw: str, valid_ids: set[str]) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None
    out: dict[str, str] = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("verdicts"), list):
        for v in parsed["verdicts"]:
            if not isinstance(v, dict):
                continue
            cid = v.get("id"); verdict = v.get("verdict")
            if cid in valid_ids and verdict in ("still_valid", "to_verify", "stale"):
                out[cid] = verdict
        return out
    for m in _VERDICT_RE.finditer(raw):
        cid, verdict = m.group(1), m.group(2)
        if cid in valid_ids:
            out[cid] = verdict
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--dry", action="store_true", help="只跑 LLM 判定，不 UPDATE")
    parser.add_argument("--force", action="store_true",
                        help="对所有 profile 跑，即使该条已经有 deps")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试）")
    parser.add_argument("--sleep", type=float, default=1.0, help="每条之间间隔秒")
    args = parser.parse_args()

    _setup_env()
    from src import memory_prompts, memory_store, openrouter
    from src.config import settings
    from sqlalchemy import text

    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        log.error("MEMU_CHAT_MODEL / OPENROUTER_MODEL 都没配，跑不动")
        return 2

    eng = memory_store.engine()

    # 拉所有 profile 按 created_at asc 排，注意：embedding 是 pgvector，转 list[float]
    where_clause = "user_id = :u AND memory_type = 'profile' AND embedding IS NOT NULL"
    if not args.force:
        where_clause += " AND (depends_on IS NULL OR array_length(depends_on, 1) IS NULL)"

    with eng.connect() as conn:
        rows = conn.execute(text(
            f"SELECT id::text AS id, summary, created_at, embedding::text AS embedding "
            f"FROM memories WHERE {where_clause} ORDER BY created_at ASC"
        ), {"u": args.user_id}).fetchall()

    profiles = [dict(id=r[0], summary=r[1], created_at=r[2], embedding=r[3]) for r in rows]
    if args.limit:
        profiles = profiles[:args.limit]

    log.info("user=%s 待跑 %d 条 profile（model=%s, dry=%s, force=%s）",
             args.user_id, len(profiles), model, args.dry, args.force)
    if not profiles:
        log.info("没东西可跑")
        return 0

    flips_total = 0
    candidates_total = 0
    skipped = 0

    for i, new in enumerate(profiles, 1):
        # 拉它之前的所有 profile（不含自己），按当前条目 embedding 找 top-N
        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT id::text AS id, summary FROM memories "
                "WHERE user_id = :u AND memory_type = 'profile' "
                "AND status != 'stale' AND created_at < :ts AND id != CAST(:nid AS uuid) "
                "ORDER BY embedding <=> CAST(:q AS vector) "
                "LIMIT :k"
            ), {"u": args.user_id, "ts": new["created_at"], "nid": new["id"],
                "q": new["embedding"], "k": CONFLICT_TOPK}).fetchall()
        candidates = [(r[0], r[1]) for r in rows]
        if not candidates:
            skipped += 1
            log.info("[%d/%d] %s « 没早于它的 profile，跳", i, len(profiles), new["id"][:8])
            continue
        candidates_total += len(candidates)

        prompt = memory_prompts.render_conflict_check(new["summary"], candidates)
        try:
            res = await openrouter.chat(
                [{"role": "user", "content": prompt}],
                model=model, temperature=0.1, max_tokens=2048,
            )
            raw = res.get("text", "") if isinstance(res, dict) else ""
        except Exception as e:
            log.warning("[%d/%d] %s ✗ LLM err %s", i, len(profiles), new["id"][:8], e)
            await asyncio.sleep(args.sleep); continue

        verdicts = _parse_verdicts(raw, valid_ids={c[0] for c in candidates})
        if not verdicts:
            log.warning("[%d/%d] %s ⚠ 解析 0 verdicts raw=%r", i, len(profiles), new["id"][:8], raw[:120])
            await asyncio.sleep(args.sleep); continue

        flips: list[tuple[str, str]] = []
        now = datetime.now(timezone.utc)
        if args.dry:
            for old_id, verdict in verdicts.items():
                if verdict in ("to_verify", "stale"):
                    flips.append((old_id, verdict))
        else:
            with eng.begin() as conn:
                for old_id, verdict in verdicts.items():
                    if verdict == "still_valid":
                        conn.execute(text(
                            "UPDATE memories SET last_verified_at = :ts "
                            "WHERE id = CAST(:id AS uuid)"
                        ), {"ts": now, "id": old_id})
                    elif verdict in ("to_verify", "stale"):
                        new_conf = 0.0 if verdict == "stale" else 0.5
                        conn.execute(text(
                            "UPDATE memories SET status = :s, confidence = :c, "
                            "updated_at = :ts, "
                            "depends_on = (SELECT ARRAY(SELECT DISTINCT unnest("
                            "  COALESCE(depends_on, ARRAY[]::uuid[]) || ARRAY[CAST(:dep AS uuid)]"
                            ")) FROM memories WHERE id = CAST(:id AS uuid)) "
                            "WHERE id = CAST(:id AS uuid)"
                        ), {"s": verdict, "c": new_conf, "ts": now,
                             "id": old_id, "dep": new["id"]})
                        flips.append((old_id, verdict))

        flips_total += len(flips)
        log.info("[%d/%d] %s « +%d cand → %d flips%s",
                 i, len(profiles), new["id"][:8], len(candidates), len(flips),
                 (" : " + ",".join(f"{oid[:6]}={v}" for oid, v in flips)) if flips else "")
        await asyncio.sleep(args.sleep)

    log.info("\n完成：%d profile · %d candidates · %d flips · %d skipped (no earlier)",
             len(profiles), candidates_total, flips_total, skipped)
    if args.dry:
        log.info("（--dry，没真改库）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
