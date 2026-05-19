# 启动 / 停止 / 日志

> 本地部署。电脑关机或进程被杀 = bot 停。重启电脑后要按下面的步骤手动起一次。

## 前置检查（只需做一次）

- 本机走 Clash 类代理，本地端口 `127.0.0.1:7897`（`.env` 里 `TELEGRAM_PROXY` 写的就是这个）。
  Telegram API 国内直连不通，**bot 启动时 Clash 必须在运行**。
- `.venv/` 已经建好（`python3 -m venv .venv && .venv/bin/pip install -e .`）。
- `.env` 已经填好（token、chat id、`OPENROUTER_API_KEY`/`MINIMAX_API_KEY`、proxy；可选 `MEMU_CHAT_MODEL` 切换 memU 上游、`JINA_API_KEY` 让网页正文读取走鉴权）。
- HF 模型已经缓存在 `~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/`。
  第一次启动时联网下载过一次，之后靠缓存，不再联网。
- **memU 持久化容器在运行**：`docker ps | grep memu-postgres`。没起就 `docker start memu-postgres`。
  不启会导致 bot 第一次 memorize 时报 postgres 连接拒绝。

## Agent Reach 工具依赖（前置，只需确认一次）

bot 的链接读取 + 搜索都走 Jina REST API（`https://r.jina.ai` / `https://s.jina.ai`），
只需 `.env::JINA_API_KEY`，无外部 CLI 依赖。

验证：

```bash
# Jina Search（search_web）
curl -s -H "Authorization: Bearer $JINA_API_KEY" -H "X-Respond-With: no-content" \
  "https://s.jina.ai/?q=test" | head -5

# Jina Reader（read_url）
curl -s -H "Authorization: Bearer $JINA_API_KEY" \
  "https://r.jina.ai/https://example.com" | head -5
```

工具不可用时 bot 仍能正常聊天，只是读不了链接/搜不了内容（失败静默跳过）。

> 历史的 `xhs search` / `mcporter call exa.web_search_exa` 自 2026-05-19 起退役（容器没装且 xhs 账号风控失效）。
代码里 `search_xhs` 已自动退化到 Exa 网页搜索（`site:xiaohongshu.com`）作为兜底。要恢复
原生 xhs 搜索，要么换浏览器登录的小红书账号、要么 `xhs login --qrcode` 扫码登另一个号。

## 方式一：前台启动（看日志方便，关终端 = 停）

开一个终端窗口：

```bash
cd /Users/yangyu/Desktop/AIDemo
.venv/bin/python -m src.main
```

看到下面这几行就说明 ready：

```
INFO src.embed_server: embedding model ready, dim=512
INFO __main__: embed server ready @ 127.0.0.1:18080
INFO __main__: ready
```

**本地端口分配**：
- `:18080` — embed server（bge-small-zh，给自搭记忆栈做 embedding）
- `:18081` — admin UI（可选，`scripts.admin` 单独跑）

> 历史端口 `:18082` 是 memU SDK 时代的 strip-think shim，2026-05-18 起换自搭记忆栈后已退役。

在 Telegram 找你那个 bot 发消息就行。

停：这个窗口里按 `Ctrl+C`，会走正常关停流程（关 scheduler → 停 polling → 关 embed server → 再见）。

## 记忆浏览 UI（可选）

查看/搜索 自搭记忆栈（postgres `memories` 表）的记忆数据 + D3 图谱。独立于 bot 进程。

```bash
nohup .venv/bin/python -m scripts.admin > data/admin.log 2>&1 & disown
```

浏览器打开 http://127.0.0.1:18081 ——三个 tab：分类 / 记忆项（带搜索） / 资源。

停：`pkill -f "scripts.admin"`。不开不会影响 bot 正常工作。

## 方式二：后台启动（关终端也继续跑，推荐日常用）

```bash
cd /Users/yangyu/Desktop/AIDemo
nohup .venv/bin/python -m src.main > data/bot.log 2>&1 &
disown
```

- `nohup` 让进程脱离 shell，关终端不受影响。
- `> data/bot.log 2>&1` 把 stdout + stderr 都写进 `data/bot.log`。
- `disown` 让 shell 不再追踪它，关窗口不发 HUP。

看日志：

```bash
tail -f /Users/yangyu/Desktop/AIDemo/data/bot.log
```

停：

```bash
pkill -f "src\.main"
```

查有没有在跑：

```bash
pgrep -fl "src\.main"
```

## 电脑睡眠怎么办

合盖 = 进程挂起。Telegram 服务器会暂存消息，唤醒后 bot 会一次性拉回来。
但"主动搭话"的 `scheduler` 定时器会被冻结，醒来后要等到下一个 tick。

临时不让 Mac 睡（调试时有用）：

```bash
caffeinate -i &                 # 只要这个终端不关，就不睡
# 或者只防盘睡眠一小时：
caffeinate -t 3600
```

## 常见问题

| 现象 | 原因 & 解决 |
|------|------|
| 启动时 `telegram.error.TimedOut` | Clash 没开 / 代理端口变了。检查 `scutil --proxy` 和 `.env` 里 `TELEGRAM_PROXY`。 |
| `openai.InternalServerError: 502` | `NO_PROXY` 没生效。`src/main.py::_purge_proxy_env` 会自动处理，别手动设 HTTPS_PROXY。 |
| `Address already in use :18080` | 上一个 bot 进程没死干净。`pkill -f "src\.main"` 再 `pkill -f uvicorn`，等 2 秒重启。 |
| chat 返回空字符串 | MiniMax-M2 的 `<think>` 块占满 max_tokens。调高 `minimax.chat` 的 max_tokens（默认 1024）。 |
| 模型加载时网络报错 | HF 被墙。第一次需要 Clash；之后 `HF_HUB_OFFLINE=1` 已经在 `main.py` 里强制设了，不应再联网。 |

## 代码同步（Git）

仓库：https://github.com/YYuRain/YYnoTomotachi（私有），branch = `main`

改完代码后推送：

```bash
git add src/ scripts/ document/ CLAUDE.md README.md .gitignore   # 按改动范围选
git commit -m "简短说明"
git push
```

**不要** `git add data/` / `.env` / `.venv/` / `.claude/`——`.gitignore` 已排除，误 add 会泄漏 API key 和本地聊天记录。

---

## 每次开机的最短手速版

```bash
# 1. 确认 Clash 已启动（菜单栏图标）
# 2. 起 memU 持久化容器
docker start memu-postgres
# 3. 起 bot
cd /Users/yangyu/Desktop/AIDemo && nohup .venv/bin/python -m src.main > data/bot.log 2>&1 & disown
# 4. 验证
sleep 5 && tail -20 data/bot.log
```

最后一步如果看到 `INFO __main__: ready`，可以去 Telegram 用了。
