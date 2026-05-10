"""把 LLM 输出变成像真人聊天那样分条发的消息流。

- 先剥掉 markdown 符号（#, **, `, > , ---, 列表符号）。
- 按中文/英文标点切句；过长的再按硬阈值切。
- 每条之间模拟打字：调用 typing_action（如有）+ sleep(随长度缩放)。
- 通过 send 回调（async）发送。回调负责真正的 Telegram send_message。
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Awaitable, Callable, Optional

Send = Callable[[str], Awaitable[None]]
TypingAction = Optional[Callable[[], Awaitable[None]]]

MAX_PIECE_CHARS = 60   # casual 默认；empathy/depth 由调用方覆盖
MIN_SLEEP = 0.4
MAX_SLEEP = 2.2
CHAR_TO_SLEEP = 0.09

_MD_PATTERNS = [
    (re.compile(r"^#{1,6}\s*", re.M), ""),         # #, ##
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),          # **bold**
    (re.compile(r"__(.+?)__"), r"\1"),              # __bold__
    (re.compile(r"`([^`]+)`"), r"\1"),              # `code`
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),       # - item
    (re.compile(r"^\s*\d+\.\s+", re.M), ""),       # 1. item
    (re.compile(r"^\s*>\s?", re.M), ""),           # blockquote
    (re.compile(r"^---+$", re.M), ""),              # hr
]

# 句末标点——在这里切是"换条发"的自然边界
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?…\n])")
# 句中停顿——只在单句超长时作为 fallback 切点
_COMMA_SPLIT_RE = re.compile(r"(?<=[，、,；;])")


def strip_markdown(text: str) -> str:
    t = text.strip()
    for pat, repl in _MD_PATTERNS:
        t = pat.sub(repl, t)
    # 压缩多余空行
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _comma_break(sentence: str, max_chars: int) -> list[str]:
    """超长句子的最后兜底：按逗号切、贪心打包，仍不下就硬切。"""
    parts = [p.strip() for p in _COMMA_SPLIT_RE.split(sentence) if p and p.strip()]
    out: list[str] = []
    cur = ""
    for p in parts:
        if len(p) > max_chars:
            if cur:
                out.append(cur); cur = ""
            while len(p) > max_chars:
                out.append(p[:max_chars])
                p = p[max_chars:]
            if p:
                cur = p
            continue
        if not cur:
            cur = p
        elif len(cur) + len(p) <= max_chars:
            cur = cur + p
        else:
            out.append(cur); cur = p
    if cur:
        out.append(cur)
    return out


def _soft_pieces(
    text: str,
    max_chars: int = MAX_PIECE_CHARS,
    merge_up_to: int | None = None,
) -> list[str]:
    """主逻辑：句号为主边界，只有合并后仍"够短"才合，超长句才动逗号。

    - `max_chars`：硬上限——单段超过才拆；决定是否动逗号。
    - `merge_up_to`：合并阈值——两个相邻短句只有合起来仍 ≤ 此值才合进一条。
      默认 = max_chars（= 旧行为：贪心合并到填满）。
      想让短回复保留自然停顿，就把它设小（如 10–15）。
    """
    if merge_up_to is None:
        merge_up_to = max_chars

    atoms = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s and s.strip()]
    if not atoms:
        return []

    out: list[str] = []
    cur = ""
    for atom in atoms:
        # 单个原子就超长——先 flush 再按逗号拆
        if len(atom) > max_chars:
            if cur:
                out.append(cur); cur = ""
            out.extend(_comma_break(atom, max_chars))
            continue
        if not cur:
            cur = atom
        elif len(cur) + len(atom) <= merge_up_to:
            cur = cur + atom          # 合并后仍够短，并成一条
        else:
            out.append(cur); cur = atom
    if cur:
        out.append(cur)
    return out


def split_for_chat(
    text: str,
    max_chars: int = MAX_PIECE_CHARS,
    merge_up_to: int | None = None,
) -> list[str]:
    return _soft_pieces(strip_markdown(text), max_chars=max_chars, merge_up_to=merge_up_to)


async def deliver(
    text: str,
    send: Send,
    typing_action: TypingAction = None,
    *,
    per_piece_pause: bool = True,
    max_piece_chars: int = MAX_PIECE_CHARS,
    merge_up_to: int | None = None,
) -> None:
    pieces = split_for_chat(text, max_chars=max_piece_chars, merge_up_to=merge_up_to)
    for i, piece in enumerate(pieces):
        if typing_action is not None:
            try:
                await typing_action()
            except Exception:
                pass
        if per_piece_pause and i > 0:
            delay = min(MAX_SLEEP, max(MIN_SLEEP, len(piece) * CHAR_TO_SLEEP))
            delay += random.uniform(-0.2, 0.3)
            await asyncio.sleep(max(0.15, delay))
        await send(piece)
