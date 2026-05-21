FROM python:3.13-slim

WORKDIR /app

# 系统依赖：psycopg 编译 + curl + tzdata（设中国时区）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# 注：之前考虑过装 gh CLI，但 v2.x 之后强制 auth（公开仓库 anon API 也要 GH_TOKEN）；
# read_github 改直接 curl https://api.github.com/... REST，不依赖 gh binary。

# 中国大陆时区——bot 的 clock.now_signal / availability / proactive 都依赖 datetime.now() 取本地时间
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 先装依赖让 layer 可缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[persist]" "huggingface-hub[cli]"

# Agent-Reach: yt-dlp（YouTube/B站字幕）+ xiaohongshu-cli（小红书 CLI，jackwener，
# 注册 'xhs' 命令；含完整签名实现支持 search/read API）—— 单独 layer 让上面 layer 可复用缓存
RUN pip install --no-cache-dir "yt-dlp>=2024.12.0" "xiaohongshu-cli"

# bge-small-zh 烤进镜像（~100MB）。新版 huggingface_hub 用 `hf`（旧 `huggingface-cli` 已移除）
RUN hf download BAAI/bge-small-zh-v1.5 \
    --local-dir /opt/hf/bge-small-zh-v1.5

ENV HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1

# 项目代码
COPY . .

# 默认起 bot；admin UI 用 docker-compose 里的 command 覆盖
CMD ["python", "-m", "src.main"]
