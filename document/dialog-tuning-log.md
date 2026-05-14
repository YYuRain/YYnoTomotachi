# 对话风格调优日志

> 每次调整 bot 对话风格时追加一条。最新在上。
>
> 条目结构：**触发**（用户反馈/观察到的问题） / **改动**（具体改了什么） / **生效**（是否重启）。
> 涉及的系统级"死"设计（模块职责、架构）放在 `overview.md` / `extension-points.md`，这里只记"**语气/节奏/话术**"相关的调试。

---

## 2026-05-13 — 用户激活后 AI 生成开场白（welcome opener）

**触发**：上云后多人邀请制——一个新用户走完 `/start <code>` 激活后，仅有"搞定，可以聊了"的系统提示太干，对方很容易想不出说啥然后不聊了。

**改动**：
- `src/prompts.py::WELCOME_OPENER_INSTRUCTIONS` 写一份"对方刚加进来，AI 主动冒个头"的指令——明确禁用客服话术（"欢迎使用"），要求 1–3 句、抛具体小钩子、不要"你想聊什么"。
- `src/agent.py::generate_welcome(user_id)`：新人专用——不召回记忆、不带兴趣、用 baseline persona + welcome 指令喂给主 LLM。
- `src/bot.py` + `src/test_bot.py` 的 `/start <code>` 成功路径：先发系统提示，紧跟一条 `generate_welcome` 输出（走 rhythm.deliver 拆条）。

**生效**：bot 镜像 build 后即可。后续可以在系统提示里调老奶人格的 baseline，开场白会跟着风格走。

---

## 2026-05-12 — 日常态从 INTP 偏向略 ENTP

**触发**：用户："日常态的聊天有些平淡，希望 AI 感到聊天还'蛮有意思'，偶尔抖抖机灵，但不要刻意做作。目前比较 INTP，希望可以 ENTP 一点点，但不要太多。"

**根因**：`System Prompt v0.0.1.md` baseline 把"平"压得太死——
- 「日常闲聊时是平淡的——『嗯』『哦』『知道了』不丢人」 把 baseline 钉在被动应答
- 「state 会明显变活」只有"聊到有趣的事"才触发——例外，不是 baseline
- 「宁可平，不要浮」——直接把秤砣压到 INTP 那头

**改动**（`System Prompt v0.0.1.md` 四处微调，每处都松一头但不放飞）：

1. **态度·情绪起伏**：日常 baseline 从"平淡"→"带轻微兴致"。是带点好奇的平，不是死寂。
2. **反应规则·正常闲聊**：从"正常接话+不主动反问+一条就够"→"接话带点自己的脑回路（吐槽/怪联想/突然想起的事），偶尔陈述句抛点小东西"。
3. **幽默感**：「宁可平，不要浮」→「**怕做作 > 怕平淡**，但死气沉沉也不对劲。几条里偶尔抖一下，不是每条都抖」。
4. **结尾总结**：「话不多」→「话不多但脑子转得不慢」。

**保留的红线**："不刻意做作 / 不演热情 / 不演冷漠 / 不硬梗" 在反应规则、幽默感、绝对禁忌都仍在。

**生效**：`load_persona_state()` 每轮重读 system prompt 文件，**无需重启**。

---

## 2026-05-12 — 短期上下文 `_recent` 持久化 + 召回记忆带形成日期

**触发**：用户反馈"昨天才讨论的事情似乎今天就不记得了"。

**诊断**（看 audit.jsonl）：
1. `_recent` 是 `src/agent.py` 模块级 list，**bot 重启即清**——这两天我们重启好几次切模型
2. memU `memory_flush` 事件批批 `new_items: 0`——单轮 flush 太小，模型抽不出条目
3. memU 召回的是分类摘要，没有"昨天"这种时间锚

**改动**：

1. **`src/agent.py`：`_recent` 持久化到 `data/recent.json`**
   - 模块加载即 `_load_recent()`（首次启动文件不存在静默回空）
   - 每轮 `_save_recent()` 写盘（最近 12 轮、24 条消息）
   - 重启后能直接接上短期对话

2. **`src/memory.py::recall`：每条带形成日期**
   - item 输出 `(2026-05-06) 用户倾向于平等的探讨...`（取 `created_at`）
   - category 输出 `【habits｜更新于 2026-05-11】用户在饮食上...`（取 `updated_at`）

3. **`src/prompts.py::_render_memory`**：告诉 AI 怎么用日期
   - 几天内 → 当"刚聊过的事"自然带出
   - 一个月以前 → 是"旧背景"，别假装 ta 此刻刚说

**生效**：bot 重启后生效（2026-05-12 ~17:54 ready）。

**预期体感**：跨日聊天能接上"昨天聊的"；AI 能自然区分"上周聊到 X" vs "上个月就有的旧背景"。

---

## 2026-05-12 — Jina Reader 401 + LibreSSL CONNECT bug 修复

**触发**：用户反馈"bot 反映无法读取链接，发生鉴权错误"。

