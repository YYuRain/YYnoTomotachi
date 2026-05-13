"""用户准入 + 邀请码（多用户化的"门"）。

设计：
- `users` 表存活跃用户名单。chat_id 是 telegram 那个数字（=user_id）。
- `invite_codes` 表存 admin 生成的邀请码；redeem 一次即标记 used。
- admin 是 settings().admin_chat_id（单例），无需另外 grant。
- 每条入站消息先经过 `is_active(chat_id)`；未激活的 silent drop（除了 /start <code> 路径）。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from .audit_log import audit
from .config import settings
from .storage import InviteCode, User, session

log = logging.getLogger(__name__)

# 8 字符 base32（无歧义子集）；安全够用，记得方便
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code() -> str:
    return "".join(secrets.choice(_ALPHA) for _ in range(8))


def is_active(chat_id: int) -> bool:
    with session() as s:
        row = s.get(User, chat_id)
        return row is not None and row.status == "active"


def is_admin(chat_id: int) -> bool:
    return chat_id == settings().admin_chat_id


def list_active() -> list[int]:
    """scheduler 每个 tick 调，返回当前活跃用户的 chat_id 列表。"""
    with session() as s:
        rows = s.execute(
            select(User.chat_id).where(User.status == "active")
        ).scalars().all()
        return list(rows)


def generate_invites(by_admin: int, n: int = 1) -> list[str]:
    """admin 生成 n 个邀请码。返回明文 code 列表。"""
    codes: list[str] = []
    with session() as s:
        for _ in range(n):
            # 防极小概率撞码：碰了重抛
            for _try in range(5):
                code = _gen_code()
                exists = s.get(InviteCode, code)
                if exists is None:
                    s.add(InviteCode(code=code, created_by=by_admin,
                                     created_at=datetime.utcnow()))
                    codes.append(code)
                    break
        s.commit()
    audit("invite_generated", user_id=by_admin, count=len(codes))
    log.info("admin=%d generated %d invite codes", by_admin, len(codes))
    return codes


def redeem(code: str, chat_id: int) -> Optional[str]:
    """校验 + 激活。成功返回 None；失败返回错误描述。"""
    code = (code or "").strip().upper()
    if len(code) != 8:
        return "邀请码格式不对"
    with session() as s:
        # 已是用户：不做无效 redeem
        existing_user = s.get(User, chat_id)
        if existing_user and existing_user.status == "active":
            return "你已经在了"
        row = s.get(InviteCode, code)
        if row is None:
            return "邀请码不存在"
        if row.used_by is not None:
            return "邀请码已被使用"
        # 标记 + 激活
        row.used_by = chat_id
        row.used_at = datetime.utcnow()
        if existing_user:
            existing_user.status = "active"
        else:
            s.add(User(
                chat_id=chat_id,
                status="active",
                created_at=datetime.utcnow(),
                note=f"redeemed:{code}",
            ))
        s.commit()
    audit("user_activated", user_id=chat_id, via_code=code)
    log.info("user activated: chat_id=%d via code=%s", chat_id, code)
    return None


def wipe_user(user_id: int) -> dict[str, int]:
    """删除一个用户的全部数据，返回每张表删了多少行。供 test bot /clear 用。

    清除范围：
    - SQLite：interests / reply_samples / last_interaction / proactive_fires / persona_snapshots / users
    - SQLite：invite_codes 中由该 uid redeem 过的码 → used_by/used_at 设回 NULL（让同一 code 可重新激活）
    - memU postgres：所有带 user_id 列的表，DELETE WHERE user_id = str(uid)
    - 进程内存：agent._recent_per_user / memory._buffer_per_user / _last_flush_ts_per_user
    - data/recent.json：重写
    """
    from datetime import datetime as _dt  # 防与上方 import 冲突
    from sqlalchemy import delete, update as _update
    from .config import settings as _settings
    from .storage import (
        Interest, ReplySample, LastInteraction, ProactiveFire,
        PersonaSnapshot, InviteCode,
    )
    counts: dict[str, int] = {}
    # SQLite
    with session() as s:
        for tbl in (Interest, ReplySample, ProactiveFire, PersonaSnapshot):
            res = s.execute(delete(tbl).where(tbl.user_id == user_id))
            counts[tbl.__tablename__] = int(res.rowcount or 0)
        res = s.execute(delete(LastInteraction).where(LastInteraction.user_id == user_id))
        counts["last_interaction"] = int(res.rowcount or 0)
        # 释放该用户用过的邀请码（让重新走一次注册流程）
        res = s.execute(
            _update(InviteCode)
            .where(InviteCode.used_by == user_id)
            .values(used_by=None, used_at=None)
        )
        counts["invite_codes_freed"] = int(res.rowcount or 0)
        res = s.execute(delete(User).where(User.chat_id == user_id))
        counts["users"] = int(res.rowcount or 0)
        s.commit()

    # memU postgres
    s = _settings()
    if s.memu_metadata_provider == "postgres" and s.memu_db_url:
        try:
            import psycopg  # type: ignore
            dsn = s.memu_db_url.replace("postgresql+psycopg://", "postgresql://")
            uid_str = str(user_id)
            with psycopg.connect(dsn, autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE column_name='user_id' AND table_schema='public'"
                ).fetchall()
                for (t,) in rows:
                    res = conn.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid_str,))
                    counts[f"memu.{t}"] = res.rowcount or 0
        except Exception as e:
            log.exception("wipe_user memU err: %s", e)
            counts["memu_error"] = -1

    # 进程内存 + recent.json
    try:
        from . import agent, memory  # 延迟避免循环
        uid_str = str(user_id)
        agent._recent_per_user.pop(uid_str, None)
        memory._buffer_per_user.pop(uid_str, None)
        memory._last_flush_ts_per_user.pop(uid_str, None)
        agent._save_recent()
    except Exception as e:
        log.debug("wipe_user in-memory cleanup err: %s", e)

    audit("user_wiped", user_id=user_id, counts=counts)
    log.info("user wiped uid=%d counts=%s", user_id, counts)
    return counts


def list_users_with_meta() -> list[dict]:
    """admin 看 /users 时用——返回 [{chat_id, status, created_at, note}, ...]。"""
    with session() as s:
        rows = s.execute(
            select(User).order_by(User.created_at.asc())
        ).scalars().all()
        return [
            {
                "chat_id": r.chat_id,
                "status": r.status,
                "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
                "note": r.note or "",
            }
            for r in rows
        ]
