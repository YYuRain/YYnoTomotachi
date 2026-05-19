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
