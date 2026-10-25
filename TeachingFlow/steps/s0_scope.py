# -*- coding: utf-8 -*-
"""S0：一句话 → FlowSpec（含模块清单与用户选择）。

链路里唯一「半交互」的一步：模块清单来自内置表或模型，选择权交给用户。
非交互终端（管道/CI）默认取第一个，并把这个决定写进 notes，不静默。
"""
from __future__ import annotations

import re

import yaml

from core import scope, templates
from core.artifacts import Run
from core.llm import LLM
from steps import s0_estimate, s0_module

_MODULES_RE = re.compile(r"^modules:\s*$", re.M)


def _parse_modules(raw: str) -> list[dict]:
    """从模型回复里切 ``modules:`` 段并解析成 [{name, goal, weeks?}]。"""
    m = _MODULES_RE.search(raw)
    if not m:
        raise ValueError("模型回复里没有 'modules:' 段")
    data = yaml.safe_load(raw[m.start():]) or {}
    items = data.get("modules") or []
    out: list[dict] = []
    for it in items:
        if isinstance(it, str):
            out.append({"name": it, "goal": ""})
        elif isinstance(it, dict) and it.get("name") or (isinstance(it, dict) and it.get("模块")):
            out.append({
                "name": str(it.get("name") or it.get("模块")),
                "goal": str(it.get("goal") or it.get("目标") or ""),
            })
    if not out:
        raise ValueError("modules: 段解析为空")
    return out


def _pick_module(spec: scope.FlowSpec, text: str, args, run: Run, llm: LLM) -> str:
    """选模块：显式 --module → 关键词 → 模型读原句 → 交互菜单 → 第 1 个。

    关键词规则只在**唯一命中**时才算数；没命中或两个模块同分，说明规则读不准，
    就把原话交给模型（``s0_module``）在给定清单里选，而不是退回清单第 1 个。
    """
    names = [m["name"] for m in spec.modules]

    def take(name: str, why: str) -> str:
        spec.module = name
        spec.goal = next(m.get("goal", "") for m in spec.modules if m["name"] == name)
        spec.notes.append(f"模块判定：{name}（{why}）")
        return name

    if args.module:
        module = scope.choose_module(spec, args.module, interactive=False)
        print(f"   ✓ --module 指定：{module}")
        return module

    guessed, why, score, tied = scope.guess_module(spec, text)
    if guessed and guessed in names and not tied:
        print(f"   ✓ 从原话识别模块：{guessed}（{why}）")
        return take(guessed, why)

    if guessed:
        print(f"   ↳ 关键词不足以定模块（{why}），交模型读原句判断 …")
    else:
        print("   ↳ 原话没有直接命中模块名，交模型读原句判断 …")
    picked, basis = s0_module.run(spec, text, run, llm)
    if picked and picked in names:
        print(f"   ✓ 模型判定模块：{picked}" + (f" —— {basis}" if basis else ""))
        return take(picked, f"模型读原句：{basis or '见 00a_module.result.md'}")

    module = scope.choose_module(spec, None, interactive=not args.no_input)
    return take(module, "规则与模型都没定，按清单/菜单")


