# -*- coding: utf-8 -*-
"""S0a：模块判定（原句 → 候选模块）。

分工：
  · 关键词规则（``scope.guess_module``）先跑，命中且不并列就直接用；
  · 规则拿不准（没命中 / 两个模块同分）→ 这里让模型读原话在**给定清单**里选；
  · 模型也说「无法判断」→ 才回到交互菜单 / 清单第 1 个（并记 notes）。

不这么做的话，「我想突击一下文言文」这类说法一旦漏命中，就会被排成清单第 1 个模块
（高中语文是「现代文阅读」），学员看到的方案完全是另一门课。
"""
from __future__ import annotations

import re

import yaml

from core import scope, templates
from core.artifacts import Run
from core.llm import LLM

_PICK_RE = re.compile(r"模块\s*[：:]\s*(.+)")
_WHY_RE = re.compile(r"依据\s*[：:]\s*(.+)")


def _menu(spec: scope.FlowSpec) -> str:
    return "\n".join(f"- {m['name']}：{m.get('goal', '')}" for m in spec.modules)


def run(spec: scope.FlowSpec, text: str, run: Run, llm: LLM) -> tuple[str | None, str]:
    """返回 ``(模块名 | None, 依据)``；None 表示模型也判断不了。"""
    names = [m["name"] for m in spec.modules]
    variables = {
        "学段": spec.stage,
        "学科": spec.subject,
        "用户原话": text,
        "模块清单": _menu(spec),
    }
    prompt = templates.build("module_pick", variables, fewshot=False)
    run.write("00a_module.prompt.md",
              f"# S0a 模块判定（module_pick）\n# 原话：{text}\n\n"
              f"# 变量表\n{yaml.safe_dump(variables, allow_unicode=True, sort_keys=False)}\n"
              f"# 提示词\n\n{prompt}\n")

    raw = llm.call("s0_module", prompt)
    run.write("00a_module.result.md", raw)

    m = _PICK_RE.search(raw)
    picked = (m.group(1).strip() if m else "")
    basis = (_WHY_RE.search(raw).group(1).strip() if _WHY_RE.search(raw) else "")[:90]

    # 只接受清单里的原名（模型可能带标点/括号解释，先做包含匹配）
    hit = next((n for n in names if n in picked), None) if picked else None
    if not hit:
        run.step_done("s0_module", ok=False,
                      artifacts=[run.path("00a_module.prompt.md"),
                                 run.path("00a_module.result.md")],
                      notes=["模型未给出可用的模块名（或输出「无法判断」）"],
                      extra={"raw_pick": picked, "basis": basis})
        return None, ""
    run.step_done("s0_module", ok=True,
                  artifacts=[run.path("00a_module.prompt.md"),
                             run.path("00a_module.result.md")],
                  extra={"module": hit, "basis": basis})
    return hit, basis
