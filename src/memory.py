"""memU 封装。多用户版本（2026-05-12 起）。

关键设计：
- 主动召回：每条用户消息到达时，先 retrieve 相关记忆，塞进 system prompt（即使 agent 最终没引用也算"想起来了"）。
- memorize：把新产生的 user/assistant 对话追加到 rolling buffer，每 N 条或达到时间窗口后 flush 成 JSON 文件，交给 memU 提取。
  （memU 的 memorize API 吃 JSON 文件，不是逐条流式。）
- LLM 调用走本地 `:18082` shim（src/llm_proxy.py），按 `MEMU_CHAT_MODEL` 路由 OpenRouter 或 MiniMax。
- embedding 走本地 `:18080` shim（src/embed_server.py，bge-small-zh-v1.5）。
- **multi-user**：所有公共函数都吃 `user_id: int`（telegram chat_id）。模块级 buffer/flush_ts 是 dict[str_uid, ...]。
  memU SDK 的 `user_id` 字段全用 `str(chat_id)`——schema 是 TEXT。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from .audit_log import audit
from .config import settings

log = logging.getLogger(__name__)

FLUSH_EVERY_N_TURNS = 6
FLUSH_INTERVAL_SEC = 15 * 60  # 或 15 分钟强制 flush 一次

_service = None
# user_id (str) → buffer of {role, content}
_buffer_per_user: dict[str, list[dict[str, str]]] = {}
_last_flush_ts_per_user: dict[str, float] = {}
_flush_lock = asyncio.Lock()


def _uid(chat_id: int | str) -> str:
    """把 telegram chat_id 转成 memU 用的 str 形式。memU postgres user_id 列是 TEXT。"""
    return str(chat_id)


def _buffer_dir() -> Path:
    d = settings().root / "data" / "memu_buffer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_memu_postgres_schema() -> None:
    """memU SDK 升级会加新列（1.5.1 加了 memory_items.happened_at + extra），旧库没就 INSERT 全报
    UndefinedColumn → memorize 静默失败。这里做最小自愈——硬编码已知缺的列，启动时尝试 ALTER。
    失败静默；仅诊断用。"""
    s = settings()
    if s.memu_metadata_provider != "postgres" or not s.memu_db_url:
        return
    try:
        import psycopg  # type: ignore
    except Exception as e:
        log.debug("memU schema check skipped (no psycopg): %s", e)
        return
    # (table, column, type) — 已知 memU SDK 升级后会缺的列；以后再加只在这里追加
    known_additions = [
        ("memory_items", "happened_at", "TIMESTAMP WITHOUT TIME ZONE"),
        ("memory_items", "extra", "JSONB"),
    ]
    dsn = s.memu_db_url.replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for table, col, typ in known_additions:
                try:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" {typ}')
                except Exception as e:
                    log.debug("memU schema add %s.%s skipped: %s", table, col, e)
    except Exception as e:
        log.warning("memU schema 自愈失败（不致命）：%s", e)


def _get_service():
    global _service
    if _service is not None:
        return _service

    # 延迟 import，避免未装包时 import 链路就炸
    from memu.app import MemoryService  # type: ignore

    s = settings()
    embed_base = f"http://{s.embed_server_host}:{s.embed_server_port}/v1"
    # memU 内部 LLM 走本地 :18082 shim（src.llm_proxy）。shim 内部根据 MEMU_CHAT_MODEL 决定上游：
    # - 设了 MEMU_CHAT_MODEL → 走 OpenRouter（带 Clash 代理；推荐 deepseek-v4-flash）
    # - 空 → 走 MiniMax 直连（剥 <think>）
    # 不直连 OpenRouter 是因为 _purge_proxy_env() 清了代理环境变量，memU 内置 httpx 拿不到 Clash 出不去
    chat_base = f"http://{s.llm_proxy_host}:{s.llm_proxy_port}/v1"
    if s.memu_chat_model and s.openrouter_api_key:
        memu_chat_key = s.openrouter_api_key
        memu_chat_model = s.memu_chat_model
    else:
        memu_chat_key = s.minimax_api_key  # shim 透传到上游
        memu_chat_model = s.minimax_chat_model

    llm_profiles: dict[str, dict[str, Any]] = {
        "default": {
            "base_url": chat_base,
            "api_key": memu_chat_key,
            "chat_model": memu_chat_model,
            "client_backend": "httpx",
        },
        "embedding": {
            # 指向本地 OpenAI 兼容 embedding shim（src.embed_server）
            "base_url": embed_base,
            "api_key": "local",
            "embed_model": s.embed_model_name,
        },
    }

    if s.memu_metadata_provider == "postgres" and s.memu_db_url:
        database_config = {
            # memU 期望字段是 dsn（不是 url）；postgres 时自动挂 pgvector
            "metadata_store": {"provider": "postgres", "dsn": s.memu_db_url},
        }
        # SDK 升级时自动补缺的列（缺了的话所有 INSERT 报 UndefinedColumn → 0 入库）
        _ensure_memu_postgres_schema()
    else:
        database_config = {"metadata_store": {"provider": "inmemory"}}

    from .memu_prompts_zh import (  # 延迟 import 避免 memU 还没装时报错
        CATEGORY_SUMMARY_PROMPT_ZH,
        MEMORY_CATEGORIES_ZH,
        MEMORY_TYPE_PROMPTS_ZH,
    )

    _service = MemoryService(
        llm_profiles=llm_profiles,
        database_config=database_config,
        retrieve_config={"method": "rag"},
        memorize_config={
            "memory_type_prompts": MEMORY_TYPE_PROMPTS_ZH,
            "memory_categories": MEMORY_CATEGORIES_ZH,
            "default_category_summary_prompt": CATEGORY_SUMMARY_PROMPT_ZH,
        },
    )
    log.info(
        "memU service initialized (db=%s, chat=%s)",
        s.memu_metadata_provider,
        memu_chat_model,
    )
    return _service


def _fmt_date(ts_str: str) -> str:
    """memU 时间戳是 '2026-05-06 09:34:32.009219+00:00'。截前 10 位拿日期。"""
    if not ts_str:
        return ""
    return str(ts_str)[:10]


async def recall(user_id: int, user_text: str, *, top_k: int = 3) -> list[str]:
    """返回若干记忆片段（字符串），每条带形成日期。失败返回空 list，不阻塞主流程。"""
    uid = _uid(user_id)
    try:
        svc = _get_service()
        queries = [{"role": "user", "content": {"text": user_text}}]
        result = await svc.retrieve(queries=queries, where={"user_id": uid})
    except Exception as e:  # memU 首次 retrieve 前若无记忆会报错，容忍
        log.debug("recall skipped: %s", e)
        return []

    snippets: list[str] = []
    for item in (result.get("items") or [])[:top_k]:
        summary = (item.get("summary") or "").strip()
        if not summary:
            continue
        date = _fmt_date(item.get("created_at") or item.get("updated_at") or "")
        snippets.append(f"({date}) {summary}" if date else summary)
    if not snippets:
        for cat in (result.get("categories") or [])[:top_k]:
            summary = (cat.get("summary") or cat.get("description") or "").strip()
            if not summary:
                continue
            name = cat.get("name", "")
            date = _fmt_date(cat.get("updated_at") or cat.get("created_at") or "")
            head = f"{name}｜更新于 {date}" if date else name
            snippets.append(f"【{head}】{summary}")

    log.info("recall uid=%s query=%r → %d hits%s",
             uid, user_text[:40], len(snippets),
             "：" + " | ".join(s[:40] for s in snippets[:3]) if snippets else "")
    audit("memory_recall", user_id=user_id, query=user_text[:200], hits=len(snippets),
          snippets=[s[:200] for s in snippets[:top_k]])
    return snippets[:top_k]


def note_turn(user_id: int, user_text: str, assistant_text: str) -> None:
    """同步追加到该用户的 buffer（不触发 IO）。"""
    uid = _uid(user_id)
    buf = _buffer_per_user.setdefault(uid, [])
    buf.append({"role": "user", "content": user_text})
    buf.append({"role": "assistant", "content": assistant_text})


async def maybe_flush(user_id: int | None = None, *, force: bool = False) -> bool:
    """按条件 flush。返回是否实际 flush 了至少一个用户。

    user_id=None：遍历所有 buffer 非空的用户（scheduler 周期性扫盘用）。
    """
    if user_id is None:
        any_flushed = False
        for uid in list(_buffer_per_user.keys()):
            try:
                # 这里 uid 是已经转好的 str；恢复成 int 让下游签名一致
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

    path = _buffer_dir() / f"conv_{uid}_{int(now)}.json"
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        svc = _get_service()
        # memU 1.5.1 多用户 bug 绕过：MemoryService 实例上的 ctx.category_name_to_id
        # 缓存第一个 user 的 category UUIDs，后续用户的 memorize 用同一份缓存 →
        # category_items.category_id FK 违反（指向其他人的 category UUID）。
        # 强制 reset 让 _ensure_categories_ready 按当前 user_scope 重 init。
        try:
            ctx = svc._get_context()
            ctx.categories_ready = False
            ctx.category_init_task = None
            ctx.category_name_to_id = {}
            ctx.category_ids = []
        except Exception as e:
            log.debug("memU ctx reset skipped: %s", e)
        result = await svc.memorize(
            resource_url=str(path),
            modality="conversation",
            user={"user_id": uid},
        )
        items = (result or {}).get("items") or []
        log.info("memorize uid=%s ok (%d msgs) -> %s, +%d items",
                 uid, len(batch), path.name, len(items))
        audit("memory_flush", user_id=int(uid) if uid.lstrip("-").isdigit() else uid,
              msgs=len(batch), file=path.name,
              new_items=len(items),
              new_item_summaries=[(it.get("summary") or it.get("content") or "")[:200]
                                  for it in items[:10]])
    except Exception as e:
        log.exception("memorize failed (uid=%s): %s", uid, e)
        audit("memory_flush", user_id=int(uid) if uid.lstrip("-").isdigit() else uid,
              msgs=len(batch), file=path.name, error=str(e)[:200])
        # 回滚到 buffer 头部，下次再试
        async with _flush_lock:
            cur = _buffer_per_user.setdefault(uid, [])
            cur[:0] = batch
        return False

    # memorize 成功后异步触发 persona 增量更新（失败静默，不影响主流程）
    async def _persona_update(b: list[dict[str, str]], _uid_str: str) -> None:
        try:
            from . import persona  # 延迟避免循环 import
            _uid_int = int(_uid_str) if _uid_str.lstrip("-").isdigit() else 0
            if _uid_int:
                await persona.update_state(_uid_int, b)
        except Exception as e:
            log.debug("persona update post-flush err: %s", e)

    asyncio.create_task(_persona_update(batch, uid))
    return True
