"""Telegram 接线（多用户版）。

入口：
- 命令：/start [<code>]、/myid、/invite [n]（admin）、/users（admin）
- 文本/图片：先校验 `users.is_active(chat_id)`；未激活的 silent drop，进 agent.handle_user_message(user_id=chat_id, ...)
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters,
)
from telegram.request import HTTPXRequest

from . import agent, users
from .config import settings


log = logging.getLogger(__name__)
_app: Application | None = None


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


def _format_reply_quote(reply_to_msg) -> str:
    """把 msg.reply_to_message 翻译成 `[对方在引用...]` 前缀。"""
    if reply_to_msg is None:
        return ""
    quoted = reply_to_msg.text or reply_to_msg.caption or ""
    if not quoted.strip():
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


# =============== 命令 handlers ===============

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start [<code>]`——未激活：要邀请码；已激活：欢迎/不打扰。"""
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    args = context.args or []

    if users.is_active(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="你已经在了，正常说话就行")
        return

    if not args:
        await context.bot.send_message(
            chat_id=chat_id,
            text="嗨。这是个邀请制的小 bot，需要邀请码才能用：\n/start <你的邀请码>",
        )
        return

    err = users.redeem(args[0], chat_id)
    if err:
        await context.bot.send_message(chat_id=chat_id, text=f"邀请码不对：{err}")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text="搞定。\n你可以直接发消息聊了——不用再打 / 命令。",
    )


async def _cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(chat_id=chat.id, text=f"你的 chat_id：{chat.id}")


async def _cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or not users.is_admin(chat.id):
        return
    args = context.args or []
    n = 1
    if args:
        try:
            n = max(1, min(20, int(args[0])))
        except ValueError:
            n = 1
    codes = users.generate_invites(chat.id, n)
    if not codes:
        await context.bot.send_message(chat_id=chat.id, text="生成失败")
        return
    body = "\n".join(codes)
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"生成了 {len(codes)} 个邀请码：\n{body}\n\n（让对方发  /start <code>  激活）",
    )


async def _cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """admin 用——返回当前 cloudflared 临时 admin UI URL（每次 cloudflared 重启会变）。"""
    chat = update.effective_chat
    if chat is None or not users.is_admin(chat.id):
        return
    import os, re
    log_path = "/shared/cf.log"
    url = ""
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
            if m:
                url = m.group(0)
        except Exception:
            pass
    if not url:
        await context.bot.send_message(
            chat_id=chat.id,
            text="cloudflared 还没拿到 URL（可能刚重启），过 30 秒再试 /admin",
        )
        return
    user_env = os.environ.get("ADMIN_UI_USER", "")
    msg = f"admin UI: {url}\n\n首次访问会提示输用户名/密码（用户名：{user_env or '设的那个'}）"
    await context.bot.send_message(chat_id=chat.id, text=msg)


async def _cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or not users.is_admin(chat.id):
        return
    rows = users.list_users_with_meta()
    if not rows:
        await context.bot.send_message(chat_id=chat.id, text="还没有任何用户")
        return
    lines = [f"共 {len(rows)} 人："]
    for r in rows[:50]:
        marker = "👑" if r["chat_id"] == settings().admin_chat_id else "·"
        lines.append(f"{marker} {r['chat_id']} ({r['status']}) {r['note']}".strip())
    await context.bot.send_message(chat_id=chat.id, text="\n".join(lines))


# =============== 业务消息 handlers ===============

async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    if not users.is_active(chat_id):
        log.info("dropped (not active): chat_id=%d", chat_id)
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return

    async def send(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)

    async def typing() -> None:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    send_sticker = _make_send_sticker(context.bot, chat_id)

    quote = _format_reply_quote(msg.reply_to_message)
    user_text = f"{quote}\n{msg.text}" if quote else msg.text

    await agent.handle_user_message(
        chat_id, user_text,
        send=send, typing_action=typing, send_sticker=send_sticker,
    )


async def _on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    if not users.is_active(chat_id):
        log.info("dropped photo (not active): chat_id=%d", chat_id)
        return
    msg = update.effective_message
    if msg is None or not msg.photo:
        return

    photo = msg.photo[-1]
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
    media_type = "image/jpeg"
    caption = (msg.caption or "").strip()

    async def send(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)

    async def typing() -> None:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    send_sticker = _make_send_sticker(context.bot, chat_id)
    quote = _format_reply_quote(msg.reply_to_message)
    user_text = f"{quote}\n{caption}".strip() if quote else caption

    log.info("photo received uid=%d, %d bytes, caption=%r, has_quote=%s",
             chat_id, len(img_bytes), caption[:40], bool(quote))
    await agent.handle_user_message(
        chat_id, user_text,
        send=send, typing_action=typing,
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
        builder = builder.request(
            HTTPXRequest(proxy=s.telegram_proxy, connect_timeout=20, read_timeout=30)
        ).get_updates_request(
            HTTPXRequest(proxy=s.telegram_proxy, connect_timeout=20, read_timeout=35)
        )
    app = builder.build()
    # 命令在前
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("myid", _cmd_myid))
    app.add_handler(CommandHandler("invite", _cmd_invite))
    app.add_handler(CommandHandler("users", _cmd_users))
    app.add_handler(CommandHandler("admin", _cmd_admin))
    # 业务
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), _on_message))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    _app = app
    return app


def make_send_and_typing(chat_id: int) -> tuple[
    Callable[[str], Awaitable[None]], Callable[[], Awaitable[None]]
]:
    """供 scheduler 的 proactive_job 使用——按目标用户构造 send/typing 闭包。"""
    app = build_application()

    async def send(text: str) -> None:
        await app.bot.send_message(chat_id=chat_id, text=text)

    async def typing() -> None:
        await app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    return send, typing
