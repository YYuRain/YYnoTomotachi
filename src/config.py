from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# override=True：.env 是本项目的 single source of truth，
# 不被 shell 里的 ANTHROPIC_MODEL / BASE_URL 之类的 export 覆盖。
load_dotenv(ROOT / ".env", override=True)


def _req(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"环境变量缺失：{key}（检查 .env）")
    return v


def _opt(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    admin_chat_id: int            # 多用户化后这个 ID 是 admin（生成邀请码、看 /users 等）
    telegram_proxy: str

    minimax_api_key: str
    minimax_group_id: str
    minimax_base_url: str
    minimax_chat_model: str
    minimax_embed_model: str

    llm_provider: str          # "anthropic" | "minimax"
    anthropic_api_key: str
    anthropic_model: str       # 主输出（主聊天 / 主动开场）
    anthropic_model_aux: str   # 辅助判断（情绪判档 / 话题抽取）
    anthropic_base_url: str    # 空 = SDK 默认 (api.anthropic.com)

    memu_metadata_provider: str
    memu_db_url: str
    memu_chat_model: str       # memU 内部抽取/分类的模型（设了就走 OpenRouter；空则回退 MiniMax via shim）

    embed_server_host: str
    embed_server_port: int
    embed_model_name: str

    llm_proxy_host: str
    llm_proxy_port: int

    # Jina Reader（read_url 用）。空则匿名调用，但 IP 被风控时会 401，建议注册免费 key
    jina_api_key: str

    # OpenRouter
    openrouter_api_key: str
    openrouter_base_url: str
    openrouter_model: str       # LLM_PROVIDER=openrouter 时主输出
    openrouter_model_aux: str   # LLM_PROVIDER=openrouter 时辅助 tier；空 → 同 main
    eval_models: str            # 仅 scripts/eval_models 使用

    proactive_idle_threshold_sec: int
    interest_decay_tau_hours: float

    app_db_path: Path
    root: Path
    system_prompt_path: Path


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=_req("TELEGRAM_BOT_TOKEN"),
        # 兼容旧名 TELEGRAM_ALLOWED_CHAT_ID：迁移期保留，新部署用 ADMIN_CHAT_ID
        admin_chat_id=int(_opt("ADMIN_CHAT_ID", "") or _req("TELEGRAM_ALLOWED_CHAT_ID")),
        telegram_proxy=_opt("TELEGRAM_PROXY", ""),
        minimax_api_key=_req("MINIMAX_API_KEY"),
        minimax_group_id=_opt("MINIMAX_GROUP_ID"),
        minimax_base_url=_opt("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/"),
        minimax_chat_model=_opt("MINIMAX_CHAT_MODEL", "MiniMax-M2"),
        minimax_embed_model=_opt("MINIMAX_EMBED_MODEL", "embo-01"),
        llm_provider=_opt("LLM_PROVIDER", "minimax").lower(),
        anthropic_api_key=_opt("ANTHROPIC_API_KEY", ""),
        anthropic_model=_opt("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        anthropic_model_aux=_opt("ANTHROPIC_MODEL_AUX", "claude-sonnet-4-6"),
        anthropic_base_url=_opt("ANTHROPIC_BASE_URL", "").rstrip("/"),
        memu_metadata_provider=_opt("MEMU_METADATA_PROVIDER", "inmemory"),
        memu_db_url=_opt("MEMU_DB_URL", ""),
        memu_chat_model=_opt("MEMU_CHAT_MODEL", ""),
        embed_server_host=_opt("EMBED_SERVER_HOST", "127.0.0.1"),
        embed_server_port=int(_opt("EMBED_SERVER_PORT", "18080")),
        embed_model_name=_opt("EMBED_MODEL_NAME", "BAAI/bge-small-zh-v1.5"),
        llm_proxy_host=_opt("LLM_PROXY_HOST", "127.0.0.1"),
        llm_proxy_port=int(_opt("LLM_PROXY_PORT", "18082")),
        jina_api_key=_opt("JINA_API_KEY", ""),
        openrouter_api_key=_opt("OPENROUTER_API_KEY", ""),
        openrouter_base_url=_opt("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        openrouter_model=_opt("OPENROUTER_MODEL", ""),
        openrouter_model_aux=_opt("OPENROUTER_MODEL_AUX", ""),
        eval_models=_opt("EVAL_MODELS", ""),
        proactive_idle_threshold_sec=int(_opt("PROACTIVE_IDLE_THRESHOLD_SEC", "21600")),
        interest_decay_tau_hours=float(_opt("INTEREST_DECAY_TAU_HOURS", "48")),
        app_db_path=(ROOT / _opt("APP_DB_PATH", "./data/app.sqlite")).resolve(),
        root=ROOT,
        system_prompt_path=ROOT / "System Prompt v0.0.1.md",
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
        _settings.app_db_path.parent.mkdir(parents=True, exist_ok=True)
    return _settings
