# 会话流水（本次搭建过程）

## 2026-05-18：抛弃 memU SDK + PRD v2 三层防线

### 触发

memU SDK (`memu-py`) 1.5.x 多次踩坑（happened_at 列偷加、ctx 多用户 race、需要 strip-think shim
兜底）。memU 给的能力其实只有抽取 LLM 调用 + pgvector RAG 召回，自己写 < 200 行就够。

### 改动

**Phase 1：等价替换 memU**（commit `2a1c661`）
- 新单表 `memories`，抽取 prompt 改 JSON（一次 call 出 profile+event 两类）
- `recall/note_turn/maybe_flush` 三个公共 API 不变，上层零改动
- 删 `src/llm_proxy.py` + `src/memu_prompts_zh.py`；卸 `memu-py`
- `scripts/migrate_memu_to_native.py` 老 `memory_items` → 新 `memories`
- 新 `DEV_SKIP_PROD_BOT=1` 让本地不抢云端 polling

**Phase 2：PRD v2 三层防线**
- **5.1 写入冲突检测**（commit `73eee02`）：每条新事实 flush 后异步 fire LLM 影响分析；同 batch 互排除；schema 加 `status / confidence / last_verified_at / depends_on UUID[]`
- **图谱视图**（commit `2532d6f`）：admin UI D3 v7 force-directed graph；conflict check 把新事实 id 写进老条目 depends_on（去重）
- **backfill 历史**（commit `661c7b4`）：admin 122 profile / 35 flips（21 stale, 14 to_verify）
- **5.2 召回反验证**（commit `2dad994`）：recall 同步阻塞 + 30min cooldown；LLM 两态（still_valid / uncertain）
- **5.3 Auto Dream**（commit `94cebc4`）：03:13 cron 批量；LLM 三态（still_valid / uncertain / stale）；用 deps 上游 + top-5 confirmed 邻居作综合上下文

### 部署到 HK 服务器

`memory-deps` 分支直接 checkout；服务器 188 条历史迁过去；admin backfill 后
147 confirmed / 21 stale / 12 to_verify；prod + test bot 双在线；Auto Dream 已挂 03:13 cron。

### 验证

沙箱跑 PRD §2.3 例子（住北京 + 通勤 + 理发卡 + 社保 → 搬上海了），三层全按设计触发。
完整描述见 `me/prd_memory.md` + `document/memory-stack.md`。

---

## 2026-04-29：Agent Reach 工具能力集成

### 目标

让 agent 能读取用户分享的链接（小红书、B 站、YouTube、普通网页），并在感知到用户询问具体内容时主动搜索，自然融入对话，不改变陪伴风格。

### 完成的改动

| 文件 | 变更 |
|------|------|
| `src/tools.py` | 新增。异步封装 Agent Reach CLI：URL 提取 + 路由读取、xhs 搜索、Exa 网页搜索、Jina 读取、xhslink 短链解析 |
| `src/agent.py` | 在 `_build_turn` 加入 `tool_task` 与 emotion/recall/topics 并行；有链接走确定性路径，无链接走 LLM 判断；工具结果注入用户消息前缀 |
| `src/prompts.py` | `build_system_prompt` 新增 `tool_context: str = ""` 可选参数（向后兼容，内容现在主要通过用户消息注入） |

### 环境安装

- `pipx install agent-reach`（含 mcporter/Exa 通道）
- `pipx install xiaohongshu-cli==0.6.4`
- 小红书认证：Safari 登录 → VSCode 授"完全磁盘访问权限" → `xhs login --browser safari`

### 关键踩坑

1. **subprocess PATH**：`asyncio.create_subprocess_exec` 不继承 shell PATH，需手动构造 `_TOOL_ENV` 注入 pipx + nvm 路径。
2. **MiniMax 忽略 system prompt 末尾**：工具内容注入 system prompt 结尾时 agent 声称"看不了"。改注入用户消息前缀后稳定生效。
3. **xhslink 短链**：需先 `curl -sL -w "\nFINAL_URL:..."` 解析重定向，再提取 note_id + xsec_token，不能直接传短链给 `xhs read`。
4. **xsec_token**：随分享 URL 携带，必须从 query string 解析后传 `--xsec-token`，否则 API 拒绝。

