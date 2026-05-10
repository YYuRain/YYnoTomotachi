#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOGFILE="$DIR/data/bot.log"
ADMIN_LOGFILE="$DIR/data/admin.log"

# ── 颜色 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }

# ── 参数解析 ──────────────────────────────────────────────────────────────
BACKGROUND=false
WITH_ADMIN=false
for arg in "$@"; do
  case "$arg" in
    -b|--background) BACKGROUND=true ;;
    --admin)         WITH_ADMIN=true ;;
    -h|--help)
      echo "用法: ./start.sh [-b] [--admin]"
      echo "  -b / --background  后台运行（日志写 data/bot.log）"
      echo "  --admin            同时启动记忆浏览 UI（:18081）"
      exit 0 ;;
  esac
done

mkdir -p "$DIR/data"

echo ""
echo "┌─────────────────────────────────────────┐"
echo "│        AIDemo Companion Agent           │"
echo "└─────────────────────────────────────────┘"
echo ""

# ── 1. 检查 Clash 代理 ────────────────────────────────────────────────────
if curl -s --max-time 2 --proxy http://127.0.0.1:7897 https://api.telegram.org > /dev/null 2>&1; then
  ok "Clash 代理 (127.0.0.1:7897) 在线"
else
  warn "Clash 代理不可达 — Telegram 可能连不上，请确认 Clash 已开"
fi

# ── 2. 启动 memU Postgres ──────────────────────────────────────────────────
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^memu-postgres$"; then
  ok "memu-postgres 已在运行"
else
  echo -n "  启动 memu-postgres..."
  if docker start memu-postgres > /dev/null 2>&1; then
    # 等 postgres 就绪（最多 15 秒）
    for i in $(seq 1 15); do
      if docker exec memu-postgres pg_isready -U postgres -q 2>/dev/null; then
        echo -e " ${GREEN}就绪${NC}"
        break
      fi
      sleep 1
      if [ "$i" -eq 15 ]; then
        echo ""
        err "postgres 15 秒内未就绪，继续启动（可能有短暂记忆错误）"
      fi
    done
  else
    err "docker start memu-postgres 失败 — 记忆层不可用"
    warn "如需 postgres：docker run -d --name memu-postgres -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=memu -p 5432:5432 pgvector/pgvector:pg16"
  fi
fi

# ── 3. 检查 venv ───────────────────────────────────────────────────────────
if [ ! -f "$DIR/.venv/bin/python" ]; then
  err ".venv 不存在，请先运行：python3 -m venv .venv && .venv/bin/pip install -e ."
  exit 1
fi
ok "Python venv (.venv) 就绪"

# ── 4. 启动 admin UI（可选）──────────────────────────────────────────────
if $WITH_ADMIN; then
  if pgrep -f "scripts.admin" > /dev/null 2>&1; then
    ok "admin UI 已在运行 (http://127.0.0.1:18081)"
  else
    nohup "$DIR/.venv/bin/python" -m scripts.admin > "$ADMIN_LOGFILE" 2>&1 & disown
    ok "admin UI 已后台启动 (http://127.0.0.1:18081) → 日志: data/admin.log"
  fi
fi

# ── 5. 启动 bot ────────────────────────────────────────────────────────────
if pgrep -f "src\.main" > /dev/null 2>&1; then
  BOT_PID=$(pgrep -f 'src\.main' | head -1)
  # 健康检查：日志最后一次写入超过 10 分钟视为卡死
  LOG_AGE=99999
  if [ -f "$LOGFILE" ]; then
    LOG_AGE=$(( $(date +%s) - $(stat -f %m "$LOGFILE" 2>/dev/null || echo 0) ))
  fi
  if [ "$LOG_AGE" -gt 600 ]; then
    warn "bot 进程 $BOT_PID 存在但日志已 ${LOG_AGE}s 未更新（可能 polling 断连），自动重启..."
    pkill -f "src\.main" 2>/dev/null; sleep 1
    pkill -f "uvicorn" 2>/dev/null; sleep 1
    lsof -ti:18080 | xargs kill -9 2>/dev/null; sleep 1
  else
    ok "bot 运行中（PID: $BOT_PID，日志 ${LOG_AGE}s 前更新）"
    echo ""
    echo "  tail -f $LOGFILE    # 查看日志"
    echo "  pkill -f 'src\.main'  # 停止"
    exit 0
  fi
fi

if $BACKGROUND; then
  nohup "$DIR/.venv/bin/python" -m src.main > "$LOGFILE" 2>&1 & disown
  BOT_PID=$!
  echo ""
  ok "bot 已后台启动（PID: $BOT_PID）"
  echo ""
  echo "  等待就绪..."
  # 等 ready 信号（最多 60 秒）
  for i in $(seq 1 60); do
    if grep -q "ready" "$LOGFILE" 2>/dev/null; then
      ok "bot ready ✓"
      echo ""
      echo "  日志：tail -f $LOGFILE"
      echo "  停止：pkill -f 'src\.main'"
      exit 0
    fi
    # 检查是否已崩溃
    if ! kill -0 $BOT_PID 2>/dev/null; then
      err "bot 进程已退出，查看日志："
      tail -20 "$LOGFILE"
      exit 1
    fi
    sleep 1
  done
  warn "60 秒内未见 'ready'，请检查日志：tail -f $LOGFILE"
else
  echo ""
  ok "前台启动（Ctrl+C 停止）"
  echo ""
  cd "$DIR" && exec "$DIR/.venv/bin/python" -m src.main
fi
