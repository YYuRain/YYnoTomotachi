"""组装发给 MiniMax 的 system prompt。

规则：
- persona 主体来自 persona.load_persona_state()（第一版即 System Prompt v0.0.1.md）。
- 把 {{#检索记忆.body#}} 替换为 recall 得到的记忆片段（bullet 列表）。
- 追加一块"你最近在意的事"——interests.top() 的热话题；以及一块"最近没聊"——cold。
- emotion 为 None 时该块完全不出现。
- 主动开场：专门一套更简短的系统提示，避免客服腔。

prompt 文本已抽到 `prompt/chat_*.md`（2026-05-21）；本模块只保留组装逻辑。
"""
from __future__ import annotations

import logging
from typing import Optional

from . import prompt_loader
from .emotion import EmotionSignal
from .persona import PersonaState

log = logging.getLogger(__name__)


MEMORY_PLACEHOLDER = "{{#检索记忆.body#}}"


def _render_memory(snippets: list[str]) -> str:
    if not snippets:
        return "（没什么特别印象，照常聊就行）"
    bullets = "\n".join(f"- {s}" for s in snippets)
    return (
        "（下面这些是**过去对话累积的总结**，不是 ta 当下/今天说的话；只作你脑子里的背景。\n"
        "每条前面的日期 `(YYYY-MM-DD)` 是这条记忆形成/最后更新的时间，可以参考——\n"
        "  · 几天内的：可以当作「刚聊过的事」自然带出\n"
        "  · 一个月以前的：是「旧背景」，别假装 ta 此刻刚说\n"
        "对话当中如果对方提某件事，先确认是不是 ta 此刻刚说的，不要把这里的旧记忆当成 ta 现在的发言。）\n"
        + bullets
    )


def _render_interests(top: list[tuple[str, float]], cold_: list[tuple[str, float]]) -> str:
    lines: list[str] = []
    if top:
        hot = "、".join(t for t, _ in top)
        lines.append(f"最近聊得多的：{hot}")
    if cold_:
        cc = "、".join(t for t, _ in cold_)
        lines.append(f"好一阵没聊的：{cc}（别硬提，自然就好）")
    return "\n".join(lines) if lines else "（还没什么明显偏好）"


def _render_emotion(em: Optional[EmotionSignal]) -> str:
    if em is None or em.mode == "casual":
        return ""
    hint_block = f"对方这会儿真正想说的大概是：『{em.hint}』。" if em.hint else ""
    if em.mode == "empathy":
        return "\n" + prompt_loader.load("chat_empathy_directive").format(hint_block=hint_block)
    if em.mode == "depth":
        return "\n" + prompt_loader.load("chat_depth_directive").format(hint_block=hint_block)
    if em.mode == "interest":
        return "\n" + prompt_loader.load("chat_interest_directive").format(hint_block=hint_block)
    return ""


def _render_stickers(tags: list[str]) -> str:
    if not tags:
        return ""
    tag_line = "、".join(tags)
    return f"""\n
# 你能发的表情包
你有这些 tag 的表情包可用：{tag_line}
真的觉得"这一刻发个表情比文字更对劲"时，在回复里写 `[sticker:tag]` 标记，
程序看到这个标记会替你发对应的表情。
- **用得克制**：一条回复最多 1 个，绝大多数情况都不用，跟现实里不会每句配表情一样。
- 不要打字说"我发个表情包"，直接放 `[sticker:xxx]` 标记就行。
- 标记可以单独成段（一条只发表情）也可以混在文字里。
- tag 必须从上面列表里选；写一个不存在的 tag 等于没用。"""


def build_system_prompt(
    persona: PersonaState,
    memories: list[str],
    interests_top: list[tuple[str, float]],
    interests_cold: list[tuple[str, float]],
    emotion: Optional[EmotionSignal] = None,
    tool_context: str = "",
    sticker_tags: Optional[list[str]] = None,
    user_id: Optional[int] = None,
) -> str:
    body = persona.body
    mem_block = _render_memory(memories)
    if MEMORY_PLACEHOLDER in body:
        body = body.replace(MEMORY_PLACEHOLDER, mem_block)
    else:
        body = body + "\n\n# 你记得的事\n" + mem_block

    interest_block = "\n# 你最近在意的事\n" + _render_interests(interests_top, interests_cold)
    emotion_block = _render_emotion(emotion)
    sticker_block = _render_stickers(sticker_tags or [])
    role_block = "\n\n" + prompt_loader.load("chat_role_discipline")

    tool_block = ""
    if tool_context:
        tool_block = (
            "\n\n# 刚查到的参考信息\n"
            + tool_context
            + '\n（用自己的话自然提到就好，不要说"根据搜索结果"，只取对话里有用的部分。）'
        )

    # PRD：用户独立 prompt overrides——追加到末尾，不改 baseline
    user_overrides_block = _render_user_overrides(user_id) if user_id else ""

    return (body + interest_block + emotion_block + sticker_block
            + role_block + tool_block + user_overrides_block)


def _render_user_overrides(user_id: int) -> str:
    """按 user 拉所有 status='active' 的 prompt_overrides，拼到 system prompt 末尾。

    feedback_agent 沉淀的偏好通过这里注入。低风险自动 active；高风险走 admin pending
    所以不会进入这里。失败静默返空——不阻塞主对话。
    """
    try:
        from . import storage
        rows = storage.list_active_overrides(user_id)
    except Exception as log_e:
        log.debug("render user overrides err uid=%s: %s", user_id, log_e)
        return ""
    if not rows:
        return ""
    lines = ["", "", "# 这位对方希望你这样做（之前对话沉淀的偏好；逐条照做）"]
    for r in rows:
        lines.append(f"- {r.text.strip()}")
    return "\n".join(lines)


# 兼容旧引用——agent.py 还在用 prompts.WELCOME_OPENER_INSTRUCTIONS / PROACTIVE_OPENER_INSTRUCTIONS
def __getattr__(name: str) -> str:
    if name == "WELCOME_OPENER_INSTRUCTIONS":
        return prompt_loader.load("chat_welcome_opener")
    if name == "PROACTIVE_OPENER_INSTRUCTIONS":
        return prompt_loader.load("chat_proactive_opener")
    raise AttributeError(name)


def render_proactive_opener(ctx: dict) -> str:
    """结合 proactive.decide 的判断，定制主动开场的指令块。"""
    user_doing = ctx.get("user_probably_doing") or "不确定对方在做什么"
    angle = ctx.get("opener_angle") or "随口一说"
    topics = ctx.get("recent_topics") or []
    topic_line = "、".join(topics[:5]) if topics else "（没有特别热的话题）"
    return prompt_loader.load("chat_proactive_opener_with_ctx").format(
        user_doing=user_doing, angle=angle, topic_line=topic_line,
    )
