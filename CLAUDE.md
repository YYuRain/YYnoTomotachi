# AIDemo — Companion Agent

陪伴型 Telegram agent（非助手/非咨询师）。**多用户邀请制**（5–50 人），单实例。
本地开发或腾讯香港云 Docker Compose 部署。

## 仓库

GitHub（私有）：https://github.com/YYuRain/YYnoTomotachi，branch = `main`

```bash
git add -p && git commit -m "..." && git push -c http.proxy=http://127.0.0.1:7897   # 本地需 Clash
```

`.dockerignore` 已让 `.env` / `data/` / `.git` 不进镜像；`.gitignore` 排除：`.env`、`data/`、`.venv/`、`.obsidian/`、`.claude/`、`Pasted image *.png`、`*.bak.*`

---

## 启动

**本地开发**：
```bash
docker start memu-postgres        # 记忆栈持久化（postgres + pgvector，容器名沿用旧名）
.venv/bin/python -m src.main      # prod bot (+ test bot 若 TEST_BOT_TOKEN 设了) + embed :18080 + scheduler
.venv/bin/python -m scripts.admin # 记忆浏览 UI :18081，可选
```

**云部署**（腾讯 HK 当前生产）：
```bash
docker compose build && docker compose up -d
docker compose logs -f bot   # 看 ready
```

详见 `document/running.md`（本地）和 `document/deployment.md`（云端 + mihomo + cloudflared）。

## 关键环境变量（`.env`）

| 变量 | 说明 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | 主 bot token |
| `ADMIN_CHAT_ID` | admin 用户的 chat_id（生成邀请码、看 /users）。兼容旧 `TELEGRAM_ALLOWED_CHAT_ID` |
| `TEST_BOT_TOKEN` | 可选——第二个 bot token（多用户模拟，`/become` 切虚拟身份） |
| `TELEGRAM_PROXY` | 本机 `http://127.0.0.1:7897`（Clash）；HK 云直连留空；compose 内 bot 设 `http://mihomo:9981` |
| `ADMIN_UI_USER` / `ADMIN_UI_PASSWORD` | 备用 admin 凭证（主登录路径已不用密码，靠 Telegram `/memory` 一键登录链接） |
| `MINIMAX_API_KEY` | MiniMax key |
| `MINIMAX_GROUP_ID` | MiniMax group id |
| `MINIMAX_CHAT_MODEL` | 默认 `MiniMax-M2` |
| `LLM_PROVIDER` | `openrouter`（当前）/ `minimax` / `anthropic` |
| `OPENROUTER_MODEL` | 主聊天模型；当前 `anthropic/claude-sonnet-4.6`（之前 `moonshotai/kimi-k2.6`，2026-05-12 切） |
| `ANTHROPIC_API_KEY` | Claude key（LLM_PROVIDER=anthropic 时） |
| `ANTHROPIC_MODEL` | 默认 `claude-opus-4-6` |
| `ANTHROPIC_MODEL_AUX` | 默认 `claude-sonnet-4-6`（辅助 tier） |
| `MEMU_DB_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/memu`（自搭记忆栈也用这个 env，容器/库名都沿用旧名） |
| `MEMU_CHAT_MODEL` | 记忆抽取模型（OpenAI 兼容，走 OpenRouter）；当前 `deepseek/deepseek-v4-flash` |
| `JINA_API_KEY` | Jina Reader 鉴权 key（网页正文读取，匿名易被 401）。免费注册：https://jina.ai/reader/ |
| `OPENROUTER_API_KEY` | OpenRouter key（主聊天 + `scripts/eval_*`） |
| `OPENROUTER_BASE_URL` | 默认 `https://openrouter.ai/api/v1` |
| `EVAL_MODELS` | 评测候选模型，逗号分隔，`<provider>/<model_id>` 格式 |
| `HF_HUB_OFFLINE` | `1`（离线用缓存模型，避免超时） |
| `TRANSFORMERS_OFFLINE` | `1` |
| `DEV_SKIP_PROD_BOT` | dev 用——`1` 时本地只跑 test bot 不跑 prod bot，避免和云上同 token 抢 polling（409） |
| `ADMIN_UI_BASE_URL` | dev 用——本地不跑 cloudflared 时设 `http://127.0.0.1:18081`，让 test bot 的 `/memory` 拿到的链接指向本地 admin UI |

