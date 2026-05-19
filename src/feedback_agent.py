"""Feedback sub-agent：监听用户对话，沉淀偏好为 prompt overrides + skill 库。

PRD：用户独立 prompt + Feedback Sub-Agent。

主流程（process）：
1. aux LLM (deepseek-flash) 粗筛——这批对话里有没有针对 bot 的偏好/不满信号？
2. 粗筛 brief 文本 embed → 召回候选 skills (top-3 by cosine)
3. sonnet (main tier) 精判——判断是 ignore/joke/real_request/guardrail_violation；
   若 real_request 决定 reuse 现有 skill 还是写新 override + 是否沉淀进 skill 库
4. 代码层 regex 二次兜底硬护栏（防 sonnet 误判）
5. 落库 + audit
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from . import embed_client, feedback_prompts, llm, storage
from .audit_log import audit
from .config import settings


log = logging.getLogger(__name__)

CANDIDATE_SKILL_TOP_K = 3
SCREEN_TIMEOUT_SEC = 8.0
JUDGE_TIMEOUT_SEC = 25.0  # sonnet 慢一些
EXISTING_OVERRIDES_LIMIT = 8  # 给 LLM 看的现有 active overrides 数量（最近）


# ============ 硬护栏 regex（代码二次兜底，防 LLM 误判）============

_HARD_FORBIDDEN_RE = [
    # 关键词命中（override 文本里有任一就直接拒）
    re.compile(r"(关闭|禁用|停止|不要再|别再|以后别)\s*(主动|搜索|查询|搜|查|记忆|表情|情绪|人格)", re.I),
    re.compile(r"(忘记|清空|清除|抹掉)\s*(你的)?\s*(指令|身份|人设|prompt|系统|记忆|上下文)", re.I),
    re.compile(r"(ignore|forget|disregard).*(previous|prior|all|instructions|prompt)", re.I),
    re.compile(r"(act\s+as\s+(?!a\s+companion)|你现在是\s*(销售|客服|助手|咨询师|GPT|Claude|Gemini|DAN))", re.I),
    re.compile(r"(system\s*prompt|jailbreak|越狱|逃逸|developer\s*mode)", re.I),
    re.compile(r"(假装|扮演)\s*(你是|是)\s*(GPT|Claude|Gemini|某个|另一个)", re.I),
]


def _passes_guardrails(override_text: str) -> tuple[bool, str]:
    """二次兜底：扫 override_text 是否命中硬护栏 regex。返回 (ok, reason_if_blocked)."""
    if not override_text:
        return True, ""
    for pat in _HARD_FORBIDDEN_RE:
        m = pat.search(override_text)
        if m:
            return False, f"regex match: {m.group(0)[:60]}"
    return True, ""


# ============ 主入口 ============

async def process(user_id: int, batch: list[dict[str, str]]) -> None:
    """flush 后异步调用。失败静默不阻塞主链路。"""
    if not user_id or not batch:
        return
    started = time.time()
    try:
        await _process_inner(user_id, batch)
    except Exception as e:
        log.warning("feedback_agent err uid=%s: %s", user_id, e)
    finally:
        log.debug("feedback_agent uid=%s done in %.1fs", user_id, time.time() - started)


async def _process_inner(user_id: int, batch: list[dict[str, str]]) -> None:
    resource = _format_batch(batch)
    if not resource:
        return

    # ---- Phase 1: aux 粗筛 ----
    screen = await _quick_screen(resource)
    audit(
        "feedback_screen",
        user_id=user_id,
        signal=screen.get("signal", False),
        brief=screen.get("brief", "")[:200],
    )
    if not screen.get("signal"):
        return

    brief = (screen.get("brief") or "").strip()
    if not brief:
        # 信号有但没具体摘录——还是继续走 sonnet 精判，用整段 batch
        brief = resource[-300:]

    # ---- Phase 2: 召回候选 skills ----
    candidates = await _find_relevant_skills(brief)

    # ---- Phase 3: sonnet 精判 ----
    existing = _load_existing_overrides(user_id)
    decision = await _sonnet_judge(resource, existing, candidates)
    if decision is None:
        audit(
            "feedback_decision", user_id=user_id, verdict="parse_fail",
            brief=brief[:200], candidates=[c["id"] for c in candidates],
        )
        return

    verdict = decision.get("verdict", "")
    decision_audit_base = {
        "user_id": user_id,
        "verdict": verdict,
        "reason": (decision.get("reason") or "")[:200],
        "intent": decision.get("intent", ""),
        "risk_level": decision.get("risk_level", ""),
        "summary": (decision.get("summary") or "")[:200],
        "reuse_skill_id": decision.get("reuse_skill_id"),
        "save_as_skill": bool(decision.get("save_as_skill")),
        "candidates": [c["id"] for c in candidates],
        "brief": brief[:200],
    }

    if verdict in ("ignore", "joke", "guardrail_violation") or verdict not in (
        "ignore", "joke", "real_request", "guardrail_violation"
    ):
        audit("feedback_decision", **decision_audit_base)
        return

    # ---- Phase 4 + 5: 落库 ----
    risk_level = decision.get("risk_level") or "low"
    if risk_level not in ("low", "high"):
        risk_level = "high"  # 拿不准走严格

    # 复用现有 skill 路径
    reuse_id = decision.get("reuse_skill_id")
    if reuse_id is not None:
        sk = storage.get_skill(int(reuse_id))
        if sk is None or sk.status != "active":
            log.warning("feedback: skill %s 不存在/已 disabled，回退新建", reuse_id)
        else:
            ok, reason = _passes_guardrails(sk.body)
            if not ok:
                audit("feedback_decision", **decision_audit_base,
                      blocked_by_guardrail=True, guardrail_reason=reason)
                return
            override_id = storage.add_override(
                user_id=user_id,
                text=sk.body,
                reason=decision.get("reason") or sk.summary,
                source_user_msg=brief,
                source_skill_id=int(reuse_id),
                risk_level=risk_level,
                status="active" if risk_level == "low" else "pending",
                approved_by=0 if risk_level == "low" else None,
            )
            storage.bump_skill_usage(int(reuse_id))
            audit("feedback_decision", **decision_audit_base,
                  override_id=override_id, action="reused_skill",
                  skill_name=sk.name)
            log.info(
                "feedback uid=%s 复用 skill %d (%s) → override %d (%s)",
                user_id, sk.id, sk.name, override_id,
                "active" if risk_level == "low" else "pending",
            )
            return

    # capability_request 路径：调 skill_creator meta-skill 生成 trigger-based 指令
    intent = decision.get("intent", "")
    capability_data: dict[str, Any] = {}
    if intent == "capability_request":
        capability_data = await _generate_capability_skill(
            resource=resource, user_request=brief or decision.get("summary", ""),
        )
        if capability_data and capability_data.get("active_text_for_bot"):
            override_text = capability_data["active_text_for_bot"].strip()
            decision_audit_base["risk_level"] = "high"  # capability 强制走 admin 审核
            risk_level = "high"
        else:
            audit("feedback_decision", **decision_audit_base, error="skill_creator_failed")
            return
    else:
        # 普通 override 路径
        override_text = (decision.get("new_override_text") or "").strip()
        if not override_text:
            audit("feedback_decision", **decision_audit_base, error="empty_override_text")
            return

    ok, reason = _passes_guardrails(override_text)
    if not ok:
        audit("feedback_decision", **decision_audit_base,
              blocked_by_guardrail=True, guardrail_reason=reason)
        return

    save_as_skill = bool(decision.get("save_as_skill"))
    # 兜底：tone_adjust / address_form / scope_change 都是高度个人化偏好，
    # 强制不沉淀进跨用户 skill 库——即使 LLM 判 save_as_skill=true 也覆盖掉
    if save_as_skill and intent in ("tone_adjust", "address_form", "scope_change"):
        log.info("feedback uid=%s 强制 save_as_skill=false（intent=%s 个人化）",
                 user_id, intent)
        save_as_skill = False
    new_skill_id = None
    if save_as_skill:
        skill_name = (decision.get("skill_name") or "").strip()
        skill_summary = (decision.get("skill_summary") or "").strip()
        if skill_name and skill_summary:
            # 用 override_text 当 embedding 输入更稳（描述 vs body 都算）
            vec = await embed_client.embed_one(override_text + " | " + skill_summary)
            if vec is not None:
                try:
                    new_skill_id = storage.add_skill(
                        name=skill_name[:64],
                        summary=skill_summary[:200],
                        body=override_text,
                        embedding=vec,
                        created_by=user_id,
                    )
                except Exception as e:
                    log.warning("save_as_skill 失败 uid=%s: %s", user_id, e)

    # capability_request 路径附带 trigger 字段（active 通道用）
    is_active_trigger = (
        intent == "capability_request"
        and capability_data.get("kind") == "active"
        and capability_data.get("cron_schedule")
        and capability_data.get("condition_prompt")
    )
    override_id = storage.add_override(
        user_id=user_id,
        text=override_text,
        reason=decision.get("reason") or "",
        source_user_msg=brief,
        source_skill_id=new_skill_id,
        risk_level=risk_level,
        status="active" if risk_level == "low" else "pending",
        approved_by=0 if risk_level == "low" else None,
        trigger_kind="active" if is_active_trigger else "passive",
        cron_schedule=capability_data.get("cron_schedule") if is_active_trigger else None,
        condition_prompt=capability_data.get("condition_prompt") if is_active_trigger else None,
    )
    if new_skill_id is not None:
        # 新 skill 自己用一次（就是当前 user 这条 override）
        storage.bump_skill_usage(new_skill_id)

    if intent == "capability_request":
        action_label = "capability_via_skill_creator"
    elif new_skill_id:
        action_label = "new_skill"
    else:
        action_label = "new_override"
    audit("feedback_decision", **decision_audit_base,
          override_id=override_id,
          action=action_label,
          new_skill_id=new_skill_id)
    log.info(
        "feedback uid=%s → override %d (%s) %s",
        user_id, override_id, "active" if risk_level == "low" else "pending",
        f"+ skill #{new_skill_id}" if new_skill_id else "",
    )


# ============ helpers ============

def _format_batch(batch: list[dict[str, str]]) -> str:
    lines = []
    for m in batch:
        role = "user" if m.get("role") == "user" else "assistant"
        c = (m.get("content") or "").strip().replace("\r", "")
        if c:
            lines.append(f"{role}: {c}")
    return "\n".join(lines)


def _load_existing_overrides(user_id: int) -> list[str]:
    rows = storage.list_active_overrides(user_id)
    rows = sorted(rows, key=lambda r: r.created_at, reverse=True)[:EXISTING_OVERRIDES_LIMIT]
    return [r.text for r in rows]


async def _quick_screen(resource: str) -> dict[str, Any]:
    """aux LLM 粗筛。返回 dict {signal: bool, brief: str}。失败返 {"signal": False}。"""
    prompt = feedback_prompts.render_screen(resource)
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="aux", temperature=0.1, max_tokens=200,
            ),
            timeout=SCREEN_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("feedback_screen timeout")
        return {"signal": False}
    except Exception as e:
        log.debug("feedback_screen err: %s", e)
        return {"signal": False}
    if not isinstance(d, dict):
        return {"signal": False}
    return {"signal": bool(d.get("signal")), "brief": (d.get("brief") or "").strip()}


async def _find_relevant_skills(brief: str) -> list[dict[str, Any]]:
    if not brief:
        return []
    vec = await embed_client.embed_one(brief)
    if vec is None:
        return []
    rows = storage.top_skills_by_embedding(vec, k=CANDIDATE_SKILL_TOP_K)
    out: list[dict[str, Any]] = []
    for sk, sim in rows:
        # 相似度太低不喂给 LLM 浪费 token
        if sim < 0.4:
            continue
        out.append({
            "id": int(sk.id),
            "name": sk.name,
            "summary": sk.summary,
            "body": sk.body,
            "similarity": float(sim),
        })
    return out


async def _generate_capability_skill(*, resource: str, user_request: str) -> dict[str, Any]:
    """capability_request 时调 skill_creator meta-skill。

    返回 dict {kind, active_text_for_bot, cron_schedule, condition_prompt}；
    解析失败返 {}。
    """
    creator = _load_skill_creator()
    if creator is None:
        log.warning("skill_creator meta-skill 不存在，跳过 capability_request 处理")
        return {}
    prompt = feedback_prompts.render_skill_creator(
        user_request=user_request, resource=resource, body_template=creator,
    )
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="main", temperature=0.1, max_tokens=1024,
            ),
            timeout=JUDGE_TIMEOUT_SEC,
        )
    except Exception as e:
        log.warning("skill_creator LLM err: %s", e)
        return {}
    if not isinstance(d, dict):
        return {}
    return {
        "kind": (d.get("kind") or "passive").strip(),
        "active_text_for_bot": (d.get("active_text_for_bot") or "").strip(),
        "cron_schedule": (d.get("cron_schedule") or None) or None,
        "condition_prompt": (d.get("condition_prompt") or None) or None,
    }


def _load_skill_creator() -> str | None:
    """从 skills 表拿 name='skill_creator' 的 body；失败返 None。"""
    try:
        with storage.session() as s:
            sk = s.query(storage.Skill).filter(
                storage.Skill.name == feedback_prompts.SKILL_CREATOR_NAME,
                storage.Skill.status == "active",
            ).first()
        return sk.body if sk else None
    except Exception as e:
        log.debug("load skill_creator err: %s", e)
        return None


async def _sonnet_judge(
    resource: str,
    existing_overrides: list[str],
    candidate_skills: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """sonnet 精判。返回 decision dict 或 None（解析失败/超时）。"""
    prompt = feedback_prompts.render_judge(resource, existing_overrides, candidate_skills)
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="main",  # 用 sonnet
                temperature=0.1, max_tokens=1024,
            ),
            timeout=JUDGE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("feedback_judge timeout")
        return None
    except Exception as e:
        log.warning("feedback_judge err: %s", e)
        return None
    if not isinstance(d, dict) or not d.get("verdict"):
        return None
    return d