def run(text: str, args, run: Run, llm: LLM) -> scope.FlowSpec:
    spec = scope.build_spec(text, args)
    # 学段/学科未确定时必须由用户确认/修正，不能静默采用推断值
    scope.resolve_identity(spec, interactive=not args.no_input,
                           accept_detected=args.accept_detected)

    # ① 模块清单：内置表优先（确定性、零成本）
    modules = scope.builtin_modules(spec.stage, spec.subject)
    prompt_path = None
    if modules:
        scope.attach_modules(spec, modules, "内置表")
        print(f"   ✓ 内置表命中 {len(modules)} 个模块（{spec.stage}{spec.subject}）")
    else:
        print(f"   ↳ 内置表无 ({spec.stage}, {spec.subject}) 组合，请模型切分模块 …")
        variables = {"学科": spec.subject, "学段": spec.stage}
        prompt = templates.build("course_map", variables, fewshot=False)
        prompt_path = run.write("00_scope.prompt.md",
                                f"# S0 模块切分（course_map）\n# 用户输入：{text}\n\n{prompt}\n")
        raw = llm.call("s0_scope", prompt)
        run.write("00_scope.result.md", raw)
        modules = _parse_modules(raw)
        scope.attach_modules(spec, modules, "模型生成")

    # ② 选模块：--module > 原句关键词 > 模型读原句 > 菜单/第 1 个
    module = _pick_module(spec, text, args, run, llm)
    print(f"   ✓ 选定模块：{module} —— {spec.goal}")

    # ②b 把目标收窄到原话聚焦的主题：「突击光合作用」不该产出一份讲「种子萌发 + 呼吸作用」的方案。
    #     模块 goal 描述的是整个模块（三块内容），直接拿去规划必然摊大；这里换成聚焦目标，
    #     同时保留模块名做上下文。S0b 估算与 S1 规划都用这一句。
    if spec.single_text:
        base = spec.goal
        spec.goal = (f"围绕《{spec.work}》做一次精读：理清内容与结构，"
                     f"分析主要写法及其作用，把握主旨，并完成典型题目。")
        spec.notes.append(f"学习目标按单篇课文收窄（模块原目标：{base}）")
        print(f"   ✓ 目标收窄（单篇）：{spec.goal}")
    elif spec.focus:
        base = spec.goal
        spec.goal = (f"本次聚焦「{spec.focus}」（属于模块：{module}）："
                     f"能把该主题的要点与方法讲清楚，并完成相关练习与典型题目。")
        spec.notes.append(f"学习目标已收窄到「{spec.focus}」（模块原目标：{base}）")
        print(f"   ✓ 目标收窄：{spec.goal}")

    # ③ 强度模糊（既没关键词也没写周期）→ 问一句；明确就跳过
    scope.ask_intensity(spec, interactive=not args.no_input)

    # ③ 周期不必用户明说：模型读完模块边界与目标后估算（用户已指定则跳过）
    s0_estimate.run(spec, run, llm, forced_term=scope.args_term(args))

    # ④ 本次渲染几个单元：也自动化——单篇/短期 1 个，长期 2 个
    if not args.units:
        spec.units_per_run = 1 if (spec.term == "短期" or spec.single_text) else 2
        spec.notes.append(f"未指定 --units：按{spec.term}任务自动取 "
                          f"{spec.units_per_run} 个单元")
    else:
        spec.units_per_run = args.units

    if spec.focus:
        print(f"   本次聚焦：{spec.focus}（S2 会先挑与它贴合的单元）")
    if spec.intensity:
        print(f"   强度：{spec.intensity}"
              + (f"｜页数：{spec.pages}（{spec.pages_source}）" if spec.pages else ""))
    print(f"【S0】学段={spec.stage} 学科={spec.subject} "
          f"周期={spec.weeks}周（{spec.term}，来源：{spec.weeks_source}） "
          f"课时/周={spec.hours_per_week}（来源：{spec.hours_source}） "
          f"预算={scope.budget_hours(spec):g}课时 每次单元数={spec.units_per_run}")
    if spec.pages:
        print(f"   页数：{spec.pages} 页/单元（{spec.pages_source or '用户指定'}）")
    else:
        print(f"   页数：未指定 → 由模型读完本单元教学内容后决定（S4a，夹 {scope.PAGES_MIN}~{scope.PAGES_MAX}）；"
              f"--pages 可固定")

    artifacts = [run.write_json("00_spec.json", spec.to_dict())]
    if prompt_path:
        artifacts.append(prompt_path)
    run.step_done("s0_scope", ok=True, artifacts=artifacts, notes=spec.notes,
                  extra={"modules": [m["name"] for m in spec.modules],
                         "module": module, "source": spec.source,
                         "term": spec.term, "weeks": spec.weeks,
                         "weeks_source": spec.weeks_source,
                         "hours_per_week": spec.hours_per_week,
                         "hours_source": spec.hours_source,
                         "budget": scope.budget_hours(spec)})
    return spec
