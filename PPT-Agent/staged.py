"""阶段化执行引擎（Plan-and-Execute）：把 ppt-master 的确定性流程交给代码，只把判断点交给模型。

对照：``agent.py`` 里的 ReAct 循环
--------------------------------------
ReAct 模式下，**每一步该做什么都由模型决定**。而 ppt-master 的 Default 路由其实已经是一条
写好的流水线（Step 1–7 + 26 条写死的命令行 + BLOCKING 门），模型只是把它重读一遍再抄一遍。
代价是实测 ~65 次往返（每次 1.5~4 秒）以及"把文档里的命令抄错"这类纯人为失误。

本模块把职责切开：

    ┌────────────────────────────────────────────────────────────┐
    │ P0 PREPARE  纯代码  guard → 选路 → 注入权威文档             │
    │ P1 PLAN     模型×1  只做真正要判断的：契约 / 页清单 / 视觉   │
    │ P2 FREEZE   代码+模型×1  init / scaffold / validate / 字体标定│
    │ P3 EXECUTE  模型×N  每页一次，只写 SVG                      │
    │ P4 VERIFY   代码+模型×k  校验 → 只把失败页交回模型修         │
    │ P5 EXPORT   纯代码  svg_to_pptx                             │
    └────────────────────────────────────────────────────────────┘

**流程由代码驱动**：模型不再决定"下一步干什么"，只在被点名时产出指定内容。
持久化同一条消息链，让前缀缓存命中，也让已注入的文档只进入一次。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent import MAX_DOC_OBSERVATION_CHARS, PPTAgent, clip, get_python_command

# ---------------------------------------------------------------------------
# 阶段 0 需要注入的文档（由代码直接读入对话，不消耗模型往返）
# ---------------------------------------------------------------------------

#: 规划阶段（P1）必须读的文档
PLAN_DOCS: Tuple[str, ...] = (
    "workflows/generate-pptx.md",
    "references/strategist.md",
    "references/plan-core.md",
    "references/canvas-formats.md",
)

#: 规划能力批次（P1 的候选来源，skill 要求"一次读完"）
PLAN_CAPABILITY_DOCS: Tuple[str, ...] = (
    "references/modes/_index.md",
    "references/visual-styles/_index.md",
    "references/image-renderings/_index.md",
)

#: 设计文件的权威 schema。**必须注入**：project_manager validate 是严格 schema 校验，
#: 它要求字段名精确、枚举值精确（如 canvas.format 必须等于 'PPT 16:9'、
#: icons.stroke_width 必须是 1.5/2/3）。模型猜不出来，代码必须把 schema 给它。
SPEC_SCHEMA_DOCS: Tuple[str, ...] = (
    "templates/schemas/design_spec.schema.json",
    "templates/schemas/spec_lock.schema.json",
    # spec_lock 的**人读规则**不在上面那份 JSON schema 里：
    # grep "user\|baseline" spec_lock.schema.json → 零命中。规则只住在这三份里：
    #   templates/spec_lock_reference.md / references/strategist.md / scripts/docs/project.md
    # 而脚手架生成的 spec_lock.md 里没有任何指路链接。后果是真跑实测到的：
    # `## forbidden` 下模型新加了一条通用规范（“禁止用斜杠代替分数”），
    # validate 报 "not a baseline rule and lacks the (user) tag"，模型不知道规则，
    # 只能猜 —— 第 2/3 轮把 (user) 写进了条目正文，越修越错，活活拖死 P2。
    # 规则原文：非 baseline 行必须是**用户原话逐字引用**并以 (user) 结尾，
    # 通用规范压根不应该进 lock（正确动作是把那行删掉）。
    # 只多 10KB，换掉一个能把整个阶段拖死的盲区。
    "templates/spec_lock_reference.md",
)

#: 执行阶段（P3）核心（skill 要求"一起读完"）
#: 执行阶段（P3）核心（skill 要求“一起读完”）
#:
#: 为什么必须包含 executor-chart.md（2026-10-08 补）：
#: 这份 deck 里 10/12 页含函数图像/切线/割线，属于 skill 的「value-driven geometry」
#: 分支（"source values determine visible geometry"）。而该分支的硬规则与工具都在
#: executor-chart.md 里：
#:   · "data owns the marks … **Do not eyeball positions** … calculate marks from the values"
#:   · skill 提供的坐标计算器 `svg_position_calculator.py`（全仓唯一提到它的文档就是这份）
#: 实测后果：不注入它，P3 就不知道有计算器、也不知道“不许凭感觉定位”，
#: 只能凭手感画曲线。exec=low 时模型的推理能力硬扛住了（慢速版几何是对的），
#: 一关推理就崩：割线全部汇聚到曲线端点而不过自称的 P，曲线画成 S 形，坐标轴都丢了。
#: 代价只有 +8.4KB（101KB → 110KB）。
EXECUTE_CORE_DOCS: Tuple[str, ...] = (
    "references/shared-standards-core.md",
    "references/executor-base.md",
    "references/executor-chart.md",
    "references/semantic-svg.md",
    "references/preset-shape-vocabulary.md",
)

#: schema 标记 → 目标文件名。这两个标记是 validate 强制要求的，
#: 所以可以**按内容识别文件**，而不依赖模型的标签写法。
SCHEMA_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("design-spec/v1", "design_spec.md"),
    ("spec-lock/v1", "spec_lock.md"),
)

#: 文字超界的报错里自带全部算术所需数据，由代码解出具体修正量。
#: 两种句式都要收：
#:   (a) 对容器： <text> (...) exceeds <g id="X"> data-pptx-bounds on the horizontal axis:
#:       content (..)-(..), container (..)-(..), overflow horizontal 6.5%, ... ≈77.2 px per CJK char at 72px
#:   (b) 对画布： <text> (...) exceeds the root viewBox on the horizontal axis:
#:       content (..)-(..), canvas (0.0, 0.0)-(1280.0, 720.0), overflow horizontal 6.1%, ...
_BOUNDS_RE = re.compile(
    r"<(?P<tag>text|g|image)\b[^>]*>.*?exceeds\s+(?:<g\s+id=\"(?P<owner_id>[^\"]+)\"[^>]*>\s*data-pptx-bounds"
    r"|the\s+root\s+viewBox)\s+on\s+the\s+(?P<axis>horizontal|vertical)\s+axis:\s*"
    r"content\s*\((?P<cx1>[-\d.]+),\s*(?P<cy1>[-\d.]+)\)-\((?P<cx2>[-\d.]+),\s*(?P<cy2>[-\d.]+)\),\s*"
    r"(?P<container_kind>container|canvas)\s*\((?P<kx1>[-\d.]+),\s*(?P<ky1>[-\d.]+)\)-\((?P<kx2>[-\d.]+),\s*(?P<ky2>[-\d.]+)\)",
    re.DOTALL,
)
_RATE_RE = re.compile(r"≈\s*(?P<rate>[\d.]+)\s*px per (?:CJK|mixed) char at\s*(?P<size>[\d.]+)px")
#: 报错尾部的「≈N chars fit in W px」——checker 直接给出了容器单行容量，
#: 用来判断“文字太长”这一种扩框/挪位都救不了的超界（需缩短或换行）。
_FIT_CHARS_RE = re.compile(r"≈\s*(?P<chars>[\d.]+)\s*chars fit in\s*[\d.]+\s*px")
_OVERLAP_RE = re.compile(
    r"<g\s+id=\"(?P<a>[^\"]+)\"\s*>\s*data-pptx-bounds\s+overlaps\s+<g\s+id=\"(?P<b>[^\"]+)\"\s*>\s*"
    r"data-pptx-bounds\s+by\s+(?P<w>[\d.]+)px\s*x\s*(?P<h>[\d.]+)px"
)


MAX_PAGE_REPAIRS = 2
#: 无 file 字段的 scope 级缺陷归类到这个键，路由到计划层修复
PLAN_SCOPE_KEY = "__plan__"

# ---------------------------------------------------------------------------
# ⚠️ 重试循环的不变量（请勿删——它是一类已重复两次的事故的防复发说明）
# ---------------------------------------------------------------------------
#
# 本文件共有三处「校验 ↔ 修复」重试循环，分两种形态：
#
#   A. 先查后修（phase_freeze / phase_verify）
#        for attempt in range(N + 1):
#            ok = check()
#            if ok or attempt >= N: break
#            repair(attempt + 1)
#      → **N 次修复必须配 N + 1 次校验**。写成 range(1, N + 1) 就会变成
#        「查→修」各 N 次后直接放弃：**最后一轮修复的成果永不被复查**，
#        而导出读到的就是那份过期报告。
#
#   B. 先修后查（_repair_spec）
#        for i in range(1, N + 1):
#            repair(i)
#            if check(): return
#      → 形态 B 的 range(1, N + 1) 是**正确**的：最后一次修复的后一道动作就是
#        校验，它天然满足不变量。**不要因为形态像就去改它。**
#
# 已发生的事实：形态 A 的两个循环先后都写错过（2026-10-08）。第一次只修了
# phase_freeze，漏了 phase_verify，于是又拦下了一份合格 deck。
# 【教训】修「模式性缺陷」时必须 grep 同模式的所有实例，而不是只修眼前那一个。
#
# 除了把两个循环改对外，phase_export 还有一道**行为层兜底**：
# `_ensure_fresh_quality_report()` 在导出前无条件重跑 checker。所以即使将来
# 又有人把循环写错，**后果也已被隔离**——最多白修一轮，不会误拒一份合格 deck。
# 能这样兜的理由：checker 是纯代码，实测 0.6~2.2s，占整跑不到 1%。

# ---------------------------------------------------------------------------
# 分阶段推理强度（环境变量驱动，不改 agent.py）
# ---------------------------------------------------------------------------
# `agent.py --thinking` / `--reasoning-effort` 是**全局**开关，一开全开。
# 但各阶段对推理的依赖差得很远，而 `call_model` / `_ask` 早就支持单次覆盖——
# 只是从来没接上（`_ask` 的 docstring 里写着「P4 修复关思考」，实际一个调用点都没传）。
#
# ⚠️ 调低执行侧之前先读这段：README §7 有实测——
#     `--thinking disabled` 单次快 6~8 倍，**但交付失败**（模型调用 4→9 次，
#     仍留 1 个 blocking 错，导出被 release gate 拒）。原因是 P3/P4 干的正是
#     SVG 坐标与字体度量的**算术活**，关掉思考就只能试错，而试错比推理更贵。
#     `--reasoning-effort none` 这一档**当时没做过交付测试**，所以它是未知，
#     不是已验证可行。两个环境变量都不设 = 完全沿用 agent.py 的全局设置。
#
# 另一个现实约束：跳过修复也**不保证出得了片**。`svg_to_pptx.py` 的 release gate
# 在 blocking 错 > 0 时直接 Error 退出（`_quality_gate_status` 要求 blocking_count == 0）。
# 所以这些旋钮只能把「快」调上去，能不能出片仍由那道门判定。
STAGE_REASONING_ENV = {"plan": "PPT_STAGE_REASONING_PLAN",
                       "exec": "PPT_STAGE_REASONING_EXEC",
                       # P4 单独一档。理由（实测 2026-10-08 19:29 那次）：
                       #   P3 关推理后几何仍然对（④ 保证），但会出现**版面自相冲突**
                       #   （模型把图画出了自己画的卡片，撞上全宽公式条）。这类错
                       #   没有机械解——嵌套不行（谁也没包住谁）、收 bounds 不行
                       #   （内容真的在那里）。实测 P4 在 none 下修 2 轮无果，7 次
                       #   调用 ~110s 全白花。把推理只还给这一个环节，是最便宜的修法：
                       #   8 次 P3 保持快，1~2 次 P4 拿到判断力。
                       "repair": "PPT_STAGE_REASONING_REPAIR"}
MAX_PAGE_REPAIRS_ENV = "PPT_MAX_PAGE_REPAIRS"
REASONING_CHOICES = ("none", "low", "medium", "high", "max")

#: P2 设计文件校验失败时的重试轮数（在所属层修复，而不是往下跑）
SPEC_REPAIR_ROUNDS = 3

# ---------------------------------------------------------------------------
# 讲稿（speaker notes）：任务文本 → notes/total.md → PPTX 备注
# ---------------------------------------------------------------------------
# 链路上游（PPT-Agent/pipeline.py 用 ppt_general / teacher_ppt 模板产出的大纲）
# 每页都带一行「旁白：」。两个模板都把旁白当**能照着讲的稿子**写的
# （teacher_ppt 更明写「100-250 字、用于配音」）。而导出命令一直是
# `--quick-generate --no-notes`，于是这段花钱让模型写出来的内容一个字都没进 PPTX。
#
# 这里把「取旁白 → 写 notes/total.md → total_md_split → --with-notes」接上。
# 为什么不交给模型：旁白是**上游已经写好的原文**，让执行体再「写一遍讲稿」
# 只会得到一个改写版，不是原来那份。直接从任务文本里取，零保真损失。
_PAGE_MARK_RE = re.compile(r"^[ \t]*第\s*(\d{1,3})\s*页(?:[（(][^）)]*[）)])?", re.M)
_NARRATION_RE = re.compile(r"旁白[：:]\s*(\S.*)")


# ---------------------------------------------------------------------------
# 返工的内容完整性闸门
# ---------------------------------------------------------------------------
# 实测事故（2026-10-08 项目 ..._194000，exec=none + P4=low）：
#   P3 产出 13 个 blocking / 7 页 → P4 逐页“定向修复” → 7 页全被重写。
#   其中 03 页的“三步列表”面板被压成 **78px 宽的窄条**，文字全删，只剩序号。
#   而 svg_quality_checker 只查结构，**查不出内容被删** —— 它报的是错误 0。
#
# 关键认知：这不是“关推理导致内容变差”，而是“关推理导致更多返工，
# 而返工为了消错把内容砍了”。内容损失与返工次数成正比。
#
# 所以要一道内容闸门：返工前后比对可见文字，丢了就回投一次，仍丢则记录。
_SVG_TEXT_RE = re.compile(r"<text\b[^>]*>(.*?)</text>", re.DOTALL)


def visible_texts(svg: str) -> List[str]:
    """取出 SVG 里的可见文字（去标签、去空白），用于返工前后的内容比对。"""
    out: List[str] = []
    for m in _SVG_TEXT_RE.finditer(svg or ""):
        s = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", m.group(1)))
        if s:
            out.append(s)
    return out


def content_loss(before: str, after: str, *, min_chars: int = 12,
                 drop_ratio: float = 0.25) -> List[str]:
    """返工丢掉了哪些可见文字。

    **阀值化而不要求一字不差**：修复“文字超容器”本来就靠改写措辞（把长句改短），
    那是合法且必要的。真正不该发生的是**整条删除**。所以用两个条件同时成立才判丢：
      · 总字数减少超过 ``drop_ratio``
      · 至少少了 ``min_chars`` 个字
    这样“改短一句”（降 10% 上下）不会误报，而“删掉三步列表”（降 50%+）会被拦。

    返回丢掉的那几条具体文字，供回投时点名要求保留。
    """
    b, a = visible_texts(before), visible_texts(after)
    chars_before, chars_after = sum(map(len, b)), sum(map(len, a))
    if chars_before - chars_after < min_chars:
        return []
    if chars_after > chars_before * (1.0 - drop_ratio):
        return []
    from collections import Counter
    pool = Counter(a)
    lost: List[str] = []
    for s in b:
        if pool.get(s, 0) > 0:
            pool[s] -= 1
        else:
            lost.append(s)
    return lost


def extract_narrations(task: str) -> List[str]:
    """按页序取出任务里每页的旁白；取不到就返回空列表（不猜、不编）。"""
    marks = list(_PAGE_MARK_RE.finditer(task or ""))
    out: List[str] = []
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(task)
        hit = _NARRATION_RE.search(task[m.end():stop])
        out.append(hit.group(1).strip() if hit else "")
    return out


# ---------------------------------------------------------------------------
# spec_lock 的完整性：计划声明的字号角色，必须一个不少地进锁
# ---------------------------------------------------------------------------
# 实测事故（8 页 deck 的真实返工来源）：
#   计划里 typography 有 5 个角色（display/title/subtitle/body/annotation）；
#   design_spec.md 也写了 `| Annotation | 18 |`；
#   但 spec_lock.md 的 ## typography 只剩脚手架发下来的 body/title 两行
#   —— 模型把「填表」理解成「把脚手架给我的格子填满」，没把计划里多出来的角色补进去。
#   而 P3 只读锁，于是正文里用了 109 处 font-size 18（annotation），
#   检查器报 `undeclared font-size 18 ... exceeds the sparse-display limit of 2`，
#   8 页每页一个同构 blocking 错 → P4 逐页返工 8 次。
#   而 references/executor-base.md 早写了 `annotation` / `footnote` 是预期存在的角色。
#
# 最阴的一点：`project_manager validate` **不查这件事**，错要到 P4 才炸，
# 代价是整趟 18 次模型调用里的 8 次。所以在这里用代码先拦一道。
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_LOCK_KV_RE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", re.M)


def lock_section_keys(text: str, section: str) -> set:
    """取出 ``spec_lock.md`` 某个 ``## 段`` 里的行键集合。"""
    marks = list(_SECTION_RE.finditer(text or ""))
    for i, m in enumerate(marks):
        if m.group(1).strip() != section:
            continue
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        return {kv.group(1) for kv in _LOCK_KV_RE.finditer(text[m.end():stop])}
    return set()


