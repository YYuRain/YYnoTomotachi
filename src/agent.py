"""turn 处理流水线（多用户版）。

顺序严格如下（记忆主动召回 + 扩展点 turn_context 贯穿）：
1. emotion.detect（按 user_id 拉短期上下文）
2. memory.recall(user_id) → 即使 agent 最终没用上也算"想起来了"
3. interests.extract_topics → bump(user_id, ...)
4. prompts.build_system_prompt（persona + memories + interests + emotion）
5. llm.chat → 文本
6. rhythm.deliver → 通过回调分条发出
7. 异步：memory.note_turn(user_id) + maybe_flush(user_id)、availability.record(user_id)

短期对话 `_recent` 是 dict[str_uid, list]——每用户一份；持久化到 `data/recent.json`：
  { "<uid>": [...], "<uid>": [...] }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from pathlib import Path

from . import availability, clock, emotion, interests, llm, memory, prompts, stickers, tools
from .audit_log import audit
from .config import settings
from .persona import load_persona_state
from .rhythm import deliver

SendSticker = Callable[[Path], Awaitable[None]]

log = logging.getLogger(__name__)

_SHORT_WINDOW = 12
_recent_per_user: dict[str, list[dict[str, str]]] = {}
_recent_loaded = False


def _uid(chat_id: int | str) -> str:
    return str(chat_id)


def _recent_path() -> Path:
    return settings().root / "data" / "recent.json"


def _load_recent() -> None:
    """启动时调一次。文件缺失/损坏静默回到空 dict。

    兼容老格式：如果文件是 list（单用户旧版），全部塞到 admin 名下。
    """
    global _recent_loaded
    if _recent_loaded:
        return
    _recent_loaded = True
    p = _recent_path()
    if not p.exists():
        return
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            # 老格式：包成 admin 名下，由迁移脚本重写文件
            admin_uid = _uid(settings().admin_chat_id)
            _recent_per_user[admin_uid] = [
                m for m in data if isinstance(m, dict) and "role" in m and "content" in m
            ]
            log.info("loaded _recent (legacy list): %d msgs → uid=%s",
                     len(_recent_per_user[admin_uid]), admin_uid)
        elif isinstance(data, dict):
            for uid, msgs in data.items():
                if isinstance(msgs, list):
                    _recent_per_user[str(uid)] = [
                        m for m in msgs if isinstance(m, dict) and "role" in m and "content" in m
                    ]
            total = sum(len(v) for v in _recent_per_user.values())
            log.info("loaded _recent: %d users, %d msgs total",
                     len(_recent_per_user), total)
    except Exception as e:
        log.warning("load _recent failed (ignored): %s", e)


def record_proactive_message(user_id: int, text: str) -> None:
    """主动开场（proactive opener / welcome opener）发出后调用，把这条 assistant
    消息追加进 _recent，让下一轮 user 回复时 handle_user_message 拼上下文能看见。

    **不**进 memory.note_turn buffer——主动开场是单边消息（没 user 输入配对），
    塞进 buffer 会让 flush 时 LLM 抽 profile/event 看到不平衡的 batch。

    历史 bug：proactive 发"草莓音乐节有个新阵容"，user 回"哪个啊"，bot 回"啊？
    你问哪个哪个"——bot 完全不知道自己刚说过什么，因为 opener 没进 _recent。
    """
    if not text:
        return
    uid = str(user_id)
    recent = _recent_per_user.setdefault(uid, [])
    recent.append({"role": "assistant", "content": text})
    _trim(uid)
    _save_recent()


def _save_recent() -> None:
    """每轮 append 完调一次。失败静默——不阻塞主流程。"""
    try:
        p = _recent_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_recent_per_user, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        log.debug("save _recent failed (ignored): %s", e)


def _trim(uid: str) -> None:
    buf = _recent_per_user.get(uid)
    if buf and len(buf) > _SHORT_WINDOW * 2:
        del buf[: len(buf) - _SHORT_WINDOW * 2]


# 模块加载即读一次。bot 启动后第一次 import agent 就会触发。
_load_recent()


async def _build_turn(
    user_id: int,
    user_text: str,
    image_b64: str | None = None,
    image_media_type: str | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    ctx: dict[str, Any] = {}
    uid = _uid(user_id)
    recent = _recent_per_user.setdefault(uid, [])

    # 用于召回 / 话题抽取 / 工具的"可搜索文本"——纯图片时给一个占位，避免空字符串挂掉这些 task
    text_for_aux = user_text or "[对方发来一张图]"

    # 1-4 并行：情绪判档 / 记忆召回 / 话题抽取 / 工具查询
    recent_snapshot = list(recent[-_SHORT_WINDOW * 2 :])
    emotion_task = asyncio.create_task(emotion.detect(text_for_aux, recent=recent_snapshot))
    memories_task = asyncio.create_task(_safe_recall(user_id, text_for_aux))
    topics_task = asyncio.create_task(interests.extract_topics(text_for_aux))
    if user_text and tools._URL_RE.search(user_text):
        tool_task = asyncio.create_task(tools.fetch_urls_in_message(user_text))
    elif user_text:
        tool_task = asyncio.create_task(_maybe_fetch_context(user_text))
    else:
        async def _empty() -> str:
            return ""
        tool_task = asyncio.create_task(_empty())

    ctx["emotion"], ctx["memories"], topics, ctx["tool_ctx"] = await asyncio.gather(
        emotion_task, memories_task, topics_task, tool_task
    )
    if topics:
        interests.bump(user_id, topics, delta=1.0)
    ctx["topics"] = topics

    # 5. 拼 system prompt
    persona = load_persona_state(user_id)
    sys_prompt = prompts.build_system_prompt(
        persona=persona,
        memories=ctx["memories"],
        interests_top=interests.top(user_id, 5),
        interests_cold=interests.cold(user_id, 3),
        emotion=ctx["emotion"],
        sticker_tags=stickers.available_tags(),
        user_id=user_id,
    )

    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(recent[-_SHORT_WINDOW * 2 :])

    bits = [f"现在 {clock.now_signal()}"]
    idle_sec = availability.seconds_since_last_interaction(user_id)
    if idle_sec != float("inf") and idle_sec > 30:
        bits.append(f"距上次聊 {clock.since_phrase(idle_sec)}")
    time_prefix = "[" + "｜".join(bits) + "]"

    tool_ctx = ctx.get("tool_ctx", "")
    text_parts = [time_prefix]
    if tool_ctx:
        text_parts.append(f"[链接内容]\n{tool_ctx}")
    if image_b64:
        text_parts.append(user_text or "（对方发了一张图，没附文字——你看一眼，自然回应）")
    else:
        text_parts.append(user_text)
    text_block = "\n\n".join(text_parts)

    if image_b64:
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


async def _safe_recall(user_id: int, user_text: str) -> list[str]:
    try:
        return await memory.recall(user_id, user_text)
    except Exception as e:
        log.debug("recall error ignored: %s", e)
        return []


_TOOL_DETECT_SYSTEM_TEMPLATE = """你是工具调用判断助手。今天是 {today}（{weekday}）。
判断用户这条消息是否需要查询实时信息才能更好地回复。

