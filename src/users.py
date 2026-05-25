"""用户准入 + 邀请码（多用户化的"门"）+ webUI session token。

设计：
- `users` 表存活跃用户名单。chat_id 是 telegram 那个数字（=user_id）。
- `invite_codes` 表存 admin 生成的邀请码；redeem 一次即标记 used。
- admin 是 settings().admin_chat_id（单例），无需另外 grant。
- 每条入站消息先经过 `is_active(chat_id)`；未激活的 silent drop（除了 /start <code> 路径）。

webUI 登录走 Telegram 链接（/memory 命令）——bot 进程铸 HMAC 签名 token，
admin 容器解 token 即设 cookie。两进程共享 `data/.webui_secret` 文件（compose 同卷）。
"""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from pathlib import Path
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


# =============== webUI session token（HMAC，bot 与 admin 进程共享）===============

# 跨进程共享密钥：写在挂载卷里的固定文件；首启者写、其它读
_SECRET_PATH_NAME = ".webui_secret"
_SESSION_TTL_DEFAULT = 7 * 86400  # 浏览器 cookie 有效期
_LOGIN_TOKEN_TTL = 600            # /memory 给的链接 token 有效期，10 分钟够点开


def _secret_path() -> Path:
    return settings().root / "data" / _SECRET_PATH_NAME


def _get_session_secret() -> bytes:
    """返回 32 字节密钥；优先 env，再 disk file，最后生成并写盘。"""
    env_val = settings().webui_session_secret
    if env_val:
        return env_val.encode()
    p = _secret_path()
    if p.exists():
        try:
            data = p.read_bytes().strip()
            if len(data) >= 16:
                return data
        except Exception as e:
            log.warning("read webui secret failed: %s", e)
    # 首次生成
    secret = secrets.token_hex(32).encode()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secret)
        try:
            p.chmod(0o600)
        except Exception:
            pass
        log.info("generated new webui session secret at %s", p)
    except Exception as e:
        log.warning("write webui secret failed (will use in-memory): %s", e)
    return secret


