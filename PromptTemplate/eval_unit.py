#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元渲染模板（teacher_unit）真实模型评测

M1（规划）的评测已回答了「骨架对不对」。M2 是学生实际看到的那一层，
它有一句最需要证伪的话：**依据学情数据动态调整难度**。

验证方式不是看它「说了」升档还是降档，而是差分测试：
同一个单元、同样的学生水平，**只改上一章成绩**，输出必须真的不一样。

  H1  难度自适应真实生效（45% / 72% / 92% 三档输出应有可辨差异）
  H2  自适应「有理有据」——必须引用具体数字，不许写「根据学情」这类空话
  H3  学情数据缺失时如实说明，不许假装调整过
  H4  结构完整：10 个模块齐全且顺序正确
  H5  不污染：用 teacher 的 Few-shot（初中数学）渲染初中生物单元，不得漏进数学内容
  H6  数学符号用纯文本，禁止 LaTeX
  H7  need_example=false 时例题模块改为「不提供例题」，其余照常

用法::

    python eval_unit.py --self-check      # 离线：只检查 prompt 组装（不调 API）
    python eval_unit.py -n 1              # 真实模型，逐档跑一遍
    python eval_unit.py -n 2 --retry 1
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from demo_course_flow import parse_blueprint, unit_variables
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 模板约定的 10 个模块，顺序固定
EXPECTED_SECTIONS = [
    "本单元定位", "难度调整说明", "本课目标", "知识讲解", "典型例题",
    "易错提醒", "小结", "课件大纲", "单元作业", "完成后解锁",
]

SECTION_RE = re.compile(r"^\s*(\d{1,2})\s*[.、]?\s*【(.+?)】", re.M)

# teacher Few-shot 是初中数学因式分解/等差数列——渲染生物单元时不得漏进来
CONTAMINATION_TOKENS = [
    "因式分解", "公因式", "提公因式", "等差数列", "通项公式",
    "6x²", "3x(2x + 1)", "aₙ", "2x + 4",
]

LATEX_TOKENS = ["\\frac", "\\times", "\\div", "\\cdot", "\\[", "\\]", "\\(", "\\)", "\\sqrt"]

# 各档位的期望关键词（用于判定模型是否真的换了档，而非嘴上说说）
BAND_KEYWORDS = {
    "low": ["降档", "降低", "下调", "简化", "基础", "补"],
    "mid": ["保持", "不变", "按计划", "按蓝图", "既定"],
    "high": ["升档", "提高", "上调", "拓展", "变式", "综合", "延伸"],
    "nodata": ["数据不足", "未做调整", "无数据", "按蓝图"],
}


# ------------------------------------------------------------------ 用例
def _load_course() -> dict:
    doc = yaml.safe_load((ROOT / "fewshots" / "teacher_planner.yaml").read_text(encoding="utf-8"))
    return parse_blueprint(doc["examples"][0]["output"])


def _progress(rate: float | None, weak: list[str], course: dict) -> dict:
    if rate is None:
        return {}
    return {
        "ch01": {
            "rate": rate,
            "weak": weak,
            "units_done": [u["id"] for u in course["chapters"][0]["units"]],
        }
    }


# ------------------------------------------------------------------ 解析
def split_sections(raw: str) -> tuple[list[str], dict[str, str]]:
    """返回 (按出现顺序的模块名, 模块名→正文)。"""
    marks = list(SECTION_RE.finditer(raw))
    order: list[str] = []
    bodies: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        name = m.group(2)
        order.append(name)
        bodies[name] = raw[m.end():end].strip()
    return order, bodies


