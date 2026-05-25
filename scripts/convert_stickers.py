"""把 data/stickers/ 下的图片/动图转成 Telegram 真正的 sticker 格式。

Telegram sticker 规格（2025）：
- 静态：WebP，至少一边 = 512px，文件 ≤ 512 KB
- 视频：WEBM (VP9)，至少一边 = 512px，最长 3s，文件 ≤ 256 KB，no audio

本脚本：
- jpg / jpeg / png → 512px 长边的 WebP（PIL，无外部依赖）
- gif → 512px 长边的 WEBM/VP9（需要系统 ffmpeg）
- 已经是 webp / webm 的 → 跳过

用法：
  # dry-run（默认）：只打印会怎么转，不动文件
  .venv/bin/python -m scripts.convert_stickers

  # 实跑：真转换 + 原图备份到 data/stickers/.original/
  .venv/bin/python -m scripts.convert_stickers --apply

  # 不要备份原图（直接删）
  .venv/bin/python -m scripts.convert_stickers --apply --no-backup

  # 自定义质量
  .venv/bin/python -m scripts.convert_stickers --apply --webp-quality 85 --webm-crf 30

依赖：
- PIL (Pillow ≥ 10) 已在 .venv 里——处理静态够用
- ffmpeg（系统命令）—— 处理 gif → webm 必须；缺则跳过 gif，仅转静态部分
  Mac 装：brew install ffmpeg
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

STICKERS_DIR = Path("data/stickers")
BACKUP_DIR = STICKERS_DIR / ".original"

STATIC_SRC_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_SRC_EXTS = {".gif"}
TARGET_LONG_EDGE = 512
WEBP_MAX_BYTES = 512 * 1024
WEBM_MAX_BYTES = 256 * 1024


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _resize_keep_aspect(img: Image.Image, long_edge: int) -> Image.Image:
    """长边缩到 long_edge，短边按比例。"""
    w, h = img.size
    if max(w, h) == long_edge:
        return img
    if w >= h:
        new_w = long_edge
        new_h = round(h * long_edge / w)
    else:
        new_h = long_edge
        new_w = round(w * long_edge / h)
    return img.resize((new_w, new_h), Image.LANCZOS)


def convert_static(src: Path, dst: Path, *, quality: int = 90) -> dict:
    """jpg/jpeg/png → webp。返回 {ok, src_size, dst_size, dim}。"""
    img = Image.open(src)
    # 先转 RGBA（jpg 没 alpha 也无所谓——webp 支持 RGB），便于统一处理
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    img = _resize_keep_aspect(img, TARGET_LONG_EDGE)
    img.save(dst, format="WEBP", quality=quality, method=6)

    dst_size = dst.stat().st_size
    # 太大就降质量重存（Telegram ≤ 512 KB）
    if dst_size > WEBP_MAX_BYTES:
        for q in (80, 70, 60, 50):
            img.save(dst, format="WEBP", quality=q, method=6)
            dst_size = dst.stat().st_size
            if dst_size <= WEBP_MAX_BYTES:
                break

    return {
        "ok": dst_size <= WEBP_MAX_BYTES,
        "src_size": src.stat().st_size,
        "dst_size": dst_size,
        "dim": img.size,
    }


def convert_video(src: Path, dst: Path, *, crf: int = 32) -> dict:
    """gif → webm/vp9。返回 {ok, src_size, dst_size}。需要 ffmpeg。"""
    if not _ffmpeg_available():
        return {"ok": False, "error": "ffmpeg not installed", "src_size": src.stat().st_size}

    # 取 gif 的尺寸跟时长——长边 scale 到 512
    # 用 scale 滤镜：长边到 512，短边按比例（保持透明 if any）；fps≤30
    scale_filter = (
        f"scale=if(gt(iw\\,ih)\\,{TARGET_LONG_EDGE}\\,-2):"
        f"if(gt(iw\\,ih)\\,-2\\,{TARGET_LONG_EDGE}):flags=lanczos"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-t", "3",  # max 3s
        "-vf", scale_filter + ",fps=30",
        "-c:v", "libvpx-vp9",
        "-b:v", "0",
        "-crf", str(crf),
        "-an",  # no audio
        "-pix_fmt", "yuva420p",  # 保留透明
        "-deadline", "good",
        "-cpu-used", "2",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        return {
            "ok": False,
            "error": (proc.stderr or "")[:300],
            "src_size": src.stat().st_size,
        }

    dst_size = dst.stat().st_size
    # 太大就提高 crf 重转
    if dst_size > WEBM_MAX_BYTES:
        for c in (40, 48, 56):
            new_cmd = list(cmd)
            new_cmd[new_cmd.index("-crf") + 1] = str(c)
            proc = subprocess.run(new_cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                continue
            dst_size = dst.stat().st_size
            if dst_size <= WEBM_MAX_BYTES:
                break

    return {
        "ok": dst_size <= WEBM_MAX_BYTES,
        "src_size": src.stat().st_size,
        "dst_size": dst_size,
    }


def list_targets() -> tuple[list[Path], list[Path], list[Path]]:
    """返回 (静态待转, 视频待转, 已经是 webp/webm 跳过)。"""
    static_targets: list[Path] = []
    video_targets: list[Path] = []
    skipped: list[Path] = []
    for f in STICKERS_DIR.iterdir():
        if not f.is_file() or f.name.startswith("."):
            continue
        if f.suffix.lower() in {".webp", ".webm"}:
            skipped.append(f)
        elif f.suffix.lower() in STATIC_SRC_EXTS:
            static_targets.append(f)
        elif f.suffix.lower() in VIDEO_SRC_EXTS:
            video_targets.append(f)
    return static_targets, video_targets, skipped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="实跑（不传则 dry-run）")
    ap.add_argument("--no-backup", action="store_true",
                    help="不备份原图（直接删）；默认会移到 data/stickers/.original/")
    ap.add_argument("--webp-quality", type=int, default=90)
    ap.add_argument("--webm-crf", type=int, default=32,
                    help="VP9 CRF（越低越清晰，越大文件越小；默认 32）")
    args = ap.parse_args()

    if not STICKERS_DIR.exists():
        print(f"!! {STICKERS_DIR} 不存在", file=sys.stderr)
        sys.exit(1)

    static_targets, video_targets, skipped = list_targets()
    print(f"=== convert_stickers (apply={args.apply}) ===")
    print(f"  静态待转: {len(static_targets)}（{', '.join(p.suffix for p in static_targets[:5])}...）")
    print(f"  视频待转: {len(video_targets)}（gif → webm vp9）")
    print(f"  已是 webp/webm 跳过: {len(skipped)}")
    print(f"  ffmpeg: {'✓ 可用' if _ffmpeg_available() else '✗ 未安装（gif 跳过；brew install ffmpeg）'}")
    print()

    if not args.apply:
        for f in static_targets:
            print(f"  [dry] static  {f.name} → {f.stem}.webp")
        for f in video_targets:
            print(f"  [dry] video   {f.name} → {f.stem}.webm")
        print(f"\n→ dry-run。要真做加 --apply")
        return

    # 备份目录
    if not args.no_backup:
        BACKUP_DIR.mkdir(exist_ok=True)
        print(f"  备份目录: {BACKUP_DIR}")

    static_ok = static_fail = video_ok = video_fail = 0

    # 静态
    for src in static_targets:
        dst = src.with_suffix(".webp")
        if dst.exists():
            print(f"  ?? {dst.name} 已存在，跳过 {src.name}")
            continue
        try:
            res = convert_static(src, dst, quality=args.webp_quality)
        except Exception as e:
            print(f"  XX {src.name}: {e}")
            static_fail += 1
            continue
        marker = "✓" if res["ok"] else "?"
        print(f"  {marker} {src.name} → {dst.name}  "
              f"{res['src_size']//1024}KB → {res['dst_size']//1024}KB  {res['dim']}")
        if res["ok"]:
            static_ok += 1
            _move_or_remove(src, args.no_backup)
        else:
            static_fail += 1
            dst.unlink(missing_ok=True)

    # 视频
    for src in video_targets:
        dst = src.with_suffix(".webm")
        if dst.exists():
            print(f"  ?? {dst.name} 已存在，跳过 {src.name}")
            continue
        try:
            res = convert_video(src, dst, crf=args.webm_crf)
        except Exception as e:
            print(f"  XX {src.name}: {e}")
            video_fail += 1
            continue
        if not res.get("ok"):
            err = res.get("error", "size > 256KB")
            print(f"  XX {src.name}: {err[:120]}")
            video_fail += 1
            dst.unlink(missing_ok=True)
        else:
            print(f"  ✓ {src.name} → {dst.name}  "
                  f"{res['src_size']//1024}KB → {res['dst_size']//1024}KB")
            video_ok += 1
            _move_or_remove(src, args.no_backup)

    print(f"\n=== 总结 ===")
    print(f"  静态: {static_ok} 成功 / {static_fail} 失败")
    print(f"  视频: {video_ok} 成功 / {video_fail} 失败")
    if not args.no_backup:
        print(f"  原图备份: {BACKUP_DIR}（不再用了可手动删）")


def _move_or_remove(src: Path, no_backup: bool) -> None:
    if no_backup:
        src.unlink()
    else:
        target = BACKUP_DIR / src.name
        if target.exists():
            target = BACKUP_DIR / f"{src.stem}_{int(src.stat().st_mtime)}{src.suffix}"
        src.rename(target)


if __name__ == "__main__":
    main()
