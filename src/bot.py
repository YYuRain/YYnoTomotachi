"""Telegram 接线。单用户白名单。"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes, MessageHandler, filters,
)
from telegram.request import HTTPXRequest

from . import agent
from .config import settings


def _make_send_sticker(bot, chat_id: int) -> Callable[[Path], Awaitable[None]]:
    """根据文件扩展名挑合适的 telegram API：webp→sticker、gif→animation、其他→photo。"""
    async def send_sticker(path: Path) -> None:
        suffix = path.suffix.lower()
        with open(path, "rb") as f:
            if suffix == ".webp":
                await bot.send_sticker(chat_id=chat_id, sticker=f)
            elif suffix == ".gif":
                await bot.send_animation(chat_id=chat_id, animation=f)
            else:
                await bot.send_photo(chat_id=chat_id, photo=f)
    return send_sticker

log = logging.getLogger(__name__)

_app: Application | None = None


def _whitelisted(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.id == settings().telegram_allowed_chat_id


def _format_reply_quote(reply_to_msg) -> str:
    """把 msg.reply_to_message 翻译成 `[对方在引用...]` 前缀。
    单聊场景下发送者只有两个——bot 自己 (is_bot=True) 或对方用户。
    """
    if reply_to_msg is None:
        return ""
    quoted = reply_to_msg.text or reply_to_msg.caption or ""
    if not quoted.strip():
        # 引用的是非文本媒体
        if reply_to_msg.photo:
            quoted = "（一张图）"
        elif reply_to_msg.sticker:
            emoji = reply_to_msg.sticker.emoji or ""
            quoted = f"（一个表情包 {emoji}）".strip()
        elif reply_to_msg.voice:
            quoted = "（一条语音）"
        elif reply_to_msg.video or reply_to_msg.animation:
            quoted = "（一个视频）"
        else:
            return ""
    quoted = quoted.strip().replace("\n", " ")
    if len(quoted) > 200:
        quoted = quoted[:200] + "..."
    sender = reply_to_msg.from_user
    if sender and getattr(sender, "is_bot", False):
        return f"[对方在引用你之前说的：「{quoted}」]"
    return f"[对方在引用 ta 自己之前说的：「{quoted}」]"


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _whitelisted(update):
        log.info("ignored chat_id=%s", update.effective_chat.id if update.effective_chat else None)
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    chat_id = update.effective_chat.id

    async def send(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)

    async def typing() -> None:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    send_sticker = _make_send_sticker(context.bot, chat_id)

    # 处理"回复某条消息"——把被引用的内容包成 [对方在引用...] 前缀塞进 text
    quote = _format_reply_quote(msg.reply_to_message)
    user_text = f"{quote}\n{msg.text}" if quote else msg.text

    await agent.handle_user_message(user_text, send=send, typing_action=typing, send_sticker=send_sticker)


async def _on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """图片消息：下载最大尺寸 → base64 → 交给 agent，让主 LLM 走 vision 路径直接看图。"""
    if not _whitelisted(update):
        return
    msg = update.effective_message
    if msg is None or not msg.photo:
        return
    chat_id = update.effective_chat.id

    photo = msg.photo[-1]  # 数组最后一项是最大尺寸
    try:
        file = await context.bot.get_file(photo.file_id)
        bio = io.BytesIO()
        await file.download_to_memory(bio)
        img_bytes = bio.getvalue()
    except Exception as e:
        log.exception("download photo failed: %s", e)
        await context.bot.send_message(chat_id=chat_id, text="图没下下来（网络？）")
        return

    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    # Telegram photo 默认输出 jpeg；用 magic bytes 校一下也行，先简化
    media_type = "image/jpeg"
    caption = (msg.caption or "").strip()

    async def send(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)

    async def typing() -> None:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    send_sticker = _make_send_sticker(context.bot, chat_id)

    # 同样处理"回复某条消息"
    quote = _format_reply_quote(msg.reply_to_message)
    user_text = f"{quote}\n{caption}".strip() if quote else caption

    log.info("photo received, %d bytes, caption=%r, has_quote=%s",
             len(img_bytes), caption[:40], bool(quote))
    await agent.handle_user_message(
        user_text, send=send, typing_action=typing,
        image_b64=img_b64, image_media_type=media_type,
        send_sticker=send_sticker,
    )


def build_application() -> Application:
    global _app
    if _app is not None:
        return _app
    s = settings()
    builder = ApplicationBuilder().token(s.telegram_bot_token)
    if s.telegram_proxy:
        # 国内环境需要经代理访问 api.telegram.org
        builder = builder.request(
            HTTPXRequest(proxy=s.telegram_proxy, connect_timeout=20, read_timeout=30)
        ).get_updates_request(
            HTTPXRequest(proxy=s.telegram_proxy, connect_timeout=20, read_timeout=35)
        )
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), _on_message))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    _app = app
    return app


def make_send_and_typing() -> tuple[
    Callable[[str], Awaitable[None]], Callable[[], Awaitable[None]]
]:
    """供 scheduler 的 proactive_job 使用。"""
    s = settings()
    app = build_application()

    async def send(text: str) -> None:
        await app.bot.send_message(chat_id=s.telegram_allowed_chat_id, text=text)

    async def typing() -> None:
        await app.bot.send_chat_action(
            chat_id=s.telegram_allowed_chat_id, action=ChatAction.TYPING
        )

    return send, typing
