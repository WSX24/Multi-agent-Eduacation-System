# -*- coding: utf-8 -*-
"""S3：单元教学（teacher_unit）→ 10 模块教学文本。

变量不是「从蓝图里抄几段文字」，而是 ``unit_variables`` 算出来的 12 个字段：
模板的 inputs: 契约对「上一章测验结果」这类字段有类型要求（百分比 / 哨兵值），
直接塞自由文本会静默走错分支。
"""
from __future__ import annotations

import yaml

from core import blueprint, gates, templates
from core.artifacts import Run
from core.llm import LLM, run_with_gate


def run(course: dict, target: dict, run: Run, llm: LLM) -> tuple[str, str]:
    """返回 (unit_id, LLM 输出的 10 模块文本)。"""
    uid = target["unit_id"]
    variables = blueprint.unit_variables(
        course, target["chapter_id"], target["unit_index"],
        progress={}, need_example=True)
    prompt = templates.build("teacher_unit", variables, fewshot=False)

    run.write(f"03_unit_{uid}.prompt.md",
              f"# S3 单元教学（teacher_unit）\n"
              f"# {target['chapter_id']} / {target['unit_title']}\n\n"
              f"# 变量表\n{yaml.safe_dump(variables, allow_unicode=True, sort_keys=False)}\n"
              f"# 提示词\n\n{prompt}\n")

    print(f"【S3】{uid} 单元教学：{target['unit_title']} …")
    raw, _, gate, attempts = run_with_gate(
        llm, "s3_unit", prompt, lambda r: (None, gates.gate_unit(r)), tag=uid,
        temps=(0.7, 0.3))
    for line in gate.lines():
        print("   " + line)

    artifacts = [run.path(f"03_unit_{uid}.prompt.md"),
                 run.write(f"03_unit_{uid}.result.md", raw)]
    # 缺模块只警告不阻断：unit_bridge 会原样透传，S4 仍能出大纲，只是少了结构参照
    run.step_done(f"s3_unit:{uid}", ok=gate.ok, artifacts=artifacts,
                  notes=[f"{n} {d}" for n, d in gate.failures],
                  extra={"attempts": attempts,
                         "checks": [[n, ok, d] for n, ok, d in gate.checks]})
    return uid, raw
