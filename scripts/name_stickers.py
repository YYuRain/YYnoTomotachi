"""一次性脚本：扫 data/stickers/，调主 LLM vision 给每张图起中文 tag，重命名。

跑法：
    .venv/bin/python -m scripts.name_stickers          # 真改
    .venv/bin/python -m scripts.name_stickers --dry    # 只打印不动文件

要求 LLM_PROVIDER=anthropic（Claude Sonnet 原生 vision）。
"""
from __future__ import annotations

import asyncio
import base64
import re
import sys
from pathlib import Path

from src import llm
from src.config import settings

DIR = settings().root / "data" / "stickers"
EXT_OK = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MEDIA_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

NAME_PROMPT = """看这张表情包，给它取一个 **2-6 字的中文 tag**，用来描述"什么情境/情绪下发这个表情最合适"。

参考风格（不要照抄，按图选合适的）：
- 情绪类：无奈、大笑、想哭、害羞、生气、社死、丧、惊喜
- 反应类：白眼、点头、摇头、赞同、拒绝、求饶、加油
- 状态类：摸鱼、吃瓜、躺平、内卷、好困、饿了
- 反讽类：笑死、好的吧、谢谢你哦、随便你

**只输出 tag 本身**——不要解释、不要标点、不要引号、不要"这是一个表示..."。"""

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\s\n\r\t]+')


def clean_tag(raw: str) -> str:
    t = raw.strip().strip('"\'`「」 \n。.,，')
    # 去掉模型可能输出的"tag："前缀
    if "：" in t:
        t = t.split("：", 1)[-1].strip()
    if ":" in t:
        t = t.split(":", 1)[-1].strip()
    t = INVALID_CHARS.sub("", t)
    if len(t) > 12:
        t = t[:12]
    return t


def unique_path(dir_: Path, tag: str, ext: str) -> Path:
    cand = dir_ / f"{tag}{ext}"
    if not cand.exists():
        return cand
    i = 2
    while True:
        cand = dir_ / f"{tag}-{i}{ext}"
        if not cand.exists():
            return cand
        i += 1


async def name_one(path: Path) -> str | None:
    media_type = MEDIA_BY_EXT.get(path.suffix.lower())
    if media_type is None:
        return None
    img_b64 = base64.b64encode(path.read_bytes()).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": img_b64},
                },
                {"type": "text", "text": NAME_PROMPT},
            ],
        }
    ]
    try:
        out = await llm.chat(messages, max_tokens=50, temperature=0.3, tier="aux")
    except Exception as e:
        print(f"  err: {e}")
        return None
    return clean_tag(out)


async def main(dry: bool = False) -> None:
    files = sorted(p for p in DIR.iterdir() if p.is_file() and p.suffix.lower() in EXT_OK)
    print(f"找到 {len(files)} 张图\n")
    renamed = 0
    for p in files:
        tag = await name_one(p)
        if not tag:
            print(f"  ✗ {p.name} → 跳过（生成失败/格式不支持）")
            continue
        new_path = unique_path(DIR, tag, p.suffix.lower())
        action = "[dry]" if dry else "[改]"
        print(f"  {action} {p.name}  →  {new_path.name}")
        if not dry:
            p.rename(new_path)
            renamed += 1
    print(f"\n完成。共重命名 {renamed} 张。{'重启 bot 让新 tag 生效。' if not dry else '加 --真改 跑实改。'}")


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    asyncio.run(main(dry=dry))
