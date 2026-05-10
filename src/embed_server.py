"""本地 OpenAI 兼容 embedding server。

用途：memU 要求 OpenAI 格式的 embedding HTTP endpoint，
     但 MiniMax embedding 非 OpenAI 兼容且可能欠费。
     这里用 sentence-transformers 跑本地模型，离线免费。

端点：POST /v1/embeddings
     请求体：{"model": "...", "input": "str" 或 ["str", ...]}
     返回：  {"data":[{"embedding":[...], "index":0, "object":"embedding"}, ...],
              "model":"...", "object":"list", "usage":{...}}

启动：作为 asyncio task 与 bot 一起跑。端口在 config.embed_server_port。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"   # 中文优化，~95MB，512 维

_model = None
_model_name: str = DEFAULT_MODEL


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # type: ignore
        log.info("loading embedding model %s (首次会下载 ~100MB)", _model_name)
        _model = SentenceTransformer(_model_name)
        log.info("embedding model ready, dim=%d", _model.get_sentence_embedding_dimension())
    return _model


class EmbedRequest(BaseModel):
    model: str | None = None
    input: str | list[str]


def build_app(model_name: str = DEFAULT_MODEL) -> FastAPI:
    global _model_name
    _model_name = model_name
    app = FastAPI(title="local-embed-shim")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "model": _model_name}

    @app.post("/v1/embeddings")
    async def embeddings(req: EmbedRequest) -> dict[str, Any]:
        texts = [req.input] if isinstance(req.input, str) else list(req.input)
        model = _get_model()
        # 线程池里跑，别卡住 event loop
        loop = asyncio.get_event_loop()
        vecs = await loop.run_in_executor(
            None, lambda: model.encode(texts, normalize_embeddings=True).tolist()
        )
        return {
            "object": "list",
            "model": req.model or _model_name,
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vecs)
            ],
            "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)},
        }

    return app


async def serve_forever(host: str = "127.0.0.1", port: int = 18080, model: str = DEFAULT_MODEL) -> None:
    """在 asyncio loop 里跑 uvicorn。"""
    app = build_app(model)
    # 预热一次：启动阶段就加载模型，避免首次请求冷启动超时
    _get_model()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
