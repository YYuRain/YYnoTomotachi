"""联调前的最小回环：MiniMax chat / embed / memU memorize+retrieve。

用法：
  .venv/bin/python -m scripts.preflight

需要先在 .env 填齐 MiniMax 的所有字段。Telegram 字段不会被用到。

每步失败会直接抛，便于定位。成功就打印 ✅。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
from pathlib import Path

from src import embed_server, minimax
from src.config import settings
from src.memory import _get_service  # noqa: PLC2701


async def step_chat() -> None:
    print("[1/3] MiniMax chat ...", end=" ", flush=True)
    # MiniMax-M2 会先输出 <think>...</think>，token 要给够
    out = await minimax.chat(
        [{"role": "user", "content": "只回两个字：收到"}],
        temperature=0.2,
        max_tokens=4000,
    )
    if not out:
        raise RuntimeError("chat 返回空（可能 max_tokens 仍然不够让 think 块完成）")
    print(f"✅ -> {out!r}")


async def step_embed_local(host: str, port: int) -> None:
    print("[2/3] 本地 embedding server ...", end=" ", flush=True)
    import httpx  # noqa: PLC0415
    async with httpx.AsyncClient(trust_env=False, timeout=30) as cli:
        r = await cli.post(
            f"http://{host}:{port}/v1/embeddings",
            json={"model": "any", "input": ["你好", "再见"]},
        )
        r.raise_for_status()
        data = r.json()
    vecs = [d["embedding"] for d in data["data"]]
    assert len(vecs) == 2 and len(vecs[0]) > 0
    print(f"✅ -> dim={len(vecs[0])}, n={len(vecs)}")


async def step_memu() -> None:
    print("[3/3] memU memorize + retrieve ...", end=" ", flush=True)
    svc = _get_service()

    sample = [
        {"role": "user", "content": "我叫阿禹，最近在学做菜，特别喜欢做番茄炒蛋。"},
        {"role": "assistant", "content": "哦，加糖还是加盐？"},
        {"role": "user", "content": "我加糖。我觉得咸的番茄炒蛋是异端。"},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False)
        path = f.name
    try:
        await svc.memorize(resource_url=path, modality="conversation", user={"user_id": "preflight"})
        result = await svc.retrieve(
            queries=[{"role": "user", "content": {"text": "我喜欢什么菜"}}],
            where={"user_id": "preflight"},
        )
        n_items = len(result.get("items") or [])
        n_cats = len(result.get("categories") or [])
        print(f"✅ -> items={n_items}, categories={n_cats}")
        if n_items == 0 and n_cats == 0:
            print("  ⚠️  未召回任何内容（可能 embedding 兼容性异常，见 document/memu-setup.md 风险表）")
    finally:
        Path(path).unlink(missing_ok=True)


async def _wait_port(host: str, port: int, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            await asyncio.sleep(0.5)
    raise RuntimeError(f"embed server 启动超时 {host}:{port}")


async def main() -> None:
    s = settings()
    print(f"MiniMax base={s.minimax_base_url}  chat={s.minimax_chat_model}")
    print(f"Embed server={s.embed_server_host}:{s.embed_server_port} model={s.embed_model_name}")

    # 先启 embed server
    print("启动本地 embedding server（首次会下载模型）...", flush=True)
    task = asyncio.create_task(
        embed_server.serve_forever(s.embed_server_host, s.embed_server_port, s.embed_model_name)
    )
    try:
        await _wait_port(s.embed_server_host, s.embed_server_port, timeout=180.0)
        print("  ready.\n")

        await step_chat()
        await step_embed_local(s.embed_server_host, s.embed_server_port)
        await step_memu()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await minimax.aclose()
    print("\n全部通过，可以 python -m src.main 了。")


if __name__ == "__main__":
    # 预检但不要求 Telegram 字段填齐
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "preflight")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    # 关键 1：清掉继承来的 HTTP(S)_PROXY；
    # 关键 2：即便 env 没设，httpx 在 macOS 会读 scutil 的系统代理，
    #         显式把本地和 MiniMax 放进 NO_PROXY，避免 memU 的 OpenAI 客户端走 Clash 造成 502。
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,api.minimaxi.com,api.minimax.chat"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    # 模型已在 ~/.cache/huggingface 缓存；强制离线，避免 transformers 去 huggingface.co 查 adapter_config 超时
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    asyncio.run(main())
