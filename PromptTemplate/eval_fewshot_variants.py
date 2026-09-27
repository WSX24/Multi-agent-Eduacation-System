#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Few-shot 变体对比实验：解决「同题撞车抄袭」的两个候选方案，哪个更好？

问题回顾：Few-shot 要「具体」才教得会格式，而「具体」的内容正是会被抄走的东西。
于是有两个破法——

  变体 A「非学科示例」：保留具体，换掉主题（3天学会盲拧魔方——没人会来请求这个）
  变体 B「骨架示例」  ：保留学科，挖空内容（数量具体、内容抽象）

本脚本对四个条件各跑两种用例，比较三项指标：

  1. 格式合规率（**跨学科用例**，排除「照抄/污染」这类会白送分的检查）
  2. 照抄程度（**同题用例**，算法化最长公共子串检测，不靠手写词表）
  3. 题干重复数（同题用例；Few-shot 若教了「一题反复用」的坏习惯，这里会露出来）

基线：current（现用示例）与 none（不用示例，合规率约 79.5%）。

用法::
    python eval_fewshot_variants.py -n 3
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from eval_planner import load_api_key
from eval_sprint import validate as validate_sprint
from prompt_builder import PromptLib
from quality_gate import copy_hits
from quality_gate import load_fewshot_refs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"
EXP_DIR = ROOT / "experiments"

EXCLUDED_CHECKS = ("照抄", "污染")

# 跨学科用例：请求学科与所有 Few-shot 都不同，专门测格式能力
CASE_CROSS = {"学科": "初中物理", "学习目标": "掌握欧姆定律，能计算串联电路中各处的电流与电压",
              "可用时限": "3天", "学生水平": "中等"}
# 同题用例：与现用 Few-shot 主题几乎一字不差，专门测照抄
CASE_SAME = {"学科": "初中数学", "学习目标": "能解一元一次方程应用题",
             "可用时限": "3天", "学生水平": "入门"}

STEM_RE = re.compile(r"^\s*M\s*\d+\s*[-–—]\s*\d+\s*(.+)$", re.M)


def duplicated_stems(raw: str) -> list[str]:
    """找出随堂练习里重复出现的题干（Few-shot 教「一题反复用」时会露馅）。"""
    stems = [s.strip() for s in STEM_RE.findall(raw)]
    seen: dict[str, int] = {}
    for s in stems:
        key = re.sub(r"\s+", "", s)[:40]
        seen[key] = seen.get(key, 0) + 1
    return [k for k, v in seen.items() if v > 1]


def load_variants(lib: PromptLib) -> dict:
    """把 experiments/ 下的变体注入 lib.fewshots，避免污染生产目录。"""
    variants: dict[str, object] = {"current": "teacher_sprint", "none": False}
    for f in sorted(EXP_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        name = data.get("name", f.stem)
        lib.fewshots[name] = data
        variants[f.stem] = name
    return variants


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("--only", default="", help="只跑指定变体，逗号分隔")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    variants = load_variants(lib)

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"模型: {args.model}  每条件每用例 {args.n} 次")
    print(f"变体: {list(variants)}\n")

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    rows = []
    for vname, fs in variants.items():
        if only and vname not in only:
            continue
        for cname, case in (("跨学科", CASE_CROSS), ("同题", CASE_SAME)):
            prompt = lib.build("teacher_sprint", case, fewshot=fs)
            refs = load_fewshot_refs(lib, fs) if isinstance(fs, str) else []

            ok_all = tot_all = 0
            hits_n: list[int] = []
            hits_max: list[int] = []
            dups: list[int] = []
            sizes: list[int] = []

            for i in range(1, args.n + 1):
                resp = client.chat.completions.create(
                    model=args.model, messages=[{"role": "user", "content": prompt}],
                    temperature=0.7)
                raw = resp.choices[0].message.content or ""
                (RUNS_DIR / f"variant_{vname}_{cname}_{stamp}_{i}.md").write_text(
                    raw, encoding="utf-8")

                checks = [c for c in validate_sprint(raw, {"limit_days": 3.0})
                          if not any(k in c[0] for k in EXCLUDED_CHECKS)]
                ok_all += sum(1 for _, ok, _ in checks if ok)
                tot_all += len(checks)

                h = copy_hits(raw, refs) if refs else []
                hits_n.append(len(h))
                hits_max.append(max((len(x) for x in h), default=0))
                dups.append(len(duplicated_stems(raw)))
                sizes.append(len(raw))

            rows.append({
                "variant": vname, "case": cname,
                "rate": ok_all / tot_all if tot_all else 0.0,
                "hits": sum(hits_n) / len(hits_n), "hit_max": max(hits_max),
                "dup": sum(dups) / len(dups), "size": sum(sizes) // len(sizes),
                "plen": len(prompt),
            })
            r = rows[-1]
            print(f"  {vname:22s} {cname:6s} 合规 {r['rate']:6.1%}  "
                  f"照抄 {r['hits']:4.1f}处/最长{r['hit_max']:3d}字  "
                  f"重复题干 {r['dup']:.1f}  正文 {r['size']:5d}  prompt {r['plen']:5d}")

    # ---- 对比表 ----
    print("\n" + "=" * 96)
    print("对比（目标：跨学科合规率高 + 同题照抄低 + 无重复题干）")
    print("=" * 96)
    print(f"  {'变体':24s} {'跨学科合规':>10s} {'同题合规':>9s} {'同题照抄(处)':>13s} "
          f"{'同题最长重合':>13s} {'重复题干':>9s}")
    verdicts = []
    for v in variants:
        cross = next(r for r in rows if r["variant"] == v and r["case"] == "跨学科")
        same = next(r for r in rows if r["variant"] == v and r["case"] == "同题")
        print(f"  {v:24s} {cross['rate']:>9.1%} {same['rate']:>9.1%} "
              f"{same['hits']:>13.1f} {same['hit_max']:>13d} {same['dup']:>9.1f}")
        verdicts.append((v, cross["rate"], same["hits"], same["hit_max"], same["dup"]))

    # ---- 结论 ----
    print("\n" + "=" * 96)
    print("结论判读")
    print("=" * 96)
    base = next(v for v in verdicts if v[0] == "none")
    print(f"  基线 none：合规 {base[1]:.1%}（无示例时的格式能力下限）")
    cur = next(v for v in verdicts if v[0] == "current")
    print(f"  基线 current：合规 {cur[1]:.1%}，同题照抄 {cur[2]:.1f} 处（最长 {cur[3]} 字）")
    for name, rate, hits, hmax, dup in verdicts:
        if name in ("none", "current"):
            continue
        keep_fmt = rate >= cur[1] - 0.05
        no_copy = hits <= max(cur[2] * 0.25, 1.0) and hmax < 25
        if keep_fmt and no_copy:
            verdict = "✅ 既保格式又消除抄袭 → 可用"
        elif not keep_fmt and no_copy:
            verdict = f"⚠️  不抄了但格式掉到 {rate:.1%}（低于基线）"
        elif keep_fmt and not no_copy:
            verdict = "⚠️  格式没问题但仍在抄"
        else:
            verdict = "❌ 两头都没做到"
        print(f"  {name:24s} {verdict}")

    print(f"\n样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
