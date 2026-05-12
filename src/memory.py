"""memU 封装。

关键设计：
- 主动召回：每条用户消息到达时，先 retrieve 相关记忆，塞进 system prompt（即使 agent 最终没引用也算"想起来了"）。
- memorize：把新产生的 user/assistant 对话追加到 rolling buffer，每 N 条或达到时间窗口后 flush 成 JSON 文件，交给 memU 提取。
  （memU 的 memorize API 吃 JSON 文件，不是逐条流式。）
- LLM/embedding 都走 MiniMax 的 OpenAI 兼容端点，通过 llm_profiles 传入。
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

USER_ID = "me"  # 单用户 MVP 固定
FLUSH_EVERY_N_TURNS = 6
FLUSH_INTERVAL_SEC = 15 * 60  # 或 15 分钟强制 flush 一次

_service = None
_buffer: list[dict[str, str]] = []  # [{role, content}]
_last_flush_ts: float = 0.0
_flush_lock = asyncio.Lock()


def _buffer_dir() -> Path:
    d = settings().root / "data" / "memu_buffer"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


async def recall(user_text: str, *, top_k: int = 3) -> list[str]:
    """返回若干记忆片段（字符串），每条带形成日期。失败返回空 list，不阻塞主流程。

    snippet 格式：
    - item: `(2026-05-06) 用户倾向于平等的探讨交流...`
    - category: `【habits｜更新于 2026-05-11】用户在饮食上偏好快餐...`

    AI 看到日期能区分"刚聊到的"和"上个月就有的旧背景"。
    """
    try:
        svc = _get_service()
        queries = [{"role": "user", "content": {"text": user_text}}]
        result = await svc.retrieve(queries=queries, where={"user_id": USER_ID})
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

    # 让日志里能看到召回了什么，方便排查"AI 不记得"类问题
    log.info("recall query=%r → %d hits%s",
             user_text[:40], len(snippets),
             "：" + " | ".join(s[:40] for s in snippets[:3]) if snippets else "")
    audit("memory_recall", query=user_text[:200], hits=len(snippets),
          snippets=[s[:200] for s in snippets[:top_k]])
    return snippets[:top_k]


def note_turn(user_text: str, assistant_text: str) -> None:
    """同步追加到 buffer（不触发 IO）。"""
    _buffer.append({"role": "user", "content": user_text})
    _buffer.append({"role": "assistant", "content": assistant_text})


async def maybe_flush(force: bool = False) -> bool:
    """按条件 flush。返回是否实际 flush 了。"""
    global _last_flush_ts
    now = time.time()
    turns = len(_buffer) // 2
    should = force or turns >= FLUSH_EVERY_N_TURNS or (
        turns > 0 and now - _last_flush_ts > FLUSH_INTERVAL_SEC
    )
    if not should:
        return False

    async with _flush_lock:
        if not _buffer:
            return False
        batch = _buffer.copy()
        _buffer.clear()
        _last_flush_ts = now

    path = _buffer_dir() / f"conv_{int(now)}.json"
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        svc = _get_service()
        result = await svc.memorize(
            resource_url=str(path),
            modality="conversation",
            user={"user_id": USER_ID},
        )
        items = (result or {}).get("items") or []
        log.info("memorize ok (%d msgs) -> %s, +%d items", len(batch), path.name, len(items))
        audit("memory_flush", msgs=len(batch), file=path.name,
              new_items=len(items),
              new_item_summaries=[(it.get("summary") or it.get("content") or "")[:200]
                                  for it in items[:10]])
    except Exception as e:
        log.exception("memorize failed: %s", e)
        audit("memory_flush", msgs=len(batch), file=path.name, error=str(e)[:200])
        # 回滚到 buffer 头部，下次再试
        async with _flush_lock:
            _buffer[:0] = batch
        return False

    # memorize 成功后异步触发 persona 增量更新（失败静默，不影响主流程）
    async def _persona_update(b: list[dict[str, str]]) -> None:
        try:
            from . import persona  # 延迟避免循环 import
            await persona.update_state(b)
        except Exception as e:
            log.debug("persona update post-flush err: %s", e)

    asyncio.create_task(_persona_update(batch))
    return True
