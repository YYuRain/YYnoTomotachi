# 架构总览

> 对应 PRD：`prd/0.md`；实施计划：`~/.claude/plans/prd-0-md-robust-lobster.md`。
> 本项目是 MVP——追求"能跑 + 架构可扩展"，不追求功能完整。

## 一句话

> 一个陪伴型（不是助手、不是心理咨询师）的 Telegram chat agent，会主动找人、像真人聊、
> 会自己想起记忆、兴趣热度会涨会退、能看图发表情包。MiniMax 或 Claude 做 LLM，本地 bge-small-zh 做 embedding，memU 做长期记忆。

## 运行时结构

```
┌──────────────── 单进程 asyncio loop ─────────────────┐
│                                                      │
│  Telegram polling ──► bot._on_message  (text)        │
│                  └──► bot._on_photo    (image)       │
│                              │                       │
│                              ▼                       │
│                       agent.handle_user_message      │
│                       ├─┐ emotion.detect (mode 判档) │
│                       ├─┼ memory.recall   (memU)     │ 四者并行
│                       ├─┼ interests topics            │
│                       ├─┘ tools.fetch / search       │
│                       ├─► clock.now_signal (时间感前缀) │
│                       ├─► interests.bump  (SQLite)   │
│                       ├─► prompts.build_system_prompt │
│                       │     (含 persona/sticker tags 动态段)│
│                       ├─► llm.chat (multimodal blocks│
│                       │      → anthropic 走 vision)   │
│                       ├─► stickers.parse_message (拆 [sticker:tag] 段) │
│                       ├─► rhythm.deliver → send      │
│                       │   send_sticker → 发 Telegram 图│
│                       └─► async: memorize / availability │
│                                                      │
│  本地服务（main.py 内 asyncio task）                   │
│   ├─ embed_server  :18080  bge-small-zh embedding shim │
│   └─ llm_proxy     :18082  memU→MiniMax 中间 strip-think│
│                                                      │
│  APScheduler                                         │
│   ├─ decay_job           每 1h  interests.decay_tick │
│   ├─ memu_flush_job      每 15m memory.maybe_flush   │
│   │                            └─► persona.update_state │
│   ├─ persona_consolidate 每日 03:07 traits 衰减/清旧观察│
│   └─ proactive_job       每 25m 看 idle + score (软门 LLM 判断夜间) │
│                                    └─► generate_opener│
│                                    └─► rhythm.deliver │
└──────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 职责 | 状态 |
|------|------|------|
| `src/config.py` | `.env` → `Settings` dataclass（含 telegram_proxy / embed_server 字段） | ✅ MVP |
| `src/storage.py` | SQLite 表：interests / reply_samples / last_interaction / persona_snapshots(预留) | ✅ MVP |
| `src/minimax.py` | 聊天走 OpenAI 兼容端点；embed 走 MiniMax 原生格式；自动剥 `<think>` | ✅ MVP |
| `src/embed_server.py` | 本地 OpenAI 兼容 embedding shim：FastAPI + sentence-transformers (bge-small-zh) :18080 | ✅ MVP |
| `src/llm_proxy.py` | 本地 strip-think shim :18082。memU 内部抽取的 LLM 走这里，剥 `<think>` 后再回 memU（防 think 污染 memory_categories.summary） | ✅ 接入（2026-05-07） |
| `src/memory.py` | memU MemoryService 封装；chat=本地 shim:18082→MiniMax，embedding=本地 shim:18080；rolling buffer → JSON → memorize | ✅ MVP |
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
| `src/agent.py` | turn 流水线（含 vision multimodal、表情包发送）+ `generate_opener` | ✅ MVP |
| `src/scheduler.py` | APScheduler 四个 job：decay/memu_flush/proactive/persona_consolidate | ✅ MVP |
| `src/bot.py` | `python-telegram-bot`，白名单单用户；text + photo handler；`send_sticker` 回调 | ✅ MVP |
| `src/main.py` | 统一启动/关停（embed_server + llm_proxy + bot + scheduler） | ✅ MVP |
| `src/admin_ui.py` | 记忆浏览/编辑 Web UI（FastAPI :18081） | ✅ MVP |

## 数据流 & 存储

- **SQLite（`data/app.sqlite`）**：业务状态
  - `interests(topic, heat, last_touch)`
  - `reply_samples(id, ts, weekday, hour, replied_within_sec)`
  - `last_interaction(id=1, ts)`
  - `proactive_fires(id, ts, why, user_probably_doing, opener_angle, opener_text)` —— AI 主动开场记录
  - `persona_snapshots(id, ts, payload_json)` —— 人格动态层（traits / mood / observations / milestones）；每次 memU flush 后追加一行，03:07 consolidate 也写一行
- **memU**（默认 `postgres`，2026-04-27 切）：长期记忆
  - 每 6 轮或 15 分钟把 rolling buffer flush 成 `data/memu_buffer/conv_*.json`，调 `service.memorize`
  - 每条用户消息到达时 `service.retrieve` 做主动召回
  - 容器：`docker memu-postgres`（pgvector），`localhost:5432/memu`
  - **memU 内部 LLM 调用走本地 :18082 shim**（`src/llm_proxy.py`）剥 `<think>` 后再回 memU
- **静态资源**：
  - `data/stickers/*.{jpg,png,gif,webp}` — 表情包，文件名（去后缀）当 tag
  - `data/eval/run_<ts>.{jsonl,md,scores.jsonl,scores.md}` — 模型评测产物（`scripts/eval_*`）

## 扩展点（为下一期明确预留）

见 `document/extension-points.md`。
