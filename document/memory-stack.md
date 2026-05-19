# 记忆栈（自搭，2026-05-18 起）

> 此前用 memU SDK（`memu-py`），1.5.x 多次踩坑后改自搭。架构等价 +
> PRD v2 三层防线（status 字段 / 写入冲突检测 / 召回反验证 / Auto Dream）。
> memU 时代的归档说明在 `memu-setup.md`。

## 一句话

postgres + pgvector 单表 RAG。flush 时跑 LLM 抽 profile/event 落库，recall 跑 cosine top-k；
所有事实带 `status`（confirmed/to_verify/stale）让记忆知道"自己有多确定"。

## Schema

`memories` 表（pgvector + 一些状态字段）：

| 列                           | 类型                                       | 说明                                     |
| --------------------------- | ---------------------------------------- | -------------------------------------- |
| `id`                        | UUID PK                                  |                                        |
| `user_id`                   | BIGINT NOT NULL, indexed                 | 跟 SQLAlchemy 端 BIGINT 对齐               |
| `summary`                   | TEXT NOT NULL                            | LLM 抽出的中文事实                            |
| `memory_type`               | VARCHAR(32) NOT NULL DEFAULT 'profile'   | `profile` / `event`                    |
| `embedding`                 | vector(512)                              | bge-small-zh-v1.5 出的向量                 |
| `created_at` / `updated_at` | TIMESTAMPTZ                              |                                        |
| `evidence_ref`              | TEXT                                     | 来源对话 `data/memu_buffer/conv_*.json` 路径 |
| **PRD v2 状态**：              |                                          |                                        |
| `status`                    | VARCHAR(16) NOT NULL DEFAULT 'confirmed' | `confirmed` / `to_verify` / `stale`    |
| `confidence`                | DOUBLE PRECISION DEFAULT 1.0             | 0.0-1.0；stale=0.0、to_verify=0.5        |
| `last_verified_at`          | TIMESTAMPTZ                              | 最后一次反验证仍成立的时间（5.2 cooldown 用）          |
| `depends_on`                | UUID[]                                   | 触发该条变 to_verify/stale 的上游事实 id 列表（去重）  |

索引：`(user_id, created_at)`、`(user_id, status)`、`embedding USING ivfflat (vector_cosine_ops)` 100+ 行后自动建。

## 主链路

```
对话 turn
   │
   ▼
note_turn(uid, user_text, assistant_text)  → 写 dict[uid, list]
   │
   │ (每 6 turns 或 15 min 强制 flush；APScheduler memu_flush_job 兜底每 15 min 扫所有 user)
   ▼
maybe_flush(uid, force) ──► _extract_items: LLM (deepseek-flash) 抽 JSON
                          │   { items: [{type, content}, ...] }
                          ▼
                         _persist_items: 算 embedding → INSERT
                          │  返回 list[{id, summary, memory_type, embedding}]
                          ▼
                  (异步) _fire_persona_update(uid, batch)
                  (异步) _fire_conflict_check(uid, new_records)  ← PRD 5.1
                                │
                                ▼
                       对每条新事实，召回 top-5 旧 profile（同 batch 互排除）
                       LLM 一次 call 拿 verdicts → UPDATE 旧条目 status / depends_on
```

```
recall(uid, user_text, top_k=3)
   │
   │ A 道门：query 太短/纯口头禅 → 跳过 recall（hits=0, skipped_reason）
   │   _is_low_value_query：CJK<6 / 英文<3 词 / 整句口头禅 regex 命中 → skip
   │
   │ embed_one(user_text) → 512-dim 向量
   ▼
SELECT … FROM memories WHERE status != 'stale'
  AND (embedding <=> :q) < 0.55  ← B 道门：cosine distance 阈值
  ORDER BY embedding <=> :q LIMIT k
   │
   │ items 中 status='to_verify' 且 last_verified_at NULL 或 30min 之前 → due 列表
   ▼
asyncio.gather(*[_reverify_one(uid, item, query) for item in due])  ← PRD 5.2 同步阻塞
   │   每条：拉 deps 上游 → LLM (still_valid / uncertain) → UPDATE
   ▼
按最新 status 拼 snippets：confirmed 不带前缀；to_verify 带 `[待确认]`
audit memory_recall 加 distances 数组（admin UI 每条 hit 前显示 d=0.32）
```

**精度调优**（`RECALL_MAX_DISTANCE = 0.55`，`RECALL_MIN_QUERY_CJK_CHARS = 6`）：
- 仍有相关 query 被滤 → 阈值调高（如 0.6）
- 仍有噪声穿过 → 阈值调低（如 0.45）
- audit `memory_recall` 每条 hit 自带 `d=` 距离值，admin 直接看分布

```
03:13 cron auto_dream_job (CST)
   │
   ▼
对每个 active user，拉所有 status='to_verify' 的条目
   │
   │ 每条：召回 top-5 同 user confirmed 邻居作综合上下文
   ▼
_dream_one(uid, item) → LLM 三态判定（still_valid / uncertain / stale）  ← PRD 5.3
                                │
                                ▼
              still_valid → status=confirmed conf=1.0
              stale       → status=stale conf=0.0
              uncertain   → 仅打 last_verified_at 戳
```

## 为什么三层

每层针对不同时机和决策风险：

