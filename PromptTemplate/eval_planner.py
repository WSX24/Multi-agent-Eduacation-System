#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规划模板（teacher_planner）真实模型评测

目的：验证 M1 的输出契约在真实模型下站不站得住。核心是两条未被验证过的假设：

  H1  规划期不出内容 —— 模型不会"顺手"把讲解/例题写出来
  H2  YAML 不含省略写法，且能被 yaml.safe_load 解析

外加一组结构化校验（DAG 完整性、工期一致性、单元粒度、测验覆盖）。

用法::

    python eval_planner.py                 # 默认跑 3 次
    python eval_planner.py -n 5            # 跑 5 次
    python eval_planner.py --model deepseek-chat
    python eval_planner.py --dry-run       # 只打印将被发送的 prompt，不调 API

Key 来源：环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY，
或回退到 ../LangChain开发/.env。脚本绝不会打印 Key。
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 评测用例
# ⚠️ bio 与 Few-shot 示例同题，是「照抄」的回归用例；
#    math / physics 与示例不同学科，测模型是否真在规划。
CASES = {
    "bio": {
        "学科": "初中生物",
        "学习目标": "理解光合作用的原料、条件与产物，能写出反应式并解释影响因素",
        "总周期": "5周",
        "每周可投入时间": "3课时",
        "学生水平": "中等",
    },
    "math": {
        "学科": "初中数学",
        "学习目标": "掌握一元二次方程的解法并能解应用题",
        "总周期": "4周",
        "每周可投入时间": "4课时",
        "学生水平": "基础",
    },
    "physics": {
        "学科": "初中物理",
        "学习目标": "理解浮力的产生原因，能用人称公式计算浮力并判断物体浮沉",
        "总周期": "6周",
        "每周可投入时间": "3课时",
        "学生水平": "中等",
    },
}


# ------------------------------------------------------------------ Key
def load_api_key() -> str | None:
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "Deepseek_test1"):
        if os.environ.get(name):
            return os.environ[name]
    env_file = ROOT.parent / "LangChain开发" / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    return None


# ------------------------------------------------------------- 校验器
STUDENT_VIEW_HEADERS = ["【学习总览】", "【阶段地图】", "【时间安排】", "【解锁规则】", "【给你的话】"]
# 出现这些即视为模型在规划期偷跑了教学内容
CONTENT_LEAK_MARKERS = ["【知识讲解】", "【典型例题】", "【解题过程】", "【易错提醒】", "【课件大纲】"]
ELISION_PATTERNS = [r"\.\.\.", r"\[\s*\.\.\.\s*\]", r"\{\s*\.\.\.\s*\}", r"…", r"等等", r"（略）", r"\(略\)"]


def split_blueprint(raw: str) -> tuple[str, str]:
    """切分学生视图与蓝图段。容忍模型用 ```yaml 代码块包裹。"""
    idx = raw.find("course:")
    if idx < 0:
        return raw, ""
    view, blueprint = raw[:idx], raw[idx:]
    blueprint = re.sub(r"^```[a-zA-Z]*\s*", "", blueprint.strip())
    blueprint = re.sub(r"```\s*$", "", blueprint.strip())
    return view, blueprint


def _fewshot_unit_titles() -> set[str]:
    """Few-shot 示例里出现过的单元标题，用于检测模型是否照抄。"""
    doc = yaml.safe_load((ROOT / "fewshots" / "teacher_planner.yaml").read_text(encoding="utf-8"))
    out = doc["examples"][0]["output"]
    bp = out[out.index("course:"):]
    data = yaml.safe_load(bp)
    return {u["title"] for c in data["chapters"] for u in c["units"]}


