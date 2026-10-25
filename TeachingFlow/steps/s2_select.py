# -*- coding: utf-8 -*-
"""S2：从蓝图挑出本次要渲染的单元（纯规则，不调模型）。

默认前 N 个单元（用户拍的口径：长期学习先出前两章的两节课）。
门控默认只报告不阻挡——见 core/blueprint.py::pick_targets 的说明。
"""
from __future__ import annotations

from core import blueprint
from core.artifacts import Run


def run(course: dict, args, run: Run, focus: str = "") -> list[dict]:
    targets, notes = blueprint.pick_targets(
        course,
        count=args.units,
        only=args.unit,
        progress={},                       # 骨架阶段无学生进度，首跑即空
        strict_gate=args.strict_gate,
        focus=focus,                       # 原话点了具体内容就优先挑贴合单元
    )
    if not targets:
        run.write_json("02_targets.json", {"targets": [], "notes": notes})
        raise SystemExit("S2：没有可渲染的单元（蓝图单元为空，或被 --strict-gate 全部拦下）")

    # 本次渲染的单元课时会不会超出整门课的预算？超出不阻断（备课口径允许提前备），
    # 但要明说——否则「2 周课却排了 5 课时 ×3 个单元」这种账没人发现。
    budget = float((course.get("course") or {}).get("net_hour_budget") or 0)
    picked_hours = sum(float(t.get("hours") or 0) for t in targets)
    if budget and picked_hours > budget:
        notes.append(f"本次 {len(targets)} 个单元共 {picked_hours:g} 课时，"
                     f"超出本阶段预算 {budget:g} 课时——用 --units/--unit 收敛")

    print(f"【S2】本次渲染 {len(targets)} 个单元（合计 {picked_hours:g} 课时，"
          f"预算 {budget:g} 课时）：")
    for t in targets:
        flag = "✅" if t.get("unlocked", True) else "🔒"
        print(f"   {flag} {t['unit_id']}  {t['chapter_id']}/{t['unit_title']} "
              f"（{t['hours']} 课时，第 {t['unit_index'] + 1}/{t['units_in_chapter']} 个）")
    for n in notes:
        print(f"   ⚠️ {n}")

    path = run.write_json("02_targets.json", {"targets": targets, "notes": notes})
    run.step_done("s2_select", ok=True, artifacts=[path], notes=notes,
                  extra={"count": len(targets), "focus": focus})
    return targets
