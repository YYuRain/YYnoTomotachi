"""启动记忆浏览 UI。

用法：
  .venv/bin/python -m scripts.admin
然后浏览器打开 http://127.0.0.1:18081
"""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "admin")
    os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "0")
    # 强制离线（本模块不需要联网 HF）
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    from src.admin_ui import build_app

    app = build_app()
    # bind 全 interface 让 docker port mapping 工作；docker-compose 把 host 端口绑死 127.0.0.1，
    # 所以暴露面没扩大——本机访问、SSH 隧道访问都 OK，公网仍打不到
    host = os.environ.get("ADMIN_HOST", "0.0.0.0")
    port = int(os.environ.get("ADMIN_PORT", "18081"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
