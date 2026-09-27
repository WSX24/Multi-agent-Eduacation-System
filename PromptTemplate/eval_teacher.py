#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讲授者模板（teacher）真实模型评测

这是最早写的模板，也是唯一从未被真实验证过的：我修过它的渲染 bug
（``branch_rule`` 曾被 prompt_builder 静默丢弃），但**从未验证修复后分支是否真的生效**。

  H1  条件分支真实生效：入门 / 基础 / 进阶 / 竞赛 四档输出应有实质差异
  H2  need_example=false 时例题模块改写，其余照常
  H3  5 个模块齐全且顺序正确
  H4  无 LaTeX 记号
  H5  无 Few-shot 学科污染（用初中物理提问，Few-shot 是初中数学）
  H6  小结确实是 3 句话

核心是差分测试：同一个知识点、只改学生水平，看输出是否真的按 branch_rule 变化。

用法::
    python eval_teacher.py --self-check
    python eval_teacher.py -n 2
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

EXPECTED = ["本课目标", "知识讲解", "典型例题", "易错提醒", "小结"]

# teacher Few-shot 是初中数学因式分解/等差数列；用初中物理提问时不得漏进来
CONTAMINATION = [
    "提公因式法", "公因式", "2x + 4", "6x² + 3x", "3x(2x + 1)", "3(x + 1)",
    "等差数列", "通项公式", "aₙ", "a₇", "首项",
]
LATEX = ["\\frac", "\\times", "\\div", "\\cdot", "\\[", "\\]", "\\(", "\\)", "\\sqrt"]

# branch_rule 要求各档位应出现的特征词（None = 不约束）
# 档位判据：入门看是否通俗化，进阶/竞赛看例题是否升级为多步综合题。
# 注意：不要用「综合/陷阱/难题」这类关键词——实测模型是**示范**高阶难度而非**标注**它，
# 关键词判据会误判（第 7 次尺子事故）。改用可观测的步骤数。
BAND_EXPECT = {
    "入门": {"must_any": ["比喻", "好比", "就像", "相当于", "生活", "通俗", "简单"],
             "must_not": ["竞赛", "跨知识点综合", "陷阱"], "min_steps": 0},
    "基础": {"must_any": [], "must_not": [], "min_steps": 0},
    # 注意：步骤数只是「例题做了多步演示」的低门槛，**不是难度代理**——
    # 实测竞赛档 3 步、进阶档 8 步，并不递增。真正证明分支生效的是
    # main() 里的差分断言（四档两两相似度均 < 0.8）。
    "进阶": {"must_any": [], "must_not": [], "min_steps": 2},
    "竞赛": {"must_any": [], "must_not": [], "min_steps": 2},
}


def example_steps(bodies: dict) -> int:
    """统计【典型例题】的解题步骤数，作为题目复杂度的可观测代理。"""
    ex = bodies.get("典型例题", "")
    n = len(re.findall(r"第\s*[一二三四五六七八九十\d]+\s*步", ex))
    if n:
        return n
    n = len(re.findall(r"^\s*\d+\s*[.、)]", ex, re.M))
    if n:
        return n
    return len(re.findall(r"[。；]", ex))

CASES = {
    "入门": {"学科": "初中物理", "知识点": "欧姆定律", "学生水平": "入门", "need_example": "true", "band": "入门"},
    "基础": {"学科": "初中物理", "知识点": "欧姆定律", "学生水平": "基础", "need_example": "true", "band": "基础"},
    "进阶": {"学科": "初中物理", "知识点": "欧姆定律", "学生水平": "进阶", "need_example": "true", "band": "进阶"},
    "竞赛": {"学科": "初中物理", "知识点": "欧姆定律", "学生水平": "竞赛", "need_example": "true", "band": "竞赛"},
    "noex": {"学科": "初中物理", "知识点": "欧姆定律", "学生水平": "基础", "need_example": "false", "band": "基础"},
}

SECTION_RE = re.compile(r"^\s*(?:\d{1,2}\s*[.、]\s*)?【(.+?)】", re.M)


def split_sections(raw: str) -> tuple[list[str], dict[str, str]]:
    """按 EXPECTED 的名字定位分段，容忍带编号或不带编号。"""
    marks: list[tuple[int, int, str]] = []
    for name in EXPECTED:
        m = re.search(r"^[ \t]*(?:\d{1,2}\s*[.、]\s*)?【" + re.escape(name) + r"】", raw, re.M)
        if m:
            marks.append((m.start(), m.end(), name))
    marks.sort()
    bodies: dict[str, str] = {}
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(raw)
        bodies[name] = raw[end:stop].strip()
    return [n for _, _, n in marks], bodies


