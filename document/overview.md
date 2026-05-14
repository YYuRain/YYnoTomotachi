# 架构总览

> 对应 PRD：`prd/0.md`；实施计划：`~/.claude/plans/prd-0-md-robust-lobster.md`。
> 本项目是 MVP——追求"能跑 + 架构可扩展"，不追求功能完整。

## 一句话

> 一个陪伴型（不是助手、不是心理咨询师）的 Telegram chat agent，会主动找人、像真人聊、
> 会自己想起记忆、兴趣热度会涨会退、能看图发表情包。OpenRouter / Claude / MiniMax 做 LLM，
> 本地 bge-small-zh 做 embedding，memU 做长期记忆。**多用户邀请制**（5–50 人），单实例。

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
│   ├─ embed_server  :18080  bge-small-zh embedding shim │
│   └─ llm_proxy     :18082  memU 上游 shim（OpenRouter/MiniMax 路由+strip-think）│
│                                                      │
│  APScheduler（4 job 各自 fan-out 遍历 users.list_active() with Sem(5)） │
│   ├─ decay_job           每 1h  interests.decay_tick(uid) per user │
│   ├─ memu_flush_job      每 15m memory.maybe_flush(uid) per user │
│   │                            └─► persona.update_state(uid) │
│   ├─ persona_consolidate 每日 03:07 (CST) per user 衰减/清旧观察│
│   └─ proactive_job       每 25m per user 软门 LLM 判断 │
│                                    └─► generate_opener(uid) │
│                                    └─► bot.make_send_and_typing(uid) │
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
| `src/storage.py` | SQLite 表：interests / reply_samples / last_interaction / proactive_fires / persona_snapshots / users / invite_codes（多用户起所有表都有 user_id） | ✅ MVP |
| `src/users.py` | 邀请码生成/redeem、用户列表、admin 判定、`wipe_user`（test bot /clear 用）、webUI 共享 HMAC token 工具 | ✅ 接入（2026-05-13） |
| `src/test_bot.py` | 可选第二个 Telegram bot（`TEST_BOT_TOKEN`）：`/become <label>` 选虚拟 user_id（与 chat_id 解耦），完整邀请码激活流程，`/clear` 清盘——同 telegram 账户能扮演多个用户 | ✅ 接入（2026-05-13，可选） |
| `src/minimax.py` | 聊天走 OpenAI 兼容端点；embed 走 MiniMax 原生格式；自动剥 `<think>` | ✅ MVP |
| `src/embed_server.py` | 本地 OpenAI 兼容 embedding shim：FastAPI + sentence-transformers (bge-small-zh) :18080 | ✅ MVP |
| `src/llm_proxy.py` | 本地 memU 上游 shim :18082。按 `MEMU_CHAT_MODEL` 自动路由：OpenRouter（带 Clash）或 MiniMax 直连；永远剥 `<think>` 防污染 `memory_categories.summary` | ✅ 接入（2026-05-07，2026-05-12 加 OpenRouter 路由） |
| `src/memory.py` | memU MemoryService 封装；chat=本地 shim:18082→上游，embedding=本地 shim:18080；rolling buffer → JSON → memorize；recall 携带形成日期 | ✅ MVP |
| `src/interests.py` | 话题抽取（轻量 LLM）+ 热度 bump/decay/top/cold | ✅ MVP |
| `src/availability.py` | 每次回消息记 (weekday, hour)；`score` 给 proactive 用 | ✅ MVP（带冷启动先验） |
| `src/emotion.py` | 聊天模式判档：casual / empathy / depth / interest | ✅ MVP |
| `src/tools.py` | Agent Reach 工具封装：URL 读取路由（xhs/B站/YouTube/通用）+ xhs 搜索（账号风控时退 Exa）+ Exa 网页搜索 | ✅ MVP |
| `src/persona.py` | traits/mood/观察/锚点动态层；flush 后增量更新 + 每日 03:07 衰减 | ✅ 接入（2026-05-06） |
| `src/clock.py` | 中文时间感字符串：`2026-05-08 周五（工作日） 14:32 下午`、`since_phrase` 体感 idle | ✅ 接入（2026-05-07） |
| `src/stickers.py` | 表情包索引：扫 `data/stickers/`、文件名当 tag、`parse_message` 切 `[sticker:tag]` 段 | ✅ 接入（2026-05-07） |
| `src/openrouter.py` | OpenAI 兼容 httpx 客户端；主聊天（LLM_PROVIDER=openrouter，2026-05-10 起）+ `scripts/eval_*`；走 Clash 代理 | ✅ 接入（2026-05-08） |
| `src/prompts.py` | 装配 system prompt + `PROACTIVE_OPENER_INSTRUCTIONS` + 表情包段 + 四档情绪指令（empathy/depth/interest） | ✅ MVP |
| `src/rhythm.py` | 剥 markdown + 按标点切短 + 打字模拟 | ✅ MVP |
| `src/agent.py` | turn 流水线（含 vision multimodal、表情包发送）+ `generate_opener`；`_recent` 持久化到 `data/recent.json`（重启接续短期上下文） | ✅ MVP |
| `src/scheduler.py` | APScheduler 四个 job：decay/memu_flush/proactive/persona_consolidate | ✅ MVP |
| `src/bot.py` | 主 bot：邀请码准入门、命令 `/start /myid /memory /invite /users`、激活后调 `agent.generate_welcome` 发开场白；text + photo handler；`send_sticker` 回调 | ✅ MVP |
| `src/main.py` | 统一启动/关停（embed_server + llm_proxy + prod bot + 可选 test bot + scheduler） | ✅ MVP |
| `src/admin_ui.py` | 记忆浏览/编辑 Web UI（FastAPI :18081）；HMAC cookie session（无密码，靠 Telegram `/memory` 一键登录链接）；按 viewer 区分（admin 看全部 + 下拉切换、普通用户只看自己）；移动端卡片自适应 | ✅ MVP |
| `src/agent.py::generate_welcome` | 用户邀请码激活后立刻生成的"拉对方进对话"第一条消息 | ✅ 接入（2026-05-13） |

