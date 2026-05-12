"""本地 OpenAI 兼容 chat-completion shim，**剥 <think> 块**后转发。

用途：memU 内部抽取/总结调 LLM 时，会把 raw response 写库。
     MiniMax-M2 输出带 `<think>...</think>`，memU 内置 OpenAI 客户端不知道剥，
     于是 `memory_categories.summary` 字段全被 think 内容污染（admin UI 直接可见）。

上游路由（由 settings 决定，启动时一次性绑定）：
- 设了 `MEMU_CHAT_MODEL` → OpenRouter（base=`OPENROUTER_BASE_URL`，key=`OPENROUTER_API_KEY`，
  走 Clash 代理 `TELEGRAM_PROXY`；deepseek-v4-flash 等无 think 模型 strip 是 no-op，零代价）
- 否则 → MiniMax 直连（不走代理；strip <think> 防 memory_categories.summary 污染）

shim 永远剥 `<think>`：对没 think 的模型是 no-op；让 memory.py 的 default profile
不必关心上游是谁，统一指向 :18082 即可（且统一吃 _purge_proxy_env 后的环境）。

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
        if s.memu_chat_model and s.openrouter_api_key:
            # OpenRouter 路径：走 Clash（_purge_proxy_env 清掉了 env，必须显式传 proxy）
            kwargs: dict[str, Any] = {
                "base_url": s.openrouter_base_url,
                "headers": {
                    "Authorization": f"Bearer {s.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/YYuRain/YYnoTomotachi",
                    "X-Title": "AIDemo memU",
                },
                "timeout": 120.0,
                "trust_env": False,
            }
            if s.telegram_proxy:
                kwargs["proxy"] = s.telegram_proxy
            _client = httpx.AsyncClient(**kwargs)
            log.info("memU shim upstream: OpenRouter (model from request, proxy=%s)",
                     s.telegram_proxy or "<none>")
        else:
            # MiniMax 老路径：直连，不走代理
            _client = httpx.AsyncClient(
                base_url=s.minimax_base_url,
                headers={
                    "Authorization": f"Bearer {s.minimax_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=120.0,
                trust_env=False,  # 防 Clash 劫持
            )
            log.info("memU shim upstream: MiniMax (%s)", s.minimax_base_url)
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
