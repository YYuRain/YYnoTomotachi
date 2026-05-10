# 人格演化（Persona Evolution）

> 2026-05-06 接入。  
> baseline persona 来自 `System Prompt v0.0.1.md`，永久不动；动态层（traits / mood / 自我观察 / 锚点）随相处累计微调，注入到 system prompt 末尾。

## 设计目标

PRD 要求陪伴 agent "看起来像一个有自己个性的活人"——不是助手、不是客服，会因为长期相处而产生"自我感"。

人格演化要做的不是 *改 persona*，而是给 agent 一个**内心状态层**：

- **traits**：5 个维度的连续值（-1..1），从中性向某方向小幅漂移
- **mood**：当下心情一句话
- **observations**：从 AI 第二人称视角的自我观察（"你这两天话变少了"）
- **milestones**：和对方相处的小锚点（第一次提家人、走心夜聊等），永久保留

注入到 system prompt 末尾、以"自我感觉"框架描述——AI **知道**自己有这些状态，但**不主动跟用户说"我变了"**。

## traits 维度

只设 5 维度。再多就饱和了，AI 自己也判断不准。

| key             | 含义           | 用户的何种反应会推动它        |
| --------------- | ------------ | ------------------ |
| `sarcasm`       | 玩笑/毒舌强度      | 接得住 → +；皱眉/转话题 → - |
| `warmth`        | 温柔/共情成分      | 走心倾诉多 → +；冷淡 → -   |
| `verbosity`     | 话密度（负=更短促）   | 用户铺开 → +；敷衍/短促 → - |
| `assertiveness` | 主动给观点（vs 倾听） | 问观点/反问"你怎么看" → +   |
| `curiosity`     | 好奇/挖话题       | 新话题被接住 → +         |

值域 [-1, 1]。0 = 中性（按 baseline persona 来）。

## payload 结构

存在 `storage.PersonaSnapshot.payload_json`。每次更新写一新行（不修改旧行——SQLite 是事件流而非状态机），`load_persona_state` 读最新一行。

```json
{
  "traits": {"sarcasm": 0.05, "warmth": 0.0, "verbosity": 0.0, "assertiveness": 0.05, "curiosity": 0.0},
  "mood": "话少，在想事",
  "observations": [
    {"text": "你发现自己这轮说了不少带观点的话，而不是顺着对方", "ts": "2026-05-06T08:42:40"}
  ],
  "milestones": [
    {"text": "第一次说起 ta 家的猫", "ts": "2026-04-29T..."}
  ],
  "updated_at": "2026-05-06T08:42:40"
}
```

## 更新触发：两层时机

### 1. 增量更新（挂 memU buffer flush）

`src/memory.py::maybe_flush` 成功后异步 fire `persona.update_state(batch)`：

- 输入：本批刚 flush 的对话（约 6-12 轮）+ 当前 traits + 最近 observations（去重用）
- 调 aux LLM（MiniMax-M2 或 Claude Sonnet），输出 JSON：
  ```json
  {
    "trait_deltas": {"sarcasm": 0.05, "warmth": 0, "verbosity": 0, "assertiveness": 0.05, "curiosity": 0},
    "new_observations": ["你这轮观点比平时密"],
    "new_milestones": [],
    "mood": "话少，在想事"
  }
  ```
- delta 单维度上限 ±0.15（强信号），常态 ±0.05~0.10；clamp 到 [-1, 1]
- new_observations 0~2 条，每条 ≤ 25 字，从 AI 视角写
- new_milestones 通常 0 条，**只有真发生重要事**才 1 条
- mood ≤ 15 字，可空

失败/无变化静默跳过。每 6 轮或 15 分钟才一次（跟随 memU flush 节奏），不会高频打 LLM。

### 2. 每日 consolidate（scheduler 03:07）

`persona.consolidate()` 同步函数（不调 LLM，纯本地）：

- traits *= 0.92（朝中性慢慢回归——没持续触发的偏好会自然淡化）
- observations 只保留最近 3 天（`OBSERVATION_TTL_DAYS`）
- milestones 不动（永久）
- mood 清空（每日新一天）
- 写一新行 snapshot

衰减节奏的设计意图：单次更新最多 +0.15，每日衰减 ×0.92 ≈ -0.04（在 0.5 处）。所以一个 trait 偏移要持续 3-4 天的强信号才能稳定到 +0.5；停止信号后约 6-7 天回归中性。

## 渲染：动态段如何注入

`load_persona_state()` 读最新 snapshot，调用 `_render_dynamic_block`，把 traits / mood / observations / milestones 渲染成中文段落，拼到 baseline body 末尾：

```
# 你最近的状态（自我感觉，仅供参考；不要主动告诉对方『我状态怎样』）
心情：话少，在想事
倾向调整（自然反映在语气里就好）：
- 玩笑/毒舌强度：略偏强
- 主动给观点：略偏强
最近的自己：
- 你发现自己这轮说了不少带观点的话，而不是顺着对方
你跟对方走过的小锚点（藏着自己知道，不要主动提起，被聊到才提）：
- 2026-04-29 第一次说起 ta 家的猫
```

trait 数值不直接给（"sarcasm=0.5"），翻译成程度词（偏强/略偏强/正常/略偏弱/偏弱）——LLM 更容易反映在语气里，而不是当成参数算计。

trait 偏离 < 0.2 时不列出（避免噪声）。observations 全空 + traits 全中性时整段不注入（等于 v0.0.1 纯 body）。

## 关键文件