详细记录见 `document/agent-reach-integration.md`。

---

## 2026-04-25（首次搭建）

### 需求对齐

- 读 `prd/0.md`：陪伴型 agent（非助手/非咨询）、主动找人、对话节奏、动态兴趣、主动记忆召回、
  MiniMax + Telegram + memU 自托管。
- 读 `System Prompt v0.0.1.md`：明确的口吻设定 + `{{#检索记忆.body#}}` 占位符。

### 关键决定（已问用户确认）

| 决定 | 选择 |
|------|------|
| memU 模式 | 自托管库（非 Cloud API） |
| 用户规模 | 单用户 MVP |
| 部署 | 本地能跑即可，后续再定 |
| Key 存放 | `.env` |
| 下一期方向 | 情绪识别 + 人格演化 → 架构预留扩展点 |
| 过程文档 | 沉淀到 `document/` |

### 搭建顺序

1. 脚手架：`pyproject.toml` / `.env.example` / `.gitignore` / `src/__init__.py`
2. 基础层：`config.py`（dotenv + Settings）、`storage.py`（SQLAlchemy + 4 张表）
3. API 客户端：`minimax.py`（chat / chat_json / embed，httpx AsyncClient）
4. 长期记忆：`memory.py`（封 memU，buffer + flush，主动召回）
5. 业务状态：`interests.py`（热度衰减）、`availability.py`（时段学习 + 冷启动先验）
6. 扩展点占位：`emotion.py`、`persona.py`（签名稳定，实现期只改内部）
7. Prompt 装配：`prompts.py`（含 `PROACTIVE_OPENER_INSTRUCTIONS`）
8. 节奏化：`rhythm.py`（剥 markdown、切句、打字模拟）
9. 流水线：`agent.py`（turn 管线 + `generate_opener`）
10. 调度 / 接入：`scheduler.py` / `bot.py` / `main.py`
11. 过程文档：`document/` 下五份文件

### 踩过的坑

- 写 prompts.py 时，多层嵌套引号踩了 Python 字符串字面量语法。
  **解决**：`PROACTIVE_OPENER_INSTRUCTIONS` 改用三引号字符串 + 中文方括号 `『』` 作内部引用。
- memU 的 `memorize` 吃文件路径（JSON），不是流式逐条。
  **解决**：rolling buffer，每 6 轮或 15 分钟 flush 一个小 JSON 文件。

### 联调期踩的坑（比代码多）

1. **Telegram 被墙**：api.telegram.org 国内直连不通。
   **解决**：`config.telegram_proxy = http://127.0.0.1:7897`，`bot.py` 里用
   `HTTPXRequest(proxy=...)` 显式注入；`scripts/get_chat_id.py` 同样处理。

2. **MiniMax embedding 不是 OpenAI 兼容**：请求字段用 `texts`（不是 `input`），
   还要 `type=db|query`；返回 `{"vectors": [...], "base_resp": ...}`（不是 `{"data":[{"embedding":..}]}`）。
   而且余额不足。**解决**：彻底绕开 MiniMax embedding，改跑本地
   `sentence-transformers`（`BAAI/bge-small-zh-v1.5`, 512 维），
   用 `src/embed_server.py` 起一个 OpenAI 兼容的 FastAPI shim 给 memU 消费。

3. **pip 装 sentence-transformers 卡死**：直连 PyPI 拉 torch 被网络拖住。
   **解决**：`-i https://pypi.tuna.tsinghua.edu.cn/simple` 走清华镜像，30 秒装完。

