"""OpenRouter 客户端（OpenAI 兼容端点）。

仅供 `scripts/eval_models.py` 使用，bot 主链路不通过这里。

设计：
- 失败**不抛**——返回 `{"text": "", "error": "..."}` 让评测脚本继续跑其他模型
- 测延迟（latency_ms）以便横向对比响应速度
- `trust_env=False` 防 Clash 劫持（参考 src/minimax.py）
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is not None:
        return _client
    s = settings()
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY 未配置（.env）")
    # 走 Clash 代理：OpenAI / Anthropic 等海外模型在中国区会被 OpenRouter 按 IP 拦
    # （403 "not available in your region"）。trust_env=False 保留——只在这里显式指定 proxy，
    # 不要让其他客户端（minimax / 内网 anthropic gateway）受影响。
    kw: dict[str, Any] = {
        "base_url": s.openrouter_base_url,
        "headers": {
            "Authorization": f"Bearer {s.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/yangyu/AIDemo",
            "X-Title": "AIDemo Companion Agent eval",
        },
        "timeout": 120.0,
        "trust_env": False,
    }
    if s.telegram_proxy:
        kw["proxy"] = s.telegram_proxy
    _client = httpx.AsyncClient(**kw)
    return _client


# reasoning model（gpt-5/o1/kimi-k2.x/...）在出 content 前先用一大块 token 做内心独白；
# max_tokens 给小了会触发 finish_reason=length、content 是空。OpenRouter 上很多模型是
# reasoning model 而我们外部不知道——统一兜底最低 budget，非 reasoning 模型不会用满，浪费可控。
_MIN_MAX_TOKENS = 4096


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """anthropic 风格 multimodal content（type=image, source.base64）→ OpenAI 风格
    （type=image_url, image_url.url=data:...;base64,...）。

    bot 主链路 agent._build_turn 发的是 anthropic 风格（兼容 SDK 直接调用）；
    走 OpenAI 兼容端点（OpenRouter / MiniMax）必须转换，否则模型看不到图。
    str content 原样保留——纯文本不走这条路径。
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
            elif btype == "image_url":
                new_content.append(block)
        out.append({**m, "content": new_content})
    return out


async def chat(
    messages: list[dict],
    model: str,
    *,
    temperature: float = 0.85,
    max_tokens: int = 600,
    tools: list[dict] | None = None,
    tool_choice: str | dict = "auto",
) -> dict[str, Any]:
    """
    返回：
      成功 → {"text": "...", "tool_calls": [...], "finish_reason": "...",
              "latency_ms": float, "model": "...", "tokens": int|None}
      失败 → {"text": "", "tool_calls": [], "error": "...", ...}

    tools / tool_choice：OpenAI 兼容 native tool calling。
    - tools = None：不传，行为同旧版
    - tools = [...] + tool_choice="auto"：让模型自己决定
    - tool_choice="none"：禁止调工具（用于二次循环时）
    """
    cli = _get_client()
    effective_max = max(max_tokens, _MIN_MAX_TOKENS)
    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(messages),
        "temperature": temperature,
        "max_tokens": effective_max,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    t0 = time.time()
    try:
        r = await cli.post("/chat/completions", json=payload)
    except Exception as e:
        return {"text": "", "tool_calls": [], "error": f"http err: {e}",
                "latency_ms": (time.time() - t0) * 1000, "model": model}

    latency_ms = (time.time() - t0) * 1000
    if r.status_code >= 400:
        body = r.text[:300]
        log.warning("openrouter %s %d: %s", model, r.status_code, body)
        return {"text": "", "tool_calls": [], "error": f"{r.status_code}: {body}",
                "latency_ms": latency_ms, "model": model}

    try:
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        finish = (data.get("choices") or [{}])[0].get("finish_reason", "?")
        usage = data.get("usage") or {}
        tokens = usage.get("completion_tokens")
    except Exception as e:
        return {"text": "", "tool_calls": [], "error": f"parse err: {e}; body={r.text[:200]}",
                "latency_ms": latency_ms, "model": model}

    # 200 但 content 和 tool_calls 都空：refuse / safety filter / 没产出 content blocks
    if not content.strip() and not tool_calls:
        return {
            "text": "",
            "tool_calls": [],
            "error": f"empty content (finish_reason={finish}, completion_tokens={tokens})",
            "latency_ms": latency_ms,
            "model": model,
            "tokens": tokens,
        }

    return {
        "text": content,
        "tool_calls": tool_calls,
        "finish_reason": finish,
        "latency_ms": latency_ms,
        "model": model,
        "tokens": tokens,
    }


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
