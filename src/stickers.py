"""表情包：本地 `data/stickers/` 目录扫文件，文件名（去后缀）当 tag。

用法约定：
- 用户/你自己往 `data/stickers/` 放图，文件名是 tag。例：`无奈.jpg`、`大笑.png`、`加油.gif`。
- AI 在回复里写 `[sticker:无奈]` 内联标记，rhythm/agent 解析后调 send_sticker 发图。
- 空库时 system prompt 不会提及 sticker 机制 → AI 也不会输出标记 → 零侵入。

支持格式：jpg / jpeg / png / gif / webp。
- jpg/png → send_photo
- gif → send_animation
- webp → send_sticker（telegram 静态贴图）
- 其他扩展名忽略。

匹配策略：
- 优先精确匹配（大小写不敏感）
- 退而求其次，子串包含（双向）
- 都不命中：丢弃这个标记（不报错）
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"\[sticker:([^\]]+)\]")
_SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_index: dict[str, Path] | None = None


def _stickers_dir() -> Path:
    d = settings().root / "data" / "stickers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_index() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for f in _stickers_dir().iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in _SUPPORTED_EXT:
            continue
        tag = f.stem  # 去后缀的文件名作为 tag
        out[tag] = f
    return out


def reload() -> int:
    """重扫目录刷新 index。返回当前可用 tag 数。"""
    global _index
    _index = _build_index()
    log.info("sticker index loaded: %d items", len(_index))
    return len(_index)


def index() -> dict[str, Path]:
    if _index is None:
        reload()
    return _index or {}


def available_tags() -> list[str]:
    return sorted(index().keys())


def find(tag: str) -> Path | None:
    idx = index()
    if not idx:
        return None
    if tag in idx:
        return idx[tag]
    tag_l = tag.lower().strip()
    for k, v in idx.items():
        if k.lower() == tag_l:
            return v
    for k, v in idx.items():
        if tag_l in k.lower() or k.lower() in tag_l:
            return v
    return None


def parse_message(text: str) -> list[tuple[str, str | Path]]:
    """切成 [(kind, payload), ...]：
    - ("text", "...")
    - ("sticker", Path)
    找不到对应 tag 的标记 **直接丢弃**——不留空文本、不报错。
    """
    parts: list[tuple[str, str | Path]] = []
    last = 0
    for m in _TAG_RE.finditer(text):
        if m.start() > last:
            chunk = text[last:m.start()].strip()
            if chunk:
                parts.append(("text", chunk))
        path = find(m.group(1))
        if path is not None:
            parts.append(("sticker", path))
        else:
            log.debug("sticker tag not found, dropped: %r", m.group(1))
        last = m.end()
    if last < len(text):
        chunk = text[last:].strip()
        if chunk:
            parts.append(("text", chunk))
    return parts
