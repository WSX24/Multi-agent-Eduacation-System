#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用结构校验器 —— 给还没有专属校验器的模板（assistant / supervisor）用

这些模板的输出格式是「N 个编号模块」，没有 teacher_planner 那样的 YAML 结构，
所以只需校验：模块是否齐全、顺序是否正确、有无 LaTeX、有无 Few-shot 学科污染。

这不是完整的质量校验（不检查内容是否合理），但足以回答消融/变体实验关心的问题：
**换掉 Few-shot 之后，格式合规率会不会掉。**
"""
from __future__ import annotations

import re

LATEX = ["\\frac", "\\times", "\\div", "\\cdot", "\\[", "\\]", "\\(", "\\)", "\\sqrt"]


def split_by_names(raw: str, names: list[str]) -> tuple[list[str], dict[str, str]]:
    """按给定模块名定位分段（容忍带编号或不带编号两种写法）。"""
    marks: list[tuple[int, int, str]] = []
    for name in names:
        m = re.search(r"^[ \t]*(?:\d{1,2}\s*[.、]\s*)?【" + re.escape(name) + r"】", raw, re.M)
        if m:
            marks.append((m.start(), m.end(), name))
    marks.sort()
    bodies: dict[str, str] = {}
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(raw)
        bodies[name] = raw[end:stop].strip()
    return [n for _, _, n in marks], bodies


def make_section_validator(
    expected: list[str],
    *,
    contamination: list[str] | None = None,
    min_chars: int = 1,  # 只查非空：「置信度」的合法取值就是「高」这一个字
):
    """生成 ``validate(raw, case=None) -> [(检查项, 是否通过, 说明)]``。"""

    def validate(raw: str, case: dict | None = None) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append((name, ok, detail))

        order, bodies = split_by_names(raw, expected)
        missing = [s for s in expected if s not in bodies]
        add(f"模块齐全({len(expected)})", not missing, f"缺 {missing}" if missing else "")
        if missing:
            return checks

        idx = [order.index(s) for s in expected]
        add("模块顺序正确", idx == sorted(idx), f"实际 {order}")

        thin = [s for s in expected if len(bodies[s]) < min_chars]
        add("模块非空", not thin, f"过短或为空 {thin}")

        latex = [t for t in LATEX if t in raw]
        add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")

        if contamination:
            leak = [t for t in contamination if t in raw]
            add("无 Few-shot 学科污染", not leak, f"混入 {leak}" if leak else "")
        return checks

    return validate


# 各模板的模块清单，与 templates/*.yaml 的 output_format 保持一致
ASSISTANT_SECTIONS = ["得分", "答案判定", "错误分析", "知识点归因", "改进建议", "置信度"]
SUPERVISOR_SECTIONS = ["一句话问候", "数据回顾", "今日任务", "激励语", "复习提醒"]

validate_assistant = make_section_validator(
    ASSISTANT_SECTIONS,
    contamination=["一元二次方程", "x² - 5x + 6", "x = 2", "x = 3", "代回检验", "求根"],
)
validate_supervisor = make_section_validator(
    SUPERVISOR_SECTIONS,
    contamination=["小明", "45 道", "62%", "提公因式法", "整式乘法", "3 天未登录"],
)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("assistant 期望模块:", ASSISTANT_SECTIONS)
    print("supervisor 期望模块:", SUPERVISOR_SECTIONS)
    bad = ("1. 【得分】3/5\n2. 【答案判定】部分正确\n")
    print("残缺样本应报缺模块:", [n for n, ok, _ in validate_assistant(bad) if not ok])
