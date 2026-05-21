# Agent Reach 工具能力集成

> 接入：2026-04-29。**2026-05-19**：mcporter / Exa 退役，search 改 Jina REST。
> **2026-05-21 重接**：借 [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) 选型，把 yt-dlp / xhs / gh CLI 真正装进容器；新加 `read_github` 工具。

## 背景

陪伴 agent 在感知到用户分享链接或问"最近 X"类话题时，能自动拉取实时信息融入回复——
不改变对话风格、不暴露"查资料"过程。

## 当前工具栈（2026-05-21 起）

| 工具 | 实现 | 容器依赖 | 触发场景 |
|------|------|---------|---------|
| `read_url(url)` | Jina Reader (`r.jina.ai/<url>`) | `JINA_API_KEY` | 网页 URL（自动域名路由的兜底） |
| `search_web(query)` | Jina Search (`s.jina.ai/?q=`) | `JINA_API_KEY` | 通用全网搜索 |
| `search_xhs(keyword)` | `xhs` Python SDK (XhsClient) | pip 包 `xhs>=0.2.13` + cookie 文件 | 用户聊到小红书话题 |
| `read_github(query)` | GitHub REST API（curl） | 无（直接 https://api.github.com） | 用户聊到 GitHub 仓库 |
| URL 自动路由 | `_fetch_one_url` 按域名派发 | 见下 | user 消息含完整 URL |

**URL 自动路由**（`tools.fetch_urls_in_message`）按域名派给最合适的实现：
- 小红书域名（xiaohongshu.com / xhslink.com / xhscdn.com）→ `_read_xhs_note`（xhs CLI）
- B 站（bilibili.com / b23.tv）+ YouTube（youtube.com / youtu.be）→ `_read_video`（yt-dlp）
- 其它 → `read_url`（Jina）

主路径失败**直接返空**，不再二次降级到 Exa（mcporter 已退役，2026-05-19）。

## 容器依赖（Dockerfile）

```dockerfile
# yt-dlp（YouTube/B站字幕）+ xhs（小红书 SDK）—— pip 包
RUN pip install --no-cache-dir "yt-dlp>=2024.12.0" "xhs>=0.2.13"
```

容器内验证：
```bash
docker compose exec bot which yt-dlp                     # /usr/local/bin/yt-dlp
docker compose exec bot python -c "from xhs import XhsClient; print('xhs SDK OK')"
docker compose exec bot python -c "
import asyncio
from src import tools
print(asyncio.run(tools.read_github('python/cpython'))[:200])
"
```

**注**：之前考虑过装 gh CLI，但 v2.x 之后强制 auth（公开仓库 anon API 也要 GH_TOKEN）；
所以 `read_github` 改直接 `curl https://api.github.com/repos/...` REST，不依赖 gh binary。

## 小红书 cookie 注入流程（手动一次）

`xhs` SDK 期望 cookie 是单行字符串（`a1=xxx; web_session=yyy; ...` 半角分号分隔）。
路径约定：容器内 `/root/.xhs/cookies.txt`（compose mount `./data/.xhs-cookie:/root/.xhs`）。
fallback 顺序：cookie.txt → 环境变量 `XHS_COOKIE`。

**注入步骤**（HK 服务器一次性）：
1. 用户本机 Chrome 已登录 xiaohongshu.com，访问 https://www.xiaohongshu.com
2. F12 → Network → 任意 xhs API 请求 → 复制 `Cookie:` 请求头的完整 value
3. ssh 到 HK：`ssh hk-bot 'mkdir -p ~/aidemo/data/.xhs-cookie && cat > ~/aidemo/data/.xhs-cookie/cookies.txt'`
4. 粘贴 cookie 字符串 + Ctrl+D
5. `docker compose restart bot`

或者更简单：把 cookie 字符串加到 HK 的 `.env`：
```
XHS_COOKIE=a1=xxx; web_session=yyy; ...
```
这种方式不需要 mount 也行（但安全角度文件 mount 更标准）。

**风控提示**：xhs API 容易 `-104 无权限`，建议**用专用小号**而不是主账号；`tools.search_xhs` 失败时直接返空，
不影响其他工具，admin UI audit 能看到 `xhs search 失败：<msg>` 日志。

## GitHub REST API

直接走 `https://api.github.com/repos/<owner>/<repo>` 公开 API：
- anon rate limit 60/h（IP 维度），对 bot 用法（用户偶尔分享 repo）足够
- 不需要任何 token / 登录
- HK 出口走 `TELEGRAM_PROXY` 环境变量（`http://mihomo:9981`）

`read_github` 输出格式：
```
仓库：anthropics/anthropic-sdk-python（⭐3.5K · Python）
介绍：The official Anthropic Python SDK
README 摘要：（前 400 字）...
最近 issue：
  #234 [open] AsyncClient connection pooling issue
  #232 [closed] Type hints for tool_use
  ...
```