| 文件 | 职责 |
|------|------|
| `src/persona.py` | `PersonaState`、`load_persona_state`、`update_state`、`consolidate`、`_render_dynamic_block` |
| `src/storage.py::PersonaSnapshot` | SQLite 表 `persona_snapshots(id, ts, payload_json)` |
| `src/memory.py::maybe_flush` | flush 成功后 `asyncio.create_task(persona.update_state(batch))` |
| `src/scheduler.py` | `persona_consolidate_job` cron `hour=3 minute=7` |
| `src/agent.py` | 没改——`load_persona_state()` 接口不变，自动拿到带动态段的 body |
| `src/prompts.py::build_system_prompt` | 没改——直接用 PersonaState.body |

## 调试 / 观察

```bash
# 当前最新 snapshot
.venv/bin/python -c "
from src import persona, json
print(json.dumps(persona._load_latest_payload(), ensure_ascii=False, indent=2))
"

# 看动态段怎么渲染的
.venv/bin/python -c "
from src import persona
state = persona.load_persona_state()
idx = state.body.find('# 你最近的状态')
print(state.body[idx:] if idx > 0 else '(无动态段——首次启动 / 全中性 / observations 为空)')
"

# 强制跑一次 consolidate（排查衰减是否生效）
.venv/bin/python -c "from src import persona; persona.consolidate()"

# 看历史 snapshot 时间线（每条 update 一行）
sqlite3 data/app.sqlite "SELECT id, ts, substr(payload_json,1,80) FROM persona_snapshots ORDER BY id DESC LIMIT 20;"
```

## 已知限制 / 未做

1. **LLM 判断 noisy**：MiniMax-M2 的 trait_deltas 偶尔偏热情（连续好几轮都给 +0.05）。靠每日 consolidate 衰减兜底；如果发现某 trait 飘到 ±0.8 以上长期不回落，可调 `MAX_DELTA_PER_UPDATE`（当前 0.15）或加更激进衰减。
2. **observations 没语义去重**：只做精确字符串去重。同一类观察换说法仍可能重复（如"你最近毒舌一些""你这两天玩笑变多"）。consolidate 时不合并，靠 TTL 兜。
3. **milestones 没去重**：永久保留意味着可能堆。当前 LLM prompt 要求"通常 0 条"，实测一周也就 0-3 条，问题不大。日后超 20 条再加裁剪。
4. **不区分 user 主导 vs assistant 自演**：用户的反应是主要信号，但 batch 里 AI 自己说了什么也会影响 LLM 判断。可接受——AI 也"知道自己说了什么"是合理的。
5. **没暴露给 admin UI**：当前 admin UI 只看 memU 记忆。后续可加一个 `/persona` tab 看 traits 时间线 + observations 列表。
6. **冷启动**：第一次跑、`persona_snapshots` 表空 → `_load_latest_payload()` 返回全空 payload → `_render_dynamic_block` 返回空字符串 → body 等于 baseline。第一次 flush 之后才会出现动态段。

## 已踩过的坑

### observations 风格污染会形成反馈循环（2026-05-07 修复）

aux LLM 写 observation 时如果用书面/学术词（"分析框架""欲盖弥彰""预设反应"），这些字符串会被
注入 system prompt → 下一轮回复风格被拽向书面 → 下次 update 看到的 batch 也书面 → 新
observation 仍然书面。**正反馈把整体语气推向论文腔**，且越聊越严重。

修复关键是 `_UPDATE_SYSTEM` 里**明确禁用学术词列表**（分析框架/预设/认知/元认知/欲盖弥彰/模板化/
引导/层次/维度/反应模式），并强调 observation 是"我自己怎么样了"（话变少、话痨、被噎住），
不是"我作为 AI 怎么应对的"这种元层面分析。

第二个修复是发现污染后**手动清空当前 observations + mood**（保留 traits/milestones）：
不清掉的话下一轮还是被旧 observation 拽住，新 prompt 没法替换正在生效的污染。

```bash
# 紧急清掉 observations + mood（traits/milestones 保留）
.venv/bin/python -c "
from src import persona
cur = persona._load_latest_payload()
persona._write_snapshot({
    'traits': cur.get('traits', {k: 0.0 for k in persona.TRAIT_KEYS}),
    'mood': '',
    'observations': [],
    'milestones': cur.get('milestones', []),
})
"
```

### `_render_dynamic_block` 触发阈值与展示阈值不一致（同上日修复）

旧版："任一 trait |v| ≥ 0.05 就触发渲染" + "trait |v| ≥ 0.2 才在展示里列出"——导致 traits 微漂、
obs/mood 全空时输出"# 你最近的状态" 标题但下面什么都没有的空段。

修复：先收集所有要展示的 sections（mood / deviated_traits / obs / milestones），任一非空才输出
整段含标题；全空直接 return ""。

## 演化曲线参考

人为构造一个"用户连续 5 天接得住毒舌、走心多"的场景，traits 大致：

| 天 | sarcasm | warmth | 备注 |
|----|---------|--------|------|
| 0 | 0.00 | 0.00 | 初始 |
| 1 | 0.10 | 0.08 | 一天 5-10 次 flush，每次 +0.05~0.10 |
| 2 | 0.18 | 0.16 | 衰减 -0.01 但累积 +0.10 |
| 3 | 0.25 | 0.22 | |
| 5 | 0.40 | 0.35 | 单 trait 偏强档（≥0.2 列出） |
| 停止后第 7 天 | 0.20 | 0.18 | 7 次衰减 0.92^7 ≈ 0.56 |
| 停止后第 14 天 | 0.10 | 0.09 | |
| 停止后第 30 天 | 0.02 | 0.02 | 基本回中性 |

设计意图是"性格漂移有惯性，但停了也会回"——既不完全失忆也不僵化。
