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


def _render_emotion(em: Optional[EmotionSignal], user_id: Optional[int] = None) -> str:
    if em is None or em.mode == "casual":
        return ""
    hint_block = f"对方这会儿真正想说的大概是：『{em.hint}』。" if em.hint else ""
    if em.mode == "empathy":
        return "\n" + prompt_loader.load("chat_empathy_directive", user_id=user_id).format(hint_block=hint_block)
    if em.mode == "depth":
        return "\n" + prompt_loader.load("chat_depth_directive", user_id=user_id).format(hint_block=hint_block)
    if em.mode == "interest":
        return "\n" + prompt_loader.load("chat_interest_directive", user_id=user_id).format(hint_block=hint_block)
    return ""


def _render_pending_ideas(ideas: list[dict]) -> str:
    """把 agent_ideas.list_pending 的结果渲染成"脑子里转过的几件事"。

    注入位置：system prompt 末尾。意图是让 bot **不只是被 user 推着走**——
    它有自己想说/想问/想分享的事，user 一开口就能从这里自然引出。

    强护栏：
    - 标明只是脑包内容，**不主动硬提**（避免 bot 每次都把 idea 念出来）
    - 仅在自然切点出现：user 提到相关话题、user 来了个短开聊信号需要抛料
    - 用完一条会被 mark used（这里只是 hint，真消费在 proactive_decide 路径）
    """
    if not ideas:
        return ""
    lines: list[str] = []
    for it in ideas[:3]:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        kind = (it.get("kind") or "").strip()
        kind_label = {
            "question": "想问",
            "follow_up": "想跟进",
            "observation": "观察",
            "share": "想分享",
        }.get(kind, "想到的")
        lines.append(f"- [{kind_label}] {text}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        '\n\n# 你最近脑子里转过这几件事（脑包，不是 todo）\n'
        '这些是你**自己**最近想到的、想跟对方聊的事——可能是想问 ta 的、想跟进的、'
        '突然冒出的观察、想顺手分享的。这是给你看的脑包，**不是必须执行的任务**。\n\n'
        f'{body}\n\n'
        '**怎么用**：\n'
        '- 对方聊到相关话题 → 自然带出来（「哦想起来」「我前几天还在想…」）\n'
        '- 对方发短开聊信号（「嗨」「在吗」「好」）需要你抛料时 → 这里挑一条最自然的出口\n'
        '- 对方在聊别的且没相关切点 → **别硬提**，留着下次\n'
        '- 复述时**不要原话照搬**，用自己的口气抛出来，挑核心点说\n'
        '- **不要复述对方的画像**（「你之前说你喜欢 X」）——那像在教育对方关于 ta 自己\n'
        '- 同一条只抛一次，对方接了/绕开了就翻篇，**别反复刨**'
    )


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
    pending_ideas: Optional[list[dict]] = None,
) -> str:
    body = persona.body
    mem_block = _render_memory(memories)
    if MEMORY_PLACEHOLDER in body:
        body = body.replace(MEMORY_PLACEHOLDER, mem_block)
    else:
        body = body + "\n\n# 你记得的事\n" + mem_block

    interest_block = "\n# 你最近在意的事\n" + _render_interests(interests_top, interests_cold)
    emotion_block = _render_emotion(emotion, user_id=user_id)
    sticker_block = _render_stickers(sticker_tags or [])
    role_block = "\n\n" + prompt_loader.load("chat_role_discipline", user_id=user_id)

    tool_block = ""
    if tool_context:
        tool_block = (
            "\n\n# 刚查到的参考信息\n"
            + tool_context
            + '\n（用自己的话自然提到就好，不要说"根据搜索结果"，只取对话里有用的部分。）'
        )

    # 2026-05-27：注入 bot 自己脑包里"想说的事"——避免 user 短开聊信号时 bot 套嘘寒问暖
    ideas_block = _render_pending_ideas(pending_ideas or [])

    # PRD：用户独立 prompt overrides——追加到末尾，不改 baseline
    user_overrides_block = _render_user_overrides(user_id) if user_id else ""

    return (body + interest_block + emotion_block + sticker_block
            + role_block + tool_block + ideas_block + user_overrides_block)


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
# 注意：这里走默认（没法从模块属性动态拿 user_id）；若要 per-user，caller 改用
# render_welcome_opener / render_proactive_opener 函数（带 user_id 参数）
def __getattr__(name: str) -> str:
    if name == "WELCOME_OPENER_INSTRUCTIONS":
        return prompt_loader.load("chat_welcome_opener")
    if name == "PROACTIVE_OPENER_INSTRUCTIONS":
        return prompt_loader.load("chat_proactive_opener")
    raise AttributeError(name)