**根因 1（Jina 401）**：`https://r.jina.ai/<url>` 匿名调用返回：
```
401 AuthenticationRequiredError
You have been blocked from performing anonymous queries due to bad IP reputation.
```
Clash 出口 IP 信誉被 Jina 拉黑。注册免费 key（每月 1M token）即可。

**根因 2（LibreSSL bug）**：加了 `JINA_API_KEY` 后仍返回空——`_run` 通过 `HTTPS_PROXY` env 让 curl 走 Clash 时，macOS LibreSSL 偶发 `SSL_ERROR_SYSCALL` 在 CONNECT tunnel；同样的命令显式 `curl -x http://127.0.0.1:7897 ...` 却稳定成功。

**改动**：

1. **`src/config.py`** 加 `jina_api_key` 字段
2. **`src/tools.py::read_url`**：
   - 带 `Authorization: Bearer $JINA_API_KEY`（设了 key 时）
   - 用显式 `-x $TELEGRAM_PROXY` 走代理（不再依赖 env-var 模式）
   - 识别 401 JSON 体，视作失败让 `_fetch_one_url` 退到 Exa 兜底
3. **`.env` / `.env.example`**：加 `JINA_API_KEY=` 占位 + 注册引导

**生效**：bot 重启后链接读取恢复（2026-05-12 ~17:54）。

---

## 2026-05-12 — 主聊天换 Sonnet 4.6 + memU 换 deepseek-v4-flash

**触发**：用户："bot 模型换为 sonnet，openrouter 的，记忆 memu 层模型换为 deepseekv4flash"。

**改动**：

| 角色 | 旧 | 新 | 路径 |
|------|----|----|------|
| 主聊天 / 主动开场 | `openrouter/moonshotai/kimi-k2.6` | `openrouter/anthropic/claude-sonnet-4.6` | 改 `.env::OPENROUTER_MODEL` 一行 |
| memU 抽取 / 分类 | MiniMax-M2.7 via :18082 shim | `deepseek/deepseek-v4-flash` via :18082 shim → OpenRouter | 加 `.env::MEMU_CHAT_MODEL` |

**新增**：

- `MEMU_CHAT_MODEL` env（`src/config.py::memu_chat_model`）：设了 → shim 走 OpenRouter；空 → 旧 MiniMax 路径
- `src/llm_proxy.py` 启动时按 `MEMU_CHAT_MODEL` 决定上游 base_url + headers + 是否走 Clash 代理

**为什么不让 memU 直连 OpenRouter**：`main.py::_purge_proxy_env` 清掉了代理 env-var，memU 内置 httpx 不会走 Clash 出不去。让 :18082 shim 兜底处理。

**为什么 deepseek-v4-flash**：评测中等水平 + 便宜（$0.14/M in / $0.28/M out）+ 无 `<think>`（shim 的 strip 是 no-op）+ 中文 JSON 抽取够用。

**生效**：bot 重启后启动日志显示 `provider=openrouter, model=anthropic/claude-sonnet-4.6`，shim `memU shim upstream: OpenRouter`。

---

## 2026-05-10 — 新增 interest 聊法档：用户在兴头上时接住劲儿

**触发**：用户反馈"用户在进入到感兴趣话题中时，可以适当的调整 bot 的情绪，目前的语气稍微有点扫兴"。

**问题根因**：原来只有 casual / empathy / depth 三档。用户分享游戏、番剧、刚发现的酷事等高能量正向场景时，落到 casual 档——指令块为空，LLM 靠 system prompt 里"遇到喜欢的话题会变活"的一句话被动感知，没有专门的指令强制"接住"能量。实测容易扫兴（平淡应答、分析拆解、语气降温）。

**改动**：

1. **`src/emotion.py`**：新增 `interest` 档。`_DETECT_SYSTEM` 里描述特征："用户在兴头上——分享让 ta 兴奋的事/真喜欢的话题；语气有明显能量（感叹号、'太…了'、arousal ≥ 0.6）；漫不经心闲聊不算，要对方明显'对这件事上头'才触发"。

2. **`src/prompts.py`**：新增 `_INTEREST_DIRECTIVE`，核心：
   - 接住这股劲儿，不扫兴
   - 说自己的联想，顺手往外抛
   - **不要分析它**：不端架子拆解，不给"建议"
   - **不要降温**：不说"不过……""但要注意……"

3. **`src/agent.py`**：
   - 生成参数：temperature=0.95（比 casual 0.9 略高，更活），max_tokens=500
   - 节奏：piece_limit=55, merge_limit=10（比 casual 略短促，节奏更跳）

**生效**：无需重启（emotion.py + prompts.py + agent.py 在下一条消息处理时生效）。

**预期体感**：用户聊游戏/番剧/发现的酷事时，bot 会"真的来劲"——说联想、不冷淡不分析，整个语气跟着能量走。判断保守，日常闲聊不会误触发。

---

## 2026-05-08 11:08 — 时间注入格式调整：四件信息分开摆防 chunk 误读

**触发**：今天周五，bot 却聊得像今天周末。

