#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同题撞车探针：学生问的正好是 Few-shot 覆盖的那个知识点时，会不会照抄？

背景：M1（规划）做了「bio 同题用例」，发现同题时 100% 逐字节照抄 Few-shot。
但 M2 / M3 一直是用**别的学科**测的（生物/物理 vs 数学 Few-shot），
所以从没触发过撞车情况。本脚本专门补这个缺口。

用法::
    python probe_same_topic.py            # 跑两个探针
"""
from __future__ import annotations

import difflib
import glob
import re
import sys
from pathlib import Path

import yaml

from demo_course_flow import unit_variables
from eval_planner import load_api_key
from eval_unit import split_sections, validate as validate_unit
from eval_sprint import validate as validate_sprint
from prompt_builder import PromptLib
from quality_gate import QualityGate, load_fewshot_refs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# ---- Few-shot 里逐字出现的算式/句式，出现在输出里即为照抄铁证 ----
# 注意：不含「提公因式法」等知识点名——那正是本探针要求渲染的单元标题，
# 出现属正常。只认 Few-shot 里独有的具体算式/句式。
TEACHER_FEWSHOT_MARKERS = [
    "2x + 4", "3x(2x + 1)", "3(x + 1)", "6x² + 3x",
    "aₙ = a₁ + (n-1)d", "a₇ = 19", "首项 + (项数-1) 个公差",
]
SPRINT_FEWSHOT_MARKERS = [
    "圈出问句里要求的那个量", "「等于」就是等号", "含分母的复杂方程",
    "方案比较类压轴题", "某数的 3 倍加 5 等于 20", "甲仓库存粮",
    "长方形长比宽多", "本子 5 元", "系数化 1",
]


def _strip_markers(text: str, markers: list[str]) -> list[str]:
    return [m for m in markers if m in text]


def probe_unit(lib: PromptLib, client, model: str) -> None:
    """M2 同题探针：渲染一个正好叫「提公因式法」的数学单元。"""
    print("=" * 74)
    print("探针 1｜M2 单元渲染 × teacher Few-shot 同题（初中数学·提公因式法）")
    print("=" * 74)

    course = {
        "course": {"subject": "初中数学", "goal": "掌握因式分解",
                   "student_level": "基础", "total_weeks": 2, "weekly_hours": 3,
                   "mode": "long_term", "buffer_ratio": 0.15, "net_hour_budget": 5.0},
        "chapters": [
            {"id": "ch01", "order": 1, "title": "整式乘法", "estimated_hours": 2,
             "unlock": {"type": "none"},
             "units": [{"id": "ch01-u1", "title": "单项式乘多项式", "type": "新授",
                        "estimated_hours": 2, "homework": {"count": 4, "difficulty": "基础"},
                        "lesson_status": "pending"}],
             "assessment": {"type": "chapter_test", "difficulty": "基础",
                            "covers": ["ch01-u1"], "unlock_next": {"min_score": 60}}},
            {"id": "ch02", "order": 2, "title": "因式分解", "estimated_hours": 3,
             "unlock": {"type": "after_chapter", "chapter": "ch01", "min_score": 60},
             "units": [{"id": "ch02-u1", "title": "提公因式法", "type": "新授",
                        "estimated_hours": 3, "homework": {"count": 4, "difficulty": "基础"},
                        "lesson_status": "pending"}],
             "assessment": {"type": "chapter_test", "difficulty": "基础",
                            "covers": ["ch02-u1"], "unlock_next": {"min_score": 60}}},
        ],
    }
    prog = {"ch01": {"rate": 0.75, "weak": [], "units_done": ["ch01-u1"]}}
    # 走生产路径（fewshot=False）——M2 已根据消融实验删除 Few-shot
    prompt = lib.build("teacher_unit",
                       unit_variables(course, "ch02", 0, prog), fewshot=False)

    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], temperature=0.7)
    raw = resp.choices[0].message.content or ""
    (RUNS_DIR / "probe_unit_same_topic.md").write_text(raw, encoding="utf-8")

    _, bodies = split_sections(raw)
    few = yaml.safe_load((ROOT / "fewshots" / "teacher.yaml").read_text(encoding="utf-8"))
    ex1 = few["examples"][0]["output"]
    ex2 = few["examples"][1]["output"]

    hits = _strip_markers(raw, TEACHER_FEWSHOT_MARKERS)
    sim = max(
        difflib.SequenceMatcher(None, bodies.get("知识讲解", "") + bodies.get("典型例题", ""), ex1).ratio(),
        difflib.SequenceMatcher(None, bodies.get("知识讲解", "") + bodies.get("典型例题", ""), ex2).ratio(),
    )
    # 本探针渲染的就是数学单元，故不查污染；只看是否复制了 Few-shot 特有算式
    checks = validate_unit(raw, {"name": "same", "band": "mid", "rate": 0.75,
                                 "weak": [], "need_example": True})
    failed = [n for n, ok, _ in checks if not ok and "污染" not in n]

    print(f"  Few-shot 特有算式命中：{hits}")
    print(f"  讲解+例题 与 Few-shot 的最大相似度：{sim:.2f}")
    print(f"  格式校验：{'全绿' if not failed else failed}")
    print(f"  {'⚠️  疑似照抄' if (hits or sim > 0.5) else '✅ 未照抄'}\n")


def probe_sprint(lib: PromptLib, client, model: str) -> None:
    """M3 同题探针：请求与 sprint Few-shot 的输入几乎一字不差。"""
    print("=" * 74)
    print("探针 2｜M3 短周期速成 × sprint Few-shot 同题（初中数学·一元一次方程应用题）")
    print("=" * 74)

    prompt = lib.build("teacher_sprint", {
        "学科": "初中数学",
        "学习目标": "能解一元一次方程应用题",   # ← 与 Few-shot 输入仅一词之差
        "可用时限": "3天",
        "学生水平": "入门",
    })
    # 走生产闸门：格式校验 + 照抄检测，不过就回投重试
    refs = load_fewshot_refs(lib, "teacher_sprint")
    gate = QualityGate(client, model, max_retry=2)
    result = gate.generate(
        prompt,
        checks=lambda r: validate_sprint(r, {"limit_days": 3.0}),
        copy_refs=refs,
    )
    raw = result.raw
    (RUNS_DIR / "probe_sprint_same_topic.md").write_text(raw, encoding="utf-8")
    print("  " + result.report().replace(chr(10), chr(10) + "  "))

    few = yaml.safe_load((ROOT / "fewshots" / "teacher_sprint.yaml").read_text(encoding="utf-8"))
    ref = few["examples"][0]["output"]

    hits = _strip_markers(raw, SPRINT_FEWSHOT_MARKERS)
    sim = difflib.SequenceMatcher(None, raw, ref).ratio()
    failed = [n for n, ok, _ in validate_sprint(raw, {"limit_days": 3.0}) if not ok]

    print(f"  Few-shot 特有词句命中：{hits}")
    print(f"  与 Few-shot 全文相似度：{sim:.2f}")
    print(f"  格式校验：{'全绿' if not failed else failed}")
    print(f"  {'⚠️  疑似照抄' if (len(hits) >= 3 or sim > 0.5) else '✅ 未照抄'}\n")


def main() -> None:
    lib = PromptLib(ROOT)
    RUNS_DIR.mkdir(exist_ok=True)
    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1")
    model = "deepseek-chat"

    probe_unit(lib, client, model)
    probe_sprint(lib, client, model)

    print("=" * 74)
    print("说明：格式校验全绿 ≠ 内容原创。照抄出来的东西格式一定是合规的——")
    print("      这正是它危险的地方。判据看「Few-shot 特有词句命中」与「相似度」。")
    print("=" * 74)


if __name__ == "__main__":
    main()
