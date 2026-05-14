"""测试 bot——多用户模拟 + 完整邀请码流程 + 清盘。

接一个**独立** Telegram bot token，跟 prod bot 共享同一个 agent / 数据库。
关键差异：身份与 telegram chat_id **解耦**——`/become <label>` 选虚拟 user_id。
注册走真实流程：选完身份后必须 `/start <邀请码>` 才能聊天。
`/clear` 把当前虚拟身份的所有数据清空（SQLite + memU postgres + 进程内存），
同时把 redeem 过的邀请码释放回去——这样可以反复测"邀请→激活→聊天→清盘"完整闭环。

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
from .rhythm import deliver

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

async def _cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(
            "测试 bot——多用户邀请码流程模拟\n\n"
            "/become <label>     选个虚拟身份（alice / bob / 数字 都行）\n"
            "/start <邀请码>      用当前虚拟身份激活（找 admin /invite 拿码）\n"
            "/whoami             看当前虚拟 + 真实 chat_id\n"
            "/clear              清空当前虚拟身份的全部数据，邀请码归还\n"
            "/help               这条帮助\n\n"
            "流程：/become alice → /start <code> → 直接聊 → 想换身份 /become bob → 走一遍\n"
            "想测重新邀请：/clear 后重新 /start 就行"
        ),
    )


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
    if users.is_active(uid):
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=f"切到 user_id={uid}（label={label}）\n这个身份已激活，直接说话",
        )
    else:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=(
                f"切到 user_id={uid}（label={label}）\n"
                f"这个身份还没激活——发 /start <邀请码> 注册（找 admin /invite 拿码）"
            ),
        )


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """走真实注册流程——但 redeem 用 _identity 里的虚拟 uid 而非真实 chat.id。"""
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id)
    if not uid:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text="先 /become <label> 选个虚拟身份再 /start <邀请码>",
        )
        return
    args = ctx.args or []
    if users.is_active(uid):
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=f"user_id={uid} 已经激活了，直接发消息聊就行",
        )
        return
    if not args:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=f"用 /start <邀请码> 激活 user_id={uid}（找 admin /invite 拿码）",
        )
        return
    err = users.redeem(args[0], uid)
    if err:
        await ctx.bot.send_message(chat_id=chat.id, text=f"邀请码不对：{err}")
        return
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=f"搞定，user_id={uid} 注册成功。/memory 看 webUI",
    )
    # AI 给新用户的第一条招呼
    try:
        text = await agent.generate_welcome(uid)
        if text:
            async def _send(t: str) -> None:
                await ctx.bot.send_message(chat_id=chat.id, text=t)

            async def _typing() -> None:
                await ctx.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

            await deliver(text, _send, _typing, max_piece_chars=60, merge_up_to=12)
    except Exception as e:
        log.exception("welcome generation err: %s", e)


async def _cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id, 0)
    if uid:
        active = users.is_active(uid)
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=(
                f"虚拟身份：user_id={uid}\n"
                f"激活状态：{'✓ active' if active else '× 未激活，发 /start <code>'}\n"
                f"真实 chat_id：{chat.id}"
            ),
        )
    else:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=f"还没选身份。/become <label> 开始\n你的真实 chat_id：{chat.id}",
        )


async def _cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """返回当前虚拟身份的 webUI 一键登录链接。"""
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id)
    if not uid or not users.is_active(uid):
        await ctx.bot.send_message(
            chat_id=chat.id,
            text="先 /become <label> + /start <邀请码> 激活，再 /memory 拿 webUI",
        )
        return
    import os, re
    log_path = "/shared/cf.log"
    url = ""
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
            if matches:
                url = matches[-1]
        except Exception:
            pass
    if not url:
        await ctx.bot.send_message(chat_id=chat.id, text="webUI 还没就绪，过 30 秒再试")
        return
    token = users.make_session_token(uid, is_admin=False)
    login_url = f"{url}/login-by-token?t={token}"
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(
            f"{login_url}\n\n"
            f"点开直接进 user_id={uid} 视图（10 分钟内有效，登进去保 7 天）"
        ),
    )


async def _cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """清空当前虚拟身份的所有数据。"""
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id)
    if not uid:
        await ctx.bot.send_message(chat_id=chat.id, text="还没选身份。/become <label>")
        return
    counts = users.wipe_user(uid)
    summary = "\n".join(f"  {k}: {v}" for k, v in counts.items() if v)
    await ctx.bot.send_message(
        chat_id=chat.id,
        text=(
            f"清空 user_id={uid} 完成：\n{summary or '  （没有数据）'}\n\n"
            f"邀请码已释放可以重用。重新 /start <code> 即可再走一遍流程"
        ),
    )


# =============== 消息 ===============

async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    uid = _identity.get(chat.id)
    if not uid:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text="先 /become <label> 选个虚拟身份；/help 看流程",
        )
        return
    if not users.is_active(uid):
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=f"user_id={uid} 还没激活，发 /start <邀请码>",
        )
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
    if not users.is_active(uid):
        await ctx.bot.send_message(chat_id=chat.id, text=f"user_id={uid} 还没激活，/start <code>")
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

def real_chat_id_for(virtual_uid: int) -> int | None:
    """虚拟 uid → 该会话当前的 real chat_id（test bot 用）；未找到返 None。
    scheduler 用这个判断要不要给虚拟用户发 proactive，以及发到哪里。"""
    for real, virt in _identity.items():
        if virt == virtual_uid:
            return real
    return None


def make_send_and_typing(chat_id: int) -> tuple[
    Callable[[str], Awaitable[None]], Callable[[], Awaitable[None]]
]:
    """供 scheduler 给 test bot 用户发 proactive 用——按 real chat_id 走 test bot Application。"""
    app = build_application()
    if app is None:
        raise RuntimeError("test bot not enabled (TEST_BOT_TOKEN missing)")

    async def send(text: str) -> None:
        await app.bot.send_message(chat_id=chat_id, text=text)

    async def typing() -> None:
        await app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    return send, typing


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
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("become", _cmd_become))
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("whoami", _cmd_whoami))
    app.add_handler(CommandHandler("clear", _cmd_clear))
    app.add_handler(CommandHandler("memory", _cmd_memory))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), _on_message))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    _app = app
    return app
