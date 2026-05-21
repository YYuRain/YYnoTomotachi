# 任务：把"功能希望"转成 trigger-based 指令

用户对 bot 表达了一个 **capability_request**（希望 bot 在某种触发条件下主动做某事）。
你要决定这条诉求适合 **passive trigger**（靠 bot 在主对话流里识别 user 关键词触发）
还是 **active trigger**（系统按 cron 时间主动扫描 + LLM 判条件 + 主动找 user）；
然后输出对应的 JSON。

## 用户原话
{user_request}

## 同对话上下文（帮你理解触发场景）
{resource}

## 两种 trigger

### passive
合适场景：触发时机**强依赖 user 主动提及**——例如「我下次说我累了的时候，让我多歇会儿」
（必须等 user 自己说"累了"才能触发，没有合理的时间点定时检测）。

### active
合适场景：触发时机**有明确的时间窗口或可由系统状态判断**——例如「下班前下雨提醒我」
（每个工作日下班前都该检查一下，不需要等 user 自己说"下班了"）。
- `cron_schedule`：APScheduler 风格 5-field cron，CST 时区。例：
  - `30 17 * * 1-5` = 工作日下午 17:30
  - `0 9 * * 1` = 周一上午 9:00
  - `*/30 * * * *` = 每 30 分钟（一般避免，太频繁）
- `condition_prompt`：丢给 sonnet 判定 + 生成消息的 prompt。LLM 看到它会**先判断条件成立否**，
  成立则**生成一条要发给 user 的话**。约定输出 JSON `{{"should_send": bool, "message": "..."}}`。
  你的 condition_prompt 要明确告诉 sonnet：可用的工具（web_search 查天气等）、
  判定标准、消息口吻。

**两种都需要附带 active_text_for_bot**：一段塞进 user system prompt 的**只读声明**，
告诉 bot **后台已经接管了这件事**，让 bot 不要再主动重复做（cron + active 通道
已经在按时跑了）。

## active_text_for_bot 的硬约束（**最重要！破坏会让 bot 反复打扰用户**）

active trigger 的 active_text_for_bot **必须**只有以下两种内容：
- ✅ 声明：告诉 bot "后台 cron 在 [时间] 自动做 [什么] 已经在跑"
- ✅ 兜底口径：如果 user 主动问起，bot 可以**简短回应当下情况**（比如查一次天气）

active_text_for_bot **绝对不能**写：
- ❌ "如果对方聊到 X 你就主动 Y"——这会让 bot 每次 user 提到关键词都重复触发
- ❌ "你顺手 web_search 查一下..."——主动行为该交给 cron，不该 passive 注入
- ❌ 列举触发关键词（"出门/上班/下班/天气/伞"）——会让 bot 在普通对话里被"上班"两个字勾起整段流程
- ❌ 任何 "你也可以"/"你顺便"/"如果...你就..." 的指令性句式

如果用户的需求**强依赖 user 主动说**（"我说累了你让我歇会儿"）→ kind 选 passive，
不要把"如果说累了你就..."硬塞进 active 的 active_text_for_bot。

## 写作要求

1. 用第二人称"对方"指代 user
2. condition_prompt 要明确：什么算条件成立、用啥工具查（如有）、消息怎么写
3. 承认 active 通道也不是绝对精确——cron 精度取决于触发频率
4. 不要要求 cron 太频繁（避免 < 15 min 间隔，浪费成本）
5. active_text_for_bot ≤ 120 字（短为美——它每个 turn 都注入 system prompt）

## 示例

**用户原话**：你能下班前提醒我看天气吗，要下雨记得告诉我

**好输出**：
```json
{{
  "kind": "active",
  "cron_schedule": "30 17 * * 1-5",
  "condition_prompt": "现在是工作日下班前。你需要：1) 用 web_search 查询「北京昌平 明日天气」；2) 判断明天是否会下雨或有降水概率；3) 如果有雨，生成一条自然口吻的消息提醒对方带伞，例如「明天昌平有雨，伞还在你家吧，记得拿」（≤30 字）；4) 如果没雨，不需要发消息。输出 JSON: {{\\"should_send\\": true|false, \\"message\\": \\"...\\"}}",
  "active_text_for_bot": "对方有'下雨提前提醒带伞'的诉求，每个工作日 17:30 后台 cron 自动查天气并推送。除非对方主动问起天气，否则不要主动提带伞——避免重复打扰。"
}}
```

注意上例 active_text_for_bot：**只声明后台在做 + 加一句"除非对方问，否则别主动提"**。
**没**写"如果对方聊到出门你就查"这种 passive 指令。

## 输出格式（**强制要求**）

你的整段输出**必须是且只是一个 JSON 对象**，**第一个字符必须是 `{`**，最后一个字符是 `}`。
**不要写解释段、不要写"我的输出是："、不要包 markdown 围栏、不要分点列表。**

错误示例（不要这样）：
> 这条诉求是 active 类型...
> ```json
> { ... }
> ```

正确示例（直接这样）：
{{"kind": "active", "cron_schedule": "30 17 * * 1-5", "condition_prompt": "...", "active_text_for_bot": "..."}}

字段约束：
- kind ∈ ["passive", "active"]
- kind="passive" 时：cron_schedule 和 condition_prompt 填 null
- kind="active" 时：三个字段都必填非空
- active_text_for_bot **任何情况都必填**（passive 用它做兜底，active 用它做 bot 在主对话流的备份指令）

## 关键判断

如果用户原话**明确强调"不要我主动问 / 你自己主动找我 / 每天/每周固定时段 X"**——
**强制 kind="active"** 并设合理的 cron_schedule。
不要把"我希望你定时提醒"误判成 passive。passive 仅用于触发场景**强依赖 user 自然提及**
（比如"我下次说累了你别问太多"——必须 user 自己说"累了"才该 trigger）。