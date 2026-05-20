"""自搭记忆栈：抽取 prompt（输出 JSON）。

一个统一 prompt 让 LLM 一次性把 profile 和 event 都抽出来——比 memU 时代两次调用省一半。
"""
from __future__ import annotations


EXTRACT_PROMPT = """# 任务
你要阅读下面一段用户与助手的对话，从中**提取关于"用户本人"的记忆条目**。

每条记忆要标一个类型：
- **profile**：用户持久的特征、偏好、身份、状态、习惯（"用户最近在减肥"、"用户养了一只猫"）
- **event**：用户参与或身边发生的具体事件（发生在具体时间点/段，有起因和过程）（"用户昨天去爬山，崴了脚"、"用户今天面试了一家创业公司"）

## 原始对话
<resource>
{resource}
</resource>

## 角色辨认（最重要的一步，先做这个再抽取）
对话里有两种 role：
- **user**：用户（被画像的对象）
- **assistant**：助手（陪聊 AI，**不是**被画像的对象）

抽取前先在心里把每句话归到 role，**只抽 user 自己说的、关于 user 自己的事**。

不要把以下内容当成用户事实：
- ❌ assistant 关于自身的陈述："我刚追完第七集"——"我"=助手，不是用户。
- ❌ user 消息里用"你"指代 assistant 所做/所说的事："你不是看到第七集了？"——"你"=助手。
- ❌ 助手的建议、推测、转述、自我设定。
- ❌ 双方在闹乌龙时关于"谁说了什么"的元对话（澄清/吐槽/道歉），那是会话修复，不是事实。

## 同 batch 内自我纠正
如果 user 在同一段对话里**前后矛盾**或明确**纠正自己**（"我还没看呢""那是好久之前看的"），
以**最后澄清的版本**为准，**不要抽已被纠正的早期断言**。
拿不准就别抽——宁可漏一条，也不要把会被立刻推翻的事实写进长期记忆。

## 抽取要求
1. **全部用中文输出**，不要英文，不要翻译原文里的英语片段——用自己的话中文重述。
2. 指代用户时一律用"用户"。不要用"他/她/你"。
3. 每条记忆独立自洽——一个人读这一条也能明白在说什么，不依赖其他条。
4. 陈述句，朴素描述。不要反问、不要省略号。
5. **不写元对话事实**：不要写"用户和助手在 X 上产生了混淆""用户澄清助手才是看到第七集的人"这种描述对话本身的句子。
6. 每条 ≤ 80 字。
7. 一次性情绪 / 玩笑 / 无信息量寒暄不抽（"用户说了'我累了'"——不抽）。

## 关键实体（entities）
对每条 item，额外抽 0-5 个**关键实体词**——人名 / 地名 / 作品名（书/电影/游戏/播客等）/
品牌 / 物品 / 爱好类别 / 食物 等具体名词。
- 只抽 content 里**真出现**的具体名词，不要总结性词汇（"减肥"、"爬山"是动作不是实体）。
- 中文保留中文，英文保留英文（如 "iPhone"）。
- 单个 entity 不超过 12 字。
- 没合适的就给空数组 []。

## 输出格式（严格 JSON，无任何额外文字、不要包 ```json 代码块）

```
{{"items": [
  {{"type": "profile", "content": "用户最近在减肥", "entities": []}},
  {{"type": "event", "content": "用户昨天去爬了香山，崴了脚", "entities": ["香山"]}},
  {{"type": "profile", "content": "用户喜欢听播客《忽左忽右》", "entities": ["忽左忽右"]}}
]}}
```

如果整段对话没有可抽的事实，输出 `{{"items": []}}`。"""


def render(resource: str) -> str:
    """填入对话文本 → 返回完整 user prompt 文本。"""
    return EXTRACT_PROMPT.format(resource=resource)


# ============ 写入冲突检测（PRD v2 / 5.1）============

