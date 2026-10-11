# -*- coding: utf-8 -*-
"""单元渲染输出 → PPT 输入的桥接。

`teacher_unit` 渲染出 10 个模块，但只有一部分适合喂给 `teacher_ppt`：

  · **内容来源**（讲什么）：本课目标 / 知识讲解 / 典型例题 / 易错提醒 / 小结
  · **页骨架参照**（怎么分页）：课件大纲 —— 它本身就是「P1 页标题 — 该页要点」的页级大纲

其余（本单元定位 / 难度调整说明 / 单元作业 / 完成后解锁）是教学管理信息，不上 PPT，
传过去只会把模型带跑（去写作业、解锁提示）。

为什么要把「课件大纲」也带上（2026-10-11 补）：
  单元渲染**已经产出了页级大纲**，而 PPT 阶段原来却从 5 个模块里**从零重新组织页**——
  两者本该对齐：内容（上面 5 段）+ 页骨架（课件大纲）一起给 PPT，让 PPT **参照**单元
  自己的分页，而不是另起一套页序。
"""
from __future__ import annotations

from eval_generic import split_by_names

#: 教学内容的 5 个模块（喂给 PPT 的「内容来源」）
UNIT_TO_PPT_SECTIONS = ["本课目标", "知识讲解", "典型例题", "易错提醒", "小结"]
#: teacher_unit 的完整 10 模块（用作切段边界）。
#: 只用上面 5 个去切，`split_by_names` 会按「匹配到的模块名」切段——「小结」会吞掉后面
#: 不匹配的「课件大纲/单元作业/完成后解锁」（它们本该被单独处理），所以切段必须用全清单。
UNIT_ALL_SECTIONS = ["本单元定位", "难度调整说明", "本课目标", "知识讲解",
                     "典型例题", "易错提醒", "小结", "课件大纲", "单元作业", "完成后解锁"]


def unit_output_to_ppt_content(unit_output: str) -> str:
    """把 `teacher_unit` 的 10 模块输出 → `teacher_ppt` 的「单元内容」。

    取 5 个教学内容模块（内容来源）+ 第 8 模块【课件大纲】（页骨架参照）。
    提取失败（模块不全 / 传进来的不是 unit 输出）时**原样返回**——宁可多传，不丢内容。
    """
    text = unit_output or ""
    try:
        _, bodies = split_by_names(text, UNIT_ALL_SECTIONS)
    except Exception:                                   # noqa: BLE001 —— 桥接不该抛
        return text
    if not all(s in bodies for s in UNIT_TO_PPT_SECTIONS):
        return text
    parts = [f"【{s}】\n{bodies[s]}" for s in UNIT_TO_PPT_SECTIONS]
    outline = (bodies.get("课件大纲") or "").strip()
    if outline:
        parts.append(
            "【上游页级大纲（参照分页：页序与页标题照它走，内容来自上面的教学模块）】\n"
            + outline)
    return "\n\n".join(parts)