需要查询的情况（必须 needed=true）：
- 问某人最近做了什么、去了哪里、说了什么（公众人物动态）
- 问实时数据：股价、行情、天气、汇率
- 问最新新闻、近期事件、周末/昨天/今天发生的事
- 用户想了解某平台（小红书/微博/B站）上的内容 → web_search（在 query 里加平台名）
- 提到了某个网址 → read_url
- 问某件具体事实但答案可能在近期变化
- **用户用"你能查到吗 / 你试试 / 你查一下 / 你能搜 X 吗"等试探口吻**提到具体人物、事件、时间——
  这其实是用户想要那个信息，不要把它当作"问 bot 能力"。
  例："你能查到五月天北京最后一天的演唱会吗" → needed=true, query="五月天 北京 演唱会 {today_year}"

不需要查询的情况：闲聊、情绪倾诉、回忆往事、问观点/建议、日常打招呼、问你个人感受。
"你有 search 吗"这种**纯能力试探**（没附带具体话题）→ needed=false。

如果用户问的是最近/实时的事情，优先 needed=true，不要因为"不确定有没有结果"而放弃搜索。

**关于 query 里的年份**：今年是 **{today_year}** 年。query 里涉及"最近/今年/最新"等时间限定时，
**必须用 {today_year}**——不要写成 {last_year} 或更早年份。