4. **HuggingFace 下 bge 模型被墙（xethub CDN 无法通过 hf-mirror 代理）**。
   **解决**：首次下载用 `HTTPS_PROXY=http://127.0.0.1:7897`；之后模型落在
   `~/.cache/huggingface/hub/` 里，运行时设 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`
   阻止 transformers 再去 HF 查 adapter_config（那次 HEAD 会超时/refused）。

5. **MiniMax-M2 的 `<think>` 块吃掉 max_tokens**：默认 512 tokens 不够 think 完就截断 → 输出空。
   **解决**：`minimax.chat` 默认提到 1024；`_strip_think` 剥掉 `<think>...</think>`，
   没闭合的残余整段丢弃。preflight 里 chat 测试 max_tokens=4000。

6. **memU 里嵌的 OpenAI SDK 经 Clash 代理返回 502**：macOS 的 httpx 会读 scutil
   的系统代理，即使 `HTTP_PROXY` env var 没设。结果 memU 打到 MiniMax 的 LLM
   调用 / 打到本地 127.0.0.1:18080 的 embedding 调用都被劫持。
   **解决**：主进程启动时清掉 `HTTP(S)_PROXY`，并显式设
   `NO_PROXY=127.0.0.1,localhost,api.minimaxi.com,api.minimax.chat`；
   Telegram 的代理通过 `HTTPXRequest(proxy=...)` 单独注入，不走 env。

### 2026-04-27 下午：memU 中文化 + 记忆浏览/编辑 UI

**中文记忆**：memU 默认 extraction prompt 是英文 + 一句"use same language"——但 MiniMax-M2 / Claude 面对英文 scaffolding 倾向于出英文，结果我们历史数据都是英文 summary。改法：
- 新增 `src/memu_prompts_zh.py`：中文版 profile / event extraction template + 中文 category_summary prompt + 10 个 default category 的中文 description。
- `src/memory.py::_get_service` 把这三份通过 `memorize_config={...}` 塞进 `MemoryService`。
- 踩坑：category_summary 的 placeholder 是 `{original_content}` / `{new_memory_items_text}`（不是我一开始写的 `{original}` / `{new_items_text}`），第一次启动会 KeyError，看源码修正。
- 注意：历史英文 item 不会被自动翻译；新 memorize 的产出开始是中文。随时间自然替换。

**admin UI 加编辑/删除**：`src/admin_ui.py` 加四个接口：
- `PATCH /api/items/{id}` body `{summary}` → 改内容 + 调本地 embed server 重算向量。
- `DELETE /api/items/{id}` → 删，级联清 category_items。
- `PATCH /api/categories/{id}` body `{summary?, description?}` → 改字段 + 重算向量（用 summary 优先，没有时用 description）。
- `DELETE /api/categories/{id}` → 手动先清 category_items 关联再删分类。

重算 embedding 通过 `http://127.0.0.1:18080/v1/embeddings`（bot 的 local embed server）。bot 未跑时跳过重算并在响应里 `embedded=false` 警告。

UI 上每行加了"编辑"/"删除"按钮，点编辑弹模态框，保存后立即刷新。

### 2026-04-27：memU 切 Postgres 持久化

触发：用户感知到"记忆全都没生效"——原因是 memU 用 `inmemory`，每次 bot 重启（前几天重启了 7+ 次）记忆全清空，`data/memu_buffer/*.json` 只是 memU memorize 的原始输入，重启不会自动加载。

改动：
- docker 起 `pgvector/pgvector:pg16`，database `memu`，建 `CREATE EXTENSION vector`。
- 装 `psycopg[binary]>=3.1` + `pgvector`。
- `.env`：`MEMU_METADATA_PROVIDER=postgres` / `MEMU_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/memu`。
- 修 `src/memory.py`：memU 的字段是 `dsn` 不是 `url`（之前踩过一次）。
- 新增 `scripts/backfill_memory.py`：把历史 `data/memu_buffer/conv_*.json` 回灌进 postgres，成功的移到 `data/memu_buffer/ingested/`。

