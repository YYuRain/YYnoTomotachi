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


async def _run(*args: str, proxy: bool = False, timeout: float | None = None) -> str:
    env = {**_TOOL_ENV}
    if proxy:
        # 读 .env::TELEGRAM_PROXY——本地 dev 是 http://127.0.0.1:7897 (Clash)，
        # HK 容器是 http://mihomo:9981（compose 内网名）
        from .config import settings as _settings
        proxy_url = _settings().telegram_proxy or "http://127.0.0.1:7897"
        env["HTTPS_PROXY"] = proxy_url
        env["HTTP_PROXY"] = proxy_url
    eff_timeout = timeout if timeout is not None else _TIMEOUT
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=eff_timeout)
        out = stdout.decode("utf-8", errors="replace").strip()
        if not out:
            err = stderr.decode("utf-8", errors="replace").strip()
            log.warning("tool %s rc=%s stdout=empty stderr=%r", args[0], proc.returncode, err[:300])
        return out
    except asyncio.TimeoutError:
        log.warning("tool timeout (%.1fs): %s", eff_timeout, args[0])
        # wait_for 超时时只 cancel awaiter——subprocess 仍在跑，不杀会僵尸化堆积
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                # 给 1s 收尸；超了说明真的卡死，让 OS 接手
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            except Exception as e:
                log.debug("tool kill err: %s", e)
        return ""
    except Exception as e:
        log.warning("tool error: %s args=%s", e, args[:3])
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                pass
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


async def _read_xhs_note(url: str) -> str:
    """用 xiaohongshu-cli (`xhs read`) 读取小红书帖子，返回标题 + 正文。"""
    from urllib.parse import urlparse, parse_qs, unquote

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
    except (json.JSONDecodeError, TypeError):
        return raw[:400]
    if not isinstance(data, dict):
        return ""
    if data.get("ok") is False:
        log.info("xhs read 失败：%s", str((data.get("error") or {}).get("message", ""))[:120])
        return ""
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return ""
    card = items[0].get("note_card") or {}
    title = card.get("title") or card.get("display_title") or ""
    desc = card.get("desc") or ""
    user = (card.get("user") or {}).get("nickname") or ""
    info = card.get("interact_info") or {}
    likes = info.get("liked_count") or "0"
    # 提取图片 URL——xhs 帖子大量信息在图里（穿搭 / 探店 / 美食），最多前 4 张
    image_list = card.get("image_list") or []
    image_urls: list[str] = []
    for img in image_list[:4]:
        if not isinstance(img, dict):
            continue
        url_default = img.get("url_default") or img.get("url") or img.get("url_pre")
        if url_default:
            image_urls.append(url_default)
    parts = []
    if title:
        parts.append(f"标题：{title}")
    if user:
        parts.append(f"作者：{user}（{likes}赞）")
    if desc:
        parts.append(f"内容：{desc[:400]}")
    if image_urls:
        # 把图链接列出来——主 LLM 觉得对用户有价值时直接把链接贴进回复，
        # Telegram 客户端会自动渲染成图片预览
        parts.append(
            f"图（共 {len(image_list)} 张，前 {len(image_urls)} 张可分享给用户）："
            + "\n  ".join([""] + image_urls)
        )
        # 前 2 张图跑 OCR——xhs 大量是文字截图（穿搭笔记/教程/语录），不 OCR 主 LLM
        # 看不见内容。控制 2 张是延迟+成本的折中（每张 vision LLM 调用 ~1-3s + ~$0.003）
        try:
            from . import vision
            ocr_texts = await vision.ocr_image_urls(image_urls[:2])
            ocr_lines: list[str] = []
            for i, txt in enumerate(ocr_texts, 1):
                if txt:
                    ocr_lines.append(f"图 {i} 内容：{txt[:500]}")
            if ocr_lines:
                parts.append("【图内 OCR/描述】\n" + "\n".join(ocr_lines))
        except Exception as e:
            log.info("xhs read ocr err: %s", e)
    # 原 URL 留一份给 bot 引用——user 已经把链接发过来，bot 引回去对方一目了然
    parts.append(f"链接：{url}")
    return "\n".join(parts)


