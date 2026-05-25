"""一次性清理重复 insight：按 cosine 聚类后每簇只保最新，其余标 valid_to=now()。

背景（2026-05-25）：auto_dream_insights 之前没把现存 insight 喂回 LLM，
也没做 embedding 写入去重。导致同 pattern 在多天里被反复改写措辞重写
（admin uid 4 天 10 条实际只覆盖 3 个独立 pattern）。

修复部署后只防新增，不动历史。这个脚本清历史。

用法：
  # dry-run（默认）：只打印会标 stale 哪些，不动数据
  .venv/bin/python -m scripts.cleanup_duplicate_insights --uid 8058993786

  # 实跑：dump backup + 标 valid_to
  .venv/bin/python -m scripts.cleanup_duplicate_insights --uid 8058993786 --apply

  # 全 user 一次清
  .venv/bin/python -m scripts.cleanup_duplicate_insights --all-users --apply

  # 自定义阈值（默认 0.85——bge-small-zh 上措辞改写实测 sim 0.92-0.95）
  .venv/bin/python -m scripts.cleanup_duplicate_insights --uid X --threshold 0.88

策略：
- 拉每个 user 的所有 valid insight + embedding
- 单链聚类（任两条 cosine ≥ threshold 视作同簇）
- 每簇保 created_at 最新一条；其余 valid_to=now()
- DELETE 前 dump 全文到 data/insight_cleanup_backup_<uid>_<ts>.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import text as sql_text


def _parse_pgvector(emb) -> list[float]:
    """pgvector 的 embedding 列默认返字符串 '[0.1,0.2,...]'。"""
    if emb is None:
        return []
    if isinstance(emb, list):
        return [float(x) for x in emb]
    s = str(emb)
    nums = re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", s)
    return [float(x) for x in nums]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _cluster(items: list[dict], threshold: float) -> list[list[dict]]:
    """单链聚类：i 和 j 任意两条 cosine ≥ threshold → 同簇。"""
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        vi = items[i]["vec"]
        if not vi:
            continue
        for j in range(i + 1, n):
            vj = items[j]["vec"]
            if not vj:
                continue
            if _cosine(vi, vj) >= threshold:
                union(i, j)

    clusters: dict[int, list[dict]] = {}
    for i, it in enumerate(items):
        clusters.setdefault(find(i), []).append(it)
    return list(clusters.values())


def _process_user(eng, user_id: int, threshold: float, apply: bool, backup_dir: Path) -> dict:
    """对单 user 跑：拉 → 聚类 → 标 stale。返回统计。"""
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT id::text, summary, created_at, embedding "
                "FROM memories "
                "WHERE user_id = :uid AND memory_type = 'insight' "
                "AND status = 'confirmed' "
                "AND (valid_to IS NULL OR valid_to > now()) "
                "ORDER BY created_at ASC"
            ),
            {"uid": user_id},
        ).fetchall()

    if not rows:
        print(f"  uid={user_id}: 无 valid insight，跳过")
        return {"uid": user_id, "total": 0, "kept": 0, "staled": 0}

    items = [
        {
            "id": r[0],
            "summary": r[1],
            "created_at": r[2],
            "vec": _parse_pgvector(r[3]),
        }
        for r in rows
    ]
    clusters = _cluster(items, threshold)

    keep_ids: list[str] = []
    stale_ids: list[str] = []
    for cluster in clusters:
        if len(cluster) == 1:
            keep_ids.append(cluster[0]["id"])
            continue
        cluster.sort(key=lambda x: x["created_at"], reverse=True)
        keep_ids.append(cluster[0]["id"])
        for it in cluster[1:]:
            stale_ids.append(it["id"])

    print(f"\n  uid={user_id}: 总 {len(items)} 条 → 聚成 {len(clusters)} 簇 → "
          f"保留 {len(keep_ids)} 条 / 标 stale {len(stale_ids)} 条")

    multi_clusters = [c for c in clusters if len(c) > 1]
    for ci, cluster in enumerate(multi_clusters):
        cluster_sorted = sorted(cluster, key=lambda x: x["created_at"], reverse=True)
        print(f"\n    簇 #{ci+1}（{len(cluster)} 条 → 留 1）:")
        for k, it in enumerate(cluster_sorted):
            tag = "★ KEEP" if k == 0 else "  stale"
            ts = it["created_at"].strftime("%Y-%m-%d") if it["created_at"] else "?"
            print(f"      {tag}  {ts}  {it['id'][:8]}  {it['summary'][:60]}")

    if not apply or not stale_ids:
        return {
            "uid": user_id,
            "total": len(items),
            "kept": len(keep_ids),
            "staled": len(stale_ids),
            "applied": False,
        }

    # backup → UPDATE
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"insight_cleanup_uid{user_id}.json"
    backup_data = [
        {"id": it["id"], "summary": it["summary"],
         "created_at": it["created_at"].isoformat() if it["created_at"] else None,
         "action": ("keep" if it["id"] in keep_ids else "stale")}
        for it in items
    ]
    backup_path.write_text(
        json.dumps(backup_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n    backup → {backup_path}")

    with eng.begin() as conn:
        result = conn.execute(
            sql_text(
                "UPDATE memories SET valid_to = now(), updated_at = now() "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": stale_ids},
        )
    print(f"    UPDATE: 标 stale {result.rowcount} 条 ✓")

    return {
        "uid": user_id,
        "total": len(items),
        "kept": len(keep_ids),
        "staled": len(stale_ids),
        "applied": True,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", type=int, help="指定 user_id；不传则需 --all-users")
    ap.add_argument("--all-users", action="store_true", help="对所有有 insight 的 user 跑")
    ap.add_argument("--apply", action="store_true",
                    help="实跑（标 stale）；不传则 dry-run")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="cosine 聚类阈值（默认 0.85）")
    args = ap.parse_args()

    if not args.uid and not args.all_users:
        ap.error("必须传 --uid X 或 --all-users")

    from src import memory_store

    eng = memory_store.engine()
    backup_dir = Path("data") / f"insight_cleanup_backup_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"

    print(f"=== insight 重复清理 (threshold={args.threshold}, apply={args.apply}) ===")

    if args.all_users:
        with eng.connect() as conn:
            uids = conn.execute(
                sql_text(
                    "SELECT DISTINCT user_id FROM memories "
                    "WHERE memory_type = 'insight' AND status = 'confirmed' "
                    "AND (valid_to IS NULL OR valid_to > now())"
                )
            ).fetchall()
        target_uids = [int(r[0]) for r in uids]
    else:
        target_uids = [args.uid]

    summary = []
    for uid in target_uids:
        try:
            stat = _process_user(eng, uid, args.threshold, args.apply, backup_dir)
            summary.append(stat)
        except Exception as e:
            print(f"  uid={uid} 失败: {e}")

    print("\n=== 总结 ===")
    for s in summary:
        print(f"  uid={s['uid']}: total={s['total']} kept={s['kept']} staled={s['staled']} applied={s.get('applied', False)}")
    if not args.apply:
        print("\n→ dry-run。要真做加 --apply")


if __name__ == "__main__":
    main()
