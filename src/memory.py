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

    格式：`(2026-05-15) 用户最近在减肥`
    """
    uid_str = _uid(user_id)
    snippets: list[str] = []
    try:
        vec = await embed_client.embed_one(user_text)
        if vec is None:
            log.debug("recall uid=%s: embed 失败，跳过 RAG", uid_str)
        else:
            eng = memory_store.engine()
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary, created_at FROM memories "
                        "WHERE user_id = :uid "
                        "ORDER BY embedding <=> CAST(:q AS vector) "
                        "LIMIT :k"
                    ),
                    {"uid": user_id, "q": embed_client.vec_literal(vec), "k": top_k},
                ).fetchall()
            for summary, created_at in rows:
                date = _fmt_date(created_at)
                snippets.append(f"({date}) {summary}" if date else str(summary))
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
        new_summaries = await _persist_items(user_id_int, items, evidence_ref=str(path))
    except Exception as e:
        log.exception("persist failed uid=%s: %s", uid, e)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              error=f"persist:{type(e).__name__}:{str(e)[:200]}")
        return False

    log.info("memorize uid=%s ok (%d msgs) -> %s, +%d items",
             uid, len(batch), path.name, len(new_summaries))
    audit(
        "memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
        new_items=len(new_summaries),
        new_item_summaries=[s[:200] for s in new_summaries[:10]],
    )

    _fire_persona_update(user_id_int, batch)
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
) -> list[str]:
    """对每条 (type, content) 算 embedding 并 INSERT 到 memories。返回 summary 列表（用于 audit）。"""
    if not items or not user_id:
        return []
    summaries = [c for _t, c in items]
    vecs = await embed_client.embed_many(summaries)

    eng = memory_store.engine()
    now = datetime.now(timezone.utc)
    inserted: list[str] = []
    with eng.begin() as conn:
        for (t, c), vec in zip(items, vecs):
            params: dict[str, Any] = {
                "id": str(_uuid.uuid4()),
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
            inserted.append(c)
    return inserted
