# 记忆架构 PRD（探索）

> **核心立论**：记忆问题是**架构工程问题**，不是模型问题、不是聊天问题。
> 现有所有公共记忆系统的"级联更新 / 消亡推理"准确率 ≤ 3%——
> 用更强的 LLM、更长的 prompt、更深的 retrieve 都解决不了；
> 唯一能逼近的方法是"全上下文喂最强模型"，单次成本约基线的 70 倍。
> 这意味着**门槛不在模型，在记忆架构本身没提供充分的信息**。

---

## 1. 现状：我们当前的记忆栈

| 层 | 实现 | 问题 |
|---|---|---|
| 短期上下文 `_recent` | dict[uid, list[12 轮]]，落盘 `data/recent.json` | OK——重启也能接续 |
| 长期记忆 memU | postgres + pgvector，RAG 召回，按 user_id 分片 | RAG 通病——见下 |
| persona 动态层 | 每用户的 `traits / mood / observations / milestones`，每日 03:07 衰减 | 脱离记忆事实层，独立运转 |

**我们用 memU 1.5.x，本质上就是一个带 embedding 的 KV 库。所有事实都是孤立条目。**

---

## 2. RAG-only 记忆的三个根本缺陷

### 2.1 结构扁平，没有权重

RAG 优势：召回强，按语义不按关键词检索。
RAG 劣势：所有条目同一层级——
- "刚才说今天累" 跟 "我是同性恋" 一样重要
- "三个月前提过想吃辣" 跟 "上周确诊抑郁" 同样会被召回
- 没有**重要性 / 持久性 / 时效性**的维度

### 2.2 越记越多 = 人设越漂

陪伴 AI 强调"长期陪伴 / 持续学习"——它努力记住每件事。
但**记忆越多，风险越高**：当系统没有清晰分层时，AI 会以为所有信息都一样重要。
- 短期情绪 = 长期偏好
- 一时玩笑 = 深度信念
- 一次错误标注 = 恒久设定

久而久之，人设自然变味。我们已经亲眼看过：admin user_id 因为我一次测试塞了 4 条假对话，立刻被抽进 4 个 category summary，要手工删才能洗干净。

### 2.3 事实之间没有关系——这是最致命的

**当前所有主流记忆框架（包括我们用的 memU）只存"单个事实"，不存"事实之间的依赖关系"。**

一个事实变了，依赖它的记忆不会自动更新。

#### 具体例子

bot 记住了 user 的 4 条事实：

1. 上班骑车通勤 15 分钟
2. 开了一个理发店的会员卡
3. 住在北京
4. 交北京社保

这时用户说一句：**"我搬去上海了"**——

| 现状（所有 RAG 系统） | 应有行为 |
|---|---|
| 第 3 条更新为"住在上海"，其他三条不动 | 系统应当意识到 1/2/4 都依赖于"住在北京" |

那剩下三条呢？
- **通勤 15 min**：应当被标记为 ❓ 待确认（地理变了，路径很可能变）
- **理发店会员卡**：✗ 大概率失效（北京的店去不了了）
- **北京社保**：❓ 待确认（迁过去要时间，可能还在交一段）

**没有一个 RAG 系统能自动做到这件事**——因为底层根本没存"通勤时长 ⊃ 居住地"这条依赖。

---

## 3. 学术现状：MEME 评测结果

> "Evaluating six memory systems spanning three memory paradigms on 100 controlled episodes,
> we find that all systems collapse on dependency reasoning under the default configuration
> (**Cascade: 3%, Absence: 1%** in average accuracy) despite adequate static retrieval performance.
> Prompt optimization, deeper retrieval, reduced filler noise, and most stronger LLMs fail to
> close this gap. Only a file-based agent paired with Claude Opus 4.7 as its internal LLM
> partially closes the gap, but at **~70× the baseline cost**."
>
> — *MEME: Multi-entity & Evolving Memory Evaluation*

**6 个开源记忆系统、3 种范式，全部在依赖推理上崩溃**。
唯一能 partially 逼近的方法：所有上下文一次性喂 Opus 4.7——单价 70× 起。

**结论：在主流架构上，靠模型推理出依赖关系是不可靠的。**

---

## 4. 已研究的解决方案（评估）

### A. 写入/更新时触发影响分析
- 思路：每次写入或更新，自动检索语义相关条目，让 LLM 判断"仍有效/待确认/已失效"
- 成本：与现有记忆量正相关；50 条记忆每次更新都要 50 次 LLM 调用
- 问题：模型判断不一定准

### B. 显式依赖图
- 思路：写入时除了事实本身，还存它依赖的前提条件
- 上游条目更新 → 自动把下游标"待确认"
- 问题：依赖关系本身也需要 LLM 抽，也不一定准。**bug 复合 bug**

