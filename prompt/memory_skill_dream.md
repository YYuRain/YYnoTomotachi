# 任务（凌晨整理 skill 仓库）
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
**整段输出必须是且只是一个 JSON 对象**。