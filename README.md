# YYnoTomotachi

> 跑在 Telegram 里的陪伴型 agent——**不是助手、不是客服、不是心理咨询师**。
> 像一个网上的朋友：话不多，懂梗，平时挺平的，遇到喜欢的话题会变活；想起来了会自己开口；记得你说过的事但不翻旧账。

私有部署、邀请制（5–50 人），单实例。本地开发或腾讯香港云 Docker Compose 跑生产。

---

## 你会感受到什么

**像在跟人打字聊天。** 长话拆几条发，中间有停顿，每条不长。不会"首先其次最后"地写小论文。
（实现：`src/rhythm.py` 拆短句 + 按字数计算"打字"延时再 send，`split_for_chat` + `deliver`）

**它会自己找你聊。** 不是定点问候——看时间、看你历史活跃时段、看最近聊过什么，"刚好想到了"才发。每天有上限，不会刷屏。深夜也可能出现，看你平时是不是夜里活跃。
（实现：`src/proactive.py::decide` 软概率门 + LLM 判断；`src/scheduler.py` 每 25min ± 10min jitter 触发；个人活跃曲线靠 `src/availability.py` 学习）

**它会主动逛网然后分享。** 不只是"想起聊什么"——它会顺手刷小红书 / B 站 / 全网，看到觉得你会喜欢的就转给你看，像朋友群里发链接那样。每天每平台 1 条，不会刷屏。
（实现：`proactive.decide` 输出 `share_intent={platform, query}` → `_select_share_item` 调对应搜索 → LLM 看结果挑一条最 fit user 画像的 → 走 `chat_proactive_opener_share_ctx` prompt 写"朋友顺手转链接"风格开场）

**它能看见图、能读链接、能上网搜。** 你扔小红书 / B 站 / YouTube / 普通网页 URL 它会自动读进去再聊；问到"你知道 X 吗 / 帮我搜下 X"它会自己调工具搜——主 LLM 通过 native tool_use 直接控制 5 个工具：`search_web` / `search_xhs` / `search_bilibili` / `read_url` / `read_github`。每 turn 最多 1 次工具循环，搜完会标"刚搜到的"作来源（不是当作"自己知道"）。
（实现：`src/tools.py` + `src/agent.py` native tool_use；URL 自动路由 `_fetch_one_url` 按域名分发到 yt-dlp / xhs CLI / Jina Reader）

**它记得你说过的事。** 长期记忆栈跑后台异步抽取——你提过的偏好/事件/情绪自动入库；下次相关时主动召回。但不会翻旧账，刚聊过的话题它知道在退热会自然换。**记忆栈知道自己不确定**：某条事实跟新事实冲突时被标 `to_verify`，召回时主 LLM 看到 `[待确认]` 前缀自己拿捏要不要用；明显失效的标 `stale` 直接不召回。
（实现：自搭 `postgres + pgvector + pg_trgm` 记忆栈；hot path 三路 RRF 融合（cosine + ngram + entity）+ 三因子 ranker（rel/imp/rec）+ bi-temporal `valid_to` 过滤；background 三层防线 5.1/5.2/5.3 详见 `document/memory-stack.md`）

**聊久了它会变。** 你接得住毒舌它会更皮；你最近多走心它也更软；停一阵不聊又慢慢回中性。内心有 "状态"——心情、自我观察、跟你的小锚点（"5-02 那次走心夜聊"），但**不会主动跟你说**它变了。
（实现：`src/persona.py` 5 维 traits + mood + observations + milestones；flush 后异步 `update_state`，每日 03:07 cron `consolidate` 衰减）

**它会按你的偏好调整自己。** 你说"别叫我宝宝"、"少反问"、"用上海话"，它会真的改。背后有个独立的 sub-agent 听你的反馈，沉淀进 prompt overrides，下次自动生效。"以后下班前下雨提醒我带伞" 这种条件式诉求会被识别成 active trigger，到点 cron 触发判断条件再发。
（实现：`src/feedback_agent.py` flush 后异步 fire（aux 粗筛 + sonnet 精判 + 硬护栏正则）→ 落 `prompt_overrides` 表 → `prompts.build_system_prompt` 拼到 system 段尾；active trigger 走独立 `src/triggered_reach.py` 通道）

