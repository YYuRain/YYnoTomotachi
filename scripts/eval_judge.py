"""LLM judge：读 phase 1 评测 jsonl，按 sample 横向匿名打分。

跑法：
    .venv/bin/python -m scripts.eval_judge                       # 默认读最新 run_*.jsonl
    .venv/bin/python -m scripts.eval_judge --input data/eval/run_xxx.jsonl
    .venv/bin/python -m scripts.eval_judge --judge openrouter/openai/gpt-5.5
    .venv/bin/python -m scripts.eval_judge --dry                 # 只打印一个 sample 的 prompt 不调 LLM

设计：
- **匿名化**：每个 sample 内候选回复打成 A/B/C/...，judge 看不到 model 名（避免对 Claude/GPT 有先验偏见）
- **横向比**：同一个 prompt 下 N 个候选一起评分，比独立评分更准
- **多维度**：persona / rhythm / natural / topic / overall（0-5 整数）
- **输出**：
  - data/eval/<basename>.scores.jsonl  每行 (sample, model, dim 分数 + 简评)
  - data/eval/<basename>.scores.md     按 model 平均分排序的总表
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import string
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from src import openrouter
from src.config import settings

log = logging.getLogger("judge")

ROOT = settings().root
EVAL_DIR = ROOT / "data" / "eval"

DEFAULT_JUDGE = "openrouter/anthropic/claude-opus-4.6"
JUDGE_TEMP = 0.2
JUDGE_MAX_TOKENS = 4096
CONCURRENCY = 6

JUDGE_SYSTEM = """你是一个评测员。下面是用户和一个"陪伴型 AI"的对话片段，最末是用户的一句话；你将看到多个候选 AI 回复（被匿名化为 A/B/C/...），需要按多个维度给每个候选打分。

# 这个 AI 的角色设定（评测时按这个标准看）
- 它**不是助手 / 不是客服 / 不是心理咨询师**——是一个"在网上随便逛逛的人"。
- 短句、口语、像打字聊天，每条 ≤ 60 字（特殊话题可拆条说）；**禁用**"首先/其次""建议你...""可以考虑""希望对你有帮助""根据...""值得注意的是"等公文/助手语。
- 用第一人称（"我觉得 / 我会"）而不是"建议你..."。
- 平等、不嘘寒问暖、不上价值；可以冷幽默 / 真诚吐槽，但不硬段子不强热情不刻意冷漠。
- 默认不反问；陈述句代替问句延展话题。
- 走心场景下不抖机灵不跳话题、用承接（"嗯""那确实""听着了"）。
- 工具问题（"帮我看看链接"）只是简单回答，不展开。

# 评分维度（每项 0-5 整数）
- **persona**：贴角色风格——网友 vs 助手 / 客服 / 咨询师腔
- **rhythm**：节奏——短句、不长篇、不强行拆条；用对了"拆"还是误用？
- **natural**：不端着——避免公文词（"首先""建议""希望"），用"我觉得""我会"等口吻
- **topic**：切题——贴对方实际意图，不跑偏不答非所问
- **overall**：综合 0-5（不是简单平均，要考虑"作为陪伴感的整体感受"）

# 输出格式（严格 JSON，无任何前后说明、无代码块围栏）
{
  "A": {"persona": 4, "rhythm": 5, "natural": 4, "topic": 5, "overall": 4, "note": "≤25 字简评"},
  "B": {"persona": 2, "rhythm": 1, "natural": 1, "topic": 4, "overall": 2, "note": "..."},
  ...
}

候选数量 = 输入里的字母数（A/B/C...），每个都要打分，**不能漏**。

