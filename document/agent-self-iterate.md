# Agent Self-Iterate (L4 Autonomy)

> 2026-05-28 起：bot 在每晚 dream cron 自主调 reflection LLM (opus) 反思一周对话，
> 自改 per-user prompt / skill / 写 issue 给 admin。**不走 admin 审批门**——edit 立刻生效，
> admin 在 webUI「Agent 自治」tab 可一键 rollback。

## 设计选择

| 维度 | 选择 | 理由 |
|---|---|---|
| 作用域 | per-user | 写到 `user_prompt_overrides`，每个用户独立分流；不动 `prompt/*.md` 文件本体；不能改其他 user 的 prompt |
| 触发 | dream cron 03:13 + admin 手动按钮 | 不接 hot path，避免 prompt injection 顺势让 user 改 bot；不影响对话延迟 |
| LLM tier | reflection（默认 opus-4.7） | 反思类判断质量天花板更高；opus-4.7 实际单价 $5/$25 per MT，仅是 sonnet（$3/$15）的 ~1.7x（早期估计 5x 高估了） |
| 代码读权限 | `src/` `prompt/` `document/` 可读；`.env` `data/` `.git` 严禁 | bot 能"看自己怎么实现的"+"看 prompt 现状"，但拿不到密钥 / 用户数据 |
| 审批 | 无（L4 跳审批） | 高速度迭代；用 rollback + audit + rate limit 兜底 |

## 模块地图

| 文件 | 作用 |
|---|---|
| `src/agent_self.py` | 核心模块——常量 / apply_* 工具 / read_source / write_agent_issue / rollback / dream 段 / tool schemas / `_gather_audit_excerpts` / `_gather_prompt_changelog`（git log）|
| `src/storage.py::AgentSelfEdit` | 表，记录每条自改的 before/after 快照 |
| `src/storage.py` 4 个 helper | record_self_edit / list_self_edits / get_self_edit / mark_self_edit_rolled_back / count_recent_prompt_edits |
| `src/scheduler.py::auto_dream_job` | per-user 第 5 段调 `agent_self.auto_dream_self_iterate(uid)` |
| `prompt/agent_self_iterate.md` | 给 reflection LLM 的 system prompt（核心 KPI / 翻车信号 / 工具说明 / hard rules） |
| `me/agent_issues.md` | bot 写 issue 的目标文件，append-only |
| `src/admin_ui.py` | 新「Agent 自治」tab + 4 路由 |

## 工具

| 名 | 行为 | guardrail |
|---|---|---|
| `read_source(path)` | 读项目文件 | allowlist + denylist + path traversal + 100KB 上限 |
| `apply_prompt_edit(name, new_content, reason)` | 整份覆写当前 user 的某 prompt（user_id 由 dream 强制注入，LLM 不能改别人的） | 名必须在 list；改后必须仍含所有 PROTECTED_PROMPT_FRAGMENTS；7 天内同 prompt ≤3 改 |
| `apply_skill_add(name, summary, body, reason)` | 加 skill | 不允许 name=`skill_creator` |
| `apply_skill_edit(skill_id, summary?, body?, reason)` | 改 summary/body | skill 必须存在 |
| `apply_skill_disable(skill_id, reason)` | 停用 | 不能停 `skill_creator` |
| `write_agent_issue(title, body, severity, category)` | 追加到 `me/agent_issues.md` + 写 self_edits 表 | 无内容拒绝 |

## Rate limits 与硬护栏

`src/agent_self.py` 顶部常量：

