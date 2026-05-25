"""自搭记忆栈：prompt 渲染器。

prompt 内容已抽到 `prompt/memory_*.md`（2026-05-21）；本模块只保留 render 函数。
"""
from __future__ import annotations

from . import prompt_loader


def render(resource: str) -> str:
    """填入对话文本 → 返回完整 user prompt 文本。"""
    return prompt_loader.load("memory_extract").format(resource=resource)


def render_conflict_check(new_fact: str, candidates: list[tuple[str, str]]) -> str:
    """new_fact: 新事实 summary。candidates: [(id_str, summary), ...]，已按相似度排好序。"""
    cand_lines = "\n".join(
        f"{i+1}. id={cid}\n   {summary}"
        for i, (cid, summary) in enumerate(candidates)
    )
    return prompt_loader.load("memory_conflict_check").format(
        new_fact=new_fact.strip(),
        candidates=cand_lines,
    )


def render_reverify(fact: str, upstream: list[str], query: str) -> str:
    """fact: 待验证 summary。upstream: 上游事实 summary 列表（按时间倒序，新的在前）。query: 当前用户消息。"""
    if upstream:
        up = "\n".join(f"{i+1}. {s}" for i, s in enumerate(upstream))
    else:
        up = "(没拿到上游事实——可能 deps 关联已被清理；按现有信息直接判)"
    return prompt_loader.load("memory_reverify").format(
        fact=fact.strip(),
        upstream=up,
        query=(query or "").strip() or "(空)",
    )


def render_dream(fact: str, upstream: list[str], neighbors: list[str]) -> str:
    """fact: to_verify summary。upstream: deps 上游 summaries（已剪枝 N 条）。
    neighbors: 同 user 语义相近的 confirmed 条目 summaries（已剪枝 K 条）。
    """
    if upstream:
        up = "\n".join(f"{i+1}. {s}" for i, s in enumerate(upstream))
    else:
        up = "(没拿到上游事实——可能 deps 关联已清理)"
    if neighbors:
        nb = "\n".join(f"{i+1}. {s}" for i, s in enumerate(neighbors))
    else:
        nb = "(语义相近的 confirmed 条目里没找到)"
    return prompt_loader.load("memory_dream").format(
        fact=fact.strip(),
        upstream=up,
        neighbors=nb,
    )


def render_skill_dream(skills: list) -> str:
    """skills: list of Skill ORM。用 str.replace 避免 .format 撞花括号。"""
    lines = []
    for sk in skills:
        ts = sk.created_at.strftime("%Y-%m-%d") if sk.created_at else "?"
        lines.append(
            f"- id={sk.id} name={sk.name} (usage={sk.usage_count}, created={ts})\n"
            f"  summary: {sk.summary}\n"
            f"  body: {sk.body[:240]}"
        )
    block = "\n".join(lines) if lines else "（空）"
    return prompt_loader.load("memory_skill_dream").replace("{skills_block}", block)


def render_insight_dream(items: list[dict], existing: list[dict] | None = None) -> str:
    """items: list of {id, memory_type, summary, created_at}（已剪枝采样）。
    existing: list of {summary, created_at} of 已存在的 insight，用于硬性反重复。

    P1-6（Generative Agents reflection 借鉴）：插入 prompt 让 LLM 写 1-3 条跨条目 insight。
    """
    lines = []
    for it in items:
        ts = it["created_at"].strftime("%Y-%m-%d") if it.get("created_at") else "?"
        short_id = it["id"][:8] if it.get("id") else "????????"
        lines.append(f"- id={short_id} [{it['memory_type']}] ({ts}) {it['summary']}")
    block = "\n".join(lines) if lines else "（空）"

    # existing insights：用 - prefix 加日期，让 LLM 看到时间分布也好辨识"哪些已经写过 N 次"
    if existing:
        ex_lines = []
        for ex in existing:
            ts = ex["created_at"].strftime("%Y-%m-%d") if ex.get("created_at") else "?"
            ex_lines.append(f"- ({ts}) {ex['summary']}")
        existing_block = "\n".join(ex_lines)
    else:
        existing_block = "（暂无——这是第一次写 insight）"

    return (
        prompt_loader.load("memory_insight_dream")
        .replace("{items_block}", block)
        .replace("{existing_insights_block}", existing_block)
    )


def render_form_ideas_dream(items: list[dict], existing: list[dict] | None = None) -> str:
    """items: 同 insight 抽样（profile + event 混合），但用更长窗（30 天足矣，过老的事
    形成的"想问"已经过期）。
    existing: 已存在的 open ideas（避免重复）。
    """
    lines = []
    for it in items:
        ts = it["created_at"].strftime("%Y-%m-%d") if it.get("created_at") else "?"
        short_id = it["id"][:8] if it.get("id") else "????????"
        lines.append(f"- id={short_id} [{it['memory_type']}] ({ts}) {it['summary']}")
    block = "\n".join(lines) if lines else "（空）"

    if existing:
        ex_lines = []
        for ex in existing:
            ts = ex["created_at"].strftime("%Y-%m-%d") if ex.get("created_at") else "?"
            ex_lines.append(f"- ({ts}) [{ex.get('kind','?')}] {ex['text']}")
        existing_block = "\n".join(ex_lines)
    else:
        existing_block = "（暂无）"

    return (
        prompt_loader.load("memory_form_ideas_dream")
        .replace("{items_block}", block)
        .replace("{existing_ideas_block}", existing_block)
    )


def render_override_dream(overrides: list) -> str:
    """overrides: list of PromptOverride ORM objects（拿 id / text / trigger_kind / created_at）。
    用 str.replace 而非 .format——prompt 里有大量字面花括号。"""
    lines = []
    for o in overrides:
        ts = o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "?"
        kind_tag = f"[{o.trigger_kind}]" if getattr(o, "trigger_kind", None) and o.trigger_kind != "passive" else ""
        lines.append(f"- id={o.id} {kind_tag} created={ts}\n  text: {o.text.strip()}")
    block = "\n".join(lines) if lines else "（空）"
    return prompt_loader.load("memory_override_dream").replace("{overrides_block}", block)