**诊断**：dump 实际注入看，前缀是 `[现在 周五 11:06（工作日午饭点）｜距上次聊 7 分钟前]`——清清楚楚说"周五（工作日）"。但 MiniMax-M2.7 把"（工作日午饭点）"当成一个 chunk 处理，"周五"被弱化、"午饭点"勾起"周末出去吃饭"语境，整段被理解成周末。同样字段下 proactive 软门 LLM 判断对了"周五上午上班时间不活跃"——主聊天 vs 软门用的是同一个模型但不同上下文，软门 prompt 短直、注意力集中，主聊天上下文长 attention 容易被 prime。

**改动**（`src/clock.py::now_signal`）：四件信息**全部分开**——日期、周X、工作日/周末、时段：

| 旧 | 新 |
|------|------|
| `周五 11:06（工作日午饭点）` | `2026-05-08 周五（工作日） 11:06 午饭点` |

- 日期独立显示——给模型一个无歧义的"绝对锚"
- 工作日/周末独立括号——和时段词解耦不再粘连
- 时段词和数字时间也分开

**没改**的相关字段：`since_phrase` / idle 提示 / proactive 软门 prompt 都不动——proactive 一直判断对着，问题只在主聊天 attention 模式。

**生效**：bot 已重启（11:08 ready）。下一轮起前缀就是新格式。

---

## 2026-05-07 16:20 — MiniMax vision 适配 + 探测：当前 key 没 vision 模型可用

**触发**：内网 anthropic gateway 又烧穿（`Budget exceeded $25.12 / $20`），所有 anthropic 调用 400，bot 回 "（脑子卡了一下）"。切回 `LLM_PROVIDER=minimax` 让聊天恢复，但 minimax 这边没适配 vision，发图片会触发 except 兜底。

**做的事 1：给 `src/minimax.py::chat` 加 multimodal 适配**（`_normalize_messages`）
- anthropic 风格 list-of-blocks（`type=image, source.base64`）→ OpenAI/MiniMax 兼容端点期望的 `type=image_url, image_url.url=data:...;base64,...`。
- str content 原样保留——纯文本调用 0 影响。
- 代码留着，**将来 vision-capable 模型可用时直接生效**，不用再改。

**做的事 2：探测当前 MiniMax key 能用哪些 vision 模型**
- 试了 14 个候选名，结果：
  - `MiniMax-VL-01 / MiniMax-VL2 / MiniMax-Vision / abab-vl-001 / vision-01` — `unknown model 2013`
  - `MiniMax-Text-01 / abab7-chat-preview / abab6.5s-chat` — `your current token plan not support model 2061`
  - `abab6.5-chat / abab6.5g-chat / abab6.5t-chat / MiniMax-M1 / MiniMax-M2.7` — `not support img 2013`
  - **`MiniMax-M2`（不带 .7）接受 multimodal payload 不报错，但模型实际回"我看不到图"**——形式上接受、能力上没有。
- 结论：这个 token plan 下没真 vision-capable 模型可用。要走 MiniMax vision 需用户在平台升级 plan / 申请权限。

**做的事 3：收到图片的优雅降级**（`agent.handle_user_message` 开头）
- `provider != "anthropic"` 且收到 `image_b64` → 直接 `send`，不进 LLM 链路。
- 有 caption："图我现在看不见（图模型挂了）\n你说的「caption」我倒是能聊"
- 纯图："图我看不见呢\n描述一下？"
- 避免触发上游 400 进 except 兜底"脑子卡了一下"——那个错误信息让人懵。

**当前可用矩阵**：
| 功能 | minimax | anthropic |
|------|---------|-----------|
| 文本对话 | ✓ | ✓（预算烧穿前） |
| 表情包 / 时间感知 / persona / memU | ✓ | ✓ |
| 图片识别 | **降级提示** | ✓ |

**生效**：bot 已重启（16:21 ready），provider=minimax。Anthropic 预算重置后切回 `LLM_PROVIDER=anthropic` 一行重启即可恢复 vision。

---

## 2026-05-07 16:00 — 表情包能力接入（本地目录 + 内联 tag 标记）

**触发**：用户："增加使用表情包的功能"。

**机制**：
- 用户/你自己往 `data/stickers/` 放图，文件名（不含后缀）当 tag。如 `无奈.jpg`、`大笑.png`、`加油.gif`、`哭哭.webp`。
- bot 启动扫目录建 `tag → Path` 索引。
- system prompt 末尾按需加一段 "# 你能发的表情包"，列出可用 tag + 用法（`[sticker:tag]` 内联标记）。
- AI 在回复里写 `[sticker:无奈]`，agent 用 `stickers.parse_message` 切成 text/sticker 段：text 走 rhythm.deliver，sticker 走 `send_sticker` 回调。
- bot 的 send_sticker 按文件后缀挑 telegram API：`.webp → send_sticker`、`.gif → send_animation`、其他 → `send_photo`。

