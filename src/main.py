"""入口：同一个 asyncio loop 里跑 Telegram polling + APScheduler。"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from . import bot, embed_server, llm, llm_proxy, scheduler, storage
from .audit_log import audit
from .config import settings


def _purge_proxy_env() -> None:
    """避免系统/会话级 HTTP(S)_PROXY 被 memU 内部 OpenAI SDK 拾取，
    导致对 MiniMax 的调用被路由到 Clash 之类的代理而 502。
    Telegram 的代理由 config.telegram_proxy 显式传入 HTTPXRequest。

    注意：macOS 上 httpx 还会读 scutil 的系统代理，所以用 NO_PROXY 把本地
    embedding server 和 MiniMax 域名豁免掉，避免被 Clash 劫持。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
              "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.minimaxi.com,api.minimax.chat"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    # 模型已缓存；主进程跑时强制离线，避免 transformers 去 HF 查配置阻塞启动
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

log = logging.getLogger(__name__)


async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _purge_proxy_env()

    s = settings()
    if s.llm_provider == "anthropic":
        active_model = s.anthropic_model
    elif s.llm_provider == "openrouter":
        active_model = s.openrouter_model or "(unset!)"
    else:
        active_model = s.minimax_chat_model
    log.info("启动：allowed_chat=%s, provider=%s, model=%s",
             s.telegram_allowed_chat_id, s.llm_provider, active_model)
    audit("startup", provider=s.llm_provider, model=active_model,
          allowed_chat=s.telegram_allowed_chat_id)

    # 初始化 DB schema
    storage.engine()

    # 本地 embedding server：先启再起 bot，避免 memU 第一次 retrieve 时打到未就绪的端口
    embed_task = asyncio.create_task(
        embed_server.serve_forever(s.embed_server_host, s.embed_server_port, s.embed_model_name),
        name="embed_server",
    )
    # 等端口可连
    await _wait_port(s.embed_server_host, s.embed_server_port, timeout=60.0)
    log.info("embed server ready @ %s:%d", s.embed_server_host, s.embed_server_port)

    # MiniMax strip-think shim（memU 内部抽取/总结的 LLM 走这里，剥 <think> 后再回 memU）
    proxy_task = asyncio.create_task(
        llm_proxy.serve_forever(s.llm_proxy_host, s.llm_proxy_port),
        name="llm_proxy",
    )
    await _wait_port(s.llm_proxy_host, s.llm_proxy_port, timeout=15.0)
    log.info("llm proxy ready @ %s:%d", s.llm_proxy_host, s.llm_proxy_port)

    # Telegram application
    app = bot.build_application()

    # Scheduler
    send, typing = bot.make_send_and_typing()
    sched = scheduler.build(send, typing)

    stop_event = asyncio.Event()

    def _handle_sig() -> None:
        log.info("收到退出信号")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_sig)
        except NotImplementedError:
            pass

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    sched.start()
    log.info("ready")

    try:
        await stop_event.wait()
    finally:
        log.info("关停...")
        audit("shutdown")
        sched.shutdown(wait=False)
        if app.updater:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
        embed_task.cancel()
        proxy_task.cancel()
        for t in (embed_task, proxy_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await llm_proxy.aclose()
        await llm.aclose()
        log.info("再见")


async def _wait_port(host: str, port: int, timeout: float = 60.0) -> None:
    import socket, time  # noqa: PLC0415
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            await asyncio.sleep(0.5)
    raise RuntimeError(f"embed server 启动超时 {host}:{port}")


if __name__ == "__main__":
    asyncio.run(_main())