| 层 | 触发 | LLM 决策权 | 错杀风险 |
|---|---|---|---|
| 5.1 写入冲突检测 | flush 后异步 | confirmed/to_verify/stale | 中（保守标 to_verify 多）|
| 5.2 召回反验证 | recall 同步 + 30min cooldown | still_valid/uncertain（不能 stale） | 低（不能 stale）|
| 5.3 Auto Dream | 03:13 cron 批量 | 三态全开 | 中但有审计可看 |

5.1 在事件刚发生时无法判断细节，宁错标 to_verify；5.2 拿当下 query 上下文升 confirmed；5.3 拿全局邻居精修。
**最差情况**：哪怕全部 LLM 判错，至少召回结果分级了——主 LLM 知道哪些事实可能不确定，不会把所有
当作 100% 真实灌进去。这一点本身就有价值。

## 公共 API

```python
from src import memory

# 召回
snippets: list[str] = await memory.recall(user_id, user_text, top_k=3)
# 例：["(2026-05-15) 用户最近在减肥", "(2026-05-13) [待确认] 用户骑车上班 15 分钟"]

# 短期 buffer 累积
memory.note_turn(user_id, user_text, assistant_text)

# 触发 flush（按 6 turns / 15min 条件，force=True 强制）
await memory.maybe_flush(user_id=None, force=False)  # None = 遍历所有 buffer 非空 user

# 后台批量整理（scheduler 调，也可手动跑）
res: dict = await memory.auto_dream(user_id)
# {"reviewed": N, "to_confirmed": X, "to_stale": Y, "uncertain": Z, "errors": E, "latency_ms": ...}
```

## 关键文件

| 文件 | 作用 |
|---|---|
| `src/memory_store.py` | `Memory` ORM 类、`engine()` 单例、`_ensure_v2_columns` 启动时 ALTER |
| `src/memory_prompts.py` | 4 个 prompt：抽取 / conflict / reverify / dream |
| `src/memory.py` | recall / note_turn / maybe_flush / auto_dream + 各异步 fire helpers |
| `src/embed_client.py` | embed_server 客户端 + pgvector 字面量 |
| `src/scheduler.py` | `auto_dream_job` cron 03:13 |
| `scripts/migrate_memu_to_native.py` | 一次性：旧 `memory_items` 表 → 新 `memories` 表 |
| `scripts/backfill_conflict_check.py` | 一次性：给历史 profile 重放 5.1（让 graph 上有 deps 边） |

## Audit 事件

`data/audit.jsonl` 里有这些 event（admin UI 审计 tab 可滤）：

- `memory_flush` — flush 一次的汇总（msgs / new_items / new_item_summaries）
- `memory_conflict_check` — 5.1 写入冲突分析单次（new_id / candidates / flips: [{id, verdict, summary}]）
- `memory_recall` — recall 一次（query / hits / snippets）
- `memory_reverify` — 5.2 反验证单条（fact_id / verdict / reason / latency_ms / upstream / query）
- `memory_dream` — 5.3 整批汇总（reviewed / to_confirmed / to_stale / uncertain / errors / latency_ms）
- `memory_dream_one` — 5.3 单条（fact_id / verdict / reason / latency_ms / upstream / neighbors）
- `feedback_screen` / `feedback_decision` — Feedback sub-agent 粗筛 + 精判（详见 `feedback-agent.md`）
- `tool_decision` / `tool_call` — search 工具触发判定 + 调用结果（详见 `agent-reach-integration.md`）

## admin UI 图谱

`/`:18081 webUI 的 **图谱** tab：D3 v7 force-directed graph。
- 节点 = memory（绿 confirmed / 橙 to_verify / 灰 stale，profile 圆比 event 大）
- 边 = `depends_on`（B → A 表示 B 依赖 A）
- hover 看 summary，双击跳到 items tab 定位
- 拖拽 / 滚轮缩放 / 力导向自由布局
- "只看有依赖关系的节点" 开关：过滤掉孤岛

## 运维 / 故障排查

**embed_server 没起来 / 不可达**：recall 会跳过 RAG 不阻塞主链路，`embed_one` 返回 None。
日志 `embed_server 不可达，跳过 embedding`。检查 :18080 是否监听。

**conflict check 0 flips**：LLM 保守判 still_valid 是常态。也可能 LLM 解析失败，
audit 里看 `memory_conflict_check` 看 `verdicts` 是不是 0 长度。

**召回带 `[待确认]` 但主 LLM 没用上**：to_verify 信号给主 LLM 看的，主 LLM 可以选择不引用。
查 prompts.py 的 system prompt 里有没有解释这个标记的指引（必要时加）。

**5.3 没跑或没产生变化**：CronTrigger 容器时区是不是 CST（`docker-compose.yml` 设了 TZ=Asia/Shanghai）；
`audit.jsonl` 里 `memory_dream` 应该每天有一条；reviewed=0 表示当时没 to_verify 条目。

**给历史用户补 deps 数据**：跑 `scripts/backfill_conflict_check.py --user-id <uid>`。
LLM 调用次数 = 该用户 profile 数；deepseek-flash 一次 ~$0.0001。

## 参考

- PRD 原文：`me/prd_memory.md`
- MEME paper 引用 + 三层方案推导：PRD §3-§5
- 历史 memU 时代细节：`memu-setup.md`（已归档）