def plot_page_figure(figure: Any) -> Tuple[str, str]:
    """把计划里的 ``figure`` 声明解成给模型的几何文本块。

    返回 ``(文本块, 错误说明)``——**永不抛异常**。

    为什么不能抛：表达式来自模型输出的 JSON，它写错（域不合法、分母为零、
    用了不支持的函数）是完全正常的事。报错会停掉整份 deck，而正确行为是
    **降级回“不带几何块”的老路径**：日志里说清原因，模型自己画。
    宁可少一张精确图，也不要因为一句话的语法错误毁掉 12 页。
    """
    import plot_math  # 同目录，惰性导入：不用函数图的 run 不必付这个开销

    if not isinstance(figure, dict):
        return "", ""
    kind = str(figure.get("kind") or "none").strip().lower()
    if kind in ("", "none"):
        return "", ""
    if kind not in ("function_plot", "conic", "solid3d", "parametric", "revolution", "formula"):
        return "", (f"figure.kind={kind!r} 不是代码支持的取值"
                     "（function_plot/conic/solid3d/parametric/revolution/formula/none）")
    try:
        if kind == "formula":
            return plot_math.formula_layout(
                str(figure.get("template") or "fraction"),
                x=float(figure.get("x") or 0), y=float(figure.get("y") or 0),
                font_size=float(figure.get("font_size") or 24),
                gap=float(figure.get("gap") or 16),
                lhs=str(figure.get("lhs") or ""),
                lim_sub=str(figure.get("lim_sub") or ""),
                numerator=str(figure.get("numerator") or ""),
                denominator=str(figure.get("denominator") or ""),
                radicand=str(figure.get("radicand") or ""),
                index=str(figure.get("index") or "")), ""
        if kind == "revolution":
            prof = figure.get("profile")
            if not (isinstance(prof, (list, tuple)) and len(prof) >= 2):
                raise ValueError("revolution.profile 应为 [[r,h], ...]（≥2 个点）")
            rv = plot_math.revolution_geometry(
                prof,
                segments=int(figure.get("segments") or 48),
                meridians=figure.get("meridians") or [0, 90, 180, 270],
                ring_count=int(figure.get("ring_count") or 3),
                azimuth=float(figure.get("azimuth") or 30),
                elevation=float(figure.get("elevation") or 20),
                scale=float(figure.get("scale") or 60),
                origin=figure.get("origin") or [640, 400])
            return plot_math.format_revolution(rv), ""
        if kind == "parametric":
            x_expr = str(figure.get("x") or "").strip()
            y_expr = str(figure.get("y") or "").strip()
            if not x_expr or not y_expr:
                raise ValueError("parametric 需要 x 和 y（关于 t 的表达式）")
            pc = plot_math.param_curve_geometry(
                x_expr, y_expr, figure.get("t") or [0, 1],
                view=figure.get("view") or [-5, 5, -5, 5],
                area=figure.get("area") or [560, 140, 1180, 620],
                samples=int(figure.get("samples") or 129))
            return plot_math.format_param_curve(pc), ""
        if kind == "solid3d":
            verts = figure.get("vertices")
            preset = figure.get("preset")
            if not preset and (not isinstance(verts, dict) or not verts):
                raise ValueError("solid3d 需要 vertices（{点名: [x,y,z]}）或 preset")
            edge_list = []
            for e in (figure.get("edges") or []):
                if isinstance(e, dict):
                    edge_list.append(e)
                elif isinstance(e, (list, tuple)):
                    edge_list.append(list(e))
            so = plot_math.solid3d_geometry(
                verts if isinstance(verts, dict) else None,
                edges=edge_list,
                faces=[f for f in (figure.get("faces") or []) if isinstance(f, dict)],
                preset=str(preset) if preset else None,
                n=int(figure.get("n") or 4),
                radius=float(figure.get("radius") or 2),
                height=float(figure.get("height") or 3),
                size=float(figure.get("size") or 2),
                azimuth=float(figure.get("azimuth") or 30),
                elevation=float(figure.get("elevation") or 20),
                projection=str(figure.get("projection") or "orthographic"),
                distance=float(figure.get("distance") or 8),
                scale=float(figure.get("scale") or 80),
                origin=figure.get("origin") or [640, 400])
            return plot_math.format_solid3d(so), ""
        if kind == "conic":
            ctype = str(figure.get("type") or "").strip().lower()
            view = figure.get("view")
            area = figure.get("area")
            if not (isinstance(view, (list, tuple)) and len(view) == 4):
                raise ValueError("conic.view 应为 [xmin, xmax, ymin, ymax]")
            if not (isinstance(area, (list, tuple)) and len(area) == 4):
                raise ValueError("conic.area 应为 [x左, y上, x右, y下]")
            center = figure.get("center") or [0, 0]
            if not (isinstance(center, (list, tuple)) and len(center) == 2):
                raise ValueError("conic.center 应为 [h, k]（数学坐标）")
            cpoints = figure.get("points") or []
            cchords = figure.get("chords") or []
            ctangents = figure.get("tangents") or []
            if not isinstance(cpoints, (list, tuple)):
                raise ValueError("conic.points 应为列表")
            if not isinstance(cchords, (list, tuple)):
                raise ValueError("conic.chords 应为列表")
            if not isinstance(ctangents, (list, tuple)):
                raise ValueError("conic.tangents 应为列表")
            conic = plot_math.conic_geometry(
                ctype,
                a=float(figure.get("a") or 0), b=float(figure.get("b") or 0),
                p=float(figure.get("p") or 0),
                center=[float(center[0]), float(center[1])],
                view=view, area=area,
                samples=int(figure.get("samples") or 65),
                points=[d for d in cpoints if isinstance(d, dict)],
                chords=[list(c) for c in cchords if isinstance(c, (list, tuple))],
                tangents=[str(t) for t in ctangents],
                free_points=[d for d in (figure.get("free_points") or [])
                             if isinstance(d, dict)],
                lines=[d for d in (figure.get("lines") or []) if isinstance(d, dict)],
                intersections=[d for d in (figure.get("intersections") or [])
                               if isinstance(d, dict)],
                ext_tangents=[d for d in (figure.get("ext_tangents") or [])
                              if isinstance(d, dict)])
            return plot_math.format_conic(conic), ""

        expr = str(figure.get("expr") or "").strip()
        domain = figure.get("domain")
        area = figure.get("area")
        if not expr:
            raise ValueError("figure 缺 expr")
        if not (isinstance(domain, (list, tuple)) and len(domain) == 2):
            raise ValueError("figure.domain 应为 [xmin, xmax]")
        if not (isinstance(area, (list, tuple)) and len(area) == 4):
            raise ValueError("figure.area 应为 [x左, y上, x右, y下]")
        secants = figure.get("secants") or []
        if not isinstance(secants, (list, tuple)):
            raise ValueError("figure.secants 应为列表")
        point = figure.get("point")
        points = figure.get("points")
        sharp = figure.get("sharp")
        if points is not None and not isinstance(points, (list, tuple)):
            raise ValueError("figure.points 应为列表")
        geo = plot_math.plot_geometry(
            expr=expr, domain=domain, area=area,
            point=None if point is None else float(point),
            points=None if points is None else [float(v) for v in points],
            sharp=None if sharp is None else float(sharp),
            secants=[float(v) for v in secants],
            samples=int(figure.get("samples") or 65),
            y_range=figure.get("y_range") or None,
        )
        return plot_math.format_geometry(geo), ""
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：降级而不是中断
        return "", f"figure 计算失败（{type(exc).__name__}: {exc}），本页回退为模型自画"


#: 页面内容里出现这些词 → 该页必须有 conic 声明
_CONIC_CUES = ("椭圆", "双曲线", "抛物线", "圆锥曲线", "圆心", "半径", "圆的切线")
#: 出现这些词 → 必须有 solid3d 声明
_SOLID_CUES = ("正方体", "长方体", "棱柱", "棱锥", "正四面体", "正八面体",
               "三棱锥", "四棱锥", "立体图形", "空间几何")
#: 出现这些词 → 必须有 parametric 声明
_PARAM_CUES = ("摆线", "心形线", "双纽线", "参数曲线")
#: 有函数图且出现这些词 → function_plot 必须带 point/points/sharp
_FUNC_GEOM_CUES = ("切点", "切线", "割线", "尖点", "相切")


#: 出现这些词 → 必须有 formula 声明（仅限真正需要排版的：分子/分母、根号、lim 极限式）。
#: 不要再放「定义式/点斜式」这类词——它们是线性公式或泛指，不需要 formula，放了会大量误报。
_FORMULA_CUES = ("分式", "根号", "lim")


#: 出现这些词 → 该页应有函数图/圆锥曲线图（用于梀「figure=none 却涉及几何」的漏网）
_GEOM_NONE_CUES = ("切线", "割线", "切点", "尖点", "椭圆", "双曲线", "抛物线",
                   "圆锥曲线")


def figure_completeness_errors(roster: Any) -> List[str]:
    """扫 plan 的 roster，找出「内容涉及某类图形、但 figure 未声明对应 kind」的页。

    为什么需要它（提示词红线不够硬）：模型在 P1 过载时会把该用代码能力的页
    （圆锥曲线/立体/参数曲线/切点）漏声明成 kind:none，然后在 P3 自己手画 ——
    实测弦端点飘、切线飞、曲线画成斜直线。这条校验在 P1 出 plan 后立刻打回。

    返回缺陷列表（空 = 通过）。
    """
    if not isinstance(roster, list):
        return []
    errors: List[str] = []
    for page in roster:
        if not isinstance(page, dict):
            continue
        fid = str(page.get("id") or page.get("file") or "?")
        text = " ".join([
            str(page.get("title") or ""),
            str(page.get("core_message") or ""),
            " ".join(str(p) for p in (page.get("content_points") or [])),
        ])
        figs = page.get("figure")
        if isinstance(figs, list):
            fig_list = [f for f in figs if isinstance(f, dict)]
        elif isinstance(figs, dict):
            fig_list = [figs]
        else:
            fig_list = []
        kinds = {str(f.get("kind") or "none").lower() for f in fig_list}
        if any(c in text for c in _CONIC_CUES) and "conic" not in kinds:
            errors.append(f"{fid}：内容涉及圆/圆锥曲线，但 figure 未声明 `conic`"
                          f"（否则模型会手画，弦/切线/端点全飘）")
        if any(c in text for c in _SOLID_CUES) and "solid3d" not in kinds:
            errors.append(f"{fid}：内容涉及立体几何，但 figure 未声明 `solid3d`")
        if any(c in text for c in _PARAM_CUES) and "parametric" not in kinds:
            errors.append(f"{fid}：内容涉及参数曲线，但 figure 未声明 `parametric`")
        if any(c in text for c in _FORMULA_CUES) and "formula" not in kinds:
            errors.append(f"{fid}：内容涉及分式/根号/定义式，但 figure 未声明 `formula`"
                          f"（否则模型手拼分式，实测分子分母同 y 重叠）")
        # 该有图却完全没声明（figure=none）→ 模型会手画曲线（实测切点/割线飘 12~64px）
        if any(c in text for c in _GEOM_NONE_CUES) and not (kinds - {"none"}):
            errors.append(f"{fid}：内容涉及切线/割线/曲线，但 figure 未声明任何图形 kind"
                          f"（否则模型手画贝塞尔曲线，切点/割线端点会飘 12~64px）")
        if "function_plot" in kinds and any(c in text for c in _FUNC_GEOM_CUES):
            fp = [f for f in fig_list
                  if str(f.get("kind") or "").lower() == "function_plot"]
            if not any(f.get("point") is not None or f.get("points")
                       or f.get("sharp") is not None for f in fp):
                errors.append(f"{fid}：有函数图且涉及切线/切点，但 function_plot 未声明 "
                              f"point/points/sharp（切点会自画、飘离曲线）")
    return errors


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
STAGED_SYSTEM_PROMPT = """你是 ppt-master 的**阶段化执行体**。

与自由 ReAct 不同：**流程由外部驱动器控制，不由你决定。** 驱动器会在每个阶段明确告诉你
当前阶段、要做什么、输出什么格式。你只需在该阶段产出该阶段的内容，不要自行跳到下一步，
不要输出本阶段未被要求的产物。

全局约定：
- SKILL_DIR = {skill_dir}
- 输出根目录 = {output_root}
- 本机 Python 命令 = `{python_cmd}`（文档里写 python3 的地方一律用它替换）
- skill 文档里的 `${{SKILL_DIR}}` 已经展开为上面的真实路径，你直接用绝对路径。
- 命令有全局执行权限，直接运行，不需要询问。
"""

