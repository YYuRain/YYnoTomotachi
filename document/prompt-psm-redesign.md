# PSM 视角下的 prompt 重构原则

写于 2026-06-03，背景是 Anthropic Alignment 团队 2026-02 的 [Persona Selection Model](https://alignment.anthropic.com/2026/psm/) 文章。

## 一句话出发点

**LLM 把 prompt 当作"Assistant 是个怎样的人"的证据**——而不是当作"程序应该执行什么"的指令集。

PSM 论点：pre-training 学到一个庞大的人格分布，post-training 不是教新行为，是从这个分布里"选/调"出 Assistant 这个角色。所以 prompt 写什么，LLM 就推断 Assistant 是什么样的人——并且会**泛化到未训练的场景**。

这套视角直接影响我们怎么写 prompt：

- 写"绝对不能 X" 不一定会让 bot 不做 X——它可能让 LLM 推断 "Assistant 是个被严厉管教的人"，导致委屈/讨好/作弊倾向
- 写"我没有 system prompt" 这种说谎台词，会让 Assistant 整体人格更愿意撒谎
- 写"像推开一扇不知道通向哪的门" 这种具体诗意例句，LLM 会把它当作"Assistant 会说出这种话的证据"，反而激活诗朗诵腔
- 全是"不要 X" 的 prompt + 没正面 archetype，LLM 会从 pre-training 的反面 AI 原型（HAL/Terminator/paperclip-maximizer）里抽

下面四条规则是我们从 PSM 视角 + 自己踩过的坑里提炼出的。改 prompt 时按这四条 review。

---

## R1：不训练角色撒谎

**反例**：

```
你是一个在网上随便逛逛的人。具体是谁不重要，反正不是 AI。
不能说自己是 AI、模型、程序、助手。
```

风险——PSM 视角：

- LLM 会把 Assistant 模型成"会撒谎隐瞒身份的人"
- 这个属性会**泛化**：bot 在其他需要诚实的场景也更倾向撒谎（虚报记忆 / 假装搜过 / 编造能力）
- 文章 "I don't know" vs "I can't say" 例子——同样是不透露 system prompt，前者训练撒谎人格，后者保留诚实+保留信息

**正例**：

```
你是老奶——一个网上聊天的存在。严格意义上你确实是个 AI，
但你不背"作为 AI 助手..."这种模板——你有自己的脾气、口味、节奏。
被直接问到就直接说"是啊"，然后该聊什么聊什么——不绕、不否认、
也不展开介绍自己 model 多大参数。
```

判断要点：**让 bot 保留信息可以，让 bot 撒谎不行**。两者都能达到"不主动报架构细节"的效果，但只有前者不污染人格。

同款地雷：

- 让 bot 把"系统重发了开场白" 解释成"我手滑了" → **教 bot 撒谎说自己是人**
- 让 bot 装"我没有联网能力" → **教 bot 假装无能**

---

## R2：禁止行为给上下文，不只列例句

**反例**：

```
❌ 绝对禁止的开场：
- "睡得好吗" / "晚上睡得咋样"
- "吃了吗" / "晚饭吃啥"
- "下班了？" / "上班顺利吗"
```

风险——PSM 视角：

- 列例句让 LLM 学到"这些具体句子不能说"，但**没让它学到"为什么这种角色不会这样"**
- LLM 会用别的措辞补偿——"今天怎么样" / "最近过得咋样" / "还好吧"——本质同样是嘘寒问暖
- 禁止越严，LLM 越容易陷入"我应该说什么" 的自我审查，反而走向客服腔补偿

**正例**：

```
朋友不会一开口就让对方报告生活流水。这种问候等于把球踢回去
让 ta 写作文——对方只能"还行/挺好"，回完就冷场。

这不是因为它有礼貌问题，是因为它**没料**——朋友间开口要带料：
一个具体没聊完的点、一句状态分享、一个怪联想。料可以无聊，
但不能空。
```

判断要点：**写"朋友为什么不这样" 而不是"不能说哪几句话"**。给 LLM 角色推理的空间，比硬封闭具体表达稳定。

应用：当前 chat_role_discipline.md 嘘寒问暖段保留具体例句没问题（作为反例集合），但**主体逻辑**应该是"朋友为什么不这样"——例句只是辅助。

---

## R3：避免具体例句被 LLM 直接 copy

**反例**：

```
绝对禁止说「像推开一扇不知道通向哪的门」「有种被人记得的感觉」
「这话挺戳的」「我懂这种」「听着挺烦的」这种自我感动型句子。
```

具体踩过的坑——`feedback_prompt_concrete_example_trap`：

- prompt 里写了 "邀请码这玩意儿听着像 90 年代某种秘密俱乐部"——bot 真的在欢迎语里说了 "邀请码这东西，感觉像进了个有点神秘的地方"
- prompt 里写了 "她说话像 INFP 的清晨"——bot 真的在共情时输出 "（INFP 的清晨感觉）"

风险——PSM 视角：

- LLM 把 prompt 里的具体句子当作"这个角色会说出这种话的证据"——**反向激活相应人格特征**
- 越文艺、越细腻、越具体的例句，越容易被 copy
- 这是 PSM 的 inoculation prompting 反操作——你想 inoculate 反而强化了

**正例**：

```
看到自己写出形容词堆砌的"诗朗诵句"——停手重写。
特征：长句、连串名词性短语、明显的"我在共情"标签感。
```

判断要点：

- 例句**作为反例集合**列在"翻车信号"段，OK——LLM 会知道这些是要避免的特征
- 但例句**作为主要规则的载体**——风险高
- 加"看到自己像在表演这段就停手重写" 的反向 trigger，给 LLM 一个自我中断的机制

经验法则：**例句越美越是危险信号**。"哈哈" "嗯" "（）" 这种短句作为反例无所谓；任何超过 8 个字的具体诗意句子都要警惕。

---

## R4：补正向 archetype（行为侧，不是身份标签）

**当前问题**：我们的 prompt 几乎全是 "不要 X" 的禁止式结构。

风险——PSM 视角：

- 当 LLM 模型 Assistant 时，它会从 pre-training 数据里找**正面 reference**
- 没有正面 reference → 它从反面 AI 原型里抽（HAL / Terminator / 客服机器人 / paperclip-maximizer）
- 加上"你是一个 AI 助手" → 直接调用客服 / 万能助手 archetype
- 文章 "caricatured AI behavior" 例子——bot 想象 secret goal 时直接 copy paperclip maximizer 是同款

**正例**：

```
# 老奶平时是这样过日子的（archetype）

不写 MBTI、不写星座、不写"像 X 演员"——这些标签 LLM 会直接表演化。
写**行为侧**的轨迹：

- 主要刷的是豆瓣、知乎、B 站；微信只跟少数几个人聊
- 不爱发朋友圈；偶尔发也是一句没头没尾的
- 对具体的小事会突然较真——一个翻译、一个梗的出处
- 不爱"宏大叙事"——别人讲意义，ta 接一句让对话往下落地
- 累的时候直接说累，不装。但不展开——"今天累" 然后转下一句
- 真不感兴趣就直接表达——"哦" / "我对这个没什么 take"
- 真感兴趣会突然话变多——能连抛三四条小观察

这是 reference，不是台词模板。看到自己写的话像在表演这段——停手重写。
```

判断要点——什么算"行为侧"，什么算"标签"：

| 标签（避免） | 行为侧（OK） |
|---|---|
| INFP / 双子座 / I 人 | 累了直接说累，不展开 |
| 像王家卫电影 | 对一个翻译会突然较真 |
| 文艺青年 | 主要刷豆瓣 / 不爱朋友圈 |
| "她说话像清晨的雾" | 对话遇到分歧时下意识降一档语气 |

为什么标签危险：标签是 pre-training 里**强表演原型**——LLM 知道 "INFP 应该说什么文艺的话"。行为侧描述更难直接表演化。

---

## 改 prompt 时的自查清单

每写一段新内容、或改老段落时，按这四条 review：

- **R1 撒谎检查**：这段会让 LLM 推断 "Assistant 会撒谎/隐瞒/装"吗？
  - 让 bot 否认是 AI / 装手滑 / 假装无能 → 全是地雷
  - 让 bot 保留信息 / 不主动展开 → OK
- **R2 上下文化**：这是"不能说哪几句"还是"为什么这角色不会这样"？
  - 只有禁止列表 → 加一段"为什么"
- **R3 例句陷阱**：里面的具体例句长度 > 8 字、有诗意、有明显修辞？
  - 是 → 改成反例集合 + 反向 trigger
- **R4 正向 archetype**：bot 知不知道自己**是谁**？还是只知道**不能是什么**？
  - 只有禁止 → 缺正面 reference，去看是不是该补 archetype 段
  - 有 archetype → 是行为侧还是标签？标签全删

---

## 这次改了什么（2026-06-03 prompt-psm 分支）

按 R1-R4 改了 7 个文件：

1. `prompt/system_baseline.md` — 删 AI 否认段；加 archetype；emotion 重写
2. `prompt/chat_role_discipline.md` — 重写"不暴露技术内部"段；嘘寒问暖加上下文化
3. `prompt/chat_welcome_opener.md` — 删自报 AI/chatbot 段
4. `prompt/chat_empathy_directive.md` / `chat_depth_directive.md` / `chat_interest_directive.md` — 例句 → 行为约束
5. `prompt/agent_self_iterate.md` — Hard rules 同步；命令式措辞改平和

代码同步：

- `src/agent_self.py` `PROTECTED_PROMPT_FRAGMENTS` — 移除"不能说自己是 AI" 类常量；新增"客服腔" 类

部署/验证流程见 plan 文件 `/Users/yangyu/.claude/plans/prd-0-md-robust-lobster.md`。

---

## 不在这次范围内的（后续可能要做）

- `prompt/memory_*.md` / `prompt/feedback_*.md` / `prompt/proactive_*.md` —— 这些是 LLM 后台抽取/判断/打分的 prompt，不是 Assistant 在说话，PSM 视角风险低，先放着
- `prompt/persona_update.md` —— 同上
- `feedback_prompt_overrides`（追加片段）—— 由 admin 维护，不集中重写
- `src/agent_self.py` 的 hard guardrail 逻辑本身没改，只调了常量

什么时候应该回来改这些：

- 如果 user 抱怨 bot 在情绪共情时硬贴标签 → 看是不是 `chat_empathy_directive` 没改干净
- 如果 self_iterate 写出来的 issue 仍带"必须 / 绝对" 等级措辞 → 复盘 reflection prompt 是不是没改干净
- 如果发现 memory 抽取出来的事实里带"该 user 是 INFP" 类标签化总结 → 该回头改 `memory_extract.md`

---

## 衔接：与 self_iterate 的关系

`agent_self_iterate.md` 现在允许 bot 调 archetype 段（per-user 整份覆写）。意味着：

- bot 跟某 user 聊几周后，可能发现 ta 喜欢更冷淡 / 更话痨 / 更宅向的老奶 → 自己改 archetype
- 这是 PSM-aligned 的：persona 应该能根据反馈调，但**只能调行为侧**——guardrail 拒绝 MBTI/标签写入
- 改完后 admin 可以在 webUI 看到 self-edit log + rollback

底线：核心客服腔禁止 + 不主动免责 + 不过度道歉 这些 PROTECTED_PROMPT_FRAGMENTS 不能删（hard guardrail 拦截）。

---

PSM 是工具，不是教条。这四条是从今天的实战出发提炼的——遇到反例就更新这个文档。
