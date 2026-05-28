"""自搭记忆栈（替换 memU SDK，2026-05-18 起）。

设计：
- 一张表 `memories`（详见 `src/memory_store.py`）；不再有 categories / resources / category_items
- 抽取走 `src/memory_prompts.py`，用 `MEMU_CHAT_MODEL`（默认 deepseek-v4-flash）输出 JSON
- 召回是简单 pgvector cosine top-k（embedding 由本地 :18080 shim 出）
- multi-user：所有公共函数吃 `user_id: int`，模块级 buffer/flush_ts 是 `dict[str_uid, ...]`

公共 API（**不变**，上层 agent.py / persona.py / scheduler.py 不用改）：
- `recall(user_id, user_text, top_k=3) -> list[str]`
- `note_turn(user_id, user_text, assistant_text)`
- `maybe_flush(user_id=None, force=False) -> bool`
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql_text

from . import embed_client, llm, memory_prompts, memory_store
from .audit_log import audit
from .config import settings

log = logging.getLogger(__name__)

FLUSH_EVERY_N_TURNS = 6
FLUSH_INTERVAL_SEC = 15 * 60  # 或 15 分钟强制一次

# recall 精度门
# P0-1（2026-05-20）：从 6 → 3。hybrid 时代短 query 走 ngram + entity 路也能命中；
# 老的 6 阈值是 cosine-only 时代为了防 cosine 短 query 不稳。
RECALL_MIN_QUERY_CJK_CHARS = 3   # query 中文字符 < 这个数 → 不 recall
RECALL_MAX_DISTANCE = 0.55       # pgvector cosine distance；越小越相似（0=同方向）。> 这个 → 视为不相关

# P0-1 hybrid retrieval（Mem0 v3 借鉴）：三路候选 + RRF 融合
HYBRID_CANDIDATES_PER_PATH = 20  # 每路召回多少候选喂给融合
RRF_K = 60                       # RRF 公式 1/(k+rank)，k=60 是 Cormack et al. 工业默认
NGRAM_WINDOW = 2                 # query 切 2-char window 做 ILIKE 子串匹配（中文友好）
NGRAM_MIN_HITS = 1               # 至少命中 1 个 ngram 才算候选

# P0-2 三因子 ranker（Generative Agents 借鉴，2026-05-21）：在 RRF 之上叠加 importance + recency
# final_score = α·RRF_norm + β·confidence + γ·recency_decay
# 三个分量都 [0,1]；weighted sum 后取 top_k
RANKER_W_RELEVANCE = 1.0         # α：RRF 分数（用 candidate set 内 max 归一化）
RANKER_W_IMPORTANCE = 0.3        # β：confidence（已 [0,1]，stale=0.0 / to_verify=0.5 / confirmed=1.0）
RANKER_W_RECENCY = 0.3           # γ：exp(-Δt / τ)
TAU_PROFILE_DAYS = 180           # profile 半年衰减一半（profile 是长期事实，老一点不算太严重）
TAU_EVENT_DAYS = 14              # event 两周衰减一半（event 时效性强，老 event 不应顶到前面）
# 一些不带语义的口头话术，整句直接命中就跳过（超出长度门时兜底）
_RECALL_STOPWORD_PATTERNS = [
    re.compile(r"^[嗯啊哦哎呀哈呵嘿耶吧呢吗的了"
               r"\s\.,。，！？!?…~]+$"),
    re.compile(r"^(是的|是吧|是啊|对啊|对的|好的|行|确实|没错|可以|嗯嗯|嗯啊|"
               r"哈哈|哈哈哈|哎|哦哦|没事|挺好|不错|牛|牛逼|可怕|绝了|"
               r"我没事|不影响|不知道|不太懂|我也是|确实是|是这样|就这样)$"),
]


def _query_ngrams(text: str) -> list[str]:
    """切 query 成 2-char window 列表，去重 + 去口头禅 + 去过泛 ngram。

    pg_trgm 对中文短词的 similarity 很弱（"上班"和长句的 sim < 0.1），
    但 GIN trigram 索引加速 ILIKE 子串很快——所以走 ngram + ILIKE 路。
    例：'草莓音乐节怎么样' → ['草莓','莓音','音乐','乐节'] （后面的 "节怎"/"怎么"/"么样" 被过滤）。
    """
    s = (text or "").strip()
    if len(s) < NGRAM_WINDOW:
        return []
    # 抠掉常见标点和口头禅字符
    skip_chars = set("，。！？!?…~ \t\n.,的了吗呢吧啊呀哦嗯哎哈呵嘿耶")
    # 过泛 ngram 黑名单——这些 2-char 窗在记忆里出现频次极高、信息量低
    skip_ngrams = {
        "什么", "怎么", "怎样", "如何", "为什", "因为", "所以", "然后", "其实",
        "一下", "一个", "一些", "可以", "可能", "已经", "正在", "应该", "需要",
        "没有", "还是", "或者", "不过", "但是", "不是", "都是", "就是",
        "今天", "明天", "昨天", "现在", "刚才", "最近", "之前", "以后",
        "这个", "那个", "这样", "那样", "哪里", "哪个",
        "我们", "你们", "他们", "用户", "助手",
    }
    seen = set()
    out: list[str] = []
    for i in range(len(s) - NGRAM_WINDOW + 1):
        win = s[i:i + NGRAM_WINDOW]
        if all(c in skip_chars for c in win):
            continue
        if win in skip_ngrams:
            continue
        if win in seen:
            continue
        seen.add(win)
        out.append(win)
    return out[:8]  # 最多 8 个窗，避免 query 过长拖慢


def _is_low_value_query(text: str) -> tuple[bool, str]:
    """A 道门：太短/纯口头禅的 query → 直接跳过 recall。返回 (是否跳, 原因)。"""
    s = (text or "").strip()
    if not s:
        return True, "empty"
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    # 中文 query 字数门
    if cjk and cjk < RECALL_MIN_QUERY_CJK_CHARS:
        return True, f"too_short_cjk={cjk}"
    # 纯英文 query 长度门（按词算）
    if not cjk and len(s.split()) < 3:
        return True, f"too_short_en_words={len(s.split())}"
    # 整句口头禅
    for pat in _RECALL_STOPWORD_PATTERNS:
        if pat.fullmatch(s):
            return True, "stopword_phrase"
    return False, ""

# PRD v2 / 5.2：召回时反验证 to_verify 条目
REVERIFY_COOLDOWN_SEC = 30 * 60  # 同一条 to_verify 30min 内最多反验证一次
REVERIFY_TIMEOUT_SEC = 8.0       # 单条反验证 LLM 调用超时（保护 recall 整体延迟）
REVERIFY_UPSTREAM_LIMIT = 3      # 喂给 LLM 的上游事实最多 3 条

# PRD v2 / 5.3：Auto Dream 后台整理
DREAM_NEIGHBOR_TOP_K = 5     # 每条 to_verify 拿多少邻居 confirmed 条目当上下文
DREAM_BATCH_SLEEP_SEC = 0.5  # 同一用户内每条之间间隔（限速 LLM）
DREAM_TIMEOUT_SEC = 15.0     # 单条 dream LLM 调用超时（后台允许更宽松）

# user_id (str) → buffer of {role, content}
_buffer_per_user: dict[str, list[dict[str, str]]] = {}
_last_flush_ts_per_user: dict[str, float] = {}
_flush_lock = asyncio.Lock()

# fire-and-forget bg task 持引用——event loop 只持 weak ref，不存就可能被 GC
_BG_TASKS: set[asyncio.Task] = set()

# infra 故障告警限速：同类故障 30 min 内只 push admin 一次（avoid 刷屏）
_LAST_INFRA_NOTIFY: dict[str, float] = {}
_INFRA_NOTIFY_COOLDOWN_SEC = 30 * 60


def _maybe_notify_infra_failure(component: str, exc: Exception) -> None:
    """关键 infra 故障（pg 连不上 / embed_server 崩）→ push admin Telegram 告警。

    限速：同 component 30 min 内只发一条，防一连串失败刷屏。
    """
    import time as _t
    now = _t.time()
    last = _LAST_INFRA_NOTIFY.get(component, 0.0)
    if now - last < _INFRA_NOTIFY_COOLDOWN_SEC:
        return
    _LAST_INFRA_NOTIFY[component] = now

    msg_type = type(exc).__name__
    msg_text = str(exc)[:200]
    text = f"⚠️ infra failure: {component}\n{msg_type}: {msg_text}"

    async def _push():
        try:
            from . import bot as _bot
            from .config import settings as _settings
            admin_id = _settings().admin_chat_id
            if not admin_id:
                return
            send, _ = _bot.make_send_and_typing(admin_id)
            await send(text)
        except Exception as e:
            log.debug("infra notify err: %s", e)

    _spawn_bg(_push())


def _spawn_bg(coro) -> None:
    """fire-and-forget 助手：持引用 + done callback 自清理 + 异常 log。"""
    try:
        t = asyncio.create_task(coro)
    except RuntimeError:
        # 没 running loop（同步上下文调用）
        return
    _BG_TASKS.add(t)

    def _on_done(task: asyncio.Task) -> None:
        _BG_TASKS.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("memory bg task failed: %r", exc)

    t.add_done_callback(_on_done)


def _uid(chat_id: int | str) -> str:
    return str(chat_id)


def _buffer_dir() -> Path:
    d = settings().root / "data" / "memu_buffer"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============ 召回 ============

def _fmt_date(dt) -> str:
    """datetime / iso-string → 'YYYY-MM-DD'。"""
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt[:10]
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


async def recall(user_id: int, user_text: str, *, top_k: int = 3) -> list[str]:
    """Hybrid recall（P0-1，Mem0 v3 借鉴 / 2026-05-20）：cosine + ngram + entity 三路 RRF 融合。

    返回 top_k 条记忆，每条带形成日期。失败返回空 list，不阻塞主流程。

    召回流水：
    1. A 道门：query 太短/纯口头禅 → 跳过
    2. cosine 路：embed 后 pgvector 取 top-20，仍带 RECALL_MAX_DISTANCE 上限当噪声底
    3. ngram 路：query 切 2-char window，对 summary 做 ILIKE substring 命中计数（pg_trgm
       GIN 索引加速）。比纯 pg_trgm similarity 对中文短词友好。
    4. entity 路：query 包含 mem.entities 中任一 entity 子串 → 命中数排序 top-20
    5. RRF 融合：score = Σ 1/(RRF_K + rank_in_path)；按 score 取 top_k
    6. 5.2 反验证：到 5 步幸存的 to_verify 条目同步跑 LLM 反验证（>30min 没验过的）

    格式：`(2026-05-15) 用户最近在减肥` 或 `(2026-05-15) [待确认] ...`
    """
    uid_str = _uid(user_id)
    snippets: list[str] = []
    audit_paths: dict[str, Any] = {"cosine": 0, "ngram": 0, "entity": 0}
    # A 道门：query 太短/纯口头禅 → 不浪费 embed call
    skipped, skip_reason = _is_low_value_query(user_text)
    if skipped:
        audit("memory_recall", user_id=user_id, query=user_text[:200],
              hits=0, snippets=[], skipped_reason=skip_reason)
        log.info("recall uid=%s skip: %s (query=%r)", uid_str, skip_reason, user_text[:60])
        return []

    try:
        vec = await embed_client.embed_one(user_text)
        if vec is None:
            log.debug("recall uid=%s: embed 失败，跳过 cosine 路（其他路仍走）", uid_str)
        eng = memory_store.engine()

        # ---- 三路并行候选拉取（一个 connection 内顺序执行就行，候选量都不大）----
        cand_per_path: dict[str, list[dict[str, Any]]] = {}
        with eng.connect() as conn:
            # 路 1：cosine（保留 distance 上限当噪声底）
            cosine_rows = []
            if vec is not None:
                cosine_rows = conn.execute(
                    sql_text(
                        "SELECT id::text AS id, summary, created_at, status, "
                        "last_verified_at, depends_on, entities, memory_type, confidence, "
                        "(embedding <=> CAST(:q AS vector)) AS dist "
                        "FROM memories "
                        "WHERE user_id = :uid AND status != 'stale' "
                        "AND (valid_to IS NULL OR valid_to > now()) "
                        "AND embedding IS NOT NULL "
                        "AND (embedding <=> CAST(:q AS vector)) < :max_dist "
                        "ORDER BY embedding <=> CAST(:q AS vector) "
                        "LIMIT :k"
                    ),
                    {"uid": user_id, "q": embed_client.vec_literal(vec),
                     "k": HYBRID_CANDIDATES_PER_PATH,
                     "max_dist": RECALL_MAX_DISTANCE},
                ).fetchall()
            cand_per_path["cosine"] = [_row_to_item(r, dist=True) for r in cosine_rows]
            audit_paths["cosine"] = len(cosine_rows)

            # 路 2：ngram-ILIKE（中文短词友好；pg_trgm GIN 索引加速 ILIKE '%xxx%'）
            # 把 query 切 2-char window 列表，每个 window 在 summary 里 substring 命中算 1 分
            ngrams = _query_ngrams(user_text)
            if ngrams:
                # 动态拼 OR 子句：每个 ngram 一个 ILIKE，得分 = 命中 ngram 数
                ilike_clauses = [
                    f"(summary ILIKE :ng{i})" for i in range(len(ngrams))
                ]
                hits_expr = " + ".join(
                    f"(CASE WHEN summary ILIKE :ng{i} THEN 1 ELSE 0 END)"
                    for i in range(len(ngrams))
                )
                params: dict[str, Any] = {
                    "uid": user_id,
                    "k": HYBRID_CANDIDATES_PER_PATH,
                    "min_hits": NGRAM_MIN_HITS,
                }
                for i, ng in enumerate(ngrams):
                    params[f"ng{i}"] = f"%{ng}%"
                ngram_rows = conn.execute(
                    sql_text(
                        "SELECT id::text AS id, summary, created_at, status, "
                        "last_verified_at, depends_on, entities, memory_type, confidence, "
                        f"({hits_expr}) AS hits "
                        "FROM memories "
                        "WHERE user_id = :uid AND status != 'stale' "
                        "AND (valid_to IS NULL OR valid_to > now()) "
                        f"AND ({' OR '.join(ilike_clauses)}) "
                        f"AND ({hits_expr}) >= :min_hits "
                        "ORDER BY hits DESC, created_at DESC "
                        "LIMIT :k"
                    ),
                    params,
                ).fetchall()
            else:
                ngram_rows = []
            cand_per_path["ngram"] = [_row_to_item(r) for r in ngram_rows]
            audit_paths["ngram"] = len(ngram_rows)

            # 路 3：entity（query 字面包含 mem.entities 中任一 entity 子串）
            ent_rows = conn.execute(
                sql_text(
                    "SELECT id::text AS id, summary, created_at, status, "
                    "last_verified_at, depends_on, entities, memory_type, confidence, "
                    "(SELECT count(*) FROM unnest(entities) e "
                    "  WHERE :q ILIKE '%' || e || '%') AS hits "
                    "FROM memories "
                    "WHERE user_id = :uid AND status != 'stale' "
                    "AND (valid_to IS NULL OR valid_to > now()) "
                    "AND entities IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM unnest(entities) e "
                    "  WHERE :q ILIKE '%' || e || '%') "
                    "ORDER BY hits DESC, created_at DESC "
                    "LIMIT :k"
                ),
                {"uid": user_id, "q": user_text[:500],
                 "k": HYBRID_CANDIDATES_PER_PATH},
            ).fetchall()
            cand_per_path["entity"] = [_row_to_item(r) for r in ent_rows]
            audit_paths["entity"] = len(ent_rows)

        # ---- RRF 融合（P0-1） ----
        rrf_score: dict[str, float] = {}
        item_by_id: dict[str, dict[str, Any]] = {}
        per_path_rank: dict[str, dict[str, int]] = {}
        for path_name, items_p in cand_per_path.items():
            per_path_rank[path_name] = {}
            for rank, it in enumerate(items_p, start=1):
                rid = it["id"]
                rrf_score[rid] = rrf_score.get(rid, 0.0) + 1.0 / (RRF_K + rank)
                per_path_rank[path_name][rid] = rank
                # 第一次见保留所有字段，后面只更新 distance（cosine 路独有）
                if rid not in item_by_id:
                    item_by_id[rid] = it
                elif it.get("dist") is not None and item_by_id[rid].get("dist") is None:
                    item_by_id[rid]["dist"] = it["dist"]

        # ---- P0-2 三因子加权（Generative Agents 借鉴）----
        # final = α·rel + β·imp + γ·rec
        #  rel = RRF_norm（用 candidate 内 max 归一化到 [0,1]）
        #  imp = confidence（已 [0,1]，stale 此处不会进来已被 SQL 滤）
        #  rec = exp(-Δt / τ)，profile τ=180d、event τ=14d
        now_for_rank = datetime.now(timezone.utc)
        rrf_max = max(rrf_score.values()) if rrf_score else 1.0
        scored: dict[str, dict[str, float]] = {}
        for rid, raw in rrf_score.items():
            it = item_by_id[rid]
            rel = (raw / rrf_max) if rrf_max > 0 else 0.0
            imp = float(it.get("confidence") or 1.0)
            mtype = it.get("memory_type") or "profile"
            tau_days = TAU_EVENT_DAYS if mtype == "event" else TAU_PROFILE_DAYS
            created = it.get("created_at")
            try:
                age_sec = (now_for_rank - created).total_seconds() if created else 0.0
            except TypeError:
                # naive datetime fallback
                age_sec = (datetime.now() - created.replace(tzinfo=None)).total_seconds() if created else 0.0
            age_days = max(0.0, age_sec / 86400.0)
            rec = math.exp(-age_days / max(1.0, tau_days))
            final = (RANKER_W_RELEVANCE * rel
                     + RANKER_W_IMPORTANCE * imp
                     + RANKER_W_RECENCY * rec)
            scored[rid] = {
                "rel": round(rel, 3), "imp": round(imp, 3),
                "rec": round(rec, 3), "final": round(final, 3),
                "raw_rrf": round(raw, 4), "age_days": round(age_days, 1),
                "tau_days": tau_days,
            }

        # 取 top_k（按 final 排）
        ranked_ids = sorted(scored, key=lambda x: scored[x]["final"], reverse=True)[:top_k]
        items = [item_by_id[rid] for rid in ranked_ids]
        # 把分量贴回 item，audit 时方便看
        for rid in ranked_ids:
            item_by_id[rid]["score_components"] = scored[rid]

        # 5.2：找需要反验证的
        now = datetime.now(timezone.utc)
        cooldown_floor = now.timestamp() - REVERIFY_COOLDOWN_SEC
        due: list[dict[str, Any]] = []
        for it in items:
            if it["status"] != "to_verify":
                continue
            lva = it.get("last_verified_at")
            if lva is None or lva.timestamp() < cooldown_floor:
                due.append(it)

        if due:
            results = await asyncio.gather(
                *[_reverify_one(user_id, it, user_text) for it in due],
                return_exceptions=True,
            )
            # 把验证结果写回 items（status 可能升级），UPDATE 数据库
            with eng.begin() as conn:
                for it, res in zip(due, results):
                    if isinstance(res, Exception) or res is None:
                        continue
                    new_status = res
                    if new_status == "still_valid":
                        conn.execute(
                            sql_text(
                                "UPDATE memories SET status = 'confirmed', "
                                "confidence = 1.0, last_verified_at = :ts, "
                                "updated_at = :ts WHERE id = CAST(:id AS uuid)"
                            ),
                            {"ts": now, "id": it["id"]},
                        )
                        it["status"] = "confirmed"
                    else:
                        # uncertain：保持 to_verify，仅打 last_verified_at 限速戳
                        conn.execute(
                            sql_text(
                                "UPDATE memories SET last_verified_at = :ts "
                                "WHERE id = CAST(:id AS uuid)"
                            ),
                            {"ts": now, "id": it["id"]},
                        )

        distances: list[float] = []
        score_breakdown: list[dict[str, Any]] = []
        for it in items:
            date = _fmt_date(it["created_at"])
            marker = "[待确认] " if it["status"] == "to_verify" else ""
            base = f"({date}) {marker}{it['summary']}" if date else f"{marker}{it['summary']}"
            snippets.append(base)
            if it.get("dist") is not None:
                distances.append(round(it["dist"], 3))
            sc = it.get("score_components") or {}
            if sc:
                score_breakdown.append({
                    "id": it["id"][:8],
                    "type": it.get("memory_type"),
                    **{k: sc[k] for k in ("rel", "imp", "rec", "final", "age_days") if k in sc},
                })
    except Exception as e:
        # 关键 infra（postgres / pgvector）故障不该静默——升 warning 并 audit
        # 让 admin 在审计流里能看到"recall 整段崩了"，不会再被 log.debug 吞掉
        log.warning("recall uid=%s 失败 (postgres/pgvector?): %s", uid_str, e)
        audit("memory_recall_error", user_id=user_id, error=str(e)[:300],
              error_type=type(e).__name__)
        _maybe_notify_infra_failure("recall", e)
        distances = []
        score_breakdown = []

    log.info(
        "recall uid=%s query=%r → %d hits (paths: cos=%d ng=%d ent=%d)%s",
        uid_str, user_text[:40], len(snippets),
        audit_paths.get("cosine", 0), audit_paths.get("ngram", 0),
        audit_paths.get("entity", 0),
        ("：" + " | ".join(s[:40] for s in snippets[:3])) if snippets else "",
    )
    audit(
        "memory_recall",
        user_id=user_id,
        query=user_text[:200],
        hits=len(snippets),
        snippets=[s[:200] for s in snippets[:top_k]],
        distances=distances[:top_k],
        max_distance_threshold=RECALL_MAX_DISTANCE,
        candidates_per_path=audit_paths,
        score_breakdown=score_breakdown,
        ranker_weights={"rel": RANKER_W_RELEVANCE, "imp": RANKER_W_IMPORTANCE,
                        "rec": RANKER_W_RECENCY, "tau_profile_d": TAU_PROFILE_DAYS,
                        "tau_event_d": TAU_EVENT_DAYS},
    )
    return snippets[:top_k]


def _row_to_item(row, *, dist: bool = False) -> dict[str, Any]:
    """SQL row → dict。三路 SELECT 列顺序保持一致：id/summary/created_at/status/
    last_verified_at/depends_on/entities/memory_type/confidence/[最后一列各路自己的分数]。"""
    return {
        "id": row[0],
        "summary": row[1],
        "created_at": row[2],
        "status": row[3],
        "last_verified_at": row[4],
        "depends_on": row[5],
        "entities": row[6],
        "memory_type": row[7],
        "confidence": float(row[8]) if row[8] is not None else 1.0,
        "dist": float(row[9]) if dist and row[9] is not None else None,
    }


# ============ 短期 buffer ============

def note_turn(user_id: int, user_text: str, assistant_text: str) -> None:
    """同步追加到该用户的 buffer（不触发 IO）。"""
    uid = _uid(user_id)
    buf = _buffer_per_user.setdefault(uid, [])
    buf.append({"role": "user", "content": user_text})
    buf.append({"role": "assistant", "content": assistant_text})


# ============ Flush（抽取 + 入库）============

async def maybe_flush(user_id: int | None = None, *, force: bool = False) -> bool:
    """按条件 flush。返回是否实际 flush 了至少一个用户。

    user_id=None：遍历所有 buffer 非空的用户（scheduler 周期性扫盘用）。
    """
    if user_id is None:
        any_flushed = False
        for uid in list(_buffer_per_user.keys()):
            try:
                if await _flush_one(uid, force=force):
                    any_flushed = True
            except Exception as e:
                log.debug("flush %s err: %s", uid, e)
        return any_flushed
    return await _flush_one(_uid(user_id), force=force)


async def _flush_one(uid: str, *, force: bool = False) -> bool:
    now = time.time()
    buf = _buffer_per_user.get(uid)
    if not buf:
        return False
    last_ts = _last_flush_ts_per_user.get(uid, 0.0)
    turns = len(buf) // 2
    should = force or turns >= FLUSH_EVERY_N_TURNS or (
        turns > 0 and now - last_ts > FLUSH_INTERVAL_SEC
    )
    if not should:
        return False

    prev_flush_ts = last_ts  # 给 episode.started_at 用——锁内会被覆盖

    async with _flush_lock:
        buf = _buffer_per_user.get(uid)
        if not buf:
            return False
        batch = buf.copy()
        _buffer_per_user[uid] = []
        _last_flush_ts_per_user[uid] = now

    user_id_int = int(uid) if uid.lstrip("-").isdigit() else 0
    audit_uid: Any = user_id_int or uid

    # 落盘 batch（调试 + 失败回放用）
    path = _buffer_dir() / f"conv_{uid}_{int(now)}.json"
    try:
        path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("flush write conv json err: %s", e)

    # P0-4：落 episode（不论后续抽取成功与否，都先记上）。失败不影响主流程。
    # started_at = 上次 flush 完成时间（从那以后的对话都在这个 episode 里）；
    # 首次 flush 时 prev_flush_ts=0，退化为用 now-1h 占位（真实开始时间无法回溯）。
    episode_id: str | None = None
    if user_id_int:
        try:
            ended_dt = datetime.now(timezone.utc)
            if prev_flush_ts > 0:
                started_dt = datetime.fromtimestamp(prev_flush_ts, tz=timezone.utc)
            else:
                started_dt = ended_dt - timedelta(hours=1)
            episode_id = memory_store.add_episode(
                user_id_int, batch, started_at=started_dt, ended_at=ended_dt,
            )
        except Exception as e:
            log.debug("flush write episode err uid=%s: %s", uid, e)

    # 抽取
    try:
        items = await _extract_items(batch, user_id=user_id_int or None)
    except Exception as e:
        log.exception("extract failed uid=%s: %s", uid, e)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              error=f"extract:{type(e).__name__}:{str(e)[:200]}", episode_id=episode_id)
        # 回滚到 buffer 头部
        async with _flush_lock:
            _buffer_per_user.setdefault(uid, [])[:0] = batch
        return False

    if not items:
        log.info("memorize uid=%s ok (%d msgs) -> %s, +0 items（LLM 没抽到）",
                 uid, len(batch), path.name)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              new_items=0, new_item_summaries=[], episode_id=episode_id)
        # 仍然 fire persona update（用户也许聊了内容只是没产生 profile/event）
        _fire_persona_update(user_id_int, batch)
        # 偏好不一定产生 profile/event（"叫我名字"是 prompt 调整不是事实），这里也要 fire
        _fire_feedback_check(user_id_int, batch)
        return True

    # 入库
    try:
        new_records = await _persist_items(
            user_id_int, items, evidence_ref=str(path), source_episode_id=episode_id,
        )
    except Exception as e:
        log.exception("persist failed uid=%s: %s", uid, e)
        audit("memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
              error=f"persist:{type(e).__name__}:{str(e)[:200]}")
        return False

    new_summaries = [r["summary"] for r in new_records]
    log.info("memorize uid=%s ok (%d msgs) -> %s, +%d items",
             uid, len(batch), path.name, len(new_summaries))
    audit(
        "memory_flush", user_id=audit_uid, msgs=len(batch), file=path.name,
        new_items=len(new_summaries),
        new_item_summaries=[s[:200] for s in new_summaries[:10]],
        episode_id=episode_id,
    )

    _fire_persona_update(user_id_int, batch)
    _fire_conflict_check(user_id_int, new_records)
    _fire_feedback_check(user_id_int, batch)
    return True


def _fire_persona_update(user_id: int, batch: list[dict[str, str]]) -> None:
    """memorize 成功后异步 fire persona 增量更新。失败静默不阻塞主链路。"""
    if not user_id:
        return

    async def _go():
        try:
            from . import persona  # 延迟避免循环 import
            await persona.update_state(user_id, batch)
        except Exception as e:
            log.debug("persona update post-flush err: %s", e)

    _spawn_bg(_go())


def _fire_feedback_check(user_id: int, batch: list[dict[str, str]]) -> None:
    """flush 后异步 fire feedback sub-agent。粗筛 → sonnet 精判 → 落库 prompt_overrides / skill。"""
    if not user_id or not batch:
        return

    async def _go():
        try:
            from . import feedback_agent  # 延迟避免循环 import
            await feedback_agent.process(user_id, batch)
        except Exception as e:
            log.debug("feedback agent post-flush err: %s", e)

    _spawn_bg(_go())


def _fire_conflict_check(user_id: int, new_records: list[dict[str, Any]]) -> None:
    """异步对每条新事实跑影响分析，把可能失效的旧 profile 标 to_verify / stale。

    PRD v2 / 5.1：写入时筛 to_verify。

    新事实和旧事实的关系组合：
    - 新 profile / 新 event 都可能让旧 profile 失效（"我搬上海了" event → "住北京" profile stale）
    - 旧 event 不会变 stale（历史时点不可被未来事件改写），所以候选 SQL 限定 profile
    - 同 batch 同伴排除：4 条事实同一段对话里同时抽出的，本就 mutually consistent，
      不应互判 to_verify（避免 LLM 把同时陈述的两个独立事实硬扯成依赖）

    失败静默不阻塞 flush 主链路。
    """
    if not user_id or not new_records:
        return
    batch_ids = [r["id"] for r in new_records if r.get("id")]

    async def _go():
        try:
            for new in new_records:
                await _check_conflicts_for_one(user_id, new, exclude_ids=batch_ids)
        except Exception as e:
            log.debug("conflict check post-flush err: %s", e)

    _spawn_bg(_go())


CONFLICT_TOPK = 5  # 每条新 profile 召回多少旧 profile 候选做判断


async def _check_conflicts_for_one(
    user_id: int,
    new: dict[str, Any],
    *,
    exclude_ids: list[str] | None = None,
) -> None:
    """对一条新事实：召回 top-N 旧 profile → LLM 判 verdicts → UPDATE 旧条目 status。

    候选限定 profile（event 不变 stale），且排除 exclude_ids（同 batch 同伴）。
    """
    new_id = new.get("id")
    new_summary = new.get("summary") or ""
    vec = new.get("embedding")
    if not new_id or not new_summary or vec is None:
        return

    exclude = list(exclude_ids or [])
    if new_id not in exclude:
        exclude.append(new_id)

    eng = memory_store.engine()
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT id::text AS id, summary FROM memories "
                "WHERE user_id = :uid AND memory_type = 'profile' "
                "AND status != 'stale' "
                "AND NOT (id::text = ANY(:excl)) "
                "ORDER BY embedding <=> CAST(:q AS vector) "
                "LIMIT :k"
            ),
            {"uid": user_id, "excl": exclude,
             "q": embed_client.vec_literal(vec), "k": CONFLICT_TOPK},
        ).fetchall()
    candidates = [(r[0], r[1]) for r in rows]
    if not candidates:
        return

    # LLM 判
    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        return
    prompt = memory_prompts.render_conflict_check(new_summary, candidates, user_id=user_id)
    try:
        from . import openrouter
        res = await openrouter.chat(
            [{"role": "user", "content": prompt}],
            model=model, temperature=0.1, max_tokens=2048,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
    except Exception as e:
        log.warning("conflict check LLM err uid=%s new=%s: %s", user_id, new_id[:8], e)
        return

    verdicts = _parse_verdicts(raw, valid_ids={c[0] for c in candidates})
    if not verdicts:
        if raw:
            log.warning("conflict check 解析 0 verdicts uid=%s raw=%r", user_id, raw[:200])
        return

    # 真正更新
    flips: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc)
    with eng.begin() as conn:
        for old_id, verdict in verdicts.items():
            if verdict == "still_valid":
                # 标 last_verified_at（5.2/5.3 才会读，但便宜就一并写了）
                conn.execute(
                    sql_text(
                        "UPDATE memories SET last_verified_at = :ts "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": old_id},
                )
            elif verdict in ("to_verify", "stale"):
                new_conf = 0.0 if verdict == "stale" else 0.5
                # P1-5：判 stale 时同步写 valid_to=now（保留 status='stale' 双层语义；
                # to_verify 不动 valid_to——它仍可能仍生效）
                # 把触发该变化的新事实 id 追加进 old.depends_on（去重，COALESCE 处理 NULL）
                conn.execute(
                    sql_text(
                        "UPDATE memories SET status = :s, confidence = :c, "
                        "updated_at = :ts, "
                        "valid_to = CASE WHEN :s = 'stale' THEN :ts ELSE valid_to END, "
                        "depends_on = (SELECT ARRAY(SELECT DISTINCT unnest("
                        "  COALESCE(depends_on, ARRAY[]::uuid[]) || ARRAY[CAST(:dep AS uuid)]"
                        ")) FROM memories WHERE id = CAST(:id AS uuid)) "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"s": verdict, "c": new_conf, "ts": now,
                     "id": old_id, "dep": new_id},
                )
                flips.append((old_id, verdict))

    if flips:
        log.info(
            "conflict check uid=%s new=%s 触发 %d 个旧条目变更：%s",
            user_id, new_id[:8], len(flips),
            " | ".join(f"{oid[:8]}→{v}" for oid, v in flips[:5]),
        )
    cand_map = {oid: s for oid, s in candidates}
    audit(
        "memory_conflict_check",
        user_id=user_id,
        new_id=new_id,
        new_summary=new_summary[:200],
        candidates=len(candidates),
        candidate_list=[{"id": oid, "summary": s[:200]} for oid, s in candidates],
        flips=[
            {"id": oid, "verdict": v, "summary": cand_map.get(oid, "")[:200]}
            for oid, v in flips
        ],
    )


_VERDICT_RE = re.compile(
    r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"verdict"\s*:\s*"(still_valid|to_verify|stale)"\s*\}',
    re.DOTALL,
)

_REVERIFY_RE = re.compile(
    r'"verdict"\s*:\s*"(still_valid|uncertain)"',
    re.DOTALL,
)


async def _reverify_one(
    user_id: int, item: dict[str, Any], query: str,
) -> str | None:
    """对一条 to_verify 跑 LLM 反验证。返回 'still_valid' / 'uncertain' / None（失败）。

    PRD v2 / 5.2：用 deps 上游 + 当前 query 喂 LLM。LLM 不能返 stale（保守约束在 prompt
    里说明），调用方据此决定 UPDATE 行为。
    """
    fact = item.get("summary") or ""
    if not fact:
        return None

    # 拉上游事实 summary（按 created_at desc，最近的 N 条）
    upstream: list[str] = []
    deps = item.get("depends_on") or []
    if deps:
        eng = memory_store.engine()
        try:
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary FROM memories "
                        "WHERE id = ANY(:ids) "
                        "ORDER BY created_at DESC LIMIT :n"
                    ),
                    {"ids": deps, "n": REVERIFY_UPSTREAM_LIMIT},
                ).fetchall()
            upstream = [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug("reverify pull upstream err uid=%s id=%s: %s",
                      user_id, item["id"][:8], e)

    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        return None
    prompt = memory_prompts.render_reverify(fact, upstream, query, user_id=user_id)

    started = time.time()
    try:
        from . import openrouter
        res = await asyncio.wait_for(
            openrouter.chat(
                [{"role": "user", "content": prompt}],
                model=model, temperature=0.1, max_tokens=512,
            ),
            timeout=REVERIFY_TIMEOUT_SEC,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
    except asyncio.TimeoutError:
        log.warning("reverify timeout uid=%s id=%s", user_id, item["id"][:8])
        audit("memory_reverify", user_id=user_id, fact_id=item["id"],
              fact=fact[:200], verdict="timeout",
              latency_ms=int((time.time() - started) * 1000))
        return None
    except Exception as e:
        log.warning("reverify LLM err uid=%s id=%s: %s",
                    user_id, item["id"][:8], e)
        return None

    verdict, reason = _parse_reverify(raw)
    latency_ms = int((time.time() - started) * 1000)
    audit(
        "memory_reverify",
        user_id=user_id,
        fact_id=item["id"],
        fact=fact[:200],
        upstream=[u[:200] for u in upstream],
        query=(query or "")[:200],
        verdict=verdict or "parse_fail",
        reason=reason[:300],
        latency_ms=latency_ms,
    )
    if verdict is None:
        log.warning("reverify 解析失败 uid=%s id=%s raw=%r",
                    user_id, item["id"][:8], raw[:160])
    else:
        log.info("reverify uid=%s id=%s → %s (%dms)",
                 user_id, item["id"][:8], verdict, latency_ms)
    return verdict


def _parse_reverify(raw: str) -> tuple[str | None, str]:
    """返回 (verdict, reason)。verdict ∈ {still_valid, uncertain, None}。"""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    for src in (raw, None):
        if src is None:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            src = m.group(0) if m else None
            if src is None:
                break
        try:
            d = json.loads(src)
            if isinstance(d, dict):
                v = d.get("verdict")
                r = (d.get("reason") or "").strip()
                if v in ("still_valid", "uncertain"):
                    return v, r
        except Exception:
            continue
    m = _REVERIFY_RE.search(raw)
    if m:
        return m.group(1), ""
    return None, ""


# ============ Auto Dream（PRD v2 / 5.3）============

_DREAM_RE = re.compile(
    r'"verdict"\s*:\s*"(still_valid|uncertain|stale)"',
    re.DOTALL,
)


def _parse_dream(raw: str) -> tuple[str | None, str]:
    """LLM 输出 → (verdict, reason)。verdict ∈ {still_valid, uncertain, stale, None}。"""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    for src in (raw, None):
        if src is None:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            src = m.group(0) if m else None
            if src is None:
                break
        try:
            d = json.loads(src)
            if isinstance(d, dict):
                v = d.get("verdict")
                r = (d.get("reason") or "").strip()
                if v in ("still_valid", "uncertain", "stale"):
                    return v, r
        except Exception:
            continue
    m = _DREAM_RE.search(raw)
    if m:
        return m.group(1), ""
    return None, ""


async def _dream_one(
    user_id: int, item: dict[str, Any],
) -> str | None:
    """对一条 to_verify 跑 dream 三态判定。返回 verdict 或 None（失败）。

    与 _reverify_one 区别：
    - 用 embedding 召回 top-K 同 user **confirmed** 条目作为上下文（不是 query）
    - LLM 可输出 stale（5.3 是后台批处理，可激进）
    - 超时窗口更宽松（15s vs 8s）
    """
    fact_id = item.get("id")
    fact = item.get("summary") or ""
    vec = item.get("embedding")
    if not fact_id or not fact:
        return None

    eng = memory_store.engine()
    upstream: list[str] = []
    deps = item.get("depends_on") or []
    if deps:
        try:
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary FROM memories WHERE id = ANY(:ids) "
                        "ORDER BY created_at DESC LIMIT :n"
                    ),
                    {"ids": deps, "n": REVERIFY_UPSTREAM_LIMIT},
                ).fetchall()
            upstream = [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug("dream upstream pull err id=%s: %s", fact_id[:8], e)

    # 召回邻居（同 user 同 type 的 confirmed 条目，不含自身）
    neighbors: list[str] = []
    if vec is not None:
        try:
            with eng.connect() as conn:
                rows = conn.execute(
                    sql_text(
                        "SELECT summary FROM memories "
                        "WHERE user_id = :uid AND status = 'confirmed' "
                        "AND id != CAST(:fid AS uuid) "
                        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                    ),
                    {"uid": user_id, "fid": fact_id,
                     "q": embed_client.vec_literal(vec),
                     "k": DREAM_NEIGHBOR_TOP_K},
                ).fetchall()
            neighbors = [r[0] for r in rows if r[0]]
        except Exception as e:
            log.debug("dream neighbor pull err id=%s: %s", fact_id[:8], e)

    s = settings()
    # 5.3 dream 批量整理走 reflection tier（opus）——判断质量直接影响 to_verify 是否被
    # 错杀成 stale 或保留。reflection 没设时 fallback 到 memu_chat_model / main。
    model = s.openrouter_model_reflection or s.memu_chat_model or s.openrouter_model
    if not model:
        return None
    prompt = memory_prompts.render_dream(fact, upstream, neighbors, user_id=user_id)

    started = time.time()
    try:
        from . import openrouter
        res = await asyncio.wait_for(
            openrouter.chat(
                [{"role": "user", "content": prompt}],
                model=model, temperature=0.1, max_tokens=512,
            ),
            timeout=DREAM_TIMEOUT_SEC,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
    except asyncio.TimeoutError:
        log.warning("dream timeout uid=%s id=%s", user_id, fact_id[:8])
        audit("memory_dream_one", user_id=user_id, fact_id=fact_id,
              fact=fact[:200], verdict="timeout",
              latency_ms=int((time.time() - started) * 1000))
        return None
    except Exception as e:
        log.warning("dream LLM err uid=%s id=%s: %s", user_id, fact_id[:8], e)
        return None

    verdict, reason = _parse_dream(raw)
    latency_ms = int((time.time() - started) * 1000)
    audit(
        "memory_dream_one",
        user_id=user_id,
        fact_id=fact_id,
        fact=fact[:200],
        upstream=[u[:200] for u in upstream],
        neighbors=[n[:200] for n in neighbors],
        verdict=verdict or "parse_fail",
        reason=reason[:300],
        latency_ms=latency_ms,
    )
    if verdict is None:
        log.warning("dream 解析失败 uid=%s id=%s raw=%r",
                    user_id, fact_id[:8], raw[:160])
    else:
        log.info("dream uid=%s id=%s → %s (%dms)",
                 user_id, fact_id[:8], verdict, latency_ms)
    return verdict


async def auto_dream(user_id: int) -> dict[str, Any]:
    """对该用户所有 to_verify 条目跑一遍三态 dream 判定。

    PRD v2 / 5.3：每天一次（搭便车 03:13 cron）。
    - still_valid → 升 confirmed (conf=1.0, last_verified_at=now)
    - stale → 降 stale (conf=0.0)
    - uncertain → 仅 last_verified_at=now（不动 status）

    返回汇总 dict 给 audit。
    """
    eng = memory_store.engine()
    started = time.time()
    with eng.connect() as conn:
        rows = conn.execute(
            sql_text(
                "SELECT id::text AS id, summary, embedding::text AS embedding, "
                "depends_on FROM memories "
                "WHERE user_id = :uid AND status = 'to_verify' "
                "ORDER BY created_at"
            ),
            {"uid": user_id},
        ).fetchall()
    items = [
        {"id": r[0], "summary": r[1], "embedding": r[2], "depends_on": r[3]}
        for r in rows
    ]
    if not items:
        log.info("auto_dream uid=%s: 0 条 to_verify，跳过", user_id)
        return {"reviewed": 0, "to_confirmed": 0, "to_stale": 0,
                "uncertain": 0, "errors": 0}

    log.info("auto_dream uid=%s: %d 条 to_verify 待整理", user_id, len(items))
    counts = {"reviewed": 0, "to_confirmed": 0, "to_stale": 0,
              "uncertain": 0, "errors": 0}

    now = datetime.now(timezone.utc)
    for it in items:
        try:
            verdict = await _dream_one(user_id, it)
        except Exception as e:
            log.warning("dream err uid=%s id=%s: %s", user_id, it["id"][:8], e)
            counts["errors"] += 1
            await asyncio.sleep(DREAM_BATCH_SLEEP_SEC)
            continue
        counts["reviewed"] += 1
        if verdict is None:
            counts["errors"] += 1
        elif verdict == "still_valid":
            with eng.begin() as conn:
                conn.execute(
                    sql_text(
                        "UPDATE memories SET status = 'confirmed', "
                        "confidence = 1.0, last_verified_at = :ts, "
                        "updated_at = :ts WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": it["id"]},
                )
            counts["to_confirmed"] += 1
        elif verdict == "stale":
            with eng.begin() as conn:
                conn.execute(
                    sql_text(
                        # P1-5：valid_to=now 保留时间维度
                        "UPDATE memories SET status = 'stale', "
                        "confidence = 0.0, last_verified_at = :ts, "
                        "valid_to = :ts, updated_at = :ts "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": it["id"]},
                )
            counts["to_stale"] += 1
        else:  # uncertain
            with eng.begin() as conn:
                conn.execute(
                    sql_text(
                        "UPDATE memories SET last_verified_at = :ts "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"ts": now, "id": it["id"]},
                )
            counts["uncertain"] += 1
        await asyncio.sleep(DREAM_BATCH_SLEEP_SEC)

    elapsed_ms = int((time.time() - started) * 1000)
    summary = {**counts, "latency_ms": elapsed_ms}
    log.info("auto_dream uid=%s 完成：%s", user_id, summary)
    audit("memory_dream", user_id=user_id, **summary)
    return summary


# ============ Auto Dream insight（P1-6，Generative Agents reflection 借鉴）============

INSIGHT_DREAM_TIMEOUT_SEC = 30.0
INSIGHT_SAMPLE_DAYS = 90      # 抽样窗：最近 N 天
INSIGHT_SAMPLE_PROFILE = 8    # 每次喂 LLM 多少条 profile
INSIGHT_SAMPLE_EVENT = 12     # 每次喂 LLM 多少条 event
INSIGHT_DEFAULT_CONFIDENCE = 0.8  # insight 默认 confidence——比原始 profile (1.0) 稍低
                                  # 三因子 ranker 的 imp 分量也会稍低，避免 insight 压制底层事实
INSIGHT_MAX_PER_RUN = 3       # 每次 dream 最多生成 N 条（prompt 也会要求）

# 去重：新 insight 与"现存 insight"做 cosine 相似度——超阈值就丢
# 0.85 是基于 bge-small-zh：实测 "用户晚饭凑合模式" 系列措辞改写互相 sim ≈ 0.92-0.95，
# 跨 pattern 的 sim ≈ 0.55-0.70。0.85 能拦掉重写、不会误伤合法新 insight
INSIGHT_DEDUP_COSINE_THRESHOLD = 0.85
INSIGHT_DEDUP_LOOKBACK_DAYS = 30   # 跟最近 N 天的 insight 做对比——更老的认为已淘汰
INSIGHT_EXISTING_LIMIT = 30        # 喂给 prompt 的 existing 上限


async def auto_dream_insights(user_id: int) -> dict[str, Any]:
    """对该用户最近事实做跨条目反思，生成 0-3 条 memory_type='insight'。

    流水：
    1. 拉最近 INSIGHT_SAMPLE_DAYS 天 confirmed memory 抽样（profile + event 混合）
    2. 调 main tier sonnet 写 insight + supporting_ids
    3. 每条 insight 算 embedding → INSERT memories(memory_type='insight', confidence=0.8,
       depends_on=supporting_ids, valid_from=now)
    4. audit memory_dream_insight
    """
    eng = memory_store.engine()
    started = time.time()

    with eng.connect() as conn:
        # 用 ORDER BY 三因子 proxy（recency + confidence + memory_type 优先）抽样
        rows = conn.execute(
            sql_text(
                "(SELECT id::text, memory_type, summary, created_at FROM memories "
                " WHERE user_id = :uid AND status = 'confirmed' "
                " AND memory_type = 'profile' "
                " AND created_at > now() - (:days || ' days')::interval "
                " AND (valid_to IS NULL OR valid_to > now()) "
                " ORDER BY created_at DESC LIMIT :prof_n) "
                "UNION ALL "
                "(SELECT id::text, memory_type, summary, created_at FROM memories "
                " WHERE user_id = :uid AND status = 'confirmed' "
                " AND memory_type = 'event' "
                " AND created_at > now() - (:days || ' days')::interval "
                " AND (valid_to IS NULL OR valid_to > now()) "
                " ORDER BY created_at DESC LIMIT :event_n) "
                "ORDER BY created_at DESC"
            ),
            {"uid": user_id, "days": INSIGHT_SAMPLE_DAYS,
             "prof_n": INSIGHT_SAMPLE_PROFILE, "event_n": INSIGHT_SAMPLE_EVENT},
        ).fetchall()

    items = [
        {"id": r[0], "memory_type": r[1], "summary": r[2], "created_at": r[3]}
        for r in rows
    ]
    if len(items) < 4:
        log.info("auto_dream_insights uid=%s: 只有 %d 条样本，跳过", user_id, len(items))
        return {"generated": 0, "samples": len(items), "skipped": "too_few_samples"}

    # 拉最近 INSIGHT_DEDUP_LOOKBACK_DAYS 天的 existing insight——既喂给 prompt 让 LLM
    # 知道写过啥，又作为下面 cosine 去重的对比基线
    with eng.connect() as conn:
        existing_rows = conn.execute(
            sql_text(
                "SELECT id::text, summary, created_at, embedding "
                "FROM memories "
                "WHERE user_id = :uid AND memory_type = 'insight' "
                "AND status = 'confirmed' "
                "AND (valid_to IS NULL OR valid_to > now()) "
                "AND created_at > now() - (:days || ' days')::interval "
                "ORDER BY created_at DESC LIMIT :lim"
            ),
            {"uid": user_id, "days": INSIGHT_DEDUP_LOOKBACK_DAYS,
             "lim": INSIGHT_EXISTING_LIMIT},
        ).fetchall()
    existing_for_prompt = [
        {"summary": r[1], "created_at": r[2]} for r in existing_rows
    ]

    prompt = memory_prompts.render_insight_dream(items, existing=existing_for_prompt, user_id=user_id)
    try:
        data = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="reflection",  # opus：跨条事实反思，判断质量优先
                max_tokens=2000,
            ),
            timeout=INSIGHT_DREAM_TIMEOUT_SEC,
        )
    except Exception as e:
        log.warning("auto_dream_insights uid=%s LLM err: %s", user_id, e)
        elapsed_ms = int((time.time() - started) * 1000)
        audit("memory_dream_insight", user_id=user_id, generated=0,
              samples=len(items), error=f"{type(e).__name__}:{str(e)[:200]}",
              latency_ms=elapsed_ms)
        return {"generated": 0, "samples": len(items), "error": str(e)}

    insights_raw = data.get("insights") if isinstance(data, dict) else None
    if not isinstance(insights_raw, list) or not insights_raw:
        elapsed_ms = int((time.time() - started) * 1000)
        log.info("auto_dream_insights uid=%s: 无新 insight 生成", user_id)
        audit("memory_dream_insight", user_id=user_id, generated=0,
              samples=len(items), latency_ms=elapsed_ms)
        return {"generated": 0, "samples": len(items)}

    # 把 supporting_ids 的短 id（前 8 位）解析回完整 UUID（用 items 里的 id 字典查找）
    id_lookup = {it["id"][:8]: it["id"] for it in items}

    valid_insights = []
    for ins in insights_raw[:INSIGHT_MAX_PER_RUN]:
        if not isinstance(ins, dict):
            continue
        summary_text = (ins.get("summary") or "").strip()
        if not summary_text or len(summary_text) > 200:
            continue
        sup_short = ins.get("supporting_ids") or []
        if not isinstance(sup_short, list) or len(sup_short) < 2:
            continue
        sup_full = [id_lookup[s] for s in sup_short if isinstance(s, str) and s in id_lookup]
        if len(sup_full) < 2:
            continue
        valid_insights.append({"summary": summary_text, "supporting_ids": sup_full})

    if not valid_insights:
        elapsed_ms = int((time.time() - started) * 1000)
        log.info("auto_dream_insights uid=%s: LLM 输出 %d 条但都没通过校验",
                 user_id, len(insights_raw))
        audit("memory_dream_insight", user_id=user_id, generated=0,
              samples=len(items), raw_count=len(insights_raw),
              latency_ms=elapsed_ms)
        return {"generated": 0, "samples": len(items), "raw_count": len(insights_raw)}

    # embedding + 去重 + INSERT
    summaries = [v["summary"] for v in valid_insights]
    vecs = await embed_client.embed_many(summaries)
    now = datetime.now(timezone.utc)

    # 解析 existing insight 的 embedding（postgres pgvector 返字符串 "[0.1,0.2,...]"）
    # 没 embedding（极少数旧条目）就 skip——只能靠 prompt 拦它了
    import re as _re
    existing_vecs: list[tuple[str, list[float]]] = []  # [(summary, vec), ...]
    for r in existing_rows:
        emb_str = r[3]
        if emb_str is None:
            continue
        try:
            if isinstance(emb_str, str):
                # "[0.1,0.2,...]" → list[float]
                nums = _re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", emb_str)
                ev = [float(x) for x in nums]
            else:
                ev = list(emb_str)
            if ev:
                existing_vecs.append((r[1], ev))
        except Exception:
            continue

    def _cosine(a: list[float], b: list[float]) -> float:
        import math
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)

    accepted: list[tuple[dict, list[float] | None]] = []
    duplicates: list[dict] = []
    for v, vec in zip(valid_insights, vecs):
        if vec and existing_vecs:
            best_sim = 0.0
            best_match = ""
            for ex_sum, ex_vec in existing_vecs:
                sim = _cosine(vec, ex_vec)
                if sim > best_sim:
                    best_sim = sim
                    best_match = ex_sum
            if best_sim >= INSIGHT_DEDUP_COSINE_THRESHOLD:
                duplicates.append({
                    "rejected_summary": v["summary"][:120],
                    "matched_existing": best_match[:120],
                    "cosine": round(best_sim, 3),
                })
                continue
        # 同 batch 内也对比——同 turn LLM 输出两条相似的也只留一条
        is_dup_in_batch = False
        for accepted_v, accepted_vec in accepted:
            if vec and accepted_vec and _cosine(vec, accepted_vec) >= INSIGHT_DEDUP_COSINE_THRESHOLD:
                duplicates.append({
                    "rejected_summary": v["summary"][:120],
                    "matched_existing": accepted_v["summary"][:120],
                    "cosine": round(_cosine(vec, accepted_vec), 3),
                    "intra_batch": True,
                })
                is_dup_in_batch = True
                break
        if is_dup_in_batch:
            continue
        accepted.append((v, vec))

    if duplicates:
        log.info("auto_dream_insights uid=%s: 拦下 %d 条重复 insight (cosine ≥ %.2f)",
                 user_id, len(duplicates), INSIGHT_DEDUP_COSINE_THRESHOLD)

    if not accepted:
        elapsed_ms = int((time.time() - started) * 1000)
        log.info("auto_dream_insights uid=%s: 全部 %d 条都跟现存重复，0 条入库",
                 user_id, len(valid_insights))
        audit("memory_dream_insight", user_id=user_id, generated=0,
              samples=len(items), raw_count=len(insights_raw),
              dedup_rejected=len(duplicates), duplicates=duplicates,
              latency_ms=elapsed_ms)
        return {"generated": 0, "samples": len(items), "dedup_rejected": len(duplicates)}

    with eng.begin() as conn:
        for v, vec in accepted:
            new_id = str(_uuid.uuid4())
            params = {
                "id": new_id,
                "user_id": user_id,
                "summary": v["summary"],
                "now": now,
                "depends_on": v["supporting_ids"],
                "conf": INSIGHT_DEFAULT_CONFIDENCE,
            }
            if vec is not None:
                params["embedding"] = embed_client.vec_literal(vec)
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, embedding, "
                        "created_at, updated_at, status, confidence, valid_from, depends_on) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, 'insight', "
                        "CAST(:embedding AS vector), :now, :now, 'confirmed', :conf, :now, "
                        "CAST(:depends_on AS uuid[]))"
                    ),
                    params,
                )
            else:
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, "
                        "created_at, updated_at, status, confidence, valid_from, depends_on) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, 'insight', "
                        ":now, :now, 'confirmed', :conf, :now, "
                        "CAST(:depends_on AS uuid[]))"
                    ),
                    params,
                )

    elapsed_ms = int((time.time() - started) * 1000)
    log.info("auto_dream_insights uid=%s 生成 %d 条 insight (samples=%d, dedup=%d, latency=%dms)",
             user_id, len(accepted), len(items), len(duplicates), elapsed_ms)
    audit("memory_dream_insight", user_id=user_id,
          generated=len(accepted), samples=len(items),
          dedup_rejected=len(duplicates), duplicates=duplicates,
          insights=[{"summary": v["summary"][:120],
                     "supporting": [s[:8] for s in v["supporting_ids"]]}
                    for v, _ in accepted],
          latency_ms=elapsed_ms)
    return {"generated": len(accepted), "samples": len(items),
            "dedup_rejected": len(duplicates),
            "latency_ms": elapsed_ms}


# ============ Auto Dream override（PRD 5.3 扩展，整理 prompt_overrides）============

OVERRIDE_DREAM_TIMEOUT_SEC = 30.0
# 仅当 active overrides 至少有这么多条才跑（少于 2 条无可冲突）
OVERRIDE_DREAM_MIN_COUNT = 2


async def auto_dream_overrides(user_id: int) -> dict[str, Any]:
    """整理该 user 的 active prompt_overrides——合并冗余 / 删除矛盾或过期。

    搭便车 auto_dream 同 cron（03:13 CST）。每个 user 跑一次 sonnet：
    - 拉所有 active overrides
    - 输出 {merge_groups, disable_ids}
    - 应用：INSERT 合并条目（status='active' approved_by=0）+ disable 老 ids
    """
    from . import storage as _storage
    started = time.time()
    overrides = _storage.list_active_overrides(user_id)
    if len(overrides) < OVERRIDE_DREAM_MIN_COUNT:
        log.info("override_dream uid=%s: 仅 %d 条 active，跳过", user_id, len(overrides))
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0}

    # active trigger（带 cron）：不能参与 merge（merge 会丢 cron / condition_prompt）；
    # 但**可以被 disable**——如果几条 trigger 互相重叠/冲突，留最优的、disable 其它
    valid_ids = {o.id for o in overrides}
    triggered_ids = {o.id for o in overrides if (o.trigger_kind or "passive") == "active"}

    prompt = memory_prompts.render_override_dream(overrides, user_id=user_id)
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="reflection",  # opus：override 合并/淘汰判断关键
                temperature=0.1, max_tokens=4000,
            ),
            timeout=OVERRIDE_DREAM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("override_dream timeout uid=%s", user_id)
        audit("override_dream", user_id=user_id, error="timeout",
              reviewed=len(overrides), merged=0, disabled=0,
              latency_ms=int((time.time() - started) * 1000))
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0, "error": "timeout"}
    except Exception as e:
        log.warning("override_dream LLM err uid=%s: %s", user_id, e)
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0, "error": str(e)}

    if not isinstance(d, dict):
        return {"reviewed": len(overrides), "merged": 0, "disabled": 0, "error": "parse_fail"}

    merge_groups = d.get("merge_groups") or []
    disable_ids = d.get("disable_ids") or []
    disable_reasons = d.get("disable_reasons") or {}

    n_merged = 0
    n_disabled = 0
    actions: list[dict[str, Any]] = []
    touched_ids: set[int] = set()  # 防止同一 id 被两条规则同时处理

    # 先处理 merge_groups
    for grp in merge_groups:
        if not isinstance(grp, dict):
            continue
        ids = [int(x) for x in (grp.get("ids") or []) if isinstance(x, (int, str))]
        ids = [i for i in ids if i in valid_ids and i not in triggered_ids and i not in touched_ids]
        merged_text = (grp.get("merged_text") or "").strip()
        reason = (grp.get("reason") or "").strip()
        if len(ids) < 2 or not merged_text:
            continue
        # 注释：不做 hard_guardrail 复检——merged 只是合并已 active 的内容，源已通过过
        try:
            from . import storage as __st
            new_id = __st.add_override(
                user_id=user_id,
                text=merged_text,
                reason=f"override_dream merge: {reason}" if reason else "override_dream merge",
                source_user_msg=f"merged from #{','.join(map(str, ids))}",
                risk_level="low",
                status="active",
                approved_by=0,
            )
            for oid in ids:
                __st.set_override_status(oid, "disabled")
                touched_ids.add(oid)
            n_merged += 1
            actions.append({
                "kind": "merge", "merged_into": new_id,
                "from_ids": ids, "merged_text": merged_text[:200], "reason": reason[:200],
            })
        except Exception as e:
            log.warning("override_dream merge err uid=%s: %s", user_id, e)

    # 再处理 disable_ids（active trigger 可以被 disable，仅 merge 时才排除）
    for x in disable_ids:
        try:
            oid = int(x)
        except Exception:
            continue
        if oid not in valid_ids or oid in touched_ids:
            continue
        try:
            from . import storage as __st
            ok = __st.set_override_status(oid, "disabled")
            if ok:
                n_disabled += 1
                actions.append({
                    "kind": "disable", "id": oid,
                    "reason": disable_reasons.get(str(oid), "")[:200],
                })
                touched_ids.add(oid)
        except Exception as e:
            log.warning("override_dream disable err uid=%s id=%s: %s", user_id, oid, e)

    elapsed_ms = int((time.time() - started) * 1000)
    summary = {
        "reviewed": len(overrides),
        "merged": n_merged,  # 几个合并 group
        "disabled": n_disabled,  # 单独 disable 几个
        "latency_ms": elapsed_ms,
    }
    log.info("override_dream uid=%s 完成：%s", user_id, summary)
    audit("override_dream", user_id=user_id, **summary, actions=actions[:20])
    return summary


SKILL_DREAM_TIMEOUT_SEC = 30.0
SKILL_DREAM_MIN_COUNT = 2
SKILL_CREATOR_PROTECTED_NAME = "skill_creator"  # 不参与整理


async def auto_dream_skills() -> dict[str, Any]:
    """整理跨用户 skill 库——合并语义重复 / disable 失效。

    全表扫一次（不是 per-user），每天 03:13 跟 auto_dream / auto_dream_overrides 同班车跑。
    保护 name='skill_creator' 这条 meta-skill 不动。
    """
    from . import storage as _storage
    from . import embed_client as _ec
    started = time.time()
    skills = _storage.list_skills(status="active", limit=500)
    # 排除 meta-skill
    candidates = [sk for sk in skills if sk.name != SKILL_CREATOR_PROTECTED_NAME]
    if len(candidates) < SKILL_DREAM_MIN_COUNT:
        log.info("skill_dream: 仅 %d 条非 meta active skill，跳过", len(candidates))
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0}

    valid_ids = {sk.id for sk in candidates}

    prompt = memory_prompts.render_skill_dream(candidates)
    try:
        d = await asyncio.wait_for(
            llm.chat_json(
                [{"role": "user", "content": prompt}],
                tier="reflection",  # opus：skill 库整理判断关键
                temperature=0.1, max_tokens=4000,
            ),
            timeout=SKILL_DREAM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("skill_dream timeout")
        audit("skill_dream", error="timeout", reviewed=len(candidates),
              merged=0, disabled=0, latency_ms=int((time.time() - started) * 1000))
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0, "error": "timeout"}
    except Exception as e:
        log.warning("skill_dream LLM err: %s", e)
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0, "error": str(e)}

    if not isinstance(d, dict):
        return {"reviewed": len(candidates), "merged": 0, "disabled": 0, "error": "parse_fail"}

    merge_groups = d.get("merge_groups") or []
    disable_ids = d.get("disable_ids") or []
    disable_reasons = d.get("disable_reasons") or {}

    n_merged = 0
    n_disabled = 0
    actions: list[dict[str, Any]] = []
    touched: set[int] = set()

    for grp in merge_groups:
        if not isinstance(grp, dict):
            continue
        ids = [int(x) for x in (grp.get("ids") or []) if isinstance(x, (int, str))]
        ids = [i for i in ids if i in valid_ids and i not in touched]
        merged_name = (grp.get("merged_name") or "").strip()
        merged_summary = (grp.get("merged_summary") or "").strip()
        merged_body = (grp.get("merged_body") or "").strip()
        reason = (grp.get("reason") or "").strip()
        if len(ids) < 2 or not merged_name or not merged_body:
            continue

        # 合并 usage_count（保留累计被复用次数，反映真实价值）
        sum_usage = sum(sk.usage_count or 0 for sk in candidates if sk.id in ids)
        # 取第一个被合并的 created_by 当代表
        first_creator = next(
            (sk.created_by for sk in candidates if sk.id in ids), 0,
        )

        # 算 embedding（合并 summary + body 一起 embed）
        try:
            vec = await _ec.embed_one(merged_summary + " | " + merged_body)
        except Exception as e:
            log.debug("skill_dream embed err: %s", e)
            vec = None
        if vec is None:
            log.warning("skill_dream merge skip uid=meta: embed 失败 group %s", ids)
            continue

        try:
            new_skill_id = _storage.add_skill(
                name=merged_name[:64],
                summary=merged_summary[:200],
                body=merged_body,
                embedding=vec,
                created_by=first_creator,
            )
            # 累加 usage_count（add_skill 默认 0）
            if sum_usage > 0:
                with _storage.session() as s:
                    sk_new = s.query(_storage.Skill).filter(_storage.Skill.id == new_skill_id).first()
                    if sk_new:
                        sk_new.usage_count = sum_usage
                        s.commit()
            for sid in ids:
                _storage.set_skill_status(sid, "disabled")
                touched.add(sid)
            n_merged += 1
            actions.append({
                "kind": "merge", "merged_into": new_skill_id,
                "from_ids": ids, "merged_name": merged_name,
                "merged_summary": merged_summary[:200],
                "preserved_usage": sum_usage, "reason": reason[:200],
            })
        except Exception as e:
            log.warning("skill_dream merge err: %s", e)

    for x in disable_ids:
        try:
            sid = int(x)
        except Exception:
            continue
        if sid not in valid_ids or sid in touched:
            continue
        try:
            ok = _storage.set_skill_status(sid, "disabled")
            if ok:
                n_disabled += 1
                actions.append({
                    "kind": "disable", "id": sid,
                    "reason": disable_reasons.get(str(sid), "")[:200],
                })
                touched.add(sid)
        except Exception as e:
            log.warning("skill_dream disable err id=%s: %s", sid, e)

    elapsed_ms = int((time.time() - started) * 1000)
    summary = {
        "reviewed": len(candidates),
        "merged": n_merged,
        "disabled": n_disabled,
        "latency_ms": elapsed_ms,
    }
    log.info("skill_dream 完成：%s", summary)
    audit("skill_dream", **summary, actions=actions[:30])
    return summary


def _parse_verdicts(raw: str, *, valid_ids: set[str]) -> dict[str, str]:
    """LLM 输出的 JSON → dict[id → verdict]。strict json 优先，失败回退 regex。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    out: dict[str, str] = {}
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

    if isinstance(parsed, dict) and isinstance(parsed.get("verdicts"), list):
        for v in parsed["verdicts"]:
            if not isinstance(v, dict):
                continue
            cid = v.get("id")
            verdict = v.get("verdict")
            if cid in valid_ids and verdict in ("still_valid", "to_verify", "stale"):
                out[cid] = verdict
        return out

    # 兜底
    for m in _VERDICT_RE.finditer(raw):
        cid, verdict = m.group(1), m.group(2)
        if cid in valid_ids:
            out[cid] = verdict
    return out