def render_welcome_opener(user_id: Optional[int] = None) -> str:
    return prompt_loader.load("chat_welcome_opener", user_id=user_id)


def render_proactive_opener(ctx: dict, user_id: Optional[int] = None) -> str:
    """结合 proactive.decide 的判断，定制主动开场的指令块。

    如果 ctx 里有 source_idea（来自 agent_ideas 非 share kind），叙事提示是"凌晨自己想到的"。
    """
    user_doing = ctx.get("user_probably_doing") or "不确定对方在做什么"
    angle = ctx.get("opener_angle") or "随口一说"
    topics = ctx.get("recent_topics") or []
    topic_line = "、".join(topics[:5]) if topics else "（没有特别热的话题）"

    source_idea = ctx.get("source_idea") or {}
    if source_idea.get("text"):
        kind = source_idea.get("kind", "follow_up")
        kind_label = {
            "question": "想问她一件事",
            "follow_up": "跟进一个之前没聊完的话题",
            "observation": "一个跨条事实的观察",
            "share": "想分享的事",
        }.get(kind, "想到的事")
        idea_hint = (
            '## 这次开口背后有一条「想起来的事」\n\n'
            f'你之前自己琢磨过：「{source_idea["text"]}」（{kind_label}）。\n'
            '现在就着这个想法开口——不是凭空起话头，是真的「想起来」。\n'
            '可以**不必**直接复述这条原话，挑核心点用自己的口气问出来即可。但**不要**\n'
            '复述对方的画像（「你之前说你喜欢 X」），那种像教育对方关于 ta 自己。'
        )
    else:
        idea_hint = ""

    return prompt_loader.load("chat_proactive_opener_with_ctx", user_id=user_id).format(
        user_doing=user_doing, angle=angle, topic_line=topic_line,
        idea_hint_block=idea_hint,
    )


def render_proactive_opener_share(ctx: dict, user_id: Optional[int] = None) -> str:
    """share_discovery 模式开场白：bot 真搜了一条想分享，prompt 覆盖"不要假装刚刷到"禁令。

    如果 ctx 里有 source_idea（来自 agent_ideas 的 share kind），叙事走"想到这事 →
    顺手搜了下"双层；否则纯"刚搜到一条"。
    """
    user_doing = ctx.get("user_probably_doing") or "不确定对方在做什么"
    item = ctx.get("share_item") or {}
    platform = item.get("platform") or "网上"
    title = item.get("title") or ""
    url = item.get("url") or ""
    blurb = item.get("blurb") or ""
    platform_label = {"xhs": "小红书", "bili": "B 站", "web": "网上"}.get(platform, platform)

    source_idea = ctx.get("source_idea") or {}
    if source_idea.get("text"):
        idea_hint = (
            '## 这次的分享是基于你之前自己想到的事\n\n'
            f'你之前在脑子里想过：「{source_idea["text"]}」——'
            '你刚才就着这个想法去搜了下，找到了上面这条。\n\n'
            '所以叙事可以是「想到 X → 顺手搜了下，看到这个」的双层感觉，不是凭空冒出来一条分享。\n'
            '示例口气：\n'
            '✅ 「突然想到 X，搜了下还真有，[链接]」\n'
            '✅ 「想到她可能感兴趣 X，[链接] 这个挺戳的」\n'
            '✅ 「X 这事我前几天还想着——刚搜到这条 [链接]」\n'
            '但**不要**复述对方的画像（「你之前说过你喜欢 X」/「你那个 AI 项目」），'
            '那是教育对方关于 ta 自己。'
        )
    else:
        idea_hint = '## 没有预设想法，纯粹是「刚翻到一条想分享」。'

    return prompt_loader.load("chat_proactive_opener_share_ctx", user_id=user_id).format(
        user_doing=user_doing, platform=platform_label,
        title=title, url=url, blurb=blurb,
        idea_hint_block=idea_hint,
    )
