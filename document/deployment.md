# 部署到云服务器（Docker Compose）

> 目标：把 bot 部署到 Linux VPS（实测腾讯香港云），支持 5–50 个邀请制用户。
> 当前生产环境：腾讯云 HK，Ubuntu 22.04，2C/2G + 2G swap，docker compose。

## 服务总览

`docker-compose.yml` 起 5 个 service：

| service | 镜像 | 端口（host） | 作用 |
|---------|------|------|------|
| `bot` | 自构建 `aidemo-bot` | 无 | 主 bot + 可选 test bot；含 embed_server :18080（容器内） |
| `admin` | 自构建 `aidemo-admin` | `0.0.0.0:18081` | webUI |
| `postgres` | `pgvector/pgvector:pg16` | 无（compose 内网） | 自搭记忆栈 `memories` 表持久化（容器名 `memu-postgres` 沿用旧名） |
| `mihomo` | `metacubex/mihomo:latest` | 无 | Clash 内核——HK 出口 IP 被 Anthropic 限制时给 OpenRouter 走美区代理 |
| `cloudflared` | `cloudflare/cloudflared:latest` | 无 | 把 admin :18081 反向代理出 `https://*.trycloudflare.com`（HTTPS + 不开公网端口）|

## 1. 服务器准备

最低配置：2C / 2G 内存（要加 2G swap 兜底）/ 50G 盘。Ubuntu 22.04+。

```bash
# 加 swap
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab

# 装 docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER  # 重新登录后生效；或 sudo docker
```

## 2. 拉代码 + 写 .env

```bash
git clone https://github.com/YYuRain/YYnoTomotachi.git aidemo
cd aidemo
cp .env.example .env
vi .env
```

必填字段：

```env
TELEGRAM_BOT_TOKEN=<@BotFather 给的>
ADMIN_CHAT_ID=<你的 chat_id；@userinfobot 查>
TELEGRAM_PROXY=                          # HK/海外服务器留空（直连 Telegram）
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-sonnet-4.6
JINA_API_KEY=jina_...                    # 可选，没填会退到 Exa
MEMU_DB_URL=postgresql+psycopg://postgres:postgres@postgres:5432/memu
MEMU_CHAT_MODEL=deepseek/deepseek-v4-flash
ADMIN_UI_USER=<env 凭证用户名，备用，登录主路径已不用密码>
ADMIN_UI_PASSWORD=<env 凭证密码>
TEST_BOT_TOKEN=                          # 可选，第二个 bot 用于多用户模拟
```

> `.env` 里 `MEMU_DB_URL` 主机名是 `postgres`（compose 内网服务名）。

## 3. （仅当 OpenRouter 主聊天模型在你出口 IP 不可用时）配置 mihomo

腾讯香港云的出口 IP 被 Anthropic 视为受限地区，OpenRouter 调 `anthropic/claude-*` 会 403 `not available in your region`。给 mihomo 一份 Clash 订阅 yaml，让 LLM 流量走美区出口：

```bash
# 把订阅 yaml 拉到 ~/aidemo/clash-config.yaml
curl -fsSL -A "ClashforWindows" "<你的订阅 URL>" -o clash-config.yaml

# 改 mode 为 rule 并加固定路由：OpenRouter / Anthropic 域名走 "美国硅谷" 节点
# （直接编辑 clash-config.yaml，参考已有规则段）
```

`docker-compose.yml` 里 mihomo 服务挂这个文件作为只读配置，bot 的 `TELEGRAM_PROXY=http://mihomo:9981` 在 compose 内部设好。**如果你的服务器出口直接能访问 OpenRouter，可以删掉这个 service**。

## 4. 数据迁移（仅从单用户老库升级时跑）

全新部署 → 跳过这一节。

从已有 `data/app.sqlite`（单用户 me）迁：

```bash
docker compose up -d postgres                 # 先起 DB
docker compose run --rm bot \
  python -m scripts.migrate_to_multiuser --migrate-memu

# 验证
docker compose run --rm bot python -c "
from src.storage import session, User
with session() as s:
    print('users:', [u.chat_id for u in s.query(User).all()])
"
```

## 5. 构建 + 启动

