"""把 data/memu_buffer/ 下所有历史对话文件回灌给当前 memU 存储（postgres）。

用法：
  .venv/bin/python -m scripts.backfill_memory

每个处理完的文件会被移到 data/memu_buffer/ingested/，避免重复入库。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path


async def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "backfill")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.minimaxi.com"
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    from src import embed_server
    from src.config import settings
    from src.memory import _get_service, USER_ID  # type: ignore

    s = settings()
    buf = Path("data/memu_buffer")
    done_dir = buf / "ingested"
    done_dir.mkdir(parents=True, exist_ok=True)

    files = sorted([p for p in buf.glob("conv_*.json") if p.is_file()])
    if not files:
        print("没有待 ingest 的文件。"); return

    # 若 bot 已经在跑（18080 已占用），复用它；否则本地起一个
    import socket, time
    owned_task = None
    try:
        with socket.create_connection((s.embed_server_host, s.embed_server_port), timeout=1.0):
            print(f"检测到 embed server 已在 {s.embed_server_host}:{s.embed_server_port} 运行，复用。")
    except OSError:
        print("embed server 未运行，本脚本临时起一个。")
        owned_task = asyncio.create_task(
            embed_server.serve_forever(s.embed_server_host, s.embed_server_port, s.embed_model_name)
        )
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                with socket.create_connection((s.embed_server_host, s.embed_server_port), timeout=1.0):
                    break
            except OSError:
                await asyncio.sleep(0.5)
    print(f"开始 memorize（{len(files)} 个，provider={s.memu_metadata_provider}）...")

    svc = _get_service()
    ok = fail = 0
    try:
        for i, path in enumerate(files, 1):
            # 节流 + 遇到 529/5xx 退避重试
            for attempt in range(3):
                try:
                    await svc.memorize(
                        resource_url=str(path),
                        modality="conversation",
                        user={"user_id": USER_ID},
                    )
                    shutil.move(str(path), str(done_dir / path.name))
                    ok += 1
                    print(f"  [{i}/{len(files)}] {path.name} ✓")
                    break
                except Exception as e:
                    msg = str(e)
                    retriable = any(s in msg for s in ("529", "503", "overload", "rate limit", "Too Many"))
                    if retriable and attempt < 2:
                        wait = 5 * (attempt + 1)
                        print(f"  [{i}/{len(files)}] {path.name} retry {attempt+1}/2 after {wait}s ({msg[:80]})")
                        await asyncio.sleep(wait)
                        continue
                    fail += 1
                    print(f"  [{i}/{len(files)}] {path.name} ✗ {msg[:120]}")
                    break
            # 节流避免 MiniMax 并发打爆
            await asyncio.sleep(2.0)
    finally:
        if owned_task is not None:
            owned_task.cancel()
            try:
                await owned_task
            except (asyncio.CancelledError, Exception):
                pass

    print(f"\n完成：{ok} 成功 / {fail} 失败")


if __name__ == "__main__":
    asyncio.run(main())
