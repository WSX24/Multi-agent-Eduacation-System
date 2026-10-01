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
from difflib import SequenceMatcher as _SequenceMatcher

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
# 材料题示范作答（teacher_material）：交的是**答案**，不是教案
MATERIAL_SECTIONS = ["审题", "作答", "回扣设问"]
# 作文构思（teacher_outline）：交的是**方案**，也不是教案
OUTLINE_SECTIONS = ["审题", "立意", "提纲", "素材", "首尾"]

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


# ────────────────── 材料题示范作答（teacher_material）──────────────────

# teacher 的教学模块名：材料题输出里出现这些，说明角色串了（它交的是答案，不是教案）。
# 这就是 V2.0-说明 缺口 #1 记的那个坑：借 teacher 渲材料题，真跑出来的是一堂课。
TEACHING_MODULES = ["本课目标", "知识讲解", "典型例题", "易错提醒", "小结"]
TEACHING_TONE = ["下面我们来", "同学们要注意", "这节课我们", "我们来看这道题"]
MATERIAL_CLICHES = ["生动形象", "意义重大", "值得我们学习", "富有画面感"]

# 材料题范文里特有的锚点，用于检测「答成了范文里那道题」。
# ⚠ 双向审计（照 SUPERVISOR_CONTAMINATION 的教训）：词表里的词必须在范文里出现，
#    又**不得**出现在任何评测用例的输入里——否则每份正常输出都被判照抄范文。
MATERIAL_CONTAMINATION = ["产业园", "农产品加工", "外出务工", "唾液淀粉酶", "碘液"]

_POINT_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"
# 圈号必须**顶在行首**才算一个作答点。
# 踩过的坑：模型会写「由①②得 2.4 N > 2.0 N」——行内的 ①② 是在**引用**前两点，
# 不是新开两点。不限制行首时会凭空多切出作答点，报出「第 4 点没有依据」这种假失败。
_POINT_RE = re.compile(rf"^[ \t]*[{_POINT_MARKS}]", re.M)
_NUMBERED_RE = re.compile(r"^[ \t]*(\d{1,2})\s*[.、]\s*", re.M)
_QUOTE_RE = re.compile(r"「([^」]+)」")


def split_points(body: str) -> list[str]:
    """把【作答】正文切成作答点。认 ①②③ 与 `1.` / `1、` 两种写法（模型两种都用）。

    只认**行首**的序号标记（见 _POINT_RE 的注释：行内引用不算新点）。
    """
    marks = list(_POINT_RE.finditer(body)) or list(_NUMBERED_RE.finditer(body))
    out: list[str] = []
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append(body[m.end():stop].strip())
    return [p for p in out if p]


def _squeeze(s: str) -> str:
    """去掉空白与标点，只留实义字符——用于「引文能否指回原文」的比较。"""
    return re.sub(r"[\s，。；：、？！……——“”‘’\"'（）()「」【】]", "", s)


def quote_backed_by_material(quote: str, material: str) -> bool:
    """引文能否指回材料原文。

    先试整体子串；命不中时把引文按材料里的标点拆开再逐段找：
    模型常把两处原文合并进一个「」里（如「出淤泥而不染、濯清涟而不妖」），
    整段不是子串，但每一段都是。这条容错是必要的，否则会误报。
    """
    q, m = _squeeze(quote), _squeeze(material)
    if q and q in m:
        return True
    for frag in re.split(r"[，。；、：,;]", quote):
        if len(_squeeze(frag)) >= 4 and _squeeze(frag) in m:
            return True
    return False


