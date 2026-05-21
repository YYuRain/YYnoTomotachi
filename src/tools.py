"""Agent Reach 工具封装（借自 https://github.com/Panniantong/Agent-Reach 的工具选型）。

对外暴露：
- fetch_urls_in_message(text)  → 提取消息里的 URL 并读取内容（确定性，无需 LLM）
- search_xhs(keyword)          → 搜索小红书前 5 条笔记摘要（需 ~/.xhs cookie）
- search_web(query)            → Jina Search 网页搜索（兼任"全网搜"通道）
- read_url(url)                → Jina Reader 读取网页正文
- read_github(query)           → gh CLI 读 GitHub 公开仓库 README + 最近 issue

均使用 asyncio.create_subprocess_exec 执行 CLI，超时 8 秒，失败返回空字符串。

容器二进制（Dockerfile 装）：
- yt-dlp（pip）：YouTube / B站 / 1800 站字幕
- gh（GitHub 官方 binary）：公开仓库 anon API（60/h）
- xhs（pip）：小红书；cookie 在 /root/.xhs/（compose volume 持久化）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger(__name__)

_TIMEOUT = 8

import os as _os

_EXTRA_PATHS = [
    "/Users/yangyu/.local/bin",
    "/Users/yangyu/.nvm/versions/node/v24.15.0/bin",
    "/opt/homebrew/bin",
]
_TOOL_ENV = {
    **_os.environ,
    "PATH": ":".join(_EXTRA_PATHS) + ":" + _os.environ.get("PATH", ""),
}

# 匹配消息里的 URL（http/https）
_URL_RE = re.compile(r"https?://[^\s一-鿿　-〿，。！？、；：""''【】《》〈〉]+")

# 小红书域名
_XHS_DOMAINS = {"xiaohongshu.com", "xhslink.com", "xhscdn.com"}
# B站域名
_BILI_DOMAINS = {"bilibili.com", "b23.tv"}
# 支持 yt-dlp 的视频域名
_VIDEO_DOMAINS = {"youtube.com", "youtu.be", "bilibili.com", "b23.tv"}


async def _run(*args: str, proxy: bool = False) -> str:
    env = {**_TOOL_ENV}
    if proxy:
        # 读 .env::TELEGRAM_PROXY——本地 dev 是 http://127.0.0.1:7897 (Clash)，
        # HK 容器是 http://mihomo:9981（compose 内网名）
        from .config import settings as _settings
        proxy_url = _settings().telegram_proxy or "http://127.0.0.1:7897"
        env["HTTPS_PROXY"] = proxy_url
        env["HTTP_PROXY"] = proxy_url
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        return stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        log.debug("tool timeout: %s", args[0])
        return ""
    except Exception as e:
        log.debug("tool error: %s", e)
        return ""


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # 去掉 www. 前缀
        return host.removeprefix("www.")
    except Exception:
        return ""


async def _resolve_url(url: str) -> str:
    """对短链（xhslink.com 等）跟随重定向，拿到真实 URL。"""
    from .config import settings as _settings
    proxy_url = _settings().telegram_proxy or "http://127.0.0.1:7897"
    raw = await _run(
        "curl", "-sL", "--proxy", proxy_url,
        "--max-time", "6", "-w", "\nFINAL_URL:%{url_effective}", "-o", "/dev/null",
        url,
    )
    for line in raw.splitlines():
        if line.startswith("FINAL_URL:"):
            return line[len("FINAL_URL:"):]
    return url


_XHS_COOKIE_PATH = "/root/.xhs/cookies.txt"


def _load_xhs_cookie() -> str:
    """从约定路径读 cookie string。本地 fallback 到 ~/.xhs/cookies.txt 让 dev 也能用。"""
    paths = [_XHS_COOKIE_PATH, _os.path.expanduser("~/.xhs/cookies.txt")]
    for p in paths:
        if _os.path.isfile(p):
            try:
                return open(p, encoding="utf-8").read().strip()
            except Exception:
                continue
    return _os.environ.get("XHS_COOKIE", "")


async def _read_xhs_note(url: str) -> str:
    """用 xhs Python SDK 读取小红书帖子，返回标题 + 正文。"""
    from urllib.parse import urlparse, parse_qs, unquote

    cookie = _load_xhs_cookie()
    if not cookie:
        log.debug("xhs note read: no cookie configured")
        return ""

    if "xhslink.com" in url:
        url = await _resolve_url(url)

    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    note_id = path_parts[-1] if path_parts else ""
    qs = parse_qs(parsed.query)
    xsec_token = unquote((qs.get("xsec_token") or [""])[0])
    if not note_id:
        return ""

    try:
        # SDK 调用是同步的，扔进 executor 避免阻塞 event loop
        import asyncio as _asyncio
        from xhs import XhsClient
        client = XhsClient(cookie=cookie)
        loop = _asyncio.get_event_loop()
        if xsec_token:
            data = await loop.run_in_executor(
                None, lambda: client.get_note_by_id(note_id, xsec_token)
            )
        else:
            # 没 xsec_token 走 HTML fallback
            data = await loop.run_in_executor(
                None, lambda: client.get_note_by_id_from_html(note_id, xsec_token or "")
            )
    except Exception as e:
        log.info("xhs note read err: %s", e)
        return ""

    if not isinstance(data, dict):
        return ""
    title = data.get("title") or data.get("display_title") or ""
    desc = data.get("desc") or ""
    user = (data.get("user") or {}).get("nickname") or ""
    info = data.get("interact_info") or {}
    likes = info.get("liked_count") or "0"
    parts = []
    if title:
        parts.append(f"标题：{title}")
    if user:
        parts.append(f"作者：{user}（{likes}赞）")
    if desc:
        parts.append(f"内容：{desc[:400]}")
    return "\n".join(parts) if parts else ""


async def _read_video(url: str) -> str:
    """用 yt-dlp 读取视频元信息（标题 + 描述）。"""
    raw = await _run("yt-dlp", "--dump-json", "--no-simulate", "--quiet", url)
    if not raw:
        # B站短链可能需要代理，重试
        raw = await _run("yt-dlp", "--dump-json", "--no-simulate", "--quiet", url, proxy=True)
    if not raw:
        return ""
    try:
        # yt-dlp 可能输出多行 JSON，取第一行
        first_line = raw.split("\n")[0]
        info = json.loads(first_line)
        title = info.get("title") or ""
        uploader = info.get("uploader") or info.get("channel") or ""
        desc = (info.get("description") or "")[:300]
        view_count = info.get("view_count") or ""
        parts = []
        if title:
            parts.append(f"标题：{title}")
        if uploader:
            parts.append(f"UP主：{uploader}" + (f"（{view_count}播放）" if view_count else ""))
        if desc:
            parts.append(f"简介：{desc}")
        return "\n".join(parts) if parts else ""
    except (json.JSONDecodeError, IndexError):
        return raw[:400]


async def _fetch_one_url(url: str) -> str:
    """根据域名路由到最合适的读取方式。主方式失败 → 返空（不再二次降级到 Exa，
    mcporter 已退役）。"""
    d = _domain(url)
    result = ""
    label = ""

    if any(xhs in d for xhs in _XHS_DOMAINS):
        result = await _read_xhs_note(url)
        label = "小红书帖子"
    elif any(bili in d for bili in _BILI_DOMAINS):
        result = await _read_video(url)
        label = "B站视频"
    elif any(yt in d for yt in ("youtube.com", "youtu.be")):
        result = await _read_video(url)
        label = "YouTube视频"
    else:
        result = await read_url(url)
        label = "网页内容"

    if result:
        log.info("url fetch %s: %d chars ← %s", label, len(result), url[:60])
        return f"[{label}]\n{result}"

    log.debug("url fetch failed: %s", url[:60])
    return ""


async def fetch_urls_in_message(text: str) -> str:
    """提取消息里所有 URL 并并发读取，返回合并后的上下文字符串。"""
    urls = _URL_RE.findall(text)
    if not urls:
        return ""
    # 最多处理前 3 个 URL，避免延迟过长
    tasks = [asyncio.create_task(_fetch_one_url(u)) for u in urls[:3]]
    results = await asyncio.gather(*tasks)
    parts = [r for r in results if r]
    return "\n\n".join(parts)


async def search_xhs(keyword: str) -> str:
    """搜索小红书，返回前 5 条笔记标题和互动数。

    用 `xhs` Python SDK 的 `XhsClient.get_note_by_keyword`。
    cookie 从 `/root/.xhs/cookies.txt` 或 env `XHS_COOKIE` 读；缺 cookie 直接返空。
    详见 `document/agent-reach-integration.md` 的 cookie 注入流程。
    """
    cookie = _load_xhs_cookie()
    if not cookie:
        log.debug("xhs search: no cookie configured")
        return ""
    try:
        import asyncio as _asyncio
        from xhs import XhsClient
        client = XhsClient(cookie=cookie)
        loop = _asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: client.get_note_by_keyword(keyword, page=1, page_size=10)
        )
    except Exception as e:
        log.info("xhs search 失败：%s", str(e)[:120])
        return ""

    if not isinstance(data, dict):
        return ""
    items = data.get("items") or []
    lines: list[str] = []
    for item in items[:5]:
        card = item.get("note_card") or {}
        title = card.get("display_title") or card.get("title") or ""
        info = card.get("interact_info") or {}
        likes = info.get("liked_count") or "0"
        if title:
            lines.append(f"「{title}」（{likes}赞）")
    if lines:
        return "小红书搜索结果：\n" + "\n".join(lines)
    return ""


async def search_web(query: str) -> str:
    """走 Jina Search API（s.jina.ai）。复用 JINA_API_KEY 不引第三方 CLI。

    历史：原来走 Exa via mcporter，但 mcporter 是本地 nvm 装的、容器没有。
    Jina Search 免费层每月 1M token，对个人项目够用。
    """
    if not query.strip():
        return ""
    from .config import settings as _settings
    from urllib.parse import quote as _quote
    s = _settings()
    url = f"https://s.jina.ai/?q={_quote(query)}"
    args = ["curl", "-s", "--max-time", "15"]
    if s.telegram_proxy:
        args += ["-x", s.telegram_proxy]
    if s.jina_api_key:
        args += ["-H", f"Authorization: Bearer {s.jina_api_key}"]
    # 默认 Jina 返回长 markdown，截 1500 字让主 LLM 看 3-4 条命中即可
    args += ["-H", "X-Respond-With: no-content"]  # 只要 title+url+desc，不要全文
    args.append(url)
    raw = await _run(*args)
    if not raw:
        return ""
    # 鉴权/限流失败时返回 JSON 错误体
    if raw.lstrip().startswith("{") and ('"code":4' in raw or 'AuthenticationRequiredError' in raw):
        log.info("jina search 失败：%s", raw[:120])
        return ""
    return raw[:1500]


_GITHUB_RE = re.compile(
    r"(?:https?://github\.com/)?([a-zA-Z0-9][a-zA-Z0-9_-]*)/([a-zA-Z0-9._-]+?)(?:\.git)?(?:/|$|\s)"
)


async def read_github(query: str) -> str:
    """读 GitHub 公开仓库的 README 摘要 + 最近 5 个 issue 标题。

    query: `owner/repo` 或 `https://github.com/owner/repo[/...]`
    直接走 GitHub REST API（anon 60/h，对 bot 用法足够）；不依赖 gh CLI——
    gh CLI v2.x 强制 auth 即便 anon 也要 GH_TOKEN，太麻烦。
    """
    q = query.strip()
    if not q:
        return ""
    m = _GITHUB_RE.search(q + " ")
    if not m:
        if "/" in q and " " not in q:
            owner_repo = q.strip("/")
        else:
            return ""
    else:
        owner_repo = f"{m.group(1)}/{m.group(2)}"

    from .config import settings as _settings
    s = _settings()

    async def _gh_api(path: str) -> str:
        args = ["curl", "-s", "--max-time", "10", "-L"]
        if s.telegram_proxy:
            args += ["-x", s.telegram_proxy]
        args += [
            "-H", "Accept: application/vnd.github.v3+json",
            "-H", "User-Agent: aidemo-bot",
            f"https://api.github.com/{path.lstrip('/')}",
        ]
        return await _run(*args)

    repo_raw = await _gh_api(f"repos/{owner_repo}")
    if not repo_raw:
        return ""
    try:
        repo = json.loads(repo_raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if "message" in repo and "Not Found" in repo.get("message", ""):
        return ""

    parts = []
    desc = repo.get("description") or ""
    stars = repo.get("stargazers_count") or 0
    lang = repo.get("language") or ""
    parts.append(f"仓库：{owner_repo}（⭐{stars}{' · ' + lang if lang else ''}）")
    if desc:
        parts.append(f"介绍：{desc[:200]}")

    readme_raw = await _gh_api(f"repos/{owner_repo}/readme")
    if readme_raw:
        try:
            readme_obj = json.loads(readme_raw)
            content_b64 = readme_obj.get("content", "")
            if content_b64:
                import base64
                readme_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                readme_clean = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", readme_text)
                readme_clean = re.sub(r"<[^>]+>", "", readme_clean)
                readme_clean = readme_clean.strip()
                if readme_clean:
                    parts.append(f"README 摘要：{readme_clean[:400]}")
        except Exception as e:
            log.debug("github readme parse err: %s", e)

    issues_raw = await _gh_api(f"repos/{owner_repo}/issues?state=all&per_page=5")
    if issues_raw:
        try:
            issues = json.loads(issues_raw)
            if isinstance(issues, list) and issues:
                lines = [
                    f"#{i.get('number')} [{i.get('state')}] {(i.get('title') or '')[:80]}"
                    for i in issues if isinstance(i, dict)
                ]
                if lines:
                    parts.append("最近 issue：\n  " + "\n  ".join(lines))
        except (json.JSONDecodeError, TypeError):
            pass

    return "\n".join(parts)


async def read_url(url: str) -> str:
    """通过 Jina Reader 读取网页正文（前 600 字）。

    Jina 对匿名查询限速，IP 信誉差时直接 401。设了 JINA_API_KEY 就带 Authorization 鉴权。
    返回的 401 / AuthenticationRequiredError JSON 当作失败让上游走 Exa 兜底。
    """
    from .config import settings as _settings
    reader_url = f"https://r.jina.ai/{url}"
    s = _settings()
    # 显式 -x 比 HTTPS_PROXY env 稳——env 模式下 LibreSSL 偶发 SSL_ERROR_SYSCALL
    args = ["curl", "-s", "--max-time", "10"]
    if s.telegram_proxy:
        args += ["-x", s.telegram_proxy]
    if s.jina_api_key:
        args += ["-H", f"Authorization: Bearer {s.jina_api_key}"]
    args.append(reader_url)
    raw = await _run(*args)  # 不再依赖 proxy=True 设 env，env_var 与显式 -x 双用易冲突
    if not raw:
        return ""
    # Jina 失败时返回 JSON 错误体，识别后视为失败让 _fetch_one_url 走 Exa
    if raw.lstrip().startswith("{") and ("AuthenticationRequiredError" in raw or '"code":4' in raw):
        log.info("jina reader 鉴权/限流失败：%s", raw[:120])
        return ""
    return raw[:600]
