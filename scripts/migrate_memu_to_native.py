"""一次性脚本：memU `memory_items` → 自搭 `memories`。

用法：
  .venv/bin/python -m scripts.migrate_memu_to_native           # 干跑（不写数据，只汇报）
  .venv/bin/python -m scripts.migrate_memu_to_native --apply   # 真迁

幂等：第二次跑（`memories` 已有同 id 的行）会被 ON CONFLICT 跳过。
不删旧表（memory_items / memory_categories / category_items / resources）——保留作回滚保险。
迁完后由人手在确认稳定后再 DROP。
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import text

from src import memory_store
from src.config import settings


logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("migrate")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真写入；不带 = dry run")
    args = parser.parse_args()

    s = settings()
    if not s.memu_db_url:
        log.error("MEMU_DB_URL 未配置")
        return 2

    eng = memory_store.engine()  # 顺便建 memories 表 + pgvector

    with eng.connect() as conn:
        # 旧表存在性
        old_exists = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='memory_items'"
            )
        ).scalar()
        if not old_exists:
            log.warning("memory_items 不存在，无需迁移")
            return 0

        old_total = conn.execute(text("SELECT count(*) FROM memory_items")).scalar() or 0
        new_total = conn.execute(text("SELECT count(*) FROM memories")).scalar() or 0

        per_user_old = conn.execute(text(
            "SELECT user_id, count(*) FROM memory_items GROUP BY user_id ORDER BY count(*) DESC"
        )).fetchall()

        log.info("旧 memory_items 共 %d 条；新 memories 现 %d 条", old_total, new_total)
        for uid, n in per_user_old:
            log.info("  uid=%s : %d", uid, n)

        if old_total == 0:
            log.info("旧表空，无需迁移")
            return 0

    # 验证新表 dim 匹配
    with eng.connect() as conn:
        dim_rows = conn.execute(text(
            "SELECT vector_dims(embedding) AS dim, count(*) FROM memory_items "
            "WHERE embedding IS NOT NULL GROUP BY dim"
        )).fetchall()
    log.info("旧表 embedding 维度分布：%s", dim_rows)
    bad = [r for r in dim_rows if r[0] is not None and r[0] != memory_store.EMBED_DIM]
    if bad:
        log.error(
            "旧 embedding 维度（%s）与新 schema (%d) 不匹配——脚本不会自动重算，"
            "请手动决定丢弃 embedding 还是重跑 backfill",
            bad, memory_store.EMBED_DIM,
        )
        if args.apply:
            return 3

    if not args.apply:
        log.info("--apply 没设，dry-run 结束（实际不写）")
        return 0

    # 真迁
    sql = text("""
        INSERT INTO memories (id, user_id, summary, memory_type, embedding,
                              created_at, updated_at, evidence_ref)
        SELECT
            CAST(id AS uuid),
            CAST(user_id AS bigint),
            summary,
            COALESCE(memory_type, 'profile'),
            embedding,
            created_at,
            updated_at,
            NULL
        FROM memory_items
        WHERE user_id ~ '^[0-9]+$'  -- 跳过非数字 user_id（防止脏数据）
        ON CONFLICT (id) DO NOTHING
    """)
    with eng.begin() as conn:
        result = conn.execute(sql)
        log.info("INSERT ... ON CONFLICT 影响 %d 行", result.rowcount)

    # 校验
    with eng.connect() as conn:
        new_total_after = conn.execute(text("SELECT count(*) FROM memories")).scalar() or 0
        per_user_new = conn.execute(text(
            "SELECT user_id, count(*) FROM memories GROUP BY user_id ORDER BY count(*) DESC"
        )).fetchall()
    log.info("新 memories 现 %d 条（迁移前 %d）", new_total_after, new_total)
    for uid, n in per_user_new:
        log.info("  uid=%s : %d", uid, n)

    log.info("✅ 完成。如果一切正常，可在确认观察一段时间后手动 DROP "
             "memory_items / memory_categories / category_items / resources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
