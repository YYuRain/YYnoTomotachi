# memU 配置与风险

> ⚠️ **历史归档（2026-05-18 起）**：memU SDK (`memu-py`) 已被自搭记忆栈替换。
> 当前架构看 [`memory-stack.md`](memory-stack.md)。本文档保留作为：
> - 选 memU 时踩过的坑参考（XML 输出、ctx 多用户 race、strip-think shim 必要性）
> - 老库迁移到自搭栈的依据（`scripts/migrate_memu_to_native.py`）
> 新部署不必读本文档。

## 选型

- 自托管 `memu-py`（pip 包）。
- 第一版用 `inmemory` metadata store（重启即丢，够 MVP 跑通）。
- 第二版切 `postgres + pgvector`（docker 一行起）。

## 初始化参数（`src/memory.py`）

```python
MemoryService(
  llm_profiles={
    # default 永远指向本地 :18082 shim（src/llm_proxy.py），shim 内部根据 settings 选上游
    "default":   {"base_url": "http://127.0.0.1:18082/v1", "api_key": ..., "chat_model": ..., "client_backend": "httpx"},
    # embedding 走本地 :18080 shim（src/embed_server.py，bge-small-zh）
    "embedding": {"base_url": "http://127.0.0.1:18080/v1", "api_key": "local", "embed_model": "BAAI/bge-small-zh-v1.5"},
  },
  database_config={"metadata_store": {"provider": "postgres", "dsn": ...}},
  retrieve_config={"method": "rag"},
  memorize_config={"memory_type_prompts": ..., "memory_categories": ..., "default_category_summary_prompt": ...},
)
```

**chat 上游由 `MEMU_CHAT_MODEL` 环境变量决定**（2026-05-12 起）：
- 设了 `MEMU_CHAT_MODEL`（如 `deepseek/deepseek-v4-flash`）→ shim 走 OpenRouter，带 Clash 代理
- 空 → shim 走 MiniMax 直连（旧路径，剥 `<think>`）

