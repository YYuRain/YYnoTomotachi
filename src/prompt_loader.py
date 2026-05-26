"""统一 prompt 加载器（2026-05-21 起）。

所有 LLM prompt 从 `prompt/*.md` 读，避免散落在各 .py 三引号串里。
原则：
- loader 只返回 raw 字符串，**不做变量代换**——caller 自己 `.format()` / `.replace()`
- 文件默认走 lru_cache 避免每 turn 读盘；prompt 文件改动需重启 bot 才生效
- 文件名 = 模块前缀_名字（如 `memory_extract.md` / `feedback_screen.md`），扁平结构

per-user 覆写（2026-05-26 起）：
- `load(name, user_id=...)` 优先返该用户在 SQLite 中的整份覆写（user_prompt_overrides 表）
- 进程内有一份 `_user_cache: (uid, name) → str | None`，避免每 turn 查库
- admin UI 改完调 `invalidate_user(uid, name)` 让下一 turn 立刻拿到新内容
- 不传 user_id 还是返默认（兼容旧调用方）
"""
from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"

# (uid, name) → 已查过的 user override 内容（None = 已查过，确认没 override，也缓存以避免重复查）
_user_cache: dict[tuple[int, str], str | None] = {}
_user_cache_lock = threading.Lock()


@lru_cache(maxsize=64)
def _load_default(name: str) -> str:
    """读 `prompt/<name>.md` 默认文件。lru_cache 避免每 turn 读盘。"""
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def load(name: str, user_id: int | None = None) -> str:
    """读 prompt。

    name: 不带 .md 后缀，如 'memory_extract'、'feedback_screen'、'system_baseline'。
    user_id: 传了就先查 user_prompt_overrides 表，有就用 user 版，没有走默认文件。
             不传 = 一定走默认（兼容旧 caller）。

    缺默认文件时抛 FileNotFoundError——上层启动时就能看到，不会偷偷退化。
    """
    if user_id is None:
        return _load_default(name)
    # 先看进程内 cache
    with _user_cache_lock:
        if (user_id, name) in _user_cache:
            cached = _user_cache[(user_id, name)]
            if cached is not None:
                return cached
            return _load_default(name)
    # cache miss → 查 SQLite
    try:
        from . import storage
        content = storage.get_user_prompt_override(user_id, name)
    except Exception:
        # 库挂了不能阻塞主对话流——退化到默认
        content = None
    with _user_cache_lock:
        _user_cache[(user_id, name)] = content
    return content if content is not None else _load_default(name)


def invalidate_user(user_id: int, name: str | None = None) -> None:
    """清掉某 user 的 cache。改完 / 删完 override 后调，让下一 turn 立刻生效。

    name=None 清该 uid 的所有条目；name 给定只清那一条。
    """
    with _user_cache_lock:
        if name is None:
            for k in list(_user_cache):
                if k[0] == user_id:
                    del _user_cache[k]
        else:
            _user_cache.pop((user_id, name), None)


def list_default_prompt_names() -> list[str]:
    """枚举 `prompt/` 目录下所有 .md 文件名（不带后缀），按字母序。

    admin UI「Prompt 文件」section 的列表数据源。
    """
    return sorted(p.stem for p in PROMPT_DIR.glob("*.md"))


def reload(name: str | None = None) -> None:
    """开发期用——清默认文件 cache 让下次 load 重读盘。生产不用。

    注意：不会清 user_cache（那是另一种 cache，要 invalidate_user）。
    """
    if name is None:
        _load_default.cache_clear()
    else:
        # lru_cache 没有 invalidate-by-key，只能整体清
        _load_default.cache_clear()
