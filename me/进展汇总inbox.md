# 进展汇总 inbox

> 每次有新进展按日期追加一节。**只写做了什么**，细节走 `document/dialog-tuning-log.md` 等专门文档。

---

## 2026-05-12

- 项目推送到 GitHub 私有仓库（`YYuRain/YYnoTomotachi`）
- `me/img/` 纳入 git，文档图片链接补 GitHub 兼容格式
- 主聊天模型切换：`kimi-k2.6` → `anthropic/claude-sonnet-4.6`（via OpenRouter）
- memU 抽取/分类模型切换：MiniMax → `deepseek/deepseek-v4-flash`（via OpenRouter）
- 修复链接读取鉴权失败（Jina API key + 显式代理绕开 macOS LibreSSL bug）
- 短期上下文 `_recent` 持久化（写盘 `data/recent.json`，重启不再清）
- memU 召回的每条记忆带上形成日期 `(YYYY-MM-DD)`，让 AI 区分新旧背景
- 日常聊天风格微调：从偏 INTP 平淡 → 略 ENTP，偶尔抖机灵但不刻意做作

## 2026-05-13

- 多用户化改造：所有存储（SQLite 5 表 + memU postgres + recent.json）加 `user_id` 维度
- 邀请码准入机制：admin `/invite <n>` 生成、用户 `/start <code>` 激活
- bot 入口重写：未激活用户 silent drop；新增 `/myid` `/invite` `/users` 命令
- scheduler 多用户 fan-out：4 个 job 都按活跃用户列表 `asyncio.gather` 派发，`Semaphore(5)` 限并发
- 写迁移脚本 `scripts/migrate_to_multiuser.py`：备份 → 加 `user_id` 列 → 复制旧 me 数据归到 admin → wrap recent.json → UPDATE memU postgres
- 部署形态：Docker Compose（bot + postgres + admin），HF 模型烤进镜像
- 本地迁移并冒烟通过：旧 me 数据（144 interests / 314 reply_samples / 552 memU 行 / 24 短期上下文）全部归到 admin chat_id 名下，单用户路径仍跑得通

## 2026-05-14

- 项目部署到腾讯香港云（Docker Compose：bot + admin + postgres + mihomo + cloudflared）
- HK 出口 IP 被 OpenRouter 拒 Anthropic 模型（403），加 mihomo 容器走 Clash 订阅美区出口
- admin webUI HTTPS 化：cloudflared 临时 tunnel `*.trycloudflare.com`，零配置
- webUI 鉴权改造两次最终落地：HMAC token URL 一键登录（bot `/memory` 命令铸链接，admin 容器解 token 设 cookie；密钥靠 `data/.webui_secret` 进程间共享）
- webUI 分用户视图：admin 看全部 + 下拉切换；普通用户只看自己；移动端表格转卡片
- 命令重命名：`/admin` → `/memory`；新增 `/mypw`（后又因切 token 路径删掉）
- 新增测试 bot（TEST_BOT_TOKEN 可选）：`/become` 选虚拟身份、`/clear` 清盘、走完整邀请码流程
- 用户激活后 AI 立刻发"开场白"（welcome opener，新指令 + `agent.generate_welcome`）
- 容器时区设为 Asia/Shanghai（Dockerfile + scheduler）
- 修一系列上云/部署期 bug：`huggingface-cli` → `hf`、`.env` 烤进镜像被 dotenv override（加 `.dockerignore`）、`EMBED_MODEL_NAME` 改绝对路径、`/myid` Markdown 解析 chat_id 下划线挂、cloudflared 必须 root 才能写共享卷、cf.log 取最后一个 URL（不是第一个，cloudflared 重启 append 不截断）、login-by-token 用 200 HTML+JS 跳代替 302（移动端 cookie 更稳）

## 2026-05-18

- 抛弃 memU SDK 改自搭记忆栈：单表 `memories`、抽取 prompt 改 JSON、删 `llm_proxy` 18082 strip-think shim、上层公共 API 不变（recall/note_turn/maybe_flush）
- 写迁移脚本 `scripts/migrate_memu_to_native.py` 把旧 `memory_items` 拷过来（admin 188 条，本地 164 条）
- 落地记忆架构 PRD v2 三层防线（详见 `me/prd_memory.md` + `document/memory-stack.md`）：
  - **5.1 写入冲突检测**：每条新事实 flush 后异步 fire LLM 影响分析；schema 加 `status / confidence / last_verified_at / depends_on UUID[]`
  - **5.2 召回反验证**：recall 命中 to_verify 时同步阻塞 LLM 反向验证 + 30min cooldown
  - **5.3 Auto Dream**：03:13 cron 批量整理；LLM 三态可激进判 stale；用 deps 上游 + top-5 confirmed 邻居作综合上下文