回灌结果：8/20 成功（12 个因 MiniMax 529 overload 失败，原文件仍在 buffer，可重试）。postgres 现状：10 resources / 14 memory_items / 10 memory_categories。

后续：bot 重启不再丢记忆；新对话直接写 postgres。

### 2026-04-26：接入情绪/谈话模式识别 + 节奏/语气多轮调优

各轮次语气/节奏调整的独立日志在 `document/dialog-tuning-log.md`（按时间倒序），包括：
- 情绪/谈话模式识别接入（三档 casual/empathy/depth）
- 节奏按信息量自适应（rhythm 切分重写 + 三档 max_piece_chars）
- 默认不反问，用陈述式隐式引导

本文件只在架构层面记录：这次改动新增 `src/emotion.py` 的 `EmotionSignal` 实现，
`src/agent.py` 引入 `asyncio.gather` 并行跑 emotion/recall/topics，
`src/prompts.py` 按 mode 注入专用指令块，
`src/rhythm.py::deliver` 开放 `max_piece_chars` 参数。

### preflight 最终结果

```
[1/3] MiniMax chat ... ✅ -> '收到'
[2/3] 本地 embedding server ... ✅ -> dim=512, n=2
[3/3] memU memorize + retrieve ... ✅ -> items=3, categories=2
全部通过，可以 python -m src.main 了。
```

### 冒烟测试结果（离线，不需真实 key）

环境：`python3 -m venv .venv && .venv/bin/pip install -e .`，Python 3.14.4，macOS darwin arm64。
已安装关键包：`memu-py 1.2.0`、`python-telegram-bot 22.7`、`httpx 0.28.1`、`sqlalchemy 2.0.49`、`apscheduler 3.11.2`、`openai 2.32.0`（memU 依赖）。

用 dummy env vars 跑脚本：
- ✅ 所有 13 个 `src/*.py` 模块 import 成功
- ✅ `rhythm.split_for_chat` 正确剥 markdown 并切短（`#` 和 `**` 都被去掉，每条 ≤ 40 字）
- ✅ `interests.bump` + `decay_tick` 正确累积 + 衰减
- ✅ `availability.score` 冷启动先验生效（周一 10 点 = 0.55，周三 3 点 = 0.15）
- ✅ `availability.record` 写入，`seconds_since_last_interaction()` 返回 ≈ 0

**发现的小瑕疵**（非阻塞）：`rhythm` 切分时孤立的标点（如句末的 `？`）会被甩到下一条开头。MVP 可接受，若碍眼将来再调 `_soft_pieces` 的合并规则。

### 联调辅助脚本（已加）

- `scripts/get_chat_id.py`：只需填 `TELEGRAM_BOT_TOKEN`，启动后在 TG 给 bot 发一句，
  终端会打印 chat_id 方便填回 `.env`。
- `scripts/preflight.py`：三步自检——`minimax.chat` → `minimax.embed` → memU
  `memorize+retrieve`（用临时 JSON）。任一步报错都会抛异常方便定位；
  memU 召回 0 条会警告并指向兼容性兜底说明。

### 下一步（实现/联调阶段，当前未做）

1. 装依赖：`python3 -m venv .venv && .venv/bin/pip install -e .`（已完成）。
2. 填 `.env`：先只填 `TELEGRAM_BOT_TOKEN` → 跑 `get_chat_id.py` 拿 chat_id → 再填 MiniMax 字段。
3. 跑 `scripts.preflight` 验证 MiniMax ↔ memU 这条链。
4. 启 `python -m src.main`，在 TG 打字对话。
4. 观察指标：
   - 消息是否真的被拆成多条短句发出；
   - `data/app.sqlite` 里 interests 热度是否随多轮聊同一话题累积；
   - 放置 > 6 小时后，下一个 proactive_tick 是否触发主动搭话；
   - 日志里 memU recall 是否命中。

### 计划文件位置

`~/.claude/plans/prd-0-md-robust-lobster.md`（不随仓库走，保留本地）。
