# AIDemo — Companion Agent

陪伴型 Telegram agent（非助手/非咨询师）。单用户 MVP，本地部署。

## 仓库

GitHub（私有）：https://github.com/YYuRain/YYnoTomotachi，branch = `main`

```bash
git add -p && git commit -m "..." && git push
```

`.gitignore` 已排除：`.env`、`data/`、`.venv/`、`.obsidian/`、`.claude/`、`Pasted image *.png`

---

## 启动

```bash
docker start memu-postgres          # memU 持久化（postgres）
.venv/bin/python -m src.main        # bot + embed server(:18080) + scheduler
.venv/bin/python -m scripts.admin   # 记忆浏览 UI(:18081)，可选
```

详见 `document/running.md`。

## 关键环境变量（`.env`）

| 变量 | 说明 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_ALLOWED_CHAT_ID` | 白名单单用户 chat id |
| `TELEGRAM_PROXY` | `http://127.0.0.1:7897`（Clash） |
| `MINIMAX_API_KEY` | MiniMax key |
| `MINIMAX_GROUP_ID` | MiniMax group id |
| `MINIMAX_CHAT_MODEL` | 默认 `MiniMax-M2` |
| `LLM_PROVIDER` | `openrouter`（当前，kimi-k2.6）/ `minimax` / `anthropic` |
| `OPENROUTER_MODEL` | 默认 `moonshotai/kimi-k2.6`（主聊天模型） |
| `ANTHROPIC_API_KEY` | Claude key（LLM_PROVIDER=anthropic 时） |
| `ANTHROPIC_MODEL` | 默认 `claude-opus-4-6` |
| `ANTHROPIC_MODEL_AUX` | 默认 `claude-sonnet-4-6`（辅助 tier） |
| `MEMU_METADATA_PROVIDER` | `postgres` |
| `MEMU_DB_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/memu` |
| `LLM_PROXY_HOST` / `LLM_PROXY_PORT` | 默认 `127.0.0.1` / `18082`（strip-think shim） |
| `OPENROUTER_API_KEY` | OpenRouter key（主聊天 + `scripts/eval_*`） |
| `OPENROUTER_BASE_URL` | 默认 `https://openrouter.ai/api/v1` |
| `EVAL_MODELS` | 评测候选模型，逗号分隔，`<provider>/<model_id>` 格式 |
| `HF_HUB_OFFLINE` | `1`（离线用缓存模型，避免超时） |
| `TRANSFORMERS_OFFLINE` | `1` |

## 模块地图

| 文件 | 一句话 |
|------|--------|
| `src/config.py` | `.env` → Settings |
| `src/storage.py` | SQLite 表定义（interests/availability/proactive） |
| `src/llm.py` | LLM 统一门面（openrouter/minimax/anthropic 分发，支持 tier） |
| `src/minimax.py` | MiniMax chat/chat_json/embed |
| `src/embed_server.py` | 本地 bge-small-zh embedding shim（:18080） |
| `src/llm_proxy.py` | 本地 strip-think shim（:18082）—— memU 内部抽取的 LLM 走这里，剥 `<think>` 后再回 memU |
| `src/memory.py` | memU 封装（recall/memorize/flush） |
| `src/interests.py` | 话题热度 bump/decay/top |
| `src/availability.py` | 用户活跃时段学习 + score |
| `src/emotion.py` | 四档聊法判断：casual/empathy/depth/interest |
| `src/tools.py` | Agent Reach 工具：URL读取/xhs搜索/Exa搜索 |
| `src/proactive.py` | 主动搭话：硬门 + LLM 软门 + 每日限额 |
| `src/persona.py` | 人格演化：traits/mood/观察/锚点；flush 后增量更新 + 每日 03:07 衰减 |
| `src/prompts.py` | system prompt 装配（含四档情绪指令：empathy/depth/interest/casual） |
| `src/rhythm.py` | 拆短句 + 打字模拟 |
| `src/agent.py` | 对话 turn 流水线 + generate_opener |
| `src/scheduler.py` | APScheduler：decay/memu_flush/proactive/persona_consolidate |
| `src/bot.py` | python-telegram-bot |
| `src/main.py` | 统一启停 |
| `src/admin_ui.py` | 记忆浏览/编辑 Web UI（FastAPI，:18081） |
| `src/memu_prompts_zh.py` | memU 中文化 prompt 模板（extraction/category_summary） |
| `src/clock.py` | 中文时间感字符串（now_signal / since_phrase） |
| `src/stickers.py` | 表情包：扫 `data/stickers/`、文件名当 tag、parse `[sticker:tag]` 标记 |
| `src/openrouter.py` | OpenAI 兼容客户端，主聊天（LLM_PROVIDER=openrouter）+ `scripts/eval_*`；走 Clash 代理 |

## 数据存储

- **SQLite** `data/app.sqlite`：interests, reply_samples, last_interaction, proactive_fires, persona_snapshots
- **memU** via **Postgres** `localhost:5432/memu`（容器 `memu-postgres`）
- **HuggingFace 模型缓存**：`~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/`
- **静态资源**：`data/stickers/*` 表情包（文件名当 tag）；`data/eval/run_*.{jsonl,md}` 模型评测产物

## Agent Reach 工具

依赖 CLI 工具（bot 不用重启即可验证）：

- `xhs`：`/Users/yangyu/.local/bin/xhs`（pipx）
- `mcporter`：`/Users/yangyu/.nvm/versions/node/v24.15.0/bin/mcporter`（nvm）

工具失败静默跳过，不影响聊天。详见 `document/agent-reach-integration.md`。

## 常见问题速查

`document/running.md` 有完整故障排查表。常见：

- **Telegram 超时**：Clash 未开
- **502 错误**：NO_PROXY 未生效，检查 `main.py::_purge_proxy_env`
- **端口 18080 / 18082 占用**：`pkill -f "src\.main"` + `pkill -f uvicorn`（embed shim=18080，strip-think shim=18082）
- **记忆不生效**：memU postgres 容器未起 → `docker start memu-postgres`

## 文档索引

| 文档 | 内容 |
|------|------|
| `document/overview.md` | 架构总览 + 模块职责 |
| `document/running.md` | 启停/日志/常见问题 |
| `document/dialog-tuning-log.md` | 对话风格调优记录（最新在上） |
| `document/agent-reach-integration.md` | 工具集成：xhs/Exa/yt-dlp |
| `document/minimax-integration.md` | MiniMax 接入与坑点 |
| `document/memu-setup.md` | memU 配置 + Postgres 持久化 |
| `document/extension-points.md` | 扩展点（情绪/人格演化/图片/表情包/评测都已落地，多用户未做） |
| `document/persona-evolution.md` | 人格演化：traits/mood/observations/milestones 设计 |
| `document/eval-system.md` | 模型评测系统（OpenRouter 多模型横向 + LLM judge） |
| `document/session-log.md` | 搭建流水 |