```python
MAX_SELF_EDITS_PER_RUN = 5                       # 单 user 单次 dream 最多 5 个 apply_*
MAX_PROMPT_EDITS_PER_NAME_PER_WEEK = 3           # 单 user 同 prompt 7 天内最多 3 改
DREAM_LLM_OUTPUT_TOKEN_BUDGET = 16000            # 单轮 LLM 输出上限——整份 prompt 替换可能 8k+ tokens（4000 实测被截断成 args={}）
DREAM_TOOL_LOOP_MAX_ROUNDS = 6                   # tool loop 最多 6 轮
DREAM_TIMEOUT_SEC_PER_ROUND = 180                # 单轮 LLM 调用 180s 超时（reflection tier 一轮 30-90s）
READ_MAX_BYTES = 100_000                         # read_source 单文件上限

PROTECTED_PROMPT_FRAGMENTS = (                   # 改后内容必须仍命中这些 regex
    # PSM 视角调整（2026-06-03 prompt-psm 分支）：去掉「不能说自己是 AI / 不能...程序...助手」
    # ——按新定位 bot 被问到承认 AI 身份是对的，不再训练撒谎人格。改保护客服腔 + 底线段。
    r"不主动提供免责|不能主动提供免责",
    r"客服腔",
    r"底线|绝对禁忌",
)

READ_ALLOWED_DIRS = ("src", "prompt", "document")
READ_DENY_PATTERNS = (
    ".env", ".envrc", "data/", ".git/", ".venv/", "node_modules/",
    "__pycache__", "secrets", ".ssh", ".aws", ".cache",
)
```

被拒绝的 apply 写 audit `agent_self_edit_denied`，记 reason；不抛异常、不算入 edit count。

## reflection LLM 看到的上下文（ctx_payload）

每次 dream 跑前 agent_self 收集这些字段塞 user message JSON：