### C. 置信度衰减 + 主动确认
- 每条记忆挂两个属性：最后更新时间、置信度
- 上游更新 → 下游置信度降低
- 低置信度记忆被召回时 → 反过来问用户确认
- 问题：用户体验差（动不动被反问）；置信度计算依然靠 LLM 推理

### D. 全量上下文 ✅ 唯一有效
- 把所有历史一次性喂最强的模型
- **70× 成本**
- 不可规模化

---

## 5. 我们项目的可选方案（不一定有效）

按"成本/收益/可实施性"权衡，三个候选叠加方案：

### 5.1 写入时筛一遍，把可能影响的条目设为「待验证」

实现：每次新 item 入库后，用一个轻量 LLM（如 deepseek-flash）跑：
> "这条新事实「我搬去上海了」，可能让以下哪些旧条目失效？"
> 输入：top-k 语义最近的旧条目 → 输出：哪些标记 stale。

成本可控（每 flush 一次额外几次 LLM call）。**漏网之鱼无法避免**。

需要 schema：每个 memory_item 加一列 `status: ENUM('confirmed', 'to_verify', 'stale')`，默认 confirmed。

### 5.2 调用时，如果是「待验证」，反向推理确认

实现：`recall()` 召回 to_verify 条目时，先让一个轻量 LLM 判断是否仍成立，再决定塞入 system prompt。
- 成立 → status = confirmed
- 不成立 → status = stale，不召回

代价：每次 recall 多一次 LLM 调用 / 待验证条目。前提是 5.1 把范围筛得足够小。

### 5.3 后台定期批量扫描清理（"Auto Dream"）

实现：每天凌晨（搭便车 persona_consolidate 03:07 那班车）跑一次：
- 取 status='to_verify' 的条目
- 找它语义相近的 confirmed 条目作为 context
- 让 LLM 整体判断一遍，更新 status / 合并 / 删除
- 输出 "今夜更新报告" 到 audit

类似人睡觉时大脑做记忆整合的机制。**也是这个方案的浪漫名字来源**：让 bot 自己"做梦"整理记忆。

---

## 6. 每个记忆条目应有的属性

> （扩展自原 stub，加上方案需要的字段）

| 属性 | 说明 |
|---|---|
| `content` | 记忆内容（文本） |
| `evidence_ref` | 事实索引——可回溯到原聊天记录的 resource_id + 消息片段 |
| `created_at` | 首次写入时间 |
| `last_updated_at` | 最后更新时间 |
| `last_verified_at` | 最后一次被验证仍成立的时间（用于置信度衰减） |
| `status` | `confirmed` / `to_verify` / `stale` |
| `confidence` | 0.0 - 1.0（置信度，stale 时为 0） |
| `depends_on` | 该条目依赖的其它 memory_item id 列表（写入时由 LLM 抽） |
| `depended_by` | 反向索引（自动维护，让上游更新时能 O(1) 找下游） |
| `category_ids` | 分类（已有） |
| `embedding` | 向量（已有） |

---

## 7. 风险 & 不解决的事

- **依赖图本身的准确率**：写入时让 LLM 抽 `depends_on` 一样会出错，跟主流方案的不可靠根因相同
- **冷启动成本**：现有 memU 库要重新跑一次依赖标注
- **可能没用**：本 PRD 列的 5.1+5.2+5.3 三件套，本质是 A+B+C 三种学术方案的组合，每个单独不超过 3% 准确率，组合起来不一定有质变
- **不做的事**：不打算做"全量喂上下文"——成本爆炸

**最差情况下**：架构改了，准确率没显著提升，但**至少做到了"知道自己不确定"**——召回时区分 confirmed / to_verify / stale，不再把所有事实都当 100% 真实灌给主 LLM。这条本身就有价值。

---

## 8. 下一步

1. 先做 **5.1 + status 字段** 跑一段时间，看 to_verify 的命中率（看抽出来的"待验证"条目里多少是真的过时了）
2. 如果第一步效果可观，再加 **5.2** 的 recall 时验证
3. **5.3 Auto Dream** 是终态，等 1+2 跑通再做

实现位置预估：
- `src/storage.py` 不动（memU 自己的 schema）；改在 memU postgres `memory_items` 表上 ALTER 加 `status / confidence / depends_on` 等列（参考 `_ensure_memu_postgres_schema` 的添加机制）
- 写入侧 hook：`memory.py::_flush_one` 在 `svc.memorize` 后跑一遍 5.1
- 召回侧 hook：`memory.py::recall` 在返回前过一遍 5.2
- Auto Dream：新 scheduler job，复用 `persona_consolidate` 的 03:07 同班车

是否值得做：等本 PRD 跟用户对齐后再开工。
