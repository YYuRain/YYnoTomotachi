"""测试 bot——为多用户模拟设计。

接一个**独立** Telegram bot token，跟 prod bot 共享同一个 agent / 数据库。
关键差异：身份与 telegram chat_id **解耦**。

工作流：
1. 用户在测试 bot 里 `/become <label>` 选一个虚拟身份（label 任意：alice/bob/数字）
2. label → 一个虚拟 user_id（落在 [9_000_000_000, ...] 范围，避开真 telegram chat_id）
3. 后续消息按这个虚拟身份路由进 `agent.handle_user_message(virtual_uid, ...)`
4. 同一个 telegram 账户能在不同时刻 /become 切到别的 label，对应不同 user_id 的记忆/persona/兴趣

不走邀请码门：测试 bot 永远 auto-activate /become 调用过的身份。
scheduler 也不给 test 用户发 proactive（status='test' 不在 list_active 里）。

启用：在 .env 里设 `TEST_BOT_TOKEN=<另一个 bot 的 token>`。
"""
from __future__ import annotations

import base64
import io
import logging
import zlib
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

# 虚拟 user_id 起点：远高于真实 telegram chat_id（一般 < 10^10）
TEST_UID_BASE = 9_000_000_000

# 当前会话身份：real_chat_id (test bot) → virtual_uid
_identity: dict[int, int] = {}


def _virtual_uid(label: str) -> int:
    """label → 虚拟 user_id。纯数字直接加偏移；其它走 crc32 hash。"""
    label = (label or "").strip()
    if not label:
        return 0
    if label.lstrip("-").isdigit():
        return TEST_UID_BASE + abs(int(label))
    return TEST_UID_BASE + (zlib.crc32(label.encode()) % 100_000_000)


def _make_send_sticker(bot, chat_id: int) -> Callable[[Path], Awaitable[None]]:
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


# =============== 命令 ===============

async def _cmd_become(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    args = ctx.args or []
    if not args:
        cur = _identity.get(chat.id)
        text = (f"当前身份：user_id={cur}\n切换：/become <label>"
                if cur else "还没选身份。/become <label>（label 可以是 alice/bob/数字）")
        await ctx.bot.send_message(chat_id=chat.id, text=text)
        return
    label = " ".join(args)
    uid = _virtual_uid(label)
    if uid == 0:
        await ctx.bot.send_message(chat_id=chat.id, text="label 不能为空")
        return
    _identity[chat.id] = uid
    users.ensure_test_user(uid, label)
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=f"切到 user_id={uid}（label={label}）\n直接发消息即可。/become 别的 label 切别的身份",
    )


async def _cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id, 0)
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(f"虚拟身份：user_id={uid}" if uid else "未设。先 /become <label>")
              + f"\n你的真实 chat_id：{chat.id}",
    )


async def _cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(
            "测试 bot——多用户模拟。\n"
            "/become <label>  选个身份（alice / bob / 1 / 2 都行）\n"
            "/whoami          看当前身份\n"
            "选了身份后直接发消息聊；切别的 label = 切别的人。"
        ),
    )


# =============== 消息 ===============

async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id)
    if not uid:
        await ctx.bot.send_message(chat_id=chat.id, text="先 /become <label> 选个身份")
        return
    msg = update.effective_message
    if msg is None or not msg.text:
        return

    async def send(text: str) -> None:
        await ctx.bot.send_message(chat_id=chat.id, text=text)

    async def typing() -> None:
        await ctx.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    send_sticker = _make_send_sticker(ctx.bot, chat.id)
    log.info("test_bot: real_chat=%d virtual_uid=%d msg=%r",
             chat.id, uid, msg.text[:40])
    await agent.handle_user_message(
        uid, msg.text,
        send=send, typing_action=typing, send_sticker=send_sticker,
    )


async def _on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id)
    if not uid:
        await ctx.bot.send_message(chat_id=chat.id, text="先 /become <label>")
        return
    msg = update.effective_message
    if msg is None or not msg.photo:
        return
    photo = msg.photo[-1]
    try:
        file = await ctx.bot.get_file(photo.file_id)
        bio = io.BytesIO()
        await file.download_to_memory(bio)
        img_bytes = bio.getvalue()
    except Exception as e:
        log.exception("download photo failed: %s", e)
        await ctx.bot.send_message(chat_id=chat.id, text="图没下下来")
        return
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    media_type = "image/jpeg"
    caption = (msg.caption or "").strip()

    async def send(text: str) -> None:
        await ctx.bot.send_message(chat_id=chat.id, text=text)

    async def typing() -> None:
        await ctx.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    send_sticker = _make_send_sticker(ctx.bot, chat.id)
    await agent.handle_user_message(
        uid, caption,
        send=send, typing_action=typing,
        image_b64=img_b64, image_media_type=media_type,
        send_sticker=send_sticker,
    )


# =============== 构造 Application ===============

def build_application() -> Application | None:
    """如果 TEST_BOT_TOKEN 没设，返回 None；调用方判断后跳过启动。"""
    global _app
    if _app is not None:
        return _app
    s = settings()
    if not s.test_bot_token:
        return None
    builder = ApplicationBuilder().token(s.test_bot_token)
    if s.telegram_proxy:
        builder = builder.request(
            HTTPXRequest(proxy=s.telegram_proxy, connect_timeout=20, read_timeout=30)
        ).get_updates_request(
            HTTPXRequest(proxy=s.telegram_proxy, connect_timeout=20, read_timeout=35)
        )
    app = builder.build()
    app.add_handler(CommandHandler("start", _cmd_help))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("become", _cmd_become))
    app.add_handler(CommandHandler("whoami", _cmd_whoami))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), _on_message))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    _app = app
    return app
