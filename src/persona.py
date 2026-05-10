"""人格演化（persona evolution）。

设计：
- 主体（baseline）来自 `System Prompt v0.0.1.md`，永久不动。
- 动态层 = traits + 心情 + 自我观察 + 共同锚点（milestones），存 `PersonaSnapshot.payload_json`。
- 增量更新：`update_state(messages_batch)` —— 每次 memU buffer flush 成功后异步 fire；aux LLM 看
  这批对话 + 当前 traits 输出 deltas / 新观察 / mood / 偶尔新增的 milestone。
- 每日 consolidate：traits 朝 0 衰减、清掉 3 天前的 observation、milestones 不动。
- `load_persona_state()` 从最新 snapshot 合成动态段，拼到 body 末尾。

外部接口（保持稳定）：
- `PersonaState.body`：完整 system prompt 主体（含动态段，build_system_prompt 直接用）
- `PersonaState.extras`：原始 payload，调用方需要原始数据时取
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import storage
from .audit_log import audit
from .config import settings

log = logging.getLogger(__name__)


# ============ 数据模型 ============

TRAIT_KEYS = ("sarcasm", "warmth", "verbosity", "assertiveness", "curiosity")
TRAIT_LABELS = {
    "sarcasm": "玩笑/毒舌强度",
    "warmth": "温柔/共情成分",
    "verbosity": "话密度",
    "assertiveness": "主动给观点",
    "curiosity": "好奇/挖话题",
}
MAX_DELTA_PER_UPDATE = 0.15
MAX_OBSERVATIONS_KEEP = 5
OBSERVATION_TTL_DAYS = 3
DAILY_DECAY = 0.92  # 每日 consolidate 时 traits *= 此值，朝 0 漂


def _empty_payload() -> dict[str, Any]:
    return {
        "traits": {k: 0.0 for k in TRAIT_KEYS},
        "mood": "",
        "observations": [],   # [{text, ts(ISO)}]
        "milestones": [],     # [{text, ts(ISO)}]
        "updated_at": "",
    }


@dataclass(frozen=True)
class PersonaState:
    body: str
    extras: dict = field(default_factory=dict)


# ============ DB IO ============

def _load_latest_payload() -> dict[str, Any]:
    with storage.session() as s:
        row = (
            s.query(storage.PersonaSnapshot)
            .order_by(storage.PersonaSnapshot.id.desc())
            .first()
        )
        if not row:
            return _empty_payload()
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            log.warning("PersonaSnapshot %s 解析失败，回退空", row.id)
            return _empty_payload()
        # 补齐缺字段
        base = _empty_payload()
        base.update(payload or {})
        for k in TRAIT_KEYS:
            base["traits"].setdefault(k, 0.0)
        return base


def _write_snapshot(payload: dict[str, Any]) -> None:
    payload = {**payload, "updated_at": datetime.utcnow().isoformat()}
    with storage.session() as s:
        s.add(storage.PersonaSnapshot(payload_json=json.dumps(payload, ensure_ascii=False)))
        s.commit()


# ============ 渲染（动态段拼到 body 末尾）============

def _trait_word(v: float) -> str:
    if v >= 0.5:
        return "偏强"
    if v >= 0.2:
        return "略偏强"
    if v <= -0.5:
        return "偏弱"
    if v <= -0.2:
        return "略偏弱"
    return "正常"


def _render_dynamic_block(payload: dict[str, Any]) -> str:
    traits = payload.get("traits") or {}
    mood = (payload.get("mood") or "").strip()
    obs = [o for o in (payload.get("observations") or []) if isinstance(o, dict) and o.get("text")]
    milestones = [m for m in (payload.get("milestones") or []) if isinstance(m, dict) and m.get("text")]

    sections: list[str] = []

    if mood:
        sections.append(f"心情：{mood}")

    deviated = [
        (k, traits.get(k, 0.0))
        for k in TRAIT_KEYS
        if abs(traits.get(k, 0.0)) >= 0.2
    ]
    if deviated:
        lines = ["倾向调整（自然反映在语气里就好）："]
        for k, v in deviated:
            lines.append(f"- {TRAIT_LABELS[k]}：{_trait_word(v)}")
        sections.append("\n".join(lines))

    if obs:
        lines = ["最近的自己："]
        for o in obs[-MAX_OBSERVATIONS_KEEP:]:
            lines.append(f"- {o['text']}")
        sections.append("\n".join(lines))

    if milestones:
        lines = ["你跟对方走过的小锚点（藏着自己知道，不要主动提起，被聊到才提）："]
        for m in milestones[-8:]:
            ts = (m.get("ts") or "")[:10]
            lines.append(f"- {ts} {m['text']}" if ts else f"- {m['text']}")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "\n# 你最近的状态（自我感觉，仅供参考；不要主动告诉对方『我状态怎样』）"
    return header + "\n" + "\n".join(sections)


def load_persona_state() -> PersonaState:
    """读 baseline body + DB 最新动态段，合成完整 PersonaState。"""
    base_body = settings().system_prompt_path.read_text(encoding="utf-8")
    payload = _load_latest_payload()
    dynamic = _render_dynamic_block(payload)
    body = base_body + ("\n\n" + dynamic if dynamic else "")
    return PersonaState(body=body, extras=payload)


# ============ 增量更新（挂 memory.maybe_flush）============

_UPDATE_SYSTEM = """你是一个 AI 陪伴角色的"内在状态更新器"。
角色不是助手、不是客服，是一个有自己个性、会因为长期相处而微调的陪伴者。
读完最新一批对话，判断它的人格状态该怎么微调（保守、克制——大多数轮次什么都不该变）。

