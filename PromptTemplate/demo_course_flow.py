#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教师 Agent 课程链路 demo —— 规划(M1) → 门控判定 → 单元渲染(M2)

本文件有两重身份：

1. **门控契约的参考实现**。模板只负责「声明解锁条件」（蓝图里的 ``unlock``
   字段），真正判定学生能否进入下一章是后端的硬约束，不能交给 LLM。
   这里的 ``parse_blueprint`` / ``can_unlock`` / ``course_status`` 就是那部分。
2. **链路自检**。用 ``fewshots/teacher_planner.yaml`` 里的示例输出当作
   LLM 回复的替身，验证「蓝图 → 解析 → 取单元 → 组装 M2 变量」整条路走得通，
   无需 API Key。

运行::

    python demo_course_flow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from prompt_builder import PromptLib
from quality_gate import require_inputs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent


# ---------------------------------------------------------------- 蓝图解析
def parse_blueprint(llm_text: str) -> dict:
    """从 M1 的自然语言输出中切出 YAML 蓝图并解析。

    M1 的输出是「学生视图 + 系统蓝图」两段，蓝图以 ``course:`` 开头。
    """
    marker = llm_text.find("course:")
    if marker < 0:
        raise ValueError("未在规划输出中找到 'course:' 蓝图段")
    data = yaml.safe_load(llm_text[marker:])
    if not isinstance(data, dict) or "chapters" not in data:
        raise ValueError("蓝图缺少 chapters 字段")

    # 校验 lesson_status：规划阶段不得预先生成任何内容
    for ch in data["chapters"]:
        for unit in ch.get("units", []):
            if unit.get("lesson_status") != "pending":
                raise ValueError(
                    f"{unit.get('id')} 的 lesson_status 应为 pending，"
                    f"实际为 {unit.get('lesson_status')!r}——规划阶段不得输出讲解内容"
                )
    return data


def _chapter(course: dict, chapter_id: str) -> dict:
    for ch in course["chapters"]:
        if ch["id"] == chapter_id:
            return ch
    raise KeyError(f"蓝图中不存在章节 {chapter_id}")


# ---------------------------------------------------------------- 门控判定
def can_unlock(course: dict, chapter_id: str, progress: dict) -> tuple[bool, str]:
    """判定某章当前是否解锁。

    progress: ``{章节id: {"rate": 0.62, "weak": ["配方法"]}}``，0.62 即正确率 62%。
    返回 ``(是否解锁, 原因说明)``。原因说明用于给学生可见的提示。
    """
    ch = _chapter(course, chapter_id)
    unlock = ch.get("unlock", {"type": "none"})

    if unlock.get("type") == "none":
        return True, "首章，无需前置"

    prev_id = unlock.get("chapter")
    need = unlock.get("min_score", 60)
    prev = progress.get(prev_id)

    if not prev:
        return False, f"尚未完成 {prev_id} 的章节测验"
    if prev.get("rate") is None:
        return False, f"{prev_id} 测验未出分"
    if prev["rate"] * 100 < need:
        return False, (
            f"{prev_id} 测验正确率 {prev['rate']:.0%} 未达 {need}%——"
            f"先补 {prev.get('weak') or '薄弱点'} 再来"
        )
    return True, f"{prev_id} 测验正确率 {prev['rate']:.0%}，已达标"


def course_status(course: dict, progress: dict) -> list[str]:
    """输出整门课的关卡地图（学生视图用）。"""
    lines = []
    for ch in course["chapters"]:
        ok, why = can_unlock(course, ch["id"], progress)
        flag = "✅ 已解锁" if ok else "🔒 未解锁"
        lines.append(
            f"{flag}  {ch['id']} {ch['title']}"
            f"（{len(ch.get('units', []))} 单元 + {ch['assessment']['type']}）— {why}"
        )
    return lines


# --------------------------------------------------- 蓝图 → M2 变量组装
def unit_variables(
    course: dict,
    chapter_id: str,
    unit_index: int,
    progress: dict | None = None,
    need_example: bool = True,
) -> dict:
    """把蓝图状态映射成 ``teacher_unit`` 所需的 12 个变量。

    这是 M1 与 M2 之间唯一的映射来源——评测与生产都走这里，
    避免两处各写一份而逐渐偏离。

    三个学情字段**刻意不同源**（早先两个字段同源，拼出来是病句，而且让模型
    以为手里有两份独立证据）：

    | 变量 | 取自 | 作用 |
    |---|---|---|
    | 上一章测验结果 | 章节测验正确率 | **主依据**：数值驱动升/降档 |
    | 学情数据 | 近期练习数据或单元完成度 | 辅助信息，不与主依据重复 |
    | 前置薄弱点 | 错题归因列表 | 告知在哪里反复强调 |
    """
    progress = progress or {}
    meta = course.get("course", {})
    ch = _chapter(course, chapter_id)
    units = ch["units"]
    unit = units[unit_index]

    prev_id = ch.get("unlock", {}).get("chapter")
    rec = (progress.get(prev_id) or {}) if prev_id else {}

    # ① 上一章测验结果：主依据（inputs 契约里是 percent 类型 + 嗲兵值）
    if not prev_id:
        prev_result = "无（首章）"
    elif rec.get("rate") is None:
        prev_result = "无数据"
    else:
        prev_result = f"正确率 {rec['rate']:.0%}"

    # ② 学情数据：只装「主依据与薄弱点都没说」的信息，避免重复注入同一个数字
    practice = rec.get("practice") or {}          # 可选：{"days": 7, "count": 45, "rate": 0.62}
    done = len(rec.get("units_done") or [])
    total_units = len(_chapter(course, prev_id)["units"]) if prev_id else 0
    if practice.get("rate") is not None:
        stu_data = (f"近 {practice.get('days', 7)} 天练习 {practice.get('count', 0)} 题，"
                    f"正确率 {practice['rate']:.0%}")
    elif prev_id and done:
        stu_data = (f"{prev_id} 已完成 {done}/{total_units} 个单元，"
                    f"暂无近期练习数据")
    else:
        stu_data = "数据不足"

    # ③ 前置薄弱点：错题归因列表
    prev_weak = "、".join(rec.get("weak") or []) or "无"

    # 本章含复习单元时，开启复习衔接
    has_review = any(u.get("type") == "复习" for u in units)

    return {
        "学科": meta.get("subject", ""),
        "当前章节": f"{ch['title']}",
        "本单元名称": unit["title"],
        "本单元序号": unit_index + 1,
        "本章单元总数": len(units),
        "本单元课时": unit["estimated_hours"],
        "上一章测验结果": prev_result,
        "学情数据": stu_data,
        "前置薄弱点": prev_weak,
        "学生水平": meta.get("student_level", "基础"),
        "need_example": "true" if need_example else "false",
        "是否安排复习": "true" if has_review else "false",
    }


