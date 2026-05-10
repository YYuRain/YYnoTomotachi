"""turn 处理流水线。

顺序严格如下（记忆主动召回 + 扩展点 turn_context 贯穿）：
1. emotion.detect（MVP 占位，None）
2. memory.recall → 即使 agent 最终没用上也算"想起来了"
3. interests: 抽话题 + bump 热度
4. prompts.build_system_prompt（persona + memories + interests + emotion）
5. minimax.chat → 文本
6. rhythm.deliver → 通过回调分条发出
7. 异步：memory.note_turn + maybe_flush、availability.record
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from pathlib import Path

from . import availability, clock, emotion, interests, llm, memory, prompts, stickers, tools
from .audit_log import audit
from .persona import load_persona_state
from .rhythm import deliver

SendSticker = Callable[[Path], Awaitable[None]]

log = logging.getLogger(__name__)

# 简单的短期对话记忆：最近 N 轮（非持久化；memU 管长期）
_SHORT_WINDOW = 12
_recent: list[dict[str, str]] = []


def _trim() -> None:
    if len(_recent) > _SHORT_WINDOW * 2:
        del _recent[: len(_recent) - _SHORT_WINDOW * 2]


async def _build_turn(
    user_text: str,
    image_b64: str | None = None,
    image_media_type: str | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    ctx: dict[str, Any] = {}

    # 用于召回 / 话题抽取 / 工具的"可搜索文本"——纯图片时给一个占位，避免空字符串挂掉这些 task
    text_for_aux = user_text or "[对方发来一张图]"

    # 1-4 并行：情绪判档 / 记忆召回 / 话题抽取 / 工具查询
    recent_snapshot = list(_recent[-_SHORT_WINDOW * 2 :])
    emotion_task = asyncio.create_task(emotion.detect(text_for_aux, recent=recent_snapshot))
    memories_task = asyncio.create_task(_safe_recall(text_for_aux))
    topics_task = asyncio.create_task(interests.extract_topics(text_for_aux))
    # URL 读取优先（确定性）：有链接就直接读，跳过 LLM 工具判断
    if user_text and tools._URL_RE.search(user_text):
        tool_task = asyncio.create_task(tools.fetch_urls_in_message(user_text))
    elif user_text:
        tool_task = asyncio.create_task(_maybe_fetch_context(user_text))
    else:
        # 纯图片消息没必要查工具
        async def _empty() -> str:
            return ""
        tool_task = asyncio.create_task(_empty())

    ctx["emotion"], ctx["memories"], topics, ctx["tool_ctx"] = await asyncio.gather(
        emotion_task, memories_task, topics_task, tool_task
    )
    if topics:
        interests.bump(topics, delta=1.0)
    ctx["topics"] = topics

    # 5. 拼 system prompt（不含 tool_context，tool_context 直接贴用户消息前）
    persona = load_persona_state()
    sys_prompt = prompts.build_system_prompt(
        persona=persona,
        memories=ctx["memories"],
        interests_top=interests.top(5),
        interests_cold=interests.cold(3),
        emotion=ctx["emotion"],
        sticker_tags=stickers.available_tags(),
    )

    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(_recent[-_SHORT_WINDOW * 2 :])

    # 时间感 + idle + tool_context 都贴在用户消息前面，LLM 一定能看到。
    # 不进 system 段：保住 prompt cache 命中率；MiniMax 链路上 user 前缀也比 system 末尾稳。
    bits = [f"现在 {clock.now_signal()}"]
    idle_sec = availability.seconds_since_last_interaction()
    if idle_sec != float("inf") and idle_sec > 30:
        bits.append(f"距上次聊 {clock.since_phrase(idle_sec)}")
    time_prefix = "[" + "｜".join(bits) + "]"

    tool_ctx = ctx.get("tool_ctx", "")
    text_parts = [time_prefix]
    if tool_ctx:
        text_parts.append(f"[链接内容]\n{tool_ctx}")
    if image_b64:
        # 标识对方是图为主——纯图无 caption 时给 LLM 一个明确的"她/他发了张图"提示
        text_parts.append(user_text or "（对方发了一张图，没附文字——你看一眼，自然回应）")
    else:
        text_parts.append(user_text)
    text_block = "\n\n".join(text_parts)

    if image_b64:
        # Anthropic 风格的 multimodal content blocks：image 在前、text 在后（让 AI 先看图再读语境）
        user_content: Any = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type or "image/jpeg",
                    "data": image_b64,
                },
            },
            {"type": "text", "text": text_block},
        ]
    else:
        user_content = text_block

    messages.append({"role": "user", "content": user_content})
    return messages, ctx


async def _safe_recall(user_text: str) -> list[str]:
    try:
        return await memory.recall(user_text)
    except Exception as e:
        log.debug("recall error ignored: %s", e)
        return []


_TOOL_DETECT_SYSTEM = """你是工具调用判断助手。判断用户这条消息是否需要查询实时信息才能更好地回复。

