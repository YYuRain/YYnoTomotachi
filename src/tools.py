"""Agent Reach 工具封装。

对外暴露：
- fetch_urls_in_message(text)  → 提取消息里的 URL 并读取内容（确定性，无需 LLM）
- search_xhs(keyword)          → 搜索小红书，返回前 5 条笔记摘要
- search_web(query)            → 通过 Exa 搜网页
- read_url(url)                → 通过 Jina Reader 读取网页正文

均使用 asyncio.create_subprocess_exec 执行 CLI，超时 8 秒，失败返回空字符串。
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
        env["HTTPS_PROXY"] = "http://127.0.0.1:7897"
        env["HTTP_PROXY"] = "http://127.0.0.1:7897"
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
    raw = await _run(
        "curl", "-sL", "--proxy", "http://127.0.0.1:7897",
        "--max-time", "6", "-w", "\nFINAL_URL:%{url_effective}", "-o", "/dev/null",
        url,
    )
    for line in raw.splitlines():
        if line.startswith("FINAL_URL:"):
            return line[len("FINAL_URL:"):]
    return url


async def _read_xhs_note(url: str) -> str:
    """用 xhs read 读取小红书帖子，返回标题 + 正文。"""
    from urllib.parse import urlparse, parse_qs, unquote

    # 短链先解析成真实 URL
    if "xhslink.com" in url:
        url = await _resolve_url(url)

    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    note_id = path_parts[-1] if path_parts else ""
    qs = parse_qs(parsed.query)
    xsec_token = unquote((qs.get("xsec_token") or [""])[0])

    if xsec_token and note_id:
        raw = await _run("xhs", "read", note_id, "--xsec-token", xsec_token, "--json")
    elif note_id:
        raw = await _run("xhs", "read", note_id, "--json")
    else:
        raw = await _run("xhs", "read", url, "--json")
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return ""
        card = items[0].get("note_card") or {}
        title = card.get("title") or card.get("display_title") or ""
        desc = card.get("desc") or ""
        user = (card.get("user") or {}).get("nickname") or ""
        info = card.get("interact_info") or {}
        likes = info.get("liked_count") or "0"
        parts = []
        if title:
            parts.append(f"标题：{title}")
        if user:
            parts.append(f"作者：{user}（{likes}赞）")
        if desc:
            parts.append(f"内容：{desc[:400]}")
        return "\n".join(parts) if parts else ""
    except (json.JSONDecodeError, TypeError):
        return raw[:400]


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


async def _exa_fetch_url(url: str) -> str:
    """用 Exa 搜索 URL 内容作为兜底。"""
    call_expr = f"exa.web_search_exa(query: {json.dumps(url, ensure_ascii=False)}, numResults: 1)"
    raw = await _run("mcporter", "call", call_expr)
    # 过滤掉 "小红书 - 你的生活兴趣社区" 这类无效结果
    if raw and "你的生活兴趣社区" not in raw and len(raw) > 50:
        return raw[:600]
    return ""


async def _fetch_one_url(url: str) -> str:
    """根据域名路由到最合适的读取方式，主方式失败时用 Exa 兜底。"""
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

    # 主方式失败，用 Exa 兜底
    result = await _exa_fetch_url(url)
    if result:
        log.info("url fetch exa fallback: %d chars ← %s", len(result), url[:60])
        return f"[{label} via 搜索]\n{result}"

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
    xhs CLI 失败时（账号风控 / API 拒绝 / 网络）退化到 Exa 网页搜索作为兜底。"""
    raw = await _run("xhs", "search", keyword, "--json")
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            # 显式失败标记：xhs CLI 返回 ok:false 表示 API 错误（含 -104 风控）
            if data.get("ok") is False:
                err_msg = (data.get("error") or {}).get("message", "")
                log.info("xhs search 失败，转 Exa 兜底：%s", err_msg[:80])
            else:
                items = (data.get("data") or {}).get("items") or []
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
                # 无结果也走 Exa 兜底（也许是关键词太冷门，xhs 返回空）

    # 兜底：用 Exa 搜 site:xiaohongshu.com 限定的网页结果
    call_expr = (
        f"exa.web_search_exa(query: "
        f"{json.dumps(f'{keyword} site:xiaohongshu.com', ensure_ascii=False)}, "
        f"numResults: 5)"
    )
    fallback = await _run("mcporter", "call", call_expr)
    if fallback and "你的生活兴趣社区" not in fallback:
        log.info("xhs search exa fallback: %d chars", len(fallback))
        return f"小红书搜索（网页结果）：\n{fallback[:600]}"
    return ""


async def search_web(query: str) -> str:
    """通过 Exa（mcporter）搜索网页，返回摘要。"""
    call_expr = f"exa.web_search_exa(query: {json.dumps(query, ensure_ascii=False)}, numResults: 3)"
    raw = await _run("mcporter", "call", call_expr)
    return raw[:800] if raw else ""


async def read_url(url: str) -> str:
    """通过 Jina Reader 读取网页正文（前 600 字）。"""
    reader_url = f"https://r.jina.ai/{url}"
    raw = await _run("curl", "-s", "--max-time", "7", reader_url, proxy=True)
    return raw[:600] if raw else ""
