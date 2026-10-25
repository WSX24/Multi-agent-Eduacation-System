# -*- coding: utf-8 -*-
"""各步的出关校验（gate）。

原则：**gate 只做确定性判定**，不做「看起来还行」的语义评判。
语义质量靠模板的 fewshot 与输出纪律，gate 负责「结构不对就别往下传」——
把一份缺页的 PPT 大纲喂给产片引擎，代价是几分钟和一次完整的图源请求。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config  # noqa: F401  —— 必须先插入 PromptTemplate/PPT-Agent 路径
from eval_generic import split_by_names

from .blueprint import budget_report, parse_blueprint

#: teacher_unit 的 10 个模块（顺序即输出顺序）
UNIT_SECTIONS = ["本单元定位", "难度调整说明", "本课目标", "知识讲解",
                 "典型例题", "易错提醒", "小结", "课件大纲", "单元作业", "完成后解锁"]

#: teacher_ppt 的 4 个模块
PPT_SECTIONS = ["课件主题", "整体结构", "逐页大纲", "视觉建议"]

_PAGE_RE = re.compile(r"^第(\d+)页[（(]([^）)]{1,10})[）)]\s*[：:]\s*(.+)$", re.M)


def count_pages(raw: str) -> int:
    """数一份 PPT 大纲里到底有多少页（S5 写 sidecar 要对齐实际页数）。"""
    return len(_PAGE_RE.findall(raw or ""))


@dataclass
class Gate:
    step: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)      # 硬性：不过就停
    warnings: list[tuple[str, bool, str]] = field(default_factory=list)    # 软性：只记录

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        """硬性检查：不通过 → gate.ok 为 False。"""
        self.checks.append((name, bool(ok), detail))

    def warn(self, name: str, ok: bool, detail: str = "") -> None:
        """软性检查：不通过只提示并记入 manifest，不阻断链路。

        用在哪：账目类差异（章课时与单元之和差 0.5）——它不影响下游渲染，
        却会让整条链在第三次重试后直接终止，代价远大于收益。
        """
        self.warnings.append((name, bool(ok), detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks) and bool(self.checks)

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(n, d) for n, ok, d in self.checks if not ok]

    @property
    def all_checks(self) -> list[tuple[str, bool, str]]:
        return self.checks + self.warnings

    def lines(self) -> list[str]:
        out = [f"{'✓' if ok else '✗'} {n}{('  ' + d) if d else ''}"
               for n, ok, d in self.checks]
        out += [f"{'✓' if ok else '⚠'} {n}（软）{('  ' + d) if d else ''}"
                for n, ok, d in self.warnings]
        return out


# ---------------------------------------------------------------------------
# S1 蓝图
# ---------------------------------------------------------------------------
def gate_blueprint(raw: str, weeks: int, hours_per_week: int | None = None,
                   buffer_ratio: float | None = None,
                   focus: str = "", single_text: bool = False) -> tuple[dict | None, Gate]:
    g = Gate("s1_plan")
    try:
        course = parse_blueprint(raw)
    except Exception as exc:                       # noqa: BLE001
        g.add("蓝图可解析", False, f"{type(exc).__name__}: {exc}")
        return None, g
    g.add("蓝图可解析", True)

    chapters = course.get("chapters") or []
    g.add("章数 ≥ 1", len(chapters) > 0, f"{len(chapters)} 章")

    unit_count = sum(len(c.get("units") or []) for c in chapters)
    g.add("单元数 ≥ 1", unit_count >= 1, f"{unit_count} 单元")

    # 规划阶段不得预生成内容：parse_blueprint 已校验，这里只做展示性复核
    pending = all(u.get("lesson_status") == "pending"
                  for c in chapters for u in (c.get("units") or []))
    g.add("单元均为 pending", pending)

    # 课时账目：小差异是记账误差 → 软；大幅超支说明方案排不进工期 → 硬
    total, budget, problems = budget_report(course)
    detail = f"分配 {total} / 预算 {budget}" + (f"；{'；'.join(problems)}" if problems else "")
    if budget and total > budget * 1.2:
        g.add("课时不严重超预算", False, detail + "（超 20% 以上）")
    else:
        g.add("课时不严重超预算", True, detail)
        g.warn("课时账目一致", not problems, detail)

    # 周期/预算必须真的对上「请求」，不能因为「小于 16 周」就勾通过——
    # 早先这里只查了 <16，于是 2 周的短期请求排出 6 周方案也显示 ✓。
    meta = course.get("course") or {}
    total_weeks = float(meta.get("total_weeks") or 0)
    g.warn("总周期与请求一致", abs(total_weeks - weeks) <= 0.5,
           f"蓝图 {meta.get('total_weeks')} 周 / 请求 {weeks} 周")
    if hours_per_week:
        from .scope import BUFFER_RATIO
        buf = BUFFER_RATIO if buffer_ratio is None else buffer_ratio
        expect = int(weeks * hours_per_week * (1 - buf) * 2) / 2
        got = float(meta.get("net_hour_budget") or 0)
        g.warn("预算与口径一致",
               abs(got - expect) <= max(0.5, expect * 0.1),
               f"蓝图 {got:g} / 口径 {weeks}×{hours_per_week}×(1-{buf:g})={expect:g}")
    if focus and single_text:
        g.warn("单篇课文只排 1 章", len(chapters) <= 1, f"{len(chapters)} 章")
        g.warn("单篇课文 1 个单元", unit_count <= 2, f"{unit_count} 单元")
    if focus:
        from .blueprint import focus_score
        from .blueprint import flatten_units
        best = max((focus_score(u, focus) for u in flatten_units(course)), default=0)
        g.warn("蓝图覆盖聚焦主题", best > 0,
               f"聚焦「{focus}」/ 最佳贴合度 {best}" +
               ("" if best else "（蓝图里没有相关章/单元，方案跑偏）"))
    return course, g


# ---------------------------------------------------------------------------
# S3 单元渲染
# ---------------------------------------------------------------------------
def gate_unit(raw: str) -> Gate:
    g = Gate("s3_unit")
    order, bodies = split_by_names(raw, UNIT_SECTIONS)
    missing = [s for s in UNIT_SECTIONS if s not in bodies]
    g.add(f"模块齐全({len(UNIT_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")
    if not missing:
        g.add("模块顺序正确",
              [s for s in UNIT_SECTIONS] == [s for s in order if s in UNIT_SECTIONS],
              f"实际 {order}")
        thin = [s for s in UNIT_SECTIONS if len(bodies[s]) < 2]
        g.add("模块非空", not thin, f"过短 {thin}" if thin else "")
    return g


# ---------------------------------------------------------------------------
# S4 课件大纲
# ---------------------------------------------------------------------------
def gate_outline(raw: str, want_pages: int) -> Gate:
    g = Gate("s4_outline")
    order, bodies = split_by_names(raw, PPT_SECTIONS)
    missing = [s for s in PPT_SECTIONS if s not in bodies]
    g.add(f"模块齐全({len(PPT_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")

    heads = _PAGE_RE.findall(raw)
    nums = [int(n) for n, _, _ in heads]
    g.add("页数达标", len(nums) >= want_pages - 2,
          f"{len(nums)} 页 / 期望 {want_pages}")
    g.add("页序连续", nums == list(range(1, len(nums) + 1)), f"实际 {nums}")

    # 逐页四要素：要点 / 配图 / 旁白
    split = list(_PAGE_RE.finditer(raw))
    bad: list[str] = []
    for i, m in enumerate(split):
        end = split[i + 1].start() if i + 1 < len(split) else len(raw)
        page = raw[m.end():end]
        problems = []
        if "·" not in page and "- " not in page:
            problems.append("无要点")
        if "配图：" not in page:
            problems.append("无配图行")
        if "旁白：" not in page:
            problems.append("无旁白")
        elif len(page.split("旁白：", 1)[1].strip()) < 60:
            problems.append("旁白过短")
        if problems:
            bad.append(f"P{m.group(1)}({'/'.join(problems)})")
    g.add("每页四要素齐全", not bad, f"{bad[:6]}" if bad else "")
    return g
