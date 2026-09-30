#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骨架示例推广实验：把已验证的「骨架示例」推向其余四个模板

`teacher_sprint` 上已证明骨架示例能做到「格式不掉 + 照抄归零」。
本脚本对其余模板（teacher_planner / teacher / assistant / supervisor）
逐一对比「现用完整实例」vs「骨架示例」，只采用两者都过关的。

判据（两条件都必须满足才可推广）：
  1. 格式合规率不下降（跨学科用例，排除会白送分的「照抄/污染」检查）
  2. 同题用例不再复现完整实例的具体内容（对**完整实例**做照抄检测）
  3. 骨架条件额外检查：无 `<占位符>` 泄漏

用法::
    python eval_skeleton_rollout.py -n 2
    python eval_skeleton_rollout.py -n 3 --only teacher_planner,teacher
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from eval_generic import validate_assistant, validate_supervisor
from eval_planner import load_api_key
from eval_planner import validate as validate_planner
from eval_teacher import validate as validate_teacher
from prompt_builder import PromptLib
from quality_gate import copy_hits, load_fewshot_refs, placeholder_leaks

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"
EXP_DIR = ROOT / "experiments"
EXCLUDED = ("照抄", "污染")

CONFIGS = {
    "teacher_planner": dict(
        skeleton="planner_skeleton",
        cases={
            "跨学科": {"学科": "初中物理", "学习目标": "理解浮力的产生原因并能计算浮力大小",
                       "总周期": "4周", "每周可投入时间": "3课时", "学生水平": "中等"},
            # 同题用例：学科与知识点同示例，但**改一个应当改变内容的参数**（5周→2周）。
            # 若输出仍复现示例的 5 周方案，才是照抄证据。
            # 反之若把输入设成与示例完全相同，正确答案本来就相似，重合度不能当证据。
            "同题": {"学科": "初中生物",
                     "学习目标": "理解光合作用的原料、条件与产物，能写出反应式并解释影响因素",
                     "总周期": "2周", "每周可投入时间": "3课时", "学生水平": "中等"},
        },
        validate=lambda raw, case: validate_planner(raw, check_copy=False),
    ),
    "teacher": dict(
        skeleton="teacher_skeleton",
        cases={
            "跨学科": {"学科": "初中物理", "知识点": "欧姆定律", "学生水平": "基础",
                       "need_example": "true", "band": "基础"},
            # 同题：同知识点，但学生水平由基础改竞赛——档位不同，内容必须明显不同
            "同题": {"学科": "初中数学", "知识点": "提公因式法", "学生水平": "竞赛",
                     "need_example": "true", "band": "竞赛"},
        },
        validate=lambda raw, case: validate_teacher(raw, case),
    ),
    "assistant": dict(
        skeleton="assistant_skeleton",
        cases={
            "跨学科": {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
                       "薄弱点候选列表": "浮力计算、单位换算",
                       "错题": "一个边长 0.1 m 的正方体浸没在水中，求它受到的浮力。（g 取 10 N/kg）",
                       "学生答案": "F = ρgV = 1000 × 10 × 0.1 = 1000 N",
                       "参考答案": "V = 0.1³ = 0.001 m³，F = ρgV = 1000 × 10 × 0.001 = 10 N"},
            # 同题：同知识点，但给**另一道方程**。
            # 若输出里出现示例的 x²-5x+6=0 或 x=2/x=3，即为照抄。
            "同题": {"学科": "初中数学", "学生姓名": "小刚", "学生水平": "基础",
                     "薄弱点候选列表": "一元二次方程的求根",
                     "错题": "解方程 x² - 7x + 12 = 0",
                     "学生答案": "x = 4",
                     "参考答案": "x₁ = 3，x₂ = 4"},
        },
        validate=lambda raw, case: validate_assistant(raw),
    ),
    "supervisor": dict(
        skeleton="supervisor_skeleton",
        cases={
            "跨学科": {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
                       "学习数据": "本周做题 30 道，正确率 70%；已 2 天未登录",
                       "待复习知识点": "浮力"},
            # 同题：同学生，但学习数据完全不同——输出若复现 45 道/62%/3天未登录 即照抄
            "同题": {"学科": "初中数学", "学生姓名": "小明", "学生水平": "基础",
                     "学习数据": "本周做题 20 道，正确率 92%；连续打卡 7 天",
                     "待复习知识点": "因式分解"},
        },
        validate=lambda raw, case: validate_supervisor(raw, case),
    ),
}


