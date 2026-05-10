"""本地 OpenAI 兼容 chat-completion shim，**剥 <think> 块**后转发。

用途：memU 内部抽取/总结调 LLM 时，会把 raw response 写库。
     MiniMax-M2 输出带 `<think>...</think>`，memU 内置 OpenAI 客户端不知道剥，
     于是 `memory_categories.summary` 字段全被 think 内容污染（admin UI 直接可见）。

方案：在 memU 和 MiniMax 之间架一层 shim：
     - 监听本地端口（默认 18082），暴露 `/v1/chat/completions`
     - 收到请求 → httpx 转发到 MiniMax（base_url 来自 settings）
     - 拿到响应 → 把 message.content 里的 <think>...</think> 剥掉
     - 原样返回给 memU
     `memory.py` 的 default LLM profile 指向这个 shim 即可，对 memU 透明。

不做：
- 流式（memU 没用 stream）。stream=true 的请求直接报 501，避免静默错。
- /embeddings（embed_server.py 已经在 :18080 提供，分两个端口职责清楚）。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from .config import settings
from .minimax import _strip_think

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = settings()
        _client = httpx.AsyncClient(
            base_url=s.minimax_base_url,
            headers={
                "Authorization": f"Bearer {s.minimax_api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
            trust_env=False,  # 防 Clash 劫持
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _strip_in_response(data: dict[str, Any]) -> dict[str, Any]:
    """剥掉所有 choices[i].message.content 里的 <think>...</think>。"""
    choices = data.get("choices") or []
    for ch in choices:
        msg = (ch or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and "<think>" in content:
            msg["content"] = _strip_think(content)
    return data


def build_app() -> FastAPI:
    app = FastAPI(title="minimax-strip-think-shim")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/v1/chat/completions")
    async def chat_completions(req: Request) -> Any:
        body = await req.json()
        if body.get("stream"):
            # memU 不用 stream；显式拒绝避免静默错
            raise HTTPException(status_code=501, detail="streaming not supported by this shim")
        try:
            r = await _get_client().post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            log.error("upstream chat err: %s", e)
            raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e
        if r.status_code >= 400:
            log.warning("upstream %s: %s", r.status_code, r.text[:300])
            # 透传上游错误（含状态码 + body）
            raise HTTPException(status_code=r.status_code, detail=r.text)
        try:
            data = r.json()
        except Exception:
            return r.text
        return _strip_in_response(data)

    return app


async def serve_forever(host: str = "127.0.0.1", port: int = 18082) -> None:
    app = build_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
