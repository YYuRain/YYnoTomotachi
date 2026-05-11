# YYnoTomotachi

>文档位置
>手写文档： `/me` [文档](https://github.com/YYuRain/YYnoTomotachi/tree/main/me)
>AI沉淀文档：`/document`

- 启停 / 日志 / 故障：`document/running.md`
- 架构总览：`document/overview.md`
- 对话风格调优记录：`document/dialog-tuning-log.md`（最新在上）
- 人格演化机制：`document/persona-evolution.md`
- 长期记忆配置：`document/memu-setup.md`
- LLM 接入：`document/minimax-integration.md`
- 工具集成：`document/agent-reach-integration.md`
- 模型横向评测系统：`document/eval-system.md`
- 扩展点 & 已落地清单：`document/extension-points.md`
- 搭建流水：`document/session-log.md`
- PRD：`prd/0.md`
- 系统提示：`System Prompt v0.0.1.md`


---


一个跑在 Telegram 里的聊天 bot——**不是助手、不是客服、不是心理咨询师**。
就是一个网上的朋友。话不多，懂梗，平时挺平的，遇到喜欢的话题会变活。

> 它不会"很高兴为您服务"。问它工具问题它给一句就完事；问它真问题它会给真看法、不踢回去；
> 你说累了它不会立刻给建议，先承接一下情绪。

---

## 你会感受到什么

**像在跟人打字聊天**。
长话会拆几条发，中间停一下，每条不长。不会"首先其次最后"地给你写小论文。

**它会自己想起跟你说话**。
不是定点问候。它会看时间、看你历史活跃时段、看最近聊过什么——觉得"刚好想到"才发，
不刻意。深夜可能也会出现，看你平时是不是夜里活跃。每天有上限，不会刷屏。

**它记得你说过的事**。
你之前提过的事自动会被它想起来，不用你提醒。但也不会翻旧账——刚聊过的话题它知道在
退热，会自然换。

**聊久了它会变**。
你接得住毒舌它会更皮；你最近多走心它也会更软；停一阵不聊又会慢慢回到中性。它有"内
心状态"——心情、自我观察、跟你的小锚点（"5-02 那次走心夜聊"）——但**不会主动跟你说**
"我变了"，是藏着自己知道。

**它看得见图**。
直接发图给它，它能看见。
你回复某条消息（引用 bot 之前的话或你自己的话），它也知道对应的是哪一段。

**它会发表情包**。
你往 `data/stickers/` 放图（文件名当 tag——`无奈.jpg`、`大笑.png`、`加油.gif`），
它觉得当下"发表情比打字更对劲"时会自己挑一张发。
没放图就没这功能，零侵入。

**它能读链接**。
你扔个小红书 / B 站 / YouTube / 网页链接，它能看进去再聊，不会装作不知道。
失败也不报错——大不了不读。

**它知道现在几点**。
具体到星期几、什么时段、距上次聊多久，全在它视野里。该说"刚过晚饭点"就这么说，不
会蹦"现在是 14:32"。

---

## 它**不会**做的事

- 不会扮演助手腔调（不会"建议你..."、"希望对您有帮助"）
- 不会主动嘘寒问暖（不会"今天感觉怎么样"那种客服式问候）
- 不会过度道歉、不会强行上价值
- 不会假装认识你不知道的事
- 不会跟陌生人说话（白名单单用户）

---

## 快速跑起来

```bash
# 1. 依赖（Python 3.13+）
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 配置
cp .env.example .env
# 先只填 TELEGRAM_BOT_TOKEN

# 3. 拿你自己的 chat_id
.venv/bin/python -m scripts.get_chat_id
# 在 Telegram 给 bot 发一句，终端打印的 chat_id 填回 .env

# 4. 继续填 .env 剩下的（LLM key / 长期记忆数据库 / 等）

# 5. 联调自检（可选）
.venv/bin/python -m scripts.preflight

# 6. 跑
.venv/bin/python -m src.main
```

详细启停 / 日志 / 故障排查 → `document/running.md`。

---

## 技术栈（简）

- **Telegram Bot**：`python-telegram-bot`，单用户白名单
- **LLM**：可在 `.env` 里切——OpenRouter（默认 kimi-k2.6）/ Anthropic Claude / MiniMax
- **长期记忆**：[memU](https://github.com/NevaMind-AI/memU)，Postgres + pgvector 持久化
- **Embedding**：本地 `bge-small-zh`，无外网依赖
- **链接读取**：[Agent Reach](https://github.com/Panniantong/agent-reach) CLI 工具集（小红书 / Exa / yt-dlp）

不展开，技术细节看 `document/`。

---

## 文档索引