**它会发表情包。** 把图丢到 `data/stickers/`、文件名当 tag（`无奈.jpg` / `大笑.png` / `加油.gif`），它觉得"发表情比打字更对劲"时会自己挑。没图就没这功能，零侵入。

**它知道现在几点。** 星期几、什么时段、距上次聊多久全在视野——但不会蹦"现在 14:32"。该说"刚过晚饭点"就这么说。
（实现：`src/clock.py` 中文时间感字符串注入 user 消息前缀）

**多个人用同一个 bot 互不干扰。** 邀请制（5–50 人）；admin 用 `/invite` 生成邀请码、`/users` 看注册列表；每个用户的记忆 / 兴趣 / 持久化 _recent / 偏好 overrides 全部独立按 `user_id` 隔离。
（实现：`src/users.py` 邀请码 + 准入；所有 SQLite 表带 `user_id` 列；scheduler 各 job `asyncio.gather` 遍历活跃用户；可选 `TEST_BOT_TOKEN` 跑第二个 test bot 一个 telegram 账户扮演多虚拟用户调试）

**有 webUI 看记忆 + 调教。** Bot 发 `/memory` 拿一键登录链接（HMAC token URL，10 min 有效）→ 进 admin UI 五个 tab：记忆项 / 图谱（D3 force-directed depends_on 关系图）/ 调教（pending overrides 审核 + active trigger 配置 + skill 库 + 兴趣热度 + Prompt 文件 per-user 整份覆写）/ Agent 自治（L4 self-edits + issues inbox + 手动触发，2026-05-28 加）/ 审计（所有事件流）。admin 看全部 + 下拉切，普通用户只看自己。
（实现：`src/admin_ui.py` FastAPI :18081；HMAC cookie session 无密码登录）

---

## 它**不会**做的事（产品红线）

- 不端"客服腔"（不"我帮你..." / "希望对您有帮助" / "好的我来..."）
- 不嘘寒问暖（不"今天感觉怎么样"那种例行问候）
- 不过度道歉、不强行上价值
- 不每条都问句收尾（朋友说完就停了，不会每次把球踢回去）
- 不假装认识你不知道的事——搜了拿到也明说"刚搜到"，不当成自己知识
- 不打包票"我记住了 / 我以后都会..."（沉淀通道异步且可能漏，承诺管不到的事）
- 陌生人发消息 silent drop（邀请码门）

---

## 架构总览

```
Telegram ──► bot.py (邀请码门 + 命令)
              │
              ▼
         agent.handle_user_message  ──── 主 LLM (native tool_use)
              │  ├ memory.recall(uid, query) (P0 hot path: 三路 RRF + 三因子 ranker + 5.2 反验证)
              │  ├ emotion.classify (4 档：casual/empathy/depth/interest)
              │  ├ interests.bump (话题热度)
              │  ├ availability.record (活跃曲线学习)
              │  └ tools/* (search_web/xhs/bili, read_url/github) 主 LLM 自己 tool_use
              │
              ▼
         rhythm.deliver (拆短句 + typing 模拟) ──► Telegram

scheduler (APScheduler 8 个 job，全配 max_instances=1 + coalesce + misfire_grace 防重叠)
   ├ decay              每 1h 兴趣热度衰减
   ├ memu_flush         每 15m flush 短期 buffer→episode + 抽取 memory + 5.1 异步冲突检测 + feedback agent fire
   ├ persona_consolidate 每日 03:07 衰减自我观察
   ├ auto_dream         每日 03:13 (1) 三态判定 (2) override 整理 (3) insight 生成（含 cosine 去重）
   │                                  (4) agent_ideas form_ideas（airi 借鉴）(5) skill 库整理
   ├ proactive          每 25m 软概率门 + LLM decide → 三路并行：
   │                       (A) 消费 share kind idea + suggested_query → "想到 X → 顺手搜了下"双层叙事
   │                       (B) 临时 share_intent → 现搜现挑（"刚翻到一条"）
   │                       (C) 消费非 share kind idea → "想起来的事"叙事 / 续旧话题
   ├ triggered_reach    每 1m  扫 active trigger override (cron match → sonnet 判 condition → 暂存或直发)
   ├ pending_reach_overdue 每 1m  pending 超 5min 没融入 → 兜底直发
   └ daily_cleanup      每日 04:23 清 audit.YYYY-MM-DD.jsonl 30 天 + wipe_backup 7 天

云部署 (docker-compose)
   bot + admin (127.0.0.1:18081 仅 loopback) + postgres (pgvector + pg_trgm)
   + mihomo (:9981 Clash 内核出美区) + cloudflared (HTTPS tunnel 暴露 admin UI)
```