- admin webUI 加「图谱」tab：D3 v7 force-directed graph，节点按 status 着色，边 = depends_on（B → A 依赖关系）
- 写 backfill 脚本 `scripts/backfill_conflict_check.py` 给历史 profile 重放 5.1（admin 122 profile / 35 flips：21 stale + 14 to_verify）
- 加 `DEV_SKIP_PROD_BOT=1` env 开关：本地 main 跳过 prod bot 只跑 test bot，避免和云上 prod 同 token 抢 polling
- test bot `/memory` 加 admin chat 路径：admin 真实 chat 直接发 `/memory` 进 admin 视角，不需要 `/become`
- 部署 `memory-deps` 分支到 HK（暂不合 main，下个 PR）：跑 migrate + backfill；prod + test bot 双在线
- 同步 docs：新建 `document/memory-stack.md`；CLAUDE.md / overview / deployment / extension-points / running 全部去 memU SDK 引用；`memu-setup.md` 加归档头

## 2026-05-19

- search 工具栈整修：诊断 admin tool_call 全部 result_chars=0，根因容器没装 mcporter/xhs（本地 nvm/pipx 全局 binary）。`search_web` 改用 Jina Search REST（`https://s.jina.ai/?q=...`），复用现有 `JINA_API_KEY`，容器零依赖
- `xhs_search` 入口删除（账号本就风控失效），并入 `web_search`；LLM 在 query 里加"小红书"关键词
- `_TOOL_DETECT_SYSTEM` 改 .format 模板 + 运行时拼今天日期（`今天是 2026-05-19（周二）` + `今年是 2026 query 必须用 2026 不要 2025`），修 LLM 写 query 错年份
- `_TOOL_DETECT_SYSTEM` 加规则：用户用"你能查 / 你试试 / 你帮我搜"等试探口吻附带具体话题 → needed=true（之前会判 false 当作"问能力"）
- `_maybe_fetch_context` 不管 needed 与否都 audit 一条 `tool_decision`，加用户原话/skipped_reason，让 admin 直接看为啥没触发
- `_ROLE_DISCIPLINE` 新增"你有联网能力"段，明确告诉 bot 它有 read_url/web_search 工具、查到的内容会以 `[链接内容]` 塞回；禁止主动声称"AI 不联网"——之前 sonnet 默认人设否认能力让用户体验差
- 引入「per-user prompt + Feedback Sub-Agent + Skill 库」子系统（`document/feedback-agent.md`）：sonnet 监听 flush 后 batch，粗筛（aux）+ 精判（main 三态判定）+ 双层硬护栏（prompt 7 条 + 代码 regex 兜底）+ low/high 风险分流（low 自动 active、high pending 等 admin 审核）+ 跨用户 skill 库（cosine top-3 召回复用）
- 新表 `prompt_overrides` / `skills`（SQLite，跟 persona_snapshots 一个库）；helpers 一套加在 `storage.py` 末尾
- `prompts.build_system_prompt(user_id=...)` 末尾追加 active overrides 段，baseline 不动
- admin webUI 新增「调教」tab：pending（approve/reject）+ active（disable）+ skill 库（disable）三段；audit 加 `feedback_screen` / `feedback_decision` 着色 + 详细渲染
- 修 `_persist_items` INSERT 漏 status/confidence 列触发 NotNullViolation（同 migrate 之前的坑）；本地 ORM CREATE 走 default 跑得通，服务器表是 ALTER 加列 PG 不走 ALTER 的 DEFAULT，必须显式补
- admin UI items 卡片显示更多信息：confidence / source / last_verified_at / updated_at；audit 摘要展开 persona_update 的 observations 内容、conflict_check 的 flip 老条目原文、reverify/dream 的 LLM reason 等

### 2026-05-19 续：主动触达通道 + skill 仓库语义 + 各种修

