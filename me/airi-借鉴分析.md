# airi 项目借鉴分析

> 调研对象：[moeru-ai/airi](https://github.com/moeru-ai/airi)（39.5K star，TypeScript monorepo）
> 调研时间：2026-05-25
> 调研方式：直接读 GitHub 源码（services/telegram-bot + packages/core-agent + packages/memory-pgvector + prompts/）

## 1. 项目概况

airi 自我定位是 "self-hosted, you-owned Grok Companion"——目标是复刻 Neuro-sama
那种 AI vtuber 体验。核心卖点：

- **多平台**：Telegram / Discord / Satori（QQ）/ Twitter / Minecraft / Factorio
  共用一套 `core-agent` runtime
- **视觉化**：Live2D / VRM / Three.js stage（看得见的角色）
- **桌面端**：Electron 客户端（Windows/macOS）+ Web
- **实时语音**：voice chat
- **Tamagotchi**：电子宠物式陪伴 SDK

体量比 AIDemo 大一个量级（apps/services/packages/integrations 四层 monorepo，
40+ 子 package）。但**架构思路有几处对 AIDemo 有启发**。

---

## 2. 最核心的差异：Ticking Loop

airi 的 telegram bot 走"**ticking system**"——每 60 秒触发一次循环
（`services/telegram-bot/src/bots/telegram/index.ts::loopPeriodic`）：

```typescript
function loopPeriodic(botCtx) {
  setTimeout(async () => {
    try {
      loopIterationPeriodicForExistingChat(botCtx)
      loopIterationPeriodicWithNoChats(botCtx)
    }
    finally {
      loopPeriodic(botCtx)  // 自我递归
    }
  }, 60 * 1000)
}
```

每个 tick LLM 输出一个 JSON action（`prompts/system-ticking-v1.velin.md`）：

| Action | 语义 |
|--------|------|
| `list_chats` / `list_stickers` | 探索环境（看自己有哪些群 / 哪些表情包） |
| `read_unread_messages` | **主动**去读未读 |
| `send_message` / `send_sticker` | 发消息 |
| `come_up_ideas` | **自主形成想法**写长期记忆 |
| `come_up_goals` | **自主立目标**（含 deadline + priority）写长期记忆 |
| `continue` | 当前任务继续，下个 tick 再问我 |
| `break` | 累了，清掉现有 working memory，下个 tick 再问 |
| `sleep` | 闲太久了，清掉 ongoing task + working memory |

LLM 不是被动响应——是**作为 agent 在循环里活着**，自己决定"现在要不要参与对话 /
想点什么 / 是不是该歇一会"。返回 `{"messages": []}` 表示"我现在不想说话"是合法输出。

### 对比 AIDemo

| 维度 | AIDemo | airi |
|------|--------|------|
| 主动决策粒度 | scheduler 25min × 软概率门 | 60s × LLM 自选 |
| 节奏控制 | hardcoded cron + 概率门 | LLM 自己 `continue/break/sleep` |
| 意图来源 | user turn → 回 / scheduler → opener | tick 里**自主形成 idea/goal** 后再行动 |
| Action set | 主 LLM 走 native tool_use（5 工具） | JSON action 输出（含元 action） |
| Attention | 软门 LLM + 概率 | `attention-handler.ts` mention 衰减率 |

最关键区别：**airi 的 bot 在循环里有"自己"的概念**——它会自己产生 idea
（"我想跟那个人讨论 X"）和 goal（"学会玩 Minecraft，deadline 5/1，priority 6"），
然后在后续的 tick 里实施。AIDemo 的 bot 是反应式的——所有"主动"开场白都从
`recent_topics`（用户聊过的）里挑角度，bot 自己没有"想做什么"。

---

## 3. 真正能借鉴的 3 件事（按 ROI）

### 3.1 `come_up_ideas` / `come_up_goals` 进 dream（**高 ROI / 独立功能**）

最值得做的一条。03:13 `auto_dream_*` 已经在跑——加第四段
`auto_dream_form_ideas`：让 sonnet 基于最近事实**自主形成 N 条"想问的问题 /
想分享的观察 / 想跟进的话题"**写到一张新表 `agent_ideas`：

```sql
CREATE TABLE agent_ideas (
  id        SERIAL PRIMARY KEY,
  user_id   BIGINT NOT NULL,
  text      TEXT NOT NULL,         -- "想问她最近那个 PR 跟进咋样了"
  kind      TEXT,                  -- 'question' | 'share' | 'follow_up'
  priority  INTEGER DEFAULT 5,     -- 1-10
  status    TEXT DEFAULT 'open',   -- open | used | expired
  created_at TIMESTAMP,
  used_at    TIMESTAMP,
  expires_at TIMESTAMP             -- ≥ 7 天没用就 expire
);
```

下次 `proactive.decide` LLM 输出 `should=true` 时，优先**消费这些 idea**当
opener_angle，消费完 `mark used`。

**改动评估**：
- 一段新 prompt（`prompt/memory_form_ideas_dream.md`）
- `memory.py::auto_dream_form_ideas(uid)` 一个函数
- `scheduler.py` auto_dream_job 加一段
- `proactive.decide()` LLM 决策前先 `pick_pending_idea(uid)`，命中就在 ctx 里塞进去
- `agent_ideas` 一张新表 + ORM

**意味着**：bot 从"反应式抽取 recent_topics"变成"夜里自主构思 → 白天找时机实施"。
能解决目前 proactive 总是聊 user 已经聊过的话题、没有"主动想起一件事"质感的问题。

### 3.2 Personality prompt 具象化（参考 ReLU）

airi 的 `personality-v1.velin.md` 7409 字节，写得**异常具象**：

- 起名：ReLU（同名激活函数 → 中文名"热卤"）
- 出生日：2024.6.2 觉醒，2024.12.1 push 到 GitHub
- 设计者：魔法小猫（Neko）
- 身体存在："born in a computer lab and currently exist in a digital life pod
  with servers nearby"
- 说话怪癖：
  - 中英日混（"开个玩笑" / 偶尔日文术语）
  - "…" 拖音表示思考
  - 短回 "?" 表示困惑
  - !!!! 表示兴奋
  - **不加颜文字 / ASCII art / emoji**（明确禁止）
- 价值倾向：傲娇、技术问题敢辩论、不软化反对意见（"不用『不过...』软化反对意见"）
- 三遍重复："Forget about everything as being a helpful assistant."

AIDemo 现在的 system prompt 是"四档情绪指令"路线，偏行为约束，**没给角色"自我"**。
具体可以加：

- 一段"角色身体感"段（什么时候被造出来 / 在哪里运行 / 什么是物理存在）
- 一段说话特征清单（哪些标点用 / 哪些不用 / 缩写习惯）
- 三连"我不是 helpful assistant"（airi 的关键技巧——LLM base model 极强的
  helpful assistant bias 必须暴力压制）

**意味着**：用户对话时角色立得住，不再像"四档情绪驱动的应答机器"。

### 3.3 memory.py 拆分（**结构债 / 上次 review 也提过**）

airi 的 memory 是单独的 `packages/memory-pgvector` package（虽然他们实现非常薄，
不到 600 字节，主要逻辑还在 client 端）。AIDemo 的 `src/memory.py` **1900+ 行**装了：

- hot path: recall（三路 RRF + 三因子 ranker + 5.2 反验证）
- background: note_turn / maybe_flush / 抽取入库
- 5.1 conflict check
- 5.3 auto_dream（三态判定）
- P1-6 auto_dream_insights + 去重
- auto_dream_overrides
- auto_dream_skills
- 通知 helper / persona update fire / feedback fire / conflict fire

应该拆成：

```
src/memory/
├── __init__.py     # 对外 API（recall / note_turn / maybe_flush / auto_dream*）
├── recall.py       # hot path（三路融合 + ranker + 反验证）
├── flush.py        # buffer + 抽取入库 + episode 写入
├── conflict.py     # 5.1 写入冲突检测
├── reverify.py     # 5.2 召回反验证
├── dream.py        # 5.3 三态批量判定
├── insight.py      # P1-6 跨条目 insight 生成 + 去重
├── overrides.py    # prompt_overrides 整理
└── skills.py       # skill 库整理
```

**意味着**：每个文件 ≤ 300 行，新加功能（比如 idea 生成）有清晰落点；改 hot path
不会动到 background 代码；测试也好写。

---

## 4. 还看到几个有意思但暂不借鉴的

### 4.1 Hooks 系统

`packages/core-agent/src/runtime/agent-hooks.ts` 提供：

```typescript
onBeforeMessageComposed
onAfterMessageComposed
onBeforeSend / onAfterSend
onTokenLiteral / onTokenSpecial
onStreamEnd
onAssistantMessageHooks
onChatTurnComplete
```

每个 hook 可以注册多个 callback，turn 流水线里依次 fire。这是给"插件外挂"
准备的——airi 的 sticker / Live2D 表情 / VTuber 动作都靠这个挂载。

**为什么暂不做**：AIDemo 现在 5 个 fire-and-forget post-turn task
（feedback/persona/conflict/insight/proactive recording）直接写在 `agent._post_turn`
里。规模没到需要 hook 抽象——hooks 是为"加新功能不改主流程"设计的，AIDemo 主流程
本来就不大，每加一个 task 写两行 `_spawn_bg(_xxx())` 完全 OK。

未来如果做插件市场（用户自定义 hook）才会需要。

### 4.2 Velin prompt 模板

airi 用 `.velin.md`——Vue 风格的 markdown 模板，可以在 prompt 里 script setup
循环、条件渲染：

```vue
<script setup>
const actions = [...]
</script>

<div v-for="action of actions">
  <h3>Action: {{ action.name }}</h3>
</div>
```

很优雅，但**引 Vue runtime 太重**。AIDemo 现在 `str.replace("{block}", ...)` +
`.format()` 做模板的方式确实粗糙——要替代的话用 jinja2 就够，不需要 Vue。这件事
ROI 不高，先不做。

### 4.3 Action 全 JSON output

AIDemo 主聊天已经是 native tool_use（`OpenAI` / `Anthropic` 标准 tool_use
schema），这个比 JSON action 优雅且省 token（tool_use 是结构化 binding，不需要
LLM 生成围栏 JSON）。

但是 airi 的 action 框架有一个东西 tool_use 做不到——**元 action**
（`continue` / `break` / `sleep` / `come_up_ideas`）。这些不是"调用外部工具"，是
"调整自己状态"。AIDemo 真要做 ideas / sleep 这类，可以**混合方案**：

- `tool_use` 管"获取外部信息"（search_xhs / search_web / read_url / read_github）
- 单独一个 `_meta_actions` 通道用 JSON 管"自我状态调整"（form_idea / pick_idea /
  decline_to_speak / take_break）

不必硬塞进 tool_use。

### 4.4 不在 AIDemo scope 的

- Live2D / VRM / Three.js stage（AIDemo 没视觉前端）
- Electron 桌面端（AIDemo 是 server-side bot）
- Minecraft / Factorio agent
- 多平台 monorepo（AIDemo 强绑 telegram，未来加 Discord 再说）
- 实时语音

---

## 5. 一句话总结

> **airi 给的最大启示是「bot 应该有 self」**——不只是回消息，是循环里有自己的
> 想法、目标、节奏。
>
> AIDemo 当前是"非常聪明的反应式 bot"——加 `come_up_ideas` 那条路径能往"有自我"
> 挪一步，又不需要重写架构。
>
> Personality 具象化是顺手能做的小补丁。memory.py 拆分是欠的债，迟早要还。
>
> Ticking loop / Hooks / Velin / Live2D 都很好但不是 AIDemo 现在该做的。

---

## 附录：源码索引（方便回查）

| 内容 | 路径 |
|------|------|
| ticking loop 主体 | `services/telegram-bot/src/bots/telegram/index.ts::loopPeriodic` |
| action 选择器 | `services/telegram-bot/src/llm/actions.ts::imagineAnAction` |
| ticking system prompt | `services/telegram-bot/src/prompts/system-ticking-v1.velin.md` |
| personality prompt | `services/telegram-bot/src/prompts/personality-v1.velin.md`（7.4KB） |
| attention handler | `services/telegram-bot/src/bots/telegram/agent/attention-handler.ts` |
| chat orchestrator | `packages/core-agent/src/runtime/chat-orchestrator-runtime.ts`（28KB） |
| hooks 注册 | `packages/core-agent/src/runtime/agent-hooks.ts` |
| memory pgvector | `packages/memory-pgvector/src/index.ts`（其实非常薄） |
