# MiniMax 接入记录

## 选型

- **LLM**：MiniMax 聊天模型（默认 `MiniMax-M2`，可在 `.env` 里改）；或通过 `LLM_PROVIDER=anthropic` 切 Claude。
- **Embedding**：~~`embo-01`~~ → **本地 `BAAI/bge-small-zh-v1.5`**（512 维）。
  MiniMax embedding API 格式与 OpenAI 不兼容（字段 `texts`/`type`，返回 `vectors`），且 MVP 阶段余额不足，已改由 `src/embed_server.py` 起一个 OpenAI 兼容的 FastAPI shim（端口 18080），供 memU 消费。
- **调用方式**：OpenAI 兼容端点 `https://api.minimaxi.com/v1/`。
  - 聊天：`POST /chat/completions`
  - ~~向量：`POST /embeddings`~~ （不再使用 MiniMax embedding）

## 为什么选兼容端点

memU 的 `llm_profiles` 参数就是吃 OpenAI 风格的 `base_url + api_key + chat_model / embed_model`，
只要 MiniMax 兼容得够好，memU 就能直接用它跑 memorize / retrieve，不用写 shim。

实际可能会遇到的兼容性问题（**实现期要验证**）：

| 场景 | 可能症状 | 兜底 |
|------|----------|------|
| `response_format={"type":"json_object"}` 不支持 | 400 / 返回非 JSON | `minimax.chat_json` 已加抠取 `{...}` 的宽松解析 |
| Embedding 返回字段不同 | memU 崩 | 在 `src/minimax.py` 里写 adapter，把返回拍成 `{"data":[{"embedding":[...]}]}` |
| stream 用不了 | 非问题（MVP 不 stream） | — |
| 并发限流 | 429 | 简单退避即可 |
| `<think>` 块吃满 max_tokens | `chat_json 解析失败：`、最终 `_strip_think` 剥完是空字符串 | aux JSON 任务（emotion/topics/persona/tool_detect）必须给 `max_tokens ≥ 2048`；不要为了"省 token"传 200~400，会被 think 吃光 |

## 关键文件

- `src/minimax.py`：三个函数 `chat / chat_json / embed`，`httpx.AsyncClient` singleton。
- `src/memory.py`：`llm_profiles` 填入 MiniMax base/key/model，memU 内部调用时走同一个兼容端点。

## `.env` 模板

```
MINIMAX_API_KEY=
MINIMAX_GROUP_ID=
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MINIMAX_CHAT_MODEL=MiniMax-M2
MINIMAX_EMBED_MODEL=embo-01
```

## 第一次联调清单

1. 最小 curl 通：
   ```bash
   curl -s -X POST "$MINIMAX_BASE_URL/chat/completions" \
     -H "Authorization: Bearer $MINIMAX_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"'"$MINIMAX_CHAT_MODEL"'","messages":[{"role":"user","content":"hi"}]}'
   ```
2. Embedding curl 通（同上路径 `/embeddings`，`input:["hello"]`）。
3. `python -c "import asyncio; from src.minimax import chat; print(asyncio.run(chat([{'role':'user','content':'hi'}])))"`。
4. 若 memU retrieve/memorize 报 embedding 字段错，来 `src/minimax.py` 写 shim。

## 已踩过的隐性陷阱

### `tier` 关键字不能透传给 minimax 分支（2026-05-06 修复）

`src/llm.py::chat_json` 是跨 provider 的统一门面，签名约定 `tier="main"|"aux"`。早期实现里，
走 minimax 分支时把 `**kw`（含 `tier`）原样传给 `minimax.chat_json` → `minimax.chat`，但
`minimax.chat()` 没有 `tier` 形参，会抛 `TypeError: chat() got an unexpected keyword argument 'tier'`。

**为什么没被发现**：所有 aux JSON 调用方（emotion / interests / proactive / tools 决策、本次新加的 persona）
都用 `try / except: log.debug(...)` 包住，异常被静默吃掉，表现就是"判断失效但 bot 不崩"——
emotion 永远 None、tool 永远不触发、persona 永远不更新。

**修复**：`src/llm.py::chat_json` 走 minimax 分支前 `kw.pop("tier", None)`。
**教训**：跨 provider 门面里所有"只属于某一边"的关键字，传透前先 pop 掉，不要靠下游容忍。