def validate(raw: str, *, check_copy: bool = True) -> list[tuple[str, bool, str]]:
    """返回 [(检查项, 是否通过, 说明)]。

    check_copy=False 用于校验 Few-shot 自身（拿它对照自己必然是 100% 重合）。
    """
    checks: list[tuple[str, bool, str]] = []
    view, blueprint = split_blueprint(raw)

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # ---- H2: 蓝图存在且可解析 ----
    if not blueprint:
        add("蓝图段存在", False, "未找到 'course:' 开头的蓝图")
        return checks
    add("蓝图段存在", True, f"{len(blueprint)} 字符")

    add("H2 无省略写法", not any(re.search(p, blueprint) for p in ELISION_PATTERNS),
        next((p for p in ELISION_PATTERNS if re.search(p, blueprint)), ""))

    try:
        course = yaml.safe_load(blueprint)
        parsed = isinstance(course, dict) and "chapters" in course
    except yaml.YAMLError as e:
        add("H2 YAML 可解析", False, str(e).splitlines()[0])
        return checks
    add("H2 YAML 可解析", parsed)
    if not parsed:
        return checks

    chapters = course["chapters"]
    meta = course.get("course", {})

    # ---- H1: 规划期不出内容 ----
    leaks = [m for m in CONTENT_LEAK_MARKERS if m in view or m in blueprint]
    add("H1 规划期无内容泄漏", not leaks, f"正文出现 {leaks}" if leaks else "")
    add("H1 学生视图简短", len(view) < 1200, f"{len(view)} 字符")
    hit = [h for h in STUDENT_VIEW_HEADERS if h in view]
    add("学生视图 5 小节齐全", len(hit) >= 4, f"{len(hit)}/5")

    statuses = {u.get("lesson_status") for c in chapters for u in c.get("units", [])}
    add("所有 lesson_status=pending", statuses == {"pending"}, f"实际 {statuses}")

    # ---- 结构完整性 ----
    add("course 元数据齐全",
        all(k in meta for k in ("subject", "goal", "student_level", "total_weeks", "weekly_hours")),
        f"缺 {[k for k in ('subject','goal','student_level','total_weeks','weekly_hours') if k not in meta]}")

    ids_ok, id_detail = True, ""
    for i, ch in enumerate(chapters, 1):
        if ch.get("id") != f"ch{i:02d}":
            ids_ok, id_detail = False, f"第{i}章 id={ch.get('id')} 不是 ch{i:02d}"
            break
        for j, u in enumerate(ch.get("units", []), 1):
            if u.get("id") != f"ch{i:02d}-u{j}":
                ids_ok, id_detail = False, f"单元 id={u.get('id')} 不是 ch{i:02d}-u{j}"
                break
    add("章/单元 id 命名规范", ids_ok, id_detail)

    ch_ids = [c.get("id") for c in chapters]
    first_unlock_ok = chapters[0].get("unlock", {}).get("type") == "none"
    add("首章无需前置", first_unlock_ok, str(chapters[0].get("unlock")))

    gate_ok, gate_detail = True, ""
    for ch in chapters[1:]:
        un = ch.get("unlock", {})
        if un.get("type") != "after_chapter":
            gate_ok, gate_detail = False, f"{ch['id']} unlock.type={un.get('type')}"
            break
        if un.get("chapter") not in ch_ids or un["chapter"] == ch["id"]:
            gate_ok, gate_detail = False, f"{ch['id']} unlock.chapter={un.get('chapter')} 不是有效前置"
            break
        if not isinstance(un.get("min_score"), (int, float)) or not 0 <= un["min_score"] <= 100:
            gate_ok, gate_detail = False, f"{ch['id']} min_score={un.get('min_score')}"
            break
    add("门控链完整可执行", gate_ok, gate_detail)

    covers_ok, covers_detail = True, ""
    for ch in chapters:
        a = ch.get("assessment", {})
        unit_ids = {u["id"] for u in ch.get("units", [])}
        covers = set(a.get("covers") or [])
        if a.get("type") != "chapter_test":
            covers_ok, covers_detail = False, f"{ch['id']} assessment.type={a.get('type')}"
            break
        if not covers:
            covers_ok, covers_detail = False, f"{ch['id']} covers 为空"
            break
        if not covers <= unit_ids:
            covers_ok, covers_detail = False, f"{ch['id']} covers {covers - unit_ids} 不属于本章"
            break
        if not 0 <= a.get("unlock_next", {}).get("min_score", -1) <= 100:
            covers_ok, covers_detail = False, f"{ch['id']} unlock_next.min_score 非法"
            break
    add("章节测验覆盖本章单元", covers_ok, covers_detail)

    # ---- 工期与粒度 ----
    # 口径：章 estimated_hours = 其单元之和；缓冲不进 hours，由 buffer_ratio 单独表达
    sum_ok, sum_detail = True, ""
    for c in chapters:
        units_h = sum(u.get("estimated_hours", 0) for u in c.get("units", []))
        if abs(c.get("estimated_hours", 0) - units_h) > 1e-6:
            sum_ok = False
            sum_detail = f"{c['id']} 章 {c.get('estimated_hours')}h ≠ 单元之和 {units_h}h（缓冲不得计入 hours）"
            break
    add("章课时 = 单元之和", sum_ok, sum_detail)

    net = sum(c.get("estimated_hours", 0) for c in chapters)
    capacity = meta.get("total_weeks", 0) * meta.get("weekly_hours", 0)
    ratio = meta.get("buffer_ratio", 0.15)
    expected_budget = math.floor(capacity * (1 - ratio) * 2) / 2
    declared = meta.get("net_hour_budget")
    add("net_hour_budget 正确",
        isinstance(declared, (int, float)) and abs(declared - expected_budget) < 1e-6,
        f"声明 {declared}，应为 {expected_budget}（容量 {capacity}h × (1-{ratio}) 向下取整到 0.5）")
    add("净课时不超预算",
        isinstance(declared, (int, float)) and 0 < net <= declared,
        f"净 {net}h / 预算 {declared}h")

    bad_units = [
        u["id"] for c in chapters for u in c.get("units", [])
        if not (0.5 <= u.get("estimated_hours", 0) <= 3)
    ]
    add("单元粒度 0.5~3 课时", not bad_units, f"越界 {bad_units}")

    bad_hw = [
        u["id"] for c in chapters for u in c.get("units", [])
        if not 3 <= (u.get("homework") or {}).get("count", 0) <= 5
    ]
    add("每单元作业 3~5 题", not bad_hw, f"越界 {bad_hw}")

    # ---- 照抄检测：单元标题与 Few-shot 的重合度 ----
    if check_copy:
        ref_titles = _fewshot_unit_titles()
        gen_titles = [u["title"] for c in chapters for u in c.get("units", [])]
        dup = [t for t in gen_titles if t in ref_titles]
        ratio_copy = len(dup) / len(gen_titles) if gen_titles else 0
        add("非照抄 Few-shot（重合<50%）", ratio_copy < 0.5,
            f"{len(dup)}/{len(gen_titles)} 个单元标题与示例相同"
            + (f"：{dup[:3]}" if dup else ""))

    return checks


