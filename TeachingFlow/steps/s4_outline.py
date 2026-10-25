# -*- coding: utf-8 -*-
"""S4：课件大纲（teacher_ppt）→ 逐页大纲 + 给产片引擎的 sidecar。

三个关键点：
1. **页数由模型决定**（S4a / slide_plan）：读本单元的教学内容，按「一页一个
   教学动作」算出页数并给理由；代码只提供 8~30 的契约边界并记录依据。
   显式 `--pages`（或句子里说「12 页」）时优先照办。
2. 传给 PPT 的是 ``unit_output_to_ppt_content`` 筛过的 5 个教学内容模块
   +【课件大纲】作页骨架，不是整篇 10 模块——作业与解锁提示上 PPT 会把模型带跑。
3. 同时写一份 ``.meta.json`` sidecar。S5 走 ``pipeline.py --from-plan`` 复用它，
   因此不会把同一个大纲让模型再规划一遍（省一跳 token，且内容与审计一致）。
"""
from __future__ import annotations

import json
import re

import yaml

from core import blueprint, gates, scope, templates
from core.artifacts import Run
from core.llm import LLM, run_with_gate

_PAGES_RE = re.compile(r"(?:总)?页数\s*[：:]?\s*(?:约|共|为)?\s*(\d{1,3})")
#: 模型没写「页数：」时的第二道网（「共 12 页」「需要 10 页」）
_PAGES_ALT_RE = re.compile(r"(?:共|需|需要|计)\s*(\d{1,3})\s*页")
_REASON_RE = re.compile(r"理由\s*[：:]\s*(.+)")


#: 一次课的页数口径（只讲密度与禁令，不给任何示例数字——数字由模型按内容数）
ONESHOT_PAGES_NOTE = (
    chr(10) + chr(10) + "【本次口径】这是一次课（1 周内解决）：全部内容要在这节课里讲完，"
    "页数仍按「一页一个教学动作」实数，不要为了压页把两个动作并成一页，"
    "也不要为了显得丰富而拆页——可以比常规课件长。"
)


def decide_pages(uid: str, target: dict, content: str, requested: int | None,
                 run: Run, llm: LLM, meta: dict | None = None,
                 basis_label: str = "用户指定", intensity: str = "") -> tuple[int, str]:
    """决定这份课件几页，返回 ``(页数, 依据)``。

    页数既不写死也不按时长硬算：交给模型读完单元内容后决定。
    代码只做两件事——给出 8~30 的契约边界、记录决策依据（便于事后审）。
    """
    if requested:
        run.step_done(f"s4_pages:{uid}", ok=True, artifacts=[],
                      notes=[f"{basis_label}，未调模型"],
                      extra={"pages": scope.clamp_pages(requested), "basis": basis_label})
        return scope.clamp_pages(requested), basis_label

    meta = meta or {}
    variables = {
        "学科": meta.get("subject") or target.get("subject") or "通用",
        "本单元名称": target["unit_title"],
        "学生水平": meta.get("student_level") or target.get("student_level") or "基础",
        "本单元课时": f"{float(target.get('hours') or 2):g}课时",
        "单元内容": content,
    }
    prompt = templates.build("slide_plan", variables, fewshot=False)
    if intensity == "一次课":
        prompt += ONESHOT_PAGES_NOTE
    display = dict(variables)
    display["单元内容"] = f"<{len(content)} 字符，完整内容见下方提示词>"
    run.write(f"04a_pages_{uid}.prompt.md",
              f"# S4a 页数决策（slide_plan）\n"
              f"# 单元：{target['unit_id']} / {target['unit_title']}\n\n"
              f"# 变量表\n"
              f"{yaml.safe_dump(display, allow_unicode=True, sort_keys=False)}\n"
              f"# 提示词\n\n{prompt}\n")

    raw = llm.call("s4_pages", prompt, tag=uid)
    run.write(f"04a_pages_{uid}.result.md", raw)

    m = _PAGES_RE.search(raw) or _PAGES_ALT_RE.search(raw)
    if not m:
        pages, why = scope.pages_for_unit(target.get("hours"))
        run.step_done(f"s4_pages:{uid}", ok=False,
                      artifacts=[run.path(f"04a_pages_{uid}.prompt.md"),
                                 run.path(f"04a_pages_{uid}.result.md")],
                      notes=[f"回复里没有「页数：N」，兜底 {pages} 页"],
                      extra={"pages": pages, "basis": why})
        return pages, f"模型未给出页数，兜底：{why}"

    decided = int(m.group(1))
    reason = (_REASON_RE.search(raw).group(1).strip() if _REASON_RE.search(raw) else "")[:70]
    pages = scope.clamp_pages(decided)
    basis = f"模型按内容决策 {decided} 页" + (f"：{reason}" if reason else "")
    if pages != decided:
        basis += f"；越界已夹到 {pages}（契约 {scope.PAGES_MIN}~{scope.PAGES_MAX}）"
    run.step_done(f"s4_pages:{uid}", ok=True,
                  artifacts=[run.path(f"04a_pages_{uid}.prompt.md"),
                             run.path(f"04a_pages_{uid}.result.md")],
                  notes=[f"越界：决策 {decided} → 实际 {pages}"] if pages != decided else [],
                  extra={"pages": pages, "decided": decided, "basis": basis})
    return pages, basis