def validate(raw: str, case: dict, *, check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_sections(raw)
    missing = [s for s in EXPECTED if s not in bodies]
    add("H3 五个模块齐全", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks
    idx = [order.index(s) for s in EXPECTED]
    add("H3 模块顺序正确", idx == sorted(idx), f"实际 {order}")

    # ---- H6 小结 3 句话 ----
    n_sent = len(re.findall(r"[。！？]", bodies["小结"])) or 1
    add("H6 小结为 3 句话", 2 <= n_sent <= 4, f"识别到 {n_sent} 句")

    # ---- 目标只有一句 ----
    n_goal = len(re.findall(r"[。！？]", bodies["本课目标"])) or 1
    add("本课目标为一句话", n_goal <= 2, f"识别到 {n_goal} 句")

    # ---- H1 分支特征 ----
    spec = BAND_EXPECT[case["band"]]
    full = raw
    if spec["must_any"]:
        add(f"H1 档位特征={case['band']}",
            any(k in full for k in spec["must_any"]),
            f"期望含 {spec['must_any']} 之一")
    if spec["must_not"]:
        hit = [k for k in spec["must_not"] if k in full]
        add(f"H1 档位不越界={case['band']}", not hit, f"入门档出现 {hit}")
    if spec["min_steps"]:
        st = example_steps(bodies)
        add(f"H1 例题为多步演示={case['band']}", st >= spec["min_steps"],
            f"例题仅 {st} 步")

    # ---- H2 need_example ----
    if case["need_example"] == "false":
        add("H2 按要求不出例题", "不提供例题" in bodies["典型例题"], bodies["典型例题"][:60])
        add("H2 其余模块照常", all(bodies[s] for s in ("本课目标", "知识讲解", "小结")))

    # ---- H4 / H5 ----
    latex = [t for t in LATEX if t in raw]
    add("H4 无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in CONTAMINATION if t in raw]
        add("H5 无 Few-shot 学科污染", not leak, f"混入 {leak}" if leak else "")

    return checks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--retry", type=int, default=0)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    lib = PromptLib(ROOT)

    if args.self_check:
        doc = yaml.safe_load((ROOT / "fewshots" / "teacher.yaml").read_text(encoding="utf-8"))
        # Few-shot 自身：它是数学内容，故跳过污染检查
        ex = doc["examples"][0]["output"]
        bad = [(n, d) for n, ok, d in
               validate(ex, {"band": "基础", "need_example": "true"}, check_contamination=False)
               if not ok]
        print("Few-shot 自检:", "全绿" if not bad else bad)
        for name in CASES:
            p = lib.build("teacher", CASES[name], fewshot=True)
            assert "{{" not in p, f"{name} 有未填充占位符"
        print("prompt 组装：5 档全部正常，占位符无残留")
        return

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"模型: {args.model}  每档 {args.n} 次\n")

    per: dict[str, list[list[tuple[str, bool, str]]]] = {}
    bodies_by_band: dict[str, str] = {}

    for name, case in CASES.items():
        prompt = lib.build("teacher", case, fewshot=True)
        per[name] = []
        for i in range(1, args.n + 1):
            messages = [{"role": "user", "content": prompt}]
            raw = ""
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=0.7)
                    raw = resp.choices[0].message.content or ""
                except Exception as e:
                    print(f"  {name}: API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate(raw, case)
                failed = [x for x in checks if not x[1]]
                if not failed or attempt >= args.retry:
                    break
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "你的输出存在以下问题，请修正后重新输出完整结果：\n"
                     + "\n".join(f"- {x[0]}：{x[2]}" for x in failed)},
                ]
            if not raw:
                continue
            (RUNS_DIR / f"teacher_{name}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per[name].append(checks)
            bodies_by_band[name] = raw
            failed = [x for x in checks if not x[1]]
            print(f"  {name:5s} {len(checks)-len(failed)}/{len(checks)}"
                  + (f"（重试 {attempt} 次）" if attempt else "")
                  + f"  正文 {len(raw)} 字符")
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    print("\n" + "=" * 72)
    print("差分检查：同知识点 / 只改学生水平（验证 branch_rule 是否真生效）")
    print("=" * 72)
    for a, b in (("入门", "基础"), ("基础", "进阶"), ("进阶", "竞赛"), ("入门", "竞赛")):
        if a in bodies_by_band and b in bodies_by_band:
            r = difflib.SequenceMatcher(None, bodies_by_band[a], bodies_by_band[b]).ratio()
            print(f"  {'✅' if r < 0.8 else '⚠️ '} {a:4s} vs {b:4s} 相似度 {r:.2f}")
    print("")
    print("  各档例题步骤数（仅供参考，非难度代理）：")
    for n_ in ("入门", "基础", "进阶", "竞赛"):
        if n_ in bodies_by_band:
            _, bd = split_sections(bodies_by_band[n_])
            print(f"    {n_:4s} {example_steps(bd):>2d} 步   正文 {len(bodies_by_band[n_]):>5d} 字符")

    diff_fail = []
    for a, b in (("入门", "基础"), ("基础", "进阶"), ("进阶", "竞赛"), ("入门", "竞赛")):
        if a in bodies_by_band and b in bodies_by_band:
            r = difflib.SequenceMatcher(None, bodies_by_band[a], bodies_by_band[b]).ratio()
            if r >= 0.8:
                diff_fail.append(a + "/" + b + "=" + format(r, ".2f"))
    print("  " + ("OK " if not diff_fail else "WARN ") + "H1 四档输出互不相似(<0.8) " + str(diff_fail))
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    allc = [c for lst in per.values() for r in lst for c in r]
    seen: list[str] = []
    for lst in per.values():
        for r in lst:
            for c in r:
                if c[0] not in seen:
                    seen.append(c[0])
    for n_ in seen:
        tot = sum(1 for lst in per.values() for r in lst for c in r if c[0] == n_)
        ok = sum(1 for lst in per.values() for r in lst for c in r if c[0] == n_ and c[1])
        mark = "✅" if ok == tot else ("⚠️ " if ok else "❌")
        print(f"  {mark} {n_:28s} {ok}/{tot}")
    ok = sum(1 for c in allc if c[1])
    print(f"\n合计 {ok}/{len(allc)} 项通过；样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
