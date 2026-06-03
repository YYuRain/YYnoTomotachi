"""主动触达 channel——独立于 proactive 冷却的"用户请求型"触达。

来源：feedback agent 把用户的 capability_request（"下班前下雨提醒"）转写成
trigger 类 prompt_overrides，带 `cron_schedule` + `condition_prompt`。

调度：scheduler 的 triggered_reach_job 每分钟跑 tick()——
1. 拉所有 status='active' 且 trigger_kind='active' 的 overrides
2. 看 cron_schedule 是否匹配当前 CST 分钟
3. 跑 sonnet 判 condition_prompt 并生成消息（输出 {should_send: bool, message: str}）
4. should_send=True：
   - user 最近 5 min 内有过 user_msg → 暂存 PendingReachMessage（让下轮 handle_user_message 融入）
   - 否则 → 直接 send + record_proactive_message + UPDATE last_fired_at
5. dedupe：同一 override 同一 cron 分钟不重复 fire（last_fired_at 检查）

兜底：dispatch_overdue() 每分钟扫 status='pending' 且 expected_send_after < now 的，直发。

不走 proactive cooldown——这是用户明确请求的有意图触达，不该被"刚才发过别再烦了"的逻辑拦掉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from . import availability, clock, llm, storage
from .audit_log import audit


log = logging.getLogger(__name__)

# user 最近 5 min 内有 user_msg → 视为"在聊"，暂存而不直发
RECENT_TALK_WINDOW_SEC = 5 * 60
# pending 暂存 5 min 后兜底直发
PENDING_TIMEOUT_SEC = 5 * 60
# 同一 override 同 cron 分钟内只能 fire 一次（dedupe）
DEDUPE_WINDOW_SEC = 90


async def tick() -> None:
    """每分钟一次的主入口。"""
    try:
        rows = storage.list_active_triggers()
    except Exception as e:
        log.warning("triggered_reach: load triggers err: %s", e)
        return
    if not rows:
        return
    # cron 表达式按 user local（CST）语义，所以 _cron_matches_now 用 local；
    # dedupe 用 UTC（last_fired_at 列存 utcnow）。两个域分开避免 dev/prod TZ 差异导致漂移。
    now_local = clock.now_local()
    now_utc = clock.utcnow()
    for ov in rows:
        try:
            if not _cron_matches_now(ov.cron_schedule or "", now_local):
                continue
            # dedupe
            if ov.last_fired_at and (now_utc - ov.last_fired_at).total_seconds() < DEDUPE_WINDOW_SEC:
                continue
            await _try_fire(ov)
        except Exception as e:
            log.warning("triggered_reach override #%s err: %s", ov.id, e)


async def _try_fire(ov) -> None:
    """跑 sonnet 判 condition + 决定 send / 暂存。"""
    msg_data = await _judge_and_compose(ov.condition_prompt or "", user_id=ov.user_id)
    if not msg_data or not msg_data.get("should_send"):
        audit("triggered_reach_check",
              user_id=ov.user_id, override_id=ov.id,
              fired=False, reason=(msg_data or {}).get("reason", "no_send"))
        # 仍标 last_fired_at 防同 cron 分钟重复扫
        storage.mark_override_fired(ov.id)
        return
    message = (msg_data.get("message") or "").strip()
    if not message:
        storage.mark_override_fired(ov.id)
        return

    # 判 user 在不在聊
    idle = availability.seconds_since_last_interaction(ov.user_id)
    if idle != float("inf") and idle < RECENT_TALK_WINDOW_SEC:
        # 暂存等下一轮融入
        expected = clock.utcnow() + timedelta(seconds=PENDING_TIMEOUT_SEC)
        reach_id = storage.add_pending_reach(
            user_id=ov.user_id, override_id=ov.id,
            message=message, expected_send_after=expected,
        )
        storage.mark_override_fired(ov.id)
        audit("triggered_reach_check",
              user_id=ov.user_id, override_id=ov.id,
              fired=True, mode="merge", reach_id=reach_id,
              message=message[:200], idle_sec=int(idle))
        log.info("triggered_reach uid=%s ov=#%d → pending #%d (idle %ds, will merge or 5min fallback): %s",
                 ov.user_id, ov.id, reach_id, int(idle), message[:60])
        return

    # 直接 send
    await _send_active_message(ov.user_id, message, override_id=ov.id, source="direct")
    storage.mark_override_fired(ov.id)
    audit("triggered_reach_check",
          user_id=ov.user_id, override_id=ov.id,
          fired=True, mode="direct",
          message=message[:200], idle_sec=int(idle) if idle != float("inf") else -1)


async def dispatch_overdue() -> None:
    """兜底——pending 超过 5 min 还没被 merge 的，直发。"""
    try:
        rows = storage.list_overdue_pending_reach()
    except Exception as e:
        log.debug("dispatch_overdue load err: %s", e)
        return
    for r in rows:
        try:
            await _send_active_message(
                r.user_id, r.message, override_id=r.override_id, source="overdue",
            )
            storage.mark_pending_reach_status(r.id, "sent")
            audit("triggered_reach_check",
                  user_id=r.user_id, override_id=r.override_id,
                  fired=True, mode="overdue_send", reach_id=r.id,
                  message=r.message[:200])
        except Exception as e:
            log.warning("dispatch_overdue #%s err: %s", r.id, e)


async def _send_active_message(user_id: int, message: str, *, override_id: int, source: str) -> None:
    """跨 prod/test bot 路由 + 发送 + record_proactive_message。"""
    from .scheduler import _resolve_send_typing  # 复用现有路由
    route = _resolve_send_typing(user_id)
    if route is None:
        log.info("triggered_reach uid=%s 无路由（test 身份未 /become 或不在白名单），跳过", user_id)
        return
    send, typing = route
    from .rhythm import deliver
    from .agent import record_proactive_message
    await deliver(message, send, typing)
    # kind="reminder"：active trigger 是条件信息推送，user 读了不回是设计意图，
    # 不计入 proactive soft gate 的 unanswered_streak（见 proactive._compute_soft_gate_skip）。
    record_proactive_message(user_id, message, kind="reminder")


_CONDITION_PARSE_FALLBACK = re.compile(
    r'"should_send"\s*:\s*(true|false)', re.I,
)


async def _judge_and_compose(
    condition_prompt: str, *, user_id: int | None = None,
) -> dict[str, Any]:
    """跑 sonnet 判 condition_prompt → {should_send, message, reason?}。

    condition_prompt 是 skill_creator 输出（含场景、查询步骤、消息格式约束）。
    这里**额外**喂最近对话给 sonnet——让它自己判"刚说过这事没必要再提"，
    避免主动触达跟最近聊天内容撞车显得刻意。
    """
    if not condition_prompt:
        return {}
    full_prompt = condition_prompt
    if user_id:
        try:
            from .agent import _recent_per_user
            # snapshot：跨 task 读，防迭代中被 agent._post_turn 异步 mutate
            recent = list(_recent_per_user.get(str(user_id), []))
        except Exception:
            recent = []
        if recent:
            last = recent[-12:]
            block = "\n".join(
                f"{m.get('role','?')}: {(m.get('content') or '')[:240]}"
                for m in last
            )
            full_prompt += (
                "\n\n## 最近对话片段（仅用于查重）\n"
                + block
                + "\n\n**唯一额外过滤条件**——上面对话里你**已经提醒过同一信息**才 skip。\n"
                "判定标准：bot 是否已经把**这次条件成立要发的同样事实**告诉过对方了？"
                "例如：要发『明天昌平有雨记得带伞』，最近 bot 已经说过『今晚/明天有雨』"
                "且对方有回应——这种已经传达到，should_send=false。\n\n"
                "**不要因为以下原因 skip**：\n"
                "- 对方现在在聊别的事 → 仍然该发（融入主话题的处理在另一层做，你只管判该不该发）\n"
                "- 觉得『时机不太合适 / 显得刻意』 → 仍然该发，宁多勿漏\n"
                "- 对方似乎心情/状态不好 → 仍然该发，对方明确请求过这个提醒\n\n"
                "原则：**没传达过同样信息就一定发**。"
            )
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": full_prompt}],
                tier="main", temperature=0.1, max_tokens=400,
            ),
            timeout=20.0,
        )
    except Exception as e:
        log.warning("condition LLM err: %s", e)
        return {}
    if isinstance(d, dict) and "should_send" in d:
        return {
            "should_send": bool(d.get("should_send")),
            "message": (d.get("message") or "").strip(),
            "reason": (d.get("reason") or "").strip(),
        }
    return {}


# ============ cron 匹配 ============

def _cron_matches_now(cron_expr: str, now: datetime) -> bool:
    """5-field 标准 cron：minute hour day month weekday。

    weekday: 0=周日? 不——APScheduler 风格 0=周一，6=周日；这里用 Python 的
    weekday() 返回值（0=周一...6=周日）跟 APScheduler 对齐。

    支持 `*` / 整数 / 逗号列表 / 范围 a-b / 步长 */N。
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False
    mn, hr, dom, mo, dow = parts
    if not _cron_field_match(mn, now.minute, 0, 59):
        return False
    if not _cron_field_match(hr, now.hour, 0, 23):
        return False
    if not _cron_field_match(dom, now.day, 1, 31):
        return False
    if not _cron_field_match(mo, now.month, 1, 12):
        return False
    # crontab 标准：0=Sun, 1=Mon, ..., 6=Sat（部分实现 7 也认 Sun）。
    # Python weekday(): 0=Mon, ..., 6=Sun。转换：cron_dow = (weekday + 1) % 7
    cron_dow = (now.weekday() + 1) % 7
    if not _cron_field_match(dow, cron_dow, 0, 7):  # 0..7 容许 7=Sun
        return False
    return True


def _cron_field_match(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for piece in field.split(","):
        piece = piece.strip()
        if not piece:
            continue
        # */N
        if piece.startswith("*/"):
            try:
                step = int(piece[2:])
                if step > 0 and value % step == 0:
                    return True
            except ValueError:
                pass
            continue
        # a-b 或 a-b/N
        if "-" in piece:
            rng_s, _, step_s = piece.partition("/")
            try:
                a_s, b_s = rng_s.split("-")
                a, b = int(a_s), int(b_s)
                step = int(step_s) if step_s else 1
                if a <= value <= b and (value - a) % step == 0:
                    return True
            except ValueError:
                pass
            continue
        # 单值
        try:
            if int(piece) == value:
                return True
        except ValueError:
            pass
    return False
