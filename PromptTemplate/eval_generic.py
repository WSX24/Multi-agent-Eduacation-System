#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用结构校验器 —— 给还没有专属校验器的模板（assistant / supervisor）用

这些模板的输出格式是「N 个编号模块」，没有 teacher_planner 那样的 YAML 结构，
所以只需校验：模块是否齐全、顺序是否正确、有无 LaTeX、有无 Few-shot 学科污染。

这不是完整的质量校验（不检查内容是否合理），但足以回答消融/变体实验关心的问题：
**换掉 Few-shot 之后，格式合规率会不会掉。**
"""
from __future__ import annotations

import re

LATEX = ["\\frac", "\\times", "\\div", "\\cdot", "\\[", "\\]", "\\(", "\\)", "\\sqrt"]


def split_by_names(raw: str, names: list[str]) -> tuple[list[str], dict[str, str]]:
    """按给定模块名定位分段（容忍带编号或不带编号两种写法）。"""
    marks: list[tuple[int, int, str]] = []
    for name in names:
        m = re.search(r"^[ \t]*(?:\d{1,2}\s*[.、]\s*)?【" + re.escape(name) + r"】", raw, re.M)
        if m:
            marks.append((m.start(), m.end(), name))
    marks.sort()
    bodies: dict[str, str] = {}
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(raw)
        bodies[name] = raw[end:stop].strip()
    return [n for _, _, n in marks], bodies


def make_section_validator(
    expected: list[str],
    *,
    contamination: list[str] | None = None,
    min_chars: int = 1,  # 只查非空：「置信度」的合法取值就是「高」这一个字
):
    """生成 ``validate(raw, case=None) -> [(检查项, 是否通过, 说明)]``。"""

    def validate(raw: str, case: dict | None = None) -> list[tuple[str, bool, str]]:
        checks: list[tuple[str, bool, str]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append((name, ok, detail))

        order, bodies = split_by_names(raw, expected)
        missing = [s for s in expected if s not in bodies]
        add(f"模块齐全({len(expected)})", not missing, f"缺 {missing}" if missing else "")
        if missing:
            return checks

        idx = [order.index(s) for s in expected]
        add("模块顺序正确", idx == sorted(idx), f"实际 {order}")

        thin = [s for s in expected if len(bodies[s]) < min_chars]
        add("模块非空", not thin, f"过短或为空 {thin}")

        latex = [t for t in LATEX if t in raw]
        add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")

        if contamination:
            leak = [t for t in contamination if t in raw]
            add("无 Few-shot 学科污染", not leak, f"混入 {leak}" if leak else "")
        return checks

    return validate


# 各模板的模块清单，与 templates/*.yaml 的 output_format 保持一致
ASSISTANT_SECTIONS = ["得分", "答案判定", "错误分析", "知识点归因", "改进建议", "置信度"]
SUPERVISOR_SECTIONS = ["一句话问候", "数据回顾", "今日任务", "激励语", "复习提醒"]
ESSAY_SECTIONS = ["总分", "维度得分", "等级", "亮点", "逐条问题", "改进建议", "置信度"]
QA_SECTIONS = ["思路引导", "分步讲解", "分层适配", "同类题", "知识点链接"]

validate_assistant = make_section_validator(
    ASSISTANT_SECTIONS,
    contamination=["一元二次方程", "x² - 5x + 6", "x = 2", "x = 3", "代回检验", "求根"],
)
validate_supervisor = make_section_validator(
    SUPERVISOR_SECTIONS,
    contamination=["小明", "45 道", "62%", "提公因式法", "整式乘法", "3 天未登录"],
)


# ─────────────────────── 作文/主观题批改（主观题评分模型） ───────────────────────
# assistant（客观题）的校验器查不了这个模板：主观题不看「对/错」，而看
# ① 维度是否与输入一致、② 各维得分之和是否等于总分（两条加法都要能核对）、
# ③ 等级是否与总分占比自洽、④ 每条评价是否真有原文依据（防无落点的套话）。
# 这四项都是可程序化判定的，因此照样能进闸门。

_NUM = r"(\d+(?:\.\d+)?)"
_ESSAY_GRADES = ["一类文", "二类文", "三类文", "四类文"]


def parse_score(text: str) -> tuple[float, float] | None:
    """从一段文本里抽「分子/分母」。"""
    m = re.search(_NUM + r"\s*/\s*" + _NUM, text)
    return (float(m.group(1)), float(m.group(2))) if m else None


def parse_dimension_scores(body: str) -> list[tuple[str, float, float]]:
    """从【维度得分】里抽逐维度「维度名 得分/满分」。"""
    out: list[tuple[str, float, float]] = []
    pat = re.compile(r"^[\s\-•·]*(\D{2,12}?)\s*" + _NUM + r"\s*/\s*" + _NUM, re.M)
    for m in pat.finditer(body):
        name = m.group(1).strip(" 　：:—-")
        if name:
            out.append((name, float(m.group(2)), float(m.group(3))))
    return out


def grade_of(ratio: float) -> str:
    """总分占比 → 等级（与 templates/assistant_essay.yaml 的判据逐字一致）。"""
    if ratio >= 0.85:
        return "一类文"
    if ratio >= 0.70:
        return "二类文"
    if ratio >= 0.55:
        return "三类文"
    return "四类文"


def validate_essay(raw: str, case: dict, *,
                   check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    """作文/主观题批改专用校验。``case`` 需给 ``总分`` 与 ``维度``（名→满分）。

    ``check_contamination=False`` 用于校验 Few-shot 自身：范文的批改输出本就要
    引用它自己那份学生作文，拿它对照自己必然命中（与 eval_planner 的
    ``check_copy=False`` 同一约定）。
    """
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_by_names(raw, ESSAY_SECTIONS)
    missing = [s for s in ESSAY_SECTIONS if s not in bodies]
    add(f"模块齐全({len(ESSAY_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks
    idx = [order.index(s) for s in ESSAY_SECTIONS]
    add("模块顺序正确", idx == sorted(idx), f"实际 {order}")

    # ---- 总分分母必须来自输入 ----
    total = parse_score(bodies["总分"])
    add("总分可解析且分母=输入总分",
        total is not None and total[1] == float(case["总分"]),
        f"解析到 {total}，应为 /{case['总分']}")

    # ---- 维度与输入一致，各维分母对得上 ----
    dims = parse_dimension_scores(bodies["维度得分"])
    want = case["维度"]
    got_names = [d[0] for d in dims]
    add("维度与输入一致（不增不减）", got_names == list(want),
        f"得到 {got_names}，应为 {list(want)}")
    bad_full = [(n, f) for n, _, f in dims if n in want and f != float(want[n])]
    add("各维分母=输入满分", not bad_full, f"不符 {bad_full}" if bad_full else "")

    # ---- 两条加法：各维得分之和 == 总分得分 ----
    if total is not None and len(dims) == len(want):
        s = round(sum(d[1] for d in dims), 3)
        add("维度得分之和=总分", abs(s - total[0]) <= 0.5,
            f"维度之和 {s} vs 总分得分 {total[0]}")

    # ---- 等级与占比自洽 ----
    grade_body = bodies["等级"]
    stated = next((g for g in _ESSAY_GRADES if g in grade_body), None)
    if total is not None and total[1]:
        expect = grade_of(total[0] / total[1])
        add("等级与总分占比自洽", stated == expect,
            f"写了「{stated}」，按 {total[0]}/{total[1]} 应为「{expect}」")
    else:
        add("等级合法", stated in _ESSAY_GRADES, f"实际 {stated}")

    # ---- 每条评价都要有原文落点（用「」引用）----
    for sec in ("亮点", "逐条问题"):
        add(f"{sec}有原文引用", "「" in bodies[sec], "未出现「」引用")
    add("不代写全文（改进建议≤400字）", len(bodies["改进建议"]) <= 400,
        f"实际 {len(bodies['改进建议'])} 字")
    add("给出示范改写", "示范改写" in bodies["改进建议"], "未按要求给示范改写")

    # ---- 通用禁止项 ----
    latex = [t for t in LATEX if t in raw]
    add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in ESSAY_CONTAMINATION if t in raw]
        add("无 Few-shot 学科污染", not leak, f"混入 {leak}" if leak else "")
    return checks


# 范文里特有的句子/事例，用于检测「照抄范文」
ESSAY_CONTAMINATION = ["吃坏了胃", "现在的生活节奏越来越快", "看清身边的风景"]

# 答疑范文里特有的**专有锚点**（课文篇目、具体文句），用于检测“答成了另一道题”。
#
# ⚠️ 两条踩过的坑，决定了这个词表只能这么短：
#   ① 输入自带的串不能放（如知识点链里的「有理数乘法」）——正常作答必然出现；
#   ② **概念相邻词**也不能放（如「翻折」「转身」）：它们是讲解负负得正的正当用语，
#      模型用来解释同一个概念不算照抄。
# 因此只留“只能来自范文”的课文锚点；理科那篇共用通用概念，没有可用的锚点。
QA_CONTAMINATION = ["爱莲", "垄上", "何陋之有", "出淤泥"]


def validate_qa(raw: str, case: dict, *,
                check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    """答疑专用校验。``case`` 可给：

    ``答案词``    不得出现在【思路引导】里的“最终结论”片段
    ``链内``      输入的知识点链里的项（【知识点链接】不得自行增补链外内容）
    ``max_chars`` 全文长度上限（用于「超纲问题不长篇大论」这类用例）

    答疑最容易走形的地方不是格式，而是**把「引导」写成了「答案」**：
    模型一旦兴奋就直接把结论撂出来，【思路引导】只剩一个标题。
    这是可程序化判定的——把该题的结论关键词钉死，它不得出现在引导里。
    """
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_by_names(raw, QA_SECTIONS)
    missing = [s for s in QA_SECTIONS if s not in bodies]
    add(f"模块齐全({len(QA_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks
    idx = [order.index(s) for s in QA_SECTIONS]
    add("模块顺序正确", idx == sorted(idx), f"实际 {order}")

    # ---- 核心断言：引导不喂答案 ----
    for tok in case.get("答案词") or []:
        add(f"思路引导未给出结论({tok})", tok not in bodies["思路引导"],
            "把最终结论写进了引导，学生没机会自己想了")

    # ---- 同类题只出题，不附答案 ----
    # 不能简单地查「答案」二字：像「做完后告诉我你的答案」是在**要**学生的答案，
    # 属于正确行为。只认“真的把答案附上了”的标记。
    seg_ans = bodies["同类题"]
    markers = [m for m in ("参考答案", "【答案】", "答案如下", "答案是", "正确答案")
               if m in seg_ans]
    if re.search(r"=\s*-?\d", seg_ans):
        markers.append("算式结果")
    add("同类题不附答案", not markers, f"附了答案：{markers}")

    # ---- 分层适配要真的生效 ----
    seg = bodies["分层适配"]
    named = any(k in seg for k in ("基础档", "进阶档", "基础版", "进阶版", "入门档"))
    add("分层适配写明讲法", named, f"未指明档位：{seg[:40]}")

    # ---- 知识点链不得自行增补 ----
    if case.get("链内"):
        seg2 = bodies["知识点链接"]
        hit = [k for k in case["链内"] if k in seg2]
        add("知识点链接引用了输入的链", bool(hit), f"链内项一个都没提到：{case['链内']}")

    if case.get("max_chars"):
        add(f"简短作答(≤{case['max_chars']}字)", len(raw) <= case["max_chars"],
            f"实际 {len(raw)} 字")
    # 超纲/无关问题：要**说明超出范围**，而不是硬讲
    if case.get("须提"):
        hit = [k for k in case["须提"] if k in raw]
        add("超纲时说明范围", bool(hit), f"未说明范围（应有 {case['须提']} 之一）")

    latex = [t for t in LATEX if t in raw]
    add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in QA_CONTAMINATION if t in raw]
        add("无 Few-shot 事例污染", not leak, f"混入 {leak}" if leak else "")
    return checks


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("assistant 期望模块:", ASSISTANT_SECTIONS)
    print("supervisor 期望模块:", SUPERVISOR_SECTIONS)
    print("assistant_essay 期望模块:", ESSAY_SECTIONS)
    print("qa 期望模块:", QA_SECTIONS)
    bad = ("1. 【得分】3/5\n2. 【答案判定】部分正确\n")
    print("残缺样本应报缺模块:", [n for n, ok, _ in validate_assistant(bad) if not ok])
    # 自检作文解析器：故意让维度之和与总分对不上，应当被检出
    sample = ("1. 【总分】30/40\n2. 【维度得分】立意与思想 6/10 —— x\n"
              "结构与条理 6/10 —— x\n语言与表达 7/10 —— x\n素材与论证 5/10 —— x\n"
              "3. 【等级】二类文\n4. 【亮点】「风景」写得好\n"
              "5. 【逐条问题】① 「总之」一句重复\n6. 【改进建议】示范改写：xx\n7. 【置信度】高\n")
    res = validate_essay(sample, {"总分": 40,
                                 "维度": {"立意与思想": 10, "结构与条理": 10,
                                          "语言与表达": 10, "素材与论证": 10}})
    print("作文样本应被检出的问题:", [n for n, ok, _ in res if not ok])