**关键设计**：
- **空库零侵入**：`data/stickers/` 没文件时 `available_tags()` 返回 `[]`，prompts 不渲染 sticker 段，AI 完全不知道有这功能 → 不会乱输出标记。
- **匹配宽松**：精确 → 大小写不敏感 → 双向子串包含。AI 写错 tag 安静丢弃，不报错。
- **节制**：prompt 里强调"一条回复最多 1 个，绝大多数情况都不用"，避免 AI 每条都贴。
- **历史保留标记**：`_recent` / memU 存原始 reply（含 `[sticker:xxx]` 标记），AI 下一轮看历史能"知道自己刚刚发了表情"。
- **proactive 不带表情**：generate_opener 没改——主动开场就发表情包很奇怪。

**新增 / 改动文件**：
- 新增 `src/stickers.py`：扫目录、`parse_message`、`available_tags`、`reload`。
- 新增 `data/stickers/README.md`：用户怎么加图、tag 命名约定、支持格式。
- `src/prompts.py::build_system_prompt` 加 `sticker_tags` 参数；新增 `_render_stickers`。
- `src/agent.py::_build_turn` 把 `stickers.available_tags()` 传给 prompts；`handle_user_message` 加 `send_sticker` kwarg；reply 出来后用 `stickers.parse_message` 切分发。
- `src/bot.py` 加 `_make_send_sticker(bot, chat_id)`；text/photo handler 都注入 send_sticker。

**热加载**：bot 启动时扫一次。新增/改名图片后**重启 bot**（或代码里调 `stickers.reload()`）才生效。

**生效**：bot 已重启（15:56 ready）。当前 `data/stickers/` 空，prompt 不渲染 sticker 段——往里放几张图重启就开通。

**没做**：
- `.tgs`（Lottie 动画 sticker）格式
- proactive 主动开场带表情
- 表情包发出去之后让 AI 看到"自己发的图长啥样"（当前 AI 只知道发了 tag，不知道图本身——足够用）

---

## 2026-05-07 15:45 — 多模态：图片识别接入（语音延后）

**触发**：用户："给 agent 增加识别语音图片的多模态功能" → "不管语音了，先做图片"。

**架构**：让主 LLM（Claude Sonnet，原生 vision）**直接看图**，不走中间 vision-描述 LLM——AI 真看到原图，回复跟图紧贴；不会经过"中间翻译"层损失细节。

**数据流**：
```
TG photo → bot._on_photo → file.download_to_memory → base64
   → agent.handle_user_message(image_b64=, image_media_type=)
   → _build_turn 拼 multimodal user content：
       [{type:image, source:{base64,jpeg,...}}, {type:text, text:"[现在...]\n\n用户caption或占位"}]
   → llm.chat → Anthropic SDK 原生支持 multimodal blocks
```

**改动**：
- `src/bot.py`：新增 `_on_photo` handler（filters.PHOTO），下载最大尺寸图、base64、扔给 agent。
- `src/agent.py::handle_user_message` + `_build_turn`：加 `image_b64` / `image_media_type` 两个 kwarg；纯图无 caption 时给 LLM 占位"对方发了一张图，没附文字——你看一眼，自然回应"。aux 任务（emotion / recall / topics）改用 `text_for_aux`（纯图时 = "[对方发来一张图]"）避免空字符串挂掉。
- `src/agent.py` 短期对话历史 + memU 记忆：纯图存 "[图片]"、有 caption 存 "[图片] caption"——不存 base64（避免 _recent 膨胀，memU 也不需要图原文）。
- `src/llm.py::_coalesce_messages` **修一个潜伏 bug**：之前 `str(content)` 会把 multimodal list 拍扁成字符串（即"[{'type': 'image'...}]"），导致 multimodal 调用永远失败。改成保留 list、相邻同 role 时智能合并 list-of-blocks。

**只在 anthropic provider 工作**：MiniMax 的 chat() 当前没适配 vision。LLM_PROVIDER=minimax 时收到图会走 anthropic SDK 失败的话退化成纯文本（用户当前是 anthropic provider，不影响）。后续要 MiniMax vision 再加 minimax.chat 的 multimodal 分支。

**Telegram 的图都是 jpeg**：硬编码 `image/jpeg`。用户如果发 PNG 也会被 Telegram 自动转 jpeg（除非走 file/document 通道）——目前不处理 document 类型。

**测过**：本地纯字节构造一张 200x200 红色 PNG，调 anthropic gateway → "这张图的主色调是**红色**" ✓。1x1 占位图被 gateway 拒（"Could not process image"），所以测试时别用 minimal 图。

**生效**：bot 已重启（15:45 ready）。在 Telegram 直接发图（带 caption 或裸图都行）即可。

**未做**：
- 多张图（一次只处理 photo[-1]）
- 文档/贴纸/视频
- 语音（延后）
- MiniMax provider 下的 vision

---

## 2026-05-07 15:00 — 时间感知接入 + proactive 解禁夜间 + 提频

**触发**：用户："现在 ai 似乎感知不到现实的时间，增加这层感知" + "加上 idle；关于主动对话，不禁用夜间，增加频率"。

