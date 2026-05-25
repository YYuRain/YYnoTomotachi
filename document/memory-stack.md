# 记忆栈（自搭，2026-05-18 起；P0-1/P0-4 升级 2026-05-20；P1-5/P1-6 升级 2026-05-21）

> 此前用 memU SDK（`memu-py`），1.5.x 多次踩坑后改自搭。架构等价 +
> PRD v2 三层防线（status 字段 / 写入冲突检测 / 召回反验证 / Auto Dream）+
> P0-1 hybrid retrieval（Mem0 v3）+ P0-4 episodes provenance（Graphiti）+
> P0-2 三因子 ranker（Generative Agents）+
> P1-5 bi-temporal 字段（Graphiti）+ P1-6 insight 生成（Generative Agents reflection）。
> memU 时代的归档说明在 `memu-setup.md`；横向研究分析在 `me/记忆框架横纵分析.md`。

## 一句话

postgres + pgvector 单表 RAG。**Hot path** = recall 同步走 cosine + ngram + entity 三路 RRF 融合；
**Background** = flush 后异步抽 + 5.1/persona/feedback + 03:13 Auto Dream。
所有事实带 `status`（confirmed/to_verify/stale）让记忆知道"自己有多确定"，
+ `source_episode_id` 反查原始 turns（episodes 表）。

## Hot path vs Background（命名借自 LangMem）

| 路径 | 何时跑 | 谁触发 | 阻塞主对话？ | 模块 |
|------|-------|-------|-------------|------|
| **Hot path** | 每个 turn 进入时 | agent | 是 | `memory.recall` + 5.2 反验证 |
| **Background** | flush / cron | scheduler / asyncio.create_task | 否 | `memory._fire_*` + auto_dream |

Hot path 必须快（< 200ms 目标），Background 可以慢（cron 任务半小时一跑也行）。
两条路径**通过 status 字段共享状态**——hot path 看 to_verify 改 last_verified_at，
background 看 to_verify 改 status——互相可见但不竞争锁。

## Schema

`memories` 表（pgvector + 一些状态字段）：

| 列                           | 类型                                       | 说明                                     |
| --------------------------- | ---------------------------------------- | -------------------------------------- |
| `id`                        | UUID PK                                  |                                        |
| `user_id`                   | BIGINT NOT NULL, indexed                 | 跟 SQLAlchemy 端 BIGINT 对齐               |
| `summary`                   | TEXT NOT NULL                            | LLM 抽出的中文事实                            |
| `memory_type`               | VARCHAR(32) NOT NULL DEFAULT 'profile'   | `profile` / `event` / `insight`（P1-6）|
| `embedding`                 | vector(512)                              | bge-small-zh-v1.5 出的向量                 |
| `created_at` / `updated_at` | TIMESTAMPTZ                              |                                        |
| `evidence_ref`              | TEXT                                     | 来源对话 `data/memu_buffer/conv_*.json` 路径 |
| **PRD v2 状态**：              |                                          |                                        |
| `status`                    | VARCHAR(16) NOT NULL DEFAULT 'confirmed' | `confirmed` / `to_verify` / `stale`    |
| `confidence`                | DOUBLE PRECISION DEFAULT 1.0             | 0.0-1.0；stale=0.0、to_verify=0.5        |
| `last_verified_at`          | TIMESTAMPTZ                              | 最后一次反验证仍成立的时间（5.2 cooldown 用）          |
| `depends_on`                | UUID[]                                   | 触发该条变 to_verify/stale 的上游事实 id 列表（去重）  |
| **P0-1/P0-4（2026-05-20）**：|                                          |                                        |
| `source_episode_id`         | UUID                                     | 抽这条时 buffer 落在 episodes 表的哪一行（反查原始对话）|
| `entities`                  | TEXT[]                                   | LLM 抽出的关键名词（人名/地名/作品/物品），entity 路 retrieval 用 |
| **P1-5 bi-temporal**：       |                                          |                                        |
| `valid_from`                | TIMESTAMPTZ                              | 事实在世界中开始生效；profile/event 默认 = created_at |
| `valid_to`                  | TIMESTAMPTZ NULL                         | 失效时间点；NULL=仍生效；5.1/5.3 判 stale 时填 now() |

索引：`(user_id, created_at)`、`(user_id, status)`、`embedding USING ivfflat (vector_cosine_ops)` 100+ 行后自动建；
**P0-1**：`summary USING GIN (gin_trgm_ops)` 加速 ILIKE 子串、`entities USING GIN` 加速 array 查询。

`episodes` 表（P0-4）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | BIGINT | |
| `raw_turns` | JSONB | list[{role, content}]，原始 buffer 内容 |
| `turn_count` | INTEGER | |
| `started_at` / `ended_at` | TIMESTAMPTZ | 这次 flush 覆盖的对话起止 |
| `created_at` | TIMESTAMPTZ | |

