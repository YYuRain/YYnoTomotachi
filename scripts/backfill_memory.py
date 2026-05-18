"""把 data/memu_buffer/ 下所有历史对话文件回灌到自搭记忆栈（postgres `memories`）。

用法：
  .venv/bin/python -m scripts.backfill_memory [--uid <bigint>]

文件名规约：
- 新格式：`conv_<uid>_<ts>.json`，自动按文件名拿 uid
- 旧格式：`conv_<ts>.json`（memU 单用户时代）→ 必须传 --uid 指定收件人

每个处理完的文件会被移到 data/memu_buffer/ingested/，避免重复入库。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
from pathlib import Path


_CONV_NAME = re.compile(r"^conv_(?:(?P<uid>-?\d+)_)?(?P<ts>\d+)\.json$")


def _parse_filename(name: str, default_uid: int | None) -> int | None:
    m = _CONV_NAME.match(name)
    if not m:
        return None
    uid = m.group("uid")
    if uid is not None:
        return int(uid)
    return default_uid


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, default=None,
                        help="给旧格式 conv_<ts>.json（无 uid）使用的默认 uid")
    args = parser.parse_args()

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "backfill")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.minimaxi.com"
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    from src import embed_server, memory, memory_store
    from src.config import settings

    s = settings()
    memory_store.engine()  # ensure table

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

    print(f"开始 ingest（{len(files)} 个文件）...")
    ok = fail = skipped = 0
    try:
        for i, path in enumerate(files, 1):
            uid = _parse_filename(path.name, args.uid)
            if uid is None:
                skipped += 1
                print(f"  [{i}/{len(files)}] {path.name} -- 旧格式且未传 --uid，跳过")
                continue

            try:
                batch = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(batch, list):
                    raise ValueError("文件内容不是 list")
                items = await memory._extract_items(batch)  # noqa: SLF001
                summaries = []
                if items:
                    summaries = await memory._persist_items(  # noqa: SLF001
                        uid, items, evidence_ref=str(path),
                    )
                shutil.move(str(path), str(done_dir / path.name))
                ok += 1
                print(f"  [{i}/{len(files)}] {path.name} (uid={uid}) ✓ +{len(summaries)} items")
            except Exception as e:
                fail += 1
                print(f"  [{i}/{len(files)}] {path.name} ✗ {type(e).__name__}: {str(e)[:120]}")
            await asyncio.sleep(1.0)  # 节流 LLM 抽取
    finally:
        if owned_task is not None:
            owned_task.cancel()
            try:
                await owned_task
            except (asyncio.CancelledError, Exception):
                pass

    print(f"\n完成：{ok} 成功 / {fail} 失败 / {skipped} 跳过")


if __name__ == "__main__":
    asyncio.run(main())