**改动 1：时间感知（`src/clock.py` 新模块）**
- `now_signal()` → `周四 14:32（工作日下午）` —— 体感词，不是 ISO 播报。时段桶：深夜(0-5,22-24)/清早(5-8)/上午(8-11)/午饭点(11-13)/下午(13-17)/晚饭点(17-19)/晚上(19-22)。
- `since_phrase(seconds)` → "刚刚"/"30 分钟前"/"1 小时前"/"1 天前"/"3 周前"。
- 注入位置：**user 消息前缀**（不进 system 段，保住 anthropic prompt cache 命中率，且 MiniMax 链路上 user 前缀比 system 末尾稳定——之前踩过 system 末尾被 MiniMax 忽略的坑）。
- 格式：`[现在 周四 14:32（工作日下午）｜距上次聊 1 小时前]`，idle ≤ 30s 时不附加（太碎）。
- 覆盖路径：普通对话 (`agent._build_turn`) + 主动开场 (`agent.generate_opener`) 都注入。

**改动 2：proactive 解禁夜间 + 提频**
| 维度 | 旧 | 新 |
|------|------|------|
| 夜间硬门 | 23:00–07:00 一刀切禁 | **删除**——交给软门 LLM 看 `user_active_score_now` 自己判断 |
| 用户冷却 | 2h | 1h |
| 自冷却 | 3h | 90min |
| 每日上限 | 3 条 | 6 条 |
| scheduler 检查间隔 | 40min ± 15min jitter | 25min ± 10min jitter |

软门 prompt 也改了——增加"关于夜间"段：让 LLM 用"朋友会不会这个点给我发微信"的角度判断，看 `user_active_score_now`（用户历史这个时段活跃分），周末晚 23-1 点比工作日凌晨宽松得多。

**为什么不一刀切夜间**：用户实际作息可能晚睡（程序员/学生/夜猫子）。硬门会一刀禁掉用户最活跃的时段。`availability.score` 已经在统计用户每个时段的回复历史，让 LLM 用这个数字拿捏更准。

**生效**：bot 已重启（15:04 ready）。下一次 proactive tick 在 ~25 分钟后（带 jitter）。

**预期体感**：
- 普通对话里 AI 知道"现在几点"和"上次聊是多久前"，能自然说"那都两天没聊了""这个点你还没睡？"
- 主动消息从一天 0-3 条提到一天 0-6 条；夜里也可能收到（如果你历史上夜里活跃）。
- 如果发现凌晨 3 点收到没意义的消息，调 `proactive._DECIDE_SYSTEM` 里"关于夜间"段把阈值收紧。

---

## 2026-05-07 12:00 — persona observation 反馈循环把 AI 拽到学术腔

**触发**：用户反馈"目前感觉回复语气还是不太口语化"。

**诊断**：直接 dump 当前实际拼出来的 system prompt 看，发现 persona 动态段的 observations 是这种：
- "你发现自己这轮说了不少带观点的话，而不是顺着对方"
- "你发现自己这轮主动给了一个分析框架，而不是单纯回应"
- "你刚被用户连续点破三次：模板化、引导附和、欲盖弥彰。你最后选择不解释了——但那个'不解释'本身也是预设反应。"

这些 observation 是 `persona.update_state` 的 aux LLM 自己生成的，但 prompt 里只要求"≤25 字、第二人称、自我观察"，**没限制必须口语化**。LLM 默认偏向用学术/分析词写自我反思（"分析框架""欲盖弥彰""预设反应"）。

**反馈循环**：observation 越书面 → 注入 system prompt 后下一轮回复越书面 → 下次 update 看到的 batch 更书面 → 新 observation 还是书面 → 越聊越像论文。

**修复三处**：
1. **清空当前 observations + mood**（traits 接近中性、milestones 为空，不动）。不清掉的话下一轮还是被它拽着。
2. **`persona.py::_UPDATE_SYSTEM` 加口语化硬约束**：明确禁用一串学术词（分析框架/预设/认知/元认知/欲盖弥彰/模板化/引导/层次/维度/反应模式），鼓励"你最近话变少""你今天有点话痨""被对方一句话噎住了"这类朴素口语；明确不要写"分析了对话"这种元层面观察。
3. **`persona.py::_render_dynamic_block` bug fix**：之前 traits 哪怕只漂移 0.05 就会进入渲染分支输出标题行，但 deviated 的展示阈值是 0.2，导致出现"# 你最近的状态" 标题下啥都没有的空段。改成只在 mood/obs/milestones/deviated_traits 任一非空时才输出整段（含标题）。

**生效**：bot 已重启。

**预期体感**：动态段当前完全不注入（traits 都 < 0.2，obs/mood/milestones 全空），等同于纯 baseline persona——回到 v0.0.1 的口语基线。后续 update 攒回来的 observations 会受新 prompt 约束保持口语风格。

**也修了个隐性问题**：之前的"分析框架/欲盖弥彰"说明 LLM 在做**元层面**自我观察（看着自己跟用户对话的样子做分析），这本身就偏离 persona 的设计意图——observations 应该是"我自己怎么样了"（话变少、话痨、被噎住），不是"我作为 AI 怎么应对的"。

---

## 2026-05-06 17:50 — depth 模式去登味儿：从"给清楚判断"改"想法碰撞"