需要查询的情况：用户想了解某平台上的内容、问某件具体事实、提到了某个网址、想知道最近流行什么。
不需要查询的情况：闲聊、情绪倾诉、回忆往事、问观点/建议、日常打招呼。

输出严格 JSON（无其他文字）：
{"needed": true/false, "tool": "xhs_search|web_search|read_url", "query": "搜索词或URL"}

needed 为 false 时 tool 和 query 可省略。宁可少搜也不要滥搜。"""

_TOOL_FUNCS = {
    "xhs_search": tools.search_xhs,
    "web_search": tools.search_web,
    "read_url": tools.read_url,
}


async def _maybe_fetch_context(user_text: str) -> str:
    """判断是否需要搜索，如需要则执行并返回结果字符串，否则返回空字符串。"""
    try:
        decision = await llm.chat_json(
            [
                {"role": "system", "content": _TOOL_DETECT_SYSTEM},
                {"role": "user", "content": user_text},
            ],
            temperature=0.1,
            max_tokens=80,
            tier="aux",
        )
        if not decision or not decision.get("needed"):
            return ""
        tool_name = decision.get("tool", "")
        query = decision.get("query", "").strip()
        func = _TOOL_FUNCS.get(tool_name)
        if not func or not query:
            return ""
        log.info("tool call: %s(%r)", tool_name, query[:60])
        result = await func(query)
        if result:
            log.info("tool result: %d chars", len(result))
        audit("tool_call", tool=tool_name, query=query[:200],
              result_chars=len(result or ""), result_preview=(result or "")[:300])
        return result
    except Exception as e:
        log.debug("tool detect/exec error: %s", e)
        return ""


async def handle_user_message(
    user_text: str,
    send: Callable[[str], Awaitable[None]],
    typing_action: Callable[[], Awaitable[None]] | None = None,
    *,
    image_b64: str | None = None,
    image_media_type: str | None = None,
    send_sticker: SendSticker | None = None,
) -> None:
    user_text = (user_text or "").strip()
    if not user_text and not image_b64:
        return

    audit("user_msg", text=user_text, has_image=bool(image_b64),
          image_media_type=image_media_type, recent_len=len(_recent))

    # 只有当前 provider 真不支持 vision 时才降级——避免触发上游 400 走 except 兜底说
    # "脑子卡了一下"那种困惑感。
    # 已支持：anthropic（原生 vision）/ openrouter（取决于具体 model 是不是 vision-capable，
    # kimi-k2.6、claude-sonnet-4.6、gemini-3.x-pro 等都行）。
    # 已知不支持：minimax（M2.7 当前 token plan 没真 vision-capable 模型）。
    from .config import settings as _settings
    if image_b64 and _settings().llm_provider == "minimax":
        log.info("image dropped: provider=minimax 当前模型不支持 vision")
        msg = (f"图我现在看不见（图模型挂了）\n你说的「{user_text[:30]}」我倒是能聊"
               if user_text else "图我看不见呢\n描述一下？")
        audit("assistant_reply", text=msg, mode="degrade", reason="provider=minimax no vision")
        await send(msg)
        return

    import time as _time
    _t0 = _time.time()
    messages, ctx = await _build_turn(user_text, image_b64=image_b64, image_media_type=image_media_type)

    # 按模式调整生成参数
    em = ctx.get("emotion")
    mode = getattr(em, "mode", "casual")
    if mode == "depth":
        # depth 不再"长 + 稳"——靠近 casual 的语气，让想法碰撞而不是教学
        temperature, max_tokens = 0.85, 600
    elif mode == "empathy":
        temperature, max_tokens = 0.6, 400
    elif mode == "interest":
        # 跟着兴头走：稍高温度更活，字数不拉长
        temperature, max_tokens = 0.95, 500
    else:
        temperature, max_tokens = 0.9, 500

    s = _settings()
    if s.llm_provider == "openrouter":
        active_model = s.openrouter_model
    elif s.llm_provider == "anthropic":
        active_model = s.anthropic_model
    else:
        active_model = s.minimax_chat_model
    try:
        reply = await llm.chat(messages, temperature=temperature, max_tokens=max_tokens)
    except Exception as e:
        log.exception("chat failed: %s", e)
        audit("assistant_reply", text="(脑子卡了一下)", mode=mode, error=str(e)[:200],
              provider=s.llm_provider, model=active_model)
        await send("（脑子卡了一下）")
        return

    reply = reply.strip()
    if not reply:
        audit("assistant_reply", text="", mode=mode, error="empty reply",
              provider=s.llm_provider, model=active_model)
        return

    audit("assistant_reply", text=reply, mode=mode,
          temperature=temperature, max_tokens=max_tokens,
          provider=s.llm_provider, model=active_model,
          latency_ms=int((_time.time() - _t0) * 1000),
          memories_used=len(ctx.get("memories") or []),
          topics=ctx.get("topics") or [],
          tool_ctx_chars=len(ctx.get("tool_ctx") or ""),
          history_len=len(_recent))

    # 更新短期对话——_recent 不存 base64（避免膨胀），纯图给一个文本占位让历史可读
    history_user_text = user_text if user_text else ("[图片]" if image_b64 else "")
    if image_b64 and user_text:
        history_user_text = f"[图片] {user_text}"
    _recent.append({"role": "user", "content": history_user_text})
    _recent.append({"role": "assistant", "content": reply})
    _trim()

    # 节奏化发送：
    # - max_piece_chars：硬上限（超了才切）
    # - merge_up_to：合并阈值（相邻短句只有合并后仍 ≤ 此值才合）
    # depth 不再贪心合并——和 casual 节奏对齐，避免"认真聊就突然一段成段"的端起来感；
    # 单条上限略高（80 vs 60），允许一句完整观点不被硬切，但不再合多句
    if mode == "depth":
        piece_limit, merge_limit = 80, 14
    elif mode == "empathy":
        piece_limit, merge_limit = 40, 12
    elif mode == "interest":
        piece_limit, merge_limit = 55, 10
    else:
        piece_limit, merge_limit = 60, 12

    # 先 parse 出 [sticker:xxx] 标记 → 切成 text/sticker 段，按顺序发
    segments = stickers.parse_message(reply) if send_sticker else [("text", reply)]
    if not segments:
        # 整条都是无效 sticker 标记 → 退化全发文本
        segments = [("text", reply)]
    for kind, payload in segments:
        if kind == "text" and payload:
            await deliver(
                payload, send, typing_action,
                max_piece_chars=piece_limit, merge_up_to=merge_limit,
            )
        elif kind == "sticker" and send_sticker is not None:
            try:
                await send_sticker(payload)  # type: ignore[arg-type]
            except Exception as e:
                log.exception("send_sticker failed: %s", e)

    # 后台任务：长期记忆 + 活跃时段
    asyncio.create_task(_post_turn(history_user_text, reply))


async def _post_turn(user_text: str, reply: str) -> None:
    try:
        memory.note_turn(user_text, reply)
        await memory.maybe_flush()
    except Exception as e:
        log.debug("post_turn memory err: %s", e)
    try:
        availability.record()
    except Exception as e:
        log.debug("post_turn availability err: %s", e)


async def generate_opener(context: dict | None = None) -> str:
    """scheduler 主动发起时用这个。
    context 由 proactive.decide 提供：user_probably_doing / opener_angle / recent_topics。
    没 context 就退回旧的通用指令。"""
    persona = load_persona_state()
    top = interests.top(5)
    cold_ = interests.cold(3)
    sys_prompt = prompts.build_system_prompt(
        persona=persona,
        memories=[],  # 主动开场不主推记忆，避免显得在翻旧账
        interests_top=top,
        interests_cold=cold_,
    )
    hint = prompts.render_proactive_opener(context) if context else prompts.PROACTIVE_OPENER_INSTRUCTIONS
    bits = [f"现在 {clock.now_signal()}"]
    idle_sec = availability.seconds_since_last_interaction()
    if idle_sec != float("inf") and idle_sec > 30:
        bits.append(f"距上次聊 {clock.since_phrase(idle_sec)}")
    hint = "[" + "｜".join(bits) + "]\n\n" + hint
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": hint},
    ]
    text = await llm.chat(messages, temperature=1.0, max_tokens=200)
    text = text.strip()
    audit("proactive_opener_generated", text=text, context=context or {},
          idle_sec=int(idle_sec) if idle_sec != float("inf") else -1)
    return text
