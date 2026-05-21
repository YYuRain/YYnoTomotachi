FROM python:3.13-slim

WORKDIR /app

# 系统依赖：psycopg 编译 + curl + tzdata（设中国时区）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Agent-Reach: gh CLI 官方二进制（amd64；HK Tencent 是 amd64）
ARG GH_VERSION=2.65.0
RUN curl -fsSL https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz \
    | tar -xz -C /tmp \
    && mv /tmp/gh_${GH_VERSION}_linux_amd64/bin/gh /usr/local/bin/gh \
    && rm -rf /tmp/gh_*

# 中国大陆时区——bot 的 clock.now_signal / availability / proactive 都依赖 datetime.now() 取本地时间
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 先装依赖让 layer 可缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[persist]" "huggingface-hub[cli]"

# Agent-Reach: yt-dlp（YouTube/B站字幕）+ xhs（小红书 CLI）—— 单独 layer 让上面 layer 可复用缓存
RUN pip install --no-cache-dir "yt-dlp>=2024.12.0" "xhs>=0.0.10"

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