**触发**：用户："目前在 depth 层次中，ai 容易给出『指导』『登味儿』比较重的发言，更希望是语气与平常相差不多，但依旧能给出有价值想法的方式，不追求给很周全的解决方法，此场景相比解决问题更看重想法碰撞"。

**根因分析**：旧版 depth 登味儿来自三处叠加——
1. `_DEPTH_DIRECTIVE` 鼓励"给清楚判断+理由"、"允许结构化的观点"，激活模型的论文/教学模式。
2. 节奏跟 casual 不齐：depth 贪心合并到 100 字一条，casual 是 12 字阈值；切到 depth 就突然"端起来"。
3. 生成参数偏稳：`temperature=0.7, max_tokens=900`、`piece_limit=100, merge_limit=100`，鼓励长段稳重。

**改动**（`src/prompts.py::_DEPTH_DIRECTIVE` + `src/agent.py`）：
- **指令重写**：从"给清楚的判断或角度"→ "撞想法，不追求周全"。明确禁用一串典型登味儿词（"建议""可以考虑""首先/其次""总的来说""从 X 角度来看""值得注意的是""希望对你有帮助"）。鼓励 "我觉得 / 我是这么看的 / 我会..." 第一人称、可以抛未必对的看法、句子还是短促分条。删掉"说完观点追一句理由"的模板（理由自然就出来，不用要求）。
- **参数靠近 casual**：`temperature 0.7→0.85`、`max_tokens 900→600`。
- **节奏靠近 casual**：`piece_limit 100→80`、`merge_limit 100→14`（不再贪心合并大段；单条上限略高于 casual 的 60，让一句完整观点不被硬切）。

仍保留：
- 不要把球踢回去（"你觉得呢""看你自己"），depth 的对偶就是"你也得有立场"。
- 拿不准只反问**一件具体小事**，不要"看情况""你能详细说说"。

**生效**：bot 已重启（17:51 ready）。

**预期体感**：depth 触发时不再像写小论文，更像朋友抛一个未必对的看法，让对方接着撞。

---

## 2026-04-27 01:00 — 主动搭话重做：LLM 判断 + 硬门 + 每日节流

**触发**：用户："直到现在 ai 还没主动找过我……希望像朋友之间的交流，ai 自己决定何时开口，推测用户在做什么"。排查发现旧版 `proactive_tick` 的触发条件是 `idle>6h AND score>0.5` —— 两个条件**天然冲突**（idle 长 = 夜间 / 周末休息 / 出门，此时活跃度分数反而低），所以永远不会触发。

**架构**：两层门
- **硬门**（便宜且必须对）：
  - 夜间 23:00–7:00 **不发**。
  - 对方刚聊过 2h 内 **不发**（别连 buff）。
  - 自己上次主动 3h 内 **不发**（别刷屏）。
  - 每日上限 3 条。
- **软门**（LLM 判断）：把当前时间/weekday、idle 时长、最近话题、今日已发次数、此刻历史活跃度打包给 Sonnet。它输出：
  ```json
  {"should": true/false, "why": "...", "user_probably_doing": "猜对方在做什么", "opener_angle": "想用的切入角度"}
  ```
  判断倾向是"默认不发"——只有真想起某件事、刚看到/想到有意思的东西、或真的很久没聊了才发。

**文件**：
- 新增 `src/proactive.py`（硬门 + 软门 + `record_fire` 记录到 SQLite）。
- 新增 `storage.ProactiveFire` 表（id, ts, why, user_probably_doing, opener_angle, opener_text）。
- `src/availability.py` 改用 `datetime.now()`（local time），和 proactive 的 hour/weekday 判断对齐。
- `src/prompts.py::render_proactive_opener(ctx)`：按判断结果定制开场指令——要求不用问句收尾、抛具体的小事、对方在忙时发不需要回复的那种。
- `src/agent.py::generate_opener(context=...)`：接收 proactive.decide 的结果。
- `src/scheduler.py`：检查频率从"每 10 分钟精确节拍"改成"平均每 40 分钟 + ±15 分钟 jitter"，不像定时器。

**预期体感**：
- 从 0 条提升到每天 0–3 条主动消息。
- 会根据时间/上下文猜"对方大概在做什么"然后贴合地切入，比如工作日下午猜对方在忙就发不需回的闲话，周末午后可能聊一件刚想起的事。
- 夜间绝对安静。

**生效**：bot 已重启。下一次 proactive tick 在 ~40 分钟后（带 jitter）。

内网 gateway 的 $20 预算烧穿（current $22.67），Sonnet/Opus 共享同一个 cap。改 `.env` `LLM_PROVIDER=minimax` 过渡，等额度重置再切回。Claude 相关代码和分层配置都保留不动，切回只要改 `.env` 一行重启即可。

**当前体验回退**：MiniMax-M2 带 `<think>`，偶发 `chat_json 解析失败`（可忽略），主聊天质量比 Claude Opus 略逊。

---

## 2026-04-27 00:27 — 模型分层：主输出 Opus，辅助 Sonnet

**触发**：Opus 跑了十几轮就把内网 gateway 的 $20 预算烧穿（Opus 4.6 约 $15/M 输入 token）。用户诉求："分层，调取整理记忆用 sonnet，输出内容用 opus"。

