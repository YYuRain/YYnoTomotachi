# Agent Reach 工具能力集成

> 完成日期：2026-04-29。

## 背景

Agent Reach 是一套可插拔的 CLI 工具集，提供小红书搜索/读帖、Exa 语义网页搜索、YouTube/B 站视频元信息等能力。本次把这些能力接入陪伴 agent，让它在感知到用户分享链接或询问具体内容时，能自动拉取实时信息并自然地融入回复——不改变对话风格，不暴露"查资料"过程。

---

## 安装 Agent Reach

### 前提

- macOS，Homebrew 已装（用来装 pipx）
- Clash 代理在 `127.0.0.1:7897`（安装过程涉及 GitHub 下载）
- nvm 已装，Node 版本 v24.15.0

### 步骤

```bash
# 1. 用 Homebrew 安装 pipx
brew install pipx
pipx ensurepath

# 2. 安装 agent-reach 本体（含 mcporter，即 Exa 的 MCP 代理）
# 安装脚本需要通过代理访问 GitHub：
HTTPS_PROXY=http://127.0.0.1:7897 bash <(curl -s --proxy http://127.0.0.1:7897 \
  https://raw.githubusercontent.com/Panniantong/agent-reach/main/docs/install.md)

# 3. 安装小红书频道
HTTPS_PROXY=http://127.0.0.1:7897 pipx install xiaohongshu-cli
# 已安装版本：xiaohongshu-cli 0.6.4
```

安装后二进制位置：
- `xhs`：`/Users/yangyu/.local/bin/xhs`（pipx）
- `mcporter`：`/Users/yangyu/.nvm/versions/node/v24.15.0/bin/mcporter`（npm global）

---

## 小红书 Cookie 认证

`xhs` CLI 通过读取浏览器 Cookie 来认证，核心 Cookie 是 `a1`（长效 session key）。

### 坑：Chrome 的 Cookie 不够

Chrome 登陆小红书后只有 `web_session` + `id_token`，没有 `a1`。`a1` 是小红书 App 流量特有的标识，在 **Safari** 中访问小红书网页版后会写入。

### 正确做法

1. 在 Safari 中打开并登录 `xiaohongshu.com`，完成登录流程。
2. 给 **VSCode** 授予"完全磁盘访问权限"（系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加 VSCode）。
   - **注意**：只给 Terminal.app 权限不够，因为 Claude Code 以 VSCode subprocess 运行。
3. 运行 `xhs login --browser safari`，CLI 会扫描 Safari Cookie 数据库，自动提取 `a1` 写入本地配置。

验证：`xhs search "测试" --json` 返回 JSON 数据即为成功。

---

## tools.py 模块设计

`src/tools.py` 是所有工具调用的入口，对外暴露四个公共函数：

| 函数 | 作用 |
|------|------|
| `fetch_urls_in_message(text)` | 提取消息里所有 URL，并发读取，返回合并内容字符串 |
| `search_xhs(keyword)` | 搜索小红书，返回前 5 条笔记标题 + 互动数 |
| `search_web(query)` | 通过 Exa（mcporter）搜网页，返回摘要 |
| `read_url(url)` | 通过 Jina Reader 读取网页正文（前 600 字） |

### PATH 注入

`asyncio.create_subprocess_exec` 不继承 shell PATH，必须手动补：

```python
_EXTRA_PATHS = [
    "/Users/yangyu/.local/bin",                        # pipx: xhs
    "/Users/yangyu/.nvm/versions/node/v24.15.0/bin",  # mcporter
    "/opt/homebrew/bin",
]
_TOOL_ENV = {**_os.environ, "PATH": ":".join(_EXTRA_PATHS) + ":" + _os.environ.get("PATH", "")}
```

### URL 路由逻辑

`_fetch_one_url(url)` 按域名路由到最合适的读取方式，主方式失败则用 Exa 兜底搜索：

```
小红书域名   → _read_xhs_note()  → xhs read CLI
B 站域名     → _read_video()     → yt-dlp --dump-json
YouTube      → _read_video()     → yt-dlp --dump-json
其他         → read_url()        → Jina Reader (r.jina.ai, 需代理)
失败时       → _exa_fetch_url()  → mcporter call exa.web_search_exa (numResults=1)
```

### 小红书短链处理

`xhslink.com/o/xxxxx` 是 App 分享的短链，需要先解析成真实 URL 再提取 note_id：

```python
async def _resolve_url(url: str) -> str:
    raw = await _run(
        "curl", "-sL", "--proxy", "http://127.0.0.1:7897",
        "--max-time", "6", "-w", "\nFINAL_URL:%{url_effective}", "-o", "/dev/null",
        url,
    )
    for line in raw.splitlines():
        if line.startswith("FINAL_URL:"):
            return line[len("FINAL_URL:"):]
    return url
```

真实 URL 格式：`xiaohongshu.com/discovery/item/<24位note_id>?xsec_token=<token>`

然后提取 `note_id` + `xsec_token`，调用：
```bash
xhs read <note_id> --xsec-token "<token>" --json
```

`xsec_token` 是小红书的防爬安全令牌，不传会被拒绝。它嵌在分享 URL 的 query string 里，随链接一起传递。

---

## agent.py 集成方式

### 工具触发逻辑

两条路径（在 `_build_turn` 里与 emotion/recall/topics 并行执行）：

```python
if tools._URL_RE.search(user_text):
    # 有链接：确定性路径，直接读，不走 LLM 判断
    tool_task = asyncio.create_task(tools.fetch_urls_in_message(user_text))
else:
    # 无链接：LLM 判断是否需要搜索
    tool_task = asyncio.create_task(_maybe_fetch_context(user_text))
```

