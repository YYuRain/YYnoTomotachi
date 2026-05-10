"""获取你自己的 Telegram chat_id。

用法：
  1. 在 .env 里先填 TELEGRAM_BOT_TOKEN（其它可以先不填；脚本只读这一个）。
  2. .venv/bin/python -m scripts.get_chat_id
  3. 在 Telegram 里给这个 bot 发任意一条消息（/start 即可）。
  4. 终端会打印 chat_id，复制到 .env 的 TELEGRAM_ALLOWED_CHAT_ID。
  5. Ctrl+C 退出。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


async def _echo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return
    print(f"\n=== 收到来自 chat_id={chat.id}"
          f"（@{user.username if user else '?'}）的消息 ===")
    print(f"把这一行加到 .env：TELEGRAM_ALLOWED_CHAT_ID={chat.id}\n")
    if update.effective_message:
        await update.effective_message.reply_text(
            f"拿到你的 chat_id 了：{chat.id}\n把它填到 .env 就行。"
        )


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(".env 里先填 TELEGRAM_BOT_TOKEN")
    proxy = os.environ.get("TELEGRAM_PROXY", "")
    builder = ApplicationBuilder().token(token)
    if proxy:
        builder = builder.request(
            HTTPXRequest(proxy=proxy, connect_timeout=20, read_timeout=30)
        ).get_updates_request(
            HTTPXRequest(proxy=proxy, connect_timeout=20, read_timeout=35)
        )
        print(f"(走代理 {proxy})")
    app = builder.build()
    app.add_handler(MessageHandler(filters.ALL, _echo))
    print("bot 已启动，在 Telegram 给它发一条消息。Ctrl+C 退出。")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n退出。")