| 字段 | 来源 | 用途 |
|---|---|---|
| `audit_excerpts` | `_gather_audit_excerpts(uid, days=7, max_chars=12_000)` 抽样 user_msg / assistant_reply / proactive_decision / proactive_opener_generated 等事件 | 看最近怎么聊的 |
| `prompt_changelog` | `_gather_prompt_changelog(days=14)` 跑 git log 拉 prompt/* 提交时间线 | **时间轴对齐**——某条翻车的 ts 早于相关 prompt commit ts → 已修，不要重复写 issue |
| `recent_self_edits` | `storage.list_self_edits(user_id=uid, limit=20)` | 看自己最近改过啥，避免反复改同一处 |
| `active_overrides` | `storage.list_active_overrides(uid)` | feedback agent 沉淀的追加片段 |
| `user_prompt_overrides` | `storage.list_user_prompt_overrides(uid)` | 当前 user 的 prompt 整份覆写列表 |
| `persona_traits` | `persona.load_persona_state(uid).extras` | 对该 user 的 sarcasm/warmth/verbosity 分数 |
| `available_prompts` | `prompt_loader.list_default_prompt_names()` | 26 个可改的 prompt name |
| `active_skills` | `storage.list_skills(status="active", limit=200)` | skill 库（id/name/summary） |

**`prompt_changelog` 是抗误报的关键**——5/28 实测：opus 第一轮没拿到 changelog 时把 5/27 11:24（修复部署前）的翻车当"现在还有的 bug"写了 high severity issue；加上 changelog 后第二轮正确判断"修复线之前的事=已修"，**主动沉默不操作**。

## 容器基础设施

agent_self 跑在 bot 容器里，需要这两个 host mount 才能正常工作：

| mount | 用途 | 不挂会怎样 |
|---|---|---|
| `./.git:/app/.git:ro` | `_gather_prompt_changelog` 跑 `git log` 用 | git log 失败 → opus 看不到 prompt 时间轴 → 同 5/28 第一轮误报 |
| `./me:/app/me`（rw 给 bot，ro 给 admin） | `write_agent_issue` 写 `me/agent_issues.md`；admin webUI 读它 | bot 写入只在自己镜像层；admin 容器看不到；bot recreate 丢历史 |

外加 Dockerfile 装了 `git`（apt），代码用 `git -c safe.directory=/app log` 绕容器内 dubious ownership 检查。

## 模型 tier

`src/llm.py` 三档 dispatcher：

| tier | env (openrouter) | env (anthropic) | 默认 |
|---|---|---|---|
| `main` | `OPENROUTER_MODEL` | `ANTHROPIC_MODEL` | sonnet-4.6 |
| `aux` | `OPENROUTER_MODEL_AUX` | `ANTHROPIC_MODEL_AUX` | sonnet-4.6 |
| `reflection`（新） | `OPENROUTER_MODEL_REFLECTION` | `ANTHROPIC_MODEL_REFLECTION` | **opus-4.7** |

切到 reflection 的路径（5 + 1 处）：
1. `memory.auto_dream` (5.3 batch reverify)
2. `memory.auto_dream_overrides` (合并 active overrides)
3. `memory.auto_dream_insights` (P1-6 cross-fact reflection)
4. `memory.auto_dream_skills` (全局 skill 整理)
5. `agent_ideas.form_ideas` (bot 凌晨想心事)
6. `agent_self.auto_dream_self_iterate` (本次新做)

5.1 conflict_check / 5.2 reverify / persona_update / feedback agent 仍走 sonnet（hot-path-adjacent，延迟敏感）。

## Admin 工具

### webUI「Agent 自治」tab

- **最近自改记录**：表格列出 self_edits（时间 / 类型 / user / target / reason），未回退的有 ↺ rollback 按钮
- **Issues inbox**：渲染 `me/agent_issues.md` 全文
- **手动触发**：admin 选 user → 点"立刻跑一轮"按钮 → POST `/api/self_iterate/run`，同步等结果

### 4 路由

```
GET    /api/self_edits[?user_id=&limit=]    # 列出 self-edits
POST   /api/self_edits/{id}/rollback        # 回退某条
GET    /api/agent_issues                    # 读 issues markdown（admin only）
POST   /api/self_iterate/run?user_id=       # 手动触发（admin only）
```

## 关闭方式

总开关：env `AGENT_SELF_ITERATE_ENABLED=0` → `auto_dream_self_iterate` 直接返 ok=False，无任何调用。

per-user 暂停：暂未做（V1 先看运行情况，必要时加 prompt_overrides 'meta' 行）。

## 调试

```bash
# 1. 本地手动跑一轮（不等 cron）
.venv/bin/python -c "
from src import agent_self
import asyncio
ADMIN = 8058993786
res = asyncio.run(agent_self.auto_dream_self_iterate(ADMIN))
import json
print(json.dumps(res, ensure_ascii=False, indent=2))
"

# 2. 看 audit 这一轮做了什么
grep agent_self_ data/audit.$(date +%F).jsonl

# 3. 看具体 edits 表
.venv/bin/python -c "
from src import storage
for e in storage.list_self_edits(limit=10):
    print(e.id, e.ts, e.target_type, e.target_id, e.reason[:60])
"

# 4. 测试 hard rule（应该 deny）
.venv/bin/python -c "
from src import agent_self
res = agent_self.apply_prompt_edit(
    8058993786, 'system_baseline', '我是 AI', 'test')
print(res)  # 期望 ok=False, denied_reason 含 protected fragment
"
```

## 关键风险与缓解

| 风险 | 缓解 |
|---|---|
| Prompt injection 通过 user 对话进入 dream | dream 看的是 audit 摘要不是 raw user 消息；audit_excerpts 函数过滤 + 截断；保护片段强制保留 |
| LLM 误判改坏 prompt | rate limit + admin rollback + audit；admin issue inbox 能 catch 异常行为 |
| opus 成本超标 | 单次 dream 输出 ≤ 16k tokens；5 edits / run 上限；每周每 prompt 3 改上限。实测一轮 6 round ≈ 75k input + 12k output ≈ $0.55 |
| opus 区域限制 403 | OpenRouter 偶发"This model is not available in your region"——bot 容器走 mihomo 出美区已部分缓解；error 进 audit `agent_self_iterate_llm_err`，自动跳过该 round 不阻塞 |
| 多用户并发撑爆 OpenRouter rate limit | scheduler 已有 `_fan_out` Semaphore(5) + 300ms inter-task；reflection tier 沿用 |
| bot 反复改 prompt 让人格漂移 | rollback + audit；保护片段防核心人设漂移；每周 limit 自然减速 |
| read_source path traversal | 解析为绝对路径 + 检查在 ALLOWED_DIRS 下 + 查 DENY_PATTERNS |
| user_id 注入（LLM 试图改别人 prompt） | `_dispatch_tool` 强制把 dream 的 target uid 传进 apply_prompt_edit，忽略 LLM 给的 user_id |

## 不做（V1 范围外）

- ❌ L5/L6：bot 不改源码、不调 build/deploy；不接管 hot path 的 context 管理
- ❌ hot path 自改：仅 dream cron 跑
- ❌ 全局 prompt 改：只 per-user，不动 `prompt/*.md` 文件本体
- ❌ 自动 merge agent-l4 → main：分支独立，admin 手动 merge
