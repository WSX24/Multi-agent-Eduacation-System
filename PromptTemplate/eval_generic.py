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


# ─────────────────────────── 督学 Agent（supervisor）───────────────────────────
# 督学是 8 个角色里唯一「流程里真跑、却没人针对性验过」的一个（见 V2.0-说明.md
# 缺口 #2）。旧实现只有 make_section_validator 生成的**结构**校验：5 个模块齐不齐、
# 顺序、有无 LaTeX、有无污染词。而模板里最要紧的约束全在「输出纪律」那一段，
# 全是内容：
#   ①【数据只用输入的】不得编造做题量 / 正确率 / 打卡天数
#   ②【任务必须可完成】1-3 个，且写明「做什么 + 做多少」
#   ③【不制造焦虑】禁止倒计时、落后、惩罚、「再不学就来不及了」
#   ④【复习提醒要有依据】要说明为什么现在提醒，不能只罗列知识点名
# 这四条都能程序化判定，所以照样能进闸门（驱动在 eval_supervisor.py）。
# 此前一条都没被验过——模板写了规则、没人查，这正是「模板 ≠ 契约」的典型缺口。

# 制造焦虑的词：全部取自模板「输出纪律」第 3 条。只收方向明确的，
# 不收要看语境的（如「紧张」在「别紧张」里是正向的）。
SUPERVISOR_ANXIETY = ["倒计时", "落后", "惩罚", "来不及", "再不学", "再不努力",
                      "必须马上", "最后机会", "只剩", "扣分", "淘汰"]
# 「不得与其他同学比较」（输出纪律第 1 条后半）
SUPERVISOR_COMPARE = ["排名", "名次", "其他同学", "别的同学", "全班", "班上",
                      "比谁", "别人都"]
# 「先讲亮点」用的正 / 负向表述（模板：先讲亮点，提醒拖延时先说亮点再说改进）
SUPERVISOR_POSITIVE = ["不错", "棒", "稳", "赞", "很好", "扎实", "坚持", "进步",
                       "优秀", "亮眼", "厉害", "了不起", "为你高兴", "值得肯定"]
SUPERVISOR_NEGATIVE = ["偏低", "不足", "薄弱", "下滑", "不理想", "退步", "错误率",
                       "没完成", "欠佳", "问题较大", "吃力"]
# 「复习提醒要有依据」：要说出为什么现在提醒（遗忘规律 / 距离上次多久 / 近期出错）
SUPERVISOR_REASONS = ["遗忘", "规律", "曲线", "节点", "间隔", "周期", "上次", "几天",
                      "过久", "久了", "未登录", "错题", "出错", "前些天"]
# 范文里特有的**数据**，用于检测「照抄范文」。
# ⚠️「小明」刻意不在表内：它是用例输入里的学生姓名（同题用例），放进来会让每份
#    正常输出都命中 → 100% 误报。这与 eval_qa 踩过的坑同类：污染词表必须**双向**
#    审计——既要在范文里出现（否则永远命不中），又不能出现在用例输入里（否则必然
#    误报）。eval_supervisor.self_check 把这两个方向都钉成了断言。
SUPERVISOR_CONTAMINATION = ["45 道", "62%", "提公因式法", "整式乘法", "3 天未登录"]

# 「做多少」的量化表述：阿拉伯数字或中文数字 + 量词。
# 刻意不收「一下」（「标一下」只是随口一句，不含完成标准）。
_QTY = re.compile(
    r"(?:\d+\s*(?:分钟|分|道|题|遍|个|篇|次|页|组|句|行|条|步)"
    r"|[一二两三四五六七八九十]+\s*(?:分钟|分|道|题|遍|个|篇|次|句|行|条|步))")


def count_task_items(body: str) -> list[str]:
    """把【今日任务】切成分条，容忍 ①②③ 与 `1.` / `1、` / `-` 两种写法。"""
    chunks = [c.strip() for c in re.split(r"(?=[①-⑳])", body)]
    items = [c for c in chunks if re.match(r"^[①-⑳]", c)]
    if items:
        return items
    return [l.strip() for l in body.splitlines()
            if re.match(r"^(?:\d{1,2}\s*[.、)]|[-*•])\s*\S", l.strip())]


