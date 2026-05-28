# 架构总览

> 对应 PRD：`prd/0.md`；实施计划：`~/.claude/plans/prd-0-md-robust-lobster.md`。
> 本项目是 MVP——追求"能跑 + 架构可扩展"，不追求功能完整。

## 一句话

> 一个陪伴型（不是助手、不是心理咨询师）的 Telegram chat agent，会主动找人、像真人聊、
> 会自己想起记忆、兴趣热度会涨会退、能看图发表情包。OpenRouter / Claude / MiniMax 做 LLM，
> 本地 bge-small-zh 做 embedding，自搭 postgres+pgvector 记忆栈（PRD v2 三层防线，详见
> `memory-stack.md`）。**多用户邀请制**（5–50 人），单实例。

## 运行时结构

```
┌──────────────── 单进程 asyncio loop ─────────────────┐
│                                                      │
│  Telegram polling                                    │
│   ├─ prod bot   ──► bot._on_message / _on_photo     │
│   └─ test bot   ──► test_bot._on_message (可选)     │
│                              │                       │
│       users.is_active(chat_id) gate（未激活 silent drop）│
│                              ▼                       │
│                       agent.handle_user_message(uid) │
│                       ├─┐ emotion.detect (mode 判档) │
│                       ├─┼ memory.recall(uid)        │ 四者并行
│                       ├─┼ interests.extract topics    │
│                       ├─┘ tools.fetch / search       │
│                       ├─► clock.now_signal (时间感前缀) │
│                       ├─► interests.bump(uid)       │
│                       ├─► prompts.build_system_prompt │
│                       │     (含 per-user persona / sticker tags) │
│                       ├─► llm.chat (multimodal blocks│
│                       │      → anthropic 走 vision)   │
│                       ├─► stickers.parse_message (拆 [sticker:tag] 段) │
│                       ├─► rhythm.deliver → send      │
│                       │   send_sticker → 发 Telegram 图│
│                       └─► async: memorize(uid) / availability(uid) │
│                                                      │
│  本地服务（main.py 内 asyncio task）                   │
│   └─ embed_server  :18080  bge-small-zh embedding shim │
│                                                      │
│  APScheduler（7 job 各自 fan-out 遍历 users.list_active() with Sem(5)） │
│   ├─ decay_job              每 1h  interests.decay_tick(uid) per user │
│   ├─ memu_flush_job         每 15m memory.maybe_flush(uid) per user │
│   │                               └─► persona.update_state(uid) │
│   │                               └─► _fire_conflict_check (PRD 5.1 异步) │
│   │                               └─► _fire_feedback_check (sub-agent 沉淀 user 偏好) │
│   ├─ persona_consolidate    每日 03:07 (CST) per user 衰减/清旧观察│
│   ├─ auto_dream             每日 03:13 (CST) 6 段流水（per-user 5 段 + 全局 1 段）：│
│   │                                5.3 三态判定 / override 整理 / insight P1-6（cosine 去重） / │
│   │                                form_ideas / **agent self_iterate (L4 自治，2026-05-28)** /  │
│   │                                skill 库整理；反思类 LLM 全部走 reflection tier (opus-4.7) │
│   ├─ proactive_job          每 25m per user 软概率门 + LLM 决策 │
│   │                                └─► decide(uid) → mode={topic_chat|share_discovery}, │
│   │                                    consumed_idea_id（采纳 agent_ideas 一条） │
│   │                                    share_discovery 三路：(A) idea-driven share / │
│   │                                    (B) 独立 share_intent / (C) topic_chat │
│   │                                └─► generate_opener(uid)（看 mode 走对应 prompt）│
│   │                                └─► bot.make_send_and_typing(uid) │
│   ├─ triggered_reach_job    每 1m  扫 active trigger override (cron match → sonnet 判 │
│   │                                condition → 暂存或直发，绕开 proactive 冷却) │
│   ├─ pending_reach_overdue  每 1m  pending 超 5min 仍没融入 → 兜底直发 │
│   └─ daily_cleanup_job      每日 04:23 audit.YYYY-MM-DD.jsonl 30 天 / wipe_backup 7 天 │
│                                                      │
│  云部署额外组件（docker-compose）                        │
│   ├─ postgres   pgvector，跨容器名 `postgres:5432`    │
│   ├─ admin      :18081 webUI（独立容器，跑 scripts.admin） │
│   ├─ mihomo     :9981 Clash 内核（HK 出口走美区代理 → OpenRouter Anthropic 模型可用） │
│   └─ cloudflared 临时 HTTPS tunnel（admin UI 走 trycloudflare.com） │
└──────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 职责 | 状态 |
|------|------|------|
| `src/config.py` | `.env` → `Settings` dataclass（含 telegram_proxy / embed_server 字段） | ✅ MVP |
| `src/storage.py` | SQLite 表：interests / reply_samples / last_interaction / proactive_fires（含 mode/platform 列）/ persona_snapshots / users / invite_codes（多用户起所有表都有 user_id）；`_ensure_columns` 启动时 ALTER 兜底 | ✅ MVP |
| `src/users.py` | 邀请码生成/redeem、用户列表、admin 判定、`wipe_user`（test bot /clear 用）、webUI 共享 HMAC token 工具 | ✅ 接入（2026-05-13） |
| `src/test_bot.py` | 可选第二个 Telegram bot（`TEST_BOT_TOKEN`）：`/become <label>` 选虚拟 user_id（与 chat_id 解耦），完整邀请码激活流程，`/clear` 清盘——同 telegram 账户能扮演多个用户 | ✅ 接入（2026-05-13，可选） |
| `src/minimax.py` | 聊天走 OpenAI 兼容端点；embed 走 MiniMax 原生格式；自动剥 `<think>` | ✅ MVP |
| `src/embed_server.py` | 本地 OpenAI 兼容 embedding shim：FastAPI + sentence-transformers (bge-small-zh) :18080 | ✅ MVP |
| `src/embed_client.py` | embed_server 客户端 + pgvector 字面量序列化（memory_store / admin_ui 共用） | ✅ 接入（2026-05-18） |
| `src/memory_store.py` | 自搭记忆栈 ORM：`memories` 表（含 P1-5 `valid_from / valid_to`、P0-1 `entities[]`、P0-4 `source_episode_id`）+ `episodes` 表（P0-4 raw turns provenance）+ `_ensure_v2_columns` 启动时 ALTER 兼容老库 | ✅ 接入（2026-05-18→21） |
| `src/memory_prompts.py` | LLM render helpers（抽取 / 冲突检测 5.1 / 反验证 5.2 / Auto Dream 5.3 / Override Dream / Skill Dream / **insight 生成 P1-6**）；prompt 文本抽到 `prompt/memory_*.md`（2026-05-21） | ✅ 接入（2026-05-18→21） |
| `src/memory.py` | **Hot path**：recall（cosine + ngram + entity 三路 RRF 融合 P0-1 + 三因子 ranker rel/imp/rec P0-2 + bi-temporal valid_to 过滤 P1-5）+ 5.2 同步反验证；**Background**：note_turn / maybe_flush（写 episode + 抽取入库 + 5.1 异步冲突检测，stale 写 valid_to）+ auto_dream（5.3 三态判定）+ auto_dream_insights（P1-6 跨条目反思生成 memory_type='insight'）。recall 输出每条带形成日期，stale 不召回，to_verify 带 `[待确认]` 标记 | ✅ MVP（PRD v2 三层防线 + P0/P1 升级 2026-05-18→21） |
| `src/prompt_loader.py` | 统一 prompt 加载器（`prompt/*.md` 扁平结构 + `@lru_cache`）；改 prompt 重启即生效零代码改动 | ✅ 接入（2026-05-21） |
| `src/interests.py` | 话题抽取（轻量 LLM）+ 热度 bump/decay/top/cold | ✅ MVP |
| `src/availability.py` | 每次回消息记 (weekday, hour)；`score` 给 proactive 用 | ✅ MVP（带冷启动先验） |
| `src/emotion.py` | 聊天模式判档：casual / empathy / depth / interest | ✅ MVP |
| `src/tools.py` | Agent Reach 工具封装：URL 读取路由（Jina Reader / yt-dlp 视频）+ Jina Search 网页搜索（2026-05-19 起 mcporter/Exa/xhs 退役）| ✅ MVP |
| `src/persona.py` | traits/mood/观察/锚点动态层；flush 后增量更新 + 每日 03:07 衰减 | ✅ 接入（2026-05-06） |
| `src/clock.py` | 中文时间感字符串：`2026-05-08 周五（工作日） 14:32 下午`、`since_phrase` 体感 idle | ✅ 接入（2026-05-07） |
| `src/stickers.py` | 表情包索引：扫 `data/stickers/`、文件名当 tag、`parse_message` 切 `[sticker:tag]` 段 | ✅ 接入（2026-05-07） |
| `src/openrouter.py` | OpenAI 兼容 httpx 客户端；主聊天（LLM_PROVIDER=openrouter，2026-05-10 起）+ `scripts/eval_*`；走 Clash 代理 | ✅ 接入（2026-05-08） |
| `src/prompts.py` | 装配 system prompt（baseline + memory + interests + emotion directive + role discipline + tool ctx + per-user overrides）。文本走 `prompt_loader` 从 `prompt/chat_*.md` 加载 | ✅ MVP（prompt 抽离 2026-05-21） |
| `src/rhythm.py` | 剥 markdown + 按标点切短 + 打字模拟 | ✅ MVP |
| `src/agent.py` | turn 流水线（含 vision multimodal、表情包发送）+ `generate_opener` + `record_proactive_message`（proactive/welcome/triggered_reach 直发后写 `_recent` 让下轮上下文看见）+ `pop_pending_reach_for_merge`（active trigger 暂存内容拼进 user 消息）；`_recent` 持久化到 `data/recent.json`（重启接续短期上下文） | ✅ MVP |
| `src/scheduler.py` | APScheduler 八个 job：decay/memu_flush/proactive/persona_consolidate (03:07)/auto_dream (03:13)/triggered_reach (1min)/pending_reach_overdue (1min)/daily_cleanup (04:23 清 audit + wipe_backup)。所有 job 配 `max_instances=1 + coalesce=True + misfire_grace_time`。auto_dream per-user 5 段（auto_dream / overrides / insights / form_ideas / **self_iterate** L4）+ 全局 1 段 (skills) | ✅ MVP |
| `src/agent_ideas.py` | bot 凌晨自主形成"想做的事" pool（airi `come_up_ideas` 借鉴，2026-05-25）：`form_ideas` 让 reflection LLM (opus) 看最近事实写 0-5 条 idea；`list_pending` proactive 决策时拉 top-3；`mark_idea_used` 采纳后落 used；`expire_old_ideas` 7 天兜底 | ✅ 接入（2026-05-25）|
| `src/agent_self.py` | **L4 自治**（2026-05-28）：bot 在 dream cron 跑 reflection LLM (opus) 自改 per-user prompt / skill / 写 issue。6 工具 + 硬护栏（PROTECTED 片段不可删）+ rate limit（5 edits/run, 3 改/prompt/周）+ rollback。详见 `document/agent-self-iterate.md` | ✅ 接入（2026-05-28）|
| `src/bot.py` | 主 bot：邀请码准入门、命令 `/start /myid /memory /invite /users`、激活后调 `agent.generate_welcome` 发开场白；text + photo handler；`send_sticker` 回调 | ✅ MVP |
| `src/main.py` | 统一启动/关停（embed_server + prod bot + 可选 test bot + scheduler）；`DEV_SKIP_PROD_BOT=1` 时跳过 prod bot 让本地不抢云端 polling | ✅ MVP |
| `src/admin_ui.py` | 记忆浏览/编辑 Web UI（FastAPI :18081）；HMAC cookie session（无密码，靠 Telegram `/memory` 一键登录链接）；按 viewer 区分（admin 看全部 + 下拉切换、普通用户只看自己）；移动端卡片自适应；五个 tab：记忆项 / 图谱 / 调教（pending/active overrides + skill + 兴趣 + **Prompt 文件 per-user 覆写**）/ **Agent 自治**（self-edits + issues inbox + 手动触发，2026-05-28）/ 审计 | ✅ MVP |
| `src/agent.py::generate_welcome` | 用户邀请码激活后立刻生成的"拉对方进对话"第一条消息 | ✅ 接入（2026-05-13） |
| `src/feedback_prompts.py` / `src/feedback_agent.py` | Sonnet 子 agent + skill 库（仓库语义）——监听用户偏好/不满/能力诉求信号，沉淀 prompt_overrides；硬护栏双层防 jailbreak；capability_request 走 skill_creator 输出 trigger-based 指令 | ✅ 接入（2026-05-19） |
| `src/triggered_reach.py` | active trigger 通道：cron 定时扫 → sonnet 判 condition + 生成消息 → user 在聊就暂存等下轮融入，否则直发；不走 proactive 冷却 | ✅ 接入（2026-05-19） |

## 数据流 & 存储

- **SQLite（`data/app.sqlite`）**：业务状态。**多用户化后所有表都带 `user_id BIGINT`**。
  - `interests(user_id, topic, heat, last_touch)` —— 复合 PK `(user_id, topic)`
  - `reply_samples(id, user_id, ts, weekday, hour, replied_within_sec)`
  - `last_interaction(user_id PK, ts)`
  - `proactive_fires(id, user_id, ts, why, user_probably_doing, opener_angle, opener_text, mode, platform)` —— `mode` ∈ {topic_chat, share_discovery}；`platform` ∈ {xhs, bili, web, NULL}（share_discovery 通道用，xhs/bili 各日 1 条独立配额）
  - `persona_snapshots(id, user_id, ts, payload_json)`
  - `users(chat_id PK, status, created_at, note, webui_password)` —— 注册用户表
  - `invite_codes(code PK, created_by, created_at, used_by, used_at)` —— 邀请码
  - `prompt_overrides(user_id, text, reason, source_skill_id, risk_level, status, trigger_kind, cron_schedule, condition_prompt, last_fired_at, ...)` —— per-user 偏好沉淀（含 active trigger 字段）
  - `skills(name, summary, body, embedding, created_by, usage_count, ...)` —— 跨用户复用的 prompt 片段库（仓库语义，含 `skill_creator` meta-skill）
  - `pending_reach_messages(user_id, override_id, message, expected_send_after, status, ...)` —— active trigger 暂存的待主动发消息
- **自搭记忆栈**（postgres + pgvector + pg_trgm，2026-05-18 替换原 memU SDK）：长期记忆
  - 每 6 轮或 15 分钟 flush rolling buffer 成 `data/memu_buffer/conv_*.json` + `episodes` 表行（P0-4 provenance），调 `_extract_items`（LLM `MEMU_CHAT_MODEL`，默认 deepseek-v4-flash via OpenRouter）输出 profile/event/entities → `_persist_items`（embedding + INSERT 到 `memories` 表，带 `source_episode_id` / `entities` / `valid_from`）
  - 每条用户消息到达时 `memory.recall(uid, query)` 做主动召回（**P0-1 hybrid retrieval**：cosine + ngram(ILIKE) + entity 三路 RRF 融合，**P0-2 三因子 ranker** rel/imp/rec 加权，**P1-5 valid_to 过滤** 排除已失效）
  - **PRD v2 三层防线 + P1-6 insight + agent_ideas**：5.1 写入冲突检测（异步，stale 时写 valid_to）+ 5.2 召回反验证（同步阻塞 + 30min cooldown）+ 5.3 Auto Dream（03:13 cron 5 段：三态判定 / override 整理 / insight 生成（含 cosine 去重）/ **agent_ideas form_ideas** / skill 库整理）；详见 `memory-stack.md`
  - 容器：`docker memu-postgres`（pgvector + pg_trgm，名字沿用旧 memU 时代不改），`localhost:5432/memu`
- **静态资源**：
  - `data/stickers/*.{jpg,png,gif,webp}` — 表情包，文件名（去后缀）当 tag
  - `data/eval/run_<ts>.{jsonl,md,scores.jsonl,scores.md}` — 模型评测产物（`scripts/eval_*`）
  - `data/recent.json` — `_recent` 持久化（dict[uid, [msgs]]，每用户最近 12 轮；重启接续）
  - `data/audit.jsonl` — 审计事件流（每条带 `user_id` 字段；admin UI 按 viewer 过滤）
  - `data/.webui_secret` — webUI session 共享 HMAC 密钥（bot 进程铸 token，admin 进程验签；首启者写盘其他读）
- **prompt 文件夹**（2026-05-21 抽离）：
  - `prompt/system_baseline.md` — persona baseline（即原 `System Prompt v0.0.1.md`）
  - `prompt/{memory,feedback,chat,emotion,persona,proactive,agent,interests}_*.md` — 24 个 LLM prompt（含 share_discovery 通道用的 `proactive_share_select.md` + `chat_proactive_opener_share_ctx.md`，2026-05-21）
  - 加载方式：`src.prompt_loader.load(name)` 带 `@lru_cache`；改文件重启即生效

## 扩展点（为下一期明确预留）

见 `document/extension-points.md`。
