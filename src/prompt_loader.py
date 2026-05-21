"""统一 prompt 加载器（2026-05-21 起）。

所有 LLM prompt 从 `prompt/*.md` 读，避免散落在各 .py 三引号串里。
原则：
- loader 只返回 raw 字符串，**不做变量代换**——caller 自己 `.format()` / `.replace()`
- @lru_cache 避免每 turn 读盘；prompt 文件改动需重启 bot 才生效（陪伴场景可接受）
- 文件名 = 模块前缀_名字（如 `memory_extract.md` / `feedback_screen.md`），扁平结构
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"


@lru_cache(maxsize=64)
def load(name: str) -> str:
    """读 `prompt/<name>.md`，返回 raw 内容。

    name: 不带 .md 后缀，如 'memory_extract'、'feedback_screen'、'system_baseline'。
    缺文件时抛 FileNotFoundError——上层启动时就能看到，不会偷偷退化。
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def reload(name: str | None = None) -> None:
    """开发期用——清缓存让下次 load 重读盘。生产不用。"""
    if name is None:
        load.cache_clear()
    else:
        # lru_cache 没有 invalidate-by-key，只能整体清
        load.cache_clear()
