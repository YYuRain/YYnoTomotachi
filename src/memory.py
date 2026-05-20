"""自搭记忆栈（替换 memU SDK，2026-05-18 起）。

设计：
- 一张表 `memories`（详见 `src/memory_store.py`）；不再有 categories / resources / category_items
- 抽取走 `src/memory_prompts.py`，用 `MEMU_CHAT_MODEL`（默认 deepseek-v4-flash）输出 JSON
- 召回是简单 pgvector cosine top-k（embedding 由本地 :18080 shim 出）
- multi-user：所有公共函数吃 `user_id: int`，模块级 buffer/flush_ts 是 `dict[str_uid, ...]`

公共 API（**不变**，上层 agent.py / persona.py / scheduler.py 不用改）：
- `recall(user_id, user_text, top_k=3) -> list[str]`
- `note_turn(user_id, user_text, assistant_text)`
- `maybe_flush(user_id=None, force=False) -> bool`
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql_text

from . import embed_client, llm, memory_prompts, memory_store
from .audit_log import audit
from .config import settings

log = logging.getLogger(__name__)

FLUSH_EVERY_N_TURNS = 6
FLUSH_INTERVAL_SEC = 15 * 60  # 或 15 分钟强制一次

# recall 精度门
RECALL_MIN_QUERY_CJK_CHARS = 6   # query 中文字符 < 这个数 → 不 recall
RECALL_MAX_DISTANCE = 0.55       # pgvector cosine distance；越小越相似（0=同方向）。> 这个 → 视为不相关
# 一些不带语义的口头话术，整句直接命中就跳过（超出长度门时兜底）
_RECALL_STOPWORD_PATTERNS = [
    re.compile(r"^[嗯啊哦哎呀哈呵嘿耶吧呢吗的了"
               r"\s\.,。，！？!?…~]+$"),
    re.compile(r"^(是的|是吧|是啊|对啊|对的|好的|行|确实|没错|可以|嗯嗯|嗯啊|"
               r"哈哈|哈哈哈|哎|哦哦|没事|挺好|不错|牛|牛逼|可怕|绝了|"
               r"我没事|不影响|不知道|不太懂|我也是|确实是|是这样|就这样)$"),
]


def _is_low_value_query(text: str) -> tuple[bool, str]:
    """A 道门：太短/纯口头禅的 query → 直接跳过 recall。返回 (是否跳, 原因)。"""
    s = (text or "").strip()
    if not s:
        return True, "empty"
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    # 中文 query 字数门
    if cjk and cjk < RECALL_MIN_QUERY_CJK_CHARS:
        return True, f"too_short_cjk={cjk}"
    # 纯英文 query 长度门（按词算）
    if not cjk and len(s.split()) < 3:
        return True, f"too_short_en_words={len(s.split())}"
    # 整句口头禅
    for pat in _RECALL_STOPWORD_PATTERNS:
        if pat.fullmatch(s):
            return True, "stopword_phrase"
    return False, ""

# PRD v2 / 5.2：召回时反验证 to_verify 条目
REVERIFY_COOLDOWN_SEC = 30 * 60  # 同一条 to_verify 30min 内最多反验证一次
REVERIFY_TIMEOUT_SEC = 8.0       # 单条反验证 LLM 调用超时（保护 recall 整体延迟）
REVERIFY_UPSTREAM_LIMIT = 3      # 喂给 LLM 的上游事实最多 3 条

# PRD v2 / 5.3：Auto Dream 后台整理
DREAM_NEIGHBOR_TOP_K = 5     # 每条 to_verify 拿多少邻居 confirmed 条目当上下文
DREAM_BATCH_SLEEP_SEC = 0.5  # 同一用户内每条之间间隔（限速 LLM）
DREAM_TIMEOUT_SEC = 15.0     # 单条 dream LLM 调用超时（后台允许更宽松）

# user_id (str) → buffer of {role, content}
_buffer_per_user: dict[str, list[dict[str, str]]] = {}
_last_flush_ts_per_user: dict[str, float] = {}
_flush_lock = asyncio.Lock()


def _uid(chat_id: int | str) -> str:
    return str(chat_id)


def _buffer_dir() -> Path:
    d = settings().root / "data" / "memu_buffer"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============ 召回 ============

def _fmt_date(dt) -> str:
    """datetime / iso-string → 'YYYY-MM-DD'。"""
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt[:10]
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


async def recall(user_id: int, user_text: str, *, top_k: int = 3) -> list[str]:
    """语义召回 top_k 条记忆，每条带形成日期。失败返回空 list，不阻塞主流程。

    PRD v2 / 5.1：`status = 'stale'` 不召回。
    PRD v2 / 5.2：召回结果中 `status = 'to_verify'` 且 30min 内没反验证过的，同步跑一次
    LLM 反验证；still_valid 升回 confirmed，uncertain 保持 to_verify 但打 last_verified_at 戳
    限速。返回时按最新 status 拼前缀（confirmed 不带，to_verify 带 `[待确认]`）。

    格式：`(2026-05-15) 用户最近在减肥` 或 `(2026-05-15) [待确认] ...`
    """
    uid_str = _uid(user_id)
    snippets: list[str] = []
    distances: list[float] = []
    # A 道门：query 太短/纯口头禅 → 不浪费 embed call
    skipped, skip_reason = _is_low_value_query(user_text)
    if skipped:
        audit("memory_recall", user_id=user_id, query=user_text[:200],
              hits=0, snippets=[], skipped_reason=skip_reason)
        log.info("recall uid=%s skip: %s (query=%r)", uid_str, skip_reason, user_text[:60])
        return []

    try:
        vec = await embed_client.embed_one(user_text)
        if vec is None:
            log.debug("recall uid=%s: embed 失败，跳过 RAG", uid_str)
            audit("memory_recall", user_id=user_id, query=user_text[:200], hits=0, snippets=[])
            return []

        eng = memory_store.engine()
        # B 道门：cosine 距离阈值——只取距离 < RECALL_MAX_DISTANCE 的
        # 拿距离一起回来方便 audit
        with eng.connect() as conn:
            rows = conn.execute(
                sql_text(
                    "SELECT id::text AS id, summary, created_at, status, "
                    "last_verified_at, depends_on, "
                    "(embedding <=> CAST(:q AS vector)) AS dist "
                    "FROM memories "
                    "WHERE user_id = :uid AND status != 'stale' "
                    "AND (embedding <=> CAST(:q AS vector)) < :max_dist "
                    "ORDER BY embedding <=> CAST(:q AS vector) "
                    "LIMIT :k"
                ),
                {"uid": user_id, "q": embed_client.vec_literal(vec),
                 "k": top_k, "max_dist": RECALL_MAX_DISTANCE},
            ).fetchall()

        items = [
            {
                "id": r[0], "summary": r[1], "created_at": r[2],
                "status": r[3], "last_verified_at": r[4], "depends_on": r[5],
                "dist": float(r[6]) if r[6] is not None else None,
            }
            for r in rows
        ]

        # 5.2：找需要反验证的
        now = datetime.now(timezone.utc)
        cooldown_floor = now.timestamp() - REVERIFY_COOLDOWN_SEC
        due: list[dict[str, Any]] = []
        for it in items:
            if it["status"] != "to_verify":
                continue
            lva = it.get("last_verified_at")
            if lva is None or lva.timestamp() < cooldown_floor:
                due.append(it)

        if due:
            results = await asyncio.gather(
                *[_reverify_one(user_id, it, user_text) for it in due],
                return_exceptions=True,
            )
            # 把验证结果写回 items（status 可能升级），UPDATE 数据库
            with eng.begin() as conn:
                for it, res in zip(due, results):
                    if isinstance(res, Exception) or res is None:
                        continue
                    new_status = res
                    if new_status == "still_valid":
                        conn.execute(
                            sql_text(
                                "UPDATE memories SET status = 'confirmed', "
                                "confidence = 1.0, last_verified_at = :ts, "
                                "updated_at = :ts WHERE id = CAST(:id AS uuid)"
                            ),
                            {"ts": now, "id": it["id"]},
                        )
                        it["status"] = "confirmed"
                    else:
                        # uncertain：保持 to_verify，仅打 last_verified_at 限速戳
                        conn.execute(
                            sql_text(
                                "UPDATE memories SET last_verified_at = :ts "
                                "WHERE id = CAST(:id AS uuid)"
                            ),
                            {"ts": now, "id": it["id"]},
                        )

        for it in items:
            date = _fmt_date(it["created_at"])
            marker = "[待确认] " if it["status"] == "to_verify" else ""
            base = f"({date}) {marker}{it['summary']}" if date else f"{marker}{it['summary']}"
            snippets.append(base)
            if it.get("dist") is not None:
                distances.append(round(it["dist"], 3))
    except Exception as e:
        log.debug("recall uid=%s 失败：%s", uid_str, e)

    log.info(
        "recall uid=%s query=%r → %d hits%s",
        uid_str, user_text[:40], len(snippets),
        ("：" + " | ".join(s[:40] for s in snippets[:3])) if snippets else "",
    )
    audit(
        "memory_recall",
        user_id=user_id,
        query=user_text[:200],
        hits=len(snippets),
        snippets=[s[:200] for s in snippets[:top_k]],
        distances=distances[:top_k],
        max_distance_threshold=RECALL_MAX_DISTANCE,
    )
    return snippets[:top_k]


# ============ 短期 buffer ============

def note_turn(user_id: int, user_text: str, assistant_text: str) -> None:
    """同步追加到该用户的 buffer（不触发 IO）。"""
    uid = _uid(user_id)
    buf = _buffer_per_user.setdefault(uid, [])
    buf.append({"role": "user", "content": user_text})
    buf.append({"role": "assistant", "content": assistant_text})


# ============ Flush（抽取 + 入库）============

async def maybe_flush(user_id: int | None = None, *, force: bool = False) -> bool:
    """按条件 flush。返回是否实际 flush 了至少一个用户。

    user_id=None：遍历所有 buffer 非空的用户（scheduler 周期性扫盘用）。
    """
    if user_id is None:
        any_flushed = False
        for uid in list(_buffer_per_user.keys()):
            try:
                if await _flush_one(uid, force=force):
                    any_flushed = True
            except Exception as e:
                log.debug("flush %s err: %s", uid, e)
        return any_flushed
    return await _flush_one(_uid(user_id), force=force)


async def _flush_one(uid: str, *, force: bool = False) -> bool:
    now = time.time()
    buf = _buffer_per_user.get(uid)
    if not buf:
        return False
    last_ts = _last_flush_ts_per_user.get(uid, 0.0)
    turns = len(buf) // 2
    should = force or turns >= FLUSH_EVERY_N_TURNS or (
        turns > 0 and now - last_ts > FLUSH_INTERVAL_SEC
    )
    if not should:
        return False

    async with _flush_lock:
        buf = _buffer_per_user.get(uid)
        if not buf:
            return False
        batch = buf.copy()
        _buffer_per_user[uid] = []
        _last_flush_ts_per_user[uid] = now

    user_id_int = int(uid) if uid.lstrip("-").isdigit() else 0
    audit_uid: Any = user_id_int or uid

    # 落盘 batch（调试 + 失败回放用）
    path = _buffer_dir() / f"conv_{uid}_{int(now)}.json"
    try:
        path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("flush write conv json err: %s", e)

    # 抽取
    try:
        items = await _extract_items(batch)
    except Exception as e:
        log.exception("extract failed uid=%s: %s", uid, e)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              error=f"extract:{type(e).__name__}:{str(e)[:200]}")
        # 回滚到 buffer 头部
        async with _flush_lock:
            _buffer_per_user.setdefault(uid, [])[:0] = batch
        return False

    if not items:
        log.info("memorize uid=%s ok (%d msgs) -> %s, +0 items（LLM 没抽到）",
                 uid, len(batch), path.name)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              new_items=0, new_item_summaries=[])
        # 仍然 fire persona update（用户也许聊了内容只是没产生 profile/event）
        _fire_persona_update(user_id_int, batch)
        # 偏好不一定产生 profile/event（"叫我名字"是 prompt 调整不是事实），这里也要 fire
        _fire_feedback_check(user_id_int, batch)
        return True

    # 入库
    try:
        new_records = await _persist_items(user_id_int, items, evidence_ref=str(path))
    except Exception as e:
        log.exception("persist failed uid=%s: %s", uid, e)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              error=f"persist:{type(e).__name__}:{str(e)[:200]}")
        return False

    new_summaries = [r["summary"] for r in new_records]
    log.info("memorize uid=%s ok (%d msgs) -> %s, +%d items",
             uid, len(batch), path.name, len(new_summaries))
    audit(
        "memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
        new_items=len(new_summaries),
        new_item_summaries=[s[:200] for s in new_summaries[:10]],
    )

    _fire_persona_update(user_id_int, batch)
    _fire_conflict_check(user_id_int, new_records)
    _fire_feedback_check(user_id_int, batch)
    return True


def _fire_persona_update(user_id: int, batch: list[dict[str, str]]) -> None:
    """memorize 成功后异步 fire persona 增量更新。失败静默不阻塞主链路。"""
    if not user_id:
        return

    async def _go():
        try:
            from . import persona  # 延迟避免循环 import
            await persona.update_state(user_id, batch)
        except Exception as e:
            log.debug("persona update post-flush err: %s", e)

    try:
        asyncio.create_task(_go())
    except RuntimeError:
        # 没有 running loop（如脚本同步上下文调用），跳过
        pass


def _fire_feedback_check(user_id: int, batch: list[dict[str, str]]) -> None:
    """flush 后异步 fire feedback sub-agent。粗筛 → sonnet 精判 → 落库 prompt_overrides / skill。"""
    if not user_id or not batch:
        return

    async def _go():
        try:
            from . import feedback_agent  # 延迟避免循环 import
            await feedback_agent.process(user_id, batch)
        except Exception as e:
            log.debug("feedback agent post-flush err: %s", e)

    try:
        asyncio.create_task(_go())
    except RuntimeError:
        pass


def _fire_conflict_check(user_id: int, new_records: list[dict[str, Any]]) -> None:
    """异步对每条新事实跑影响分析，把可能失效的旧 profile 标 to_verify / stale。

    PRD v2 / 5.1：写入时筛 to_verify。

    新事实和旧事实的关系组合：
    - 新 profile / 新 event 都可能让旧 profile 失效（"我搬上海了" event → "住北京" profile stale）
    - 旧 event 不会变 stale（历史时点不可被未来事件改写），所以候选 SQL 限定 profile
    - 同 batch 同伴排除：4 条事实同一段对话里同时抽出的，本就 mutually consistent，
      不应互判 to_verify（避免 LLM 把同时陈述的两个独立事实硬扯成依赖）

    失败静默不阻塞 flush 主链路。
    """
    if not user_id or not new_records:
        return
    batch_ids = [r["id"] for r in new_records if r.get("id")]

    async def _go():
        try:
            for new in new_records:
                await _check_conflicts_for_one(user_id, new, exclude_ids=batch_ids)
        except Exception as e:
            log.debug("conflict check post-flush err: %s", e)

    try:
        asyncio.create_task(_go())
    except RuntimeError:
        pass


CONFLICT_TOPK = 5  # 每条新 profile 召回多少旧 profile 候选做判断


async def _check_conflicts_for_one(
    user_id: int,
    new: dict[str, Any],
    *,
    exclude_ids: list[str] | None = None,
) -> None:
    """对一条新事实：召回 top-N 旧 profile → LLM 判 verdicts → UPDATE 旧条目 status。

    候选限定 profile（event 不变 stale），且排除 exclude_ids（同 batch 同伴）。
    """
    new_id = new.get("id")
    new_summary = new.get("summary") or ""
    vec = new.get("embedding")
    if not new_id or not new_summary or vec is None:
        return

    exclude = list(exclude_ids or [])
    if new_id not in exclude:
        exclude.append(new_id)

    eng = memory_store.engine()
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT id::text AS id, summary FROM memories "
                "WHERE user_id = :uid AND memory_type = 'profile' "
                "AND status != 'stale' "
                "AND NOT (id::text = ANY(:excl)) "
                "ORDER BY embedding <=> CAST(:q AS vector) "
                "LIMIT :k"
            ),
            {"uid": user_id, "excl": exclude,
             "q": embed_client.vec_literal(vec), "k": CONFLICT_TOPK},
        ).fetchall()
    candidates = [(r[0], r[1]) for r in rows]
    if not candidates:
        return

    # LLM 判
    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        return
    prompt = memory_prompts.render_conflict_check(new_summary, candidates)
    try:
        from . import openrouter
        res = await openrouter.chat(
            [{"role": "user", "content": prompt}],
            model=model, temperature=0.1, max_tokens=2048,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
    except Exception as e:
        log.warning("conflict check LLM err uid=%s new=%s: %s", user_id, new_id[:8], e)
        return

    verdicts = _parse_verdicts(raw, valid_ids={c[0] for c in candidates})
    if not verdicts:
        if raw:
            log.warning("conflict check 解析 0 verdicts uid=%s raw=%r", user_id, raw[:200])
        return

    # 真正更新
    flips: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc)
    with eng.begin() as conn:
        for old_id, verdict in verdicts.items():
            if verdict == "still_valid":
                # 标 last_verified_at（5.2/5.3 才会读，但便宜就一并写了）
                conn.execute(
                    sql_text(
                        "UPDATE memories SET last_verified_at = :ts "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": old_id},
                )
            elif verdict in ("to_verify", "stale"):
                new_conf = 0.0 if verdict == "stale" else 0.5
                # 把触发该变化的新事实 id 追加进 old.depends_on（去重，COALESCE 处理 NULL）
                conn.execute(
                    sql_text(
                        "UPDATE memories SET status = :s, confidence = :c, "
                        "updated_at = :ts, "
                        "depends_on = (SELECT ARRAY(SELECT DISTINCT unnest("
                        "  COALESCE(depends_on, ARRAY[]::uuid[]) || ARRAY[CAST(:dep AS uuid)]"
                        ")) FROM memories WHERE id = CAST(:id AS uuid)) "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"s": verdict, "c": new_conf, "ts": now,
                     "id": old_id, "dep": new_id},
                )
                flips.append((old_id, verdict))

    if flips:
        log.info(
            "conflict check uid=%s new=%s 触发 %d 个旧条目变更：%s",
            user_id, new_id[:8], len(flips),
            " | ".join(f"{oid[:8]}→{v}" for oid, v in flips[:5]),
        )
    cand_map = {oid: s for oid, s in candidates}
    audit(
        "memory_conflict_check",
        user_id=user_id,
        new_id=new_id,
        new_summary=new_summary[:200],
        candidates=len(candidates),
        candidate_list=[{"id": oid, "summary": s[:200]} for oid, s in candidates],
        flips=[
            {"id": oid, "verdict": v, "summary": cand_map.get(oid, "")[:200]}
            for oid, v in flips
        ],
    )


_VERDICT_RE = re.compile(
    r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"verdict"\s*:\s*"(still_valid|to_verify|stale)"\s*\}',
    re.DOTALL,
)

_REVERIFY_RE = re.compile(
    r'"verdict"\s*:\s*"(still_valid|uncertain)"',
    re.DOTALL,
)


async def _reverify_one(
    user_id: int, item: dict[str, Any], query: str,
) -> str | None:
    """对一条 to_verify 跑 LLM 反验证。返回 'still_valid' / 'uncertain' / None（失败）。

    PRD v2 / 5.2：用 deps 上游 + 当前 query 喂 LLM。LLM 不能返 stale（保守约束在 prompt
    里说明），调用方据此决定 UPDATE 行为。
    """
    fact = item.get("summary") or ""
    if not fact:
        return None

    # 拉上游事实 summary（按 created_at desc，最近的 N 条）
    upstream: list[str] = []
    deps = item.get("depends_on") or []
    if deps:
        eng = memory_store.engine()
        try:
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary FROM memories "
                        "WHERE id = ANY(:ids) "
                        "ORDER BY created_at DESC LIMIT :n"
                    ),
                    {"ids": deps, "n": REVERIFY_UPSTREAM_LIMIT},
                ).fetchall()
            upstream = [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug("reverify pull upstream err uid=%s id=%s: %s",
                      user_id, item["id"][:8], e)

    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        return None
    prompt = memory_prompts.render_reverify(fact, upstream, query)

    started = time.time()
    try:
        from . import openrouter
        res = await asyncio.wait_for(
            openrouter.chat(
                [{"role": "user", "content": prompt}],
                model=model, temperature=0.1, max_tokens=512,
            ),
            timeout=REVERIFY_TIMEOUT_SEC,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
    except asyncio.TimeoutError:
        log.warning("reverify timeout uid=%s id=%s", user_id, item["id"][:8])
        audit("memory_reverify", user_id=user_id, fact_id=item["id"],
              fact=fact[:200], verdict="timeout",
              latency_ms=int((time.time() - started) * 1000))
        return None
    except Exception as e:
        log.warning("reverify LLM err uid=%s id=%s: %s",
                    user_id, item["id"][:8], e)
        return None

    verdict, reason = _parse_reverify(raw)
    latency_ms = int((time.time() - started) * 1000)
    audit(
        "memory_reverify",
        user_id=user_id,
        fact_id=item["id"],
        fact=fact[:200],
        upstream=[u[:200] for u in upstream],
        query=(query or "")[:200],
        verdict=verdict or "parse_fail",
        reason=reason[:300],
        latency_ms=latency_ms,
    )
    if verdict is None:
        log.warning("reverify 解析失败 uid=%s id=%s raw=%r",
                    user_id, item["id"][:8], raw[:160])
    else:
        log.info("reverify uid=%s id=%s → %s (%dms)",
                 user_id, item["id"][:8], verdict, latency_ms)
    return verdict


def _parse_reverify(raw: str) -> tuple[str | None, str]:
    """返回 (verdict, reason)。verdict ∈ {still_valid, uncertain, None}。"""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    for src in (raw, None):
        if src is None:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            src = m.group(0) if m else None
            if src is None:
                break
        try:
            d = json.loads(src)
            if isinstance(d, dict):
                v = d.get("verdict")
                r = (d.get("reason") or "").strip()
                if v in ("still_valid", "uncertain"):
                    return v, r
        except Exception:
            continue
    m = _REVERIFY_RE.search(raw)
    if m:
        return m.group(1), ""
    return None, ""


# ============ Auto Dream（PRD v2 / 5.3）============

_DREAM_RE = re.compile(
    r'"verdict"\s*:\s*"(still_valid|uncertain|stale)"',
    re.DOTALL,
)


def _parse_dream(raw: str) -> tuple[str | None, str]:
    """LLM 输出 → (verdict, reason)。verdict ∈ {still_valid, uncertain, stale, None}。"""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    for src in (raw, None):
        if src is None:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            src = m.group(0) if m else None
            if src is None:
                break
        try:
            d = json.loads(src)
            if isinstance(d, dict):
                v = d.get("verdict")
                r = (d.get("reason") or "").strip()
                if v in ("still_valid", "uncertain", "stale"):
                    return v, r
        except Exception:
            continue
    m = _DREAM_RE.search(raw)
    if m:
        return m.group(1), ""
    return None, ""


async def _dream_one(
    user_id: int, item: dict[str, Any],
) -> str | None:
    """对一条 to_verify 跑 dream 三态判定。返回 verdict 或 None（失败）。

    与 _reverify_one 区别：
    - 用 embedding 召回 top-K 同 user **confirmed** 条目作为上下文（不是 query）
    - LLM 可输出 stale（5.3 是后台批处理，可激进）
    - 超时窗口更宽松（15s vs 8s）
    """
    fact_id = item.get("id")
    fact = item.get("summary") or ""
    vec = item.get("embedding")
    if not fact_id or not fact:
        return None

    eng = memory_store.engine()
    upstream: list[str] = []
    deps = item.get("depends_on") or []
    if deps:
        try:
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary FROM memories WHERE id = ANY(:ids) "
                        "ORDER BY created_at DESC LIMIT :n"
                    ),
                    {"ids": deps, "n": REVERIFY_UPSTREAM_LIMIT},
                ).fetchall()
            upstream = [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug("dream upstream pull err id=%s: %s", fact_id[:8], e)

    # 召回邻居（同 user 同 type 的 confirmed 条目，不含自身）
    neighbors: list[str] = []
    if vec is not None:
        try:
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary FROM memories "
                        "WHERE user_id = :uid AND status = 'confirmed' "
                        "AND id != CAST(:fid AS uuid) "
                        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                    ),
                    {"uid": user_id, "fid": fact_id,
                     "q": embed_client.vec_literal(vec),
                     "k": DREAM_NEIGHBOR_TOP_K},
                ).fetchall()
            neighbors = [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug("dream neighbor pull err id=%s: %s", fact_id[:8], e)

    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        return None
    prompt = memory_prompts.render_dream(fact, upstream, neighbors)

    started = time.time()
    try:
        from . import openrouter
        res = await asyncio.wait_for(
            openrouter.chat(
                [{"role": "user", "content": prompt}],
                model=model, temperature=0.1, max_tokens=512,
            ),
            timeout=DREAM_TIMEOUT_SEC,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
    except asyncio.TimeoutError:
        log.warning("dream timeout uid=%s id=%s", user_id, fact_id[:8])
        audit("memory_dream_one", user_id=user_id, fact_id=fact_id,
              fact=fact[:200], verdict="timeout",
              latency_ms=int((time.time() - started) * 1000))
        return None
    except Exception as e:
        log.warning("dream LLM err uid=%s id=%s: %s", user_id, fact_id[:8], e)
        return None

    verdict, reason = _parse_dream(raw)
    latency_ms = int((time.time() - started) * 1000)
    audit(
        "memory_dream_one",
        user_id=user_id,
        fact_id=fact_id,
        fact=fact[:200],
        upstream=[u[:200] for u in upstream],
        neighbors=[n[:200] for n in neighbors],
        verdict=verdict or "parse_fail",
        reason=reason[:300],
        latency_ms=latency_ms,
    )
    if verdict is None:
        log.warning("dream 解析失败 uid=%s id=%s raw=%r",
                    user_id, fact_id[:8], raw[:160])
    else:
        log.info("dream uid=%s id=%s → %s (%dms)",
                 user_id, fact_id[:8], verdict, latency_ms)
    return verdict


async def auto_dream(user_id: int) -> dict[str, Any]:
    """对该用户所有 to_verify 条目跑一遍三态 dream 判定。

    PRD v2 / 5.3：每天一次（搭便车 03:13 cron）。
    - still_valid → 升 confirmed (conf=1.0, last_verified_at=now)
    - stale → 降 stale (conf=0.0)
    - uncertain → 仅 last_verified_at=now（不动 status）

    返回汇总 dict 给 audit。
    """
    eng = memory_store.engine()
    started = time.time()
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT id::text AS id, summary, embedding::text AS embedding, "
                "depends_on FROM memories "
                "WHERE user_id = :uid AND status = 'to_verify' "
                "ORDER BY created_at"
            ),
            {"uid": user_id},
        ).fetchall()
    items = [
        {"id": r[0], "summary": r[1], "embedding": r[2], "depends_on": r[3]}
        for r in rows
    ]
    if not items:
        log.info("auto_dream uid=%s: 0 条 to_verify，跳过", user_id)
        return {"reviewed": 0, "to_confirmed": 0, "to_stale": 0,
                "uncertain": 0, "errors": 0}

    log.info("auto_dream uid=%s: %d 条 to_verify 待整理", user_id, len(items))
    counts = {"reviewed": 0, "to_confirmed": 0, "to_stale": 0,
              "uncertain": 0, "errors": 0}

    now = datetime.now(timezone.utc)
    for it in items:
        try:
            verdict = await _dream_one(user_id, it)
        except Exception as e:
            log.warning("dream err uid=%s id=%s: %s", user_id, it["id"][:8], e)
            counts["errors"] += 1
            await asyncio.sleep(DREAM_BATCH_SLEEP_SEC)
            continue
        counts["reviewed"] += 1
        if verdict is None:
            counts["errors"] += 1
        elif verdict == "still_valid":
            with eng.begin() as conn:
                conn.execute(
                    sql_text(
                        "UPDATE memories SET status = 'confirmed', "
                        "confidence = 1.0, last_verified_at = :ts, "
                        "updated_at = :ts WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": it["id"]},
                )
            counts["to_confirmed"] += 1
        elif verdict == "stale":
            with eng.begin() as conn:
                conn.execute(
                    sql_text(
                        "UPDATE memories SET status = 'stale', "
                        "confidence = 0.0, last_verified_at = :ts, "
                        "updated_at = :ts WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": it["id"]},
                )
            counts["to_stale"] += 1
        else:  # uncertain
            with eng.begin() as conn:
                conn.execute(
                    sql_text(
                        "UPDATE memories SET last_verified_at = :ts "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": it["id"]},
                )
            counts["uncertain"] += 1
        await asyncio.sleep(DREAM_BATCH_SLEEP_SEC)

    elapsed_ms = int((time.time() - started) * 1000)
    summary = {**counts, "latency_ms": elapsed_ms}
    log.info("auto_dream uid=%s 完成：%s", user_id, summary)
    audit("memory_dream", user_id=user_id, **summary)
    return summary


# ============ Auto Dream override（PRD 5.3 扩展，整理 prompt_overrides）============

OVERRIDE_DREAM_TIMEOUT_SEC = 30.0
# 仅当 active overrides 至少有这么多条才跑（少于 2 条无可冲突）
OVERRIDE_DREAM_MIN_COUNT = 2


async def auto_dream_overrides(user_id: int) -> dict[str, Any]:
    """整理该 user 的 active prompt_overrides——合并冗余 / 删除矛盾或过期。

    搭便车 auto_dream 同 cron（03:13 CST）。每个 user 跑一次 sonnet：
    - 拉所有 active overrides
    - 输出 {merge_groups, disable_ids}
    - 应用：INSERT 合并条目（status='active' approved_by=0）+ disable 老 ids
    """
    from . import storage as _storage
    started = time.time()
    overrides = _storage.list_active_overrides(user_id)
    if len(overrides) < OVERRIDE_DREAM_MIN_COUNT:
        log.info("override_dream uid=%s: 仅 %d 条 active，跳过", user_id, len(overrides))
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0}

    # active trigger（带 cron）：不能参与 merge（merge 会丢 cron / condition_prompt）；
    # 但**可以被 disable**——如果几条 trigger 互相重叠/冲突，留最优的、disable 其它
    valid_ids = {o.id for o in overrides}
    triggered_ids = {o.id for o in overrides if (o.trigger_kind or "passive") == "active"}

    prompt = memory_prompts.render_override_dream(overrides)
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="main", temperature=0.1, max_tokens=4000,
            ),
            timeout=OVERRIDE_DREAM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("override_dream timeout uid=%s", user_id)
        audit("override_dream", user_id=user_id, error="timeout",
              reviewed=len(overrides), merged=0, disabled=0,
              latency_ms=int((time.time() - started) * 1000))
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0, "error": "timeout"}
    except Exception as e:
        log.warning("override_dream LLM err uid=%s: %s", user_id, e)
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0, "error": str(e)}

    if not isinstance(d, dict):
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0, "error": "parse_fail"}

    merge_groups = d.get("merge_groups") or []
    disable_ids = d.get("disable_ids") or []
    disable_reasons = d.get("disable_reasons") or {}

    n_merged = 0
    n_disabled = 0
    actions: list[dict[str, Any]] = []
    touched_ids: set[int] = set()  # 防止同一 id 被两条规则同时处理

    # 先处理 merge_groups
    for grp in merge_groups:
        if not isinstance(grp, dict):
            continue
        ids = [int(x) for x in (grp.get("ids") or []) if isinstance(x, (int, str))]
        ids = [i for i in ids if i in valid_ids and i not in triggered_ids and i not in touched_ids]
        merged_text = (grp.get("merged_text") or "").strip()
        reason = (grp.get("reason") or "").strip()
        if len(ids) < 2 or not merged_text:
            continue
        # 注释：不做 hard_guardrail 复检——merged 只是合并已 active 的内容，源已通过过
        try:
            from . import storage as __st
            new_id = __st.add_override(
                user_id=user_id,
                text=merged_text,
                reason=f"override_dream merge: {reason}" if reason else "override_dream merge",
                source_user_msg=f"merged from #{','.join(map(str, ids))}",
                risk_level="low",
                status="active",
                approved_by=0,
            )
            for oid in ids:
                __st.set_override_status(oid, "disabled")
                touched_ids.add(oid)
            n_merged += 1
            actions.append({
                "kind": "merge", "merged_into": new_id,
                "from_ids": ids, "merged_text": merged_text[:200], "reason": reason[:200],
            })
        except Exception as e:
            log.warning("override_dream merge err uid=%s: %s", user_id, e)

    # 再处理 disable_ids（active trigger 可以被 disable，仅 merge 时才排除）
    for x in disable_ids:
        try:
            oid = int(x)
        except Exception:
            continue
        if oid not in valid_ids or oid in touched_ids:
            continue
        try:
            from . import storage as __st
            ok = __st.set_override_status(oid, "disabled")
            if ok:
                n_disabled += 1
                actions.append({
                    "kind": "disable", "id": oid,
                    "reason": disable_reasons.get(str(oid), "")[:200],
                })
                touched_ids.add(oid)
        except Exception as e:
            log.warning("override_dream disable err uid=%s id=%s: %s", user_id, oid, e)

    elapsed_ms = int((time.time() - started) * 1000)
    summary = {
        "reviewed": len(overrides),
        "merged": n_merged,  # 几个合并 group
        "disabled": n_disabled,  # 单独 disable 几个
        "latency_ms": elapsed_ms,
    }
    log.info("override_dream uid=%s 完成：%s", user_id, summary)
    audit("override_dream", user_id=user_id, **summary, actions=actions[:20])
    return summary


SKILL_DREAM_TIMEOUT_SEC = 30.0
SKILL_DREAM_MIN_COUNT = 2
SKILL_CREATOR_PROTECTED_NAME = "skill_creator"  # 不参与整理


async def auto_dream_skills() -> dict[str, Any]:
    """整理跨用户 skill 库——合并语义重复 / disable 失效。

    全表扫一次（不是 per-user），每天 03:13 跟 auto_dream / auto_dream_overrides 同班车跑。
    保护 name='skill_creator' 这条 meta-skill 不动。
    """
    from . import storage as _storage
    from . import embed_client as _ec
    started = time.time()
    skills = _storage.list_skills(status="active", limit=500)
    # 排除 meta-skill
    candidates = [sk for sk in skills if sk.name != SKILL_CREATOR_PROTECTED_NAME]
    if len(candidates) < SKILL_DREAM_MIN_COUNT:
        log.info("skill_dream: 仅 %d 条非 meta active skill，跳过", len(candidates))
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0}

    valid_ids = {sk.id for sk in candidates}

    prompt = memory_prompts.render_skill_dream(candidates)
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="main", temperature=0.1, max_tokens=4000,
            ),
            timeout=SKILL_DREAM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("skill_dream timeout")
        audit("skill_dream", error="timeout", reviewed=len(candidates),
              merged=0, disabled=0, latency_ms=int((time.time() - started) * 1000))
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0, "error": "timeout"}
    except Exception as e:
        log.warning("skill_dream LLM err: %s", e)
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0, "error": str(e)}

    if not isinstance(d, dict):
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0, "error": "parse_fail"}

    merge_groups = d.get("merge_groups") or []
    disable_ids = d.get("disable_ids") or []
    disable_reasons = d.get("disable_reasons") or {}

    n_merged = 0
    n_disabled = 0
    actions: list[dict[str, Any]] = []
    touched: set[int] = set()

    for grp in merge_groups:
        if not isinstance(grp, dict):
            continue
        ids = [int(x) for x in (grp.get("ids") or []) if isinstance(x, (int, str))]
        ids = [i for i in ids if i in valid_ids and i not in touched]
        merged_name = (grp.get("merged_name") or "").strip()
        merged_summary = (grp.get("merged_summary") or "").strip()
        merged_body = (grp.get("merged_body") or "").strip()
        reason = (grp.get("reason") or "").strip()
        if len(ids) < 2 or not merged_name or not merged_body:
            continue

        # 合并 usage_count（保留累计被复用次数，反映真实价值）
        sum_usage = sum(sk.usage_count or 0 for sk in candidates if sk.id in ids)
        # 取第一个被合并的 created_by 当代表
        first_creator = next(
            (sk.created_by for sk in candidates if sk.id in ids), 0,
        )

        # 算 embedding（合并 summary + body 一起 embed）
        try:
            vec = await _ec.embed_one(merged_summary + " | " + merged_body)
        except Exception as e:
            log.debug("skill_dream embed err: %s", e)
            vec = None
        if vec is None:
            log.warning("skill_dream merge skip uid=meta: embed 失败 group %s", ids)
            continue

        try:
            new_skill_id = _storage.add_skill(
                name=merged_name[:64],
                summary=merged_summary[:200],
                body=merged_body,
                embedding=vec,
                created_by=first_creator,
            )
            # 累加 usage_count（add_skill 默认 0）
            if sum_usage > 0:
                with _storage.session() as s:
                    sk_new = s.query(_storage.Skill).filter(_storage.Skill.id == new_skill_id).first()
                    if sk_new:
                        sk_new.usage_count = sum_usage
                        s.commit()
            for sid in ids:
                _storage.set_skill_status(sid, "disabled")
                touched.add(sid)
            n_merged += 1
            actions.append({
                "kind": "merge", "merged_into": new_skill_id,
                "from_ids": ids, "merged_name": merged_name,
                "merged_summary": merged_summary[:200],
                "preserved_usage": sum_usage, "reason": reason[:200],
            })
        except Exception as e:
            log.warning("skill_dream merge err: %s", e)

    for x in disable_ids:
        try:
            sid = int(x)
        except Exception:
            continue
        if sid not in valid_ids or sid in touched:
            continue
        try:
            ok = _storage.set_skill_status(sid, "disabled")
            if ok:
                n_disabled += 1
                actions.append({
                    "kind": "disable", "id": sid,
                    "reason": disable_reasons.get(str(sid), "")[:200],
                })
                touched.add(sid)
        except Exception as e:
            log.warning("skill_dream disable err id=%s: %s", sid, e)

    elapsed_ms = int((time.time() - started) * 1000)
    summary = {
        "reviewed": len(candidates),
        "merged": n_merged,
        "disabled": n_disabled,
        "latency_ms": elapsed_ms,
    }
    log.info("skill_dream 完成：%s", summary)
    audit("skill_dream", **summary, actions=actions[:30])
    return summary


def _parse_verdicts(raw: str, *, valid_ids: set[str]) -> dict[str, str]:
    """LLM 输出的 JSON → dict[id → verdict]。strict json 优先，失败回退 regex。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    out: dict[str, str] = {}
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

    if isinstance(parsed, dict) and isinstance(parsed.get("verdicts"), list):
        for v in parsed["verdicts"]:
            if not isinstance(v, dict):
                continue
            cid = v.get("id")
            verdict = v.get("verdict")
            if cid in valid_ids and verdict in ("still_valid", "to_verify", "stale"):
                out[cid] = verdict
        return out

    # 兜底
    for m in _VERDICT_RE.finditer(raw):
        cid, verdict = m.group(1), m.group(2)
        if cid in valid_ids:
            out[cid] = verdict
    return out