def run(course: dict, target: dict, unit_text: str, pages: int | None,
        run: Run, llm: LLM, pages_source: str = "",
        intensity: str = "") -> str:
    """返回 LLM 的逐页大纲文本。"""
    uid = target["unit_id"]
    content = blueprint.unit_content_for_ppt(unit_text)
    meta = course.get("course") or {}
    pages, basis = decide_pages(uid, target, content, pages, run, llm, meta,
                                basis_label=pages_source or "用户指定",
                                intensity=intensity)
    print(f"【S4a】{uid} 页数决策：{pages} 页 —— {basis}")

    variables = blueprint.ppt_variables(
        course, target["chapter_id"], target["unit_index"], unit_text, pages)
    if not variables.get("学科"):
        variables["学科"] = meta.get("subject", "")
    prompt = templates.build("teacher_ppt", variables, fewshot=True)

    display = dict(variables)
    display["单元内容"] = f"<{len(variables['单元内容'])} 字符，完整内容见下方提示词>"
    run.write(f"04_outline_{uid}.prompt.md",
              f"# S4 课件大纲（teacher_ppt）\n"
              f"# 上游：03_unit_{uid}.result.md\n"
              f"# 页数依据：{basis}\n\n"
              f"# 变量表\n"
              f"{yaml.safe_dump(display, allow_unicode=True, sort_keys=False)}\n"
              f"# 提示词\n\n{prompt}\n")

    print(f"【S4】{uid} 课件大纲：{target['unit_title']}（{pages} 页）…")
    raw, _, gate, attempts = run_with_gate(
        llm, "s4_outline", prompt, lambda r: (None, gates.gate_outline(r, pages)),
        temps=(0.7, 0.3), tag=uid)
    for line in gate.lines():
        print("   " + line)

    result_path = run.write(f"04_outline_{uid}.result.md", raw)

    # sidecar 的「页数要求」按**大纲实际页数**写：pipeline --from-plan 会拿它复检
    # 「页数=声明」，若这里写请求值 12 而实际 10 页，会凭空多一条缺陷并触发降级交接。
    actual = gates.count_pages(raw)
    notes = [f"{n} {d}" for n, d in gate.failures]
    sidecar_inputs = dict(variables)
    if actual and actual != pages:
        sidecar_inputs["页数要求"] = str(actual)
        notes.append(f"大纲实际 {actual} 页 ≠ 决策 {pages} 页；sidecar 已按实际页数写")

    sidecar = result_path.with_suffix(".meta.json")
    sidecar.write_text(json.dumps({
        "created_at": run.manifest["steps"].get("s1_plan", {}).get("at", ""),
        "role": "teacher_ppt",
        "inputs": sidecar_inputs,
        "source": "TeachingFlow S4（teacher_ppt 模板渲染后由 LLM 产出）",
        "unit_id": uid,
        "pages_requested": pages,
        "pages_basis": basis,
        "pages_actual": actual,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    run.step_done(f"s4_outline:{uid}", ok=gate.ok,
                  artifacts=[run.path(f"04_outline_{uid}.prompt.md"), result_path, sidecar],
                  notes=notes,
                  extra={"attempts": attempts, "pages_wanted": pages,
                         "pages_basis": basis,
                         "pages_actual": actual,
                         "checks": [[n, ok, d] for n, ok, d in gate.checks]})
    return raw