# ============ 抽取（LLM → JSON → list[(type, content)]）============

def _format_resource(batch: list[dict[str, str]]) -> str:
    """[{role, content}, ...] → prompt 里的对话片段。"""
    lines: list[str] = []
    for m in batch:
        role = "user" if m.get("role") == "user" else "assistant"
        content = (m.get("content") or "").strip().replace("\r", "")
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


_ITEM_RE = re.compile(
    r'\{\s*"type"\s*:\s*"(profile|event)"\s*,\s*"content"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*\}',
    re.DOTALL,
)


def _parse_items_loose(raw: str) -> list[tuple[str, str, list[str]]]:
    """从 LLM 输出抠出 (type, content, entities) 三元组。

    第一道：strict json.loads 整体；第二道：regex 逐个对象抠（防 LLM 中间漏字段时全部失败）。
    entities：P0-1 抽取的关键实体词列表（人名/地名/作品/物品等），fallback 路径给空。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    # 去掉 ```json 围栏
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

    out: list[tuple[str, str, list[str]]] = []
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        for it in parsed["items"]:
            if not isinstance(it, dict):
                continue
            t = (it.get("type") or "").strip()
            c = (it.get("content") or "").strip()
            ents_raw = it.get("entities") or []
            ents: list[str] = []
            if isinstance(ents_raw, list):
                for e in ents_raw[:5]:  # cap at 5 per item
                    if isinstance(e, str):
                        es = e.strip()[:12]
                        if es:
                            ents.append(es)
            if t in ("profile", "event") and c:
                out.append((t, c[:300], ents))
        return out

    # 兜底：strict 失败 → regex 逐对象抠（fallback 路径丢 entities，给空列表）
    for m in _ITEM_RE.finditer(raw):
        t = m.group(1)
        c = m.group(2).encode().decode("unicode_escape", errors="ignore").strip()
        if c:
            out.append((t, c[:300], []))
    return out


async def _extract_items(
    batch: list[dict[str, str]], user_id: int | None = None,
) -> list[tuple[str, str, list[str]]]:
    """跑 LLM 抽取，返回 [(type, content), ...]。"""
    if not batch:
        return []
    s = settings()
    model = s.memu_chat_model or s.openrouter_model
    if not model:
        log.warning("memory extract: 没设 MEMU_CHAT_MODEL/OPENROUTER_MODEL，跳过")
        return []
    resource = _format_resource(batch)
    user_prompt = memory_prompts.render(resource, user_id=user_id)

    try:
        # 用 openrouter.chat 直接调，不走 :18082 shim
        from . import openrouter
        res = await openrouter.chat(
            [{"role": "user", "content": user_prompt}],
            model=model,
            temperature=0.1,
            max_tokens=2048,
        )
        raw = res.get("text", "") if isinstance(res, dict) else ""
        if not raw:
            log.warning("memory extract: LLM 空响应（model=%s）", model)
            return []
    except Exception as e:
        log.warning("memory extract LLM call failed: %s", e)
        return []

    items = _parse_items_loose(raw)
    if not items and raw:
        log.warning("memory extract: 解析失败 raw=%r", raw[:300])
    return items


# ============ 入库（embedding + INSERT）============

async def _persist_items(
    user_id: int,
    items: list[tuple[str, str, list[str]]],
    *,
    evidence_ref: str | None = None,
    source_episode_id: str | None = None,
) -> list[dict[str, Any]]:
    """对每条 (type, content, entities) 算 embedding 并 INSERT 到 memories。

    返回 list[dict]，每条 dict 含 id / summary / memory_type / embedding / entities；
    上层既用这个填 audit，也喂 _fire_conflict_check 复用 embedding。

    P0-4: source_episode_id 反查原始 turns。
    P0-1: entities 给 hybrid retrieval 的 entity 路用。
    """
    if not items or not user_id:
        return []
    summaries = [c for _t, c, _e in items]
    vecs = await embed_client.embed_many(summaries)

    eng = memory_store.engine()
    now = datetime.now(timezone.utc)
    inserted: list[dict[str, Any]] = []
    with eng.begin() as conn:
        for (t, c, ents), vec in zip(items, vecs):
            new_id = str(_uuid.uuid4())
            ents_param = ents if ents else None  # 空列表写 NULL 比 {} 干净
            params: dict[str, Any] = {
                "id": new_id,
                "user_id": user_id,
                "summary": c,
                "memory_type": t,
                "created_at": now,
                "updated_at": now,
                "evidence_ref": evidence_ref,
                "source_episode_id": source_episode_id,
                "entities": ents_param,
            }
            # status / confidence 必须显式给——这俩列在 ALTER 加的时候 NOT NULL，
            # raw INSERT 不走 ORM default、PG 也不会自动填 ALTER 设的 default。
            # source_episode_id / entities 走 CAST；NULL 安全。
            # P1-5: valid_from = created_at；valid_to NULL（仍生效）
            if vec is not None:
                params["embedding"] = embed_client.vec_literal(vec)
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, embedding, "
                        "created_at, updated_at, evidence_ref, status, confidence, "
                        "source_episode_id, entities, valid_from) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, :memory_type, "
                        "CAST(:embedding AS vector), :created_at, :updated_at, :evidence_ref, "
                        "'confirmed', 1.0, CAST(:source_episode_id AS uuid), :entities, "
                        ":created_at)"
                    ),
                    params,
                )
            else:
                conn.execute(
                    sql_text(
                        "INSERT INTO memories (id, user_id, summary, memory_type, "
                        "created_at, updated_at, evidence_ref, status, confidence, "
                        "source_episode_id, entities, valid_from) "
                        "VALUES (CAST(:id AS uuid), :user_id, :summary, :memory_type, "
                        ":created_at, :updated_at, :evidence_ref, 'confirmed', 1.0, "
                        "CAST(:source_episode_id AS uuid), :entities, :created_at)"
                    ),
                    params,
                )
            inserted.append({
                "id": new_id, "summary": c, "memory_type": t,
                "embedding": vec, "entities": ents,
            })
    return inserted
