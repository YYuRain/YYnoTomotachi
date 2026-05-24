"""Vision LLM 工具——给图做 OCR + 简短画面描述。

主用途：xhs 帖子大量是文字截图（穿搭笔记 / 教程 / 语录 / 日记），单纯返图链给主 LLM
看不见内容；OCR 出来文字 + 一句画面描述，主 LLM 才能"理解"图说了啥。

设计：
- 下载图 → base64 → 走 vision LLM（aux tier 即可，OCR 不需要顶级模型）
- silent 失败：xhs cdn 偶尔 403 / 图太大 / LLM 超时——返空字符串让上游照旧
- 每张图 timeout 25s（含下载 + LLM）；并发用上层管
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from . import llm

log = logging.getLogger(__name__)

_OCR_PROMPT = (
    "把这张图里的所有文字尽量完整 OCR 出来——保留原排版的换行/段落感。"
    "如果图里没文字（纯照片/穿搭/食物等），就用 ≤40 字描述画面（穿什么 / 在哪 / 谁 / 在干嘛 / 给人的感觉）。"
    "不要加'图片显示...'/'这张图是...'前缀，直接输出文字内容或描述。"
    "不要加 markdown 标题或列表符号。"
)

_DOWNLOAD_TIMEOUT = 10.0
_TOTAL_TIMEOUT = 25.0


async def _download_image(url: str) -> tuple[bytes, str] | None:
    """下载图片返 (bytes, media_type)。失败返 None。"""
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, trust_env=False) as cli:
            # xhs CDN 不强 referer，直接 GET 即可
            resp = await cli.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log.info("vision download %s: HTTP %d", url[:80], resp.status_code)
            return None
        media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not media_type.startswith("image/"):
            media_type = "image/jpeg"
        data = resp.content
        if not data or len(data) > 4 * 1024 * 1024:  # 4MB 上限——太大 base64 后上下文吃不消
            log.info("vision skip %s: size %d bytes", url[:80], len(data))
            return None
        return data, media_type
    except Exception as e:
        log.info("vision download err %s: %s", url[:80], e)
        return None


async def ocr_image_url(url: str) -> str:
    """对单张图 URL 跑 OCR + 描述，返回纯文本。失败返空。"""
    if not url:
        return ""
    try:
        result = await asyncio.wait_for(_ocr_inner(url), timeout=_TOTAL_TIMEOUT)
        return result
    except asyncio.TimeoutError:
        log.info("vision timeout: %s", url[:80])
        return ""
    except Exception as e:
        log.info("vision err: %s", e)
        return ""


async def _ocr_inner(url: str) -> str:
    downloaded = await _download_image(url)
    if downloaded is None:
        return ""
    data, media_type = downloaded
    b64 = base64.standard_b64encode(data).decode("ascii")

    # 走 anthropic 风格 image block（src/openrouter._normalize_messages 会自动转
    # OpenAI 风格——agent.py 处理 user 发图也是这条路径）
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {"type": "text", "text": _OCR_PROMPT},
            ],
        }
    ]
    text = await llm.chat(messages, temperature=0.1, max_tokens=400, tier="aux")
    return (text or "").strip()


async def ocr_image_urls(urls: list[str], *, max_concurrent: int = 3) -> list[str]:
    """对一批图 URL 并发 OCR，返回同序文本列表。失败位置是空字符串。"""
    if not urls:
        return []
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(u: str) -> str:
        async with sem:
            return await ocr_image_url(u)

    return await asyncio.gather(*(_one(u) for u in urls), return_exceptions=False)