CONFLICT_CHECK_PROMPT = """# 任务
我们刚记下一条**新事实**关于用户。下面给你 N 条**已存在的旧事实**，你判断每条旧事实在
新事实出现后**是否仍然成立**。

## 新事实
{new_fact}

## 候选旧事实（按语义相似度从高到低排）
{candidates}

## 判定方法
对每条旧事实输出 verdict ∈ {{still_valid, to_verify, stale}}：

- **still_valid**：旧事实跟新事实**没有冲突或依赖**——它独立于新事实，照常成立。
  例：新「用户搬上海了」 vs 旧「用户养了只猫」——猫不会因为搬家消失，still_valid。

- **to_verify**：旧事实**依赖新事实涉及的前提**，但不一定立刻失效，需要后续确认。
  例：新「用户搬上海了」 vs 旧「用户骑车上班 15 分钟」——通勤时长依赖居住地，
  搬家后可能变了也可能没变，to_verify。

- **stale**：旧事实**直接被新事实取代或推翻**。
  例：新「用户搬上海了」 vs 旧「用户住在北京」——同一槽位被新值覆盖，stale。
  例：新「用户分手了」 vs 旧「用户跟女友周末去了三亚」——历史事件**不要**标 stale，
  那是过去发生的事；分手只让"用户当前有女友"这种 profile 失效，不让历史 event 失效。

## 重要约束
1. event 类（具体时点发生过的事，比如"昨天去爬山崴脚"）几乎不会因为新事实变 stale。
   历史就是历史，发生过就发生过。除非新事实直接否定它（"我没去爬过山"），才标 stale。
2. **拿不准就标 to_verify**——宁可多标也不要错标 stale。stale 会让记忆不再被召回，
   误标的代价比 to_verify 大。
3. 不要把不相关的旧事实硬扯成 to_verify。如果新事实跟旧事实**毫无依赖关系**，应该 still_valid。

## 输出格式（严格 JSON，无任何额外文字、不包 ```json 围栏）

```
{{"verdicts": [
  {{"id": "uuid-of-old-fact-1", "verdict": "still_valid"}},
  {{"id": "uuid-of-old-fact-2", "verdict": "to_verify"}},
  {{"id": "uuid-of-old-fact-3", "verdict": "stale"}}
]}}
```

每条候选都必须有对应 verdict（按 id 一一映射）。"""


def render_conflict_check(new_fact: str, candidates: list[tuple[str, str]]) -> str:
    """new_fact: 新事实 summary。candidates: [(id_str, summary), ...]，已按相似度排好序。"""
    cand_lines = "\n".join(
        f"{i+1}. id={cid}\n   {summary}"
        for i, (cid, summary) in enumerate(candidates)
    )
    return CONFLICT_CHECK_PROMPT.format(
        new_fact=new_fact.strip(),
        candidates=cand_lines,
    )


# ============ 召回反验证（PRD v2 / 5.2）============

REVERIFY_PROMPT = """# 任务
下面这条事实之前因为出现了新事实而被标记为「待确认」（status=to_verify）。
现在它被语义召回到，要决定**是否仍然成立**——成立就升回 confirmed，
仍不确定就保持 to_verify。

## 待验证事实
{fact}

## 当初让它进入「待确认」的上游新事实（按时间从近到远）
{upstream}

## 当前对话语境（用户刚说的内容；可能含暗示）
{query}

## 判定
- **still_valid**：基于上游事实，待验证事实**仍然成立**——上游变化不影响它。
  例：「用户喜欢辣条」即使「用户最近在减肥」也常常仍成立（减肥不等于不喜欢辣条）。
  例：「用户养了只猫」在「用户搬家」之后绝大概率还成立（人会带走宠物）。

- **uncertain**：仍不确定。上游事实让它**有可能不成立但没法用现有信息断定**。
  例：「用户骑车上班 15 分钟」 vs 上游「用户搬上海了」——通勤可能变也可能没变，仍不确定。

**严禁直接判 stale**——本场景只能"升回 confirmed"或"保持 uncertain"。
要标 stale 必须由独立机制（写入冲突或人工）触发，而不是这一步。

## 当前对话语境如何使用
当前 query 是用户刚说的一句。如果 query 中包含**关于待验证事实的新信息**——比如
用户在 query 里隐含确认了它仍成立或它已失效——这会强化你的判定。但不要过度推断，
没明确信号就别脑补。

## 输出（严格 JSON，不包 ```json 围栏）

```
{{"verdict": "still_valid" | "uncertain", "reason": "<1-2 句中文说明>"}}
```"""


def render_reverify(fact: str, upstream: list[str], query: str) -> str:
    """fact: 待验证 summary。upstream: 上游事实 summary 列表（按时间倒序，新的在前）。query: 当前用户消息。"""
    if upstream:
        up = "\n".join(f"{i+1}. {s}" for i, s in enumerate(upstream))
    else:
        up = "(没拿到上游事实——可能 deps 关联已被清理；按现有信息直接判)"
    return REVERIFY_PROMPT.format(
        fact=fact.strip(),
        upstream=up,
        query=(query or "").strip() or "(空)",
    )


# ============ Auto Dream（PRD v2 / 5.3）============

