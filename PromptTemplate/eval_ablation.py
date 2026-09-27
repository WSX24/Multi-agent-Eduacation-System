#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Few-shot 消融实验：它到底在不在挣工资？

质疑：既然 Few-shot 会导致「换数字抄袭」甚至抄出无解题，那不如全部去掉。

本脚本用数据回答，不靠观点。对三个模板各跑「带 Few-shot / 不带 Few-shot」两组，
比较**格式合规率**（用既有校验器）与产出规模：

  - 若去掉后合规率不掉 → Few-shot 无必要，应删（抄袭风险白担）
  - 若去掉后合规率崩   → Few-shot 在承担「格式规范」职能，应保留并解决抄袭

注意：本实验全部使用**跨学科用例**（请求学科 ≠ Few-shot 学科），
否则抄袭会污染对比结果——那属于另一个问题（同题撞车），已由 probe_same_topic.py 单独测。

用法::
    python eval_ablation.py -n 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from demo_course_flow import parse_blueprint, unit_variables
from eval_planner import CASES as PLANNER_CASES
from eval_planner import load_api_key
from eval_planner import validate as validate_planner
from eval_sprint import CASES as SPRINT_CASES
from eval_sprint import validate as validate_sprint
from eval_teacher import CASES as TEACHER_CASES
from eval_teacher import validate as validate_teacher
from eval_unit import validate as validate_unit
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"


def build_teacher(lib: PromptLib, fewshot):
    case = TEACHER_CASES["基础"]
    prompt = lib.build("teacher", case, fewshot=fewshot)
    return prompt, lambda r: validate_teacher(r, case)


def build_planner(lib: PromptLib, fewshot):
    prompt = lib.build("teacher_planner", PLANNER_CASES["math"], fewshot=fewshot)
    return prompt, validate_planner


def build_unit(lib: PromptLib, fewshot):
    doc = yaml.safe_load((ROOT / "fewshots" / "teacher_planner.yaml").read_text(encoding="utf-8"))
    course = parse_blueprint(doc["examples"][0]["output"])
    chapter = course["chapters"][1]
    prog = {"ch01": {"rate": 0.72, "weak": [], "units_done": []}}
    prompt = lib.build("teacher_unit", unit_variables(course, chapter["id"], 0, prog),
                       fewshot=fewshot)
    case = {"name": "ab", "band": "mid", "rate": 0.72, "weak": [], "need_example": True}
    return prompt, lambda r: validate_unit(r, case)


def build_sprint(lib: PromptLib, fewshot):
    prompt = lib.build("teacher_sprint", SPRINT_CASES["d3"], fewshot=fewshot)
    return prompt, lambda r: validate_sprint(r, {"limit_days": 3.0})


TARGETS = [
    ("teacher_planner", "规划 YAML 蓝图", build_planner, "teacher_planner"),
    ("teacher_unit", "单元渲染 10 模块", build_unit, "teacher"),
    ("teacher_sprint", "速成 6 模块", build_sprint, "teacher_sprint"),
    ("teacher", "讲授 5 模块", build_teacher, "teacher"),
]

# 不带 Few-shot 时，「照抄/污染」类检查会白送分，必须排除才能公平对比格式能力
EXCLUDED_CHECKS = ("照抄", "污染")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="每种条件运行次数")
    ap.add_argument("--only", default="", help="只跑指定模板，逗号分隔")
    ap.add_argument("--conds", default="with,without",
                    help="跑哪些条件：with / without / auto（auto=同名 fewshot）")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"模型: {args.model}  每种条件 {args.n} 次\n")
    print("说明：全部使用跨学科用例，避免抄袭污染对比。\n")

    cond_specs = []
    for c in args.conds.split(","):
        c = c.strip()
        if c == "with":
            cond_specs.append(("带 Few-shot(指定)", "__named__"))
        elif c == "auto":
            cond_specs.append(("带 Few-shot(同名)", True))
        elif c == "without":
            cond_specs.append(("不带 Few-shot", False))

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    targets = [t for t in TARGETS if not only or t[0] in only]

    rows = []
    for name, label, builder, fs_name in targets:
        for cond, fs in cond_specs:
            real_fs = fs_name if fs == "__named__" else fs
            prompt, validator = builder(lib, real_fs)
            assert ("【示例输入】" in prompt) == bool(real_fs), f"{name}/{cond} 示例拼接状态不符"

            passes, totals, sizes = 0, 0, []
            for i in range(1, args.n + 1):
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                )
                raw = resp.choices[0].message.content or ""
                tag = {"不带 Few-shot": "without", "带 Few-shot(同名)": "auto"}.get(cond, "with")
                (RUNS_DIR / f"ablation_{name}_{tag}_{stamp}_{i}.md").write_text(
                    raw, encoding="utf-8")
                checks = [c for c in validator(raw)
                          if not any(k in c[0] for k in EXCLUDED_CHECKS)]
                p = sum(1 for _, ok, _ in checks if ok)
                if p < len(checks):
                    bad = [n for n, ok, _ in checks if not ok]
                    print(f"    {name}/{cond} #{i} 失败项: {bad}")
                passes += p
                totals += len(checks)
                sizes.append(len(raw))
            rows.append((name, label, cond, passes, totals, sum(sizes) // max(len(sizes), 1),
                         len(prompt)))

    print("\n" + "=" * 92)
    print("消融结果（跨学科用例）")
    print("=" * 92)
    print(f"  {'模板':18s} {'任务':16s} {'条件':14s} {'合规率':>9s} {'平均产出':>9s} {'prompt':>8s}")
    for name, label, cond, p, t, sz, plen in rows:
        rate = p / t if t else 0
        mark = "✅" if rate == 1 else ("⚠️ " if rate >= 0.9 else "❌")
        print(f"  {name:18s} {label:16s} {cond:14s} {mark} {rate:6.1%} {sz:>9d} {plen:>8d}")

    print("\n" + "=" * 92)
    print("结论判读")
    print("=" * 92)
    for name, label, _builder, _fs in targets:
        group = [r for r in rows if r[0] == name]
        base = next((r for r in group if r[2] == "不带 Few-shot"), None)
        if not base:
            continue
        dwo = base[3] / base[4]
        for r in group:
            if r is base:
                continue
            dw = r[3] / r[4]
            if dw - dwo > 0.02:
                verdict = f"提升格式合规 {dw - dwo:+.1%} → 在挣工资"
            elif dw - dwo < -0.02:
                verdict = f"反而更差 {dw - dwo:+.1%}"
            else:
                verdict = "对格式无贡献 → 可删（白担抄袭风险）"
            print(f"  {name:18s} {r[2]:16s} {dw:5.1%} vs 不带 {dwo:5.1%}  → {verdict}")

    print(f"\n样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
