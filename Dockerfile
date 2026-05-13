FROM python:3.13-slim

WORKDIR /app

# 系统依赖：psycopg 编译 + curl（agent reach 工具用 curl 拉链接）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖让 layer 可缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[persist]" "huggingface-hub[cli]"

# bge-small-zh 烤进镜像（~100MB）
RUN huggingface-cli download BAAI/bge-small-zh-v1.5 \
    --local-dir /opt/hf/bge-small-zh-v1.5

ENV HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1

# 项目代码
COPY . .

# 默认起 bot；admin UI 用 docker-compose 里的 command 覆盖
CMD ["python", "-m", "src.main"]