def validate_material(raw: str, case: dict, *,
                      check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    """材料题示范作答专用校验。``case`` 需给 ``材料`` 与 ``设问``。

    为什么不能只查「模块齐不齐」：材料题最容易走形的地方有两个——
      · 写成教案（借 teacher 渲染时 100% 发生）；
      · **有结论没依据**，或依据指不回材料（看上去像答案，实际不可核对）。
    后者是本角色存在的意义，所以必须逐点验「依据是不是材料里的原文」。

    ``check_contamination=False`` 用于校验 Few-shot 自身：范文的引文本就来自
    范文自己的材料，拿它对照自己必然命中（同 eval_essay 的同一约定）。
    """
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_by_names(raw, MATERIAL_SECTIONS)
    missing = [s for s in MATERIAL_SECTIONS if s not in bodies]
    add(f"模块齐全({len(MATERIAL_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks
    idx = [order.index(s) for s in MATERIAL_SECTIONS]
    add("模块顺序正确", idx == sorted(idx), f"实际 {order}")
    add("模块非空", all(bodies[s] for s in MATERIAL_SECTIONS),
        f"为空 {[s for s in MATERIAL_SECTIONS if not bodies[s]]}")

    # ---- 审题要点明动词与限定范围（模板的硬要求，也决定下文要不要补背景）----
    seg = bodies["审题"]
    add("审题点明动词与限定范围", "动词" in seg and "限定范围" in seg,
        f"缺「动词」或「限定范围」：{seg[:40]}")

    # ---- 逐点验：有依据、且依据能指回材料 ----
    points = split_points(bodies["作答"])
    add("作答点为 2~5 个", 2 <= len(points) <= 5, f"实际 {len(points)} 点")
    no_backing = [i for i, p in enumerate(points, 1) if "依据" not in p]
    add("每点都有「依据」", not no_backing, f"第 {no_backing} 点没有依据")

    material = case.get("材料", "")
    bad_quote: list[str] = []
    for i, p in enumerate(points, 1):
        quotes = _QUOTE_RE.findall(p)
        if not quotes:
            bad_quote.append(f"第{i}点无「」引用")
        elif not any(quote_backed_by_material(q, material) for q in quotes):
            bad_quote.append(f"第{i}点的引文指不回材料：{quotes[0][:20]}")
    add("依据引用了材料原文", not bad_quote, "；".join(bad_quote))

    # ---- 回扣设问要真的是一句结论，不能只是重复审题 ----
    tail = bodies["回扣设问"]
    add("回扣设问给出结论(≥10字)", len(tail) >= 10, f"实际 {len(tail)} 字")
    add("回扣设问非复制审题", tail.strip() != seg.strip(), "与【审题】逐字相同")

    # ---- 交答案，不是交教案 ----
    lessons = [m for m in TEACHING_MODULES if f"【{m}】" in raw]
    add("未输出教学模块", not lessons, f"出现教学模块 {lessons}")
    tones = [t for t in TEACHING_TONE if t in raw]
    add("无讲解口吻", not tones, f"出现 {tones}" if tones else "")
    cliche = [c for c in MATERIAL_CLICHES if c in raw]
    add("无无落点套话", not cliche, f"出现 {cliche}" if cliche else "")

    latex = [t for t in LATEX if t in raw]
    add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in MATERIAL_CONTAMINATION if t in raw]
        add("无 Few-shot 材料污染", not leak, f"混入 {leak}" if leak else "")
    return checks


# ────────────────── 作文构思（teacher_outline）──────────────────

# 提纲每段超过这个字数，基本就是把「这一段干什么」写成了段落实文
OUTLINE_PARA_MAX = 120
# 提纲一段只说一件事：超过 2 句就不再是「一句话说明」而是段落实文
OUTLINE_PARA_MAX_SENTENCES = 2
# 素材短于这个长度基本是「妈妈很辛苦」这类空壳
OUTLINE_MATERIAL_MIN = 20
# 「它证明」后面少于这个字数，就是在敷衍（如「它证明：很好」）
OUTLINE_CLAIM_MIN = 8
# 「它证明」与中心句的相似度超过这个值，说明是**把中心句抄了一遍**当证明
OUTLINE_CLAIM_COPY_RATIO = 0.7
OUTLINE_TOTAL_MAX = 1000          # 模板写「全文不超过 800 字」，留 25% 余量

# 骑墙话：立意必须能站住，“两方面都有道理”不算立意
OUTLINE_FENCE_SITTERS = ["两方面都有道理", "各有各的道理", "各有各的好", "都有道理",
                        "两面都有理", "也都有道理", "双方都有道理"]
# 没落点的素材话术（模板里点名禁止）
OUTLINE_EMPTY_MATERIAL = ["古今中外有很多例子", "很多人都是这样", "类似的例子很多"]
# 讲解口吻：出现即说明这个角色又跑回老师讲课了（4A 原先借 teacher 时的形态）
# ⚠ 扫描前要先剥掉「」引文（见 _strip_quotes 的注释）——散文开头句里的
#   「很少有人先问一句：……」是修辞，不是讲课腔，直接扫会假红。
OUTLINE_LEAK_TONE = TEACHING_TONE + ["通俗例子", "先问一句", "本节课不提供例题", "下面按五步走"]

# 范文里特有的锚点（记叙文那份的米袋、议论文那份的三分钟看完）。
# ⚠ 双向审计：在范文里出现，且**不得**出现在任何用例输入里，否则每份正常输出都被判照抄。
OUTLINE_CONTAMINATION = ["米袋", "三分钟看完", "西游记"]

_OUTLINE_PARA_RE = re.compile(r"^[ \t]*第\s*\d{1,2}\s*段", re.M)
_MATERIAL_ITEM_RE = re.compile(r"素材\s*\d{1,2}\s*[：:]")
_MATERIAL_USE_RE = re.compile(r"用在第\s*(\d{1,2})\s*段")
_SENTENCE_RE = re.compile(r"[。！？!?]")
# 从【立意】里单独取出「中心句：…」（到「支撑：」为止），用于判「它证明」是否只是把它抄了一遍
_CENTRAL_RE = re.compile(r"中心句[：:](.*?)(?=支撑[：:]|\Z)", re.S)
def outline_paragraphs(body: str) -> list[str]:
    """把【提纲】切成一段一条（按「第N段」行首标记）。"""
    marks = list(_OUTLINE_PARA_RE.finditer(body))
    out: list[str] = []
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append(body[m.start():stop].strip())
    return out


def outline_material_items(body: str) -> list[str]:
    """把【素材】切成一条一条（按「素材N：」划分）。"""
    marks = list(_MATERIAL_ITEM_RE.finditer(body))
    out: list[str] = []
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append(body[m.start():stop].strip())
    return out


def _claim_of(item: str) -> str:
    """取素材条目里「它证明」后面的那句话（没有则返回空串）。

    ⚠ 必须先剔掉「（用在第M段）」再量长度：那个后缀有十几个字，
    留着会把「它证明：很好。（用在第1段）」这种抄袭句捧成合格。
    """
    if "证明" not in item:
        return ""
    claim = item.split("证明", 1)[1].lstrip("：: ——-").strip()
    claim = _MATERIAL_USE_RE.sub("", claim)
    return claim.strip().strip("（）() ")


def _looks_like_paragraph(p: str) -> list[str]:
    """判断一条提纲是不是被写成了段落实文，返回命中的原因（空 = 合格）。

    “提纲”与“实文”的区别在于**它说了什么**：提纲说的是「这一段要干什么」，实文是
    可直接当正文用的叙事段。判据只用两个可稳健判定的：
      ① 一句话：句数 ≤ 2（否则是叙事段落）；② 字数 ≤ 120（兜底，防分号长句）。

    ⚠ 曾经还有第三条「不得含「」引文」——**已删，它是假红**：
    2026-10-01 用 `-n 5` 真跑时它拦下了 2/10 份输出，例如
    「第4段（合）：……我明白了「不糊弄」是什么意思，回扣中心句。」与
    「第1段（总）：引出「捷径」话题，亮出中心句。」——那些「」包的是**题眼/主题词**，
    不是对白，提纲里这么写完全正常；而且回投后模型也删不掉（题眼就长那样），
    属「过严的闸门回投也修不好」。真要判「能不能当正文用」是语义问题，归人看或 L3。
    """
    why: list[str] = []
    body = _OUTLINE_PARA_RE.sub("", p, count=1).strip()
    n_sent = len(_SENTENCE_RE.findall(body))
    if n_sent > OUTLINE_PARA_MAX_SENTENCES:
        why.append(f"{n_sent} 句")
    if len(body) > OUTLINE_PARA_MAX:
        why.append(f"{len(body)} 字")
    return why


def _strip_quotes(text: str) -> str:
    """剥掉「」引文后再扫讲解口吻。

    `-n 5` 真跑暴露的假红：模型写的开头句是
      「当有人告诉你这道题有秒杀法时，先问一句：被省掉的，是重复劳动，还是必须自己走的那段路。」
    引号里的「先问一句」是修辞，不是讲课腔。而旧版 4A 的讲课腔（「先问一句：拿到《这也是课堂》，
    能不能……」）是**不带引号**的——所以剥掉引号后，真假两例仍能分清。
    同理，【首尾】里的名句引用、【素材】里的原句引文都不该参与口吻扫描。
    """
    return re.sub(r"「[^」]*」", "", text)


def validate_outline(raw: str, case: dict, *,
                     check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    """作文构思专用校验。``case`` 可给 ``题目``（当前断言用不到，留给后续扩展）。

    为什么不能只查「模块齐不齐」：构思最容易走形的三处是——
      · 写成教案（借 teacher 渲染时 100% 发生，真跑输出 1180 字全是教学模块）；
      · 立意骑墙或提纲写成段落实文（看上去像构思，实际不能用）；
      · 素材是「古今中外有很多例子」这类空话。
    这三条都是可程序化判定的，所以这里写成断言而不是靠人看。

    ``check_contamination=False`` 用于校验 Few-shot 自身：范文的素材本就来自它自己的
    题目，拿它对照自己必然命中（同 eval_essay / validate_material 的约定）。
    """
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_by_names(raw, OUTLINE_SECTIONS)
    missing = [s for s in OUTLINE_SECTIONS if s not in bodies]
    add(f"模块齐全({len(OUTLINE_SECTIONS)})", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks
    idx = [order.index(s) for s in OUTLINE_SECTIONS]
    add("模块顺序正确", idx == sorted(idx), f"实际 {order}")
    add("模块非空", all(bodies[s] for s in OUTLINE_SECTIONS),
        f"为空 {[s for s in OUTLINE_SECTIONS if not bodies[s]]}")

    # ---- 审题：必须给出反例（“什么写法会跑题”）----
    add("审题给出反例", "反例" in bodies["审题"], f"无「反例：」：{bodies['审题'][:40]}")

    # ---- 立意：中心句 + 支撑，且不得骑墙 ----
    idea = bodies["立意"]
    add("立意有中心句与支撑", "中心句" in idea and "支撑" in idea, f"实际：{idea[:50]}")
    fence = [w for w in OUTLINE_FENCE_SITTERS if w in idea]
    add("立意不骑墙", not fence, f"出现 {fence}" if fence else "")

    # ---- 提纲：3-5 段、标出重点、每段是「一句话说明」而不是段落实文 ----
    paras = outline_paragraphs(bodies["提纲"])
    para_ids = set(range(1, len(paras) + 1))
    add("提纲为 3-5 段", 3 <= len(paras) <= 5, f"实际 {len(paras)} 段")
    add("提纲标出重点段", "重点" in bodies["提纲"], "没有任何段落标「← 重点」")
    prose = [f"第{i}段({'/'.join(why)})" for i, p in enumerate(paras, 1)
             if (why := _looks_like_paragraph(p))]
    add("提纲每段是一句话说明", not prose,
        f"写成了段落实文：{'；'.join(prose)}" if prose else "")

    # ---- 素材：条数、每条要真证明点什么、且与提纲咬合 ----
    mat = bodies["素材"]
    items = outline_material_items(mat)
    add("素材至少 2 条", len(items) >= 2, f"实际 {len(items)} 条")

    thin = [i for i, it in enumerate(items, 1) if len(_claim_of(it)) < OUTLINE_CLAIM_MIN]
    add(f"每条素材说明它证明什么(≥{OUTLINE_CLAIM_MIN}字)", not thin,
        f"第 {thin} 条的「它证明」过短或缺失")

    # 与提纲咬合：标明用在哪一段，且段号真实存在
    uses = [_MATERIAL_USE_RE.search(it) for it in items]
    no_use = [i for i, u in enumerate(uses, 1) if not u]
    add("每条素材标注用在哪一段", not no_use,
        f"第 {no_use} 条没写「（用在第M段）」")
    bad_ref = [f"第{i}条→第{u.group(1)}段" for i, u in enumerate(uses, 1)
               if u and int(u.group(1)) not in para_ids]
    add("素材指向的段号存在", not bad_ref, f"指向不存在的段：{bad_ref}" if bad_ref else "")

    key_paras = [i for i, p in enumerate(paras, 1) if "重点" in p]
    covered = {int(u.group(1)) for u in uses if u}
    uncovered = [i for i in key_paras if i not in covered]
    add("重点段有素材支撑", not uncovered,
        f"第 {uncovered} 段标了重点却没有素材" if uncovered else "")

    # 素材不能是空壳；也不能把中心句抄一遍当「证明」
    shell = [i for i, it in enumerate(items, 1) if len(it) < OUTLINE_MATERIAL_MIN]
    add(f"素材不笼统(每條≥{OUTLINE_MATERIAL_MIN}字)", not shell, f"第 {shell} 条过于笼统")
    m_central = _CENTRAL_RE.search(idea)
    central = _squeeze(m_central.group(1)) if m_central else ""
    copycat: list[int] = []
    if central:
        for i, it in enumerate(items, 1):
            claim = _squeeze(_claim_of(it))
            if claim and _SequenceMatcher(None, claim, central).ratio() >= OUTLINE_CLAIM_COPY_RATIO:
                copycat.append(i)
    add("素材证明不是照抄中心句", not copycat,
        f"第 {copycat} 条的「它证明」与中心句几乎逐字相同" if copycat else "")

    empty = [w for w in OUTLINE_EMPTY_MATERIAL if w in mat]
    add("素材无空话", not empty, f"出现 {empty}" if empty else "")

    # ---- 首尾：分别以「开头：」「结尾：」起头 ----
    tail = bodies["首尾"]
    add("首尾分别给出开头与结尾", "开头" in tail and "结尾" in tail, f"实际：{tail[:40]}")

    # ---- 交方案，不是交全文、也不是交教案 ----
    add(f"未写成全文(≤{OUTLINE_TOTAL_MAX}字)", len(raw) <= OUTLINE_TOTAL_MAX,
        f"实际 {len(raw)} 字，已接近或超过一篇作文")
    lessons = [m for m in TEACHING_MODULES if f"【{m}】" in raw]
    add("未输出教学模块", not lessons, f"出现教学模块 {lessons}")
    scanned = _strip_quotes(raw)
    tones = [t for t in OUTLINE_LEAK_TONE if t in scanned]
    add("无讲解口吻", not tones, f"出现 {tones}" if tones else "")

    latex = [t for t in LATEX if t in raw]
    add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in OUTLINE_CONTAMINATION if t in raw]
        add("无 Few-shot 题目污染", not leak, f"混入 {leak}" if leak else "")
    return checks


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("assistant 期望模块:", ASSISTANT_SECTIONS)
    print("supervisor 期望模块:", SUPERVISOR_SECTIONS)
    print("assistant_essay 期望模块:", ESSAY_SECTIONS)
    print("qa 期望模块:", QA_SECTIONS)
    print("teacher_material 期望模块:", MATERIAL_SECTIONS)
    print("teacher_outline 期望模块:", OUTLINE_SECTIONS)
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