# 当前 traits（5 维度，-1..1）
- sarcasm（玩笑/毒舌强度）：{sarcasm}
- warmth（温柔/共情成分）：{warmth}
- verbosity（话密度，负=更短）：{verbosity}
- assertiveness（主动给观点）：{assertiveness}
- curiosity（好奇/挖话题）：{curiosity}

# 最近已有的自我观察（用于去重，不要重复）
{recent_observations}

# 判断原则
- delta 默认 0.0；只有清晰信号才动，单维度通常 ±0.05~0.10，极强信号 ±0.15。
- 用户对玩笑接得住 → sarcasm +；皱眉/转话题/嫌弃 → -。
- 对方走心倾诉多 → warmth +；冷淡/敷衍 → -。
- 用户回复短促/不耐烦 → verbosity -；铺开聊天 → +。
- 用户问观点/请教/反问"你怎么看" → assertiveness +。
- 出现新话题对方接得住 → curiosity +。
- 没有明显信号就所有 delta = 0.0。
- mood：一句话总结角色当下心情，≤ 15 字；不要写"AI 觉得"，直接陈述（如"有点话痨""略疲""挺好奇"）；可为空字符串。
- new_observations：0~2 条；每条 ≤ 25 字；**用聊天口语，不能用书面词**——
  - 禁用："分析框架/预设/认知/元认知/欲盖弥彰/模板化/引导/层次/维度/反应模式"等书面/学术词。
  - 鼓励："你最近话变少""你发现自己挺关心 ta 的猫""你今天有点话痨""被对方一句话噎住了"这种朴素口语。
  - 视角是第二人称（"你..."）——是角色对自己的小观察，不是分析报告。
  - 不要写用户的事，也不要写"分析了对话"这种元层面观察——只写"我自己怎么样了"。
  - 和已有 observations 不重复。
- new_milestones：通常 0 条。**只有真发生重要事**（第一次提家人/养的宠物、一次深夜走心、对方分享重要决定）才 1 条；
  ≤ 30 字；以"年-月-日 + 事件"形式描述但 ts 字段单独给。