```bash
docker compose build         # 第一次 ~5–10 分钟（含 bge 模型烤进镜像）
docker compose up -d
docker compose logs -f bot   # 看到 INFO __main__: ready 即就绪
```

## 5.5. （仅当从 memU SDK 时代升级）迁移老记忆 + backfill 5.1

如果服务器有从 memU 时代留下的 `memory_items` 表（自搭栈替换前的历史数据，2026-05-18 之前的部署），
启动后跑一次性迁移：

```bash
# 把旧 memory_items 拷到新 memories 表（幂等，可重跑）
docker compose exec bot python -m scripts.migrate_memu_to_native --apply

# 给历史 profile 重放一次 5.1 写入冲突检测，让 graph 上有 deps 边可看
docker compose exec bot python -m scripts.backfill_conflict_check --user-id <admin-chat-id>
```

迁移期间不停服。`backfill_conflict_check` 跑 ~115 LLM call、几分钱、5-7 分钟。
跑完去 admin webUI 的图谱 tab 看 stale / to_verify 节点和 depends_on 边。

## 6. 邀请用户 + 登录 webUI

**完整流程**（你 admin 这端）：

1. 在 prod bot 里发 `/myid` → bot 回你的 chat_id。如果跟 `.env::ADMIN_CHAT_ID` 不一致，改 .env 重启 bot。
2. 发 `/invite 3` → bot 回三个邀请码
3. 把码私发给朋友，让对方在 prod bot 发 `/start <code>` 激活
4. 激活成功后 bot 会发系统提示 + AI 生成的开场白

**任意激活用户访问 webUI**：

1. 在 bot 里发 `/memory` → bot 回一条 `https://<random>.trycloudflare.com/login-by-token?t=...` 链接
2. 点开 → 自动 set cookie → 进 webUI 主页（admin 看全部 + 下拉切用户，普通用户只看自己）
3. cookie 保 7 天；token 链接 10 分钟内有效，过期重发 `/memory`

**注意**：cloudflared 的临时 URL 每次容器重启会变（trycloudflare 限制）。`/memory` 命令实时生成最新 URL，永远用最近一条。

## 7. 多用户测试（test bot，可选）

设了 `TEST_BOT_TOKEN` 后会启用第二个 bot：

- `/become <label>` 选虚拟身份（`alice` / `bob` / 数字都行）
- `/start <code>` 走完整邀请码流程激活那个虚拟身份
- 直接发消息聊
- `/clear` 清空当前虚拟身份所有数据 + 把邀请码归还（可重测）
- `/whoami` 看当前虚拟 + 真实 chat_id
- `/memory` 一键 webUI 链接

虚拟 user_id 是 `9_000_000_000 + crc32(label)`，不会跟真 chat_id 冲突。
scheduler 不给 `status='test'` 的用户跑 proactive。

## 8. 常用运维

```bash
# 看日志
docker compose logs -f bot
docker compose logs -f admin
docker compose exec bot tail -f data/audit.jsonl

# 重启单服务
docker compose restart bot
docker compose restart cloudflared      # URL 会变，重发 /memory 拿新的

# 备份
docker compose exec postgres pg_dump -U postgres memu > backups/memu-$(date +%F).sql
tar czf backups/data-$(date +%F).tgz data/

# 升级
git pull
docker compose build
docker compose up -d
```

## 9. 时区

容器 `TZ=Asia/Shanghai`（Dockerfile 里设的）+ scheduler `timezone="Asia/Shanghai"`，bot 的时间感、proactive 决策、persona 03:07 衰减都按 CST 算。

## 10. 风险点

- **OpenRouter 账单**：50 用户 × 每天 30 轮 sonnet ≈ $30–50/天。盯紧 OpenRouter dashboard
- **postgres 数据丢失**：`pg-data` 是 docker volume，宿主机重做盘会丢——定期 `pg_dump`
- **audit.jsonl 增长**：50 用户 ~每周 50MB，半年 ~1GB；本期不做轮转，必要时手动 `mv` + truncate
- **cloudflared URL 不稳定**：trycloudflare 是临时 tunnel，重启就换；想要稳定 URL 需自己有域名挂 Cloudflare 跑 named tunnel
- **mihomo 订阅过期**：节点失效 → Anthropic 403 又回来；重新拉订阅 + `restart mihomo`