def build_unit_prompt(
    lib: PromptLib,
    course: dict,
    chapter_id: str,
    unit_index: int,
    progress: dict | None = None,
    need_example: bool = True,
) -> str:
    """组装 ``teacher_unit`` 的完整 Prompt。

    刻意不挂 Few-shot：消融实验显示格式合规率不因此变化（各 5 次均 100%），
    而去掉后可省约 40% token，并消除同题撞车抄袭的风险。

    调用前先做**入口预检**：蓝图里字段为空（如学科为空）时直接拒绝，不把
    「缺输入」交给模型——助教评测已证明模型会用编造内容填满空白。
    """
    variables = unit_variables(course, chapter_id, unit_index, progress, need_example)
    require_inputs(lib, "teacher_unit", variables)
    return lib.build("teacher_unit", variables, fewshot=False)


def build_planner_prompt(lib: PromptLib, variables: dict) -> str:
    """组装 M1 规划 Prompt（同样先过入口预检）。"""
    require_inputs(lib, "teacher_planner", variables)
    return lib.build("teacher_planner", variables)


def next_unlocked_unit(course: dict, progress: dict) -> tuple[str, int] | None:
    """找出下一章待渲染的单元（该章已解锁且尚未生成内容）。"""
    for ch in course["chapters"]:
        if not can_unlock(course, ch["id"], progress)[0]:
            return None
        for i, unit in enumerate(ch["units"]):
            produced = progress.get(ch["id"], {}).get("units_done", [])
            if unit["id"] not in produced:
                return ch["id"], i
    return None


# ---------------------------------------------------------------- demo
def main() -> None:
    lib = PromptLib(ROOT)

    print("=" * 72)
    print("M1 规划：Prompt 已就绪，此处用 Few-shot 示例输出模拟 LLM 回复")
    print("=" * 72)
    planner_prompt = build_planner_prompt(lib, {
        "学科": "初中数学",
        "学习目标": "掌握一元二次方程的解法并能解应用题",
        "总周期": "4周",
        "每周可投入时间": "4课时",
        "学生水平": "基础",
    })
    print(f"planner prompt: {len(planner_prompt)} 字符\n")

    fake_llm_reply = yaml.safe_load(
        (ROOT / "fewshots" / "teacher_planner.yaml").read_text(encoding="utf-8")
    )["examples"][0]["output"]

    course = parse_blueprint(fake_llm_reply)
    print(f"✅ 蓝图解析成功：{len(course['chapters'])} 章，"
          f"总 {sum(len(c['units']) for c in course['chapters'])} 单元，"
          f"全部 lesson_status=pending（规划阶段未预生成内容）\n")

    print("=" * 72)
    print("门控判定：学生刚做完第 1 章测验，正确率 58% 和 72% 两种情形")
    print("=" * 72)
    for rate in (0.58, 0.72):
        progress = {"ch01": {"rate": rate, "weak": ["配方法"], "units_done": [
            u["id"] for u in course["chapters"][0]["units"]
        ]}}
        print(f"\n—— 假设第1章测验正确率 {rate:.0%} ——")
        for line in course_status(course, progress):
            print("  " + line)

    print()
    print("=" * 72)
    print("M2 渲染：第1章已达标，取第一个单元组装 Prompt")
    print("=" * 72)
    progress = {
        "ch01": {
            "rate": 0.72,
            "weak": ["配方法"],
            "units_done": [u["id"] for u in course["chapters"][0]["units"]],
        }
    }
    target = next_unlocked_unit(course, progress)
    if not target:
        print("无可渲染单元（课程已完结或未解锁）")
        return
    chapter_id, idx = target
    print(f"目标单元: {chapter_id} / 第 {idx + 1} 个\n")

    unit_prompt = build_unit_prompt(lib, course, chapter_id, idx, progress)
    head = unit_prompt[:unit_prompt.index("【难度自适应规则】")]
    print(head)
    print("...（后略：难度自适应规则 / 输出格式 / 输出纪律 + teacher Few-shot）")
    print(f"\nunit prompt: {len(unit_prompt)} 字符")


if __name__ == "__main__":
    main()