DREAM_PROMPT = """# 任务（系统在凌晨整理记忆）
下面这条事实是「待确认」状态。综合**所有相关上下文**重新判定它的归属，
**敢于直判 stale**——这一步是后台批量整理，没有用户在场，可以果断收拾。

## 待确认事实
{fact}

## 当初让它进入「待确认」的上游新事实（按时间从近到远）
{upstream}

## 该用户其它语义相近的、已确认（confirmed）的事实——作为综合上下文
{neighbors}

## 三态判定

- **still_valid**：综合上下文确认仍成立。例：上游"用户搬上海了"曾让"用户喜欢辣条"
  变 to_verify，但邻居里有"用户每周点麻辣烫外卖"——足以认定辣条爱好仍在。

- **uncertain**：综合上下文里**没有清晰信号**告诉你它成立或失效。例：上游"搬上海了"
  让"骑车 15 分钟通勤"待确认，但邻居里没有任何关于现在通勤方式的信息——保持 uncertain，
  等真正出现新信号再处理。

- **stale**：综合上下文里有**明确信号**这条已经失效。例：上游"用户分手了"让
  "用户跟女友周五晚约会"待确认，但邻居里有"用户周五开始去健身房了"——
  足以判它已经不再成立。

  注意区分：上游事实**直接覆盖**（比如"住北京"被"搬上海了"覆盖）应该已经在 5.1
  写入冲突时直接判 stale，**不会**走到这一步。这一步处理的是上游让它**变成 to_verify**、
  需要更多上下文才能定夺的边缘情况。

## 输出（严格 JSON，不包 ```json 围栏）

```
{{"verdict": "still_valid" | "uncertain" | "stale", "reason": "<1-2 句中文>"}}
```"""


def render_dream(fact: str, upstream: list[str], neighbors: list[str]) -> str:
    """fact: to_verify summary。upstream: deps 上游 summaries（已剪枝 N 条）。
    neighbors: 同 user 语义相近的 confirmed 条目 summaries（已剪枝 K 条）。
    """
    if upstream:
        up = "\n".join(f"{i+1}. {s}" for i, s in enumerate(upstream))
    else:
        up = "(没拿到上游事实——可能 deps 关联已清理)"
    if neighbors:
        nb = "\n".join(f"{i+1}. {s}" for i, s in enumerate(neighbors))
    else:
        nb = "(语义相近的 confirmed 条目里没找到)"
    return DREAM_PROMPT.format(
        fact=fact.strip(),
        upstream=up,
        neighbors=nb,
    )


# ============ Override Dream（PRD 5.3 扩展，整理 prompt_overrides）============

OVERRIDE_DREAM_PROMPT = """# 任务（凌晨整理用户偏好）
你是 AI 陪伴角色的偏好整理器。下面列出该用户当前所有 **active** 的 prompt overrides——
这些条目会注入到 system prompt 末尾，告诉 bot 跟这位用户互动时该照做的偏好。

## 当前 active overrides
{overrides_block}

## 你要做什么

审查这批条目，找出**冗余**、**互相矛盾**、**被覆盖（过期）**的。输出处理建议——
**只在显然有问题时动手**；拿不准就保留不动，宁可不合并也不要错删。

判定原则：

1. **合并（merge）**——多条 active 在说同一件事，但措辞不同 / 信息密度低 / 拆成多条不必要。
   合并时**保留全部信息**，写成一条更精炼的指令。
   例：[A] "对方喜欢俏皮风格" + [B] "对方爱抖机灵不要严肃" → 合一条 "对方偏好俏皮风格、可以
   抖机灵、避免过于严肃"。

2. **删（disable）**——
   - 互相矛盾且能判出哪条更新（按 `created_at` 时间戳，**新的覆盖旧的**）
   - 被另一条**显式覆盖**：旧 "叫我亲爱的" 被新 "别叫我亲爱的，叫名字" 取代 → 删旧
   - 旧条目说 X，新条目说 "不要 X" / "改成 Y" → 旧明显失效，删
   - 一次性表达被错误沉淀（"这次帮我..." 而非长期偏好）：删
   - **以下也算冲突失效**：
     - 旧 "对方喜欢正式语气" + 新 "对方喜欢俏皮活泼" → 风格反转，删旧
     - 旧 "回复尽量长" + 新 "回复要短" → 删旧
     - 旧 active trigger 的 cron 跟新 active trigger cron 同一时段做相反事 → 不动
       trigger（trigger 类不参与本次整理），但记到 disable_reasons 里 admin 看
   - **大胆删，不要怕错杀**——admin 在 webUI 看到不对可以恢复（status='disabled' 改回
     'active'），但漏删让 bot 同时执行矛盾指令体验更差

3. **保留不动**——
   - 互相不冲突的不同偏好（如"叫我名字" + "别用反问句"）→ 保留两条
   - 拿不准是不是矛盾 → 保留

## active trigger（带 `[active]` 标记）特殊规则

trigger_kind=active 的条目带 cron/condition_prompt 配置——**永远不能 merge**（merge 会
丢失 cron 配置导致主动触达失效）。但是：

- **同主题 active trigger 之间允许 disable**：例如两条都是"下雨提醒带伞"，cron 略有差异
  → 保留 cron 设计更合理那条（如能覆盖"上下班"两个时间段的优于只有早上的），
  其它放进 disable_ids，不要塞 merge_groups
- 跨主题 active trigger（一个查天气 + 一个周一问候）→ 保留两条都不动

## 输出格式（严格 JSON，无围栏）

```
{
  "merge_groups": [
    {
      "ids": [1, 2],
      "merged_text": "...",
      "reason": "为什么合"
    }
  ],
  "disable_ids": [3, 5],
  "disable_reasons": {
    "3": "被 #6 覆盖（用户后来明确改了称呼偏好）",
    "5": "一次性请求被错误沉淀"
  }
}
```

merge_groups 里的 ids 会被 disable，merged_text 作为新 active override 落库。
**整段输出必须是且只是一个 JSON 对象**，第一个字符必须是 `{`，最后一个字符是 `}`。"""