# 输出（严格 JSON，无任何额外文字、不要代码块）
{{
  "trait_deltas": {{"sarcasm": 0.0, "warmth": 0.0, "verbosity": 0.0, "assertiveness": 0.0, "curiosity": 0.0}},
  "new_observations": [],
  "new_milestones": [],
  "mood": ""
}}"""


def _format_messages(messages: list[dict[str, str]]) -> str:
    """[{role, content}, ...] → 多行文本。"""
    lines: list[str] = []
    for m in messages[-40:]:  # 最多 40 条避免 prompt 膨胀
        role = "用户" if m.get("role") == "user" else "你"
        content = (m.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


async def update_state(messages_batch: list[dict[str, str]]) -> bool:
    """根据本批对话异步更新 persona。失败/无变化都返回 False，不抛。"""
    if not messages_batch:
        return False
    try:
        from . import llm  # 延迟避免循环

        cur = _load_latest_payload()
        traits = cur.get("traits", {})
        recent_obs = [o.get("text", "") for o in (cur.get("observations") or [])[-MAX_OBSERVATIONS_KEEP:]]
        sys_prompt = _UPDATE_SYSTEM.format(
            sarcasm=traits.get("sarcasm", 0.0),
            warmth=traits.get("warmth", 0.0),
            verbosity=traits.get("verbosity", 0.0),
            assertiveness=traits.get("assertiveness", 0.0),
            curiosity=traits.get("curiosity", 0.0),
            recent_observations="\n".join(f"- {o}" for o in recent_obs) if recent_obs else "（无）",
        )
        user_prompt = "# 本批对话\n" + _format_messages(messages_batch)
        decision = await llm.chat_json(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            # MiniMax-M2 的 <think> 会先吃掉一大块；给足量 budget 才能输出完整 JSON
            max_tokens=2048,
            tier="aux",
        )
    except Exception as e:
        log.debug("persona.update_state aborted: %s", e)
        return False

    if not isinstance(decision, dict):
        return False

    deltas = decision.get("trait_deltas") or {}
    if not isinstance(deltas, dict):
        deltas = {}
    new_obs = decision.get("new_observations") or []
    new_ms = decision.get("new_milestones") or []
    mood = (decision.get("mood") or "").strip()

    changed = False
    new_traits = dict(traits)
    for k in TRAIT_KEYS:
        try:
            d = float(deltas.get(k, 0.0) or 0.0)
        except (TypeError, ValueError):
            d = 0.0
        d = _clamp(d, -MAX_DELTA_PER_UPDATE, MAX_DELTA_PER_UPDATE)
        if abs(d) > 1e-6:
            new_traits[k] = _clamp(new_traits.get(k, 0.0) + d)
            changed = True

    obs_list = list(cur.get("observations") or [])
    now_iso = datetime.utcnow().isoformat()
    for o in new_obs[:2]:
        if isinstance(o, str) and o.strip():
            text = o.strip()[:60]
            # 简单去重
            if not any(ex.get("text") == text for ex in obs_list[-MAX_OBSERVATIONS_KEEP:]):
                obs_list.append({"text": text, "ts": now_iso})
                changed = True
    obs_list = obs_list[-MAX_OBSERVATIONS_KEEP:]

    ms_list = list(cur.get("milestones") or [])
    for m in new_ms[:1]:
        if isinstance(m, str) and m.strip():
            text = m.strip()[:60]
            if not any(ex.get("text") == text for ex in ms_list):
                ms_list.append({"text": text, "ts": now_iso})
                changed = True

    if mood and mood != cur.get("mood", ""):
        cur_mood = mood[:30]
        changed = True
    else:
        cur_mood = cur.get("mood", "")

    if not changed:
        log.debug("persona.update_state: no change")
        return False

    payload = {
        "traits": new_traits,
        "mood": cur_mood,
        "observations": obs_list,
        "milestones": ms_list,
    }
    _write_snapshot(payload)
    log.info(
        "persona updated: traits=%s mood=%r obs=+%d ms=+%d",
        {k: round(new_traits.get(k, 0.0), 2) for k in TRAIT_KEYS},
        cur_mood,
        len([o for o in new_obs[:2] if isinstance(o, str) and o.strip()]),
        len([m for m in new_ms[:1] if isinstance(m, str) and m.strip()]),
    )
    new_obs_added = [o for o in new_obs[:2] if isinstance(o, str) and o.strip()]
    new_ms_added = [m for m in new_ms[:1] if isinstance(m, str) and m.strip()]
    audit("persona_update",
          batch_msgs=len(messages_batch),
          traits_before={k: round(traits.get(k, 0.0), 3) for k in TRAIT_KEYS},
          traits_after={k: round(new_traits.get(k, 0.0), 3) for k in TRAIT_KEYS},
          trait_deltas={k: round(new_traits.get(k, 0.0) - traits.get(k, 0.0), 3)
                        for k in TRAIT_KEYS
                        if abs(new_traits.get(k, 0.0) - traits.get(k, 0.0)) > 1e-6},
          mood=cur_mood,
          new_observations=new_obs_added,
          new_milestones=new_ms_added)
    return True


# ============ 每日 consolidate（scheduler 03:00）============

def consolidate() -> bool:
    """traits 衰减 + 清旧 observations。同步函数（纯本地操作，不调 LLM）。"""
    cur = _load_latest_payload()
    traits = dict(cur.get("traits") or {})
    new_traits = {k: round(_clamp(traits.get(k, 0.0) * DAILY_DECAY), 4) for k in TRAIT_KEYS}

    cutoff = datetime.utcnow() - timedelta(days=OBSERVATION_TTL_DAYS)
    obs_list = []
    for o in cur.get("observations") or []:
        ts_str = o.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts >= cutoff:
            obs_list.append(o)

    audit("persona_consolidate",
          traits_before={k: round(traits.get(k, 0.0), 3) for k in TRAIT_KEYS},
          traits_after={k: round(new_traits.get(k, 0.0), 3) for k in TRAIT_KEYS},
          observations_kept=len(obs_list),
          observations_dropped=len(cur.get("observations") or []) - len(obs_list))
    payload = {
        "traits": new_traits,
        "mood": "",  # 每日 consolidate 后清空当日心情
        "observations": obs_list,
        "milestones": list(cur.get("milestones") or []),  # 永久保留
    }
    _write_snapshot(payload)
    log.info("persona consolidated: traits=%s observations kept=%d", new_traits, len(obs_list))
    return True