async def _read_video(url: str) -> str:
    """用 yt-dlp 读取视频元信息（标题 + 描述）。"""
    # `--dump-json` 默认 simulate 模式（只拿 metadata 不下载）；之前误加 `--no-simulate`
    # 会强制真下载视频文件——8 秒 timeout 必然超时（容器 2026-05-21 实测确认）。
    raw = await _run("yt-dlp", "--dump-json", "--skip-download", "--quiet", url)
    if not raw:
        # B站国内 IP 限制，重试走 mihomo 美区代理
        raw = await _run("yt-dlp", "--dump-json", "--skip-download", "--quiet", url, proxy=True)
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
        webpage_url = info.get("webpage_url") or info.get("original_url") or ""
        parts = []
        if title:
            parts.append(f"标题：{title}")
        if uploader:
            parts.append(f"UP主：{uploader}" + (f"（{view_count}播放）" if view_count else ""))
        if desc:
            parts.append(f"简介：{desc}")
        if webpage_url:
            parts.append(f"链接：{webpage_url}")
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


def _split_kw_fallback(keyword: str) -> str | None:
    """多关键词搜出 0 时，挑最有信息量的单 token 重试。

    xhs / bili API 对空格分隔多关键词是 AND 匹配——'人机恋 AI恋爱' 比 '人机恋' 命中率低
    很多。返回最长的非通用 token；只有一个 token 时返 None（无 fallback 可用）。
    """
    parts = [p for p in keyword.split() if p.strip()]
    if len(parts) <= 1:
        return None
    # 挑最长（最具体）的；中文按 char 数算
    parts_sorted = sorted(parts, key=lambda x: len(x), reverse=True)
    return parts_sorted[0]


async def search_xhs(keyword: str) -> str:
    """搜索小红书，按"权威"+"时效"两条线返回结果。

    用 xiaohongshu-cli (`xhs search KEYWORD --sort {popular,latest} --json`)：
    - `--sort popular` 拿"热度高"组——置信度更稳的权威信源
    - `--sort latest` 拿"刚发"组——时效性强的最新动态
    - 并发跑两份，过滤掉"0赞0评 / 1赞0评"这种低质量噪声
    - URL 去重后合并：popular 取前 3，latest 取前 2

    多关键词 0 结果时自动用最长单 token 重试（xhs 对多关键词是严格 AND，命中率低）。
    """
    pop_items, lat_items = await _xhs_search_two_modes(keyword)
    if not pop_items and not lat_items:
        # 多关键词 fallback：拆成单 token 重试一次
        fb = _split_kw_fallback(keyword)
        if fb:
            log.info("xhs search '%s' 0 结果，fallback 重试 '%s'", keyword, fb)
            pop_items, lat_items = await _xhs_search_two_modes(fb)

    if not pop_items and not lat_items:
        return ""

    seen_urls: set[str] = set()
    sections: list[str] = []
    if pop_items:
        lines = _format_xhs_items(pop_items[:3], seen_urls)
        if lines:
            sections.append("【热度高（权威讨论）】\n" + "\n".join(lines))
    if lat_items:
        lines = _format_xhs_items(lat_items[:2], seen_urls)
        if lines:
            sections.append("【近期发布（时效）】\n" + "\n".join(lines))
    if not sections:
        return ""
    return "小红书搜索结果：\n\n" + "\n\n".join(sections)


async def _xhs_search_two_modes(keyword: str) -> tuple[list[dict], list[dict]]:
    """并发跑 popular + latest 两次，各自解析 + 过滤低质量。"""
    pop_raw, lat_raw = await asyncio.gather(
        _run("xhs", "search", keyword, "--sort", "popular", "--json"),
        _run("xhs", "search", keyword, "--sort", "latest", "--json"),
        return_exceptions=False,
    )
    pop_items = _parse_xhs_search_items(pop_raw)
    lat_items = _parse_xhs_search_items(lat_raw)

    # popular 阈值：likes >= 5（明显 traction）；过滤 0赞0评
    pop_items = [it for it in pop_items if _xhs_passes_filter(it, mode="popular")]
    # latest 阈值：likes + comments >= 2，且至少有 1 个 interaction（防 0 赞 0 评）
    lat_items = [it for it in lat_items if _xhs_passes_filter(it, mode="latest")]

    return pop_items, lat_items