## 模块地图

| 文件 | 一句话 |
|------|--------|
| `src/config.py` | `.env` → Settings |
| `src/storage.py` | SQLite 表定义；多用户起所有表带 `user_id`，加 `users` / `invite_codes` |
| `src/users.py` | 邀请码 + 准入 + `wipe_user`（test bot /clear）；webUI 共享 HMAC token（`make_session_token`/`verify_session_token`，密钥落盘 `data/.webui_secret`） |
| `src/test_bot.py` | 可选——TEST_BOT_TOKEN 设了启用；`/become <label>` 选虚拟 user_id，走完整邀请码流程，`/clear` 清盘 |
| `src/llm.py` | LLM 统一门面（openrouter/minimax/anthropic 分发，支持 tier） |
| `src/minimax.py` | MiniMax chat/chat_json/embed |
| `src/embed_client.py` | 本地 :18080 embed_server 客户端 + pgvector 字面量序列化（memory_store / admin_ui 共用） |
| `src/embed_server.py` | 本地 bge-small-zh embedding shim（:18080） |
| `src/memory_store.py` | 自搭记忆栈：`memories` (含 P1-5 valid_from/valid_to) + `episodes` (P0-4 provenance) 表 ORM + engine + pgvector / pg_trgm 索引 |
| `src/memory_prompts.py` | LLM render helpers（抽取 / 冲突检测 5.1 / 反验证 5.2 / Auto Dream 5.3 / **insight 生成 P1-6**），prompt 文本在 `prompt/memory_*.md` |
| `src/memory.py` | **Hot path**: recall（cosine + ngram + entity 三路 RRF 融合 P0-1，叠加三因子 ranker rel+imp+rec P0-2，bi-temporal 过滤 valid_to P1-5）+ 5.2 同步反验证；**Background**: note_turn（短期 buffer）+ maybe_flush（写 episode + 抽取入库 + 5.1 异步冲突检测，stale 写 valid_to P1-5）+ auto_dream（5.3 批量整理）+ **auto_dream_insights**（P1-6 跨条目反思，生成 memory_type='insight'） |
| `src/feedback_prompts.py` | Feedback sub-agent 的 SCREEN（aux 粗筛）+ JUDGE（sonnet 精判，含硬护栏）+ SKILL_CREATOR（capability_request 转 trigger 指令）prompt |
| `src/feedback_agent.py` | flush 后异步 fire；监听偏好/不满/能力诉求信号，沉淀 prompt_overrides + skill 库（仓库语义；详见 `document/feedback-agent.md`）|
| `src/triggered_reach.py` | active trigger 通道：每分钟扫 cron + sonnet 判 condition + user 在聊就暂存等下轮融入、否则直发；不走 proactive 冷却 |
| `src/interests.py` | 话题热度 bump/decay/top |
| `src/availability.py` | 用户活跃时段学习 + score |
| `src/emotion.py` | 四档聊法判断：casual/empathy/depth/interest |
| `src/tools.py` | Agent Reach 工具：URL 读取（Jina）/ 全网搜索（Jina）/ 小红书（xhs Python SDK，需 cookie）/ GitHub（REST API anon）/ YouTube + B站（yt-dlp 域名自动路由） |
| `src/proactive.py` | 主动搭话：硬门 + LLM 软门 + 每日限额 |
| `src/persona.py` | 人格演化：traits/mood/观察/锚点；flush 后增量更新 + 每日 03:07 衰减 |
| `src/prompts.py` | system prompt 装配（含四档情绪指令：empathy/depth/interest/casual + per-user prompt overrides 段尾追加 + 联网能力声明） |
| `src/rhythm.py` | 拆短句 + 打字模拟 |
| `src/agent.py` | 对话 turn 流水线（吃 `user_id`）+ `generate_opener` + `generate_welcome`（新人激活后开场白）；`_recent_per_user` dict 持久化 `data/recent.json` |
| `src/scheduler.py` | APScheduler 七个 job：decay/memu_flush/proactive/persona_consolidate (03:07)/auto_dream (03:13)/triggered_reach (1min)/pending_reach_overdue (1min) |
| `src/bot.py` | 主 bot：邀请码门 + 命令 `/start /myid /memory /invite /users`；激活成功后调 `agent.generate_welcome` 发开场白 |
| `src/main.py` | 统一启停 |
| `src/admin_ui.py` | 记忆浏览/编辑 Web UI（FastAPI :18081）；HMAC cookie session（无密码登录，靠 `/memory` 给 token URL）；按 viewer 区分（admin 看全部 + 下拉切；普通用户只看自己）；移动端卡片自适应；四个 tab「记忆项」「图谱（D3 force-directed）」「调教（pending/active overrides + skill 库）」「审计」 |
| `src/clock.py` | 中文时间感字符串（now_signal / since_phrase） |
| `src/stickers.py` | 表情包：扫 `data/stickers/`、文件名当 tag、parse `[sticker:tag]` 标记 |
| `src/openrouter.py` | OpenAI 兼容客户端，主聊天（LLM_PROVIDER=openrouter）+ `scripts/eval_*`；走 Clash 代理 |

