"""本地 embed_server (:18080) 的薄客户端 + pgvector 字面量序列化。

memory_store / admin_ui 共用。
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)


def embed_url() -> str:
    s = settings()
    return f"http://{s.embed_server_host}:{s.embed_server_port}/v1/embeddings"


async def embed_one(text: str) -> list[float] | None:
    """单个文本→向量。失败返 None（不抛——上层决定要不要跳过 embedding）。"""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as c:
            r = await c.post(embed_url(), json={"model": "any", "input": [text]})
            r.raise_for_status()
            data = r.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        log.warning("embed_server 不可达，跳过 embedding：%s", e)
        return None


async def embed_many(texts: list[str]) -> list[list[float] | None]:
    """批量。失败的位置塞 None；保留长度对齐。"""
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.post(embed_url(), json={"model": "any", "input": texts})
            r.raise_for_status()
            data = r.json()
            # 按 index 排序兜底（OpenAI 兼容响应一般已按入序）
            entries = sorted(data["data"], key=lambda x: x.get("index", 0))
            return [e["embedding"] for e in entries]
    except Exception as e:
        log.warning("embed_server 批量失败，回退逐个 embed_one：%s", e)
        out: list[list[float] | None] = []
        for t in texts:
            out.append(await embed_one(t))
        return out


def vec_literal(vec: list[float]) -> str:
    """pgvector 字面量：`[0.1,0.2,...]`，用于 INSERT/UPDATE 时的参数化值。"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
