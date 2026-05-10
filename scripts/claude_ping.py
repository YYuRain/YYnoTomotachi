"""快速 ping Claude key 是否可用。只测 messages API + 剥 <think> 无关逻辑。

用法：
  .venv/bin/python -m scripts.claude_ping
"""
from __future__ import annotations

import asyncio
import os


async def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "ping")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    # 清 HTTP(S)_PROXY，避免 Clash 劫持 Anthropic 调用
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.anthropic.com,api.minimaxi.com"

    from src.config import settings
    from src import llm

    s = settings()
    print(f"provider={s.llm_provider}  model={s.anthropic_model}")
    if not s.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY 为空，先在 .env 填上再跑")
    if s.llm_provider != "anthropic":
        print("提示：当前 LLM_PROVIDER != anthropic；这只是 ping，临时走 anthropic 分支。")
        # 直接用内部函数
        out = await llm._anthropic_chat(
            [
                {"role": "system", "content": "你只回两个字：收到。"},
                {"role": "user", "content": "ping"},
            ],
            temperature=0.1,
            max_tokens=32,
            model=None,
        )
    else:
        out = await llm.chat(
            [
                {"role": "system", "content": "你只回两个字：收到。"},
                {"role": "user", "content": "ping"},
            ],
            temperature=0.1,
            max_tokens=32,
        )
    print(f"回复: {out!r}")
    await llm.aclose()
    print("✅ Claude key 可用")


if __name__ == "__main__":
    asyncio.run(main())