### LLM 工具判断（_maybe_fetch_context）

使用 `tier="aux"`（Sonnet，快且便宜）判断是否需要实时信息：

```json
{"needed": true, "tool": "xhs_search|web_search|read_url", "query": "搜索词或URL"}
```

触发条件（prompt 明确列出）：想了解某平台上的内容、问具体事实、提到网址、想知道最近流行什么。
不触发：闲聊、情绪倾诉、回忆往事、问观点/建议、日常打招呼。宁可少搜不滥搜。

### 关键：内容注入位置

工具结果**注入用户消息**，而不是 system prompt 末尾：

```python
if tool_ctx:
    user_msg = f"[链接内容]\n{tool_ctx}\n\n{user_text}"
else:
    user_msg = user_text
messages.append({"role": "user", "content": user_msg})
```

这是因为 MiniMax 会忽略 system prompt 末尾追加的内容（实测），注入用户消息则可靠生效。

---

## 踩过的坑

| 问题 | 根因 | 解决 |
|------|------|------|
| `mcporter: command not found` | subprocess 不继承 shell PATH | 手动构造 `_TOOL_ENV` 注入 nvm/pipx 路径 |
| mcporter 中文变 `\uXXXX` | `json.dumps` 默认 `ensure_ascii=True` | 所有 mcporter call expression 用 `json.dumps(..., ensure_ascii=False)` |
| Jina 搜索需要 auth | `s.jina.ai` 需要付费 | 改用 `exa.web_search_exa` via mcporter（免费） |
| prompts.py SyntaxError | 双引号嵌套中文字符串 | 外层改单引号，内部中文引号用 `"..."` |
| agent 说"看不了链接" | tool_context 注入 system prompt 末尾，MiniMax 忽略 | 改注入用户消息前缀 |
| xhslink 短链读不到内容 | Exa 搜索这个 URL 返回 GitHub 的 "xhslink resolver" 项目 | 先 `_resolve_url` 跟随重定向拿真实 URL，再提取 note_id + xsec_token |
| xhs read 24 字符 ID 失败 | 某些帖子路径是 `discovery/item/` 而非 `explore/`，xsec_token 必传 | 从 query string 解析 xsec_token 并明确传参 |
| Jina Reader 401 AuthenticationRequiredError | Clash 出口 IP 被 Jina 标记为 bad reputation，匿名查询拒绝 | `.env` 加 `JINA_API_KEY`；`read_url` 带 `Authorization: Bearer` |
| `read_url` 在 macOS 静默返回空（curl `SSL_ERROR_SYSCALL`） | LibreSSL 通过 `HTTPS_PROXY` env-var 做 CONNECT tunnel 偶发挂；同样命令显式 `-x` 走代理却稳 | `read_url` 改成显式 `curl -x $TELEGRAM_PROXY ...`，不再依赖 env 模式 |

---

## 工具可用性确认

```bash
# 验证 xhs
xhs search "穿搭" --json | head -5

# 验证 mcporter/Exa
mcporter call 'exa.web_search_exa(query: "Python 教程", numResults: 1)'

# 验证 yt-dlp（B站）
yt-dlp --dump-json --no-simulate --quiet "https://www.bilibili.com/video/BV1xx411c7mD"

# 验证 Jina（需代理）
curl -s --max-time 7 --proxy http://127.0.0.1:7897 "https://r.jina.ai/https://example.com" | head -20
```

---

## 局限与已知问题

- **B 站视频**：`yt-dlp` 部分视频需要登录才能获取完整信息，未登录时只返回标题。
- **YouTube**：国内需代理，`_read_video` 首次失败会自动用代理重试。
- **Jina Reader**：需代理（国内直连超时），慢于 Exa。主路径用于通用网页，XHS/视频已有专用路径。**2026-05-12 起需 `JINA_API_KEY`**（Clash 出口 IP 信誉差，匿名查询会 401；免费 key 注册：https://jina.ai/reader/，每月 1M token 够单用户用）。空 key 时 `read_url` 直接返回空，`_fetch_one_url` 退到 Exa 兜底。
- **32 字符 XHS note_id**：标准 24 字符 ID 可读，部分较新格式的 32 字符 ID 未经充分测试。
- **工具超时**：全部 8 秒硬超时（`_TIMEOUT = 8`），超时静默返回空字符串，不阻塞主流程。

## XHS 账号风控 + Exa fallback（2026-05-07 加）

小红书会按账号风控。账号被风控后 `xhs search` 返回：

```json
{"ok": false, "error": {"message": "API error: {\"code\": -104, ..., \"msg\": \"您当前登录的账号没有权限访问\"}"}}
```

**症状区分**：
- `xhs search` 整体失败（-104）—— 账号搜索权限被禁
- `xhs read` 大多数时候还能跑（read endpoint 风控范围不一样）
- Safari / Chrome 浏览器 cookie 都返回同样错误 → 不是 cookie 失效，是**账号本身**被禁

**修法**：`src/tools.py::search_xhs` 在拿到 `ok: false` 或空结果时，自动退化到 Exa 网页搜索（限定 `site:xiaohongshu.com`）。返回的是网页快照（标题+描述）而非原生 API JSON，但够用。bot 主链路无感。

**长期解药**（按从轻到重）：
1. 在浏览器里登录另一个小红书账号（小号），cookie auto-detect 会读到新账号的
2. `xhs login --qrcode` 用 xhs CLI 自带 cookie 存储扫码登
3. 等小红书风控解除（几天到几周）
