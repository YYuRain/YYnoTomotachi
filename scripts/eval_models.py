"""多模型回复质量评测脚本。

跑法：
    .venv/bin/python -m scripts.eval_models                   # 全集合
    .venv/bin/python -m scripts.eval_models --dry             # 只采样不调 LLM
    .venv/bin/python -m scripts.eval_models --samples 5       # 改采样数（默认 20）
    .venv/bin/python -m scripts.eval_models --models a,b,c    # 覆盖 .env 模型列表

设计要点：
- 完全独立脚本，**不污染线上 bot 任何状态**：
  - 不 import src.agent / src.memory / src.persona / src.storage / src.interests / src.availability
  - 只 import src.config / src.llm / src.minimax / src.openrouter / src.clock
  - 用静态 baseline persona（直接读 System Prompt v0.0.1.md），不注入 persona/memory/interest/sticker/emotion 动态段
- 输出：
  - data/eval/run_<ts>.jsonl  程序友好（phase 2 judge 读它）
  - data/eval/run_<ts>.md     人类友好的横向对比表
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml  # type: ignore

from src import clock, llm, minimax, openrouter
from src.config import settings

log = logging.getLogger("eval")

ROOT = settings().root
BUFFER_DIR = ROOT / "data" / "memu_buffer" / "ingested"
FIXTURES_PATH = ROOT / "eval" / "fixtures.yaml"
EVAL_OUT_DIR = ROOT / "data" / "eval"
SYSTEM_PROMPT_PATH = ROOT / "System Prompt v0.0.1.md"

# 评测时统一的生成参数。
# max_tokens=4096 而不是 600：reasoning model（gpt-5/o1/MiniMax-M2 think）会先用一大块 token
# 做内心独白，content 才出来；token 少了 finish_reason=length，content 永远是空。
# 评测一次性，多花点 token 拿到所有模型的真实回复 > 省钱。
GEN_TEMPERATURE = 0.85
GEN_MAX_TOKENS = 4096
CONCURRENCY = 8

EMOTION_KEYWORDS = ["难过", "累", "丧", "生气", "崩溃", "焦虑", "孤独", "烦", "委屈", "心碎"]
URL_RE = re.compile(r"https?://\S+|xhslink\.com/\S+|b23\.tv/\S+")


# ============ 采样 ============

def _classify(user_text: str) -> str:
    if URL_RE.search(user_text):
        return "tool"
    if "?" in user_text or "？" in user_text:
        return "question"
    if any(k in user_text for k in EMOTION_KEYWORDS):
        return "emotion"
    if len(user_text) < 10:
        return "chitchat"
    if len(user_text) > 50:
        return "long"
    return "chitchat"


def _load_buffer_samples(target_n: int, seed: int = 42) -> list[dict]:
    """从 data/memu_buffer/ingested/ 抽 target_n 个 sample。
    每个 sample 取该 conv 文件最末的一条 user 消息作为 prompt，前面作为 history。
    按 kind 均匀覆盖。"""
    if not BUFFER_DIR.exists():
        log.warning("buffer dir 不存在: %s", BUFFER_DIR)
        return []

    files = sorted(BUFFER_DIR.glob("conv_*.json"))
    candidates: list[dict] = []
    for f in files:
        try:
            msgs = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(msgs, list) or not msgs:
            continue
        # 找最末的 user 消息
        last_user_idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx < 1:  # 至少要有 history
            continue
        user_text = (msgs[last_user_idx].get("content") or "").strip()
        if not user_text:
            continue
        history = msgs[:last_user_idx]
        # 限制 history 大小防 prompt 过长
        if len(history) > 12:
            history = history[-12:]
        candidates.append({
            "id": f"buf-{f.stem.replace('conv_', '')}",
            "kind": _classify(user_text),
            "history": history,
            "user_text": user_text,
            "source": f.name,
        })

    if not candidates:
        return []

    # 按 kind 分桶，每桶最多 ceil(target/k) 个；用固定种子保证可复现
    rng = random.Random(seed)
    by_kind: dict[str, list[dict]] = {}
    for c in candidates:
        by_kind.setdefault(c["kind"], []).append(c)
    for k in by_kind:
        rng.shuffle(by_kind[k])

    # 轮询各 kind 取，凑够 target_n
    out: list[dict] = []
    kinds = list(by_kind.keys())
    while len(out) < target_n and any(by_kind[k] for k in kinds):
        for k in kinds:
            if len(out) >= target_n:
                break
            if by_kind[k]:
                out.append(by_kind[k].pop())
    return out


def _load_fixtures() -> list[dict]:
    if not FIXTURES_PATH.exists():
        return []
    try:
        data = yaml.safe_load(FIXTURES_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("fixtures.yaml 解析失败: %s", e)
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not item.get("user_text"):
            continue
        out.append({
            "id": item.get("id") or f"fix-{len(out)+1:03d}",
            "kind": item.get("kind", "fixture"),
            "history": item.get("history") or [],
            "user_text": item["user_text"],
            "source": "fixtures.yaml",
        })
    return out


# ============ 构造 messages（不读 storage） ============

def build_messages(sample: dict) -> list[dict]:
    sys_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    msgs: list[dict] = [{"role": "system", "content": sys_prompt}]
    for m in sample.get("history") or []:
        role = m.get("role")
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": m.get("content", "")})
    time_prefix = f"[现在 {clock.now_signal()}]"
    msgs.append({
        "role": "user",
        "content": f"{time_prefix}\n\n{sample['user_text']}",
    })
    return msgs


# ============ provider 派发 ============

async def _call_one(model_key: str, messages: list[dict]) -> dict:
    """根据 'provider/model_id' 的前缀派发到对应客户端。
    统一返回 {text, latency_ms, model, error?, tokens?}"""
    t0 = time.time()
    if model_key.startswith("openrouter/"):
        sub = model_key[len("openrouter/"):]
        return await openrouter.chat(messages, sub, temperature=GEN_TEMPERATURE, max_tokens=GEN_MAX_TOKENS)

    if model_key.startswith("local/minimax"):
        # local/minimax-m2.7 → 用配置里的 chat_model（M2.7）；如果指定别的可加判断
        sub = model_key[len("local/"):]
        try:
            text = await minimax.chat(
                messages,
                temperature=GEN_TEMPERATURE,
                max_tokens=max(GEN_MAX_TOKENS, 2048),  # MiniMax 要给 think 留空间
                model=sub if sub != "minimax-m2.7" else None,
            )
            return {"text": text, "latency_ms": (time.time() - t0) * 1000, "model": model_key}
        except Exception as e:
            return {"text": "", "error": f"{e}", "latency_ms": (time.time() - t0) * 1000, "model": model_key}

    if model_key.startswith("local/claude"):
        # local/claude-sonnet-4-6 → 走 src.llm._anthropic_chat（内网 gateway）
        sub = model_key[len("local/"):]
        try:
            text = await llm._anthropic_chat(
                messages,
                temperature=GEN_TEMPERATURE,
                max_tokens=GEN_MAX_TOKENS,
                model=sub,
            )
            return {"text": text, "latency_ms": (time.time() - t0) * 1000, "model": model_key}
        except Exception as e:
            return {"text": "", "error": f"{e}", "latency_ms": (time.time() - t0) * 1000, "model": model_key}

    return {"text": "", "error": f"未知 provider: {model_key}", "latency_ms": 0, "model": model_key}


# ============ 主流程 ============

async def run(samples: list[dict], models: list[str], dry: bool, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = out_dir / f"run_{ts}.jsonl"
    md_path = out_dir / f"run_{ts}.md"

    print(f"\n=== 评测 ===")
    print(f"sample 数: {len(samples)}")
    print(f"按 kind 分布: {dict((k, sum(1 for s in samples if s['kind']==k)) for k in sorted({s['kind'] for s in samples}))}")
    print(f"模型数: {len(models)}")
    for m in models:
        print(f"  - {m}")
    print(f"\n输出:")
    print(f"  {jsonl_path}")
    print(f"  {md_path}")

    if dry:
        print("\n[DRY] 采样预览:")
        for s in samples[:5]:
            preview = s["user_text"][:60].replace("\n", " ")
            print(f"  [{s['kind']}] {s['id']}: {preview}")
        print(f"  ... (共 {len(samples)} 条)")
        return

    # 收集所有结果
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []

    async def task(sample: dict, model: str) -> None:
        msgs = build_messages(sample)
        async with sem:
            r = await _call_one(model, msgs)
        results.append({
            "sample_id": sample["id"],
            "sample_kind": sample["kind"],
            "model": model,
            "user_text": sample["user_text"],
            "history_len": len(sample.get("history") or []),
            "reply": r.get("text", ""),
            "latency_ms": r.get("latency_ms", 0),
            "error": r.get("error"),
            "tokens": r.get("tokens"),
            "source": sample.get("source"),
        })
        status = "✓" if r.get("text") else "✗"
        print(f"  {status} [{sample['kind']}] {sample['id']} ← {model} ({int(r.get('latency_ms', 0))}ms)")

    print("\n=== 跑模型 ===\n")
    tasks = [task(s, m) for s in samples for m in models]
    await asyncio.gather(*tasks)

    # 写 jsonl
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 写 markdown
    write_markdown(md_path, samples, models, results)

    print(f"\n完成。{len(results)} 行结果。")
    print(f"  jsonl: {jsonl_path}")
    print(f"  md:    {md_path}")

    # 关闭客户端
    await openrouter.aclose()
    await minimax.aclose()


def write_markdown(path: Path, samples: list[dict], models: list[str], results: list[dict]) -> None:
    by_sample: dict[str, dict[str, dict]] = {}
    for r in results:
        by_sample.setdefault(r["sample_id"], {})[r["model"]] = r

    with path.open("w", encoding="utf-8") as f:
        f.write(f"# 多模型评测 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"- 样本数: {len(samples)}\n")
        f.write(f"- 模型数: {len(models)}\n")
        f.write(f"- 生成参数: temperature={GEN_TEMPERATURE}, max_tokens={GEN_MAX_TOKENS}\n\n")
        f.write("---\n\n")

        for sample in samples:
            f.write(f"## {sample['id']} ({sample['kind']})\n\n")
            history = sample.get("history") or []
            if history:
                f.write("**history**（最后两轮）：\n")
                for m in history[-4:]:
                    role_zh = "user" if m["role"] == "user" else "ai"
                    content = (m.get("content") or "").replace("\n", " ")[:120]
                    f.write(f"- {role_zh}: {content}\n")
                f.write("\n")
            f.write(f"**user**: {sample['user_text']}\n\n")
            f.write("| 模型 | 回复 | 延迟 |\n")
            f.write("|------|------|------|\n")
            for model in models:
                r = by_sample.get(sample["id"], {}).get(model)
                if r is None:
                    continue
                # markdown 表格里换行用 <br>
                if r.get("error"):
                    cell = f"⚠️ {r['error'][:120]}"
                else:
                    cell = (r.get("reply") or "").replace("|", "\\|").replace("\n", "<br>")
                latency = f"{int(r.get('latency_ms', 0))}ms"
                f.write(f"| {model} | {cell} | {latency} |\n")
            f.write("\n---\n\n")


# ============ CLI ============

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for n in ("httpx", "httpcore"):
        logging.getLogger(n).setLevel(logging.WARNING)

    p = argparse.ArgumentParser()
    p.add_argument("--samples", type=int, default=20, help="从 buffer 采样的对话数（默认 20）")
    p.add_argument("--models", type=str, default="", help="覆盖 .env EVAL_MODELS（逗号分隔）")
    p.add_argument("--dry", action="store_true", help="只采样不调 LLM")
    p.add_argument("--seed", type=int, default=42, help="采样随机种子")
    args = p.parse_args()

    s = settings()
    models_str = args.models or s.eval_models
    models = [m.strip() for m in models_str.split(",") if m.strip()]
    if not models:
        print("ERROR: 没有模型配置（.env::EVAL_MODELS 或 --models）")
        sys.exit(1)

    fixtures = _load_fixtures()
    buffer_samples = _load_buffer_samples(args.samples, seed=args.seed)
    samples = buffer_samples + fixtures

    if not samples:
        print("ERROR: 没有任何 sample（buffer 空 + fixtures 空）")
        sys.exit(1)

    asyncio.run(run(samples, models, args.dry, EVAL_OUT_DIR))


if __name__ == "__main__":
    main()