---

## tools.py 模块设计

`src/tools.py` 当前对外暴露：

| 函数 | 作用 |
|------|------|
| `fetch_urls_in_message(text)` | 提取消息里所有 URL，并发读取，返回合并内容字符串 |
| `search_web(query)` | Jina Search REST 搜网页，返回 ≤1500 字摘要 |
| `read_url(url)` | Jina Reader 读取网页正文，返回 ≤600 字 |

`agent._TOOL_FUNCS` 映射 4 条路径——LLM detect prompt 也对应 4 选 1：
- `web_search` / `read_url` / `search_xhs` / `read_github`

### URL 路由逻辑

`_fetch_one_url(url)` 按域名路由（**容器内 binary 都已装**，2026-05-21 起）：

```
小红书域名（xiaohongshu.com / xhslink.com / xhscdn.com）→ _read_xhs_note() → xhs CLI（需 cookie）
B 站（bilibili.com / b23.tv）+ YouTube                  → _read_video()    → yt-dlp --dump-json
其它                                                    → read_url()       → Jina Reader (r.jina.ai)
失败时                                                  → 直接返空（不再二次降级）
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

使用 `tier="aux"`（deepseek-flash 等，快且便宜）判断是否需要实时信息：

```json
{"needed": true, "tool": "web_search|read_url|search_xhs|read_github", "query": "搜索词 / URL / owner/repo"}
```

触发条件（prompt 明确列出）：
- 想了解 GitHub 仓库 → `read_github`，query=`owner/repo`
- 用户聊到小红书话题 → `search_xhs`，query=纯关键词（不加"小红书"）
- 提到网址 → 一般走 URL 自动提取（`fetch_urls_in_message`）
- 通用搜索 → `web_search`

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
| 容器 search 全 0 chars | 容器没装 mcporter/xhs | 2026-05-19 临时改用 Jina Search REST 顶替；2026-05-21 把 xhs/yt-dlp 装进容器，gh CLI 加 binary，正式接入 |
| LLM 写 query 用 2025 | baseline 训练截止 | `_TOOL_DETECT_SYSTEM` 运行时拼今天日期 + "今年是 2026" |
| user 问"你能查 X 吗"被判 false | LLM 当成"问能力" | prompt 加规则：试探口吻 + 具体话题 → needed=true |
| bot 回"我不联网" | sonnet 默认人设 | `_ROLE_DISCIPLINE` 显式说明"你有 read_url/web_search 工具" |
| prompts.py SyntaxError | 双引号嵌套中文字符串 | 外层改单引号，内部中文引号用 `"..."` |
| agent 说"看不了链接" | tool_context 注入 system prompt 末尾，MiniMax 忽略 | 改注入用户消息前缀 |
| xhslink 短链读不到内容 | Exa 搜索这个 URL 返回 GitHub 的 "xhslink resolver" 项目 | 先 `_resolve_url` 跟随重定向拿真实 URL，再提取 note_id + xsec_token |
| xhs read 24 字符 ID 失败 | 某些帖子路径是 `discovery/item/` 而非 `explore/`，xsec_token 必传 | 从 query string 解析 xsec_token 并明确传参 |
| Jina Reader 401 AuthenticationRequiredError | Clash 出口 IP 被 Jina 标记为 bad reputation，匿名查询拒绝 | `.env` 加 `JINA_API_KEY`；`read_url` 带 `Authorization: Bearer` |
| `read_url` 在 macOS 静默返回空（curl `SSL_ERROR_SYSCALL`） | LibreSSL 通过 `HTTPS_PROXY` env-var 做 CONNECT tunnel 偶发挂；同样命令显式 `-x` 走代理却稳 | `read_url` 改成显式 `curl -x $TELEGRAM_PROXY ...`，不再依赖 env 模式 |

---

## 工具可用性确认

```bash
# 验证 Jina Reader（read_url）
curl -s --max-time 7 -H "Authorization: Bearer $JINA_API_KEY" \
  "https://r.jina.ai/https://example.com" | head -20

# 验证 Jina Search（search_web）
curl -s --max-time 10 -H "Authorization: Bearer $JINA_API_KEY" \
  -H "X-Respond-With: no-content" \
  "https://s.jina.ai/?q=$(python3 -c 'import urllib.parse;print(urllib.parse.quote("最近大事 2026"))')" | head -20

# 验证 yt-dlp（B站，仅本地有）
yt-dlp --dump-json --no-simulate --quiet "https://www.bilibili.com/video/BV1xx411c7mD"

# 验证 xhs（仅本地有 + 账号未风控时）
xhs search "穿搭" --json | head -5
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