输出严格 JSON（无其他文字）：
{{"needed": true/false, "tool": "web_search|read_url", "query": "搜索词或URL"}}

needed 为 false 时 tool 和 query 可省略。"""


def _tool_detect_system() -> str:
    """运行时拼今天日期——LLM 判定时知道当前年份，避免 query 里写成去年。"""
    from datetime import datetime
    now = datetime.now()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return _TOOL_DETECT_SYSTEM_TEMPLATE.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=weekdays[now.weekday()],
        today_year=now.year,
        last_year=now.year - 1,
    )

_TOOL_FUNCS = {
    "web_search": tools.search_web,
    "read_url": tools.read_url,
}


async def _maybe_fetch_context(user_text: str) -> str:
    """判断是否需要搜索，如需要则执行并返回结果字符串，否则返回空字符串。"""
    try:
        decision = await llm.chat_json(
            [
                {"role": "system", "content": _tool_detect_system()},
                {"role": "user", "content": user_text},
            ],
            temperature=0.1,
            max_tokens=80,
            tier="aux",
        )
        needed = bool(decision and decision.get("needed"))
        tool_name = (decision or {}).get("tool", "") or ""
        query = ((decision or {}).get("query", "") or "").strip()

        # 不管 needed 与否都 audit——观测决策本身，方便回查"为什么没触发搜"
        if not needed:
            audit("tool_decision", needed=False, user_text=user_text[:200],
                  tool=tool_name, query=query[:200])
            return ""

        func = _TOOL_FUNCS.get(tool_name)
        if not func or not query:
            audit("tool_decision", needed=True, user_text=user_text[:200],
                  tool=tool_name, query=query[:200],
                  skipped_reason="unknown_tool" if not func else "empty_query")
            return ""

        log.info("tool call: %s(%r)", tool_name, query[:60])
        result = await func(query)
        if result:
            log.info("tool result: %d chars", len(result))
        audit("tool_call", tool=tool_name, query=query[:200],
              user_text=user_text[:200],
              result_chars=len(result or ""), result_preview=(result or "")[:300])
        return result
    except Exception as e:
        log.debug("tool detect/exec error: %s", e)
        audit("tool_decision", needed=False, user_text=user_text[:200],
              skipped_reason=f"err:{type(e).__name__}:{str(e)[:120]}")
        return ""


async def handle_user_message(
    user_id: int,
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

    uid = _uid(user_id)
    recent = _recent_per_user.setdefault(uid, [])

    audit("user_msg", user_id=user_id, text=user_text, has_image=bool(image_b64),
          image_media_type=image_media_type, recent_len=len(recent))

    from .config import settings as _settings
    if image_b64 and _settings().llm_provider == "minimax":
        log.info("image dropped: provider=minimax 当前模型不支持 vision")
        msg = (f"图我现在看不见（图模型挂了）\n你说的「{user_text[:30]}」我倒是能聊"
               if user_text else "图我看不见呢\n描述一下？")
        audit("assistant_reply", user_id=user_id, text=msg, mode="degrade",
              reason="provider=minimax no vision")
        await send(msg)
        return

    import time as _time
    _t0 = _time.time()
    messages, ctx = await _build_turn(user_id, user_text,
                                      image_b64=image_b64, image_media_type=image_media_type)

    em = ctx.get("emotion")
    mode = getattr(em, "mode", "casual")
    if mode == "depth":
        temperature, max_tokens = 0.85, 600
    elif mode == "empathy":
        temperature, max_tokens = 0.6, 400
    elif mode == "interest":
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
        audit("assistant_reply", user_id=user_id, text="(脑子卡了一下)", mode=mode,
              error=str(e)[:200], provider=s.llm_provider, model=active_model)
        await send("（脑子卡了一下）")
        return

    reply = reply.strip()
    if not reply:
        audit("assistant_reply", user_id=user_id, text="", mode=mode, error="empty reply",
              provider=s.llm_provider, model=active_model)
        return

    audit("assistant_reply", user_id=user_id, text=reply, mode=mode,
          temperature=temperature, max_tokens=max_tokens,
          provider=s.llm_provider, model=active_model,
          latency_ms=int((_time.time() - _t0) * 1000),
          memories_used=len(ctx.get("memories") or []),
          topics=ctx.get("topics") or [],
          tool_ctx_chars=len(ctx.get("tool_ctx") or ""),
          history_len=len(recent))

    history_user_text = user_text if user_text else ("[图片]" if image_b64 else "")
    if image_b64 and user_text:
        history_user_text = f"[图片] {user_text}"
    recent.append({"role": "user", "content": history_user_text})
    recent.append({"role": "assistant", "content": reply})
    _trim(uid)
    _save_recent()

    if mode == "depth":
        piece_limit, merge_limit = 80, 14
    elif mode == "empathy":
        piece_limit, merge_limit = 40, 12
    elif mode == "interest":
        piece_limit, merge_limit = 55, 10
    else:
        piece_limit, merge_limit = 60, 12

    segments = stickers.parse_message(reply) if send_sticker else [("text", reply)]
    if not segments:
        segments = [("text", reply)]
    for kind, payload in segments:
        if kind == "text" and payload:
            await deliver(
                payload, send, typing_action,
                max_piece_chars=piece_limit, merge_up_to=merge_limit,
            )
        elif kind == "sticker" and send_sticker is not None:
            try:
                await send_sticker(payload)
            except Exception as e:
                log.exception("send_sticker failed: %s", e)

    asyncio.create_task(_post_turn(user_id, history_user_text, reply))


async def _post_turn(user_id: int, user_text: str, reply: str) -> None:
    try:
        memory.note_turn(user_id, user_text, reply)
        await memory.maybe_flush(user_id)
    except Exception as e:
        log.debug("post_turn memory err: %s", e)
    try:
        availability.record(user_id)
    except Exception as e:
        log.debug("post_turn availability err: %s", e)


async def generate_welcome(user_id: int) -> str:
    """新用户激活后的第一条 AI 消息——目的是让对方愿意继续聊。

    - 不召回记忆（新人没记忆）
    - 不带兴趣（没素材）
    - 用 WELCOME_OPENER_INSTRUCTIONS 指令套路
    """
    persona = load_persona_state(user_id)
    sys_prompt = prompts.build_system_prompt(
        persona=persona,
        memories=[],
        interests_top=[],
        interests_cold=[],
    )
    bits = [f"现在 {clock.now_signal()}"]
    hint = "[" + "｜".join(bits) + "]\n\n" + prompts.WELCOME_OPENER_INSTRUCTIONS
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": hint},
    ]
    text = await llm.chat(messages, temperature=1.0, max_tokens=300)
    text = text.strip()
    audit("welcome_generated", user_id=user_id, text=text)
    return text


async def generate_opener(user_id: int, context: dict | None = None) -> str:
    """scheduler 主动发起时用这个。
    context 由 proactive.decide 提供：user_probably_doing / opener_angle / recent_topics。
    没 context 就退回旧的通用指令。"""
    persona = load_persona_state(user_id)
    top = interests.top(user_id, 5)
    cold_ = interests.cold(user_id, 3)
    sys_prompt = prompts.build_system_prompt(
        persona=persona,
        memories=[],  # 主动开场不主推记忆，避免显得在翻旧账
        interests_top=top,
        interests_cold=cold_,
    )
    hint = prompts.render_proactive_opener(context) if context else prompts.PROACTIVE_OPENER_INSTRUCTIONS
    bits = [f"现在 {clock.now_signal()}"]
    idle_sec = availability.seconds_since_last_interaction(user_id)
    if idle_sec != float("inf") and idle_sec > 30:
        bits.append(f"距上次聊 {clock.since_phrase(idle_sec)}")
    hint = "[" + "｜".join(bits) + "]\n\n" + hint
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": hint},
    ]
    text = await llm.chat(messages, temperature=1.0, max_tokens=200)
    text = text.strip()
    audit("proactive_opener_generated", user_id=user_id, text=text, context=context or {},
          idle_sec=int(idle_sec) if idle_sec != float("inf") else -1)
    return text