`agent_ideas` 表（airi 借鉴，2026-05-25）：

bot 凌晨自主形成的"想做的事" pool。proactive 决策时优先消费当 opener_angle，
让 bot 显得"想起来一件事"而不是机械抽 recent_topics。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | SERIAL PK | |
| `user_id` | BIGINT | |
| `text` | TEXT | "想问她那个 PR 后来咋样了" |
| `kind` | VARCHAR(32) | `question` / `share` / `follow_up` / `observation` |
| `priority` | INTEGER 1-10 | LLM 自评；6+ 优先消费 |
| `status` | VARCHAR(16) | `open` / `used` / `expired` |
| `source_ids` | UUID[] | 引用了哪些 memories.id（admin 跳出处用）|
| `suggested_query` | TEXT | **share kind 必带**——proactive 消费时当 search query |
| `created_at` | TIMESTAMPTZ | |
| `used_at` | TIMESTAMPTZ | mark_idea_used 时填 |
| `expires_at` | TIMESTAMPTZ | 默认 created_at + 7 天；expire_old_ideas 标 expired |

索引：`(user_id, status)` / `(user_id, priority)`。模块：`src/agent_ideas.py`。

## Background：写入流水

```
对话 turn
   │
   ▼
note_turn(uid, user_text, assistant_text)  → 写 dict[uid, list]
   │
   │ (每 6 turns 或 15 min 强制 flush；APScheduler memu_flush_job 兜底每 15 min 扫所有 user)
   ▼
maybe_flush(uid, force)
   │
   ├─► add_episode(uid, batch)              ← P0-4：先落 episodes 表，拿 episode_id
   │
   ├─► _extract_items: LLM (deepseek-flash) 抽 JSON
   │      { items: [{type, content, entities}, ...] }   ← P0-1：entities 字段
   │
   ├─► _persist_items: 算 embedding → INSERT memories
   │     带 source_episode_id（P0-4） + entities（P0-1）
   │     返回 list[{id, summary, memory_type, embedding, entities}]
   │
   ├─► (异步) _fire_persona_update(uid, batch)
   ├─► (异步) _fire_feedback_check(uid, batch)
   └─► (异步) _fire_conflict_check(uid, new_records)  ← PRD 5.1
                  │
                  ▼
            对每条新事实，召回 top-5 旧 profile（同 batch 互排除）
            LLM 一次 call 拿 verdicts → UPDATE 旧条目 status / depends_on
```

## Hot path：recall 三路 RRF + 三因子 ranker（P0-1 + P0-2，2026-05-20/21）

```
recall(uid, user_text, top_k=3)
   │
   │ A 道门：query 太短/纯口头禅 → 跳过（hits=0, skipped_reason）
   │   _is_low_value_query：CJK<3 / 英文<3 词 / 整句口头禅 regex 命中 → skip
   │   （P0-1 把阈值从 6 → 3：hybrid 时代 ngram/entity 路对短 query 友好）
   │
   ▼
三路并行候选拉取（HYBRID_CANDIDATES_PER_PATH=20）：
  ┌─ cosine 路   embedding <=> :q < 0.55，按 distance 升序
  ├─ ngram 路    把 query 切 2-char window（去口头禅+黑名单"什么/今天/这个"等过泛词）；
  │             OR 起来 ILIKE '%win%'，按命中数 DESC（pg_trgm GIN 加速）
  └─ entity 路   EXISTS unnest(entities) e WHERE :q ILIKE '%'||e||'%'，按命中数 DESC
   │
   ▼
RRF 融合：rrf_score(doc) = Σ 1/(RRF_K + rank_in_path)，RRF_K=60
   │
   │ P0-2 三因子加权（Generative Agents 借鉴）：在 RRF 上叠加 importance + recency
   │   rel = rrf_score / max(rrf_score in candidates)
   │   imp = confidence 字段（[0,1]，stale=0/to_verify=0.5/confirmed=1）
   │   rec = exp(-age_days / τ)，profile τ=180d、event τ=14d
   │   final = α·rel + β·imp + γ·rec  （α=1.0, β=0.3, γ=0.3 默认）
   ▼
按 final 排序取 top_k
   │
   │ 候选中 status='to_verify' 且 last_verified_at NULL 或 30min 之前 → due
   ▼
asyncio.gather(*[_reverify_one(uid, item, query) for item in due])  ← PRD 5.2 同步阻塞
   │   每条：拉 deps 上游 → LLM (still_valid / uncertain) → UPDATE
   ▼
按最新 status 拼 snippets：confirmed 不带前缀；to_verify 带 `[待确认]`
audit memory_recall 加 candidates_per_path（cosine/ngram/entity 各路命中数）
```

