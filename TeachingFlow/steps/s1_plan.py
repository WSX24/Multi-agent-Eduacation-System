# -*- coding: utf-8 -*-
"""S1：学习方案（teacher_planner）→ 蓝图 YAML。

这一跳的产物是后续所有步骤的唯一真源，所以门控最严：
蓝图解析失败、课时超预算、单元被预生成内容——一律不往下走。
"""
from __future__ import annotations

import yaml

from core import gates, scope, templates
from core.artifacts import Run
from core.llm import LLM, run_with_gate


def _term_note(spec: scope.FlowSpec) -> str:
    """本次口径补充（追加在模板提示词之后，不改模板文件）。

    为什么必须有：`teacher_planner` 模板的算例写「6 周」、范文也是 5 周 3 章，
    YAML 骨架还写死 `mode: long_term`。短期任务（2 周）如果只靠变量里的总周期，
    模型很容易照抄范文规模；这里用三行把「任务类型 + 预算 + 复习步骤」说清。
    """
    mode = "short_term" if spec.term == "短期" else "long_term"
    budget = scope.budget_hours(spec)
    lines = [
        "",
        "──── 以下为系统补充的本次口径（不改变上面任何契约与字段）────",
        f"- 任务类型：{spec.term}（{spec.weeks} 周 × {spec.hours_per_week} 课时/周）。",
        f"- 净课时预算按口径算得 {budget:g}（{spec.weeks}×{spec.hours_per_week}"
        f"×(1-{scope.BUFFER_RATIO:g})，向下取整到 0.5），蓝图 net_hour_budget 与它一致。",
        f"- 蓝图 course.mode 写 {mode}。",
    ]
    if spec.single_text:
        lines.append(f"- 单篇课文：只排 **1 章、1 个单元**（整篇精读，1.5~2 课时）；"
                     f"这一章就是这一篇课文，不要再拆成多单元或多章。")
    if spec.focus:
        lines.append(f"- 本次聚焦「{spec.focus}」：只排与它直接相关的章/单元；"
                     f"其它内容留到后续阶段，不要为了凑章数展开。")
    if spec.term == "短期":
        lines.append("- 短期任务：跳过第 5 步【插复习】；章数与单元数按预算收敛，"
                     "不要照抄范文/算例里的周数、章数与单元数。")
    else:
        lines.append("- 长期任务：按范文规模正常展开，章数由总周期与预算决定。")
    return "\n".join(lines) + "\n"


def run(spec: scope.FlowSpec, run: Run, llm: LLM) -> dict:
    variables = scope.planner_variables(spec)
    prompt = templates.build("teacher_planner", variables, fewshot=True) + _term_note(spec)
    run.write("01_plan.prompt.md",
              f"# S1 学习方案（teacher_planner）\n"
              f"# 模块：{spec.module}｜阶段：{spec.stage}{spec.subject}｜"
              f"{spec.weeks}周 × {spec.hours_per_week}课时/周\n\n"
              f"# 变量表\n{yaml.safe_dump(variables, allow_unicode=True, sort_keys=False)}\n"
              f"# 提示词\n\n{prompt}\n")

    print(f"【S1】学习方案：{spec.subject}·{spec.module}，{spec.weeks} 周 …")
    raw, course, gate, attempts = run_with_gate(
        llm, "s1_plan", prompt,
        lambda r: gates.gate_blueprint(r, spec.weeks, spec.hours_per_week,
                                               focus=spec.focus,
                                               single_text=spec.single_text))

    for line in gate.lines():
        print("   " + line)

    run.write("01_plan.result.md", raw)
    artifacts = [run.path("01_plan.prompt.md"), run.path("01_plan.result.md")]
    if course:
        artifacts.append(run.write("01_blueprint.yaml",
                                   yaml.safe_dump(course, allow_unicode=True, sort_keys=False)))

    extra = {"attempts": attempts,
             "checks": [[n, ok, d] for n, ok, d in gate.checks],
             "warnings": [[n, ok, d] for n, ok, d in gate.warnings]}
    if not gate.ok:
        run.step_done("s1_plan", ok=False, artifacts=artifacts, extra=extra)
        raise SystemExit("S1 未过 gate：蓝图是后续所有步骤的输入，不能降级继续。"
                         "详见 01_plan.result.md")

    # 模板的 YAML 骨架把 mode 写死成 long_term（PromptTemplate 是共用资产，不改它），
    # 所以短周期任务会带着「long_term」这个假标签继续往下走。这里按实际周期改写，
    # 并把偏差记进 manifest——否则「长期/短期」在图谱里永远分辨不出来。
    meta = course.setdefault("course", {})
    want_mode = "short_term" if spec.term == "短期" else "long_term"
    notes = [f"{n}（软）：{d}" for n, ok, d in gate.warnings if not ok]
    if meta.get("mode") != want_mode:
        note = (f"蓝图 mode 由模板固定值 {meta.get('mode')!r} 改写为 {want_mode!r}"
                f"（{spec.term}，{spec.weeks} 周）")
        meta["mode"] = want_mode
        notes.append(note)
        print(f"   ↳ {note}")
    meta["term"] = spec.term                     # 长期 / 短期，供下游与审计读取
    meta["weeks_source"] = spec.weeks_source
    artifacts[-1].write_text(
        yaml.safe_dump(course, allow_unicode=True, sort_keys=False), encoding="utf-8")

    chapters = course.get("chapters") or []
    units = sum(len(c.get("units") or []) for c in chapters)
    print(f"   ✓ 蓝图：{len(chapters)} 章 / {units} 单元 "
          f"（{course['course'].get('net_hour_budget')} 课时预算，"
          f"{spec.term}/{meta['mode']}）")
    run.step_done("s1_plan", ok=True, artifacts=artifacts, extra=extra,
                  notes=notes)
    return course
