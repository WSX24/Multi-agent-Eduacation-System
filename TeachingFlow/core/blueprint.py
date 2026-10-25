# -*- coding: utf-8 -*-
"""蓝图（S1 产物）的解析、映射与挑选。

**刻意复用 ``PromptTemplate/demo_course_flow.py`` 里的实现**，不另抄一份：

  · ``parse_blueprint`` / ``can_unlock`` / ``unit_variables`` 是该文件里
    「门控契约的参考实现」，评测与生产共用同一份才算同一个契约。
  · 抄一份的后果是两处各自演进——蓝图字段一改，链路静默错位。

本模块只补三件 demo 没覆盖、但链路需要的事：
  1. 把蓝图摊平成「按顺序的单元列表」（S2 选单元用）；
  2. 组装 ``teacher_ppt`` 的变量表（demo 的 build_ppt_prompt 只返回 prompt，
     而 S5 的 ``--from-plan`` 需要 sidecar 里的变量表）；
  3. 课时预算的自核算（gate 用）。
"""
from __future__ import annotations

from . import config  # noqa: F401  —— 必须先插入 PromptTemplate/PPT-Agent 路径
from demo_course_flow import (can_unlock, course_status, parse_blueprint,  # noqa: E402,F401
                              unit_variables)
from unit_bridge import unit_output_to_ppt_content                         # noqa: E402

__all__ = [
    "parse_blueprint", "can_unlock", "course_status", "unit_variables",
    "flatten_units", "pick_targets", "ppt_variables", "budget_report",
]


def flatten_units(course: dict) -> list[dict]:
    """蓝图 → 按「章序 + 单元序」排好的单元条目（含所属章元信息）。"""
    out: list[dict] = []
    chapters = course.get("chapters") or []
    for ch in sorted(chapters, key=lambda c: c.get("order", 0)):
        units = ch.get("units") or []
        for i, unit in enumerate(units):
            out.append({
                "chapter_id": ch["id"],
                "chapter_order": ch.get("order", len(out) + 1),
                "chapter_title": ch.get("title", ""),
                "unit_index": i,
                "unit_id": unit["id"],
                "unit_title": unit.get("title", ""),
                "unit_type": unit.get("type", ""),
                "hours": unit.get("estimated_hours"),
                "units_in_chapter": len(units),
            })
    return out


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（不用 DP 表格，聚焦词都很短，暴力滑窗够快）。"""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def focus_score(unit: dict, focus: str) -> int:
    """单元与本轮聚焦主题的贴合度。

    判定故意收紧：早先用 2 字窗口，「光合作用」会命中「情节**作用**与环境描写」，
    于是把小说单元当成光合作用课。现在要求**最长公共子串覆盖 focus 的 60%**
    （至少 2 字）才算沾边；整串命中直接给高分。
    """
    if not focus:
        return 0
    hay = f"{unit.get('chapter_title', '')} {unit.get('unit_title', '')}"
    if focus in hay:
        return 100 + len(focus) * 2
    need = max(2, -(-len(focus) * 3 // 5))          # ceil(0.6 × len)
    hit = _lcs_len(focus, hay)
    return hit * 10 if hit >= need else 0


def pick_targets(course: dict, count: int = 2,
                 only: str | None = None,
                 progress: dict | None = None,
                 strict_gate: bool = False,
                 focus: str = "") -> tuple[list[dict], list[str]]:
    """挑出本次要渲染的单元（默认前 N 个），返回 ``(targets, notes)``。

    与 demo 的 ``next_unlocked_unit`` 的差别——**一个刻意的产品决定**：

    门控（``can_unlock``）管的是「学生能不能学到下一章」，不是
    「老师能不能提前备课」。因此默认不吃门控，按章序取前 N 个单元并
    *报告*解锁状态；``--strict-gate`` 才按学生进度硬卡。
    """
    notes: list[str] = []
    units = flatten_units(course)
    if only:
        hit = [u for u in units if u["unit_id"] == only]
        if not hit:
            raise KeyError(f"蓝图里没有单元 {only!r}；可选：{[u['unit_id'] for u in units]}")
        return hit, notes

    progress = progress or {}
    picked: list[dict] = []

    # 用户原话点了具体内容（「光合作用」）就优先挑贴合的单元，
    # 而不是按章序取第一个——否则「突击光合作用」会交付「种子萌发」。
    ordered = units
    if focus:
        scored = [(focus_score(u, focus), i, u) for i, u in enumerate(units)]
        hit = [s for s in scored if s[0] > 0]
        if hit:
            hit.sort(key=lambda x: -x[0])
            first = [u for _, _, u in hit]
            rest = [u for _, i, u in sorted(scored, key=lambda x: x[1]) if u not in first]
            ordered = first + rest
            notes.append(f"按原话聚焦「{focus}」优先：{first[0]['unit_id']} "
                         f"{first[0]['unit_title']}")
        else:
            notes.append(f"⚠️ 蓝图里没有与「{focus}」贴合的章/单元（规划跑偏）："
                         f"已退回章序取单元。建议 --resume 重跑 S1，或用 --unit 指定；"
                         f"也可用 --module 把范围先锁住")

    for u in ordered:
        if len(picked) >= count:
            break
        unlocked, why = can_unlock(course, u["chapter_id"], progress)
        if not unlocked:
            if strict_gate:
                notes.append(f"跳过 {u['unit_id']}（{why}）")
                continue
            notes.append(f"{u['unit_id']} 所属 {u['chapter_id']} 未解锁（{why}）"
                         f"——按备课口径仍生成，仅内容预览，不可直接布置给学生")
        extra = {"focus_score": focus_score(u, focus)} if focus else {}
        picked.append({**u, "unlocked": unlocked, "unlock_reason": why, **extra})
    return picked, notes


def unit_content_for_ppt(unit_output: str) -> str:
    """单元 10 模块 → PPT 可见的 5 个教学模块 + 页骨架（与 S4 同源）。"""
    return unit_output_to_ppt_content(unit_output)


def ppt_variables(course: dict, chapter_id: str, unit_index: int,
                  unit_output: str, pages: int) -> dict:
    """组装 ``teacher_ppt`` 的 5 个变量。

    与 demo 的 ``build_ppt_prompt`` 同源同序（它也调 ``unit_output_to_ppt_content``），
    区别只是这里把变量表也返回出来——S5 走 ``pipeline --from-plan`` 时，
    复检需要 sidecar 里的这份输入表。
    """
    meta = course.get("course", {})
    ch = next(c for c in course["chapters"] if c["id"] == chapter_id)
    unit = ch["units"][unit_index]
    return {
        "学科": meta.get("subject", ""),
        "课件主题": unit["title"],
        "页数要求": str(pages),
        "学生水平": meta.get("student_level", "基础"),
        "单元内容": unit_output_to_ppt_content(unit_output),
    }


def budget_report(course: dict) -> tuple[float, float, list[str]]:
    """返回 ``(已分配课时, 预算课时, 问题列表)``。"""
    meta = course.get("course", {})
    budget = float(meta.get("net_hour_budget") or 0)
    problems: list[str] = []
    total = 0.0
    for ch in course.get("chapters") or []:
        ch_sum = sum(float(u.get("estimated_hours") or 0) for u in ch.get("units") or [])
        declared = float(ch.get("estimated_hours") or 0)
        if abs(ch_sum - declared) > 0.011:
            problems.append(f"{ch.get('id')} 章课时 {declared} ≠ 单元之和 {ch_sum}")
        total += ch_sum
    if budget and total > budget + 0.011:
        problems.append(f"各章课时合计 {total} 超出预算 {budget}")
    return total, budget, problems