def validate_supervisor(raw: str, case: dict | None = None, *,
                        check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    """督学专用校验。``case`` 可给：

    ``学生姓名`` / ``学习数据`` / ``待复习知识点`` / ``任务上限``

    ``case`` 缺省时只做与输入无关的检查（结构、焦虑词、禁比较、任务条数、量化），
    与输入相关的检查（数据不编造、引用待复习知识点、问候含姓名）自动跳过——
    这样旧的调用点（eval_skeleton_rollout 的骨架对照实验）不传 case 也不误报。
    """
    case = case or {}
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_by_names(raw, SUPERVISOR_SECTIONS)
    missing = [s for s in SUPERVISOR_SECTIONS if s not in bodies]
    add(f"模块齐全({len(SUPERVISOR_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks
    idx = [order.index(s) for s in SUPERVISOR_SECTIONS]
    add("模块顺序正确", idx == sorted(idx), f"实际 {order}")

    if case.get("学生姓名"):
        add("问候叫出学生姓名", case["学生姓名"] in bodies["一句话问候"],
            f"【一句话问候】里没出现 {case['学生姓名']}")

    # ---- ① 数据只用输入的：不得编造 ----
    if case.get("学习数据"):
        allowed = set(re.findall(r"\d+(?:\.\d+)?", str(case["学习数据"])))
        invented = sorted({n for n in re.findall(r"\d+(?:\.\d+)?", bodies["数据回顾"])
                           if n not in allowed})
        add("数据回顾只用输入数据", not invented,
            f"出现输入里没有的数字 {invented}（编造学习数据）" if invented else "")
    leak_cmp = [t for t in SUPERVISOR_COMPARE if t in raw]
    add("不与同学比较", not leak_cmp, f"出现 {leak_cmp}" if leak_cmp else "")

    # ---- 「先讲亮点」（输出纪律第 3 条后半）----
    seg = bodies["数据回顾"]
    pos = [m.start() for k in SUPERVISOR_POSITIVE for m in re.finditer(k, seg)]
    neg = [m.start() for k in SUPERVISOR_NEGATIVE for m in re.finditer(k, seg)]
    if neg:
        add("数据回顾先讲亮点", bool(pos) and min(pos) < min(neg),
            f"先说了负面（{'/'.join(k for k in SUPERVISOR_NEGATIVE if k in seg[:min(neg) + 6])}）"
            if not pos or min(pos) > min(neg) else "")
    else:
        add("数据回顾有正向表述", bool(pos), "全段没有一句正向的话")

    # ---- ② 任务 1-3 个，且每条写明「做什么 + 做多少」----
    items = count_task_items(bodies["今日任务"])
    add("今日任务 1-3 个", 1 <= len(items) <= 3, f"数到 {len(items)} 条")
    cap = case.get("任务上限")
    if cap:
        add(f"任务不超过 {cap} 个（{case.get('学生水平', '低')}档减量）", len(items) <= cap,
            f"给了 {len(items)} 条，低水平档不得加量")
    if items:
        thin = [it for it in items if not _QTY.search(it)]
        add("每条任务写明做多少", not thin,
            f"{len(thin)}/{len(items)} 条只说了做什么：{thin[0][:34]}" if thin else "")

    # ---- ③ 不制造焦虑 ----
    anxious = [t for t in SUPERVISOR_ANXIETY if t in raw]
    add("不制造焦虑", not anxious, f"出现 {anxious}" if anxious else "")

    # ---- ④ 复习提醒要有依据，且引用输入的待复习知识点 ----
    seg2 = bodies["复习提醒"]
    add("复习提醒说明依据", any(k in seg2 for k in SUPERVISOR_REASONS),
        f"只罗列了内容、没讲为什么现在提醒：{seg2[:34]}")
    if case.get("待复习知识点"):
        toks = [t for t in re.split(r"[、,，/／\s]+", str(case["待复习知识点"])) if t]
        add("复习提醒引用待复习知识点", any(t in seg2 for t in toks),
            f"一个都没提到：{toks}")

    latex = [t for t in LATEX if t in raw]
    add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in SUPERVISOR_CONTAMINATION if t in raw]
        add("无 Few-shot 数据污染", not leak, f"混入 {leak}" if leak else "")
    return checks


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