def _parse_count_str(s: str | int | None) -> int:
    """xhs 数据里 likes/comments 是字符串，可能是 '12' / '1.2万' / '3千'。统一转 int。"""
    if s is None or s == "":
        return 0
    if isinstance(s, int):
        return max(0, s)
    s = str(s).strip()
    try:
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        if "千" in s:
            return int(float(s.replace("千", "")) * 1000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _xhs_passes_filter(item: dict, *, mode: str) -> bool:
    likes = _parse_count_str(item.get("likes"))
    comments = _parse_count_str(item.get("comments"))
    if mode == "popular":
        # 权威组：要求一定热度——> 5 赞或有评论
        return likes >= 5 or comments >= 1
    # latest：刚发可能赞数还没积累，但完全 0 赞 0 评的明显是无人问津
    return likes + comments >= 2


def _format_xhs_items(items: list[dict], seen_urls: set[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        url = it.get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = it.get("title") or ""
        likes = it.get("likes") or "0"
        comments = it.get("comments") or "0"
        cover_url = it.get("cover_url") or ""
        # 把 likes / comments 都展示——主 LLM 看得到置信度信号
        meta = f"{likes}赞"
        if _parse_count_str(comments) > 0:
            meta += f" {comments}评"
        line = f"「{title}」（{meta}）\n  链接：{url}"
        if cover_url:
            line += f"\n  封面图：{cover_url}"
        out.append(line)
    return out


def _parse_xhs_search_items(raw: str) -> list[dict]:
    """xhs search --json → 结构化 list[dict]，每条 {title, url, likes, comments, cover_url}。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("ok") is False:
        err_msg = (data.get("error") or {}).get("message", "")
        log.info("xhs search 失败：%s", err_msg[:120])
        return []
    items = (data.get("data") or {}).get("items") or []
    out: list[dict] = []
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        card = item.get("note_card") or {}
        title = card.get("display_title") or card.get("title") or ""
        info = card.get("interact_info") or {}
        likes = info.get("liked_count") or "0"
        comments = info.get("comment_count") or "0"
        note_id = item.get("id") or ""
        xsec = item.get("xsec_token") or ""
        if note_id:
            url = f"https://www.xiaohongshu.com/explore/{note_id}"
            if xsec:
                url += f"?xsec_token={xsec}"
        else:
            url = ""
        # 首图——主 LLM 分享时可附图，Telegram 自动渲染预览
        cover_url = ""
        cover = card.get("cover") or {}
        if isinstance(cover, dict):
            cover_url = cover.get("url_default") or cover.get("url") or cover.get("url_pre") or ""
        if not cover_url:
            img_list = card.get("image_list") or []
            if img_list and isinstance(img_list[0], dict):
                cover_url = img_list[0].get("url_default") or img_list[0].get("url") or ""
        if title:
            out.append({
                "title": title,
                "url": url,
                "likes": likes,
                "comments": comments,
                "cover_url": cover_url,
            })
    return out


async def search_web(query: str) -> str:
    """走 Jina Search API（s.jina.ai）。复用 JINA_API_KEY 不引第三方 CLI。

    Jina Search 偶尔慢——curl --max-time 15s + _run timeout=18s（外层兜底比内层多 3s
    避免 race condition：curl 内部 timeout 触发返错误体 vs subprocess 被外层 kill 返空）。
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
    raw = await _run(*args, timeout=18.0)
    if not raw:
        return ""
    # 鉴权/限流失败时返回 JSON 错误体
    if raw.lstrip().startswith("{") and ('"code":4' in raw or 'AuthenticationRequiredError' in raw):
        log.info("jina search 失败：%s", raw[:120])
        return ""
    # Jina 返回 [1] Title: / [1] URL Source: / [1] Description: 机器模板格式
    # 实测主 LLM 会原文复制粘贴（"刚搜出来一点东西——[1] Title: ..."），完全违反 prompt
    # 约束。在结果前加硬性引导段——把"禁止复制"放在最贴近 raw 数据的地方
    header = (
        "【以下是 Jina 搜索原始结果（机器格式，仅供你提取信息用）。"
        "禁止把 `[1] Title:` / `[1] URL Source:` / `[1] Description:` 等字段名"
        "直接出现在你给用户的回复里——挑一条用你自己的话简述（≤30 字）+ 贴链接即可】\n\n"
    )
    return header + raw[:1500]


# OpenAI 兼容 tool schema 给主 LLM 用（native tool_use，2026-05-21 接入）
# 参数名跟函数签名严格对齐：search_xhs/bilibili 是 keyword；search_web/read_github 是 query；read_url 是 url
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Jina 全网搜索。用于通用查询、最近新闻、时事、人物近况、当下事实。"
                "query 必带上下文关键词；涉及'最近/今年'要带年份。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_xhs",
            "description": (
                "小红书笔记搜索。用户聊到小红书 / 笔记 / 攻略 / 测评 / 探店类才用。"
                "keyword 是纯关键词，不要加'小红书'三字。返回 5 条带链接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "搜索关键词（纯主题，不加'小红书'）"}},
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_bilibili",
            "description": (
                "B 站视频搜索。用户聊到 B 站 / up 主 / 视频博主 / 切片 / 投稿才用。"
                "keyword 是纯关键词，不要加'B站'/'bilibili'。返回 5 条带 BV 链接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "搜索关键词（纯主题，不加'B站'）"}},
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": (
                "读单个网页 / 小红书帖子 / 视频元信息。url 是完整 URL。"
                "**通常不用主动调**——用户消息里直接出现 URL 时另有自动路径，这里只在你想读"
                "搜索结果中某一条具体链接时用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 URL，含 http(s)://"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_github",
            "description": "读 GitHub 公开仓库 README + 最近 issue。用户聊到 GitHub 仓库时用。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "owner/repo 或完整 GitHub URL"}},
                "required": ["query"],
            },
        },
    },
]


async def search_bilibili(keyword: str) -> str:
    """搜索 B 站视频，返回前 5 条标题 + UP主 + 链接。

    用 bilibili-cli (`bili search KEYWORD --type video --max 5 --json`)。匿名搜索不需 cookie。
    多关键词 0 结果时自动用最长单 token 重试。
    """
    if not keyword.strip():
        return ""
    raw = await _run("bili", "search", keyword, "--type", "video", "--max", "5", "--json")
    parsed = _parse_bili_search(raw)
    if not parsed:
        fb = _split_kw_fallback(keyword)
        if fb:
            log.info("bili search '%s' 0 结果，fallback 重试 '%s'", keyword, fb)
            raw = await _run("bili", "search", fb, "--type", "video", "--max", "5", "--json")
            parsed = _parse_bili_search(raw)
    if parsed:
        return "B 站搜索结果：\n" + "\n".join(parsed)
    return ""


def _parse_bili_search(raw: str) -> list[str]:
    """bili search --json 输出 → bot 友好 list[str]。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("ok") is False:
        log.info("bili search 失败：%s", str((data.get("error") or {}).get("message", ""))[:120])
        return []
    # bili-cli 返 `data` 是 list（item 直接）
    raw_items = data.get("data") or []
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or []
    items = raw_items if isinstance(raw_items, list) else []
    lines: list[str] = []
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or ""
        up = it.get("author") or it.get("uploader") or ""
        plays = it.get("play") or it.get("view") or it.get("view_count") or ""
        bvid = it.get("bvid") or it.get("id") or ""
        if title:
            line = f"「{title}」 UP主：{up}" + (f"（{plays} 播放）" if plays else "")
            if bvid:
                line += f"\n  https://www.bilibili.com/video/{bvid}"
            lines.append(line)
    return lines


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
        return await _run(*args, timeout=13.0)

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
    html_url = repo.get("html_url") or f"https://github.com/{owner_repo}"
    parts.append(f"仓库：{owner_repo}（⭐{stars}{' · ' + lang if lang else ''}）")
    parts.append(f"链接：{html_url}")
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
    """读单个网页正文。

    按域名路由（跟 _fetch_one_url 一致）——xhs 走 xhs CLI 拿原帖正文 + 图 OCR；
    B 站 / YouTube 走 yt-dlp 拿视频元信息；其他走 Jina Reader 拿网页正文。

    之前只走 Jina Reader——主 LLM search_xhs 后调 read_url 拿到的是网页二手摘要而
    不是帖子正文，体感"看不到内容"（2026-05-24 实测翻车）。
    """
    if not url:
        return ""
    domain = _domain(url)
    # xhs / xhslink → xhs CLI（拿原帖标题 / 作者 / 正文 / 图 OCR）
    if any(xhs in domain for xhs in _XHS_DOMAINS):
        result = await _read_xhs_note(url)
        if result:
            return result
        # xhs CLI 失败时 fallback Jina Reader 至少拿网页摘要
    # 视频域名 → yt-dlp
    if any(v in domain for v in _VIDEO_DOMAINS):
        result = await _read_video(url)
        if result:
            return result
        # yt-dlp 失败 fallback Jina

    # 默认 / fallback：Jina Reader
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
    raw = await _run(*args, timeout=13.0)  # 不再依赖 proxy=True 设 env，env_var 与显式 -x 双用易冲突
    if not raw:
        return ""
    # Jina 失败时返回 JSON 错误体，识别后视为失败让 _fetch_one_url 走 Exa
    if raw.lstrip().startswith("{") and ("AuthenticationRequiredError" in raw or '"code":4' in raw):
        log.info("jina reader 鉴权/限流失败：%s", raw[:120])
        return ""
    return raw[:600]