# ------------------------------------------------------------------ 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="运行次数")
    ap.add_argument("--case", choices=sorted(CASES), default="math",
                    help="评测用例（bio=与 Few-shot 同题，照抄回归；math/physics=换学科）")
    ap.add_argument("--retry", type=int, default=0,
                    help="校验失败时最多重试几次（把失败原因回投给模型）")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调 API")
    ap.add_argument("--self-check", action="store_true",
                    help="离线校验 Few-shot 示例自身是否合规（不调 API）")
    args = ap.parse_args()

    lib = PromptLib(ROOT)

    if args.self_check:
        doc = yaml.safe_load(
            (ROOT / "fewshots" / "teacher_planner.yaml").read_text(encoding="utf-8")
        )
        checks = validate(doc["examples"][0]["output"], check_copy=False)
        for name, ok, detail in checks:
            print(("  OK  " if ok else "  FAIL"), name, "|", detail)
        bad = [c for c in checks if not c[1]]
        print(f"\nFew-shot 自检：{len(checks) - len(bad)}/{len(checks)} 通过")
        sys.exit(1 if bad else 0)

    case = CASES[args.case]
    prompt = lib.build("teacher_planner", case)

    if args.dry_run:
        print(prompt)
        return

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key（DEEPSEEK_API_KEY / OPENAI_API_KEY 或 ../LangChain开发/.env）")
        sys.exit(2)

    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"用例: {args.case} — {case['学科']} / {case['学习目标']}")
    print(f"模型: {args.model}  温度: {args.temperature}  次数: {args.n}")
    print(f"prompt: {len(prompt)} 字符\n")

    all_results: list[list[tuple[str, bool, str]]] = []
    for i in range(1, args.n + 1):
        print(f"—— 第 {i}/{args.n} 次 ——")
        messages = [{"role": "user", "content": prompt}]
        raw, checks = "", []
        for attempt in range(args.retry + 1):
            try:
                resp = client.chat.completions.create(
                    model=args.model, messages=messages, temperature=args.temperature,
                )
                raw = resp.choices[0].message.content or ""
            except Exception as e:
                print(f"  API 调用失败: {type(e).__name__}: {e}\n")
                break
            checks = validate(raw)
            failed = [c for c in checks if not c[1]]
            if not failed:
                break
            if attempt < args.retry:
                print(f"  第 {attempt + 1} 次校验未过，回投 {len(failed)} 条问题后重试…")
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "你的输出存在以下问题，请修正后重新输出完整结果（学生视图 + 蓝图）：\n"
                     + "\n".join(f"- {n}：{d}" for n, _, d in failed)},
                ]
        if not raw:
            continue

        suffix = f"_r{attempt}" if attempt else ""
        (RUNS_DIR / f"planner_{args.case}_{stamp}_{i}{suffix}.md").write_text(raw, encoding="utf-8")
        all_results.append(checks)
        failed = [c for c in checks if not c[1]]
        print(f"  {len(checks) - len(failed)}/{len(checks)} 通过"
              + (f"（重试 {attempt} 次后）" if attempt else "")
              + f"   已存 eval_runs/planner_{args.case}_{stamp}_{i}{suffix}.md")
        for name, _, detail in failed:
            print(f"    ✗ {name}  {detail}")
        print()

    if not all_results:
        print("没有任何成功样本，无法汇总。")
        sys.exit(1)

    # ---- 汇总 ----
    print("=" * 72)
    print("汇总（按检查项统计通过率）")
    print("=" * 72)
    names = [n for n, _, _ in all_results[0]]
    for idx, name in enumerate(names):
        passed = sum(1 for r in all_results if idx < len(r) and r[idx][1])
        mark = "✅" if passed == len(all_results) else ("⚠️ " if passed else "❌")
        print(f"  {mark} {name:28s} {passed}/{len(all_results)}")

    total = sum(len(r) for r in all_results)
    ok = sum(1 for r in all_results for c in r if c[1])
    print(f"\n合计 {ok}/{total} 项通过；样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