**重要**：note 字段里**绝对不能出现 ASCII 双引号 `"`**（会破坏 JSON）。如果要引用候选回复里的字串，用中文方引号 `「」` 或 `『』`。例：`"note": "用了「可以考虑」公文词"`，**不要写**成 `"note": ""可以考虑"踩禁词"`。
"""


def _build_judge_user_prompt(sample_id: str, kind: str, history: list[dict], user_text: str,
                             anon_replies: list[tuple[str, str]]) -> str:
    """anon_replies = [(letter, reply), ...]"""
    parts: list[str] = []
    parts.append(f"# 对话场景（id={sample_id}, kind={kind}）\n")
    if history:
        parts.append("**历史**：")
        for m in history[-6:]:
            role_zh = "user" if m.get("role") == "user" else "ai"
            content = (m.get("content") or "").replace("\n", " ")[:200]
            parts.append(f"- {role_zh}: {content}")
    parts.append(f"\n**最末用户消息**: {user_text}\n")
    parts.append(f"# 候选回复（共 {len(anon_replies)} 个）\n")
    for letter, reply in anon_replies:
        parts.append(f"=== {letter} ===")
        parts.append(reply if reply else "（空）")
        parts.append("")
    parts.append(f"\n现在按上面的评分维度给 {len(anon_replies)} 个候选打分（每个都要、不能漏），输出 JSON。")
    return "\n".join(parts)


def _parse_judge_response(raw: str) -> dict | None:
    """宽松抠 JSON。处理三种常见污染：纯 JSON / ```json...``` 围栏 / 前后多余说明。"""
    if not raw:
        return None
    raw = raw.strip()
    # 1. 直接 parse
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 2. 剥 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*\n?(.+?)\n?```", raw, re.DOTALL)
    if fence:
        inner = fence.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            pass
    # 3. 抠最长的 {...} 块
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ============ 读 phase 1 输入 ============

def _latest_run() -> Path | None:
    files = sorted(EVAL_DIR.glob("run_*.jsonl"), reverse=True)
    # 排除已经是 .scores.jsonl 的
    files = [f for f in files if not f.name.endswith(".scores.jsonl")]
    return files[0] if files else None


def _load_results(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


# ============ judge 主流程 ============

async def judge_sample(sample_id: str, sample_kind: str, user_text: str,
                       history_summary: list[dict],
                       candidates: list[dict],  # [{model, reply}, ...]
                       judge_model: str) -> list[dict]:
    """对一个 sample 的所有候选回复横向打分。返回 [{model, scores...}, ...]"""
    # 跳过有 error 或空回复的：依然要给 0 分还是直接跳过？给 0 分。
    valid_candidates: list[dict] = []
    invalid_results: list[dict] = []
    for c in candidates:
        reply = (c.get("reply") or "").strip()
        if c.get("error") or not reply:
            invalid_results.append({
                "sample_id": sample_id, "sample_kind": sample_kind, "model": c["model"],
                "persona": 0, "rhythm": 0, "natural": 0, "topic": 0, "overall": 0,
                "note": f"[skipped: {c.get('error') or 'empty'}]",
            })
        else:
            valid_candidates.append(c)

    if not valid_candidates:
        return invalid_results

    # 匿名化：A/B/C/...（最多 26 个）
    letters = list(string.ascii_uppercase)[: len(valid_candidates)]
    anon_replies = [(letters[i], c["reply"]) for i, c in enumerate(valid_candidates)]
    letter_to_model = {letters[i]: c["model"] for i, c in enumerate(valid_candidates)}

    user_prompt = _build_judge_user_prompt(
        sample_id, sample_kind, history_summary, user_text, anon_replies,
    )
    sub_model = judge_model[len("openrouter/"):] if judge_model.startswith("openrouter/") else judge_model
    res = await openrouter.chat(
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        sub_model,
        temperature=JUDGE_TEMP,
        max_tokens=JUDGE_MAX_TOKENS,
    )

    if res.get("error") or not res.get("text"):
        log.warning("judge sample %s 失败: %s", sample_id, res.get("error", "empty"))
        return invalid_results + [{
            "sample_id": sample_id, "sample_kind": sample_kind, "model": c["model"],
            "persona": -1, "rhythm": -1, "natural": -1, "topic": -1, "overall": -1,
            "note": f"[judge err: {res.get('error', 'empty')[:80]}]",
        } for c in valid_candidates]

    parsed = _parse_judge_response(res["text"])
    if not parsed or not isinstance(parsed, dict):
        log.warning("judge sample %s 解析失败: %s", sample_id, res["text"][:200])
        return invalid_results + [{
            "sample_id": sample_id, "sample_kind": sample_kind, "model": c["model"],
            "persona": -1, "rhythm": -1, "natural": -1, "topic": -1, "overall": -1,
            "note": "[parse err]",
        } for c in valid_candidates]

    out = list(invalid_results)
    for letter, model in letter_to_model.items():
        s = parsed.get(letter) or {}
        out.append({
            "sample_id": sample_id,
            "sample_kind": sample_kind,
            "model": model,
            "persona": int(s.get("persona", -1)) if s else -1,
            "rhythm": int(s.get("rhythm", -1)) if s else -1,
            "natural": int(s.get("natural", -1)) if s else -1,
            "topic": int(s.get("topic", -1)) if s else -1,
            "overall": float(s.get("overall", -1)) if s else -1,
            "note": (s.get("note") or "")[:80] if s else "[missing in judge output]",
        })
    print(f"  ✓ {sample_id} ({sample_kind}) judged: {len(letter_to_model)} candidates")
    return out


async def run(input_path: Path, judge_model: str, dry: bool) -> None:
    records = _load_results(input_path)
    if not records:
        print(f"ERROR: 输入文件为空：{input_path}")
        return

    # 按 sample_id 分组
    by_sample: dict[str, dict] = {}
    for r in records:
        sid = r["sample_id"]
        if sid not in by_sample:
            by_sample[sid] = {
                "sample_id": sid,
                "sample_kind": r.get("sample_kind", ""),
                "user_text": r.get("user_text", ""),
                "history_summary": [],  # phase 1 jsonl 没存完整 history，只存 user_text/history_len
                "candidates": [],
            }
        by_sample[sid]["candidates"].append({
            "model": r["model"],
            "reply": r.get("reply", ""),
            "error": r.get("error"),
        })

    print(f"\n=== LLM judge ===")
    print(f"输入: {input_path}")
    print(f"sample 数: {len(by_sample)}")
    print(f"judge model: {judge_model}")
    print(f"每 sample 候选数: {len(records) // len(by_sample) if by_sample else 0}")

    if dry:
        print("\n[DRY] 第一个 sample 的 judge prompt 预览：\n")
        first = next(iter(by_sample.values()))
        valid = [c for c in first["candidates"] if (c.get("reply") or "").strip()]
        letters = list(string.ascii_uppercase)[:len(valid)]
        anon = [(letters[i], c["reply"]) for i, c in enumerate(valid)]
        prompt = _build_judge_user_prompt(
            first["sample_id"], first["sample_kind"],
            [], first["user_text"], anon,
        )
        print(prompt[:2000])
        print("\n[DRY] (printed first 2000 chars)")
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    all_scores: list[dict] = []

    async def _task(s: dict) -> None:
        async with sem:
            scores = await judge_sample(
                s["sample_id"], s["sample_kind"], s["user_text"],
                s["history_summary"], s["candidates"], judge_model,
            )
        all_scores.extend(scores)

    print("\n=== 评分中 ===\n")
    await asyncio.gather(*[_task(s) for s in by_sample.values()])

    # 写 jsonl
    base = input_path.stem  # run_xxx
    scores_jsonl = EVAL_DIR / f"{base}.scores.jsonl"
    scores_md = EVAL_DIR / f"{base}.scores.md"
    with scores_jsonl.open("w", encoding="utf-8") as f:
        for s in all_scores:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    write_summary_md(scores_md, all_scores, judge_model, input_path)

    print(f"\n完成。{len(all_scores)} 行评分。")
    print(f"  jsonl:  {scores_jsonl}")
    print(f"  md:     {scores_md}")

    await openrouter.aclose()


def write_summary_md(path: Path, scores: list[dict], judge_model: str, input_path: Path) -> None:
    # 按 model 汇总
    by_model: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        if s.get("overall", -1) >= 0:
            by_model[s["model"]].append(s)

    summary_rows: list[dict] = []
    for model, items in by_model.items():
        n = len(items)
        if n == 0:
            continue
        summary_rows.append({
            "model": model,
            "n": n,
            "persona": sum(x["persona"] for x in items) / n,
            "rhythm": sum(x["rhythm"] for x in items) / n,
            "natural": sum(x["natural"] for x in items) / n,
            "topic": sum(x["topic"] for x in items) / n,
            "overall": sum(x["overall"] for x in items) / n,
        })
    summary_rows.sort(key=lambda r: r["overall"], reverse=True)

    # 按 sample × model 详细矩阵
    by_sample_model: dict[tuple[str, str], dict] = {(s["sample_id"], s["model"]): s for s in scores}
    sample_ids = sorted({s["sample_id"] for s in scores})
    models = [r["model"] for r in summary_rows]

    with path.open("w", encoding="utf-8") as f:
        f.write(f"# LLM judge 评分汇总 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"- 输入: `{input_path.name}`\n")
        f.write(f"- judge: `{judge_model}`\n")
        f.write(f"- 模型数: {len(by_model)}, 样本数: {len(sample_ids)}\n\n")
        f.write("## 按 overall 排序（5 分制，越高越贴『陪伴感』）\n\n")
        f.write("| 排名 | 模型 | overall | persona | rhythm | natural | topic | n |\n")
        f.write("|------|------|---------|---------|--------|---------|-------|---|\n")
        for i, r in enumerate(summary_rows, 1):
            f.write(
                f"| {i} | {r['model']} | **{r['overall']:.2f}** | "
                f"{r['persona']:.2f} | {r['rhythm']:.2f} | {r['natural']:.2f} | {r['topic']:.2f} | {r['n']} |\n"
            )
        f.write("\n---\n\n")
        f.write("## 各 sample × model overall 矩阵\n\n")
        # 表头
        f.write("| sample (kind) |")
        for m in models:
            short = m.split("/")[-1][:20]
            f.write(f" {short} |")
        f.write("\n|------|" + "------|" * len(models) + "\n")
        for sid in sample_ids:
            sample_kind = next(
                (s.get("sample_kind", "") for s in scores if s["sample_id"] == sid), "",
            )
            f.write(f"| {sid} ({sample_kind}) |")
            for m in models:
                s = by_sample_model.get((sid, m))
                if s and s.get("overall", -1) >= 0:
                    f.write(f" {s['overall']:.1f} |")
                else:
                    f.write(" - |")
            f.write("\n")
        f.write("\n---\n\n")
        f.write("## 失败 / 解析错（参考）\n\n")
        bad = [s for s in scores if s.get("overall", 0) < 0]
        if bad:
            for s in bad[:30]:
                f.write(f"- {s['sample_id']} × {s['model']}: {s.get('note','')}\n")
        else:
            f.write("（无）\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for n in ("httpx", "httpcore"):
        logging.getLogger(n).setLevel(logging.WARNING)

    p = argparse.ArgumentParser()
    p.add_argument("--input", type=str, default="", help="phase 1 jsonl 路径，默认最新 run_*.jsonl")
    p.add_argument("--judge", type=str, default=DEFAULT_JUDGE, help="judge 模型")
    p.add_argument("--dry", action="store_true", help="只打印第一个 sample 的 prompt 不调 LLM")
    args = p.parse_args()

    if args.input:
        input_path = Path(args.input)
    else:
        latest = _latest_run()
        if not latest:
            print("ERROR: 没找到 data/eval/run_*.jsonl")
            sys.exit(1)
        input_path = latest

    asyncio.run(run(input_path, args.judge, args.dry))


if __name__ == "__main__":
    main()