详见 `document/overview.md`。

---

## 快速跑起来

### 本地开发

```bash
# 1) 依赖（Python 3.13+）
python3 -m venv .venv
.venv/bin/pip install -e .

# 2) 配置
cp .env.example .env
# 先填 TELEGRAM_BOT_TOKEN

# 3) 拿 chat_id 当 admin
.venv/bin/python -m scripts.get_chat_id      # bot 发条消息看终端打印
# 把 ADMIN_CHAT_ID 填回 .env

# 4) 启 postgres（pgvector + pg_trgm 记忆栈）
docker start memu-postgres                    # 容器名沿用旧名

# 5) 跑
.venv/bin/python -m src.main                  # bot + embed_server :18080 + scheduler
.venv/bin/python -m scripts.admin             # （可选）admin webUI :18081
```

### 云部署（腾讯 HK 当前生产）

```bash
docker compose build && docker compose up -d
docker compose logs -f bot
```

详细启停 / 日志 / 故障排查见 `document/running.md`，云端配置见 `document/deployment.md`。

---

## 关键 .env 变量

| 变量 | 用途 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | 主 bot token |
| `ADMIN_CHAT_ID` | admin 用户 chat_id（生成邀请码 / 看 /users） |
| `TEST_BOT_TOKEN` | 可选——第二个 bot，`/become` 切虚拟身份调试多用户 |
| `TELEGRAM_PROXY` | 本机 `http://127.0.0.1:7897`（Clash）；HK 直连留空；compose 内是 `http://mihomo:9981` |
| `LLM_PROVIDER` | `openrouter`（默认）/ `anthropic` / `minimax` |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | 当前主聊天 `anthropic/claude-sonnet-4.6` |
| `MEMU_DB_URL` | 自搭记忆栈连接（容器/库名沿用旧名） |
| `MEMU_CHAT_MODEL` | 记忆抽取模型（OpenAI 兼容走 OpenRouter，当前 `deepseek/deepseek-v4-flash`） |
| `JINA_API_KEY` | Jina Reader/Search 鉴权（搜索 + URL 读取，必填） |

完整列表 + 默认值见 `.env.example` 或 CLAUDE.md "关键环境变量" 段。

---

## 模块地图（一行版）

| 文件 | 职责 |
|------|------|
| `src/main.py` | 启停（embed_server + bot + 可选 test bot + scheduler） |
| `src/bot.py` | 主 bot：邀请码门 + 命令 `/start /myid /memory /invite /users /proactive_test` |
| `src/test_bot.py` | 可选第二 bot（`/become` 切虚拟身份） |
| `src/agent.py` | 对话流水线 + `generate_opener` + `generate_welcome` + native tool_use 派发 |
| `src/memory.py` / `memory_store.py` / `memory_prompts.py` | 自搭记忆栈三层防线（recall hot path / 5.1 写入冲突 / 5.2 反验证 / 5.3 Auto Dream / P1-6 insight 含去重） |
| `src/agent_ideas.py` | bot 凌晨"想做的事" pool（airi `come_up_ideas` 借鉴）；form_ideas / list_pending / mark_idea_used / expire_old_ideas |
| `src/proactive.py` | 主动搭话：软概率门 + LLM decide → 三路并行（idea-driven share / 临时 share_intent / topic_chat） |
| `src/triggered_reach.py` | active trigger 通道：cron match → 判 condition → 暂存或直发 |
| `src/feedback_agent.py` / `feedback_prompts.py` | 偏好沉淀 sub-agent + skill 库（含 SCREEN/JUDGE 双层 + 硬护栏） |
| `src/persona.py` | 人格演化（traits / mood / observations / milestones） |
| `src/tools.py` | 5 工具 + URL 自动路由（小红书/B站/YouTube → 对应 CLI；其他 → Jina Reader） |
| `src/prompts.py` / `prompt_loader.py` | system prompt 装配 + `prompt/*.md` 扁平加载（26 个 prompt） |
| `src/storage.py` | SQLite 表 + 启动 ALTER 兜底（自动补新增 nullable 列） |
| `src/admin_ui.py` | webUI :18081（5 tab：记忆 / 图谱 / 调教 / Agent 自治 / 审计） |
| `src/scheduler.py` | APScheduler 7 个 job |
| `src/clock.py` / `stickers.py` / `rhythm.py` / `interests.py` / `availability.py` / `emotion.py` | 时间感 / 表情包 / 拆短句 / 兴趣热度 / 活跃曲线 / 情绪四档 |