- **bot 错拒主动能力 + 打包票承诺**：`_ROLE_DISCIPLINE` 加段——明确 bot 有 proactive 能力（不要说"我没法主动"），承诺时谨慎表达（"我跟系统提一下，不一定每次准"）
- **主动消息进 _recent**：之前 proactive opener 发完没追加进 `_recent_per_user`，下轮 user 回话 bot 看不到刚说的，回"啊？你问哪个哪个"。新加 `agent.record_proactive_message(uid, text)`，scheduler / bot welcome / test_bot welcome / triggered_reach 三处统一调
- **bot 加 admin /proactive_test 命令**：主进程内手动触发 generate_opener + deliver + record_proactive_message，验证链路
- **memory.recall 加双门精度过滤**：query 太短/纯口头禅 skip + cosine distance ≤0.55 阈值；audit 加 distances 数组让 admin webUI 看每条 hit 的相似度
- **search 工具 + bot prompt 一组修**：tools.search_web 走 mcporter 容器没装 → 全 0 chars，改 Jina Search REST；删 xhs_search 入口；_TOOL_DETECT_SYSTEM 加今天日期防 LLM 写 query 用旧年；不管 needed 与否都 audit `tool_decision`；prompt 显式说"你有联网能力，不要说我搜不到"
- **PRD v2 capability_request 落地**：JUDGE_PROMPT intent 增 `capability_request`；feedback_agent 命中时调 skill_creator meta-skill（特殊 skill 存在 skills 表，body 是 sonnet prompt template）输出 trigger-based 指令；启动时 storage._seed_skill_creator 自动种入
- **主动触达 channel（active trigger 通道）**：
  - prompt_overrides 加 `trigger_kind / cron_schedule / condition_prompt / last_fired_at`
  - skill_creator 升级输出 JSON（含 cron + condition + active_text）；render_skill_creator 改 str.replace 避免 .format 误吃花括号
  - 新表 `pending_reach_messages`（暂存待主动发的消息）
  - 新模块 `src/triggered_reach.py`：`tick()` 每分钟扫 cron + 跑 sonnet 判 condition + 喂最近 12 条对话防重复 + 暂存或直发；`dispatch_overdue()` 5min 兜底
  - scheduler 加 `triggered_reach_job` + `pending_reach_overdue_job`（1min interval）
  - `handle_user_message` 入口 `pop_pending_reach_for_merge` 把暂存内容拼进 user 消息当 `[系统暗示]` 段，bot 自然融入
  - 不走 proactive 冷却（独立 dedupe 走 last_fired_at 90s）
  - audit 新事件 `triggered_reach_check`
- **risk_level 默认 low + skill 仓库语义**：移除 capability_request 强制 high；JUDGE_PROMPT 收紧 high 仅限"改写核心人设 / 关闭核心能力 / 假冒身份"；新建 skill 时 override.source_skill_id=NULL 不 bump usage_count，仅当跨用户 reuse_skill_id 命中才 +1
- **个人化偏好禁止沉淀进 skill 库**：JUDGE_PROMPT 强制 tone_adjust / address_form / scope_change 一律 save_as_skill=false；feedback_agent 代码层兜底 + 服务器现存的 lively_tone skill 标 disabled
- **修 SCREEN_PROMPT 漏识别**：补"capability_request"关键词列表（"以后/下次/每次/如果...就..."）+ 8 条具体例子，让 deepseek-flash 别再判 false
- **bot prompt 两层禁打包票**：`_ROLE_DISCIPLINE` 显式说"沉淀机制是异步的，你不知道结果"，禁止"我记住了/以后都会"，要"我尽量记着，不一定准"
- **修 admin webUI 「通过」按钮 500**：admin 容器 `./data:/app/data:ro` 改读写——admin UI 现在要 UPDATE prompt_overrides
- **修 _persist_items NOT NULL bug**：跟 migrate 脚本同坑，INSERT 没列 status/confidence 时 PG 不走 ALTER DEFAULT，显式补
- **bot 拼上下文段补 [系统暗示]**：active trigger 暂存内容明确指引 bot "**这一轮的回复**要把这条信息**自然地融入**主话题"，避免硬转/罗列

## 2026-05-20