PLAN_PROMPT = """# 阶段 1：规划（Plan）

你已收到 skill 的权威文档（见上文）。现在完成规划，产出**一份结构化计划**。

要求：
1. 按 generate-pptx.md 的 Step 1–4 与 plan-core.md 的方法论做判断：
   沟通契约（受众/意图/成果/核心信息）、页清单 roster、画布、配色、字体层级、视觉风格。
2. roster **必须**是完整的有序页清单，每页给：id、输出文件名、页面角色、标题、该页核心信息、
   以及这一页要承载的实质内容要点（3~6 条，要具体，不要占位符）。

2b. **每页必须给一个 `figure` 字段，声明这一页要画的函数图像。** 这是把坐标算术交给
   代码的关键：你只声明“画什么”，代码会把曲线、切点、切线、割线的**全部 SVG 坐标**算好给你，
   你不需要（也不应该）自己算。取值：
     · 不需要图形 → `"figure": {{"kind": "none"}}`
     · 函数图像（曲线/切点/切线/割线/尖点）→
       `"figure": {{"kind": "function_plot", "expr": "0.35*x**2 + 0.5",`
       `"domain": [-3, 3], "area": [560, 140, 1180, 620],`
       `"point": 2.0, "secants": [0.0, 1.0, 1.5], "samples": 65}}`
       字段含义：
         · `expr`   — 函数表达式（Python 写法；`x^2`、`x²`、`2x`、`√x`、`sin(x)`、`abs(x)` 都能识别）
         · `domain` — 数学定义域 `[xmin, xmax]`
         · `area`   — SVG 绘图区 `[x左, y上, x右, y下]`（画布内、给图形腾出的矩形）。
             **必须落在你给图安排的面板内**：左文右图时图在右半，area 的 x 就取右半面板（如 `[640, ...]`），
             不要填成全页宽（否则曲线会画进左侧文字区）
         · `point`  — 单个切点的 x（没有切点就省掉）
         · `points` — 多个切点的 x 列表，一页要画多条切线时用（如「正负与单调」「陡峭」）；与 `point` 二选一
         · `sharp`  — 尖点的 x（`abs(x)` 这类不可导点，只画左右割线、不画切线）
         · `secants`— 割线另一端点 Q 的 x 列表（都连到 `point`/`points` 的第一个点，或 `sharp` 尖点；没有就省掉）
         · `samples`— 采样点数，默认 65，一般不用写
       **只能声明一个函数**。要画多条曲线就分页。
     · **圆锥曲线**（椭圆/双曲线/抛物线——它们**不是函数**，必须用它，禁止手画）→
       `"figure": {{"kind": "conic", "type": "ellipse", "a": 3, "b": 2,`
       `"center": [0, 0], "view": [-5, 5, -4, 4], "area": [660, 140, 1230, 620],`
       `"points": [{{"name": "A", "t": 30}}, {{"name": "B", "t": 150}}],`
       `"free_points": [{{"name": "P", "at": [2, 3]}}],`
       `"chords": [["A", "B"]], "tangents": ["A"],`
       `"lines": [{{"name": "l1", "through": ["A", "P"]}}],`
       `"intersections": [{{"name": "Q", "of": ["l1", "l2"]}}],`
       `"ext_tangents": [{{"point": "P", "names": ["t1", "t2"], "touch": ["T1", "T2"]}}]}}`
       字段：`type`（circle 圆 / ellipse 椭圆 / hyperbola 双曲线 / parabola 抛物线，
       圆用 `a` 填半径）、
       `a`/`b`（椭圆半长轴/半短轴，或双曲线实半轴/虚半轴，数学单位）、
       `p`（抛物线焦准距，右开口 y²=2p(x−h)）、`center`（中心，数学坐标）、
       `view`（数学视窗 `[xmin,xmax,ymin,ymax]`）、`area`（SVG 绘图区，同上）；
       `points`（曲线上的点：椭圆 `t` 是角度°、0=右端逆时针，双曲线/抛物线 `t` 是参数，
       双曲线可加 `"branch": -1` 取左支）、`free_points`（自由点，数学坐标 `"at": [x,y]`）、
       `chords`（弦，引用点名）、`tangents`（点名列表，点处切线）、
       `lines`（线段，`"through": [点名A, 点名B]`，加 `"arrow": true` 则末端带箭头=向量）、
       `intersections`（两线交点，`"of": [线1, 线2]`）、
       `ext_tangents`（过外点的切线，`"point"` 引用点、`"names"` 切线名、`"touch"` 切点名）。
       代码返回曲线 path + 焦点 + 顶点 + **点/弦/切线/线段/交点/过外点切线**（端点都精确算好），
       坐标**照抄**（等比例，椭圆不变形）。
     · **立体几何（3D）**（棱柱/棱锥/正方体等——**必须用它，禁止手画**）→
       预设（最省事）：`"figure": {{"kind": "solid3d", "preset": "prism", "n": 6,`
       `"radius": 2, "height": 3, "azimuth": 30, "elevation": 20,`
       `"projection": "perspective", "distance": 8, "scale": 80, "origin": [640, 400]}}`
       `preset` ∈ cube（正方体）/ tetrahedron（正四面体）/ octahedron（正八面体）/ prism（正 n 棱柱）/
       pyramid（正 n 棱锥），`n`=底面边数、`radius`/`height`/`size`=尺寸；也可自己给顶点：
       `"vertices": {{"A": [0,0,0], ...}}` + `"edges": [["A","B"], ...]`
       （棱也可写成 `{{"of": ["A","B"], "arrow": true}}` 表示**向量箭头**）+
       `"faces": [{{"of": ["A","B","C","D"]}}, ...]`（可选，代码按深度排序）。
       `projection` ∈ orthographic（正交，默认）/ perspective（透视，配 `distance`）；
       `azimuth`/`elevation`=相机方位角/仰角（度）、`scale`=px/单位、`origin`=投影原点。
       代码返回**所有顶点/棱/面的 2D 投影坐标**（面按深度排序、每条棱标了 hidden 用于虚线、向量标了 arrow），坐标**照抄**。
     · **参数曲线**（摆线/心形线等任意参数方程，函数与圆锥曲线都装不下时用）→
       `"figure": {{"kind": "parametric", "x": "t - sin(t)", "y": "1 - cos(t)",`
       `"t": [0, 12.566], "view": [-1, 14, -1, 3], "area": [660, 140, 1230, 620]}}`
       `x`/`y`=关于 `t` 的表达式、`t`=参数范围 `[tmin,tmax]`、`view`=数学视窗、`area`=SVG 绘图区。
       代码返回曲线 path（等比例），坐标**照抄**。
     · **旋转体**（圆柱/圆锥/圆台/球等）→
       `"figure": {{"kind": "revolution", "profile": [[0,0],[2,0],[2,3],[0,3]],`
       `"ring_count": 3, "azimuth": 30, "elevation": 20, "scale": 60, "origin": [640, 400]}}`
       `profile`=母线 `[[半径, 高度], ...]`（在轴的一侧）、`meridians`=要画的母线方位角（默认 4 条）、
       `ring_count`=纬线圆数量。代码返回母线 + 纬线圆的 2D path（纬线已投影成椭圆），坐标**照抄**。
     · **数学公式（分式/根号/极限式）**（**禁止手拼**——实测分子分母同 y 重叠）→
       `"figure": {{"kind": "formula", "template": "limit_fraction",`
       `"lhs": "f′(x₀) =", "lim_sub": "Δx→0",`
       `"numerator": "f(x₀+Δx) − f(x₀)", "denominator": "Δx",`
       `"x": 96, "y": 278, "font_size": 24}}`
       `template` ∈ limit_fraction（左端+lim+下标+分式）/ fraction（左端+分式）/ sqrt（根号，
       配 `radicand` 被开方数、`index` 根指数）；`x`/`y`=公式左侧基线起点。
       代码返回**分子/分母/分数线/lim/下标/根号**的全部坐标，**照抄**。

   **数学符号纪律**（写页面文字时）：
   · 上下标优先用 **Unicode**（x₀、x²、f′、Δ、→、≥、∫、Σ、√、π、θ…），不要用 `^` `_`；
   · 需要真正的**分式 / 根号 / 极限式** → 必须用 `formula`（禁止用多个 `<text>` 拼）；
   · 希腊字母/算子一律用 Unicode 字符，**禁止 LaTeX**（`\frac` `\times` 等不得出现）。

   **硬性红线**：
   · 出现「切点/切线/割线/尖点」→ 必须用 `function_plot` 声明 `point`/`points`/`sharp`；
   · 出现「圆/椭圆/双曲线/抛物线」→ 必须用 `conic` 声明（**禁止手画**——实测手画会变成斜直线或任意贝塞尔，完全不像）；
   · 出现「正方体/棱柱/棱锥/立体」等 3D → 必须用 `solid3d` 声明；
   · 出现「摆线/心形线」等参数方程 → 必须用 `parametric` 声明；
   · 出现「旋转体/圆柱/圆锥/圆台」→ 必须用 `revolution` 声明；
   · 出现「定义式/分式/根号/极限式」→ 必须用 `formula` 声明；
   · **绝对禁止自己心算坐标、自己手画**。
3. 若用户的 PPT 任务里已经给了逐页大纲（每页标了「第N页」），roster **必须沿用它的
   页数与页序**，不要自行增删或重排；否则页数由内容决定，不要为了凑数拆页，
   也不要为了省事合并。
4. 配色给 16 进制；字体给具体字号；视觉风格给一个 skill 里存在的风格 id。
   **配色纪律（必须遵守，实测违反会让整册显乱）**：
   · 以**浅色系**为主：背景/表面用白或极浅灰（如 `#FFFFFF` / `#F4F6F9` / `#EEF2F8`）；
     深色只作**文字**和**细线**，**禁止大面积深色块**（绘图区背景、卡片底、色带都不许用深色打底）；
   · **只用 1 个强调色**（accent），不要同时用第二个高饱和色（橙+蓝混用最显乱）；
     需要区分层次时用同一色相的深浅或透明度，不要换色相；
   · accent 只上**小面积**：重点词、关键线、编号圆点、细强调条；不要拿它铺大色块。
5. 语言跟随用户任务的语言。

**只输出下面这一个块，不要有任何其他文字：**

<plan>
{{
  "project_name": "英文小写下划线，用作目录名",
  "canvas": "ppt169",
  "communication": {{
    "audience": "受众",
    "intent": "沟通意图",
    "outcome": "期望成果",
    "core_message": "一句话核心信息"
  }},
  "roster": [
    {{
      "id": "P01",
      "file": "01_cover.svg",
      "role": "cover",
      "title": "页面标题",
      "core_message": "该页核心信息",
      "content_points": ["要点1", "要点2", "要点3"],
      "figure": {{"kind": "none"}}
    }}
  ],
  "colors": {{
    "background": "#FFFFFF",
    "surface": "#F4F6F9",
    "text_primary": "#1F2430",
    "text_secondary": "#6B7280",
    "accent": "#2F6FE0"
  }},
  "typography": {{
    "family": "Microsoft YaHei",
    "display": 72,
    "title": 48,
    "subtitle": 32,
    "body": 24,
    "annotation": 18
  }},
  "visual_style": "swiss-minimal",
  "notes_enabled": false,
  "motion": "none"
}}
</plan>

用户的 PPT 任务：
{task}
"""

SPEC_FILL_PROMPT = """# 阶段 2：冻结设计（Freeze）

代码已经在 {project_path} 创建了项目，并生成了两个官方脚手架文件：
- design_spec.md（design-spec/v1）
- spec_lock.md（spec-lock/v1）

它们现在是模板骨架。请把占位内容换成真实设计内容。

## 硬性要求（违反会直接导致 project_manager validate 失败）

1. **第一行的 schema 标记必须原样保留**：
   `<!-- ppt-master-schema: design-spec/v1 -->` / `<!-- ppt-master-schema: spec-lock/v1 -->`
   丢了它会退化成 legacy 校验，很多规则就不再生效。
2. **所有章节标题必须原样保留**，一个都不能删、不能改名、不能合并、不能重排。
   编号（I. / II. / ## canvas …）和英文原名都按脚手架原样写。
   **章节里的行也一行都不能删**（包括你判断「这一册用不到」的字号角色、颜色角色、
   图标库字段）：你拿到的是一张已经列好全部字段的表，你的工作是把值填进去，
   不是决定哪些字段该存在。删行的代价很具体：脚手架给了 6 个字号角色，
   删到只剩下 2 个，随后正文里用到的字号就成了「未声明字号」，
   校验器会逐页报 blocking 错，整份 deck 都要返工。
   用不到的字段把值改小、改保守，但**不要删行**。
3. 以下章节是校验器硬性要求的，必须存在且非空：
   - design_spec.md → `## IX. Content Outline`
   - spec_lock.md  → `## communication`
4. **§IX 是执行阶段唯一权威的有序页清单**，必须逐页写全，粒度要能让 Executor 直接开工：
  每页给 id（P01…）、页面角色、标题、以及该页要承载的具体内容要点。
5. spec_lock.md 里的颜色/字体/画布等锚点必须用**具体值**（16 进制颜色、具体字号、具体 viewBox），
   不能留占位符——执行阶段只读 lock，不会再去推断。
   **`## typography` 尤其不能只填脚手架发下来的 body / title 两行**：计划里给了几个
   字号角色（如 display / subtitle / annotation），锁里就得有几个。执行器按
   `annotation` 等角色写页码、注释、图文说明，锁里没这个角色就只能写死一个
   未声明字号，逐页校验会判成 `undeclared font-size ... exceeds the sparse-display limit`，
   整份 deck 每页都要返工。
6. 只改字段的值。字段名、表格列头、箭头缩进都保持脚手架原样。
   自检：将填充后的文件与脚手架逐行对照，除了冒号后面的值，其余应逐字一致。

已确认的计划（权威，不得改动其中的决策）：
<plan>
{plan_json}
</plan>

## 权威 schema（字段名与枚举值以此为准，逐个对照）

{schemas}

两个脚手架的当前内容：
<scaffold name="design_spec.md">
{design_spec}
</scaffold>

<scaffold name="spec_lock.md">
{spec_lock}
</scaffold>

**只输出下面这两个块，不要有任何其他文字：**

<file name="design_spec.md">
（保留 schema 标记与全部章节标题，填好值的完整文件内容）
</file>

<file name="spec_lock.md">
（保留 schema 标记与全部章节标题，填好值的完整文件内容）
</file>
"""

SPEC_REPAIR_PROMPT = """# 阶段 2b：修正设计文件（Act）

`project_manager validate` 拒绝了上面你写的文件。**只修列出的问题**，
不要重写无关内容。

<validate_errors>
{errors}
</validate_errors>

## 权威 schema（逐个字段对照修正）

{schemas}

你上一版写的两个文件：
<file name="design_spec.md">
{design_spec}
</file>

<file name="spec_lock.md">
{spec_lock}
</file>

修正要点（对照报错检查）：
- schema 标记是否还在第一行？
- 报错点名的章节是否存在且非空？（尤其 `## IX. Content Outline` 与 `## communication`）
- 章节标题是否被改动或删除？
- §IX 里每个 `#### Slide NN` 标题都会被当成正式页校验，必须带 `Audience move`
  与 `Relationships` 两行，且编号不得重复。若报错提示某页缺这两行，通常是
  模型新增了多余的 `#### Slide NN` 标题（如“版面适配修正”记录）；删掉这些
  多余块、把改动并回原块即可，不要给多余块补写这两行。

**只输出这两个块：**

<file name="design_spec.md">
（修正后的完整文件）
</file>

<file name="spec_lock.md">
（修正后的完整文件）
</file>
"""

PAGE_PROMPT = """# 阶段 3：逐页创作（Execute）—— 第 {index}/{total} 页

按 spec_lock.md 的锚点与 executor-base.md 的规则，创作下面这一页。

页面规格：
- 文件名：{file}
- 页面角色：{role}
- 标题：{title}
- 核心信息：{core_message}
- 必须承载的要点：
{points}
{figure_geometry}
画布：viewBox="{viewbox}"（所有页必须完全一致）

已冻结的锚点（必须遵守）：
- 配色：{colors}
- 字体：{family}；display {display}px / title {title_size}px / subtitle {subtitle}px / body {body}px / annotation {annotation}px
- 视觉风格：{visual_style}

代码已算好的单行字数上限（**硬约束**：每条文字不得超过对应上限，否则超界返工）：
{char_budgets}

已交付页面的紧凑签名（守住跟前面的页面的视觉连贯性）：
{prev_signatures}

要求：
1. 输出**完整的、可直接写盘的单页 SVG**，根元素是 `<svg>`，带 `viewBox`、
   `xmlns`、`width`/`height`，以及该页的 `data-pptx-page-role`。
2. **内容简洁、宁短勿长**：把上面的要点用最短的句子表达完即可。每条文字都要
   明显短于它所在列的可用宽度（留 15%% 以上余量），**绝不**贴边或超界。
   宁可只放标题 + 3~4 条短 bullet，也不要写满写长——超界会被打回返工，
   返工比简洁更烧钱更慢。
3. 跟前几页保持同一套配色/字体/构图语言，但不要重复同一套版式。
4. 只用手写 SVG。**绝对禁止** `<use data-icon="...">` / `data-pptx-icon` 等图标引用，
   也禁止引用任何外部图片。所有图形（勾、叉、箭头、图标等）一律用
   `<circle>` / `<rect>` / `<path>` / `<line>` 等原生元素自己画——
   图标引用不会自动生成图标文件，会被校验门以 "icon not found" 打回返工。
5. 不要用 markdown 代码块包裹输出。

**只输出下面这两个块（`<signature>` 是必须的，用于跨页一致性）：**

<page file="{file}">
<svg ...>
...
</svg>
</page>

<signature>一行紧凑描述：页面角色 | 主构图 | 视觉中心 | 复用的图形语言 | 本页新增的视觉元素</signature>
"""

