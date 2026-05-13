"""一次性把单用户 ('me') 数据迁移到多用户结构。

跑一次：把现有 SQLite 表加 user_id 列、复制旧数据到 admin 名下；可选 --migrate-memu 同时更新
postgres memU 表（user_id='me' → str(ADMIN_CHAT_ID)）；重写 data/recent.json 从 list 到 dict。

幂等：脚本开头检查 users 表是否存在；存在则跳过 SQLite 步骤。--migrate-memu 仍可单独跑（用 SQL
WHERE user_id='me' 兜底，第二次跑匹配 0 行无影响）。

Usage:
    .venv/bin/python -m scripts.migrate_to_multiuser           # 仅 SQLite + recent.json
    .venv/bin/python -m scripts.migrate_to_multiuser --migrate-memu  # 也迁移 postgres memU
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from src.config import settings

log = logging.getLogger("migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# 旧 schema 表 → 哪些列在迁移后要原样复制（不含新加的 user_id）
OLD_TABLES = {
    "interests": ["topic", "heat", "last_touch"],
    "reply_samples": ["id", "ts", "weekday", "hour", "replied_within_sec"],
    "last_interaction": ["id", "ts"],  # 旧的 id=1 → 新的 user_id=ADMIN_CHAT_ID
    "proactive_fires": ["id", "ts", "why", "user_probably_doing", "opener_angle", "opener_text"],
    "persona_snapshots": ["id", "ts", "payload_json"],
}


def migrate_sqlite(db_path: Path, admin_id: int) -> None:
    if not db_path.exists():
        log.info("SQLite 不存在，新部署直接 create_all 即可：%s", db_path)
        # 触发一次 storage.engine() 让 create_all 跑
        from src import storage
        storage.engine()
        log.info("已创建空 schema")
        return

    # 幂等：已经迁移过就跳过
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        if cur.fetchone():
            log.info("users 表已存在，SQLite 似乎已迁移过——跳过")
            return

    # 备份
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = db_path.with_suffix(f".sqlite.bak.{ts}")
    shutil.copy2(db_path, bak)
    log.info("backup: %s → %s", db_path.name, bak.name)

    # 读旧数据（raw sqlite3 不依赖 SA 模型，避开 schema 不匹配）
    old_rows: dict[str, list[tuple]] = {}
    with sqlite3.connect(str(db_path)) as conn:
        for table, cols in OLD_TABLES.items():
            try:
                rows = conn.execute(
                    f"SELECT {','.join(cols)} FROM {table}"
                ).fetchall()
                old_rows[table] = rows
                log.info("read %s: %d rows", table, len(rows))
            except sqlite3.OperationalError as e:
                log.warning("read %s skipped: %s", table, e)
                old_rows[table] = []

    # 删旧文件让 SA 用新 schema 重建
    db_path.unlink()
    from src import storage
    # 强制重建 engine（settings 缓存的 path 仍指向原位置，但文件现在不存在）
    storage._engine = None  # type: ignore[attr-defined]
    storage._Session = None  # type: ignore[attr-defined]
    storage.engine()  # create_all 跑一次
    log.info("created new schema with user_id columns + users / invite_codes tables")

    # 写回旧数据，附 user_id = admin_id
    with sqlite3.connect(str(db_path)) as conn:
        # interests
        for topic, heat, last_touch in old_rows.get("interests", []):
            conn.execute(
                "INSERT OR IGNORE INTO interests (user_id, topic, heat, last_touch) VALUES (?,?,?,?)",
                (admin_id, topic, heat, last_touch),
            )
        # reply_samples（id 自增，重新分配；保留其他列）
        for _id, ts_, wd, hr, rep in old_rows.get("reply_samples", []):
            conn.execute(
                "INSERT INTO reply_samples (user_id, ts, weekday, hour, replied_within_sec) VALUES (?,?,?,?,?)",
                (admin_id, ts_, wd, hr, rep),
            )
        # last_interaction：旧只有一行（id=1 → user_id=admin_id）
        for _id, ts_ in old_rows.get("last_interaction", []):
            conn.execute(
                "INSERT OR REPLACE INTO last_interaction (user_id, ts) VALUES (?,?)",
                (admin_id, ts_),
            )
        # proactive_fires
        for _id, ts_, why, doing, angle, text in old_rows.get("proactive_fires", []):
            conn.execute(
                "INSERT INTO proactive_fires (user_id, ts, why, user_probably_doing, opener_angle, opener_text) "
                "VALUES (?,?,?,?,?,?)",
                (admin_id, ts_, why, doing, angle, text),
            )
        # persona_snapshots
        for _id, ts_, payload in old_rows.get("persona_snapshots", []):
            conn.execute(
                "INSERT INTO persona_snapshots (user_id, ts, payload_json) VALUES (?,?,?)",
                (admin_id, ts_, payload),
            )
        # users 表插入 admin
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, status, created_at, note) VALUES (?,?,?,?)",
            (admin_id, "active", datetime.utcnow().isoformat(), "admin (migrated)"),
        )
        conn.commit()

    log.info("SQLite 迁移完成。所有旧数据归入 user_id=%d", admin_id)


def migrate_recent_json(root: Path, admin_id: int) -> None:
    p = root / "data" / "recent.json"
    if not p.exists():
        log.info("recent.json 不存在，跳过")
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("recent.json 解析失败：%s（不动它）", e)
        return
    if isinstance(data, dict):
        log.info("recent.json 已是 dict 格式，跳过")
        return
    if isinstance(data, list):
        wrapped = {str(admin_id): data}
        p.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("recent.json 已包装：list (%d msgs) → dict[%d]", len(data), admin_id)


def migrate_memu_postgres(admin_id: int) -> None:
    s = settings()
    if s.memu_metadata_provider != "postgres" or not s.memu_db_url:
        log.info("memU 不是 postgres provider，跳过 --migrate-memu")
        return

    # 用 psycopg 直连，绕过 memU SDK
    try:
        import psycopg  # type: ignore
    except ImportError:
        log.error("缺 psycopg；先 .venv/bin/pip install 'psycopg[binary]'")
        return

    # 把 sqlalchemy URL 转成 psycopg DSN
    dsn = s.memu_db_url.replace("postgresql+psycopg://", "postgresql://")

    new_uid = str(admin_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        # 列出 user_id 列存在的表
        rows = conn.execute("""
            SELECT table_name FROM information_schema.columns
            WHERE column_name='user_id' AND table_schema='public'
        """).fetchall()
        tables = [r[0] for r in rows]
        log.info("memU 含 user_id 列的表：%s", tables)
        for t in tables:
            res = conn.execute(
                f"UPDATE {t} SET user_id=%s WHERE user_id=%s",
                (new_uid, "me"),
            )
            log.info("  %s: %d 行 'me' → '%s'", t, res.rowcount, new_uid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate-memu", action="store_true",
                        help="同时把 memU postgres 的 user_id='me' 改成 str(ADMIN_CHAT_ID)")
    args = parser.parse_args()

    s = settings()
    admin_id = s.admin_chat_id
    if not admin_id:
        log.error("ADMIN_CHAT_ID 未设。在 .env 加 ADMIN_CHAT_ID=<你的 chat_id>")
        sys.exit(1)

    log.info("=== 多用户迁移（admin=%d）===", admin_id)
    migrate_sqlite(s.app_db_path, admin_id)
    migrate_recent_json(s.root, admin_id)
    if args.migrate_memu:
        migrate_memu_postgres(admin_id)
    log.info("=== 完成 ===")


if __name__ == "__main__":
    main()
