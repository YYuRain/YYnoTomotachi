"""主聊天 LLM 的统一门面。

- `chat(messages, ...)` / `chat_json(messages, ...)`
- 按 `settings().llm_provider` 分发：
  - `anthropic`：原生 Anthropic SDK，system prompt 走 `system=`，**启用 prompt caching**
    （system 段比较稳定，能显著省钱/减少首 token 延迟）。
  - `minimax`：沿用 `src.minimax`（保留 MiniMax-M2 的 <think> 剥离逻辑）。

设计约定：
- 统一的 `messages` 形状是 OpenAI 风格 `[{role: "system"/"user"/"assistant", content: "..."}]`；
  Anthropic 分支内部把 system 抠出来传 `system=`，非 system 保留在 `messages=`。
- `chat_json` 不依赖 API 侧的 json_object 模式（Anthropic 没这选项，MiniMax 也经常吐 think）
  统一做法：在系统提示里明确要求 JSON，然后宽松抠 `{...}`。

memU 内部的 LLM 调用与这里无关——那条链路仍然直接指向 MiniMax（在 `memory.py` 配置）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import settings
from . import minimax, openrouter

log = logging.getLogger(__name__)

_anth = None  # anthropic.AsyncAnthropic 单例


def _get_anth():
    global _anth
    if _anth is not None:
        return _anth
    import anthropic  # type: ignore  # noqa: PLC0415
    import httpx  # noqa: PLC0415
    s = settings()
    # 显式 trust_env=False，避免 macOS scutil/Clash 把请求劫持
    http_client = httpx.AsyncClient(trust_env=False, timeout=120.0)
    kw: dict[str, Any] = {"api_key": s.anthropic_api_key, "http_client": http_client}
    if s.anthropic_base_url:
        kw["base_url"] = s.anthropic_base_url
    _anth = anthropic.AsyncAnthropic(**kw)
    return _anth


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, list):
                system_parts.append("".join(x.get("text", "") for x in c if isinstance(x, dict)))
            else:
                system_parts.append(str(c or ""))
        else:
            rest.append({"role": m["role"], "content": m["content"]})
    return "\n\n".join(p for p in system_parts if p), rest


def _coalesce_messages(msgs: list[dict]) -> list[dict]:
    """Anthropic 要求 user/assistant 交替，且第一条必须是 user。
    把相邻同 role 合并，必要时丢开头的 assistant。
    保留 multimodal content（content 是 list of blocks 时不能 str() 拍扁）。"""
    out: list[dict] = []
    for m in msgs:
        role = m["role"]
        if role not in ("user", "assistant"):
            continue
        if not out and role == "assistant":
            continue  # 不能以 assistant 开头
        cur = m["content"]
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            if isinstance(prev, str) and isinstance(cur, str):
                out[-1]["content"] = prev + "\n" + cur
            else:
                # 至少一边是 list of blocks → 统一转 list 再 concat
                blocks: list[Any] = []
                if isinstance(prev, str):
                    if prev:
                        blocks.append({"type": "text", "text": prev})
                else:
                    blocks.extend(prev)
                if isinstance(cur, str):
                    if cur:
                        blocks.append({"type": "text", "text": cur})
                else:
                    blocks.extend(cur)
                out[-1]["content"] = blocks
        else:
            out.append({"role": role, "content": cur})
    return out


async def _anthropic_chat(
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    model: str | None,
    tier: str = "main",
    cache_system: bool = True,
) -> str:
    s = settings()
    if model is None:
        if tier == "reflection" and s.anthropic_model_reflection:
            model = s.anthropic_model_reflection
        elif tier == "aux" and s.anthropic_model_aux:
            model = s.anthropic_model_aux
        else:
            model = s.anthropic_model
    system_text, rest = _split_system(messages)
    rest = _coalesce_messages(rest)
    if not rest:
        rest = [{"role": "user", "content": "（空）"}]

    system_param: Any
    if system_text and cache_system and len(system_text) >= 200:
        # prompt caching：system 段做缓存（≥1024 tokens 才真缓存，但打标签不报错）
        system_param = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system_param = system_text or ""

    cli = _get_anth()
    try:
        resp = await cli.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=min(1.0, max(0.0, temperature)),
            system=system_param,
            messages=rest,
        )
    except Exception as e:
        log.error("anthropic chat failed: %s", e)
        raise

    # 聚合 content blocks
    out_parts: list[str] = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            out_parts.append(getattr(block, "text", ""))
    return "".join(out_parts).strip()


async def _openrouter_chat(
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    model: str | None,
    tier: str = "main",
) -> str:
    s = settings()
    if model is None:
        if tier == "reflection" and s.openrouter_model_reflection:
            model = s.openrouter_model_reflection
        elif tier == "aux" and s.openrouter_model_aux:
            model = s.openrouter_model_aux
        else:
            model = s.openrouter_model
    if not model:
        raise RuntimeError("OPENROUTER_MODEL 未配置（.env）")
    res = await openrouter.chat(messages, model, temperature=temperature, max_tokens=max_tokens)
    if res.get("error"):
        raise RuntimeError(f"openrouter ({model}): {res['error']}")
    return res["text"]


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.8,
    max_tokens: int = 1024,
    response_format: dict | None = None,
    model: str | None = None,
    tier: str = "main",   # "main"=给用户看的输出；"aux"=情绪/话题等辅助判断
) -> str:
    s = settings()
    if s.llm_provider == "anthropic":
        return await _anthropic_chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            tier=tier,
        )
    if s.llm_provider == "openrouter":
        return await _openrouter_chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            tier=tier,
        )
    return await minimax.chat(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        model=model,
    )


async def chat_with_tools(
    messages: list[dict],
    *,
    tools: list[dict],
    tool_choice: str | dict = "auto",
    temperature: float = 0.85,
    max_tokens: int = 1024,
    model: str | None = None,
    tier: str = "main",
) -> dict[str, Any]:
    """主 LLM 调用 + native tool_use 支持（Anthropic / OpenRouter）。

    返回统一 dict：{"text": str, "tool_calls": list[dict], "finish_reason": str}
    tool_calls 元素是 OpenAI 格式：
        {"id": "call_xxx", "type": "function",
         "function": {"name": "...", "arguments": '{"query": "..."}'}}

    minimax 不支持 native tool_use → fallback 等价 chat()，tool_calls 永远空。
    """
    s = settings()
    if s.llm_provider == "openrouter":
        m = model
        if m is None:
            if tier == "reflection" and s.openrouter_model_reflection:
                m = s.openrouter_model_reflection
            elif tier == "aux" and s.openrouter_model_aux:
                m = s.openrouter_model_aux
            else:
                m = s.openrouter_model
        if not m:
            raise RuntimeError("OPENROUTER_MODEL 未配置")
        res = await openrouter.chat(
            messages, m,
            temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice,
        )
        if res.get("error") and not res.get("tool_calls"):
            raise RuntimeError(f"openrouter ({m}): {res['error']}")
        return {
            "text": res.get("text", ""),
            "tool_calls": res.get("tool_calls") or [],
            "finish_reason": res.get("finish_reason", ""),
        }
    if s.llm_provider == "anthropic":
        return await _anthropic_chat_with_tools(
            messages, tools=tools, tool_choice=tool_choice,
            temperature=temperature, max_tokens=max_tokens, model=model, tier=tier,
        )
    # minimax fallback：忽略 tools，等价无工具 chat
    txt = await minimax.chat(
        messages, temperature=temperature, max_tokens=max_tokens, model=model,
    )
    return {"text": txt, "tool_calls": [], "finish_reason": "stop"}


async def _anthropic_chat_with_tools(
    messages: list[dict],
    *,
    tools: list[dict],
    tool_choice: str | dict,
    temperature: float,
    max_tokens: int,
    model: str | None,
    tier: str,
) -> dict[str, Any]:
    """Anthropic SDK 原生 tool_use。把 OpenAI 格式 tools 转成 Anthropic 格式。"""
    s = settings()
    if model is None:
        if tier == "reflection" and s.anthropic_model_reflection:
            model = s.anthropic_model_reflection
        elif tier == "aux" and s.anthropic_model_aux:
            model = s.anthropic_model_aux
        else:
            model = s.anthropic_model
    system_text, rest = _split_system(messages)
    # Anthropic messages 不接受 role="tool"，要把它转成 user(role) + tool_result block
    rest = _convert_messages_for_anthropic(rest)
    if not rest:
        rest = [{"role": "user", "content": "（空）"}]

    # OpenAI tool schema → Anthropic
    anth_tools = [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in tools
    ]
    anth_tool_choice: dict
    if tool_choice == "auto":
        anth_tool_choice = {"type": "auto"}
    elif tool_choice == "none":
        anth_tool_choice = {"type": "none"}  # SDK 0.x 用 "any"=必调；none = 不许调
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        anth_tool_choice = {"type": "tool", "name": tool_choice["function"]["name"]}
    else:
        anth_tool_choice = {"type": "auto"}

    cli = _get_anth()
    try:
        resp = await cli.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=min(1.0, max(0.0, temperature)),
            system=system_text or "",
            messages=rest,
            tools=anth_tools,
            tool_choice=anth_tool_choice,
        )
    except Exception as e:
        log.error("anthropic chat_with_tools failed: %s", e)
        raise

    # 聚合 content blocks: text + tool_use → 转 OpenAI 格式 tool_calls
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            import json as _json
            tool_calls.append({
                "id": getattr(block, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(block, "name", ""),
                    "arguments": _json.dumps(getattr(block, "input", {}), ensure_ascii=False),
                },
            })
    return {
        "text": "".join(text_parts).strip(),
        "tool_calls": tool_calls,
        "finish_reason": getattr(resp, "stop_reason", ""),
    }


def _convert_messages_for_anthropic(rest: list[dict]) -> list[dict]:
    """把 OpenAI 风格 messages 转 Anthropic 风格——主要是 role='tool' / assistant tool_calls。"""
    out: list[dict] = []
    import json as _json
    for m in rest:
        role = m.get("role")
        if role == "tool":
            # OpenAI: {"role":"tool", "tool_call_id":"...", "content":"result"}
            # Anthropic: {"role":"user", "content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]}
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": m.get("content") or "",
                }],
            })
        elif role == "assistant" and m.get("tool_calls"):
            # OpenAI: {"role":"assistant","content":text or null,"tool_calls":[{"id","function":{"name","arguments"}}]}
            # Anthropic: {"role":"assistant","content":[{"type":"text",...},{"type":"tool_use","id","name","input"}]}
            blocks: list[dict] = []
            text = m.get("content")
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "input": args,
                })
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append(m)
    return out


_JSON_HINT = (
    "\n\n【输出格式】只输出一个合法 JSON 对象，不要任何前后说明、注释、代码块围栏。"
)


async def chat_json(messages: list[dict], **kw) -> Any:
    """让模型输出 JSON 并解析。失败返回空 dict。
    跨 provider 用一致策略：在 system 里追加 JSON 要求，然后宽松抠 `{...}`。
    默认 tier="aux"（辅助任务，便宜 model）。"""
    s = settings()
    kw.setdefault("tier", "aux")
    if s.llm_provider in ("anthropic", "openrouter"):
        msgs2 = list(messages)
        if msgs2 and msgs2[0].get("role") == "system":
            msgs2[0] = {"role": "system", "content": msgs2[0]["content"] + _JSON_HINT}
        else:
            msgs2.insert(0, {"role": "system", "content": _JSON_HINT.strip()})
        kw.setdefault("temperature", 0.2)
        # OpenRouter 上的 reasoning model（gpt-5/o1/kimi/...）会先用 token 做 reasoning，
        # 给小了 finish_reason=length、content 为空。两个 provider 用同一份大默认值即可。
        kw.setdefault("max_tokens", 4096)
        raw = await chat(msgs2, **kw)
    else:
        # MiniMax 不分 main/aux tier；剥掉 tier 再透传
        kw.pop("tier", None)
        return await minimax.chat_json(messages, **kw)

    # 宽松解析
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    log.warning("chat_json(anthropic) 解析失败：%s", raw[:200])
    return {}


async def aclose() -> None:
    await minimax.aclose()
    await openrouter.aclose()
    global _anth
    if _anth is not None:
        try:
            await _anth.close()
        except Exception:
            pass
        _anth = None