def make_session_token(user_id: Optional[int], is_admin: bool, *, ttl: int = _LOGIN_TOKEN_TTL) -> str:
    """铸一个签名 token。bot 进程在 /memory 命令里用——admin 容器收到后 set cookie。"""
    payload = {
        "v": user_id,
        "a": bool(is_admin),
        "exp": int(time.time()) + ttl,
    }
    body = urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = hmac.new(_get_session_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_session_token(token: str) -> Optional[dict]:
    """校验 token；过期/无效返回 None。返回 payload dict（含 v / a / exp）。"""
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_get_session_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = body + "=" * ((4 - len(body) % 4) % 4)
        payload = _json.loads(urlsafe_b64decode(padded.encode()))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


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


def _dump_wipe_backup(user_id: int) -> None:
    """DELETE 前 dump 用户全数据到 data/wipe_backup_<uid>_<ts>/。

    备份范围（best-effort，单点失败不阻塞 wipe）：
    - SQLite 所有有 user_id 列的表 → JSONL
    - postgres memories / episodes / 其它 user_id 表 → JSONL
    - data/recent.json[uid] → recent.json
    """
    from datetime import datetime as _dt
    from pathlib import Path
    from sqlalchemy import select
    from .storage import Base
    from .config import settings as _settings

    ts = _dt.utcnow().strftime("%Y%m%dT%H%M%S")
    out = _settings().root / "data" / f"wipe_backup_{user_id}_{ts}"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("wipe_backup mkdir err: %s", e)
        return

    # SQLite tables
    try:
        with session() as s:
            for tbl in Base.metadata.sorted_tables:
                cols = {c.name for c in tbl.columns}
                filter_col = None
                if tbl.name == "users":
                    filter_col = "chat_id"
                elif tbl.name == "skills" and "created_by" in cols:
                    filter_col = "created_by"
                elif "user_id" in cols:
                    filter_col = "user_id"
                if filter_col is None:
                    continue
                rows = s.execute(
                    select(tbl).where(tbl.c[filter_col] == user_id)
                ).mappings().all()
                if not rows:
                    continue
                fp = out / f"sqlite.{tbl.name}.jsonl"
                with fp.open("w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(_json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.warning("wipe_backup sqlite err: %s", e)

    # postgres tables
    try:
        s = _settings()
        if s.memu_db_url:
            import psycopg  # type: ignore
            dsn = s.memu_db_url.replace("postgresql+psycopg://", "postgresql://")
            uid_str = str(user_id)
            with psycopg.connect(dsn, autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE column_name='user_id' AND table_schema='public'"
                ).fetchall()
                for (t,) in rows:
                    cur = conn.execute(f"SELECT * FROM {t} WHERE user_id=%s", (uid_str,))
                    cols = [d[0] for d in (cur.description or [])]
                    fp = out / f"pg.{t}.jsonl"
                    with fp.open("w", encoding="utf-8") as f:
                        for row in cur.fetchall():
                            f.write(_json.dumps(dict(zip(cols, row)), ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.warning("wipe_backup pg err: %s", e)

    # recent.json[uid]
    try:
        from . import agent  # 延迟避免循环
        snap = agent._recent_per_user.get(str(user_id))
        if snap:
            fp = out / "recent.json"
            fp.write_text(_json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("wipe_backup recent err: %s", e)

    log.info("wipe_backup uid=%d → %s", user_id, out)


def wipe_user(user_id: int) -> dict[str, int]:
    """删除一个用户的全部数据，返回每张表删了多少行。供 test bot /clear 用。

    清除范围：
    - SQLite：interests / reply_samples / last_interaction / proactive_fires / persona_snapshots / users
    - SQLite：invite_codes 中由该 uid redeem 过的码 → used_by/used_at 设回 NULL（让同一 code 可重新激活）
    - memU postgres：所有带 user_id 列的表，DELETE WHERE user_id = str(uid)
    - 进程内存：agent._recent_per_user / memory._buffer_per_user / _last_flush_ts_per_user
    - data/recent.json：重写

    **DELETE 前先 dump backup 到 data/wipe_backup_<uid>_<ts>/**——MEMORY.md 硬规则：
    任何用户级 DELETE 之前必须备份，保 7 天（scheduler.daily_cleanup_job 自动清）。
    """
    from sqlalchemy import delete, update as _update
    from .config import settings as _settings
    from .storage import Base, InviteCode

    # ===== 第 0 步：dump backup =====
    _dump_wipe_backup(user_id)

    counts: dict[str, int] = {}
    # SQLite——反射 Base.metadata 自动找所有有 user_id 列的表，避免漏删。
    # 老法 hardcode (Interest, ReplySample, ProactiveFire, PersonaSnapshot) 漏了
    # PromptOverride / Skill / PendingReachMessage（test bot /clear 后 stale row 留）。
    with session() as s:
        for tbl in Base.metadata.sorted_tables:
            cols = {c.name for c in tbl.columns}
            # User 表自身（chat_id 是 PK，不是 user_id 列名）单独处理；放最后删
            if tbl.name == "users":
                continue
            if tbl.name == "invite_codes":
                continue  # 单独 update（释放码而非 delete）
            # Skill 表 created_by=user_id 但仓库性质——admin 创建的不该被普通用户 wipe
            # 仅当 created_by==user_id（user 自己创建）才删
            if tbl.name == "skills":
                if "created_by" in cols:
                    res = s.execute(delete(tbl).where(tbl.c.created_by == user_id))
                    counts[tbl.name] = int(res.rowcount or 0)
                continue
            if "user_id" in cols:
                res = s.execute(delete(tbl).where(tbl.c.user_id == user_id))
                counts[tbl.name] = int(res.rowcount or 0)
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

    # 记忆栈 postgres：把所有带 user_id 的表都按 uid 清掉
    # （包括新表 memories 和遗留的 memU 表 memory_items/memory_categories/...）
    s = _settings()
    if s.memu_db_url:
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
