"""Feedback sub-agent 用的 prompt 渲染器。

prompt 内容已抽到 `prompt/feedback_*.md`（2026-05-21）；本模块只保留 render 函数。
"""
from __future__ import annotations

from . import prompt_loader


# 启动时种入 skills 表的 skill_creator meta-skill
SKILL_CREATOR_NAME = "skill_creator"
SKILL_CREATOR_SUMMARY = "[meta] 把用户的功能希望（capability_request）转写成 trigger-based 指令"


def _skill_creator_body() -> str:
    """种 skill_creator 时用——返回最新版 body。"""
    return prompt_loader.load("feedback_skill_creator")


# 兼容旧引用：storage._seed_skill_creator 和外部代码可能拿 SKILL_CREATOR_BODY
# 走 module __getattr__ 实现 lazy load + 始终最新
def __getattr__(name: str) -> str:
    if name == "SKILL_CREATOR_BODY":
        return prompt_loader.load("feedback_skill_creator")
    if name == "HARD_GUARDRAILS":
        return prompt_loader.load("feedback_hard_guardrails")
    if name == "SCREEN_PROMPT":
        return prompt_loader.load("feedback_screen")
    if name == "JUDGE_PROMPT":
        return prompt_loader.load("feedback_judge")
    raise AttributeError(name)


def render_screen(resource: str) -> str:
    return prompt_loader.load("feedback_screen").format(resource=resource.strip())


def render_skill_creator(user_request: str, resource: str, body_template: str) -> str:
    """body_template 是 skill_creator skill 的 body（默认从 feedback_skill_creator.md 读；
    admin 改 skill 表里的 body 也走得通——传进来什么用什么）。

    用 str.replace 而不是 .format——body 里有大量字面 `{` `}`（JSON schema 例子等），
    .format 会把 single brace 当变量名抛 KeyError。约定 placeholder 是
    `{user_request}` 和 `{resource}`，replace 即可。
    """
    out = body_template
    out = out.replace("{user_request}", user_request.strip())
    out = out.replace("{resource}", resource.strip())
    return out


def render_judge(
    resource: str,
    existing_overrides: list[str],
    candidate_skills: list[dict],
) -> str:
    """existing_overrides: list of override.text；candidate_skills: list of {id, name, summary, body, similarity}."""
    if existing_overrides:
        eo = "\n".join(f"- {t}" for t in existing_overrides)
    else:
        eo = "（无）"

    if candidate_skills:
        cs_lines = []
        for c in candidate_skills:
            cs_lines.append(
                f"- id={c['id']} sim={c.get('similarity', 0):.2f} name={c['name']}\n"
                f"  summary: {c['summary']}\n"
                f"  body: {c['body']}"
            )
        cs = "\n".join(cs_lines)
    else:
        cs = "（无候选 skill）"

    return prompt_loader.load("feedback_judge").format(
        resource=resource.strip(),
        existing_overrides=eo,
        candidate_skills=cs,
        hard_guardrails=prompt_loader.load("feedback_hard_guardrails"),
    )