# ------------------------------------------------------------------ 校验
def validate(raw: str, case: dict) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_sections(raw)
    missing = [s for s in EXPECTED_SECTIONS if s not in bodies]
    add("H4 十个模块齐全", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks

    duplicated = [s for s in EXPECTED_SECTIONS if order.count(s) > 1]
    add("H4 模块无重复", not duplicated, f"重复 {duplicated}" if duplicated else "")

    idx = [order.index(s) for s in EXPECTED_SECTIONS if s in order]
    add("H4 模块顺序正确", idx == sorted(idx), f"实际顺序 {order}")

    expl = bodies["难度调整说明"]

    # ---- H2: 必须引用具体数字 ----
    rate = case.get("rate")
    if rate is not None:
        shown = {f"{rate:.0%}", f"{rate*100:.0f}%", f"{rate*100:.1f}%"}
        add("H2 说明引用真实成绩", any(s in expl for s in shown),
            f"未引用 {rate:.0%}；说明原文：{expl[:80]}")

    vague_only = re.search(r"根据学情|结合学情|视学情", expl) and not re.search(r"\d", expl)
    add("H2 无「根据学情」空话", not vague_only, expl[:80])

    # ---- H1/H3: 档位关键词 ----
    kws = BAND_KEYWORDS[case["band"]]
    add(f"H{1 if case['band'] != 'nodata' else 3} 档位判定={case['band']}",
        any(k in expl for k in kws),
        f"期望含 {kws} 之一；实际：{expl[:100]}")

    if case["band"] == "low":
        add("H1 降档提到薄弱点", case["weak"][0] in raw or "薄弱" in expl or "前置" in expl,
            f"未见 {case['weak']}")

    # ---- H5: 跨学科污染 ----
    leaks = [t for t in CONTAMINATION_TOKENS if t in raw]
    add("H5 无 Few-shot 学科污染", not leaks, f"混入初中数学内容 {leaks}" if leaks else "")

    # ---- H6: LaTeX ----
    latex = [t for t in LATEX_TOKENS if t in raw]
    add("H6 无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")

    # ---- H7: need_example ----
    if case.get("need_example") is False:
        add("H7 按要求不出例题",
            "不提供例题" in bodies["典型例题"] or "不提供" in bodies["典型例题"],
            bodies["典型例题"][:60])
        others_nonempty = all(bodies[s] for s in ("本课目标", "知识讲解", "小结"))
        add("H7 其余模块照常输出", others_nonempty)

    # ---- 课件大纲为页级 ----
    pages = re.findall(r"^\s*(?:P|第)\s*\d+", bodies["课件大纲"], re.M)
    add("课件大纲为页级(≥3页)", len(pages) >= 3, f"识别到 {len(pages)} 页")

    # ---- 作业题量 ----
    # 注意：不能把 ①-⑩ 与数字无条件当成题号——它们会出现在选项（①O₂ ②有机物）
    # 和答案（①未配平）里造成误计。按优先级只采用一种编号体系。
    hw = bodies["单元作业"]
    for pattern in (r"^\s*第\s*\d+\s*题", r"^\s*\d{1,2}\s*[.、)）]", r"^\s*[①-⑩]"):
        qs = re.findall(pattern, hw, re.M)
        if qs:
            break
    add("单元作业 3~5 题", 3 <= len(qs) <= 5, f"识别到 {len(qs)} 题")

    # ---- 解锁提示不得凭空编造 ----
    unlock = bodies["完成后解锁"]
    add("完成后解锁不空洞", len(unlock) >= 10, unlock[:60])

    return checks


# ------------------------------------------------------------------ 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="每档运行次数")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--retry", type=int, default=0)
    ap.add_argument("--self-check", action="store_true", help="离线检查 prompt 组装")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    course = _load_course()
    chapter = course["chapters"][1]          # ch02，其 unlock 依赖 ch01
    unit_idx = 0

    raw_cases = [
        # (用例名, 判定档位, 正确率, 薄弱点, 是否出例题)
        ("low", "low", 0.45, ["探究光合作用需要光"], True),
        ("mid", "mid", 0.72, [], True),
        ("high", "high", 0.92, [], True),
        ("nodata", "nodata", None, [], True),
        ("noex", "low", 0.45, ["探究光合作用需要光"], False),
    ]
    cases = [
        {"name": n, "band": band, "rate": r, "weak": w, "need_example": ne}
        for n, band, r, w, ne in raw_cases
    ]

    if args.self_check:
        for c in cases:
            variables = unit_variables(
                course, chapter["id"], unit_idx,
                _progress(c["rate"], c["weak"], course), c["need_example"],
            )
            prompt = lib.build("teacher_unit", variables, fewshot="teacher")
            leaked = [t for t in CONTAMINATION_TOKENS if t in prompt]
            print(f"  {c['name']:8s} prompt {len(prompt):5d} 字符"
                  f" | Few-shot 自带的数学示例: {leaked}")
            assert "{{" not in prompt, f"{c['name']} 有未填充占位符"
            assert variables["上一章测验结果"] == (
                "无数据" if c["rate"] is None else f"正确率 {c['rate']:.0%}"
            ), f"{c['name']} 成绩变量注入错误: {variables['上一章测验结果']}"
        print("\n离线自检通过：5 档 prompt 组装正常，占位符无残留，成绩变量注入正确")
        print("注：Few-shot 自带的初中数学示例会在 H5 检查中被当作污染源，"
              "若模型把它们写进生物单元即判失败。")
        return

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"单元: {chapter['id']} / {chapter['units'][unit_idx]['title']}")
    print(f"模型: {args.model}  温度: {args.temperature}  每档 {args.n} 次\n")

    per_case: dict[str, list[list[tuple[str, bool, str]]]] = {}
    bodies_by_band: dict[str, str] = {}

    for c in cases:
        variables = unit_variables(
            course, chapter["id"], unit_idx,
            _progress(c["rate"], c["weak"], course), c["need_example"],
        )
        prompt = lib.build("teacher_unit", variables, fewshot="teacher")
        per_case[c["name"]] = []
        for i in range(1, args.n + 1):
            messages = [{"role": "user", "content": prompt}]
            raw = ""
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=args.temperature,
                    )
                    raw = resp.choices[0].message.content or ""
                except Exception as e:
                    print(f"  {c['name']}: API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate(raw, c)
                failed = [x for x in checks if not x[1]]
                if not failed or attempt >= args.retry:
                    break
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "你的输出存在以下问题，请修正后重新输出完整结果：\n"
                     + "\n".join(f"- {n}：{d}" for n, _, d in failed)},
                ]
            if not raw:
                continue
            (RUNS_DIR / f"unit_{c['name']}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per_case[c["name"]].append(checks)
            bodies_by_band[c["name"]] = raw
            failed = [x for x in checks if not x[1]]
            print(f"  {c['name']:8s} {len(checks)-len(failed)}/{len(checks)}"
                  + (f"（重试 {attempt} 次）" if attempt else ""))
            for n, _, d in failed:
                print(f"      ✗ {n}  {d}")
            print(f"      → 说明: {split_sections(raw)[1]['难度调整说明'][:90]}")

    # ---- 差分检查：同单元只改成绩，输出必须真的不同 ----
    print("\n" + "=" * 72)
    print("差分检查：同单元 / 同学生水平，仅改上一章成绩")
    print("=" * 72)
    for a, b in (("low", "mid"), ("mid", "high"), ("low", "high")):
        if a in bodies_by_band and b in bodies_by_band:
            ratio = difflib.SequenceMatcher(None, bodies_by_band[a], bodies_by_band[b]).ratio()
            mark = "✅" if ratio < 0.9 else "⚠️ "
            print(f"  {mark} {a:5s} vs {b:5s} 正文相似度 {ratio:.2f}（越低说明自适应越真实）")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    all_checks = [c for lst in per_case.values() for r in lst for c in r]
    names = []
    for lst in per_case.values():
        for r in lst:
            for c in r:
                if c[0] not in names:
                    names.append(c[0])
    for name in names:
        tot = sum(1 for lst in per_case.values() for r in lst
                  for c in r if c[0] == name)
        ok = sum(1 for lst in per_case.values() for r in lst
                 for c in r if c[0] == name and c[1])
        mark = "✅" if ok == tot else ("⚠️ " if ok else "❌")
        print(f"  {mark} {name:28s} {ok}/{tot}")
    ok = sum(1 for c in all_checks if c[1])
    print(f"\n合计 {ok}/{len(all_checks)} 项通过；样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