**精度调优**：
- `RECALL_MAX_DISTANCE = 0.55` 仅 cosine 路用（噪声底）
- `NGRAM_MIN_HITS = 1` 命中至少一个 ngram 才算候选；调高更严
- `RRF_K = 60` 工业默认，越大越平均越小越尖锐
- `RANKER_W_RELEVANCE/IMPORTANCE/RECENCY` 三因子权重，默认 1.0 / 0.3 / 0.3。
  调小 RECENCY 会让老 profile 更容易进 top；调大 RECENCY 让最新 event 更突出。
- `TAU_PROFILE_DAYS=180 / TAU_EVENT_DAYS=14`：半衰期。event 时效性强（τ 短），profile 长期稳定（τ 长）。
- audit `memory_recall.candidates_per_path` 看每路命中数；`score_breakdown` 看每条 hit 的 rel/imp/rec/final 分量、`age_days` 和实际 τ。某路一直是 0 = 该路设计有问题或数据没填好（如老 memory entities 都是 NULL，需要 backfill 或等新写入）

```
03:13 cron auto_dream_job (CST)
   │
   ├─► 1. auto_dream(uid)            ← PRD 5.3 三态判定
   │   │
   │   ▼
   │   拉所有 to_verify 条目；每条召回 top-5 confirmed 邻居作上下文
   │   _dream_one → LLM 三态判定（still_valid / uncertain / stale）
   │     · still_valid → status=confirmed conf=1.0
   │     · stale       → status=stale conf=0.0 + valid_to=now（P1-5）
   │     · uncertain   → 仅打 last_verified_at 戳
   │
   ├─► 2. auto_dream_overrides(uid)   ← prompt_overrides 冲突合并
   │
   ├─► 3. auto_dream_insights(uid)   ← P1-6（Generative Agents reflection）
   │   │
   │   ▼
   │   抽样最近 90 天 confirmed memory（profile 8 + event 12）
   │   拉最近 30 天 existing insights 喂回 prompt（避免重写同 pattern）
   │   sonnet 写 0-3 条跨条目高阶观察 + supporting_ids
   │   每条算 embedding，与现存 insight + 同 batch cosine 比；≥ 0.85 拦截（去重）
   │   通过的 INSERT 为 memory_type='insight', confidence=0.8, depends_on=supporting
   │   audit memory_dream_insight（含 dedup_rejected / duplicates 字段）
   │
   ├─► 4. agent_ideas.form_ideas(uid)  ← airi `come_up_ideas` 借鉴（2026-05-25）
   │   │
   │   ▼
   │   抽样最近 30 天 profile + event；拉近 14 天现存 idea（open + used）作"已写过"清单
   │   sonnet 自主形成 0-5 条"想问她 X / 想跟进 Y / 想分享 Z"
   │   kind ∈ {question, share, follow_up, observation}；priority 1-10
   │   share kind 必带 suggested_query（具体搜索关键词）；缺则降级 follow_up
   │   写入前 cosine 去重（≥ 0.85 拦）；通过的 INSERT agent_ideas 表
   │   expires_at = now + 7 天；7 天没 used 自动 expire（daily_cleanup 跑）
   │   audit agent_ideas_form
   │
   └─► 5. auto_dream_skills()         ← skill 库整理（全局一次）
```

### agent_ideas 怎么被消费

`proactive.decide` 调用 `list_pending(uid, top_n=3)` 拉优先级最高的 3 条 idea 塞进
ctx，喂给软门 LLM。软门 LLM 输出 `consumed_idea_id`（可选）表示采纳哪条；校验 id
必须在 pending 列表里防伪造。**采纳的处理走三路并行**：

```
LLM 输出 consumed_idea_id
   │
   ▼
┌─────────────────────────────────────────────────────┐
│ 路径 A：share kind idea + suggested_query 不空      │
│   → 自动用 query 调 _select_share_item              │
│   → mode=share_discovery + share_item 来自 idea     │
│   → opener prompt 走"想到 X → 顺手搜了下"双层叙事    │
├─────────────────────────────────────────────────────┤
│ 路径 B：LLM 没消费 share idea 但临时输出 share_intent│
│   → 现搜现挑（现状 share_discovery 路径，"刚翻到一条"）│
├─────────────────────────────────────────────────────┤
│ 路径 C：question/follow_up/observation kind         │
│   → topic_chat + opener prompt 走"想起来的事"叙事   │
└─────────────────────────────────────────────────────┘

无论哪路，采纳后 mark_idea_used(id) → status='used'
```

设计意图：营造"有时看了某些帖子引发的思考，有时只是单纯想分享"的并存感觉，
而不是 share 跟 idea 互不通气。
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