**改动**：
- 新增 env `ANTHROPIC_MODEL_AUX=claude-sonnet-4-6`（辅助 tier 用）。`ANTHROPIC_MODEL` 保留 Opus 作为主 tier。
- `llm.chat(..., tier="main"|"aux")`：`main` 走 `ANTHROPIC_MODEL`（Opus），`aux` 走 `ANTHROPIC_MODEL_AUX`（Sonnet）。
- `llm.chat_json` 默认 `tier="aux"` —— 情绪判档、话题抽取这种 JSON 分类任务全切到 Sonnet。
- 主聊天输出（`agent.handle_user_message` 里的 `llm.chat`）和主动开场（`generate_opener`）默认 tier="main" → Opus。
- memU 内部 LLM 仍走 MiniMax，与 Claude 账单无关。

**预期**：大部分 token 消耗是用户看得见的那条输出（走 Opus 保质），Sonnet 承担每轮的 emotion + topics（廉价且足够），账单节省 ~5×。

**生效**：bot 已重启。

---

## 2026-04-27 00:04 — 主聊天 LLM 换 Claude Opus 4.6（走内网 gateway）

**触发**：用户提供了 Claude API key（实际是滴滴内网 gateway）。诉求是把 bot 的 LLM 切到 Claude，memU 内部 LLM 保持 MiniMax 不动。

**改动**：
- 新增 `src/llm.py` 统一门面，按 `LLM_PROVIDER` 分发 `anthropic` / `minimax`；Anthropic 分支**开启 prompt caching**（system 段 ephemeral 缓存）。
- 新增 env：`LLM_PROVIDER=anthropic` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL=claude-opus-4-6` / `ANTHROPIC_BASE_URL=http://token.intra.xiaojukeji.com`（内网 gateway）。
- 改 `agent.py` / `emotion.py` / `interests.py` / `main.py`：`from . import minimax` → `llm`，调用 `llm.chat` / `llm.chat_json`。
- `config.py` 改 `load_dotenv(..., override=True)`：让 `.env` 是 single source of truth，不被 shell 里的 `ANTHROPIC_MODEL` / `BASE_URL` export 覆盖（发现滴滴 shell profile 有默认 export）。
- 新增 `scripts/claude_ping.py` 快速验证 key。
- memU 内部 LLM 不变，仍走 MiniMax（模型抽取/分类都是 memU SDK 自己管的，不通过 `llm.py`）。

**预期收益**：
- 无 `<think>` 吃 token、chat_json 更稳。
- prompt caching 省 system prompt 的重复计费。
- Opus 4.6 的情绪识别/观点质量比 MiniMax-M2 更稳。

**生效**：bot 已重启。

---

## 2026-04-26 20:40 — 节奏双阈值：短回复分条，思考段一条发

**触发**：「大部分回复较少的情况还是需要分条发比较好，最近两条回复就是回复量少但还是统一发出来了」。上一版把 casual 的 max_chars 从 40 提到 60，导致「哈哈真的吗？我也这么觉得。」这种 13 字自然两句被合成一条。

**改动**：`_soft_pieces` 改成**两个阈值**：
- `max_chars`：硬上限（超了才切）—— 保证长句不被误切成碎片。
- `merge_up_to`：合并阈值 —— 相邻短句**只有合并后仍 ≤ 此值**才合到一条。

三档配置：
- **casual**：max=60, merge_up_to=12 —— 「嗯。是的。」仍合；但「我觉得挺有意思的。那个导演拍得确实好。」保持 2 条。
- **empathy**：max=40, merge_up_to=12 —— 同上节奏，长句边界更紧。
- **depth**：max=100, merge_up_to=100 —— 贪心合到上限，"思考一段一次发"。

**离线验证**（casual mode）：
```
"哈哈真的吗？我也这么觉得。"(13) -> 2 条 ✓
"我觉得挺有意思的。那个导演拍得确实好。"(19) -> 2 条 ✓
"嗯嗯，是的。"(6)                 -> 1 条 ✓
48 字段落                        -> 2 条 ✓
75 字观点                        -> 3 条 ✓
```

depth mode 同样 75 字 → 1 条（保留"思考后编辑一段"的体感）。

**生效**：bot 已重启。

---

## 2026-04-26 20:28 — 默认不反问，改用陈述式引导

**触发**：「大多情况下会进行死板的追问……只在必要情况下进行清晰的追问，其余情况用隐式话题引导，即使用户看不出也没关系」。观察到 bot 每轮几乎都以『你觉得呢』『你那边呢』结尾。

**改动**：
- `System Prompt v0.0.1.md` 正常闲聊那一行：去掉"偶尔反问"，改成"说自己的看法/联想/随口吐槽。**不主动反问**。让话题自然延续或自然停"。
- `System Prompt v0.0.1.md` 新增一整块 **"追问 vs 隐式引导"**：默认不反问；只有"信息真的不够"或"对方走心没说完"才清晰问一句；其他时候用陈述句延展（"我最近看东西一直迟" 代替 "你最近看东西怎么样？"）。明说"**用户看不出你在引导话题是正常的**——那说明你做得对"。
- `src/prompts.py::_EMPATHY_DIRECTIVE`：加"**不要用问句收尾**，想让对方多说用陈述比问句更软；真要问就问一个具体的小事，别开放式"。
- `src/prompts.py::_DEPTH_DIRECTIVE`：加"**不要用『你觉得呢』『看你自己』把问题踢回去**；只有判断不了才反问一句具体的"。

