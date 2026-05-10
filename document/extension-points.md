# 扩展点（为下一期预留）

用户明确下一步要做：**情绪识别** + **人格演化**。
架构上已经留好钩子，实现时理论上只改列出的文件。

## 情绪识别 ✅（2026-04-26 接入，2026-05-10 加 interest 档）

现在的 `EmotionSignal` 不是"情感分类"，而是**"下一步该怎么聊"的四档判断**（2026-05-10 加 `interest`）：

```python
@dataclass(frozen=True)
class EmotionSignal:
    mode: Literal["casual", "empathy", "depth", "interest"]
    valence: float  # -1..1，参考
    arousal: float  #  0..1，参考
    hint: str       # 一句话总结对方真正想说什么
```

- **casual**：日常闲聊/调侃/梗/工具问题——默认。按 system prompt 的网友风格。
- **empathy**：走心诉说/脆弱/疲惫/重要人事——承接情绪，不抖机灵不跳话题。
- **depth**：认真探讨/请教/要观点——撞想法、不追求周全、第一人称有立场。
- **interest**：用户在兴头上，分享让 ta 兴奋的事/真喜欢的话题——接住劲儿、说联想、不扫兴、不分析降温。

### 影响的下游（都已接通）

1. `prompts._render_emotion` 四档各一套指令块，插在 system prompt 末尾；casual 时空。
2. `agent.handle_user_message` 按模式切生成参数：
   - depth：temperature=0.85, max_tokens=600
   - empathy：temperature=0.6, max_tokens=400
   - interest：temperature=0.95, max_tokens=500
   - casual：temperature=0.9, max_tokens=500
3. `rhythm.deliver` 按模式切节奏参数（max_piece_chars / merge_up_to）：
   - depth：piece=80, merge=14（单条略宽，允许完整观点不被切）
   - empathy：piece=40, merge=12（短促，留停顿感）
   - interest：piece=55, merge=10（比 casual 略短促，节奏更跳）
   - casual：piece=60, merge=12
4. `agent._build_turn` 里 `emotion.detect / memory.recall / interests.extract_topics` 改成 `asyncio.gather` 并行，避免额外 LLM 调用把 latency 叠加。

### detect 本身

用 `llm.chat_json` + 精简 system prompt + 近 6 条上下文。判断保守——"拿不准就 casual"——避免例外档滥用把风格搞僵。失败/超时 → `None`，全链路 bypass 回 casual。

---

## 人格演化 ✅（2026-05-06 接入）

5 维度 traits（sarcasm / warmth / verbosity / assertiveness / curiosity，-1..1）
+ mood + 自我观察 + 共同锚点（milestones）。两层时机：

- **增量更新**：每次 memU buffer flush 成功后异步 fire `persona.update_state(batch)`。aux LLM
  看本批对话 + 当前 traits，输出 `trait_deltas / new_observations / new_milestones / mood`，
  apply 到最新 snapshot 后写 `persona_snapshots` 一行。单次 delta ±0.15 上限。
- **每日 consolidate**：`scheduler` 03:07 跑 `persona.consolidate()`：traits *= 0.92 朝中性
  衰减、清掉 3 天前的 observations、milestones 永久保留。

`load_persona_state()` 自动把动态段（中文，trait 数值翻译成"偏强/略偏强/正常/..."而非数字）
拼到 baseline body 末尾。`prompts.build_system_prompt` 签名不变。

完整设计、payload schema、调试命令、演化曲线参考见 **`document/persona-evolution.md`**。

---

## 图片识别（vision）✅（2026-05-07 接入）

- `src/bot.py::_on_photo`：filters.PHOTO handler，下载最大尺寸 → base64 → 交 agent
- `src/agent.py::_build_turn` 接 `image_b64` / `image_media_type`，构造 anthropic 风格 multimodal blocks（`type:image, source.base64` + `type:text`）
- `src/llm.py::_coalesce_messages` 已修：保留 list-of-blocks，不会 `str()` 拍扁
- `src/minimax.py::_normalize_messages` 已写 anthropic-blocks → OpenAI-image_url 转换（**LLM_PROVIDER=anthropic 才生效**——MiniMax 当前 token plan 没真正 vision-capable 模型；详 `document/minimax-integration.md`）
- minimax provider 时降级提示"图我看不见，描述一下？"，不进 LLM 链路

## 表情包能力 ✅（2026-05-07 接入）

- `data/stickers/` 放图，文件名（去后缀）当 tag。例 `无奈.jpg`、`大笑.png`
- `src/stickers.py`：扫目录索引 + `parse_message` 切 `[sticker:tag]` 段
- `src/prompts.py::_render_stickers`：tag 列表注入 system prompt（库空 → 不注入 → AI 不知道有此功能 → 零侵入）
- `src/bot.py::_make_send_sticker`：`.webp→sticker / .gif→animation / 其他→photo`
- `scripts/name_stickers.py`：批量调 vision LLM 给图自动起中文 tag

## 模型评测系统 ✅（2026-05-08 接入）

完全独立脚本，0 影响线上 bot。详 `document/eval-system.md`。

- `src/openrouter.py`：OpenAI 兼容 httpx 客户端
- `scripts/eval_models.py`：phase 1，多模型横向回复
- `scripts/eval_judge.py`：phase 2，LLM judge 多维度打分

---

## memU 持久化

`src/memory.py::_get_service` 已经根据 `MEMU_METADATA_PROVIDER` env 分叉（`inmemory` vs `postgres`）。
切换只需改 `.env`，代码不用动。

---

## Webhook / 云部署

`src/bot.py::build_application` 封装了 Application 构造。切 webhook 只改这里 +
用 `app.updater.start_webhook(...)` 替换 `start_polling()`。
`src/scheduler.py`、`src/agent.py` 都不依赖 polling 模式。

---

## 多用户（较大重构，不在预留范围）

现在所有状态单用户写死：
- memU `user_id = "me"`
- interests/availability 全表单行单人
- Telegram `TELEGRAM_ALLOWED_CHAT_ID` 白名单

**真要做多用户**需要：interests/reply_samples 加 `user_id` 列；memory 按 chat_id 派生 user_id；
agent 流水线改成实例化对象而不是模块级变量。**这条不是"下一期"的目标，按需再拆。**