---

## 数据存储

- **SQLite** `data/app.sqlite`：业务态——`interests` / `last_interaction` / `proactive_fires`（含 `mode`/`platform` 列）/ `persona_snapshots` / `users` / `invite_codes` / `prompt_overrides` (含 active trigger 字段) / `skills` / `pending_reach_messages`。所有表带 `user_id`，启动 ALTER 兜底
- **postgres + pgvector + pg_trgm**：长期记忆栈。表 `memories`（带 `entities[]` / `valid_from` / `valid_to` / `depends_on[]` / `source_episode_id`）+ `episodes`（raw turns provenance）
- **本地状态文件**：`data/recent.json`（dict[uid, [12 轮]] 重启接续）/ `data/audit.jsonl`（事件流，每条带 user_id）/ `data/.webui_secret`（HMAC 共享密钥）
- **静态资源**：`data/stickers/*` / `data/eval/run_*.{jsonl,md}` / `prompt/*.md`

---

## Telegram 命令

主 bot（所有用户）：
- `/start <code>` — 邀请码激活（激活后立刻收到 AI 生成的欢迎开场白）
- `/myid` — 返回自己 chat_id
- `/memory` — 拿 webUI 一键登录链接（10 min 有效）

主 bot（admin 专属）：
- `/invite [n]` — 生成 n 个邀请码（默认 1）
- `/users` — 注册用户列表
- `/proactive_test` — 立刻触发一次 proactive opener，验证主动通道

test bot（如启用）：
- `/become <label>` — 选虚拟身份（label = alice / bob / 数字）
- `/clear` — 清空当前虚拟身份所有数据，邀请码归还
- `/whoami` — 看当前虚拟 + 真实 chat_id

---

## 文档导航

| 文档 | 内容 |
|------|------|
| `document/overview.md` | 架构总览 + 模块职责 + 数据流 |
| `document/running.md` | 本地启停 / 日志 / 故障排查表 |
| `document/deployment.md` | 云部署（Docker Compose + mihomo + cloudflared + 多用户测试流程） |
| `document/memory-stack.md` | 自搭记忆栈 + PRD v2 三层防线 + P0/P1 升级实现 |
| `document/feedback-agent.md` | per-user prompt overrides + Feedback Sub-Agent + skill 库 |
| `document/agent-reach-integration.md` | 工具集成：5 工具 + native tool_use 架构 |
| `document/persona-evolution.md` | 人格演化 traits/mood/observations/milestones |
| `document/dialog-tuning-log.md` | 对话风格调优记录（最新在上） |
| `document/extension-points.md` | 扩展点（情绪 / 人格演化 / 图片 / 表情包 / 评测 / 多用户） |
| `document/eval-system.md` | 模型评测系统（OpenRouter 多模型横向 + LLM judge） |
| `document/minimax-integration.md` | MiniMax 接入与坑点 |
| `document/session-log.md` | 搭建流水（按时间） |
| `document/memory-decisions.md` | 记忆架构演进叙事：memU → 自搭的决策路径 + 踩坑附录 |
| `document/dream-and-override.md` | Dream + Override 设计动机：bot 自我演化的两个机制 |
| `me/` | 手写 PRD 与笔记（`prd_memory.md` / `记忆框架横纵分析.md` / `进展汇总inbox.md` 等） |
| `prompt/` | 24 个 LLM prompt（扁平结构，命名 `<module>_<name>.md`，改文件重启即生效） |

---

## 仓库

GitHub（私有）：<https://github.com/YYuRain/YYnoTomotachi>，branch = `main`

```bash
git add -p && git commit -m "..."
git -c http.proxy=http://127.0.0.1:7897 push origin main   # 本地需 Clash
```

`.dockerignore` 让 `.env` / `data/` / `.git` 不进镜像；`.gitignore` 排除 `.env` / `data/` / `.venv/` / `.obsidian/` / `.claude/` / `Pasted image *.png` / `*.bak.*`。