# ============ 抽取（LLM → JSON → list[(type, content)]）============

def _format_resource(batch: list[dict[str, str]]) -> str:
    """[{role, content}, ...] → prompt 里的对话片段。"""
    lines: list[str] = []
    for m in batch:
        role = "user" if m.get("role") == "user" else "assistant"
        content = (m.get("content") or "").strip().replace("\r", "")
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


_ITEM_RE = re.compile(
    r'\{\s*"type"\s*:\s*"(profile|event)"\s*,\s*"content"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*\}',
    re.DOTALL,
)


def _parse_items_loose(raw: str) -> list[tuple[str, str]]:
    """从 LLM 输出抠出 (type, content) pairs。

    第一道：strict json.loads 整体；第二道：regex 逐个对象抠（防 LLM 中间漏字段时全部失败）。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    # 去掉 ```json 围栏
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

    out: list[tuple[str, str]] = []
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        for it in parsed["items"]:
            if not isinstance(it, dict):
                continue
            t = (it.get("type") or "").strip()
            c = (it.get("content") or "").strip()
            if t in ("profile", "event") and c:
                out.append((t, c[:300]))
        return out

    # 兜底：strict 失败 → regex 逐对象抠
    for m in _ITEM_RE.finditer(raw):
        t = m.group(1)
        c = m.group(2).encode().decode("unicode_escape", errors="ignore").strip()
        if c:
            out.append((t, c[:300]))
    return out


async def _extract_items(batch: list[dict[str, str]]) -> list[tuple[str, str]]:
    """跑 LLM 抽取，返回 [(type, content), ...]。"""
    if not batch:
        return []
    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        log.warning("memory extract: 没设 MEMU_CHAT_MODEL/OPENROUTER_MODEL，跳过")
        return []
    resource = _format_resource(batch)
    user_prompt = memory_prompts.render(resource)

    try:
        # 用 openrouter.chat 直接调，不走 :18082 shim
        from . import openrouter
        res = await openrouter.chat(
            [{"role": "user", "content": user_prompt}],
            model=model,
            temperature=0.1,
            max_tokens=2048,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
        if not raw:
            log.warning("memory extract: LLM 空响应（model=%s）", model)
            return []
    except Exception as e:
        log.warning("memory extract LLM call failed: %s", e)
        return []

    items = _parse_items_loose(raw)
    if not items and raw:
        log.warning("memory extract: 解析失败 raw=%r", raw[:300])
    return items


# ============ 入库（embedding + INSERT）============

async def _persist_items(
    user_id: int,
    items: list[tuple[str, str]],
    *,
    evidence_ref: str | None = None,
) -> list[dict[str, Any]]:
    """对每条 (type, content) 算 embedding 并 INSERT 到 memories。

    返回 list[dict]，每条 dict 含 id / summary / memory_type / embedding（可能 None）；
    上层既用这个填 audit，也喂 _fire_conflict_check 复用 embedding。
    """
    if not items or not user_id:
        return []
    summaries = [c for _t, c in items]
    vecs = await embed_client.embed_many(summaries)

    eng = memory_store.engine()
    now = datetime.now(timezone.utc)
    inserted: list[dict[str, Any]] = []
    with eng.begin() as conn:
        for (t, c), vec in zip(items, vecs):
            new_id = str(_uuid.uuid4())
            params: dict[str, Any] = {
                "id": new_id,
                "user_id": user_id,
                "summary": c,
                "memory_type": t,
                "created_at": now,
                "updated_at": now,
                "evidence_ref": evidence_ref,
            }
            # status / confidence 必须显式给——这俩列在 ALTER 加的时候 NOT NULL，
            # raw INSERT 不走 ORM default、PG 也不会自动填 ALTER 设的 default。
            if vec is not None:
                params["embedding"] = embed_client.vec_literal(vec)
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, embedding, "
                        "created_at, updated_at, evidence_ref, status, confidence) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, :memory_type, "
                        "CAST(:embedding AS vector), :created_at, :updated_at, :evidence_ref, "
                        "'confirmed', 1.0)"
                    ),
                    params,
                )
            else:
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, "
                        "created_at, updated_at, evidence_ref, status, confidence) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, :memory_type, "
                        ":created_at, :updated_at, :evidence_ref, 'confirmed', 1.0)"
                    ),
                    params,
                )
            inserted.append({"id": new_id, "summary": c, "memory_type": t, "embedding": vec})
    return inserted
