"""MiniMax API 封装。优先走 OpenAI 兼容端点。

兼容端点参考：POST {base}/chat/completions（OpenAI 格式）
          POST {base}/embeddings（OpenAI 格式）
若日后发现 key 只能走原生接口，再在这里加 fallback。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

import httpx

from .config import settings

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)


def _strip_think(text: str) -> str:
    """剥掉 MiniMax-M2 之类 reasoning 模型的 <think>...</think> 块。
    若被截断、只剩未闭合的 <think> 起始标签，整段丢掉。"""
    t = _THINK_RE.sub("", text)
    t = _THINK_OPEN_RE.sub("", t)
    return t.strip()

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _client_singleton() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = settings()
        # 显式关掉系统代理：MiniMax 是国内服务，不应该走 Clash 之类的代理
        _client = httpx.AsyncClient(
            base_url=s.minimax_base_url,
            headers={
                "Authorization": f"Bearer {s.minimax_api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
            trust_env=False,
        )
    return _client


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """把 anthropic 风格的 multimodal content（list of blocks，type=image/text）
    转成 OpenAI/MiniMax 兼容端点期望的 list（type=image_url/text）。

    str content 原样保留——大多数文本调用都是 str，不走这条路径。
    """
    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        new_content: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                new_content.append({"type": "text", "text": block.get("text", "")})
            elif btype == "image":
                src = block.get("source") or {}
                if src.get("type") == "base64":
                    media = src.get("media_type", "image/jpeg")
                    data = src.get("data", "")
                    new_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{media};base64,{data}"},
                    })
                # 其他 source 类型（url 等）暂不支持，静默丢
            elif btype == "image_url":
                # 已经是 OpenAI 风格，原样
                new_content.append(block)
        out.append({**m, "content": new_content})
    return out


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.8,
    max_tokens: int = 1024,
    response_format: dict | None = None,
    model: str | None = None,
) -> str:
    """返回 assistant 文本（已剥掉 <think>）。response_format={'type':'json_object'} 可强制 JSON。
    支持 multimodal：messages 里 content 可以是 list of blocks（anthropic 风格 image+text），
    会被自动转成 MiniMax 兼容端点期望的 image_url 格式。"""
    s = settings()
    payload: dict[str, Any] = {
        "model": model or s.minimax_chat_model,
        "messages": _normalize_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    r = await _client_singleton().post("/chat/completions", json=payload)
    if r.status_code >= 400:
        log.error("minimax chat %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"minimax 返回格式异常：{data}") from e
    return _strip_think(content)


async def chat_json(messages: list[dict[str, str]], **kw) -> Any:
    """让模型输出 JSON 并解析。失败返回空 dict。
    注意：MiniMax-M2 会先吐 <think>，token 要给够，否则 JSON 被截断。"""
    kw.setdefault("response_format", {"type": "json_object"})
    kw.setdefault("temperature", 0.2)
    kw.setdefault("max_tokens", 2048)
    raw = await chat(messages, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 有些模型 json_object 支持不好，尝试抠出第一段 {..}
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        log.warning("chat_json 解析失败：%s", raw[:200])
        return {}


async def embed(
    texts: Iterable[str], *, model: str | None = None, etype: str = "db"
) -> list[list[float]]:
    """MiniMax 的 embedding 接口是 **非 OpenAI 风格**：
    - 请求体用 `texts`，不是 `input`；还要 `type` = "db"（存库）或 "query"（查询）。
    - 返回体是 `{"vectors": [[...], ...], "base_resp": {...}}`。
    这里统一适配成 list[list[float]]。
    """
    s = settings()
    payload = {
        "model": model or s.minimax_embed_model,
        "texts": list(texts),
        "type": etype,
    }
    r = await _client_singleton().post("/embeddings", json=payload)
    if r.status_code >= 400:
        log.error("minimax embed %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
    data = r.json()
    base = data.get("base_resp") or {}
    if base.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax embed 失败：{base}")
    vectors = data.get("vectors")
    if not vectors:
        raise RuntimeError(f"MiniMax embed 无 vectors：{data}")
    return vectors


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
