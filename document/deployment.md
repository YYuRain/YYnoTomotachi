# 部署到云服务器（Docker Compose）

> 目标：把 bot 部署到一台 Linux VPS（如腾讯香港云），支持 5–50 个邀请制用户。
> 假设：境外 IP（HK/海外），可直连 Telegram / OpenRouter / Jina 不需代理。

## 1. 服务器准备（一次性）

最低配置：2C / 4G / 20G 盘。Ubuntu 22.04+ / Debian 12+。

```bash
# 装 docker + compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER  # 让当前用户能用 docker，重新登录生效
# compose plugin 已包含在 docker 新版里；老版可装：
# sudo apt install docker-compose-plugin
```

防火墙：bot 是纯出站连接，**不需要开任何入站端口**。admin UI 只绑 `127.0.0.1`，靠 SSH 隧道访问。

## 2. 拉代码 + 写 .env

```bash
git clone https://github.com/YYuRain/YYnoTomotachi.git aidemo
cd aidemo
cp .env.example .env
vi .env   # 关键字段如下
```

必填：

```env
TELEGRAM_BOT_TOKEN=...                   # @BotFather 给的
ADMIN_CHAT_ID=8058993786                 # 你自己的 chat_id（先用 @userinfobot 查；或先 /myid 临时部署后再回填）
TELEGRAM_PROXY=                          # HK 直连，留空
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.6
JINA_API_KEY=jina_...                    # 不填也能跑（退到 Exa 兜底）
MEMU_METADATA_PROVIDER=postgres
MEMU_DB_URL=postgresql+psycopg://postgres:postgres@postgres:5432/memu
MEMU_CHAT_MODEL=deepseek/deepseek-v4-flash
```

> **bot/admin 容器内的 `MEMU_DB_URL` 已被 docker-compose.yml 重写指向服务名 `postgres`**，
> 所以 `.env` 里这一行实际生效的只是兜底——容器外（如本地开发）才用得到。

## 3. 构建镜像

```bash
docker compose build
```

第一次拉模型 + 装依赖大约 5–10 分钟。镜像最终 ~1.5 GB（含 bge-small-zh 模型）。

## 4. 数据迁移（仅从单用户老库升级时跑）

如果服务器是**全新部署**——跳到第 5 步。

如果你是从已有 `data/app.sqlite`（单用户 me）迁移：

```bash
# 先起 postgres，让 memU 表能被 UPDATE
docker compose up -d postgres

# 跑迁移：SQLite 加 user_id 列、复制旧数据归到 admin、wrap recent.json、UPDATE postgres
docker compose run --rm bot python -m scripts.migrate_to_multiuser --migrate-memu

# 验证迁移成功
docker compose run --rm bot python -c "
from src.storage import session, User
with session() as s:
    print('users:', [u.chat_id for u in s.query(User).all()])
"
```

## 5. 启动

```bash
docker compose up -d
docker compose logs -f bot
```

看到 `INFO __main__: ready` 即就绪。

## 6. admin UI 访问

不暴露公网。本地隧道：

```bash
ssh -L 18081:127.0.0.1:18081 user@your.host
# 然后在本地浏览器开 http://127.0.0.1:18081
```

## 7. 常用运维命令

```bash
# 看 bot 日志
docker compose logs -f bot

# 看 audit
docker compose exec bot tail -f data/audit.jsonl

# 重启 bot（不动 postgres）
docker compose restart bot

# 备份 postgres
docker compose exec postgres pg_dump -U postgres memu > backups/memu-$(date +%F).sql

# 备份本地状态
tar czf backups/data-$(date +%F).tgz data/

# 升级（拉新代码）
git pull
docker compose build
docker compose up -d   # 自动重建变化的服务
```

## 8. 邀请新用户

在 Telegram 用 admin 账号给 bot 发 `/invite 3`，bot 回三个邀请码；把码发给朋友，让对方 `/start <code>` 激活。

`/users` 看当前用户名单；`/myid` 任何人都能用，返回自己的 chat_id。

## 9. 风险点

- **OpenRouter 账单**：50 用户 × 每天 30 轮 sonnet ≈ $30–50/天。盯紧 OpenRouter dashboard，超预算就在 `.env` 把 `OPENROUTER_MODEL` 切成便宜模型重启
- **postgres 数据丢失**：`pg-data` 是 docker volume，宿主机重做盘会丢——一定要定期 `pg_dump`
- **audit.jsonl 增长**：50 用户 ~每周 50MB，半年 ~1GB；本期不做轮转，必要时手动 `mv` + truncate
- **Jina API quota**：每月 1M token；用满了 `read_url` 退到 Exa 兜底，体感降级但不崩