- **横向研究**：写 `me/记忆框架横纵分析.md`（Mem0/Letta/Graphiti/Cognee/LangMem 实拉 README + 经典论文综述 → AIDemo P0/P1/P2 借鉴清单）。**关键反转**：Mem0 v3 (2026-04) 放弃 ADD/UPDATE/DELETE 写入决策，回到 ADD-only + 强 hybrid retrieval（LoCoMo +20 / LongMemEval +27）
- **P0-4 episodes provenance**（Graphiti 借鉴）：新 `episodes` 表存 raw turns；`memories.source_episode_id` 反查；admin UI 加"当时聊了啥"链接
- **P0-1 hybrid retrieval**（Mem0 v3）：cosine + ngram(ILIKE) + entity 三路 RRF 融合；新 `memories.entities TEXT[]` + `pg_trgm` ext + GIN 索引；抽取 prompt 增加 entities 字段；`RECALL_MIN_QUERY_CJK_CHARS` 6→3（短 query 走得通）
- **P0-3 命名一统**（LangMem 借鉴）：文档统一"hot path / background / procedural memory"术语；admin UI 调教 tab 加 LangMem 命名 tooltip
- **P0-2 三因子 ranker**（Generative Agents）：在 RRF 上叠加 `final = α·rel + β·imp + γ·rec`（α=1.0/β=0.3/γ=0.3；τ_profile=180d/τ_event=14d）；audit 加 `score_breakdown` 看每条 hit 的分量
- **proactive 加新硬门**：连续 N 次 assistant 没等到 user 回复就 backoff；不依赖 last_interaction 表，看 `_recent` 真实状态——修 admin uid 8120470097 数据被错清后 idle=inf 反复发同一句 opener 的 bug
- **天气反复打扰修**：active trigger 的 `active_text_for_bot` 写了"如果对方聊到出门/上班/下班...你顺手查天气"这种 passive 指令导致 user 说"上班"就触发——改 override #3 text + 在 `prompt/feedback_skill_creator.md` 加"硬约束"段禁 passive 注入
- **prompts 减"刻意"感**：`_EMPATHY/INTEREST/DEPTH_DIRECTIVE` 拼掉"可以说嗯/那确实/我就是！" 等 positive examples——LLM 复读 example 让 bot 听起来在演网友；保留 "不要 X" 黑名单
- **关于时间段**：`_ROLE_DISCIPLINE` 加段——精确分钟时间戳是上下文不是播报词；用"下午/晚饭点/三点多"替代"14:32"
- **persona baseline 微调**：`System Prompt v0.0.1.md`（后改名 `prompt/system_baseline.md`）user 手动调几句客套话/示例
- **重大事故**：清空"admin 数据"时把 admin uid 误判成 8120470097（其实是普通用户，admin = 8058993786），13 条 memory + 24 turns + 313 audit 行不可恢复——加两条 feedback memory（`feedback_admin_uid_caution.md` / `feedback_wipe_must_backup.md`）

## 2026-05-21

- **prompt 抽离到 `/prompt`**：22 个 LLM prompt 从 `src/*.py` 三引号串抽到 `prompt/*.md`（扁平 `<module>_<name>.md` 命名）；`System Prompt v0.0.1.md` → `prompt/system_baseline.md`；新加 `src/prompt_loader.py`（lru_cache）；改 prompt 重启即生效零代码改动
- **P1-5 bi-temporal 字段**（Graphiti）：`memories.valid_from / valid_to` 两列；老数据 valid_from 回填 = created_at；5.1 / 5.3 判 stale 时同步写 `valid_to=now`（保留 status='stale' 双层语义）；recall 三路 SELECT 加 `(valid_to IS NULL OR valid_to > now())` 过滤；admin UI 卡片 stale 条目显示"失效于 X"
- **P1-6 Auto Dream insight 生成**（Generative Agents reflection）：新 `auto_dream_insights(uid)` 抽样最近 90 天 confirmed memory（profile 8 + event 12）→ sonnet 写 1-3 条跨条目高阶观察 + supporting_ids → INSERT 为 `memory_type='insight' confidence=0.8 depends_on=supporting`；scheduler 03:13 cron 第 3 段；admin UI type 过滤器加 insight / 图谱紫色 stroke r=10 / audit 渲染 `memory_dream_insight` 事件；写 `prompt/memory_insight_dream.md` + `memory_prompts.render_insight_dream` helper
- **记忆评测调研**：写 `me/记忆评测系统调研.md`（学术 benchmark 对照 LoCoMo/LongMemEval/PerLTQA + 6 维评测设计 + LLM-as-judge 方法 + AIDemo P0/P1/P2 落地建议）
- **实测**：8058993786 跑 auto_dream_insights → 3 条高质量 insight（工作进取 / 睡眠困难模式 / 饮食随意将就），supporting_ids 正确，conf=0.8
