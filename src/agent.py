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
    # 2026-05-21：aux detect 路径退役——主 LLM 走 native tool_use 自己决定调工具。
    # 这里只保留 URL 自动路由（确定性，不需要 LLM 判断）。
    if user_text and tools._URL_RE.search(user_text):
        tool_task = asyncio.create_task(tools.fetch_urls_in_message(user_text))
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

    # PRD：active trigger 暂存的"该主动告诉对方"的内容——融入这一轮上下文，
    # 让 bot 自然带出来（避免单独冒一条打断对话）
    pending_reach_msgs = []
    try:
        from . import storage as _storage
        pending_reach_msgs = _storage.pop_pending_reach_for_merge(user_id)
    except Exception as e:
        log.debug("pop_pending_reach err uid=%s: %s", user_id, e)

    tool_ctx = ctx.get("tool_ctx", "")
    text_parts = [time_prefix]
    if pending_reach_msgs:
        merged = "\n".join(f"- {m.message}" for m in pending_reach_msgs)
        text_parts.append(
            "[系统暗示] 后台触达通道刚好有内容要主动告诉对方（基于对方之前提的偏好/请求）。"
            "你**这一轮的回复**要把下面这条信息**自然地融入**主话题里——不要硬转、不要罗列，"
            "**当成你刚好想起来要顺嘴提的事**。如果对方刚说的内容跟这件事强相关，就接着这个话题；"
            "如果对方在聊别的，先回应对方那条，再用一两句自然带过：\n"
            + merged
        )
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


# 主 LLM native tool_use 的工具派发表（2026-05-21 起取代 aux detect 路径）
_TOOL_FUNCS = {
    "search_web": tools.search_web,
    "read_url": tools.read_url,
    "search_xhs": tools.search_xhs,
    "search_bilibili": tools.search_bilibili,
    "read_github": tools.read_github,
}


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
    # 主 LLM 走 native tool_use（2026-05-21；2026-05-24 放开到 2 次循环）：
    # 允许 search → read 这种连续——之前限 1 次循环导致 bot 搜了但没法读详情，反过来
    # 问 user "你知道吗"。MAX_TOOL_LOOPS=2 让 LLM 自己决定要不要补一次 read_url。
    # 最后一轮 tool_choice='none' 强制写最终回复防止无限循环。
    # MiniMax 不支持，chat_with_tools 内部 fallback 等价无 tools chat。
    MAX_TOOL_LOOPS = 2
    reply = ""
    used_tool: dict[str, Any] | None = None
    last_tool_result = ""
    last_res_text = ""
    messages_w = list(messages)
    try:
        for loop_i in range(MAX_TOOL_LOOPS + 1):
            is_last = loop_i == MAX_TOOL_LOOPS
            choice = "none" if is_last else "auto"
            try:
                res = await llm.chat_with_tools(
                    messages_w,
                    tools=tools.TOOL_SCHEMAS,
                    tool_choice=choice,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except RuntimeError as e:
                # 偶尔 LLM 拿到空 tool_result 后返 whitespace-only content
                log.info("tool loop %d 空 reply: %s", loop_i, str(e)[:120])
                break
            tool_calls = res.get("tool_calls") or []
            last_res_text = res.get("text") or ""
            if not tool_calls:
                reply = last_res_text
                break
            if is_last:
                # tool_choice='none' 不应该返 tool_calls；保险——拿 text 退出
                reply = last_res_text
                break

            tc = tool_calls[0]
            tc_name = tc.get("function", {}).get("name") or ""
            tc_id = tc.get("id") or ""
            args_raw = tc.get("function", {}).get("arguments") or "{}"
            try:
                tc_args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except Exception:
                tc_args = {}
            log.info("主 LLM tool_use loop=%d: %s(%s)", loop_i, tc_name, tc_args)
            audit("main_tool_call", user_id=user_id, tool=tc_name, args=tc_args,
                  call_id=tc_id, loop=loop_i)

            func = _TOOL_FUNCS.get(tc_name)
            if func:
                try:
                    tool_result = await func(**tc_args)
                except TypeError:
                    if tc_args:
                        tool_result = await func(next(iter(tc_args.values())))
                    else:
                        tool_result = ""
                except Exception as e:
                    log.warning("tool exec %s err: %s", tc_name, e)
                    tool_result = ""
            else:
                log.warning("主 LLM 调了未知工具 %s——dispatch table 缺 key", tc_name)
                tool_result = ""

            audit("main_tool_call_result", user_id=user_id, tool=tc_name,
                  result_chars=len(tool_result or ""),
                  result_preview=(tool_result or "")[:300], loop=loop_i)
            last_tool_result = tool_result or ""
            used_tool = {"name": tc_name, "args": tc_args, "result_chars": len(tool_result or "")}

            messages_w.append({
                "role": "assistant",
                "content": last_res_text,
                "tool_calls": [tc],
            })
            messages_w.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result or "（工具未返回结果）",
            })

        # 兜底：tool 跑完但 reply 为空（LLM 返 whitespace 或异常退出）
        if not reply.strip():
            if last_res_text.strip():
                reply = last_res_text
            elif last_tool_result:
                reply = f"刚搜出来一点东西但不知道有没有用——{last_tool_result[:200]}"
            else:
                reply = "搜了下没找到，关键词换换？"
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
    没 context 就退回旧的通用指令。

    PRD：必须看 recent + active overrides——避免选已聊过的话题作 opener_angle，
    避免跟用户表达过的偏好冲突。
    """
    persona = load_persona_state(user_id)
    top = interests.top(user_id, 5)
    cold_ = interests.cold(user_id, 3)
    # 传 user_id → build_system_prompt 把 active overrides 拼到 prompt 末尾
    sys_prompt = prompts.build_system_prompt(
        persona=persona,
        memories=[],  # 主动开场不主推记忆，避免显得在翻旧账
        interests_top=top,
        interests_cold=cold_,
        user_id=user_id,
    )
    if context and context.get("mode") == "share_discovery" and context.get("share_item"):
        hint = prompts.render_proactive_opener_share(context)
    elif context:
        hint = prompts.render_proactive_opener(context)
    else:
        hint = prompts.PROACTIVE_OPENER_INSTRUCTIONS
    bits = [f"现在 {clock.now_signal()}"]
    idle_sec = availability.seconds_since_last_interaction(user_id)
    if idle_sec != float("inf") and idle_sec > 30:
        bits.append(f"距上次聊 {clock.since_phrase(idle_sec)}")
    hint = "[" + "｜".join(bits) + "]\n\n" + hint
    # 把最近 recent 也喂进去——避免写出"问昨天淋雨没"这种已经聊过的话
    recent_msgs = list(_recent_per_user.get(str(user_id), []))[-_SHORT_WINDOW * 2 :]
    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(recent_msgs)
    messages.append({"role": "user", "content": hint})
    text = await llm.chat(messages, temperature=1.0, max_tokens=200)
    text = text.strip()
    audit("proactive_opener_generated", user_id=user_id, text=text, context=context or {},
          idle_sec=int(idle_sec) if idle_sec != float("inf") else -1)
    return text
