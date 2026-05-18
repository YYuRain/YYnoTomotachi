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

# PRD v2 / 5.2：召回时反验证 to_verify 条目
REVERIFY_COOLDOWN_SEC = 30 * 60  # 同一条 to_verify 30min 内最多反验证一次
REVERIFY_TIMEOUT_SEC = 8.0       # 单条反验证 LLM 调用超时（保护 recall 整体延迟）
REVERIFY_UPSTREAM_LIMIT = 3      # 喂给 LLM 的上游事实最多 3 条

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
    try:
        vec = await embed_client.embed_one(user_text)
        if vec is None:
            log.debug("recall uid=%s: embed 失败，跳过 RAG", uid_str)
            audit("memory_recall", user_id=user_id, query=user_text[:200], hits=0, snippets=[])
            return []

        eng = memory_store.engine()
        with eng.connect() as conn:
            rows = conn.execute(
                sql_text(
                    "SELECT id::text AS id, summary, created_at, status, "
                    "last_verified_at, depends_on FROM memories "
                    "WHERE user_id = :uid AND status != 'stale' "
                    "ORDER BY embedding <=> CAST(:q AS vector) "
                    "LIMIT :k"
                ),
                {"uid": user_id, "q": embed_client.vec_literal(vec), "k": top_k},
            ).fetchall()

        items = [
            {
                "id": r[0], "summary": r[1], "created_at": r[2],
                "status": r[3], "last_verified_at": r[4], "depends_on": r[5],
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
    audit(
        "memory_conflict_check",
        user_id=user_id,
        new_id=new_id,
        new_summary=new_summary[:200],
        candidates=len(candidates),
        flips=[{"id": oid, "verdict": v} for oid, v in flips],
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

    verdict = _parse_reverify(raw)
    latency_ms = int((time.time() - started) * 1000)
    audit(
        "memory_reverify",
        user_id=user_id,
        fact_id=item["id"],
        fact=fact[:200],
        upstream=[u[:200] for u in upstream],
        query=(query or "")[:200],
        verdict=verdict or "parse_fail",
        latency_ms=latency_ms,
    )
    if verdict is None:
        log.warning("reverify 解析失败 uid=%s id=%s raw=%r",
                    user_id, item["id"][:8], raw[:160])
    else:
        log.info("reverify uid=%s id=%s → %s (%dms)",
                 user_id, item["id"][:8], verdict, latency_ms)
    return verdict


def _parse_reverify(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        d = json.loads(raw)
        v = d.get("verdict") if isinstance(d, dict) else None
        if v in ("still_valid", "uncertain"):
            return v
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            v = d.get("verdict") if isinstance(d, dict) else None
            if v in ("still_valid", "uncertain"):
                return v
        except Exception:
            pass
    m = _REVERIFY_RE.search(raw)
    if m:
        return m.group(1)
    return None


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
            if vec is not None:
                params["embedding"] = embed_client.vec_literal(vec)
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, embedding, "
                        "created_at, updated_at, evidence_ref) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, :memory_type, "
                        "CAST(:embedding AS vector), :created_at, :updated_at, :evidence_ref)"
                    ),
                    params,
                )
            else:
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, "
                        "created_at, updated_at, evidence_ref) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, :memory_type, "
                        ":created_at, :updated_at, :evidence_ref)"
                    ),
                    params,
                )
            inserted.append({"id": new_id, "summary": c, "memory_type": t, "embedding": vec})
    return inserted