## 数据流 & 存储

- **SQLite（`data/app.sqlite`）**：业务状态。**多用户化后所有表都带 `user_id BIGINT`**。
  - `interests(user_id, topic, heat, last_touch)` —— 复合 PK `(user_id, topic)`
  - `reply_samples(id, user_id, ts, weekday, hour, replied_within_sec)`
  - `last_interaction(user_id PK, ts)`
  - `proactive_fires(id, user_id, ts, why, user_probably_doing, opener_angle, opener_text)`
  - `persona_snapshots(id, user_id, ts, payload_json)`
  - `users(chat_id PK, status, created_at, note, webui_password)` —— 注册用户表
  - `invite_codes(code PK, created_by, created_at, used_by, used_at)` —— 邀请码
- **memU**（默认 `postgres`，2026-04-27 切）：长期记忆
  - 每 6 轮或 15 分钟把 rolling buffer flush 成 `data/memu_buffer/conv_*.json`，调 `service.memorize`
  - 每条用户消息到达时 `service.retrieve` 做主动召回
  - 容器：`docker memu-postgres`（pgvector），`localhost:5432/memu`
  - **memU 内部 LLM 调用走本地 :18082 shim**（`src/llm_proxy.py`）；shim 按 `MEMU_CHAT_MODEL` 选 OpenRouter（默认 deepseek-v4-flash）或 MiniMax，永远剥 `<think>`
- **静态资源**：
  - `data/stickers/*.{jpg,png,gif,webp}` — 表情包，文件名（去后缀）当 tag
  - `data/eval/run_<ts>.{jsonl,md,scores.jsonl,scores.md}` — 模型评测产物（`scripts/eval_*`）
  - `data/recent.json` — `_recent` 持久化（dict[uid, [msgs]]，每用户最近 12 轮；重启接续）
  - `data/audit.jsonl` — 审计事件流（每条带 `user_id` 字段；admin UI 按 viewer 过滤）
  - `data/.webui_secret` — webUI session 共享 HMAC 密钥（bot 进程铸 token，admin 进程验签；首启者写盘其他读）

## 扩展点（为下一期明确预留）

见 `document/extension-points.md`。
