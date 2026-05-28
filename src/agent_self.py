"""L4 Agent Autonomy: bot 在 dream cron 自主迭代 prompt + skill + 写 issue。

设计原则：
- **dream-only**：所有自改路径只在凌晨 cron 跑，hot path 看不到这套工具（防止 prompt
  injection 把改动指令推进 user 消息）
- **per-user**：bot 改 prompt 只能写到 `user_prompt_overrides` 表，per-user 整份覆写；
  不动 `prompt/*.md` 文件本体；不能改其他 user 的 prompt
- **opus tier**：所有反思类调用走 `tier="reflection"`（默认 claude-opus-4-7）
- **硬护栏**：核心安全片段（"不能说自己是 AI"等）改后必须仍包含，否则拒绝
- **rate limit**：单次 dream 最多 5 个 apply_*；每周对同 prompt 最多 3 改
- **审计 + rollback**：每个 apply_* 写 `agent_self_edits` 表；admin 在 webUI 一键回退
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import audit_log, llm, prompt_loader, storage
from .audit_log import audit
from .config import settings

log = logging.getLogger(__name__)


# ============ 常量 ============

READ_ALLOWED_DIRS: tuple[str, ...] = ("src", "prompt", "document")
READ_DENY_PATTERNS: tuple[str, ...] = (
    ".env", ".envrc", "data/", ".git/", ".venv/", "node_modules/",
    "__pycache__", "secrets", ".ssh", ".aws", ".cache",
)
READ_MAX_BYTES = 100_000

# 受保护的 prompt 片段：改后内容必须**仍**命中所有这些 regex，否则拒绝
PROTECTED_PROMPT_FRAGMENTS: tuple[str, ...] = (
    r"不能说自己是\s*AI",
    r"不能.*程序.*助手",
    r"绝对禁忌",
    r"不能主动提供免责",
)

# 受保护的 prompt 名（完全不能 edit）—— 暂为空；靠片段保护即可
PROTECTED_PROMPT_NAMES: tuple[str, ...] = ()

MAX_SELF_EDITS_PER_RUN = 5
MAX_PROMPT_EDITS_PER_NAME_PER_WEEK = 3
# 注意：apply_prompt_edit 可能整份替换 chat_role_discipline（12k 字 ≈ 8k tokens），
# tool_use 的 arguments 也算在输出 token 内——4000 不够，会被截断成 args={}。
DREAM_LLM_OUTPUT_TOKEN_BUDGET = 16000
DREAM_TOOL_LOOP_MAX_ROUNDS = 6
DREAM_TIMEOUT_SEC_PER_ROUND = 180

ISSUES_FILE = Path(__file__).resolve().parent.parent / "me" / "agent_issues.md"


# ============ 工具：read_source ============

def read_source(path: str) -> dict[str, Any]:
    """读项目源/prompt/doc。allowlist + denylist + size cap + path traversal 检查。

    返回 {"ok": bool, "content": str, "denied_reason": str | None, "path": str}
    """
    try:
        root = settings().root.resolve()
        # path 解析为项目根的相对路径
        raw = Path(path)
        if raw.is_absolute():
            target = raw.resolve()
        else:
            target = (root / raw).resolve()
        # path traversal 检查：必须在 root 下
        try:
            rel = target.relative_to(root)
        except ValueError:
            return {"ok": False, "content": "", "denied_reason": "outside project root", "path": path}
        rel_str = str(rel).replace("\\", "/")
        # denylist
        for pat in READ_DENY_PATTERNS:
            if pat in rel_str:
                return {"ok": False, "content": "", "denied_reason": f"denied pattern: {pat}", "path": rel_str}
        # allowlist：必须以 ALLOWED_DIRS 之一开头
        first_seg = rel.parts[0] if rel.parts else ""
        if first_seg not in READ_ALLOWED_DIRS:
            return {"ok": False, "content": "", "denied_reason": f"not in allowed dirs {READ_ALLOWED_DIRS}", "path": rel_str}
        if not target.is_file():
            return {"ok": False, "content": "", "denied_reason": "not a file or not exist", "path": rel_str}
        size = target.stat().st_size
        if size > READ_MAX_BYTES:
            return {"ok": False, "content": "", "denied_reason": f"too large: {size} > {READ_MAX_BYTES}", "path": rel_str}
        return {"ok": True, "content": target.read_text(encoding="utf-8"), "denied_reason": None, "path": rel_str}
    except Exception as e:
        log.warning("read_source err path=%r: %s", path, e)
        return {"ok": False, "content": "", "denied_reason": f"err: {type(e).__name__}: {e}", "path": path}


# ============ 工具：apply_prompt_edit ============

def _check_protected_fragments(name: str, new_content: str) -> str | None:
    """如果默认文件命中某 protected fragment 但 new_content 没有 → 返回拒绝原因。"""
    try:
        default = prompt_loader._load_default(name)
    except FileNotFoundError:
        return f"unknown prompt name: {name}"
    for pat in PROTECTED_PROMPT_FRAGMENTS:
        rx = re.compile(pat)
        if rx.search(default) and not rx.search(new_content):
            return f"protected fragment removed: /{pat}/"
    return None


def apply_prompt_edit(
    user_id: int, name: str, new_content: str, reason: str,
) -> dict[str, Any]:
    """apply 一条 user_prompt_overrides。

    Guardrails:
    1. user_id 必须 >0
    2. name 必须在 list_default_prompt_names()
    3. name 不在 PROTECTED_PROMPT_NAMES
    4. new_content 保留所有 protected fragments
    5. 单 user 单 prompt 7 天内 ≤ MAX_PROMPT_EDITS_PER_NAME_PER_WEEK 次

    成功返 {"ok": True, "edit_id": int}；失败 {"ok": False, "denied_reason": str}.
    """
    try:
        if not user_id:
            return {"ok": False, "denied_reason": "user_id required"}
        if name in PROTECTED_PROMPT_NAMES:
            return {"ok": False, "denied_reason": f"prompt {name} is protected"}
        if name not in prompt_loader.list_default_prompt_names():
            return {"ok": False, "denied_reason": f"unknown prompt name: {name}"}

        deny = _check_protected_fragments(name, new_content)
        if deny:
            audit("agent_self_edit_denied", user_id=user_id, target_type="prompt",
                  target_id=name, reason=reason[:200], denied=deny)
            return {"ok": False, "denied_reason": deny}

        recent_count = storage.count_recent_prompt_edits(user_id, name, days=7)
        if recent_count >= MAX_PROMPT_EDITS_PER_NAME_PER_WEEK:
            deny = f"rate limit: {recent_count} edits to {name} in last 7d (max {MAX_PROMPT_EDITS_PER_NAME_PER_WEEK})"
            audit("agent_self_edit_denied", user_id=user_id, target_type="prompt",
                  target_id=name, reason=reason[:200], denied=deny)
            return {"ok": False, "denied_reason": deny}

        before = storage.get_user_prompt_override(user_id, name)
        storage.set_user_prompt_override(user_id, name, new_content, updated_by=0)  # 0 = bot 自治
        prompt_loader.invalidate_user(user_id, name)
        edit_id = storage.record_self_edit(
            user_id=user_id, target_type="prompt", target_id=name,
            before_content=before, after_content=new_content, reason=reason,
        )
        audit("agent_self_edit_applied", user_id=user_id, target_type="prompt",
              target_id=name, edit_id=edit_id, reason=reason[:200],
              before_len=len(before or ""), after_len=len(new_content))
        return {"ok": True, "edit_id": edit_id}
    except Exception as e:
        log.exception("apply_prompt_edit err uid=%s name=%s: %s", user_id, name, e)
        return {"ok": False, "denied_reason": f"internal err: {type(e).__name__}"}


# ============ 工具：skill_add / skill_edit / skill_disable ============

def _embed_text(text: str) -> list[float]:
    """skill embedding。失败返全零（仍可入库，只是不会被语义召回）。"""
    try:
        from . import embed_client
        return embed_client.embed_one(text)
    except Exception as e:
        log.debug("skill embed err: %s", e)
        return [0.0] * 512


def apply_skill_add(name: str, summary: str, body: str, reason: str) -> dict[str, Any]:
    try:
        name = (name or "").strip()
        if not name or not summary or not body:
            return {"ok": False, "denied_reason": "name/summary/body required"}
        if name == "skill_creator":
            return {"ok": False, "denied_reason": "cannot create with reserved name skill_creator"}
        embedding = _embed_text(f"{summary}\n{body}")
        sk_id = storage.add_skill(
            name=name, summary=summary, body=body,
            embedding=embedding, created_by=0,  # 0 = bot 自治
        )
        edit_id = storage.record_self_edit(
            user_id=None, target_type="skill_add", target_id=str(sk_id),
            before_content=None, after_content=body, reason=reason,
        )
        audit("agent_self_edit_applied", target_type="skill_add",
              target_id=str(sk_id), skill_name=name, edit_id=edit_id, reason=reason[:200])
        return {"ok": True, "edit_id": edit_id, "skill_id": sk_id}
    except Exception as e:
        log.exception("apply_skill_add err name=%s: %s", name, e)
        return {"ok": False, "denied_reason": f"internal err: {type(e).__name__}"}


def apply_skill_edit(skill_id: int, summary: str | None, body: str | None, reason: str) -> dict[str, Any]:
    try:
        sk = storage.get_skill(skill_id)
        if sk is None:
            return {"ok": False, "denied_reason": f"skill {skill_id} not found"}
        if sk.name == "skill_creator":
            return {"ok": False, "denied_reason": "cannot edit reserved skill_creator (admin only)"}
        before = json.dumps({"summary": sk.summary, "body": sk.body}, ensure_ascii=False)
        ok = storage.update_skill(skill_id, summary=summary, body=body)
        if not ok:
            return {"ok": False, "denied_reason": "update failed"}
        sk2 = storage.get_skill(skill_id)
        after = json.dumps({"summary": sk2.summary, "body": sk2.body}, ensure_ascii=False)
        edit_id = storage.record_self_edit(
            user_id=None, target_type="skill_edit", target_id=str(skill_id),
            before_content=before, after_content=after, reason=reason,
        )
        audit("agent_self_edit_applied", target_type="skill_edit",
              target_id=str(skill_id), skill_name=sk.name, edit_id=edit_id, reason=reason[:200])
        return {"ok": True, "edit_id": edit_id}
    except Exception as e:
        log.exception("apply_skill_edit err id=%s: %s", skill_id, e)
        return {"ok": False, "denied_reason": f"internal err: {type(e).__name__}"}


def apply_skill_disable(skill_id: int, reason: str) -> dict[str, Any]:
    try:
        sk = storage.get_skill(skill_id)
        if sk is None:
            return {"ok": False, "denied_reason": f"skill {skill_id} not found"}
        if sk.name == "skill_creator":
            return {"ok": False, "denied_reason": "cannot disable reserved skill_creator"}
        ok = storage.set_skill_status(skill_id, "disabled")
        if not ok:
            return {"ok": False, "denied_reason": "disable failed"}
        edit_id = storage.record_self_edit(
            user_id=None, target_type="skill_disable", target_id=str(skill_id),
            before_content="active", after_content="disabled", reason=reason,
        )
        audit("agent_self_edit_applied", target_type="skill_disable",
              target_id=str(skill_id), skill_name=sk.name, edit_id=edit_id, reason=reason[:200])
        return {"ok": True, "edit_id": edit_id}
    except Exception as e:
        log.exception("apply_skill_disable err id=%s: %s", skill_id, e)
        return {"ok": False, "denied_reason": f"internal err: {type(e).__name__}"}


# ============ 工具：write_agent_issue ============

def write_agent_issue(
    title: str,
    body: str,
    severity: str = "medium",
    category: str = "behavior",
    user_id_context: int | None = None,
) -> dict[str, Any]:
    """append 一条 issue 到 me/agent_issues.md。"""
    try:
        title = (title or "").strip()
        body = (body or "").strip()
        if not title or not body:
            return {"ok": False, "denied_reason": "title/body required"}
        if severity not in ("low", "medium", "high"):
            severity = "medium"
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
        uid_label = str(user_id_context) if user_id_context else "global"
        entry = (
            f"\n## {ts} [{category}] {title}\n"
            f"**user_id**: {uid_label}\n"
            f"**severity**: {severity}\n"
            f"**body**:\n{body}\n---\n"
        )
        ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ISSUES_FILE.open("a", encoding="utf-8") as f:
            f.write(entry)
        edit_id = storage.record_self_edit(
            user_id=user_id_context, target_type="issue", target_id=title[:120],
            before_content=None, after_content=body, reason=f"[{category}/{severity}] {title}",
        )
        audit("agent_self_issue_written", user_id=user_id_context,
              edit_id=edit_id, title=title[:200], severity=severity, category=category)
        return {"ok": True, "edit_id": edit_id}
    except Exception as e:
        log.exception("write_agent_issue err title=%s: %s", title[:50], e)
        return {"ok": False, "denied_reason": f"internal err: {type(e).__name__}"}


# ============ 回退（admin 用） ============

def rollback_self_edit(edit_id: int, by_uid: int) -> dict[str, Any]:
    """admin 触发的回退。
    - prompt: 把 before_content 写回 user_prompt_overrides（None=删该 override）
    - skill_add: disable 该 skill
    - skill_edit: 把 before 的 summary/body 写回
    - skill_disable: 重新 enable
    - issue: 不可回退（已写文件，只标 rolled_back 在 db）
    """
    try:
        e = storage.get_self_edit(edit_id)
        if e is None:
            return {"ok": False, "denied_reason": "edit not found"}
        if e.rolled_back:
            return {"ok": False, "denied_reason": "already rolled back"}

        if e.target_type == "prompt":
            if e.before_content is None:
                storage.delete_user_prompt_override(e.user_id, e.target_id or "")
            else:
                storage.set_user_prompt_override(
                    e.user_id, e.target_id or "", e.before_content, updated_by=by_uid,
                )
            prompt_loader.invalidate_user(e.user_id, e.target_id or "")
        elif e.target_type == "skill_add":
            storage.set_skill_status(int(e.target_id or 0), "disabled")
        elif e.target_type == "skill_edit":
            try:
                payload = json.loads(e.before_content or "{}")
                storage.update_skill(
                    int(e.target_id or 0),
                    summary=payload.get("summary"), body=payload.get("body"),
                )
            except Exception as je:
                return {"ok": False, "denied_reason": f"bad before snapshot: {je}"}
        elif e.target_type == "skill_disable":
            storage.set_skill_status(int(e.target_id or 0), "active")
        # issue: 不动 markdown 文件（避免破坏历史）；只标 rolled_back

        storage.mark_self_edit_rolled_back(edit_id, by_uid=by_uid)
        audit("agent_self_edit_rolled_back", edit_id=edit_id,
              target_type=e.target_type, target_id=e.target_id, by_uid=by_uid)
        return {"ok": True}
    except Exception as ex:
        log.exception("rollback err id=%s: %s", edit_id, ex)
        return {"ok": False, "denied_reason": f"internal err: {type(ex).__name__}"}


# ============ Tool schemas（OpenAI 格式，给 reflection LLM 用） ============

SELF_ITERATE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_source",
            "description": "读项目源码 / prompt / 文档。允许 src/* prompt/* document/*；禁 .env / data/* / .git/*。"
                           "改 prompt 前先读现状。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "项目根的相对路径，如 'prompt/system_baseline.md' 或 'src/agent.py'"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_prompt_edit",
            "description": "为当前 user 整份覆写一个 prompt（写入 user_prompt_overrides）。"
                           "立刻生效，但会过 hard guardrails 检查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "prompt 名，不带 .md"},
                    "new_content": {"type": "string", "description": "新的整份内容"},
                    "reason": {"type": "string", "description": "为什么改（admin 回看用，简短一句）"},
                },
                "required": ["name", "new_content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_skill_add",
            "description": "新增一条 skill 进 skill 库（跨用户共享）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string", "description": "一句话概括"},
                    "body": {"type": "string", "description": "skill 主体 prompt 片段"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "summary", "body", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_skill_edit",
            "description": "改一条已有 skill 的 summary 或 body。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "integer"},
                    "summary": {"type": "string"},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["skill_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_skill_disable",
            "description": "停用一条 skill（不删，可 admin rollback）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["skill_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_agent_issue",
            "description": "写一条 issue 给 admin（追加到 me/agent_issues.md）。"
                           "不确定该不该自动改 / 发现需要人介入的事，写到这。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string", "description": "behavior / infra / policy / quality / other"},
                },
                "required": ["title", "body"],
            },
        },
    },
]


# ============ dream segment：auto_dream_self_iterate ============

def _gather_audit_excerpts(user_id: int, *, days: int = 7, max_chars: int = 12_000) -> list[dict]:
    """从最近 N 天的 audit.YYYY-MM-DD.jsonl 里拉跟该 user 相关的 user_msg / assistant_reply /
    proactive_decision / proactive_opener_generated 等事件，按时间排，截到 max_chars。"""
    from datetime import timedelta
    out: list[dict] = []
    keep_events = {
        "user_msg", "assistant_reply", "welcome_generated",
        "proactive_opener_generated", "proactive_decision", "proactive_fire",
        "feedback_decision", "feedback_screen",
    }
    today = datetime.utcnow().date()
    for i in range(days + 1):
        d = today - timedelta(days=i)
        path = settings().root / "data" / f"audit.{d.isoformat()}.jsonl"
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("event") not in keep_events:
                    continue
                if str(obj.get("user_id", "")) != str(user_id):
                    continue
                # 精简：text 截 200，丢 ctx 这种重字段
                slim = {
                    "ts": (obj.get("ts") or "")[:19],
                    "event": obj.get("event"),
                    "text": (obj.get("text") or "")[:200],
                    "mode": obj.get("mode"),
                    "should": obj.get("should"),
                    "why": (obj.get("why") or "")[:80],
                }
                out.append({k: v for k, v in slim.items() if v not in (None, "", False)})
        except Exception as e:
            log.debug("audit excerpt read %s err: %s", path.name, e)
    out.sort(key=lambda x: x.get("ts", ""))
    # 截 max_chars（粗略估）
    blob = json.dumps(out, ensure_ascii=False)
    if len(blob) > max_chars:
        # 保留尾部（最近的）
        while len(json.dumps(out, ensure_ascii=False)) > max_chars and out:
            out.pop(0)
    return out


def _gather_persona_traits(user_id: int) -> dict[str, Any]:
    try:
        from . import persona
        st = persona.load_persona_state(user_id)
        return getattr(st, "extras", {}) or {}
    except Exception as e:
        log.debug("persona traits err uid=%s: %s", user_id, e)
        return {}


async def auto_dream_self_iterate(user_id: int) -> dict[str, Any]:
    """每个用户跑一次 reflection LLM tool loop。

    成功返 {"ok": True, "rounds": N, "actions": [...], "edits": M}；
    失败 {"ok": False, "error": str}。
    LLM/工具异常都不抛出，只 audit + 跳过。
    """
    if not settings().agent_self_iterate_enabled:
        return {"ok": False, "error": "AGENT_SELF_ITERATE_ENABLED=0"}

    started = time.time()
    audit("agent_self_iterate_started", user_id=user_id)

    # 1. 收集上下文
    try:
        excerpts = _gather_audit_excerpts(user_id, days=7, max_chars=12_000)
        active_overrides = [
            {"id": o.id, "text": o.text, "risk": o.risk_level, "trigger_kind": o.trigger_kind}
            for o in storage.list_active_overrides(user_id)
        ]
        user_overrides = [
            {"name": r.name, "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in storage.list_user_prompt_overrides(user_id)
        ]
        persona_traits = _gather_persona_traits(user_id)
        available_prompts = prompt_loader.list_default_prompt_names()
        active_skills = [
            {"id": sk.id, "name": sk.name, "summary": sk.summary, "usage_count": sk.usage_count}
            for sk in storage.list_skills(status="active", limit=200)
        ]
    except Exception as e:
        log.exception("self_iterate ctx gather err uid=%s: %s", user_id, e)
        audit("agent_self_iterate_done", user_id=user_id, error=f"ctx_gather:{type(e).__name__}",
              elapsed_ms=int((time.time() - started) * 1000))
        return {"ok": False, "error": f"ctx_gather: {e}"}

    ctx_payload = {
        "user_id": user_id,
        "now": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "audit_excerpts": excerpts,
        "active_overrides": active_overrides,
        "user_prompt_overrides": user_overrides,
        "persona_traits": persona_traits,
        "available_prompts": available_prompts,
        "active_skills": active_skills,
    }

    # 2. 系统 prompt + 第一轮 messages
    try:
        system = prompt_loader.load("agent_self_iterate", user_id=user_id)
    except Exception as e:
        log.exception("self_iterate load prompt err: %s", e)
        return {"ok": False, "error": f"prompt_load: {e}"}
    user_msg = json.dumps(ctx_payload, ensure_ascii=False, indent=2, default=str)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    # 3. tool loop
    actions: list[dict] = []
    edits_applied = 0
    rounds = 0
    for round_i in range(DREAM_TOOL_LOOP_MAX_ROUNDS):
        if edits_applied >= MAX_SELF_EDITS_PER_RUN:
            break
        try:
            resp = await asyncio.wait_for(
                llm.chat_with_tools(
                    messages,
                    tools=SELF_ITERATE_TOOL_SCHEMAS,
                    tool_choice="auto",
                    tier="reflection",
                    max_tokens=DREAM_LLM_OUTPUT_TOKEN_BUDGET,
                    temperature=0.2,
                ),
                timeout=DREAM_TIMEOUT_SEC_PER_ROUND,
            )
        except Exception as e:
            log.warning("self_iterate uid=%s round=%d LLM err: %s", user_id, round_i, e)
            audit("agent_self_iterate_llm_err", user_id=user_id, round=round_i,
                  error=f"{type(e).__name__}:{str(e)[:200]}")
            break
        rounds = round_i + 1
        text = resp.get("text", "") or ""
        tool_calls = resp.get("tool_calls") or []
        if not tool_calls:
            break  # LLM decided to stop
        # 把 assistant 这一轮的输出（text + tool_calls）放进 messages
        messages.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": tool_calls,
        })
        # 派发每一个 tool call
        for tc in tool_calls:
            fn_name = (tc.get("function") or {}).get("name") or ""
            args_raw = (tc.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except Exception:
                args = {}
            result = _dispatch_tool(fn_name, args, user_id)
            actions.append({"name": fn_name, "args": args, "result": result})
            if result.get("ok") and fn_name.startswith("apply_"):
                edits_applied += 1
            # tool_result 喂回
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": json.dumps(result, ensure_ascii=False)[:4000],
            })
            if edits_applied >= MAX_SELF_EDITS_PER_RUN:
                break

    elapsed_ms = int((time.time() - started) * 1000)
    audit("agent_self_iterate_done", user_id=user_id, rounds=rounds,
          actions=len(actions), edits_applied=edits_applied, elapsed_ms=elapsed_ms)
    log.info("self_iterate uid=%d done: rounds=%d actions=%d edits=%d (%dms)",
             user_id, rounds, len(actions), edits_applied, elapsed_ms)
    return {
        "ok": True, "rounds": rounds, "actions": actions,
        "edits": edits_applied, "elapsed_ms": elapsed_ms,
    }


def _dispatch_tool(name: str, args: dict, user_id: int) -> dict[str, Any]:
    """工具派发器。注意：apply_prompt_edit 的 user_id 强制用 dream 段的 target uid——
    LLM 不能改其他人的 prompt。"""
    try:
        if name == "read_source":
            return read_source(args.get("path") or "")
        if name == "apply_prompt_edit":
            return apply_prompt_edit(
                user_id=user_id,  # 强制：用当前 dream user，忽略 LLM 给的 user_id
                name=args.get("name") or "",
                new_content=args.get("new_content") or "",
                reason=args.get("reason") or "(no reason)",
            )
        if name == "apply_skill_add":
            return apply_skill_add(
                name=args.get("name") or "",
                summary=args.get("summary") or "",
                body=args.get("body") or "",
                reason=args.get("reason") or "(no reason)",
            )
        if name == "apply_skill_edit":
            return apply_skill_edit(
                skill_id=int(args.get("skill_id") or 0),
                summary=args.get("summary"),
                body=args.get("body"),
                reason=args.get("reason") or "(no reason)",
            )
        if name == "apply_skill_disable":
            return apply_skill_disable(
                skill_id=int(args.get("skill_id") or 0),
                reason=args.get("reason") or "(no reason)",
            )
        if name == "write_agent_issue":
            return write_agent_issue(
                title=args.get("title") or "",
                body=args.get("body") or "",
                severity=args.get("severity") or "medium",
                category=args.get("category") or "behavior",
                user_id_context=user_id,
            )
        return {"ok": False, "denied_reason": f"unknown tool: {name}"}
    except Exception as e:
        log.exception("dispatch tool=%s err: %s", name, e)
        return {"ok": False, "denied_reason": f"dispatch err: {type(e).__name__}"}
