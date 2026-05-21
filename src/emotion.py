"""情绪/谈话模式识别。

不做"你开心你难过"的情感分类。做的是**接下来该怎么聊**的三档判断：

- `casual`：日常闲聊，默认。按 system prompt 的网友风格走。
- `empathy`：用户在走心——在表达脆弱、诉苦、失落、认真的喜悦、重要人事。
              bot 需要承接情绪，不抖机灵、不跳话题、允许慢半拍。
- `depth`：用户在认真探讨——在请教、在分析、在提一个具体问题想听你看法。
              bot 可以说长一点、给可参考的观点、必要时反问澄清。

副带 `valence` / `arousal` 仅作参考，不强制下游用。
`hint` 是一句从当前消息里提炼的"对方此刻核心想说的"，用于 prompt 侧稳住主题。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from . import llm

log = logging.getLogger(__name__)

Mode = Literal["casual", "empathy", "depth", "interest"]


@dataclass(frozen=True)
class EmotionSignal:
    mode: Mode
    valence: float = 0.0   # -1..1（负=难过/生气，正=兴奋/开心）
    arousal: float = 0.0   #  0..1（越高情绪越强）
    hint: str = ""         # 一句话总结"对方这会儿真的想说什么"


def _detect_system() -> str:
    """从 prompt/emotion_detect.md 加载（2026-05-21 抽出）。"""
    from . import prompt_loader
    return prompt_loader.load("emotion_detect")


_FALLBACK = EmotionSignal(mode="casual")


async def detect(
    user_text: str,
    recent: Optional[list[dict]] = None,
) -> Optional[EmotionSignal]:
    """失败/超时/不确定 → 返回 None（= casual），下游 bypass。"""
    if not user_text or not user_text.strip():
        return None

    ctx_lines: list[str] = []
    if recent:
        for m in recent[-6:]:
            role = "user" if m.get("role") == "user" else "你"
            content = (m.get("content") or "").replace("\n", " ")[:80]
            ctx_lines.append(f"{role}: {content}")
    context_block = "\n".join(ctx_lines) if ctx_lines else "（无近况）"

    prompt = (
        f"最近对话：\n{context_block}\n\n"
        f"用户最新消息：\n{user_text}\n\n"
        f"判断。"
    )

    try:
        data = await llm.chat_json(
            [
                {"role": "system", "content": _detect_system()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
        )
    except Exception as e:
        log.debug("emotion detect failed: %s", e)
        return None

    mode = data.get("mode")
    if mode not in ("casual", "empathy", "depth", "interest"):
        return None

    def _f(key: str, default: float = 0.0) -> float:
        try:
            return max(-1.0, min(1.0, float(data.get(key, default))))
        except (TypeError, ValueError):
            return default

    sig = EmotionSignal(
        mode=mode,  # type: ignore[arg-type]
        valence=_f("valence"),
        arousal=max(0.0, min(1.0, _f("arousal"))),
        hint=str(data.get("hint") or "")[:40],
    )
    log.info("mode=%s val=%+.1f aro=%.1f hint=%r", sig.mode, sig.valence, sig.arousal, sig.hint)
    return sig