def load_skeletons(lib: PromptLib) -> None:
    for cfg in CONFIGS.values():
        f = EXP_DIR / f"{cfg['skeleton']}.yaml"
        lib.fewshots[cfg["skeleton"]] = yaml.safe_load(f.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--cases", default="跨学科,同题")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    load_skeletons(lib)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    want_cases = {x.strip() for x in args.cases.split(",") if x.strip()}
    targets = [k for k in CONFIGS if not only or k in only]

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"模型: {args.model}  每条件每用例 {args.n} 次\n")

    results = []
    for name in targets:
        cfg = CONFIGS[name]
        # 照抄检测的参照物始终是**完整实例**——它才是「不该被复现的内容」
        concrete = load_fewshot_refs(lib, name)

        for cond, fs in (("完整实例", name), ("骨架示例", cfg["skeleton"])):
            rates = []
            for cname, case in cfg["cases"].items():
                if cname not in want_cases:
                    continue
                prompt = lib.build(name, case, fewshot=fs)
                ok = tot = 0
                hits_max: list[int] = []
                leaks = 0
                for i in range(1, args.n + 1):
                    resp = client.chat.completions.create(
                        model=args.model, messages=[{"role": "user", "content": prompt}],
                        temperature=0.7)
                    raw = resp.choices[0].message.content or ""
                    (RUNS_DIR / f"rollout_{name}_{cond}_{cname}_{stamp}_{i}.md").write_text(
                        raw, encoding="utf-8")
                    checks = [c for c in cfg["validate"](raw, case)
                              if not any(k in c[0] for k in EXCLUDED)]
                    ok += sum(1 for _, o, _ in checks if o)
                    tot += len(checks)
                    h = copy_hits(raw, concrete) if concrete else []
                    hits_max.append(max((len(x) for x in h), default=0))
                    leaks += len(placeholder_leaks(raw))
                rates.append((cname, ok / tot if tot else 0.0,
                              max(hits_max) if hits_max else 0, leaks))
            results.append((name, cond, rates))
            summary = "  ".join(
                f"{c}: 合规{r:.1%} 最长重合{h}字" + (f" 占位符泄漏{l}" if l else "")
                for c, r, h, l in rates)
            print(f"  {name:17s} {cond:6s} {summary}")

    # ---- 结论 ----
    print("\n" + "=" * 92)
    print("结论判读（跨学科合规率不降 + 同题最长重合降到 60 字以下 → 可推广）")
    print("=" * 92)
    for name in targets:
        cur = next(r for n_, c, r in results if n_ == name and c == "完整实例")
        sk = next(r for n_, c, r in results if n_ == name and c == "骨架示例")
        cur_cross = next((r for c, r, _, _ in cur if c == "跨学科"), None)
        sk_cross = next((r for c, r, _, _ in sk if c == "跨学科"), None)
        cur_same_h = next((h for c, _, h, _ in cur if c == "同题"), None)
        sk_same_h = next((h for c, _, h, _ in sk if c == "同题"), None)
        if cur_same_h is None or sk_same_h is None:
            print(f"  {name:17s} 只跑了部分用例，跳过判读")
            continue
        cur_cross = cur_cross if cur_cross is not None else 0.0
        sk_cross = sk_cross if sk_cross is not None else 0.0
        sk_leak = sum(l for _, _, _, l in sk)

        ok_fmt = sk_cross >= cur_cross - 0.02
        ok_copy = sk_same_h < 60
        ok_leak = sk_leak == 0
        if cur_same_h < 60 and ok_fmt and ok_leak:
            v = "⏸  现用实例本身就不抄（同题重合 <%d 字）→ 无需改动" % 60
        elif ok_fmt and ok_copy and ok_leak:
            v = "✅ 可推广（保持格式且消除抄袭）"
        else:
            v = "❌ 不可推广：" + "、".join(filter(None, [
                None if ok_fmt else f"格式降到 {sk_cross:.1%}（原 {cur_cross:.1%}）",
                None if ok_copy else f"骨架仍有重合 {sk_same_h} 字",
                None if ok_leak else f"占位符泄漏 {sk_leak} 处",
            ]))
        print(f"  {name:17s} 完整实例 合规{cur_cross:6.1%}/同题重合{cur_same_h:4d}字"
              f"  →  骨架 合规{sk_cross:6.1%}/同题重合{sk_same_h:4d}字   {v}")

    print(f"\n样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
