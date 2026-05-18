"""把 postgres 里已有的英文记忆翻译成中文。

- 遍历 memories.summary（自搭记忆栈，2026-05-18 起；旧 memU 的 categories 已退役）
- 用 llm.chat 翻译；跳过已经是中文的（CJK 字符占比 ≥10%）
- 翻译完调本地 embed server 重算向量，否则检索会对不上新文本

用法：
  .venv/bin/python -m scripts.translate_memory_to_zh          # 正式跑
  .venv/bin/python -m scripts.translate_memory_to_zh --dry    # 只列出将翻译哪些
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

import httpx
from sqlalchemy import create_engine, text


def _setup_env() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "tr")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.minimaxi.com,api.anthropic.com"
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    return cjk / max(1, len(s))


def _is_english(s: str) -> bool:
    """简单判定：中文字符占比 <10% 就算英文。"""
    return _cjk_ratio(s) < 0.10


TRANSLATE_SYSTEM = (
    "你是个翻译工具。把输入的英文句子翻译成**自然的中文**，指代用户时一律用\"用户\"。\n"
    "要求：\n"
    "1) 只输出译文，不要加任何说明、标点修饰、前后缀。\n"
    "2) 尽量贴近原意，不额外加信息也不删信息。\n"
    "3) 人名、地名、专有名词保持原样或用通用中译。\n"
    "4) 不输出 <think>、引号包裹、markdown 格式。"
)


async def translate(text_in: str, *, llm_mod) -> str:
    out = await llm_mod.chat(
        [
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": text_in},
        ],
        temperature=0.2,
        max_tokens=1024,
        tier="aux",
    )
    return (out or "").strip()


async def _embed_one(text_in: str, embed_url: str) -> list[float] | None:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as c:
            r = await c.post(embed_url, json={"model": "any", "input": [text_in]})
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
    except Exception as e:
        print(f"  ⚠ embed 失败：{e!s:.80}", file=sys.stderr)
        return None


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


async def _call_with_retry(coro_factory, what: str) -> Any:
    for attempt in range(3):
        try:
            return await coro_factory()
        except Exception as e:
            msg = str(e)
            retriable = any(s in msg for s in ("529", "503", "overload", "rate limit", "Too Many"))
            if retriable and attempt < 2:
                wait = 3 * (attempt + 1)
                print(f"  ... retry {what} after {wait}s ({msg[:60]})")
                await asyncio.sleep(wait)
                continue
            raise


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只列出要翻译的条目，不写库")
    ap.add_argument("--sleep", type=float, default=1.5, help="每条之间间隔秒，默认 1.5")
    args = ap.parse_args()

    _setup_env()
    from src import llm
    from src.config import settings

    s = settings()
    if not s.memu_db_url:
        raise SystemExit("需要 MEMU_DB_URL（postgres URL）")
    eng = create_engine(s.memu_db_url, future=True)
    embed_url = f"http://{s.embed_server_host}:{s.embed_server_port}/v1/embeddings"

    with eng.connect() as c:
        items = c.execute(
            text("SELECT id::text AS id, memory_type, summary FROM memories ORDER BY created_at")
        ).mappings().all()

    tasks: list[tuple[str, str, str, str]] = []  # (kind, id, field, current_text)
    for it in items:
        if _is_english(it["summary"] or ""):
            tasks.append(("item", it["id"], "summary", it["summary"]))

    total = len(tasks)
    print(f"待翻译：{total} 条 memories.summary")
    if args.dry:
        for i, (kind, _id, field, txt) in enumerate(tasks, 1):
            print(f"  [{i}/{total}] {kind}.{field} · {txt[:70]}…")
        return
    if total == 0:
        print("没有英文条目，无需翻译。")
        return

    ok = fail = 0
    try:
        for i, (kind, row_id, field, original) in enumerate(tasks, 1):
            try:
                translated = await _call_with_retry(
                    lambda: translate(original, llm_mod=llm),
                    what=f"{kind}#{row_id[:8]}",
                )
                if not translated or _is_english(translated):
                    print(f"  [{i}/{total}] {kind}.{field} ✗ 译文异常：{translated[:60]!r}")
                    fail += 1
                    await asyncio.sleep(args.sleep); continue

                vec = await _embed_one(translated, embed_url)
                with eng.begin() as conn:
                    if vec is not None:
                        conn.execute(
                            text("UPDATE memories SET summary=:s, embedding=CAST(:v AS vector), "
                                 "updated_at=NOW() WHERE id=CAST(:id AS uuid)"),
                            {"s": translated, "v": _vec_literal(vec), "id": row_id},
                        )
                    else:
                        conn.execute(
                            text("UPDATE memories SET summary=:s, updated_at=NOW() "
                                 "WHERE id=CAST(:id AS uuid)"),
                            {"s": translated, "id": row_id},
                        )
                ok += 1
                print(f"  [{i}/{total}] {kind}.{field} ✓ → {translated[:60]}")
            except Exception as e:
                fail += 1
                print(f"  [{i}/{total}] {kind}.{field} ✗ {e!s:.100}")
            await asyncio.sleep(args.sleep)
    finally:
        await llm.aclose()

    print(f"\n完成：{ok} 成功 / {fail} 失败 / 总 {total}")


if __name__ == "__main__":
    asyncio.run(main())