`memory.py` 不直连 OpenRouter 是因为 `main.py::_purge_proxy_env` 清掉了代理环境变量，
memU 内置 httpx 客户端不会走 Clash。让 shim 兜底处理上游和代理。详见 [strip-think shim 章节](#strip-think-shim2026-05-07-加--防-think-块污染-category-summary)。

## MemU 的 API 使用方式

| 动作 | 调用 | 说明 |
|------|------|------|
| 写入记忆 | `await svc.memorize(resource_url=path, modality="conversation", user={"user_id": "me"})` | 吃 JSON 文件路径，格式 `[{"role": "user"/"assistant", "content": "..."}]` |
| 主动召回 | `await svc.retrieve(queries=[{"role":"user","content":{"text": user_msg}}], where={"user_id":"me"})` | 返回 `{categories, items, resources}` |

MVP 的 buffer 策略：每 6 条对话或 15 分钟触发一次 flush（见 `memory.maybe_flush`）。
flush 文件落盘在 `data/memu_buffer/conv_<epoch>.json`，失败会回滚到 buffer 头部重试。

## 实际踩坑（已确认）

1. **MiniMax embedding 不是 OpenAI 兼容** —— 请求用 `texts`+`type`，返回
   `{"vectors":...,"base_resp":...}`。且 MVP 阶段账户 embedding 余额不足。
   **已采方案**：启本地 `src/embed_server.py`（FastAPI + sentence-transformers
   `BAAI/bge-small-zh-v1.5`），暴露 OpenAI 格式的 `/v1/embeddings`，memU 指向它。
2. **macOS scutil 系统代理会被 httpx 读到** —— 即使 env 没设 HTTPS_PROXY。
   memU 内嵌的 OpenAI SDK 会被 Clash 劫持返回 502。
   **已采方案**：进程启动时 `NO_PROXY=127.0.0.1,localhost,api.minimaxi.com,api.minimax.chat`。
3. **HF 模型下载被 xethub CDN 挡** —— hf-mirror 代理不到 xethub。
   **已采方案**：首次下载走 Clash；之后 `HF_HUB_OFFLINE=1 + TRANSFORMERS_OFFLINE=1` 用缓存。
4. **Python 版本**：memU 要求 3.13+。本机 3.14.4 OK。
5. **inmemory 持久化**：重启后长期记忆清空。MVP 能用，上线切 postgres。
6. **多用户隔离**：`where={"user_id": USER_ID}`。单用户 MVP 固定 `me`。

## Postgres 持久化（2026-04-27 起启用）

**为什么切**：`inmemory` 每次 bot 重启记忆全丢，`data/memu_buffer/*.json` 是原始输入不是持久化产物，重启后 memU 不会自动加载。开发期高频重启就会感觉"记忆全没生效"。

### 一次性搭好

```bash
# 1. pgvector 容器
docker run -d --name memu-postgres \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=memu \
  -p 5432:5432 pgvector/pgvector:pg16

# 2. 启用 vector 扩展
docker exec memu-postgres psql -U postgres -d memu -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3. Python 侧
.venv/bin/pip install 'psycopg[binary]>=3.1' pgvector

# 4. .env
MEMU_METADATA_PROVIDER=postgres
MEMU_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/memu
```

### 回灌历史对话

```bash
.venv/bin/python -m scripts.backfill_memory
```

扫 `data/memu_buffer/conv_*.json`，挨个调 `memU.memorize`，成功的移到 `data/memu_buffer/ingested/`。失败的留在原位可重试（常见失败原因：MiniMax 并发打爆返回 `529 overload`）。

### 验证持久化有数据

```bash
docker exec memu-postgres psql -U postgres -d memu -c \
  "SELECT count(*) FROM resources; SELECT count(*) FROM memory_items; SELECT count(*) FROM memory_categories;"
```

### 关键坑点

- memU 的 postgres 配置字段叫 `dsn`，**不是 `url`**。踩过一次。
- 数据库名写 `memu`；容器里用 `psql` 而非 `pg`。
- Docker 容器每次开机要手动启（`docker start memu-postgres`）或配 `--restart unless-stopped`。

## 自定义抽取 prompt 必须输出 XML（不是 JSON）—— 2026-05-06 修复

**症状**：admin UI 看到的"记忆"停留在 4-27 不再增长，但 `resources` 表每天都在增加。

**根因**：`src/memu_prompts_zh.py` 写的中文抽取 prompt 让 LLM 输出 JSON 格式（`{"memories_items": [...]}`），但 memU 当前版本 (`memu/app/memorize.py:534`) 只调用 `_parse_memory_type_response_xml`，期望根标签 `<item>` 包裹的 XML：

```xml
<item>
    <memory>
        <content>一条记忆</content>
        <categories>
            <category>preferences</category>
        </categories>
    </memory>
</item>
```

JSON 输出 → `_find_xml_boundaries` 找不到 `<item>/<profile>/...` 任一根标签 → 日志只打一条 `WARNING: Could not find valid root tag in XML response`（非 ERROR），上层 `memorize` 继续返回成功 → bot 日志看到 `memorize ok`，但 `memory_items` 表始终 0 增长。

**为什么 4-27 那批 39 条能成功**：那批是 4-27 切 postgres 当天 backfill 时跑的，当时还没切到中文 prompt（默认英文 prompt 输出标准 XML），所以解析 OK。我们引入中文 prompt 后所有抽取就一直在静默失败。

**修复**：`src/memu_prompts_zh.py` 的 `_PROFILE_PROMPT` / `_EVENT_PROMPT` 输出段已改为 XML（保留中文规则、分类、示例不变）。

**验证**：

```bash
.venv/bin/python -c "
import asyncio, sys; sys.path.insert(0, '.')
from src.memory import _get_service, USER_ID
async def m():
    svc = _get_service()
    r = await svc.memorize(resource_url='data/memu_buffer/ingested/<some-conv>.json',
                           modality='conversation', user={'user_id': USER_ID})
    print('items:', len(r.get('items', [])))
asyncio.run(m())
"
```

输出 `items: N`（N>0）即修复生效。

**回灌历史**：之前在 4-27 之后产生但因 prompt bug 没抽到的 buffer 文件，可以一次性重跑（`memorize` 不去重 resource，所以同一文件重跑会再插一次 resource——历史已清零的情况下没问题）。本次修复时回灌了 29 个文件，共抽出 26 条 memory_items。回灌完后手动 `mv data/memu_buffer/conv_*.json data/memu_buffer/ingested/` 归档，避免下次误重跑。

## strip-think shim（2026-05-07 加）—— 防 think 块污染 category summary

**最初症状**：admin UI 看到的 `memory_categories.summary` 字段全是 `<think>用户提供了一个分类的旧摘要...</think>` 内容。

**根因**：memU 内部 category summary 是直接拿 LLM raw response 写库，不走 XML 解析。MiniMax-M2 输出带 `<think>...</think>` 块，memU 内置 OpenAI 风格客户端**不知道**剥（这是 MiniMax 模型行为），raw content 直接进 DB。
（`memory_items` 没受影响，因为我们的 prompt 让 extraction 输出 XML，memU 的 XML 解析自然过滤掉非 `<item>` 包裹的 think。）

**架构**：本地 shim `src/llm_proxy.py`（FastAPI :18082）作为 memU 与上游 LLM 之间的代理层。

`main.py` 启动时一并起 shim（先于 bot ready）。对 memU 透明——它依然以为自己在调一个普通的 OpenAI 兼容 chat 端点。

### 上游路由（2026-05-12 扩展）

shim 启动时按 `settings()` 一次性绑定上游：

| 条件 | 上游 | 代理 | 为什么 |
|------|------|------|--------|
| `MEMU_CHAT_MODEL` 设了（如 `deepseek/deepseek-v4-flash`）+ `OPENROUTER_API_KEY` 有 | OpenRouter | 显式 `TELEGRAM_PROXY`（Clash） | 当前推荐：deepseek-v4-flash 便宜（$0.14/M）、无 think、中文抽取够用 |
| 否则 | MiniMax 直连 | 不走代理 | 旧路径；MiniMax-M2.7 带 `<think>` 必须经 shim 剥 |

shim 永远剥 `<think>`：对没 think 块的模型是 no-op，零代价。这样 memory.py 不必关心上游是谁，统一指向 :18082。

切回 MiniMax：把 `.env` 的 `MEMU_CHAT_MODEL` 留空重启即可。

### 清理已污染数据

```sql
-- 一次性清理已污染的 summary
UPDATE memory_categories
SET summary = trim(regexp_replace(summary, '<think>.*?</think>\s*', '', 'gs'))
WHERE summary LIKE '%<think>%';
```