## 数据存储

- **SQLite** `data/app.sqlite`：interests / reply_samples / last_interaction / proactive_fires / persona_snapshots（都带 `user_id`）+ `users` / `invite_codes` + **procedural memory**（LangMem 命名）：`prompt_overrides`（per-user 偏好沉淀，含 active trigger 字段）/ `skills`（跨用户仓库 + `skill_creator` meta-skill）/ `pending_reach_messages`（active trigger 暂存）
- **记忆栈** via **Postgres + pgvector + pg_trgm**：本地 `localhost:5432/memu`（容器 `memu-postgres`，名字沿用旧名以免 compose 改动）；compose 内是服务名 `postgres:5432`。表 `memories`（带 `entities TEXT[]` / `source_episode_id UUID`）+ `episodes`（P0-4 raw turns provenance）
- **HuggingFace 模型缓存**：本地 `~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/`；镜像内烤进 `/opt/hf/bge-small-zh-v1.5/`（`EMBED_MODEL_NAME` 容器内绝对路径）
- **本地状态文件**：`data/recent.json`（dict[uid, [12 轮]]）；`data/audit.jsonl`（每条带 user_id）；`data/.webui_secret`（HMAC 共享密钥）
- **静态资源**：`data/stickers/*`（文件名当 tag）；`data/eval/run_*.{jsonl,md}`（模型评测）

## Telegram 命令

**主 bot**（所有用户）：
- `/start <code>` 邀请码激活（激活后会立刻收到 AI 生成的欢迎开场白）
- `/myid` 返回自己 chat_id
- `/memory` 拿 webUI 一键登录链接（10min 内有效；admin 进去看全部，普通用户只看自己）

**主 bot**（admin 专属，对应 `ADMIN_CHAT_ID`）：
- `/invite [n]` 生成 n 个邀请码（默认 1）
- `/users` 看注册用户列表
- `/proactive_test` 立刻触发一次 proactive opener（在 bot 主进程内调 generate_opener + deliver + record_proactive_message，验证主动消息通道 + `_recent` 写入）

**test bot**（如启用）：
- `/become <label>` 选虚拟身份（label = alice/bob/数字）
- `/start <code>` 走完整邀请流程激活当前虚拟身份
- `/whoami` 看当前虚拟 + 真实 chat_id
- `/clear` 清空当前虚拟身份的所有数据（SQLite + memU + 内存），邀请码归还
- `/memory` 拿当前虚拟身份的 webUI 链接

## 记忆架构 PRD v2（2026-05-18 起）

memory 不只是 RAG。三层防线让记忆"知道自己不确定"：

- **5.1 写入冲突检测**（异步）：每条新事实 flush 入库后，对它语义最近的 top-5 旧 profile 跑一次 LLM 影响分析。verdicts ∈ {still_valid / to_verify / stale}；后两者写老条目的 `status` + 把新事实 id append 到老条目的 `depends_on` 数组（去重）。同 batch 内同伴互排除避免互判。
- **5.2 召回反验证**（同步阻塞 recall）：recall 命中 `to_verify` 条目时，同步跑 LLM 反向验证（输入条目+depends_on 上游+当前 user query）。verdicts ∈ {still_valid / uncertain}（不能直接判 stale 防错杀）。30 min cooldown 用 `last_verified_at` 限速。still_valid → 升回 confirmed；uncertain → 保留 to_verify 仅打戳。
- **5.3 Auto Dream**（03:13 cron 批量）：对每个用户的所有 to_verify 条目跑一遍三态判定（still_valid / uncertain / stale）。后台无打扰，可激进直判 stale。LLM 拿 deps 上游 + top-5 confirmed 邻居作综合上下文。

