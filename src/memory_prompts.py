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

## 输出格式（严格 JSON，无任何额外文字、不要包 ```json 代码块）

```
{{"items": [
  {{"type": "profile", "content": "用户最近在减肥"}},
  {{"type": "event", "content": "用户昨天去爬了香山，崴了脚"}}
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
