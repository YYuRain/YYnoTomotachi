"""bot 自主形成的"想做的事" pool（airi `come_up_ideas` 借鉴）。

定位：不是 todo / 不是任务调度——是**陪伴角色凌晨独自想心事时浮现的"想问她 X /
想跟她聊 Y / 想分享 Z"**。proactive 决策时优先消费这些 idea 当 opener_angle，
让 bot 显得"想起来一件事"，而不是机械抽 recent_topics。

数据流：
1. 03:13 auto_dream → `form_ideas(uid)` 让 sonnet 看最近事实自主写 0-5 条
2. proactive.decide → `list_pending(uid, top_n=3)` 拿优先级最高几条塞进 ctx
3. LLM 输出 `consumed_idea_id` 选了哪条 → `mark_idea_used(id)`
4. 7 天没 used 自动 expire（防 idea 池失控膨胀）

设计要点：
- 跟 memories / persona / overrides 一样存 postgres（同库不同表）
- 写入前 cosine 去重——跟现存 open + 最近 30 天 used 的 idea 比，sim ≥ 0.85 丢
- expires_at = created_at + 7 天
- 跑频次：03:13 一次/天/用户
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text as sql_text

from . import embed_client, llm, memory_prompts, memory_store
from .audit_log import audit

log = logging.getLogger(__name__)

# 跟 memory.py::auto_dream_insights 类似的参数
FORM_IDEAS_SAMPLE_DAYS = 30      # 抽样窗（idea 比 insight 更"短期"——30 天足矣）
FORM_IDEAS_SAMPLE_PROFILE = 8
FORM_IDEAS_SAMPLE_EVENT = 12
FORM_IDEAS_MIN_SAMPLES = 4
FORM_IDEAS_MAX_PER_RUN = 5
FORM_IDEAS_TIMEOUT_SEC = 30.0
FORM_IDEAS_DEFAULT_TTL_DAYS = 7   # idea 7 天没 used 就 expire

# 去重阈值——跟 insight 复用同套 cosine 阈值（bge-small-zh 上措辞改写 ≈ 0.92-0.95）
IDEA_DEDUP_COSINE_THRESHOLD = 0.85
# 喂给 LLM 的 existing 上限（让它知道写过啥不要重）
IDEA_EXISTING_LIMIT = 30


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _parse_pgvector(emb) -> list[float]:
    if emb is None:
        return []
    if isinstance(emb, list):
        return [float(x) for x in emb]
    nums = re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", str(emb))
    return [float(x) for x in nums]


# ============ pool 维护 ============

def list_pending(user_id: int, top_n: int = 3) -> list[dict]:
    """拿该 user 优先级最高、最新的几条 open idea。proactive 决策前调。"""
    eng = memory_store.engine()
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT id, text, kind, priority, source_ids, suggested_query, created_at "
                "FROM agent_ideas "
                "WHERE user_id = :uid AND status = 'open' "
                "AND expires_at > now() "
                "ORDER BY priority DESC, created_at DESC LIMIT :lim"
            ),
            {"uid": user_id, "lim": top_n},
        ).fetchall()
    return [
        {
            "id": r[0],
            "text": r[1],
            "kind": r[2],
            "priority": r[3],
            "source_ids": [str(x) for x in (r[4] or [])],
            "suggested_query": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def mark_idea_used(idea_id: int) -> bool:
    """proactive 决策真的采纳这条 idea 后调。返回是否成功（idea 可能已 expire / used）。"""
    if not idea_id:
        return False
    eng = memory_store.engine()
    with eng.begin() as conn:
        result = conn.execute(
            sql_text(
                "UPDATE agent_ideas SET status = 'used', used_at = now() "
                "WHERE id = :id AND status = 'open' "
                "RETURNING id"
            ),
            {"id": idea_id},
        ).fetchone()
    if result:
        audit("agent_idea_used", idea_id=idea_id)
        log.info("agent idea #%d marked used", idea_id)
        return True
    return False


def expire_old_ideas() -> int:
    """到期的 idea 标 expired。daily_cleanup 调一次。"""
    eng = memory_store.engine()
    with eng.begin() as conn:
        result = conn.execute(
            sql_text(
                "UPDATE agent_ideas SET status = 'expired' "
                "WHERE status = 'open' AND expires_at <= now()"
            )
        )
    n = result.rowcount or 0
    if n:
        log.info("agent_ideas expire_old: %d 条转 expired", n)
    return n


# ============ form_ideas（dream 阶段调）============

async def form_ideas(user_id: int) -> dict[str, Any]:
    """让 sonnet 看最近事实自主形成 0-5 条 idea，写 agent_ideas 表。

    去重：写入前与"近 30 天 open + used" idea + 同 batch 内做 cosine。
    """
    eng = memory_store.engine()
    started = time.time()

    # 拉抽样事实——跟 auto_dream_insights 同 SQL 形状（profile 8 + event 12）
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "(SELECT id::text, memory_type, summary, created_at FROM memories "
                " WHERE user_id = :uid AND status = 'confirmed' "
                " AND memory_type = 'profile' "
                " AND created_at > now() - (:days || ' days')::interval "
                " AND (valid_to IS NULL OR valid_to > now()) "
                " ORDER BY created_at DESC LIMIT :prof_n) "
                "UNION ALL "
                "(SELECT id::text, memory_type, summary, created_at FROM memories "
                " WHERE user_id = :uid AND status = 'confirmed' "
                " AND memory_type = 'event' "
                " AND created_at > now() - (:days || ' days')::interval "
                " AND (valid_to IS NULL OR valid_to > now()) "
                " ORDER BY created_at DESC LIMIT :event_n) "
                "ORDER BY created_at DESC"
            ),
            {"uid": user_id, "days": FORM_IDEAS_SAMPLE_DAYS,
             "prof_n": FORM_IDEAS_SAMPLE_PROFILE, "event_n": FORM_IDEAS_SAMPLE_EVENT},
        ).fetchall()

    items = [
        {"id": r[0], "memory_type": r[1], "summary": r[2], "created_at": r[3]}
        for r in rows
    ]
    if len(items) < FORM_IDEAS_MIN_SAMPLES:
        log.info("form_ideas uid=%s: 只有 %d 条样本，跳过", user_id, len(items))
        return {"generated": 0, "samples": len(items), "skipped": "too_few_samples"}

    # 拉 existing ideas（含 used，做去重；prompt 只展示 open + 近 used）
    with eng.connect() as conn:
        existing_rows = conn.execute(
            sql_text(
                "SELECT id, text, kind, status, created_at "
                "FROM agent_ideas "
                "WHERE user_id = :uid "
                "AND ( status = 'open' "
                "      OR (status = 'used' AND used_at > now() - INTERVAL '14 days') ) "
                "ORDER BY created_at DESC LIMIT :lim"
            ),
            {"uid": user_id, "lim": IDEA_EXISTING_LIMIT},
        ).fetchall()
    existing_for_prompt = [
        {"text": r[1], "kind": r[2], "created_at": r[4]}
        for r in existing_rows
    ]
    existing_texts = [r[1] for r in existing_rows]

    prompt = memory_prompts.render_form_ideas_dream(items, existing=existing_for_prompt, user_id=user_id)
    try:
        data = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="main",
                max_tokens=2000,
            ),
            timeout=FORM_IDEAS_TIMEOUT_SEC,
        )
    except Exception as e:
        log.warning("form_ideas uid=%s LLM err: %s", user_id, e)
        elapsed_ms = int((time.time() - started) * 1000)
        audit("agent_ideas_form", user_id=user_id, generated=0,
              samples=len(items), error=f"{type(e).__name__}:{str(e)[:200]}",
              latency_ms=elapsed_ms)
        return {"generated": 0, "samples": len(items), "error": str(e)}

    raw = data.get("ideas") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        log.info("form_ideas uid=%s: LLM 未生成", user_id)
        audit("agent_ideas_form", user_id=user_id, generated=0,
              samples=len(items), latency_ms=int((time.time() - started) * 1000))
        return {"generated": 0, "samples": len(items)}

    # 校验 + 短 id 解析
    id_lookup = {it["id"][:8]: it["id"] for it in items}
    valid_ideas: list[dict] = []
    for ins in raw[:FORM_IDEAS_MAX_PER_RUN]:
        if not isinstance(ins, dict):
            continue
        text_v = (ins.get("text") or "").strip()
        if not text_v or len(text_v) > 240:
            continue
        kind = (ins.get("kind") or "follow_up").strip()
        if kind not in ("question", "share", "follow_up", "observation"):
            kind = "follow_up"
        try:
            priority = int(ins.get("priority") or 5)
        except (TypeError, ValueError):
            priority = 5
        priority = max(1, min(10, priority))
        sup_short = ins.get("supporting_ids") or []
        sup_full: list[str] = []
        if isinstance(sup_short, list):
            sup_full = [id_lookup[s] for s in sup_short
                        if isinstance(s, str) and s in id_lookup]

        # share kind 必须带 suggested_query；其他 kind 不接受这字段
        suggested_query: Optional[str] = None
        if kind == "share":
            sq = (ins.get("suggested_query") or "").strip()
            if not sq or len(sq) > 200:
                # 空 / 太长 → 降级成 follow_up（或干脆丢？这里降级，让 LLM 文本仍能用）
                log.info("form_ideas: share kind 缺 suggested_query 或过长，降为 follow_up: %s",
                         text_v[:60])
                kind = "follow_up"
            else:
                suggested_query = sq

        valid_ideas.append({
            "text": text_v, "kind": kind, "priority": priority,
            "source_ids": sup_full,
            "suggested_query": suggested_query,
        })

    if not valid_ideas:
        log.info("form_ideas uid=%s: %d 条原始全没过校验", user_id, len(raw))
        audit("agent_ideas_form", user_id=user_id, generated=0,
              samples=len(items), raw_count=len(raw),
              latency_ms=int((time.time() - started) * 1000))
        return {"generated": 0, "samples": len(items), "raw_count": len(raw)}

    # cosine 去重——跟 existing texts + 同 batch 内
    new_texts = [v["text"] for v in valid_ideas]
    all_texts_to_embed = existing_texts + new_texts
    try:
        all_vecs = await embed_client.embed_many(all_texts_to_embed)
    except Exception as e:
        log.warning("form_ideas uid=%s embed err: %s", user_id, e)
        all_vecs = [None] * len(all_texts_to_embed)
    existing_vecs = all_vecs[:len(existing_texts)]
    new_vecs = all_vecs[len(existing_texts):]

    accepted: list[tuple[dict, list[float] | None]] = []
    duplicates: list[dict] = []
    for v, vec in zip(valid_ideas, new_vecs):
        if vec and existing_vecs:
            best_sim = 0.0
            best_match = ""
            for ex_text, ex_vec in zip(existing_texts, existing_vecs):
                if not ex_vec:
                    continue
                sim = _cosine(vec, ex_vec)
                if sim > best_sim:
                    best_sim, best_match = sim, ex_text
            if best_sim >= IDEA_DEDUP_COSINE_THRESHOLD:
                duplicates.append({
                    "rejected_text": v["text"][:120],
                    "matched_existing": best_match[:120],
                    "cosine": round(best_sim, 3),
                })
                continue
        # 同 batch 内
        is_dup = False
        for ac_v, ac_vec in accepted:
            if vec and ac_vec and _cosine(vec, ac_vec) >= IDEA_DEDUP_COSINE_THRESHOLD:
                duplicates.append({
                    "rejected_text": v["text"][:120],
                    "matched_existing": ac_v["text"][:120],
                    "cosine": round(_cosine(vec, ac_vec), 3),
                    "intra_batch": True,
                })
                is_dup = True
                break
        if is_dup:
            continue
        accepted.append((v, vec))

    if not accepted:
        log.info("form_ideas uid=%s: %d 条全跟现存重复", user_id, len(valid_ideas))
        audit("agent_ideas_form", user_id=user_id, generated=0,
              samples=len(items), raw_count=len(raw),
              dedup_rejected=len(duplicates), duplicates=duplicates,
              latency_ms=int((time.time() - started) * 1000))
        return {"generated": 0, "samples": len(items), "dedup_rejected": len(duplicates)}

    # INSERT
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=FORM_IDEAS_DEFAULT_TTL_DAYS)
    inserted_ids: list[int] = []
    with eng.begin() as conn:
        for v, _ in accepted:
            params = {
                "user_id": user_id,
                "text": v["text"],
                "kind": v["kind"],
                "priority": v["priority"],
                "source_ids": v["source_ids"] or None,
                "suggested_query": v.get("suggested_query"),
                "now": now,
                "expires": expires,
            }
            row = conn.execute(
                sql_text(
                    "INSERT INTO agent_ideas (user_id, text, kind, priority, "
                    "source_ids, suggested_query, status, created_at, expires_at) "
                    "VALUES (:user_id, :text, :kind, :priority, "
                    "CAST(:source_ids AS uuid[]), :suggested_query, 'open', :now, :expires) "
                    "RETURNING id"
                ),
                params,
            ).fetchone()
            if row:
                inserted_ids.append(int(row[0]))

    elapsed_ms = int((time.time() - started) * 1000)
    log.info("form_ideas uid=%s 生成 %d 条 (samples=%d, dedup=%d, latency=%dms)",
             user_id, len(accepted), len(items), len(duplicates), elapsed_ms)
    audit("agent_ideas_form", user_id=user_id,
          generated=len(accepted), samples=len(items),
          dedup_rejected=len(duplicates), duplicates=duplicates,
          ideas=[{"id": _id, "text": v["text"][:120],
                  "kind": v["kind"], "priority": v["priority"],
                  "suggested_query": v.get("suggested_query")}
                 for _id, (v, _) in zip(inserted_ids, accepted)],
          latency_ms=elapsed_ms)
    return {
        "generated": len(accepted),
        "samples": len(items),
        "dedup_rejected": len(duplicates),
        "ids": inserted_ids,
        "latency_ms": elapsed_ms,
    }