REPAIR_PROMPT = """# 阶段 4：定向修复（Act）

校验器对你的页面报了错。**只修复下面列出的问题**，不要重写整页，不要改动未被报错的页面。

<errors file="{file}">
{errors}
</errors>

{arithmetic}

修复要求：
1. 逐条修掉上面的错误。每条都要在 <thought> 里说明你改了什么、依据哪条规则。
2. 保持这一页原有的内容、信息层次和视觉方向不变——这是修复，不是重设计。
3. 输出**完整的、修复后的单页 SVG**（不是 diff），根元素仍是 `<svg>`。
4. 若错误是 "icon not found"：把 `<use data-icon="...">` 直接替换成原生
   `<path>` / `<circle>` 等手画图形（保留原位置、大小与颜色），**不要**
   尝试生成图标文件。

**只输出下面这一个块：**

<page file="{file}">
<svg ...>
...
</svg>
</page>

⚠️ **一条内容都不许丢。** 你输出的是整页，但必须与输入逐条对应：
输入的**每一个** `<text>` 都要在输出里找到。为了避让超界可以**改写措辞**
（把长句改短），但**不允许整条删除**。
实测过反面案例：为了消掉一个超界错，把“三步列表”面板压成只有序号的 78px 窄条，
文字全部丢失——校验器不查内容，报的是“错误 0”，但人一眼就能看出不对。
输出前逐条核对：输入的每一条文字，输出里是否都在？
"""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class StagedRunner:
    """按 P0→P5 阶段驱动 ppt-master，代码决定流程，模型只产出内容。"""

    def __init__(
        self,
        agent: PPTAgent,
        parallel_pages: int = 1,
        plan_model: Optional[str] = None,
        execute_model: Optional[str] = None,
    ) -> None:
        self.agent = agent
        self.tools = agent.tools
        self.skill_dir = agent.skill_dir
        self.python_cmd = agent.python_cmd
        #: P3 逐页生成的并发度；>1 且页数 >1 时并行，否则走串行
        self.parallel_pages = max(1, int(parallel_pages))
        #: 阶段化模型分级：规划/填值用快模型，执行/修复用强模型；None = 沿用 --model
        self.plan_model = plan_model
        self.execute_model = execute_model
        self.messages: List[Dict[str, str]] = []
        self.plan: Dict[str, Any] = {}
        self.project_path: Optional[Path] = None
        #: P0 预备的各阶段文档文本，按阶段注入、用完即撤
        self._blocks: Dict[str, str] = {}
        self.stage_times: List[Tuple[str, float, int]] = []  # (阶段, 秒, 模型调用数)
        #: 任务原文与从它取出的逐页讲稿（P5 写 notes/total.md 用）
        self.task: str = ""
        self.narrations: List[str] = []
        #: 是否把讲稿落进 PPTX 备注。**由代码定，不听模型的**：
        #: 计划 JSON 里那个 notes_enabled 字段历史上从不被读取，写什么都白写。
        self.notes_enabled: bool = False
        #: 分阶段推理强度（None = 沿用 agent.py 全局设置）；见 STAGE_REASONING_ENV 注释
        self.plan_reasoning = os.getenv(STAGE_REASONING_ENV["plan"]) or None
        self.exec_reasoning = os.getenv(STAGE_REASONING_ENV["exec"]) or None
        #: P4 专用。未设则继承 exec_reasoning（保持旧行为），实现“P3 快、P4 会判断”。
        self.repair_reasoning = os.getenv(STAGE_REASONING_ENV["repair"]) or None
        #: P4 逐页修复轮数上限（0 = 不修，直接交给导出那道门判定）
        raw_repairs = os.getenv(MAX_PAGE_REPAIRS_ENV)
        try:
            self.max_page_repairs = max(0, int(raw_repairs)) if raw_repairs else MAX_PAGE_REPAIRS
        except ValueError:
            self.max_page_repairs = MAX_PAGE_REPAIRS

    # ------------------------------------------------------------ 基础设施

    def _log(self, text: str) -> None:
        print(text, flush=True)

    def _stage(self, name: str) -> None:
        self._log(f"\n{'━' * 64}\n▌ {name}\n{'━' * 64}")

    # ------------------------------------------------------------ 阶段化上下文

    def _system_prompt(self) -> str:
        return STAGED_SYSTEM_PROMPT.format(
            skill_dir=self.skill_dir,
            output_root=self.agent.output_root,
            python_cmd=self.python_cmd,
        )

    def _doc_text(self, relative_paths: List[str], per_doc_limit: int = 90_000) -> str:
        """读入若干 skill 文档，展开 ${SKILL_DIR}。"""
        parts: List[str] = []
        for rel in relative_paths:
            file = self.skill_dir / rel
            if not file.is_file():
                parts.append(f"（缺失：{rel}）")
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            text = text.replace("${SKILL_DIR}", str(self.skill_dir))
            text = text.replace("$SKILL_DIR", str(self.skill_dir))
            parts.append(
                f"════════ {rel} ════════\n"
                f"{clip(text, per_doc_limit, rel)}\n"
                f"════════ {rel} 结束 ════════"
            )
        return "\n\n".join(parts)

    def _context(self, *blocks: Tuple[str, str]) -> None:
        """**重建**消息链：system + 本阶段真正需要的内容块。

        这是阶段化的第二个省时点。ReAct 与早期 staged 都把文档永久追加进历史，
        于是每次调用都要重复预填充全部 263KB——而规划文档在 P1 之后就再也没用过。
        这里每个阶段重建一次，用完即撤：

            P1  规划权威 + 能力批次                    ≈ 146 KB
            P2  计划 + schema + 脚手架                 ≈  22 KB
            P3  spec_lock + 页序 + 执行核心             ≈ 106 KB
            P4  spec_lock + 执行核心 + 待修页面         ≈ 105 KB

        注意：重建会让**前缀缓存失效**（前缀变了），但总量下降带来的收益远大于此。
        """
        self.messages = [{"role": "system", "content": self._system_prompt()}]
        for title, body in blocks:
            self.messages.append({
                "role": "user",
                "content": f"{title}\n\n{body}\n\n（以上内容已读入，无需再调用工具读取）",
            })
        chars = sum(len(m["content"]) for m in self.messages)
        print(f"   🔖 本阶段上下文重建：{len(blocks)} 个内容块 / {chars:,} 字符", flush=True)

    def _append(self, title: str, body: str) -> None:
        """向当前阶段上下文追加一个块（不重建）。"""
        self.messages.append({"role": "user", "content": f"{title}\n\n{body}"})

    def _ask(
        self,
        prompt: str,
        label: str = "",
        reasoning_effort: Optional[str] = None,
        thinking: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """一次模型调用，返回原始输出（已追加到消息链）。

        reasoning_effort / thinking / model 允许**单次覆盖**（None = 沿用全局默认）。
        用于按阶段切换推理强度与模型：P4 修复关思考，规划/执行分级换模型。
        """
        self.messages.append({"role": "user", "content": prompt})
        print(f"\n🤖 模型调用{'：' + label if label else ''}...", flush=True)
        raw = self.agent.call_model(
            self.messages,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
            model=model,
        )
        if self.agent.last_prompt_tokens:
            print(
                f"   📊 本次预填充 {self.agent.last_prompt_tokens:,} tokens"
                f"   ⏱️ {self.agent.last_model_seconds:.1f}s",
                flush=True,
            )
        return raw

    def _run_checked(self, command: str, label: str, fatal: bool = True) -> Tuple[bool, str]:
        """跑一条命令并检查退出码。返回 (成功, 输出)。"""
        code, body = self.tools.run_command_checked(command)
        observation = clip(
            f"exit_code={code}\n{body}",
            self.tools.max_observation_chars,
            "命令输出",
        )
        ok = code == 0
        self._log(f"   {'✅' if ok else '❌'} {label}")
        if not ok:
            tail = clip(observation, 1500, "命令输出")
            self._log(f"      {tail.splitlines()[-1][:160] if tail.splitlines() else ''}")
            if fatal:
                raise RuntimeError(f"{label} 失败：\n{observation[:3000]}")
            return False, observation
        return True, observation

    def _schema_text(self) -> str:
        """读入两份权威 schema，供 P2 注入。"""
        chunks: List[str] = []
        for rel in SPEC_SCHEMA_DOCS:
            file = self.skill_dir / rel
            if file.is_file():
                chunks.append(f"--- {Path(rel).name} ---\n{file.read_text(encoding='utf-8', errors='replace')}")
        return "\n\n".join(chunks) or "（schema 文件缺失）"

    def _extract_pages(self, raw: str, fallback_file: str = "") -> List[Tuple[str, str]]:
        """从模型输出里抽出页面块。

        容错：引号单双都收；`<page>` 不带 file 属性时用 fallback_file；
        没有 <page> 包裹但含 <svg> 时，把整段当单页（修复场景常见）。
        """
        out: List[Tuple[str, str]] = []
        pattern = re.compile(
            r'<page\b[^>]*?file\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</page>', re.DOTALL
        )
        for match in pattern.finditer(raw):
            name, body = match.group(1).strip(), self._clean_svg(match.group(2))
            if body:
                out.append((name, body))
        if out:
            return out

        for match in re.finditer(r"<page\b[^>]*>(.*?)</page>", raw, re.DOTALL):
            body = self._clean_svg(match.group(1))
            if body and fallback_file:
                return [(fallback_file, body)]

        if fallback_file:
            body = self._clean_svg(raw)
            if body:
                return [(fallback_file, body)]
        return []

    @staticmethod
    def _clean_svg(text: str) -> str:
        """去掉代码围栏 / 包裹文字，取出真正的 <svg>…</svg>。

        `</svg>` 缺失时（截断）返回空，宁可不写也不能写半截页面。
        """
        body = re.sub(r"^```[a-zA-Z]*\s*", "", (text or "").strip())
        body = re.sub(r"\s*```$", "", body).strip()
        start = body.find("<svg")
        if start < 0:
            return ""
        end = body.rfind("</svg>")
        if end > start:
            return body[start: end + 6]
        self_close = re.match(r"<svg\b[^>]*?/>", body[start:])
        if self_close:
            return body[start: start + self_close.end()]
        return ""

    @staticmethod
    def _clean_text(text: str) -> str:
        """去掉整段外层代码围栏。"""
        body = (text or "").strip()
        body = re.sub(r"^```[a-zA-Z]*\s*\n", "", body)
        body = re.sub(r"\n?```\s*$", "", body)
        return body.strip()

    def _extract_files(self, raw: str) -> Dict[str, str]:
        """从模型输出里抽出文件块。

        容错：单引号 / 双引号 / 无引号、属性顺序颠倒、大小写都收。
        早期版本只认 `name="..."`，模型一换写法就整个运行挂掉。
        """
        files: Dict[str, str] = {}
        pattern = re.compile(
            r'<file\b[^>]*?name\s*=\s*["\']?([^"\'>\s]+)["\']?[^>]*>(.*?)</file>',
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(raw):
            files[match.group(1).strip()] = self._clean_text(match.group(2))

        # 退路：按 schema 标记识别。skill 的 validate 强制要求两个文件
        # 首行就是标记，所以这比标签写法可靠得多——模型换了标签格式也能认出来。
        for marker, name in SCHEMA_MARKERS:
            if name in files:
                continue
            found = re.search(
                rf"<!--\s*ppt-master-schema:\s*{re.escape(marker)}\s*-->", raw
            )
            if not found:
                continue
            body = raw[found.start():]
            nxt = re.search(r"<!--\s*ppt-master-schema:\s*[\w-]+/v\d+\s*-->", body[1:])
            if nxt:
                body = body[: nxt.start() + 1]
            body = re.sub(r"\s*</?file\b[^>]*>\s*$", "", body.strip())
            body = self._clean_text(body)
            if body:
                files[name] = body
        return files

    def _save_parse_failure(self, label: str, raw: str) -> Optional[Path]:
        """解析失败时把模型原始输出留盘，便于事后定位而不是靠猜。"""
        if not self.project_path:
            return None
        target = self.project_path / "validation" / f"_parse_failure_{label}.txt"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(raw, encoding="utf-8")
            return target
        except OSError:
            return None

    def _ask_files(
        self, prompt: str, label: str, expected: List[str], model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Dict[str, str]:
        """要模型输出若干文件块；解析不到就存盘 + 重试一次。

        P2 失败会让整个运行停在原地，而“模型没按格式输出”是最容易发生的一类。
        所以不硬失败：先重试一次，并把原始输出存下来供排查。
        """
        raw = self._ask(prompt, label, model=model, reasoning_effort=reasoning_effort)
        files = self._extract_files(raw)
        missing = [name for name in expected if name not in files]
        if not missing:
            return files

        saved = self._save_parse_failure(label, raw)
        self._log(
            f"   ⚠️ 未解析到 {missing}；原始输出已存到 "
            f"{saved.relative_to(self.skill_dir) if saved else '（存盘失败）'}"
        )
        retry_prompt = (
            "你上一条输出里没有可解析的文件块。解析失败的原始输出已保存供排查。\n"
            f"请**只**输出下面这个格式，每个文件一个块，标签必须完整闭合：\n\n"
            + "\n\n".join(
                f'<file name="{n}">\n（{n} 的完整内容）\n</file>' for n in expected
            )
            + "\n\n不要用 markdown 代码块包裹，不要加任何解释文字。"
        )
        raw2 = self._ask(retry_prompt, f"{label}（重试）", model=model,
                         reasoning_effort=reasoning_effort)
        files2 = self._extract_files(raw2)
        if not [n for n in expected if n not in files2]:
            return files2

        self._save_parse_failure(f"{label}_retry", raw2)
        raise RuntimeError(
            f"连续两次都没解析出 {missing}。\n"
            f"  已保存原始输出：{saved}\n"
            f"  第二次输出开头：{(raw2 or '')[:300]}"
        )

    @staticmethod
    def _solve_bounds_errors(errors: List[str]) -> List[str]:
        """把“文字/区域超界”与“区域重叠”类报错直接解成具体修正量。

        这两类都是**纯几何算术**，而且报错文本自带全部输入。模型自己算不准——
        实测同一个错误连修 3 轮不过。所以由代码解出答案，把“把容器扩到 ≥X px”
        或“字号降到 ≤Y px”或“沿 Y 轴移动 Z px”直接告诉模型。

        关键：溢出可能是**位置上探出边界**（内容比容器小但起点在容器之前），
        不是尺寸差。只算 span 之差会得出负值而完全跑偏——这是早期版本的 bug。
        """
        hints: List[str] = []

        for message in errors:
            overlap = _OVERLAP_RE.search(message)
            if overlap:
                g = overlap.groupdict()
                ow, oh = float(g["w"]), float(g["h"])
                # 用面积判定是否属于“布局级”：496×92=45,632（大）vs 8×56=448（小）
                big = ow * oh >= 4_000
                hints.append(
                    f"  · 区域重叠：<g id=\"{g['a']}\"> 与 <g id=\"{g['b']}\"> 相交 "
                    f"{ow:.0f} px × {oh:.0f} px（容差 1 px）\n"
                    f"    ✅ 做法（任一）：把其中一个区域的 data-pptx-bounds 在水平方向"
                    f"收缩/位移 ≥ {ow + 4:.0f} px，或在垂直方向 ≥ {oh + 4:.0f} px；"
                    "或者把两个区域改成同一容器内的子区，用明确的上下/左右分栏把它们分开。"
                    + (
                        "\n    ⚠️ 重叠面积很大，这是**布局级**缺陷：仅调数值可能不够，"
                        "**允许你移动、缩放这些区域，甚至重新划分版面块**——"
                        "只要求文字内容与信息层次不变，不要求坐标不变。"
                        if big else ""
                    )
                )
                continue

            found = _BOUNDS_RE.search(message)
            if not found:
                continue
            g = found.groupdict()
            horizontal = g["axis"] == "horizontal"
            owner = g.get("owner_id") or "画布 root viewBox"
            kind = "画布" if g["container_kind"] == "canvas" else "容器"
            if horizontal:
                c1, c2 = float(g["cx1"]), float(g["cx2"])
                k1, k2 = float(g["kx1"]), float(g["kx2"])
                axis_cn, pos_attr = "水平", "x"
            else:
                c1, c2 = float(g["cy1"]), float(g["cy2"])
                k1, k2 = float(g["ky1"]), float(g["ky2"])
                axis_cn, pos_attr = "垂直", "y"

            before = k1 - c1   # > 0：内容起点在容器起点之前（上/左探出）
            after = c2 - k2    # > 0：内容终点在容器终点之后（下/右探出）
            content_span, container_span = c2 - c1, k2 - k1

            lines = [
                f"  · {axis_cn}超界（{owner}，{kind}）："
                f"内容区间 {c1:.1f}–{c2:.1f}（长 {content_span:.1f}），"
                f"{kind}区间 {k1:.1f}–{k2:.1f}（长 {container_span:.1f}）"
            ]
            if before > 0:
                lines.append(
                    f"    起点在{kind}之前 {before:.1f} px"
                    + (
                        "" if g["container_kind"] == "canvas"
                        else f" → 或把 <g id=\"{g['owner_id']}\"> 的起点从 {k1:.0f} 改到 ≤ {c1:.0f}"
                    )
                )
            if after > 0:
                lines.append(
                    f"    终点超出{kind} {after:.1f} px"
                    + (
                        "" if g["container_kind"] == "canvas"
                        else f" → 或把 <g id=\"{g['owner_id']}\"> 的终点从 {k2:.0f} 扩到 ≥ {c2 + 8:.0f}"
                    )
                )

            rate = _RATE_RE.search(message)
            if rate:
                per_char, font_size = float(rate.group("rate")), float(rate.group("size"))
                chars = content_span / per_char if per_char else 0
                lines.append(
                    f"    算术：{content_span:.1f} px ÷ {per_char:.1f} px/字符 ≈ {chars:.1f} 字符；当前字号 {font_size:.0f}px"
                )
            if content_span > container_span > 0 and rate:
                shrink = int(font_size * container_span / content_span)
                lines.append(
                    f"    ✅ 不改布局时：字号降到 ≤ {shrink} px"
                    f"（= {font_size:.0f} × {container_span:.1f} ÷ {content_span:.1f}，向下取整）"
                )
            # 文字本身太长（checker 直接在报错里给了「≈N chars fit in W px」）：
            # 这类错扩框/挪位都可能救不了（容器被邻居挡死），正确解是缩短或换行。
            fit = _FIT_CHARS_RE.search(message)
            if fit and rate and chars > float(fit.group("chars")):
                lines.append(
                    f"    ⚠️ 文字太长：约 {chars:.0f} 字 > 容器只放得下 {fit.group('chars')} 字。"
                    f"若扩框/挪位都不可行，**必须把文字缩短到 ≤ {fit.group('chars')} 字**，"
                    "或拆成两行（拆前确认下方有空间、不压到别的文字）。"
                )
            lines.append(
                f"    ✅ 推荐：沿{axis_cn}方向调整该 <text> 的 {pos_attr} 与容器 bounds，"
                "使内容区间完全落在容器区间内（两边各留 ≥4 px 余量），改完按上面的 px/字符 重算确认。"
            )
            hints.append("\n".join(lines))
        return hints

    def _extract_json(self, raw: str, tag: str = "plan") -> Dict[str, Any]:
        """从模型输出里抽 JSON。

        模型常常在字符串值里写**未转义的引号**，例如：
            "core_message": "它不替代传统数据库，而是补上"相似检索"这块拼图"
        直接 json.loads 会报 “Expecting ',' delimiter”。这里先尝试直接解析，
        失败则用状态机把字符串内部的裸引号转义后重试。
        """
        match = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.DOTALL)
        text = match.group(1) if match else raw
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text).strip()
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start: end + 1]

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        repaired = self._escape_inner_quotes(text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # 去尾逗号后再试一次
            return json.loads(re.sub(r",\s*([}\]])", r"\1", repaired))

    @staticmethod
    def _escape_inner_quotes(text: str) -> str:
        """把字符串值内部的裸引号转义。

        判定：遇到 `"` 时向前看第一个非空白字符；若它不是一个合法的 JSON
        结构符（, } ] :），那这个引号就不是闭合符，而是内容的一部分。
        """
        out: List[str] = []
        in_string = False
        index = 0
        while index < len(text):
            char = text[index]
            if not in_string:
                if char == '"':
                    in_string = True
                out.append(char)
            elif char == "\\":
                out.append(char)
                if index + 1 < len(text):
                    out.append(text[index + 1])
                    index += 1
            elif char == '"':
                look = index + 1
                while look < len(text) and text[look] in " \t\r\n":
                    look += 1
                if look >= len(text) or text[look] in ",}]:":
                    in_string = False
                    out.append(char)
                else:
                    out.append('\\"')
            else:
                out.append(char)
            index += 1
        return "".join(out)

    # ------------------------------------------------------------ P0

    def phase_prepare(self, task: str) -> None:
        """纯代码：完整性闸门 + 选路 + 预备各阶段的文档文本。

        本阶段**不调用模型**，上下文留到各阶段按需构建，避免“一次全注入、全程背着跑”。
        """
        self._stage("P0 PREPARE（纯代码）")

        # 完整性闸门由代码执行，不再让模型决定
        self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/attribution_guard.py"',
            "完整性闸门 attribution_guard.py",
        )

        # 各阶段真正需要的文本，先准备好（此时不进上下文）
        self._blocks: Dict[str, str] = {
            "routing": self._doc_text(["SKILL.md", "workflows/routing.md"]),
            "planning": self._doc_text(list(PLAN_DOCS)),
            "capability": self._doc_text(list(PLAN_CAPABILITY_DOCS)),
            "execute_core": self._doc_text(list(EXECUTE_CORE_DOCS)),
        }
        total = sum(len(v) for v in self._blocks.values()) + len(self._schema_text())
        self._log(f"   闸门通过；已预备文档 {total:,} 字符（分阶段注入，用完即撤）")
        self._log(
            "   规划阶段 ≈{:,} 字符 / 冻结阶段 ≈{:,} / 执行阶段 ≈{:,}".format(
                len(self._blocks["routing"]) + len(self._blocks["planning"]) + len(self._blocks["capability"]),
                len(self._schema_text()),
                len(self._blocks["execute_core"]),
            )
        )

    # ------------------------------------------------------------ P1

    def phase_plan(self, task: str) -> Dict[str, Any]:
        """模型 1 次：只做规划判断。"""
        self._stage("P1 PLAN（模型 ×1）")
        # 只注入规划需要的东西；执行核心与 schema 此刻不进来
        self._context(
            ("【路由权威】", self._blocks["routing"]),
            ("【规划权威文档】", self._blocks["planning"]),
            ("【规划能力批次】", self._blocks["capability"]),
        )
        raw = self._ask(PLAN_PROMPT.format(task=task), "规划", model=self.plan_model,
                        reasoning_effort=self.plan_reasoning)
        try:
            self.plan = self._extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._log(f"   ⚠️ 计划解析失败：{exc}\n   原始输出尾部：{raw[-500:]}")
            raise RuntimeError("规划阶段的输出无法解析为 JSON") from exc

        roster = self.plan.get("roster") or []
        if not roster:
            raise RuntimeError("计划里没有 roster，无法进入执行阶段")

        # figure 完备性校验：该用代码能力（圆锥曲线/立体/参数曲线/切点）的页漏声明 → 打回重填一次
        fig_errs = figure_completeness_errors(roster)
        if fig_errs:
            self._log(f"   ⚠️ figure 声明不完备（{len(fig_errs)} 条），打回重填一次：")
            for err in fig_errs:
                self._log(f"      · {err}")
            retry_raw = self._ask(
                PLAN_PROMPT.format(task=task)
                + "\n\n【上一版计划的 figure 声明不完备，请修正后重新输出**完整**计划】\n"
                + "\n".join(f"- {e}" for e in fig_errs)
                + "\n\n只改 figure 声明，其它内容尽量保持不变。",
                "规划（figure 重填）", model=self.plan_model,
                reasoning_effort=self.plan_reasoning)
            try:
                retry_plan = self._extract_json(retry_raw)
            except (json.JSONDecodeError, ValueError):
                retry_plan = {}
            if retry_plan.get("roster"):
                rest = figure_completeness_errors(retry_plan["roster"])
                if len(rest) < len(fig_errs):
                    self.plan = retry_plan
                    roster = retry_plan["roster"]
                    self._log(f"   ✅ figure 重填后：{len(fig_errs)} → {len(rest)} 条缺陷")
                else:
                    self._log(f"   ↩️ figure 重填未改善（{len(fig_errs)} → {len(rest)}），保留原计划")
            else:
                self._log("   ↩️ figure 重填输出不可用，保留原计划")

        self._log(f"   沟通契约：{self.plan.get('communication', {}).get('core_message', '')[:70]}")
        self._log(f"   页清单（{len(roster)} 页）：")
        for page in roster:
            self._log(f"     {page.get('id')}  {page.get('file'):<18} {page.get('role'):<8} {page.get('title', '')[:40]}")

        # 讲稿备注：数目必须与 roster 对得上才敢按序贴。
        # 不对齐时宁可不要备注——旁白是按**页序**对应的，页数不一致会把
        # A 页的讲稿贴到 B 页上，那比没备注更坑。
        self.narrations = extract_narrations(self.task)
        got, want = len(self.narrations), len(roster)
        if got and got == want and all(self.narrations):
            self.notes_enabled = True
            self._log(f"   讲稿：任务里取到 {got} 页旁白，与 roster 一致 → 将写入 PPTX 备注")
        else:
            self.notes_enabled = False
            if not got:
                self._log("   讲稿：任务里没有「旁白：」，本次不写 PPTX 备注")
            else:
                self._log(f"   ⚠️ 任务里有 {got} 页旁白，但 roster 是 {want} 页"
                          f"（或存在空旁白）—— 无法按序对齐，本次不写备注")
        # 回写计划：P2 填 design_spec.md §I 时读的就是它。
        # 原来这里固定写 false，且代码从不读，两个后果叠在一起才让旁白彻底消失。
        self.plan["notes_enabled"] = self.notes_enabled
        return self.plan

    # ------------------------------------------------------------ P2

    def phase_freeze(self) -> None:
        """代码建项目 + 脚手架；模型 1 次填内容；代码 validate + 字体标定。"""
        self._stage("P2 FREEZE（代码 + 模型 ×1）")

        name = self.plan.get("project_name") or "ppt_project"
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower() or "ppt_project"
        out_root = self.agent.output_root
        out_root.mkdir(parents=True, exist_ok=True)

        # init 会自动追加 _<YYYYMMDD>，同一天重跑必然撞名。
        # 方案：不删除、不重命名旧目录（删会销毁上次产物；Windows 上重命名
        # 可能因目录被占用而 WinError 5），改为给项目名加时间戳，天然唯一。
        base = name
        if any(p.is_dir() for p in out_root.glob(f"{base}_*")):
            stamp = _time.strftime("%H%M%S")
            name = f"{base}_{stamp}"
            suffix = 1
            while any(p.is_dir() for p in out_root.glob(f"{name}_*")):
                suffix += 1
                name = f"{base}_{stamp}_{suffix}"
            self._log(f"   同名项目已存在，本次使用唯一名：{name}")

        canvas = self.plan.get("canvas") or ""
        fmt = f" --format {canvas}" if canvas in {"ppt169", "ppt43", "wechat", "xiaohongshu", "moments", "story", "banner", "a4"} else ""
        self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/project_manager.py" init {name}{fmt} '
            f'--dir "{out_root}"',
            f"project_manager init {name}{fmt}",
        )
        created = sorted(p for p in out_root.glob(f"{name}_*") if p.is_dir())
        if not created:
            raise RuntimeError(f"init 之后没有找到项目目录（{out_root}/{name}_*）")
        self.project_path = created[-1]
        self._log(f"   项目目录：{self.project_path}")

        # 脚手架
        for sub, label in (("scaffold-spec", "design_spec.md"), ("scaffold-lock", "spec_lock.md")):
            self._run_checked(
                f'{self.python_cmd} "{self.skill_dir}/scripts/project_manager.py" {sub} "{self.project_path}"',
                f"{sub} → {label}",
            )

        design_spec = (self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace")
        spec_lock = (self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace")

        # 只注入冻结阶段需要的东西：计划 + 权威 schema + 脚手架。
        # 121KB 的规划文档到此被移出上下文。
        def freeze_context() -> None:
            self._context(
                ("【已确认的计划】", json.dumps(self.plan, ensure_ascii=False, indent=2)),
                ("【权威 schema（字段/枚举以此为准）】", self._schema_text()),
                ("【design_spec.md 脚手架】", (self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace")),
                ("【spec_lock.md 脚手架】", (self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace")),
            )

        freeze_context()
        filled = self._ask_files(
            SPEC_FILL_PROMPT.format(
                project_path=self.project_path,
                plan_json=json.dumps(self.plan, ensure_ascii=False, indent=2),
                schemas=self._schema_text(),
                design_spec=design_spec,
                spec_lock=spec_lock,
            ),
            "fill_spec",
            ["design_spec.md", "spec_lock.md"],
            model=self.plan_model,
            reasoning_effort=self.plan_reasoning,
        )
        for target in ("design_spec.md", "spec_lock.md"):
            body = filled.get(target)
            if not body:
                raise RuntimeError(f"模型没有输出 {target} 的内容")
            (self.project_path / target).write_text(body, encoding="utf-8")
            self._log(f"   已写入 {target}（{len(body):,} 字符）")

        # 校验设计文件；失败就在**所属层**修（设计文件），而不是带病往下跑。
        #
        # 循环边界：SPEC_REPAIR_ROUNDS 次**修复**必须配 SPEC_REPAIR_ROUNDS + 1 次
        # **校验**。早先写成 range(1, ROUNDS + 1)，等于「验→修」各 3 次后直接 raise——
        # 第 3 次修复的成果**从未被验过就被丢弃**。实测一次真跑就是这么挂的：
        # 日志里第 3 轮已重写两个文件，手动重跑 validate 是 [OK]，但引擎已经抛错。
        # 计划声明的字号角色：锁里必须一个不少（见 lock_section_keys 的注释）
        plan_type_roles = sorted(
            k for k, v in (self.plan.get("typography") or {}).items()
            if isinstance(v, (int, float)))
        if plan_type_roles:
            self._log(f"   计划声明的字号角色：{'、'.join(plan_type_roles)}")
        ok = False
        for attempt in range(SPEC_REPAIR_ROUNDS + 1):
            ok, observation = self._run_checked(
                f'{self.python_cmd} "{self.skill_dir}/scripts/project_manager.py" validate "{self.project_path}"',
                f"project_manager validate（第 {attempt + 1} 轮）",
                fatal=False,
            )
            if ok:
                # validate 过了 ≠ 信息没丢：它不查「计划里的字号角色是否都进了锁」。
                # 缺角色的账要等到 P4 才结（执行器在正文里用出未声明字号，逐页返工）。
                in_lock = lock_section_keys(
                    (self.project_path / "spec_lock.md").read_text(
                        encoding="utf-8", errors="replace"), "typography")
                missing = [r for r in plan_type_roles if r not in in_lock]
                if not missing:
                    break
                ok = False
                observation = (
                    "[ERROR] spec_lock.md 的 ## typography 缺了计划声明的字号角色：\n"
                    + "\n".join(f"  - {m}: {self.plan['typography'][m]}" for m in missing)
                    + "\n执行器只读锁，正文里的每个字号必须能在锁里找到，否则会被\n"
                      "判为「未声明字号」并在逐页校验时报 blocking 错。\n"
                      "请把这些角色（连同脚手架已有的）一并写进 ## typography，值取计划里的数。")
                self._log(f"   ⚠️ spec_lock 的 typography 缺 {len(missing)} 个计划角色：{missing}")
            if attempt >= SPEC_REPAIR_ROUNDS:
                break

            errors = "\n".join(
                line.strip() for line in observation.splitlines()
                if line.strip().startswith("-") or "[ERROR]" in line
            )[:2500]
            self._log(f"   → 把 validate 报错回灌给模型修正（第 {attempt + 1}/{SPEC_REPAIR_ROUNDS} 轮）")
            # 重建为“当前文件内容 + 报错”，不带历史，避免每轮重发旧文本
            self._context(
                ("【权威 schema（字段/枚举以此为准）】", self._schema_text()),
                ("【design_spec.md 当前内容】", (self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace")),
                ("【spec_lock.md 当前内容】", (self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace")),
            )
            raw = self._ask(
                SPEC_REPAIR_PROMPT.format(
                    errors=errors or observation[-2000:],
                    schemas=self._schema_text(),
                    design_spec=(self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace"),
                    spec_lock=(self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace"),
                ),
                "修正设计文件",
                model=self.plan_model,
                reasoning_effort=self.plan_reasoning,
            )
            filled = self._extract_files(raw)
            if not filled:
                saved = self._save_parse_failure("spec_repair", raw)
                self._log(f"   ⚠️ 修正轮未解析到文件块，原始输出已存到 {saved}")
                continue
            for target, body in filled.items():
                if target in ("design_spec.md", "spec_lock.md"):
                    (self.project_path / target).write_text(body, encoding="utf-8")
                    self._log(f"   已重写 {target}（{len(body):,} 字符）")

        if not ok:
            raise RuntimeError(
                "design_spec.md / spec_lock.md 连续 "
                f"{SPEC_REPAIR_ROUNDS} 轮修正后仍未通过 project_manager validate。"
                "阶段化引擎选择在此停止，而不是带着无效设计继续画页。"
            )

        # 字体标定：角色取自计划里的字体层级
        self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/text_measure.py" calibrate '
            f'"{self.project_path}" --outline {self._spec_role_args()}',
            "text_measure calibrate --outline",
        )

        # 字体标定跑到这里，此时上下文已只剩冻结阶段的内容。
        # 执行核心在执行阶段才注入。

    # ------------------------------------------------------------ P3

    def _viewbox(self) -> str:
        from agent import CANVAS_VIEWBOX  # type: ignore
        return CANVAS_VIEWBOX.get(self.plan.get("canvas") or "ppt169", "0 0 1280 720")

    def _canvas_size(self) -> Tuple[float, float]:
        """解析画布 viewBox 得到 (宽, 高)；缺省退回 1280×720。

        集中在这里，避免各处用 `self._viewbox().split()[2]/[3]` 这类脆弱的
        下标取宽高。
        """
        parts = self._viewbox().split()
        w = float(parts[2]) if len(parts) >= 4 else 1280.0
        h = float(parts[3]) if len(parts) >= 4 else 720.0
        return w, h

    def _calibration_roles(self) -> Dict[str, Any]:
        """读入 text_calibration.json 的 roles 段；缺失/损坏返回空 dict。

        这是 `_find_fit_problems` 与 `_per_role_char_budget` 共同的数据源，
        统一在此解析与容错，避免两处各自重读文件。
        """
        file = self.project_path / "validation" / "text_calibration.json" if self.project_path else None
        if not file or not file.is_file():
            return {}
        try:
            data = json.loads(file.read_text(encoding="utf-8", errors="replace")) or {}
        except (json.JSONDecodeError, OSError):
            return {}
        return data.get("roles") or {}

    def _usable_width(self, factor: float = 1.0) -> float:
        """整页可用文字宽度 = (画布宽 − 左右各 64px 边距) × factor。

        factor=1.0 为整页可用宽度；factor=0.9 为单列文字宽度（列通常窄于整页）。
        """
        canvas_w, _ = self._canvas_size()
        return (canvas_w - 128.0) * factor

    def phase_execute(self) -> None:
        """模型 N 次：每页一次，只写 SVG。页数 >1 且 parallel_pages >1 时并行。"""
        roster = self.plan["roster"]
        svg_dir = self.project_path / "svg_output"
        svg_dir.mkdir(parents=True, exist_ok=True)

        # 只注入执行阶段需要的东西：冻结的锁 + 页序 + 执行核心 + 字体标定表。
        # 规划文档与 schema 到此被移出上下文（共 ~160KB）。
        spec_lock = (self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace")
        roster_lines = "\n".join(
            f"  {p.get('id')}  {p.get('file'):<20} {p.get('role'):<8} {p.get('title', '')}"
            for p in roster
        )
        shared_blocks: List[Tuple[str, str]] = [
            ("【执行锁 spec_lock.md（冻结锚点，不得偏离）】", spec_lock),
            ("【整份 deck 的页序（保持一致性与节奏）】", roster_lines),
            ("【字体标定表（推算文字宽度用它，不要猜）】", self._calibration_text()),
            ("【执行核心（skill 要求一起读完）】", self._blocks["execute_core"]),
        ]

        parallel = self.parallel_pages if self.parallel_pages > 1 and len(roster) > 1 else 0
        mode = f"并行 ≤{parallel}" if parallel else "串行"
        self._stage(f"P3 EXECUTE（模型 ×{len(roster)}，{mode}）")

        if parallel:
            self._execute_parallel(roster, shared_blocks, svg_dir)
        else:
            self._execute_serial(roster, shared_blocks, svg_dir)

    def _page_prompt(
        self, page: Dict[str, Any], index: int, total: int, prev_signatures: str
    ) -> str:
        """构造单页 PAGE_PROMPT（串行/并行共用，避免两处重复填参数）。"""
        typo = self.plan.get("typography") or {}
        colors = self.plan.get("colors") or {}
        points = page.get("content_points") or []
        points_text = "\n".join(f"   {i}. {p}" for i, p in enumerate(points, 1)) or "   （未指定）"
        # 函数图像的几何：代码算好再给模型（见 plot_page_figure 的 docstring）。
        # 失败不拦路，只把原因记下来——日志里能看到，模型回退到自画。
        figure_block, figure_err = plot_page_figure(page.get("figure"))
        if figure_err:
            print(f"   ⚠️ {page.get('file')} 的 {figure_err}", flush=True)
        color_text = ", ".join(f"{k}={v}" for k, v in colors.items()) or "未指定"
        return PAGE_PROMPT.format(
            index=index, total=total,
            file=page.get("file"), role=page.get("role", "content"),
            title=page.get("title", ""), core_message=page.get("core_message", ""),
            points=points_text, viewbox=self._viewbox(), colors=color_text,
            figure_geometry=("\n" + figure_block + "\n") if figure_block else "",
            family=typo.get("family", "Microsoft YaHei"),
            display=typo.get("display", 72), title_size=typo.get("title", 48),
            subtitle=typo.get("subtitle", 32), body=typo.get("body", 24),
            annotation=typo.get("annotation", 18),
            visual_style=self.plan.get("visual_style", ""),
            char_budgets=self._per_role_char_budget(),
            prev_signatures=prev_signatures,
        )

    def _global_style_desc(self) -> str:
        """并行模式的跨页一致性锚点：所有页共享同一段风格描述，替代串行的「前页签名」。"""
        typo = self.plan.get("typography") or {}
        colors = self.plan.get("colors") or {}
        color_text = ", ".join(f"{k}={v}" for k, v in colors.items()) or "未指定"
        return (
            "本 deck 所有页面共享同一视觉语言：风格 "
            f"{self.plan.get('visual_style', '未指定')}，配色 {color_text}，"
            f"字体 {typo.get('family', 'Microsoft YaHei')}。"
            "各页在角色（cover/content/section）允许的差异内，复用同一套图形语言、"
            "留白节奏与视觉元素画法，不要在页间引入新的配色或字体。"
        )

    def _execute_serial(
        self, roster: List[Dict[str, Any]], shared_blocks: List[Tuple[str, str]], svg_dir: Path
    ) -> None:
        """串行路径：逐页生成，靠「前页签名」守住跨页一致性（原实现）。"""
        self._context(*shared_blocks)
        signatures: List[str] = []
        compact_pages = len(roster) > 6
        total = len(roster)

        for index, page in enumerate(roster, 1):
            prev = "\n".join(signatures) if signatures else "（这是第一页）"
            raw = self._ask(
                self._page_prompt(page, index, total, prev),
                f"第 {index}/{total} 页 {page.get('file')}",
                model=self.execute_model,
                reasoning_effort=self.exec_reasoning,
            )
            pages = self._extract_pages(raw, fallback_file=str(page.get("file") or ""))
            if not pages:
                saved = self._save_parse_failure(f"page_{page.get('file')}", raw)
                self._log(
                    f"   ⚠️ 第 {index} 页没有解析出页面块（模型只输出 {len(raw)} 字符），"
                    f"原始输出已存到 {saved.name if saved else '（存盘失败）'}，重试一次"
                )
                raw = self._ask(
                    "你上一条输出里没有可解析的 <page file=\"...\"> 块（解析失败的原始输出已存盘）。\n"
                    "请**只**输出那一个块，标签必须完整闭合，并确保 </page> 闭合。\n"
                    "注意：必须输出**完整**的单页 SVG，不能只给描述或摘要。",
                    f"第 {index} 页重试",
                    model=self.execute_model,
                    reasoning_effort=self.exec_reasoning,
                )
                pages = self._extract_pages(raw, fallback_file=str(page.get("file") or ""))
            if not pages:
                saved = self._save_parse_failure(f"page_{page.get('file')}_retry", raw)
                raise RuntimeError(
                    f"第 {index} 页 {page.get('file')} 连续两次产出失败。\n"
                    f"  第二次模型只输出 {len(raw)} 字符，开头：{(raw or '')[:200]}\n"
                    f"  原始输出已保存供排查。"
                )

            for name, body in pages:
                (svg_dir / name).write_text(body, encoding="utf-8")
                self._log(f"   ✅ {name}（{len(body):,} 字符）")

            # 上下文控制：小 deck 不压缩，保留整页以便跨页一致；大 deck 才压缩。
            if compact_pages:
                sig = ""
                sig_match = re.search(r"<signature>(.*?)</signature>", raw, re.DOTALL)
                if sig_match:
                    sig = " ".join(sig_match.group(1).split())[:300]
                else:
                    written = ", ".join(n for n, _ in pages)
                    sizes = ", ".join(f"{n}={len(b)}" for n, b in pages)
                    sig = f"{page.get('id')} {page.get('role')} {page.get('title','')}｜已写 {written}（{sizes}）"
                signatures.append(f"  {sig}")
                # 把 (提问, 回答) 换成一条驱动器侧记录，不伪造模型语气
                if len(self.messages) >= 2:
                    self.messages[-2:] = [{
                        "role": "user",
                        "content": (
                            f"【驱动器记录】第 {index}/{total} 页已交付：{pages[0][0]}\n"
                            f"签名：{sig}\n"
                            "（完整 SVG 已写入 svg_output/，为控制上下文不再在此展开；"
                            "需要回顾请用 read_file 读取。）"
                        ),
                    }]
                self._log(f"   🗜️ 上下文已压缩为驱动器记录：{sig[:70]}")
            else:
                signatures.append(f"  {page.get('id')} {page.get('role')} {page.get('title','')}")

    def _render_page(
        self, shared_blocks: List[Tuple[str, str]], page: Dict[str, Any], index: int, total: int
    ) -> Tuple[str, str]:
        """在**独立上下文**里生成单页，返回 (文件名, SVG 正文)。

        并行路径专用：不碰 self.messages，用局部 messages；除 agent 统计累加
        （已由 agent._stats_lock 保护）外无共享可变状态。
        """
        prompt = self._page_prompt(page, index, total, self._global_style_desc())
        messages = [{"role": "system", "content": self._system_prompt()}]
        for title, body in shared_blocks:
            messages.append({
                "role": "user",
                "content": f"{title}\n\n{body}\n\n（以上内容已读入，无需再调用工具读取）",
            })
        messages.append({"role": "user", "content": prompt})
        raw = self.agent.call_model(messages, stream=False, model=self.execute_model)
        pages = self._extract_pages(raw, fallback_file=str(page.get("file") or ""))
        if not pages:
            messages.append({"role": "user", "content": (
                "你上一条输出里没有可解析的 <page file=\"...\"> 块。\n"
                "请**只**输出那一个块，标签必须完整闭合，并确保 </page> 闭合。\n"
                "注意：必须输出**完整**的单页 SVG，不能只给描述或摘要。"
            )})
            raw = self.agent.call_model(messages, stream=False, model=self.execute_model)
            pages = self._extract_pages(raw, fallback_file=str(page.get("file") or ""))
        if not pages:
            self._save_parse_failure(f"page_{page.get('file')}", raw)
            raise RuntimeError(
                f"第 {index} 页 {page.get('file')} 连续两次产出失败。\n"
                f"  第二次模型只输出 {len(raw)} 字符，开头：{(raw or '')[:200]}\n"
                f"  原始输出已保存供排查。"
            )
        return pages[0][0], pages[0][1]

    def _execute_parallel(
        self, roster: List[Dict[str, Any]], shared_blocks: List[Tuple[str, str]], svg_dir: Path
    ) -> None:
        """并行路径：所有页同时生成，跨页一致性靠 spec_lock + 全局风格描述。"""
        total = len(roster)
        workers = min(self.parallel_pages, total)
        style = self._global_style_desc()
        self._log(f"   全局风格锚点：{style[:80]}…")

        def submit(index: int, page: Dict[str, Any]) -> Tuple[int, str, str]:
            t0 = _time.time()
            self._log(f"   🚀 第 {index}/{total} 页 {page.get('file')} 开始生成")
            name, body = self._render_page(shared_blocks, page, index, total)
            self._log(
                f"   ✅ 第 {index}/{total} 页 {name} 完成（{len(body):,} 字符，"
                f"{_time.time() - t0:.1f}s）"
            )
            return index, name, body

        results: Dict[int, Tuple[str, str]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(submit, index, page)
                for index, page in enumerate(roster, 1)
            ]
            for fut in concurrent.futures.as_completed(futures):
                index, name, body = fut.result()
                results[index] = (name, body)

        # 按页序写盘，保证 svg_output/ 里文件顺序稳定
        for index in sorted(results):
            name, body = results[index]
            (svg_dir / name).write_text(body, encoding="utf-8")

    # ------------------------------------------------------------ P4

    def _calibration_text(self) -> str:
        """读入字体标定表。

        校验器的报错会写“≈76.3 px per CJK char at 72px incl. headroom”这类换算，
        模型若看不到表就只能猜，反复改而不收敛。把表给它，让它用算术而不是手感。
        """
        file = self.project_path / "validation" / "text_calibration.json" if self.project_path else None
        if not file or not file.is_file():
            return "（标定表缺失）"
        return clip(file.read_text(encoding="utf-8", errors="replace"), 20_000, "标定表")

    def _find_fit_problems(self) -> List[str]:
        """比对标定表的“最长计划行”与画布宽度，找出放不下的计划文案。

        这是 P4 反复修复的根源：计划里 1408 px 的正文行放在 1280 px 画布上，
        每页都必然溢出；而修复提示要求“内容不变”，模型无法在版式层解决。
        """
        roles = self._calibration_roles()
        if not roles:
            return []
        canvas_w, _ = self._canvas_size()
        # 常规页边距：两侧各留 ~64 px
        usable = self._usable_width()

        problems: List[str] = []
        for name, role in roles.items():
            line = role.get("longest_planned_line") or {}
            px = float(line.get("px") or 0)
            if px <= usable:
                continue
            cjk = float(role.get("cjk_chars_per_100px") or 0)
            per_char = 100.0 / cjk if cjk else 0.0
            budget = int(usable / per_char) if per_char else 0
            problems.append(
                f"· 角色 {name}（{role.get('size'):.0f}px，≈{per_char:.1f} px/汉字，来自标定表）\n"
                f"  出现位置：{line.get('slide', '?')}\n"
                f"  原文：{line.get('text', '')}\n"
                f"  估算宽度 {px:.0f} px > 可用宽度 {usable:.0f} px（画布 {canvas_w:.0f} px 减左右各 64 px）\n"
                f"  → 该角色的单行容量约 {budget} 个汉字。请拆行或缩到 {budget} 字以内。"
            )
        return problems

    def _outline_integrity_errors(self, design_text: str) -> List[str]:
        """检测 §IX Content Outline 的结构性破坏（会让 final gate 拒导）。

        复刻 skill ``validate_communication_trace`` 对 Slide block 的检查：
        每个 ``#### Slide NN`` 标题都会被当成正式页，必须带 ``Audience move``
        与 ``Relationships`` 两行；同时编号不得重复——重复意味着模型把
        “修正记录”误写成了新的 Slide 块（如“版面适配修正”段）。
        """
        if not design_text:
            return []
        outline_match = re.search(
            r'^##[ \t]+IX\.[ \t]+Content Outline\b.*$',
            design_text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if outline_match is None:
            return []
        next_section = re.search(
            r'^##[ \t]+',
            design_text[outline_match.end():],
            flags=re.MULTILINE,
        )
        outline_end = (
            outline_match.end() + next_section.start()
            if next_section
            else len(design_text)
        )
        outline = design_text[outline_match.end():outline_end]
        slide_matches = list(re.finditer(
            r'^#{3,6}[ \t]+Slide[ \t]+([0-9]+|NN)\b.*$',
            outline,
            flags=re.IGNORECASE | re.MULTILINE,
        ))
        if not slide_matches:
            return []

        errors: List[str] = []
        seen: Dict[str, int] = {}
        missing_moves: List[str] = []
        missing_rels: List[str] = []
        for index, slide_match in enumerate(slide_matches):
            num = slide_match.group(1)
            seen[num] = seen.get(num, 0) + 1
            block_end = (
                slide_matches[index + 1].start()
                if index + 1 < len(slide_matches)
                else len(outline)
            )
            slide_block = outline[slide_match.end():block_end]
            if re.search(
                r'^[ \t]*-[ \t]+(?:\*\*)?Audience move(?:\*\*)?[ \t]*:',
                slide_block,
                flags=re.IGNORECASE | re.MULTILINE,
            ) is None:
                missing_moves.append(num)
            if re.search(
                r'^[ \t]*-[ \t]+(?:\*\*)?Relationships(?:\*\*)?[ \t]*:',
                slide_block,
                flags=re.IGNORECASE | re.MULTILINE,
            ) is None:
                missing_rels.append(num)

        for num, count in seen.items():
            if count > 1:
                errors.append(
                    f"§IX 里 Slide {num} 出现了 {count} 次（编号重复 = 把修正记录误写成"
                    f"新的 Slide 块；请删除多余的 `#### Slide {num}` 标题，把改动并回原块）"
                )
        if missing_moves:
            errors.append(
                f"§IX 的 Slide {', '.join(missing_moves)} 缺少 `Audience move` 行"
            )
        if missing_rels:
            errors.append(
                f"§IX 的 Slide {', '.join(missing_rels)} 缺少 `Relationships` 行"
            )
        return errors

    def _per_role_char_budget(self) -> str:
        """从标定表算每个字号在整页可用宽度下的单行最大汉字数，注入 P3。

        SVG 超界是 P4 反复修复的最大来源。超界需要像素级算术，而算术恰恰
        是推理最烧钱的地方。这里把上限直接算好给模型，模型只需“数字数”，
        不需要推理就能避开超界。
        """
        typo = self.plan.get("typography") or {}
        sizes = [
            ("display", typo.get("display", 72)),
            ("title", typo.get("title", 48)),
            ("subtitle", typo.get("subtitle", 32)),
            ("body", typo.get("body", 24)),
            ("annotation", typo.get("annotation", 18)),
        ]
        roles = self._calibration_roles()
        # 左右各 64px 边距，再打 0.9 折（列宽通常窄于整页宽）
        usable = self._usable_width(0.9)
        lines: List[str] = []
        for name, size in sizes:
            role = roles.get(name) or {}
            cjk = float(role.get("cjk_chars_per_100px") or 0)
            if cjk:
                budget = int(usable * cjk / 100.0)
            else:
                budget = int(usable / max(1.0, float(size)))
            lines.append(f"   - {name}（{size:.0f}px）：每行 ≤ {budget} 个汉字")
        return "\n".join(lines)

    def _spec_role_args(self) -> str:
        typo = self.plan.get("typography") or {}
        family = typo.get("family") or "Microsoft YaHei"
        roles = [("display", family, typo.get("display", 72)), ("title", family, typo.get("title", 48)),
                 ("subtitle", family, typo.get("subtitle", 32)), ("body", family, typo.get("body", 24)),
                 ("annotation", family, typo.get("annotation", 18))]
        return " ".join(f'--role "{r}:{f}:{s}"' for r, f, s in roles)

    def _run_validate(self, label: str) -> Tuple[bool, str]:
        return self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/project_manager.py" validate "{self.project_path}"',
            label, fatal=False,
        )

    def _run_calibrate(self, label: str) -> None:
        self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/text_measure.py" calibrate '
            f'"{self.project_path}" --outline {self._spec_role_args()}',
            label, fatal=False,
        )

    def phase_fit_plan(self) -> None:
        """P2.5：纯代码检查，不再为字宽反复调模型。

        早先这里会让模型反复拆行/缩写计划文案，实测 3 轮都改不动、白烧
        60s+ 与 3 次调用——因为“拆行”需要推理。而现在逐页阶段有代码算好的
        每行字数上限硬约束，模型写页时自然会写短，计划层偏宽不会真的超界。
        所以字宽问题只提示、不修复；只有 §IX 结构被破坏（会导致导出被拒）
        才调模型修一次。
        """
        self._stage("P2.5 FIT（纯代码检查）")
        design_text = (self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace")

        integrity = self._outline_integrity_errors(design_text)
        if integrity:
            self._log(f"   ⚠️ §IX 结构被破坏（{len(integrity)} 处），必须修，否则导出会被拒：")
            for m in integrity:
                self._log("     " + m)
            self._repair_spec("\n".join(f"- {m}" for m in integrity))
            design_text = (self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace")
            remaining = self._outline_integrity_errors(design_text)
            if remaining:
                self._log(f"   ❌ §IX 结构仍有 {len(remaining)} 处未修复，最终导出可能被拒：")
                for m in remaining:
                    self._log("     " + m)
            else:
                self._log("   ✅ §IX 结构已修复")
        else:
            self._log("   ✅ §IX 结构完整")

        problems = self._find_fit_problems()
        if problems:
            self._log(f"   ℹ️ 有 {len(problems)} 处计划文案偏宽，P3 会按每行字数上限自动写短，无需在此修复")
        else:
            self._log("   ✅ 计划文案宽度均合规")

    def _repair_spec(self, errors: str) -> None:
        """在**计划层**修正 design_spec.md / spec_lock.md（由 validate 或 scope 级错误驱动）。"""
        for round_index in range(1, SPEC_REPAIR_ROUNDS + 1):
            self._context(
                ("【权威 schema（逐字段对照）】", self._schema_text()),
                ("【design_spec.md 当前内容】", (self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace")),
                ("【spec_lock.md 当前内容】", (self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace")),
            )
            raw = self._ask(
                SPEC_REPAIR_PROMPT.format(
                    errors=errors,
                    schemas=self._schema_text(),
                    design_spec=(self.project_path / "design_spec.md").read_text(encoding="utf-8", errors="replace"),
                    spec_lock=(self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace"),
                ),
                f"修正计划文件（第 {round_index} 轮）",
                model=self.plan_model,
                reasoning_effort=self.plan_reasoning,
            )
            files = self._extract_files(raw)
            if not files:
                self._save_parse_failure(f"spec_repair_{round_index}", raw)
                continue
            for target, body in files.items():
                if target in ("design_spec.md", "spec_lock.md"):
                    (self.project_path / target).write_text(body, encoding="utf-8")
                    self._log(f"   已重写 {target}（{len(body):,} 字符）")
            passed, _ = self._run_validate(f"validate（计划修正第 {round_index} 轮后）")
            if passed:
                self._log("   ✅ 计划文件已重新通过 validate")
                return
        self._log("   ⚠️ 计划文件修正轮数用尽")

    def _run_checker(self, stage: str) -> Tuple[bool, Dict[str, List[str]]]:
        """跑校验器，并从**权威 JSON 报告**按文件归集 blocking 错误。

        早期版本抓 stdout 归类，会把 advisory 警告误判成错误，导致对已经合格的页面
        反复“修复” 3 轮而其实“通过”。现在只读 categories.blocking.issues：
        那才是 skill 要求必须修的东西，advisory 警告不阻断交付。
        """
        ok, observation = self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/svg_quality_checker.py" '
            f'"{self.project_path}" --canonical-authoring --stage {stage} --json',
            f"svg_quality_checker --stage {stage}",
            fatal=False,
        )

        report_name = (
            "svg_quality_early_report.json" if stage == "early" else "svg_quality_report.json"
        )
        report_path = self.project_path / "validation" / report_name
        if not report_path.is_file():
            self._log(f"   ⚠️ 未找到 {report_name}，回退到退出码判断")
            return ok, {}

        try:
            report = json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as exc:
            self._log(f"   ⚠️ 报告解析失败：{exc}")
            return ok, {}

        summary = report.get("summary") or {}
        self._log(
            f"   报告：{summary.get('total', '?')} 页 / "
            f"通过 {summary.get('passed', '?')} / "
            f"警告 {summary.get('warnings', '?')} / "
            f"错误 {summary.get('errors', '?')}"
        )

        grouped: Dict[str, List[str]] = {}
        blocking = (report.get("categories") or {}).get("blocking") or {}
        for issue in blocking.get("issues") or []:
            name = issue.get("file") or issue.get("page")
            if not name:
                # 无 file 的多为 scope 级缺陷（如 design_spec.md 缺必填行），
                # 属于**计划层**，不能当成页面去修——否则会“修”一个不存在的文件。
                name = PLAN_SCOPE_KEY
            grouped.setdefault(name, []).append(str(issue.get("message", "")).strip())
        grouped = {k: v for k, v in grouped.items() if v}
        return (not grouped), grouped

    def _auto_fix_bounds(self) -> None:
        """P4 的第一道：把「元素超出容器」类错**直接用代码改掉**（不花模型调用）。

        为什么值得（实测 2026-10-08 12 页 deck）：18 次模型调用里 **8 次**是 P4
        逐页返工同类错，而每次往返 ≈ 80s —— **约 640s 花在“改一个数字”上**。
        这个数字的解是**唯一确定的**，且报错文本自带全部输入，让模型花整次调用
        去算它纯属浪费。

        为什么安全：**快照 → 改 → 复验 → 保留或回滚**。
        最坏情况是“白改一次”（代价 ~1s 的 checker），绝不会把 deck 改坏。
        回滚后还会重跑 checker，保证导出门读到的报告与当前文件一致。

        只碰「超出容器」类；**重叠类不碰**（那是真布局错，要重划版面，无唯一解）。
        详见 bounds_fix 模块的 docstring。
        """
        import bounds_fix

        passed, grouped = self._run_checker("final")
        if passed and not grouped:
            return
        total_before = sum(len(ms) for ms in grouped.values())

        # 四条策略按「改动从小到大」排序，**各自独立复验、不好就回滚**：
        #   嵌套（改层级）→ 挪元素（改位置）→ 扩框（改声明边界）→ 拆行（改内容）
        # 顺序有讲究：
        #   **嵌套必须最先**——它靠“子块是否完全落在外层 bounds 内”判定层级错，
        #   而扩框会把包含关系弄坏（实测：annotation 扩到 1243.7 后就不在 panel
        #   的 1240 之内了，嵌套策略判不出目标）。
        #   挪元素不改边界，放中间；扩框可能级联（子块扩大→超出父块），放其后。
        #   拆行是唯一改文字内容的策略，风险最高，放最后兜底——只在「容器被邻居
        #   挡死 + 文字单行 + CJK 主导 + 拆完放得下」时才动手。
        # 外面套多趟：嵌套之后往往还要再扩父块，一趟跑不干净。
        strategies = (
            ("嵌套完全包含的模块", bounds_fix.nest_contained_root_groups, "overlap"),
            ("挪越界元素", bounds_fix.shift_content_into_container, "bounds"),
            ("扩容器框", bounds_fix.expand_bounds_for_overflow, "bounds"),
            ("拆行超长文字", bounds_fix.reflow_overflow_text, "bounds"),
        )
        for _pass in range(3):
            improved = False
            for label, fixer, kind in strategies:
                snapshots: Dict[str, str] = {}
                changed: List[str] = []
                for fname, msgs in grouped.items():
                    picked = ([m for m in msgs if _OVERLAP_RE.search(m)]
                              if kind == "overlap"
                              else [m for m in msgs
                                    if _BOUNDS_RE.search(m) and not _OVERLAP_RE.search(m)])
                    if not picked:
                        continue
                    path = self.project_path / "svg_output" / fname
                    if not path.is_file():
                        continue
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    new, notes = fixer(raw, picked)
                    for n in notes:
                        self._log(f"     [{label}] {fname} {n}")
                    if new != raw:
                        snapshots[fname] = raw
                        path.write_text(new, encoding="utf-8")
                        changed.append(fname)

                if not changed:
                    continue

                after_passed, after_grouped = self._run_checker("final")
                total_after = sum(len(ms) for ms in after_grouped.values())
                if total_after < total_before or (after_passed and not after_grouped):
                    self._log(f"   ✅ 代码自动修复（{label}）：{total_before} → {total_after} 个 blocking"
                              f"（{len(changed)} 个文件，0 次模型调用）")
                    passed, grouped, total_before = after_passed, after_grouped, total_after
                    improved = True
                    continue

                for fname, raw in snapshots.items():
                    (self.project_path / "svg_output" / fname).write_text(raw, encoding="utf-8")
                self._log(f"   ↩️ {label} 未减少错误（{total_before} → {total_after}），已回滚"
                          f" {len(snapshots)} 个文件")
                passed, grouped = self._run_checker("final")   # 回滚后重跑，报告与文件一致
            if not improved:
                break

        if passed and not grouped:
            self._log("   ✅ 代码自动修复后已无 blocking（全程 0 次模型调用）")
        else:
            self._log(f"   ⚠️ 代码修复已收敛，仍有 {total_before} 个 blocking，交模型处理")

    def phase_verify(self) -> None:
        """代码校验 + 模型只修**有 blocking 错误**的页。

        轮数上限由 ``max_page_repairs`` 控制（环境变量 ``PPT_MAX_PAGE_REPAIRS``）。
        设为 0 就是「不修，直接交给导出」——但要注意导出那道门（见文件头部注释），
        它只认 ``blocking_count == 0``，所以 0 轮并不保证能出片。
        """
        self._stage("P4 VERIFY（代码校验 + 定向修复）")

        if self.max_page_repairs <= 0:
            self._auto_fix_bounds()
            passed, grouped = self._run_checker("final")
            if passed and not grouped:
                self._log("   ✅ 最终校验通过：0 个 blocking 错误")
            else:
                self._log(f"   ⏭️ PPT_MAX_PAGE_REPAIRS=0，跳过模型修复"
                          f"（{len(grouped)} 页仍有 blocking 错误），直接交给导出阶段判定")
            return

        # 循环边界：max_page_repairs 次**修复**必须配 max_page_repairs + 1 次**校验**。
        #
        # 这里曾经是 range(1, max + 1)，等于「查→修」各两轮后就放弃 ——
        # 最后一轮修复的成果**从未被复查**，而 svg_to_pptx 的 release gate 读的正是那份
        # **过期报告**，于是把一份已经合格的 deck 判为 failed 拒绝导出。
        # 实测（2026-10-08 项目 ..._153906）：header 容器已从 126 扩到 131、内部文字底部
        # 126.3，早就不超界；手动重跑 checker 得 blocking=0，重新导出直接成功（12 页 12 备注）。
        # 这与 phase_freeze 里修过的 SPEC_REPAIR_ROUNDS 是**同一个错**，当时漏了这一处。
        settled = False
        for attempt in range(self.max_page_repairs + 1):
            # **每一轮都先让代码修一遍**，而不是只在开头修一次。
            #
            # 实测教训（2026-10-08 项目 ..._173742）：开头跑一次自动修复确实把
            # 2 个 blocking 降到了 1（0 次模型调用）；但随后模型修复**重写了整页
            # SVG**，把代码改好的 bounds 又冲掉了 —— 超界错重新出现，最终导出被拒。
            # 模型的修复输出是整页替换，不是局部补丁，所以代码的修改必须放在
            # “模型可能重写页面”的**每一次之后**。代价是每轮多跑一次 checker（~1s）。
            self._auto_fix_bounds()
            passed, grouped = self._run_checker("final")
            if passed and not grouped:
                self._log("   ✅ 最终校验通过：0 个 blocking 错误")
                settled = True
                break
            if not grouped:
                self._log(f"   ⚠️ 校验未通过（exit 非 0）但报告里没有 blocking 错误，停止自动修复：{attempt}")
                settled = True
                break
            if attempt >= self.max_page_repairs:
                break

            self._log(f"   第 {attempt + 1}/{self.max_page_repairs} 轮修复，涉及 {len(grouped)} 页：{', '.join(grouped)}")
            svg_dir = self.project_path / "svg_output"

            repaired_any = False
            # 先处理计划层缺陷（无 file 的 scope 级错误）
            plan_issues = grouped.pop(PLAN_SCOPE_KEY, None)
            if plan_issues:
                self._log(f"   其中 {len(plan_issues)} 条属于**计划层**（design_spec.md），交给计划修正")
                for m in plan_issues[:4]:
                    self._log(f"      · {m[:110]}")
                self._repair_spec("\n".join(f"- {m}" for m in plan_issues))
                self._run_calibrate("text_measure recalibrate（计划修正后）")
                repaired_any = True
            if not grouped:
                continue

            spec_lock = (self.project_path / "spec_lock.md").read_text(encoding="utf-8", errors="replace")
            calibration = self._calibration_text()
            core = self._blocks["execute_core"]

            for name, errors in grouped.items():
                target = svg_dir / name
                current_svg = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else "（文件不存在）"

                # **每个页面单独重建上下文，不跨页累积。**
                #
                # 这是曾经的成本泄漏：早期版本在一轮里把所有失败页面依次 append，
                # 4 页 ×（≤60,000 字符页面 + 模型 ~11KB 回复）≈ 300 KB ≈ 85K tokens，
                # 叠加 105 KB 执行核心后单次调用预填充高达 ~115K tokens
                # （实测峰值 120,374），总输入 2.2M。每页重建后单次只带
                # 执行核心 + 1 页，降回 ~30K tokens。
                self._context(
                    ("【执行锁 spec_lock.md】", spec_lock),
                    ("【字体标定表（报错里的“≈X px per char”就出自它）】", calibration),
                    ("【执行核心】", core),
                    (f"【待修页面 {name} 的当前内容】", f"```svg\n{clip(current_svg, MAX_DOC_OBSERVATION_CHARS, name)}\n```"),
                )

                hints = self._solve_bounds_errors(errors)
                arithmetic = (
                    "## 代码已解出的修正量（直接采用，不要自己估）\n\n" + "\n\n".join(hints)
                    if hints
                    else "（本轮错误不含文字超界类，无需算术换算）"
                )
                raw = self._ask(
                    REPAIR_PROMPT.format(
                        file=name,
                        errors="\n".join(errors[:25]),
                        arithmetic=arithmetic,
                    ),
                    f"修复 {name}（{len(errors)} 个错误）",
                    model=self.execute_model,
                    reasoning_effort=self.repair_reasoning or self.exec_reasoning,
                )
                pages = self._extract_pages(raw, fallback_file=name)

                # —— 内容完整性闸门（见 visible_texts/content_loss 的注释）——
                # 返工为消错而删内容是实测事故，checker 查不出。丢了就点名回投一次。
                if pages:
                    cand_name, cand_body = pages[0]
                    lost = content_loss(current_svg, cand_body)
                    if lost:
                        self._log(f"   ⚠️ {cand_name} 返工删了 {len(lost)} 条文字"
                                  f"（如「{(lost[0])[:24]}」），回投要求补回")
                        raw2 = self._ask(
                            REPAIR_PROMPT.format(
                                file=name,
                                errors="\n".join(errors[:25]),
                                arithmetic=arithmetic,
                            )
                            + "\n\n【上一轮你把这些文字删掉了，必须保留（可改写措辞但不能删除）】\n"
                            + "\n".join(f"  - {s}" for s in lost[:20]),
                            f"修复 {name}（补回被删内容）",
                            model=self.execute_model,
                            reasoning_effort=self.repair_reasoning or self.exec_reasoning,
                        )
                        pages2 = self._extract_pages(raw2, fallback_file=name)
                        if pages2:
                            lost2 = content_loss(current_svg, pages2[0][1])
                            if len(lost2) < len(lost):
                                pages = pages2
                                self._log(f"   ✅ 补回后只剩 {len(lost2)} 条缺失")
                            else:
                                self._log(f"   ⚠️ 补回无效（仍缺 {len(lost2)} 条），"
                                          f"保留原版并记录——宁可留一个视觉错，不静默丢内容")
                                pages = []

                wrote = False
                for page_name, body in pages:
                    if body.strip() == current_svg.strip():
                        # 模型交了与原文完全相同的内容：不算修复，避免白烧更多轮
                        self._log(f"   ⚠️ {page_name} 修复输出与原文完全相同（无进展），跳过")
                        continue
                    (svg_dir / page_name).write_text(body, encoding="utf-8")
                    repaired_any = True
                    wrote = True
                    self._log(f"   🔧 已写回 {page_name}（{len(body):,} 字符）")
                if not wrote:
                    self._log(
                        f"   ⚠️ {name} 本轮未产生任何改动（模型原地打转），"
                        "停止对它继续重试（改稿需求请直接编辑 svg_output/ 下的文件）"
                    )
            if not repaired_any:
                self._log("   ⚠️ 模型没有输出可解析的页面，停止自动修复")
                return
        if not settled:
            self._log(f"   ⚠️ 已达修复上限 {self.max_page_repairs} 轮仍有 blocking 错误，交给导出阶段判定")

    # ------------------------------------------------------------ P5

    def _write_notes_total(self) -> int:
        """把任务里的逐页旁白写成 ``notes/total.md``，返回写入页数。

        先清空 ``notes/`` 下所有 ``.md``：``scripts/README.md`` 说得很清楚，
        ``--no-notes`` 存在的理由就是「防 ``notes/`` 里的旧文件被嵌进新 PPTX」。
        既然本次要开备注，就必须自己保证目录里只有本轮讲稿，不能留上一轮残货。

        格式按 ``total_md_split.py`` 的要求：每段以 ``# `` 起头、标题与 SVG 同名
        （用不带扩展名的 stem，命中它的 exact 匹配）、段间以 ``---`` 分隔。
        """
        notes_dir = self.project_path / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        for stale in notes_dir.glob("*.md"):
            stale.unlink()

        roster = self.plan.get("roster") or []
        chunks: List[str] = []
        for page, narration in zip(roster, self.narrations):
            stem = Path(str(page.get("file") or "")).stem
            if not stem:
                raise RuntimeError(
                    f"roster 第 {len(chunks) + 1} 页没有 file 字段，讲稿对不到 SVG")
            chunks.append(f"# {stem}\n\n{narration.strip()}")
        (notes_dir / "total.md").write_text(
            "\n\n---\n\n".join(chunks) + "\n", encoding="utf-8")
        return len(chunks)

    def _ensure_fresh_quality_report(self) -> None:
        """导出前**无条件**重跑一次 checker，保证报告对应当前 svg_output。

        为什么必须有这一步（而不是相信 P4 已经查过）：

        `svg_to_pptx.py --quick-generate` 的 release gate 读的是
        `validation/svg_quality_report.json` 这个**文件**，而它可能过期——
        任何一条「改了 SVG 却没重跑 checker」的代码路径都会让我们读到旧结论。

        更阴的是 gate 的判定顺序：

            if blocking_count > 0:      return 'failed'     # ← 先判这个
            if source_match == 'mismatch': return 'stale'

        「过期 + 旧错」会被报成 `failed`，把真正的原因（报告过期）盖掉。
        实测事故（2026-10-08 项目 ..._153906）：P4 最后一轮修复成功但未被复查，
        导出一份 header 容器早已从 126 扩到 131、文字底部 126.3 的合格 deck
        被拒；手动重跑 checker 得 blocking=0，重新导出直接成功（12 页 12 备注）。

        成本几乎为零：checker 是纯代码，实测 0.6~2.2s（对整跑 ~350s 是 0.5%）。
        收益是**兜住整类「顺序错」缺陷**——不只是重试循环，还有未来任何
        「先查后改」或「改完忘了查」的路径。
        """
        passed, grouped = self._run_checker("final")
        if passed and not grouped:
            self._log("   导出前复检：0 个 blocking 错误")
            return
        self._log(f"   ⚠️ 导出前复检仍有 blocking：{len(grouped)} 页"
                  f"（{'、'.join(list(grouped)[:3])}…）—— release gate 很可能拒绝导出")

    def phase_export(self) -> Path:
        """纯代码：写讲稿备注（如有）→ 切分 → 导出。
        """
        self._stage("P5 EXPORT（纯代码）")

        # 先保证报告新鲜：所有会改 SVG 的阶段都已经结束，就在这一步定稿。
        self._ensure_fresh_quality_report()

        notes_flag = "--no-notes"
        if self.notes_enabled:
            pages = self._write_notes_total()
            self._log(f"   讲稿：{pages} 页 → notes/total.md")
            self._run_checked(
                f'{self.python_cmd} "{self.skill_dir}/scripts/total_md_split.py" "{self.project_path}"',
                "total_md_split 切分讲稿",
            )
            # 切分成功（它自己会校验 SVG ↔ 讲稿的一一对应）才敢开备注，
            # 否则宁可退回 --no-notes，也不要把对不上的备注嵌进成品。
            notes_flag = "--with-notes"
        else:
            self._log("   讲稿：本次不带备注（--no-notes）")

        # 正式流程是 total_md_split → finalize_svg → svg_to_pptx（见 svg-pipeline.md
        # 的 Recommended Pipeline）。finalize_svg 聚合 embed_icons / <use> 展开 /
        # 图片对齐与压缩 / tspan 展平，并产出自包含的 svg_final/ 预览。
        # 原生导出器读的是 svg_output/、不依赖 svg_final，但这一步把资源规范化
        # 与预览补齐，且是 skill 流程规定的一环。
        self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/finalize_svg.py" "{self.project_path}"',
            "finalize_svg 收尾",
        )

        self._run_checked(
            f'{self.python_cmd} "{self.skill_dir}/scripts/svg_to_pptx.py" "{self.project_path}" '
            f'--quick-generate {notes_flag}',
            "svg_to_pptx 导出",
        )
        exports = sorted((self.project_path / "exports").glob("*.pptx"))
        if not exports:
            raise RuntimeError("导出命令成功但没有找到 .pptx")
        return exports[-1]

    # ------------------------------------------------------------ 主驱动

    def run(self, task: str) -> str:
        # 留住原文：P5 要从它里面取逐页旁白写 notes/total.md
        self.task = task or ""
        timeline: List[Tuple[str, float, int]] = []
        started = _time.time()

        def timed(label, fn):
            t0 = _time.time()
            calls_before = self.agent.calls
            result = fn()
            elapsed = _time.time() - t0
            timeline.append((label, elapsed, self.agent.calls - calls_before))
            return result

        timed("P0 PREPARE", lambda: self.phase_prepare(task))
        timed("P1 PLAN", lambda: self.phase_plan(task))
        timed("P2 FREEZE", lambda: self.phase_freeze())
        timed("P2.5 FIT", lambda: self.phase_fit_plan())
        timed("P3 EXECUTE", lambda: self.phase_execute())
        timed("P4 VERIFY", lambda: self.phase_verify())
        pptx = timed("P5 EXPORT", lambda: self.phase_export())
        total = _time.time() - started

        lines = [
            f"阶段化执行完成，总耗时 {total:.1f}s，模型调用 {self.agent.calls} 次。",
            "",
            f"{'阶段':<12}{'耗时':>9}{'模型调用':>10}",
            "-" * 34,
        ]
        for label, elapsed, calls in timeline:
            lines.append(f"{label:<12}{elapsed:>8.1f}s{calls:>10}")
        lines += [
            "",
            f"项目目录：{self.project_path}",
            f"交付文件：{pptx}",
            f"页数：{len(self.plan.get('roster') or [])}",
            f"核心信息：{(self.plan.get('communication') or {}).get('core_message', '')}",
        ]
        return "\n".join(lines)
