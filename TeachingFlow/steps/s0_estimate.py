# -*- coding: utf-8 -*-
"""S0b：学习周期估算（模型决定「学几周、每周几课时」）。

为什么单独一步：`teacher_planner` 的输入契约要求确切的「总周期 < 16 周」与
「每周可投入时间 ≤ 24 课时」，但用户通常只说「我想系统学高中语文」。
以前是代码静默取 10 周 / 3 课时——等于把课程体量交给一个常数；也不该反过来
逼用户先回答「学几周」。这里让模型读完模块边界、目标与水平后给周期，代码只夹边界。

用户已经说了周期（`--weeks/--days` 或句子里写「两周」）就不跑这一步。
"""
from __future__ import annotations

import re

import yaml

from core import scope, templates
from core.artifacts import Run
from core.llm import LLM

_WEEKS_RE = re.compile(r"总周期\s*[：:]\s*(\d{1,2})")
_HOURS_RE = re.compile(r"每周可投入时间\s*[：:]\s*(\d{1,2})")
_BASIS_RE = re.compile(r"依据\s*[：:]\s*(.+)")


def run(spec: scope.FlowSpec, run: Run, llm: LLM,
        forced_term: str | None = None) -> bool:
    """调模型估算周期；返回是否成功。失败不阻断（保留临时默认值并记警告）。"""
    if not scope.needs_estimate(spec):
        return True

    variables = {
        "学段": spec.stage,
        "学科": spec.subject,
        "模块": spec.module,
        "学习目标": spec.goal or f"系统掌握{spec.module}",
        "学生水平": spec.level,
        "用户原话": spec.raw,
    }
    prompt = templates.build("scope_estimate", variables, fewshot=False)
    if spec.intensity in ("一次课", "单篇"):
        prompt += (
            chr(10) + chr(10) + "【本次口径】这是一次课（本轮 1 周内解决）："
            "「每周可投入时间」就是这一次课的总课时，请按「这节课实际能讲完多少」给，"
            "不要按整周常规投入给；给不出依据就不要往上凑。" + chr(10)
        )
    run.write("00b_estimate.prompt.md",
              f"# S0b 学习周期估算（scope_estimate）\n"
              f"# {spec.stage}{spec.subject}·{spec.module}｜水平 {spec.level}\n\n"
              f"# 变量表\n{yaml.safe_dump(variables, allow_unicode=True, sort_keys=False)}\n"
              f"# 提示词\n\n{prompt}\n")

    print(f"【S0b】周期估算：读完「{spec.module}」与目标后判断学几周、每周几课时 …")
    raw = llm.call("s0_estimate", prompt)
    run.write("00b_estimate.result.md", raw)

    m_weeks = _WEEKS_RE.search(raw)
    m_hours = _HOURS_RE.search(raw)
    m_basis = _BASIS_RE.search(raw)
    weeks = int(m_weeks.group(1)) if m_weeks else None
    hours = int(m_hours.group(1)) if m_hours else None
    basis = (m_basis.group(1).strip() if m_basis else "")[:120]

    if not weeks and not hours:
        spec.notes.append("周期估算失败（回复里没有「总周期：N」），沿用临时默认值")
        run.step_done("s0_estimate", ok=False,
                      artifacts=[run.path("00b_estimate.prompt.md"),
                                 run.path("00b_estimate.result.md")],
                      notes=["未解析出周期/周课时"])
        print("   ⚠️ 没解析出周期，沿用临时默认值（--weeks 可指定）")
        return False

    pend_weeks = "待模型估算" in spec.weeks_source
    pend_hours = "待模型估算" in spec.hours_source
    scope.apply_estimate(spec, weeks, hours, basis, forced_term=forced_term)
    used = [n for n, p_ in (("周数", pend_weeks), ("周课时", pend_hours)) if p_]
    detail = "" if len(used) == 2 else ("只估了" + "、".join(used) if used else "")

    run.step_done("s0_estimate", ok=True,
                  artifacts=[run.path("00b_estimate.prompt.md"),
                             run.path("00b_estimate.result.md")],
                  extra={"weeks": spec.weeks, "hours_per_week": spec.hours_per_week,
                         "term": spec.term, "basis": basis,
                         "estimated_weeks": weeks, "estimated_hours": hours})
    print(f"   ✓ 采用：{spec.weeks} 周 × {spec.hours_per_week} 课时/周"
          f"（{spec.term}）" + (f"｜{detail}" if detail else "")
          + (f" —— {basis}" if basis else ""))
    return True