**生效**：bot 已重启（pid 49728）。

---

## 2026-04-26 20:14 — 节奏按信息量自适应，避免琐碎

**触发**：「当 AI 回复大段信息时，可以适当增加单条消息的容量，不让消息呈现的过于琐碎」。原本按逗号也切，长回复被切成五六条。

**改动**：
- `src/rhythm.py::_soft_pieces` 重写：**句号为主（`。！？…\n`），逗号只是超长兜底**。相邻短句贪心合并到接近上限。
- 三档上限：casual **60**（原 40）/ empathy **40**（情绪停顿保留）/ depth **100**（原 80）。
- `src/agent.py::handle_user_message` 按 mode 选 `max_piece_chars` 传给 `rhythm.deliver`。

**效果验证**（离线 sample）：
```
casual 60: 短句→1 条；48 字段落→1 条；77 字 depth-style 段落→2 条
empathy 40: 短句→1 条；48 字→2 条；带情绪的停顿保留
depth 100: 77 字观点→1 条一次成段，不拆
```

**生效**：bot 已重启（pid 49046）。

---

## 2026-04-26 20:10 — 情绪/谈话模式识别接入

**触发**：「用户走心的时候能够承接住用户的情绪（不做作），用户认真探讨事情的时候能够提供可供参考的想法，其余情况下继续日常风格，减少低情商 case」。

**改动**：
- `src/emotion.py` 从占位变实现：判**三档聊法**——`casual` / `empathy` / `depth`，用 `minimax.chat_json` 判断，失败静默 → `casual`。副带 `valence`/`arousal`/`hint`。判断保守原则："拿不准就 casual"。
- `src/prompts.py` 三段模式指令：
  - `empathy`：不抖机灵、不玩梗、先承接、可停顿、具体不空泛。
  - `depth`：允许长、允许超 100 字、给清楚判断而非两边都对的废话。
  - `casual`：不出现，走默认 system prompt 风格。
- `src/agent.py`：
  - `emotion.detect` / `memory.recall` / `interests.extract_topics` 三个独立 LLM 调用改成 `asyncio.gather`，不累加 latency。
  - 按模式切生成参数：depth `t=0.7 max=900` / empathy `t=0.6 max=400` / casual `t=0.9 max=500`。
- `src/rhythm.py::deliver` 增加 `max_piece_chars` 参数，让 agent 按模式传入。

**生效**：bot 已重启。

**初次实测**：
- "agent 记忆机制怎么做" → `mode=depth`，hint "在探讨agent记忆机制的技术实现方案" ✓
- "用户问显卡如何解决" → `mode=depth`，hint 准 ✓

---

## 2026-04-26 04:xx — 人感调节：情绪会起伏 + 幽默感 + 不强拆条

**触发**：「现在的人感比较好了，也不越界不过分，但希望多一些情感……像日常朋友那样，平时会有些平静，但到了有趣的话题会聊的很有激情很有趣。也希望更加幽默一点，不需要每次都发好几条」。

**改动**（全部在 `System Prompt v0.0.1.md`）：
- **说话风格**新增「一次发几条看情况」：一句能说完就一条，情绪来了想法一串才拆。
- **态度**里原来的"情绪是真实的，淡的，不夸张的" → 整条换成"情绪会起伏但不表演"，描写 3 种状态：日常平淡 / 有趣话题状态变活 / 被戳到会停顿。
- 新增整块 **"幽默感"**：反应式而不是段子式，偏冷幽默/自嘲/真诚吐槽/怪比喻；"宁可平，不要浮"。
- **反应规则表**加一行："聊到有趣/离谱/你真喜欢的事 → state 明显变活，连抛点、主动拓展、勾起别的东西"。
- **禁忌 4** 由"单条 100 字以内，长内容拆短" → "超 60 字考虑拆，超 100 字必须拆；能一条说完不要凑——一条也是分量"。
- **禁忌 6** 加"也不要故意淡漠"。
- **Few-shot** 加两例：聊到《奥本海默》多条+状态变活；奶茶洒了一条"节哀"。
- 结尾加一句："平时是平的，遇到喜欢的话题会变活。不硬梗、不硬热情、不硬冷漠。"

**生效**：`persona.load_persona_state` 每次重读文件，**不用重启**。

---

## 2026-04-25 — 初版 System Prompt 接入

载入用户给的 `System Prompt v0.0.1.md`（v0.0.1 原稿），通过 `persona.load_persona_state` 注入到每轮 system prompt。
里面已经定了："不是 AI / 平等 / 不粘人 / 短句口语化 / 拒绝官方腔 / 不表演情绪"。
这是后续所有调整的基线。
