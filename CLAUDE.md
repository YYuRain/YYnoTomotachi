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
| `src/memory_store.py` | 自搭记忆栈：`memories` 表 ORM + engine + pgvector 索引 |
| `src/memory_prompts.py` | LLM 抽取 prompt（profile / event 一次性 JSON 输出） |
| `src/memory.py` | recall（pgvector cosine RAG）+ note_turn（短期 buffer）+ maybe_flush（抽取入库） |
| `src/interests.py` | 话题热度 bump/decay/top |
| `src/availability.py` | 用户活跃时段学习 + score |
| `src/emotion.py` | 四档聊法判断：casual/empathy/depth/interest |
| `src/tools.py` | Agent Reach 工具：URL读取/xhs搜索/Exa搜索 |
| `src/proactive.py` | 主动搭话：硬门 + LLM 软门 + 每日限额 |
| `src/persona.py` | 人格演化：traits/mood/观察/锚点；flush 后增量更新 + 每日 03:07 衰减 |
| `src/prompts.py` | system prompt 装配（含四档情绪指令：empathy/depth/interest/casual） |
| `src/rhythm.py` | 拆短句 + 打字模拟 |
| `src/agent.py` | 对话 turn 流水线（吃 `user_id`）+ `generate_opener` + `generate_welcome`（新人激活后开场白）；`_recent_per_user` dict 持久化 `data/recent.json` |
| `src/scheduler.py` | APScheduler：decay/memu_flush/proactive/persona_consolidate |
| `src/bot.py` | 主 bot：邀请码门 + 命令 `/start /myid /memory /invite /users`；激活成功后调 `agent.generate_welcome` 发开场白 |
| `src/main.py` | 统一启停 |
| `src/admin_ui.py` | 记忆浏览/编辑 Web UI（FastAPI :18081）；HMAC cookie session（无密码登录，靠 `/memory` 给 token URL）；按 viewer 区分（admin 看全部 + 下拉切；普通用户只看自己）；移动端卡片自适应；只剩「记忆项」+「审计」两个 tab |
| `src/clock.py` | 中文时间感字符串（now_signal / since_phrase） |
| `src/stickers.py` | 表情包：扫 `data/stickers/`、文件名当 tag、parse `[sticker:tag]` 标记 |
| `src/openrouter.py` | OpenAI 兼容客户端，主聊天（LLM_PROVIDER=openrouter）+ `scripts/eval_*`；走 Clash 代理 |

## 数据存储

- **SQLite** `data/app.sqlite`：interests / reply_samples / last_interaction / proactive_fires / persona_snapshots（都带 `user_id`）+ `users` / `invite_codes`
- **记忆栈** via **Postgres + pgvector**：本地 `localhost:5432/memu`（容器 `memu-postgres`，名字沿用旧名以免 compose 改动）；compose 内是服务名 `postgres:5432`。表 `memories`（schema 见 `src/memory_store.py`）
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

**test bot**（如启用）：
- `/become <label>` 选虚拟身份（label = alice/bob/数字）
- `/start <code>` 走完整邀请流程激活当前虚拟身份
- `/whoami` 看当前虚拟 + 真实 chat_id
- `/clear` 清空当前虚拟身份的所有数据（SQLite + memU + 内存），邀请码归还
- `/memory` 拿当前虚拟身份的 webUI 链接

## Agent Reach 工具

依赖 CLI 工具（bot 不用重启即可验证）：

- `xhs`：`/Users/yangyu/.local/bin/xhs`（pipx）
- `mcporter`：`/Users/yangyu/.nvm/versions/node/v24.15.0/bin/mcporter`（nvm）

工具失败静默跳过，不影响聊天。详见 `document/agent-reach-integration.md`。

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
| `document/memu-setup.md` | memU 配置 + Postgres 持久化 |
| `document/extension-points.md` | 扩展点（情绪/人格演化/图片/表情包/评测/多用户都已落地） |
| `document/deployment.md` | 云部署（Docker Compose + mihomo + cloudflared + 多用户测试流程） |
| `document/persona-evolution.md` | 人格演化：traits/mood/observations/milestones 设计 |
| `document/eval-system.md` | 模型评测系统（OpenRouter 多模型横向 + LLM judge） |
| `document/session-log.md` | 搭建流水 |
