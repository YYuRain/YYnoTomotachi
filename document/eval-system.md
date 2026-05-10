# 模型评测系统（OpenRouter）

> 2026-05-08 接入。横向评测不同 LLM 用作主聊天的"陪伴感"质量，
> 决策"换主模型"时的依据。完全独立脚本，**0 影响线上 bot 状态**。

## 用途

回答两个问题：
1. 当前主模型（MiniMax-M2.7 / Claude Sonnet）和 OpenRouter 上的 N 个候选相比，差距多大？
2. 不同候选模型在不同对话场景（闲聊 / 走心 / 工具问 / 长回复）上各自的表现？

## 两步走

### Phase 1：生成横向回复表（人工看）

```bash
.venv/bin/python -m scripts.eval_models                    # 全集合
.venv/bin/python -m scripts.eval_models --dry              # 只采样不调 LLM
.venv/bin/python -m scripts.eval_models --samples 3 \
    --models openrouter/anthropic/claude-sonnet-4.6,local/minimax-m2.7   # 冒烟
```

输出：
- `data/eval/run_<ts>.jsonl` — 程序友好（phase 2 输入）
- `data/eval/run_<ts>.md` — 人类友好横向对比表

### Phase 2：LLM judge 多维度自动打分

```bash
.venv/bin/python -m scripts.eval_judge                     # 默认 judge=Claude Opus 4.6
.venv/bin/python -m scripts.eval_judge --judge openrouter/openai/gpt-5.5
.venv/bin/python -m scripts.eval_judge --input data/eval/run_xxx.jsonl
.venv/bin/python -m scripts.eval_judge --dry               # 只打印 judge prompt
```

输出：
- `data/eval/run_<ts>.scores.jsonl` — 每行 `{sample, model, persona/rhythm/natural/topic/overall, note}`
- `data/eval/run_<ts>.scores.md` — 按 overall 排序的总表 + sample×model 矩阵

## 评测样本来源

`scripts/eval_models.py` 的样本 = 自动采样 + 手工 fixture：

- **自动采样**：从 `data/memu_buffer/ingested/conv_*.json` 抽真实对话（默认 20 个），按 kind
  分桶（chitchat/emotion/question/long/tool）保证覆盖
- **手工 fixture**：`eval/fixtures.yaml` 5 个用户填的特别想验证的场景

## 评分维度（5 分制）

| 维度        | 含义                          |
| --------- | --------------------------- |
| `persona` | 贴角色风格——网友 vs 助手腔/客服腔/咨询师腔   |
| `rhythm`  | 短句、不长篇、不强行拆条                |
| `natural` | 不端着——禁用"建议/首先/总的来说/希望对你..." |
| `topic`   | 切题、不跑偏                      |
| `overall` | 综合"陪伴感"（不是简单平均）             |

## 隔离保证（不污染线上 bot）

| 风险点 | 写入触发 | 处理 |
|--------|---------|------|
| memU postgres 表 | 仅 `memory.maybe_flush` | 脚本不 import `src.memory` ✓ |
| SQLite interests / availability | 仅相关模块写入 | 脚本不 import 这些 ✓ |
| `_recent` agent 内存 | 仅 `agent.handle_user_message` | 脚本不 import `src.agent` ✓ |
| `persona_snapshots` | 仅 `persona.update_state/consolidate` | 脚本不 import `src.persona` ✓ |

脚本只 import：`src.config / src.llm / src.minimax / src.openrouter / src.clock`。
跑完用 SQL 验证 `max(ts)` 不变即可。

## 关键文件

| 文件 | 职责 |
|------|------|
| `src/openrouter.py` | OpenAI 兼容 httpx 客户端；走 Clash 代理（OpenAI 中国区被 OpenRouter 拒）；失败不抛、记录 latency |
| `scripts/eval_models.py` | phase 1：采样 + provider 派发 + 并发 + 出 jsonl/md |
| `scripts/eval_judge.py` | phase 2：按 sample 横向匿名打分（A/B/C... 防 judge 偏见） |
| `eval/fixtures.yaml` | 5 个手工场景 |

## 配置

`.env`：

```
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
EVAL_MODELS=openrouter/anthropic/claude-sonnet-4,openrouter/openai/gpt-5,...,local/minimax-m2.7,local/claude-sonnet-4-6
```

`EVAL_MODELS` 的 provider 前缀派发：
- `openrouter/<sub>` → 走 `src.openrouter.chat`
- `local/minimax-*` → 走 `src.minimax.chat`（不需要 OpenRouter）
- `local/claude-*` → 走 `src.llm._anthropic_chat`（内网 gateway，注意预算限制）

## 已踩过的坑

### 1. OpenAI 模型 403 "not available in your region"

OpenRouter 按 IP 拦中国 IP 调用 OpenAI 模型。`src/openrouter.py` 走 `TELEGRAM_PROXY`（Clash 同端口 7897）解决。
其他客户端（minimax / 内网 anthropic）保持 `trust_env=False` 不动。

### 2. Reasoning model 用光 max_tokens、content 永远空

gpt-5 / o1 / MiniMax-M2 等推理模型先用一大块 token 内心独白，content 才出来。
`max_tokens=600` 全被 reasoning 吃光、`finish_reason=length`、content 是 None。

**修法**：评测脚本 `GEN_MAX_TOKENS=4096`（一次性评测，多花点钱拿到所有模型的真实回复 > 省钱）。

### 3. Judge 输出含 ```json...``` 围栏

Claude Opus 4.6 当 judge 时倾向用 markdown fence 包 JSON。
**修法**：`_parse_judge_response` 加 fence 抠取（`r"```(?:json)?\s*\n?(.+?)\n?```"`）。

### 4. Judge 在 note 字段嵌套 ASCII 双引号破坏 JSON

`"note": ""可以考虑"踩禁词"` —— ASCII 双引号嵌套字面破坏 JSON 结构。
**修法**：在 judge system prompt 显式禁止 ASCII 双引号、要求用中文方引号 `「」` 引用。

## 一次评测的产物（参考）

13 模型 × 22 sample = 286 cell，全跑完 5-10 分钟，OpenRouter 费用 $1-3。

**2026-05-08 跑过一次**，judge 用 Claude Opus 4.6，前三名：
1. **kimi-k2.6** overall=4.23（persona/rhythm/natural 都强）
2. gemini-3.1-pro-preview 4.05
3. claude-sonnet-4.6 3.91

垫底：gpt-5-mini 1.50（persona 1.50，bullet 列表 + 助手腔严重）。

完整结果：`data/eval/run_20260508-114915.scores.md`。