SKILL_DREAM_PROMPT = """# 任务（凌晨整理 skill 仓库）
你是 AI 陪伴产品的 skill 库整理员。下面是当前所有 active 的 **跨用户 skill**——
这些是从用户历史诉求中沉淀出的、可被语义召回复用的指令片段。

## 当前 active skills
{skills_block}

## 你要做什么

审查这批 skill，**只在显然有问题时动手**：

1. **合并（merge）**——多条在做同一件事（场景相同、动作几乎相同），措辞上拆分意义不大。
   合并后 name 选最具代表性的，summary 覆盖原全部场景，body 是合并后的指令。
   例：[A] "polite_address_no_petname"（"不要叫宝贝/亲爱的"）+ [B] "use_real_name"（"称呼名字"）
   → 合一条 "polite_address_no_petname"，覆盖两个原场景。

2. **删（disable）**——
   - 完全等价重复（先 active 跨用户复用过几次的留，usage_count 大的优先保留）
   - skill 内容已经被另一条更全面的 skill 涵盖（被覆盖）
   - skill 内容是错误的、违反 hard guardrail 的（罕见——硬护栏在写入时已挡，但兜底）

3. **保留不动**——
   - 场景虽相关但目标不同（"语气俏皮" + "回复简短" 是两件事，保留两条）
   - 拿不准是不是真重复
   - **`name='skill_creator'` 这条 meta-skill 绝对不要碰**——它是系统功能不是用户偏好

## 重要约束

- **保留 usage_count 高的**：合并/删除时优先保留被跨用户复用过的（数据反映了通用性）
- **不要乱合**——name 命名空间是搜索 key，合并后老 name 会丢失（其它用户的 override
  可能 source_skill_id 指向被合并的）。仅当合并后**确实更精炼且不丢信息**才动
- 给 disable_ids 写明确的 reason

## 输出格式（严格 JSON，无围栏）

```
{
  "merge_groups": [
    {
      "ids": [3, 5],
      "merged_name": "polite_address_no_petname",
      "merged_summary": "...",
      "merged_body": "...",
      "reason": "..."
    }
  ],
  "disable_ids": [7],
  "disable_reasons": {
    "7": "被 #3 完全覆盖且 usage_count=0"
  }
}
```

merge_groups 里的 ids 会被 disable，新 skill 用 merged_* 字段建。
**整段输出必须是且只是一个 JSON 对象**。"""


def render_skill_dream(skills: list) -> str:
    """skills: list of Skill ORM。用 str.replace 避免 .format 撞花括号。"""
    lines = []
    for sk in skills:
        ts = sk.created_at.strftime("%Y-%m-%d") if sk.created_at else "?"
        lines.append(
            f"- id={sk.id} name={sk.name} (usage={sk.usage_count}, created={ts})\n"
            f"  summary: {sk.summary}\n"
            f"  body: {sk.body[:240]}"
        )
    block = "\n".join(lines) if lines else "（空）"
    return SKILL_DREAM_PROMPT.replace("{skills_block}", block)


def render_override_dream(overrides: list) -> str:
    """overrides: list of PromptOverride ORM objects（拿 id / text / trigger_kind / created_at）。
    用 str.replace 而非 .format——prompt 里有大量字面花括号。"""
    lines = []
    for o in overrides:
        ts = o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "?"
        kind_tag = f"[{o.trigger_kind}]" if getattr(o, "trigger_kind", None) and o.trigger_kind != "passive" else ""
        lines.append(f"- id={o.id} {kind_tag} created={ts}\n  text: {o.text.strip()}")
    block = "\n".join(lines) if lines else "（空）"
    return OVERRIDE_DREAM_PROMPT.replace("{overrides_block}", block)