`memories` 表加列：`status` (confirmed/to_verify/stale)、`confidence`、`last_verified_at`、`depends_on UUID[]`。
recall 返回时 `stale` 完全过滤、`to_verify` 带 `[待确认]` 前缀让主 LLM 自己拿捏。

详见 `me/prd_memory.md`（PRD 原文）+ `document/memory-stack.md`（实现细节）。

## Hot path / Background 分层（P0-3 命名一统，借自 LangMem，2026-05-20）

记忆栈两条路径明确分开：
- **Hot path**（同步阻塞 turn）：`recall` 三路 RRF 融合 + 5.2 反验证。要求 < 200ms。
- **Background**（异步 / cron）：flush 后的抽取入库、5.1 冲突检测、persona 演化、feedback agent、5.3 Auto Dream、active trigger 扫描。允许慢。

**Procedural memory** = `prompt_overrides` + `skills` + `skill_creator` meta-skill —— 跟 `memories`（semantic/episodic）平级的一类记忆，admin UI 走「调教」tab。

## Agent Reach 工具（2026-05-21 起，借 [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) 选型）

容器内 binary 已全装好（Dockerfile）：

- `read_url(url)` — Jina Reader (`r.jina.ai/<url>`)，`JINA_API_KEY` Bearer
- `search_web(query)` — Jina Search (`s.jina.ai/?q=`)，通用全网搜索
- `search_xhs(keyword)` — `xhs` Python SDK (XhsClient)，需 cookie（`data/.xhs-cookie/cookies.txt` 或 env `XHS_COOKIE`）；admin UI audit 看 `xhs search 失败：...` 排查
- `read_github(owner/repo)` — GitHub REST API anon（`api.github.com/repos/...`）；rate limit 60/h；不需要 GH_TOKEN
- URL 自动路由 `_fetch_one_url`：xiaohongshu.com → xhs SDK；bilibili / youtube → yt-dlp；其它 → Jina Reader
- 历史的 mcporter / Exa 已彻底退役（2026-05-21 删 `_exa_fetch_url` 和所有 fallback）；gh CLI 装过又删（v2 强制 auth 不适合 anon API）

工具失败静默跳过，不影响聊天。`prompt/agent_tool_detect.md` 运行时拼今天日期防 LLM 写错年份。
详见 `document/agent-reach-integration.md`。

## 常见问题速查

`document/running.md` 有完整故障排查表。常见：

- **Telegram 超时**：Clash 未开
- **502 错误**：NO_PROXY 未生效，检查 `main.py::_purge_proxy_env`
- **端口 18080 占用**：`pkill -f "src\.main"` + `pkill -f uvicorn`（embed shim）
- **记忆不生效**：postgres 容器未起 → `docker start memu-postgres`（容器名沿用旧名）

## 文档索引

| 文档 | 内容 |
|------|------|
| `document/overview.md` | 架构总览 + 模块职责 |
| `document/running.md` | 启停/日志/常见问题 |
| `document/dialog-tuning-log.md` | 对话风格调优记录（最新在上） |
| `document/agent-reach-integration.md` | 工具集成：xhs/Exa/yt-dlp |
| `document/minimax-integration.md` | MiniMax 接入与坑点 |
| `document/memory-stack.md` | 自搭记忆栈（postgres+pgvector）+ PRD v2 三层防线实现 |
| `document/feedback-agent.md` | per-user prompt overrides + Feedback Sub-Agent + skill 库 |
| `document/memu-setup.md` | （已归档）memU SDK 时代配置；自搭栈替换前的踩坑参考 |
| `document/extension-points.md` | 扩展点（情绪/人格演化/图片/表情包/评测/多用户都已落地） |
| `document/deployment.md` | 云部署（Docker Compose + mihomo + cloudflared + 多用户测试流程） |
| `document/persona-evolution.md` | 人格演化：traits/mood/observations/milestones 设计 |
| `document/eval-system.md` | 模型评测系统（OpenRouter 多模型横向 + LLM judge） |
| `document/session-log.md` | 搭建流水 |
