# -*- coding: utf-8 -*-
"""S0 范围解析：一句话 → FlowSpec。

为什么 S0 必须存在、且必须做「模块切分」：
``teacher_planner`` 的输入契约写死「总周期 < 16 周」。而「系统学习高中语文」
实际是 3 年课程——原样传进去会被 ``require_inputs`` 拦下，硬把 3 年塞进 12 周
则产出的方案必然糊弄。所以先把大范围切成「可教学的模块」，让用户选一个，
再用 8~15 周为一个阶段做规划。这也是用户确认过的交互点。

切分来源两级：内置表（确定性、零成本）→ 模型生成（表里没有的学段/学科）。
"""
from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, field

from . import config  # noqa: F401  —— 确保单独 import 本模块时 stdout 也是 utf-8

# ---------------------------------------------------------------------------
# 学段 / 学科 / 水平
# ---------------------------------------------------------------------------
STAGES = ("小学", "初中", "高中", "大学", "考研")

_GRADE_HINTS = {
    "小学": re.compile(r"小学|[一二三四五六]年级"),
    "初中": re.compile(r"初中|初一|初二|初三|七年级|八年级|九年级"),
    "高中": re.compile(r"高中|高一|高二|高三"),
    "大学": re.compile(r"大学|本科|大[一二三四]"),
    "考研": re.compile(r"考研|研究生"),
}

SUBJECTS = ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理",
            "政治", "思想政治", "道德与法治", "信息技术", "计算机", "编程",
            "科学", "音乐", "美术", "体育")

LEVEL_SYNONYMS = {
    "零基础": "入门", "小白": "入门", "初学": "入门", "入门": "入门",
    "基础": "基础", "初级": "基础", "普通": "基础", "一般": "基础",
    "中等": "中等", "中级": "中等", "进阶": "进阶", "提高": "进阶",
    "拔高": "进阶", "竞赛": "竞赛", "奥赛": "竞赛", "尖子": "竞赛",
}

#: 内置模块表。值是「模块名 → 一句话目标」，顺序即教学顺序。
MODULE_MAP: dict[tuple[str, str], dict[str, str]] = {
    ("高中", "语文"): {
        "现代文阅读": "散文/小说/论述类文本的细读、题型判定与规范作答",
        "古诗文阅读": "文言实词虚词、断句翻译与古代诗歌鉴赏",
        "语言文字运用": "词语辨析、病句修改、语段衔接与修辞",
        "写作": "审题立意、结构搭建与议论文论证",
    },
    ("高中", "数学"): {
        "集合与函数": "集合运算、函数性质与基本初等函数",
        "三角函数与解三角形": "三角恒等变换、图象性质与正余弦定理",
        "数列与不等式": "等差等比数列、递推与不等式证明",
        "立体几何与解析几何": "空间向量、直线与圆锥曲线",
        "概率统计": "计数原理、概率分布与统计推断",
        "导数与应用": "导数运算、单调性极值与不等式",
    },
    ("高中", "英语"): {
        "词汇与语法": "高频词汇、时态语态与非谓语",
        "阅读理解": "细节、推理、主旨与七选五",
        "完形与语法填空": "语篇逻辑与语法填空",
        "写作": "应用文与读后续写",
    },
    ("高中", "物理"): {
        "力学": "运动学、动力学、动量与能量",
        "电磁学": "电场磁场、电路与电磁感应",
        "热学与光学": "分子动理论、气体定律与几何光学",
        "近代物理": "原子结构、光电效应与核反应",
    },
    ("高中", "化学"): {
        "基本概念与计算": "物质的量、离子反应与氧化还原",
        "元素化合物": "金属与非金属及其化合物",
        "反应原理": "速率平衡、水溶液与电化学",
        "有机化学": "官能团、反应类型与合成路线",
        "实验": "实验装置、操作规范与探究设计",
    },
    ("高中", "生物"): {
        "分子与细胞": "细胞结构、代谢与增殖",
        "遗传与进化": "遗传规律、变异与进化",
        "稳态与调节": "神经体液免疫调节与生态",
        "生物技术与工程": "基因工程、细胞工程与发酵",
    },
    ("高中", "历史"): {
        "中国古代史": "政治制度、经济与社会、思想文化",
        "中国近现代史": "列强侵略、救亡图存与新中国建设",
        "世界史": "古代文明、资本主义发展与两次世界大战",
    },
    ("高中", "地理"): {
        "自然地理": "地球运动、大气水文与地貌",
        "人文地理": "人口城市、农业工业与交通",
        "区域地理": "区域认知、区域发展与国土整治",
    },
    ("高中", "政治"): {
        "经济生活": "价格、生产、分配与市场经济",
        "政治生活": "公民、政府、民主与法治",
        "文化生活": "文化传承、创新与民族精神",
        "生活与哲学": "唯物论、辩证法、认识论与历史唯物主义",
    },
    ("初中", "语文"): {
        "现代文阅读": "记叙文/说明文/议论文的阅读与答题",
        "文言文阅读": "实词、翻译与文意理解",
        "名著与综合性学习": "名著导读与语言实践",
        "写作": "记叙文写作与审题立意",
    },
    ("初中", "数学"): {
        "数与式": "有理数、整式分式与二次根式",
        "方程与不等式": "一元一次/二次方程与不等式组",
        "函数": "一次函数、反比例函数与二次函数",
        "几何": "三角形、四边形、圆与相似",
        "统计与概率": "数据描述与概率初步",
    },
    ("初中", "英语"): {
        "词汇与语法": "核心词汇、时态与从句",
        "阅读与完形": "阅读理解与完形填空",
        "听力与口语": "听力技巧与情景交际",
        "写作": "书面表达与短文写作",
    },
}

#: 抽周期 / 页数（汉字数字支持到 99）
_CN_NUM = r"[一二两三四五六七八九十]{1,3}"
_WEEKS_RE = re.compile(rf"(\d{{1,3}}|{_CN_NUM})\s*(?:个)?\s*(?:星期|周)")
_DAYS_RE = re.compile(rf"(\d{{1,3}}|{_CN_NUM})\s*天")
_MONTHS_RE = re.compile(rf"(\d{{1,3}}|{_CN_NUM})\s*个?\s*月")
_YEARS_RE = re.compile(rf"(\d{{1,2}}|{_CN_NUM})\s*年")
_HALF_YEAR_RE = re.compile(r"半\s*年|半年")
#: 「今天/今晚/这周」是**期限**不是周期：期限到了就要一个能当天/本周跑完的最小排期。
_DEADLINE_RE = re.compile(r"今天|今日|今晚|今早|马上|立刻|立即|尽快|现在|明天|后天|这周|本周|本周内|本周末|这周末|周末")
_PAGES_RE = re.compile(r"(?:共\s*)?(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*页")
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

#: 契约边界（与模板 inputs: 一致）
WEEKS_MAX_EXCLUSIVE = 16
PAGES_MIN, PAGES_MAX = 8, 30
#: 周期口径：≤4 周算短期（突击/冲刺/考前集训），>4 周算长期。
#: 默认周期按周期类型分开给，不再是一个「10 周」了事。
SHORT_TERM_MAX_WEEKS = 4
SHORT_WEEKS_DEFAULT = 2
LONG_WEEKS_DEFAULT = 10
#: 课时预算的缓冲比例（与 teacher_planner 模板骨架的 buffer_ratio 对齐）。
#: 代码只拿它算「预算应该是多少」用于核对，不改模板。
BUFFER_RATIO = 0.15
DEFAULT_WEEKS = LONG_WEEKS_DEFAULT          # 兼容旧引用：长期兜底值
TERMS = ("长期", "短期")
DEFAULT_HOURS_PER_WEEK = 3
#: 每课时多少页 PPT。教学 PPT 的常见密度：一节 45 分钟课约 10~15 张，
#: 即 1 课时 4~5 页。取 4 作基准，宁少不多（页数一多必然注水）。
PAGES_PER_HOUR = 4.0
#: 推不出课时时的兜底（模板里 estimated_hours 的常见值）
HOURS_FALLBACK = 2.0


def cn_to_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        if head and head not in _CN:
            return None
        if tail and tail not in _CN:
            return None
        return ((_CN.get(head, 1) if head else 1) * 10
                + (_CN.get(tail, 0) if tail else 0))
    return _CN.get(s)


@dataclass
class FlowSpec:
    raw: str
    stage: str = "高中"
    stage_known: bool = True       # False = 没识别出学段，当前值只是默认候选
    subject: str = "通用"
    subject_known: bool = True      # False = 残句推断所得，必须让用户确认/修正
    level: str = "基础"
    module: str = ""
    goal: str = ""
    weeks: int = LONG_WEEKS_DEFAULT
    term: str = "长期"                # 长期 / 短期：决定默认周期、蓝图 mode 与提示
    term_source: str = "默认"          # 类型怎么来的：--term / 句子关键词 / 按周期推算
    weeks_source: str = "默认"         # 周期怎么来的：--weeks / 句子 / 周期类型默认 / 用户选择
    hours_per_week: int = DEFAULT_HOURS_PER_WEEK
    hours_source: str = "默认"         # 每周课时怎么来的：--hours-per-week / 追问采用默认
    pages: int | None = None          # None = 未指定，由 S4 按单元课时推导
    units_per_run: int = 2
    focus: str = ""                    # 原话里真正要学的东西（如「光合作用」），用于挑单元
    work: str = ""                     # 《作品名》——单篇教学对象
    single_text: bool = False          # 是否「就讲这一篇课文」
    effort: str = ""                   # 投入强度：轻 / 重 / 空
    intensity: str = ""                # 一次课 / 系统 / 单篇 / 空（模糊→问用户或估算）
    pages_source: str = ""             # 页数怎么来的：用户指定 / 一次课默认加长 / S4a 决策
    modules: list[dict] = field(default_factory=list)
    source: str = "内置表"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
def extract_stage(text: str) -> str | None:
    for stage, rx in _GRADE_HINTS.items():
        if rx.search(text):
            return stage
    return None


def guess_subject(text: str) -> tuple[str, bool]:
    """返回 ``(学科, 是否命中标准学科表)``。

    未命中时返回残句推断值，但 **known=False**：调用方必须据此让用户确认，
    不能静默采用——学科决定整门课往哪个知识域排章，猜错代价是整份方案作废。
    """
    for s in sorted(SUBJECTS, key=len, reverse=True):
        if s in text:
            return ("政治" if s == "思想政治" else s), True
    # 文学类：出现《作品名》/课文/小说/散文/诗歌 → 语文（候选，subject_known 仍 False）
    if _WORK_RE.search(text) or re.search(r"课文|小说|散文|诗歌|文言文|古诗|杂文|戏剧", text):
        return "语文", False
    # 表外学科（如「天文学」「建筑学」）：把学段与包装词剔掉后的残句当候选。
    hint = extract_course_hint(text)
    if 2 <= len(hint) <= 8 and hint not in STAGES:
        return hint, False
    return "通用", False


def extract_level(text: str) -> str:
    for kw, lv in sorted(LEVEL_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        if kw in text:
            return lv
    return "基础"


def extract_course_hint(text: str) -> str:
    """去掉学段/学科/包装词后的残句，作为「学什么」的线索（仅提示用）。"""
    t = text
    for rx in _GRADE_HINTS.values():
        t = rx.sub("", t)
    for s in sorted(SUBJECTS, key=len, reverse=True):
        t = t.replace(s, " ")
    t = re.sub(r"我想|想学|系统|学习|学完|备考|帮我|规划|计划|定制|课程|从零|零基础|"
               r"的|，|,|。|！|？|\s+", " ", t)
    return t.strip()


#: 短期/长期的关键词。只用来**判断任务类型**，具体周期仍由 --weeks / 句子 / 追问决定。
_SHORT_WORDS = re.compile(
    r"短期|突击|冲刺|速成|临考|考前|集中突破|集训|补习|恶补|速通|"
    r"几天内|一个月内|两周内|这学期前|周末突击")
_LONG_WORDS = re.compile(
    r"长期|系统|学完|完整学|全面|打基础|从零|零基础|一年|半年|整个学期|一学期|长期跟")


def extract_weeks(text: str) -> int | None:
    """从一句话里取周期（周）。支持「3周」「三周」「3个星期」「2个月」「半年」。"""
    m = _WEEKS_RE.search(text)
    if m:
        return cn_to_int(m.group(1))
    if _HALF_YEAR_RE.search(text):
        return 26
    m = _MONTHS_RE.search(text)
    if m:
        weeks = cn_to_int(m.group(1))
        return weeks * 4 if weeks else None
    m = _YEARS_RE.search(text)
    if m:
        years = cn_to_int(m.group(1))
        return years * 52 if years else None
    return None


def extract_days(text: str) -> int | None:
    m = _DAYS_RE.search(text)
    if not m:
        return None
    return cn_to_int(m.group(1))


_WORK_RE = re.compile(r"《([^》]{1,30})》")
#: 「简单学一下」这类**投入强度**信号：命中就按最小闭环排（1 周、1 个单元、课时封顶）
_LIGHT_RE = re.compile(r"简单|简略|粗略|大概|随便|了解一下|了解|知道一下|过一下|"
                       r"速览|速通|概览|浅尝|入门|应付|意思一下|看看就行|不深究")
_HEAVY_RE = re.compile(r"系统|深入|彻底|吃透|精通|全面|扎实|打基础|逐字|精讲|深挖")


def extract_work(text: str) -> str:
    """原话里的《作品/课文》名（单篇教学对象）。没有返回空串。"""
    m = _WORK_RE.search(text or "")
    return m.group(1).strip() if m else ""


#: 「一次课就解决」的信号：突击/速成/今天要/考前/简单了解一下…
_ONE_SESSION_RE = re.compile(
    r"突击|速成|速通|速览|临时|赶一赶|救急|今天|明天|后天|今晚|今早|马上|立刻|立即|尽快|"
    r"就要|要考|考前|一节|一节课|一堂课|简单|简略|粗略|大概|随便|了解一下|了解|知道一下|"
    r"过一下|应付|意思一下|看看就行|不深究|浅尝|概览|入门")
#: 「要系统铺开」的信号
_SYSTEM_RE = re.compile(r"系统|全面|深入|彻底|吃透|精通|扎实|打基础|逐字|精讲|深挖|长期")


def extract_intensity(text: str) -> str:
    """本次的投入强度：``一次课`` / ``系统`` / ``""``（说不清，需要问或估算）。"""
    t = text or ""
    if _ONE_SESSION_RE.search(t):
        return "一次课"
    if _SYSTEM_RE.search(t):
        return "系统"
    return ""


def extract_effort(text: str) -> str:
    """投入强度：``轻`` / ``重`` / ``""``（没表达）。"""
    if _LIGHT_RE.search(text or ""):
        return "轻"
    if _HEAVY_RE.search(text or ""):
        return "重"
    return ""


def is_single_text(text: str, work: str) -> bool:
    """是不是「就讲这一篇」：有《作品名》，或明确说课文/这一课/这一篇。"""
    return bool(work) or bool(re.search(r"课文|这一课|这篇|这一篇|一文|精读", text or ""))


def extract_deadline(text: str) -> str | None:
    """原话里的期限词（今天/这周…）。期限≠周期，命中就按 1 周排最小闭环。"""
    m = _DEADLINE_RE.search(text)
    return m.group(0) if m else None


#: 抽聚焦主题时要剥掉的壳（包装、学段、学科、期限、动作、虚词）
_FOCUS_NOISE = re.compile(
    r"我们|我想|想学|想要|需要|帮我|给我|安排|规划|计划|定制|系统|学习|学完|学会|掌握|我|"
    r"复习|预习|突击|冲刺|速成|集中|突破|强化|辅导|补课|看看|讲讲|讲一下|说一下|"
    r"今天|今日|今晚|今早|马上|立刻|立即|尽快|现在|明天|后天|这周|本周|周末|"
    r"零基础|入门|基础|初级|中等|进阶|竞赛|"
    r"小学|初中|高中|大学|考研|"
    r"[0-9一二两三四五六七八九十]+\s*(?:个)?\s*(?:周|星期|天|月|年)|"
    r"每周|课时|小时|分钟|"
    r"简单|简略|粗略|大概|随便|了解|知道|过一下|看看|读读|学一下|学一学|学点|"
    r"这篇|这一篇|那篇|课文|一篇|一文|文章|"
    r"一下|一点|一些|怎么|如何|怎样|是什么|为什么|"
    r"[\s，。,.;；:：、！!？?（）()《》\[\]\"'“”‘’]+")


def extract_focus(text: str, stage: str = "", subject: str = "",
                  module: str = "") -> str:
    """从原话里抽「这次到底要学什么」（如「光合作用」「文言文断句」）。

    为什么要它：用户说「突击光合作用」，而模块清单只能给到「生物圈中的绿色植物」
    这一层；不把聚焦词提出来，选单元就会按章序取第一个（种子萌发），
    交付的课件和用户要的完全不是一件事。
    """
    cleaned = _FOCUS_NOISE.sub(" ", text or "")
    for w in (stage, subject, module):
        if w:
            cleaned = cleaned.replace(w, " ")
    cleaned = re.sub(r"[\s]+", "", cleaned)
    # 两端残留的虚词/单字动词（「在的光合作用」「用文言文」→「光合作用」「文言文」）
    for _ in range(4):
        before = cleaned
        cleaned = cleaned.strip("的了着过吧呢啊呀哦嘛")
        cleaned = re.sub(r"^(?:在|用|把|给|让|对|和|与|为|是|学|练|看|做|搞|弄|讲|说|"
                         r"按|从|向|就|也|都|还|想|要|会|能|可以|备考|要考|考试|考|背|"
                         r"刷|读|写|听|复习|预习|我)+", "", cleaned)
        if cleaned == before:
            break
    cleaned = cleaned.strip("的了着过吧呢啊呀哦嘛")
    if len(cleaned) < 2 or len(cleaned) > 20:
        return ""
    return cleaned


def extract_term(text: str) -> str | None:
    """句话里有没有明说「长期 / 短期」。两词都有时按短期算（用户其实想快）。"""
    if _SHORT_WORDS.search(text):
        return "短期"
    if _LONG_WORDS.search(text):
        return "长期"
    return None


def term_for_weeks(weeks: int) -> str:
    """周期 → 任务类型：≤4 周算短期，其余长期。"""
    return "短期" if weeks <= SHORT_TERM_MAX_WEEKS else "长期"


def default_weeks_for(term: str) -> int:
    return SHORT_WEEKS_DEFAULT if term == "短期" else LONG_WEEKS_DEFAULT


def extract_pages(text: str) -> int | None:
    m = _PAGES_RE.search(text)
    if not m:
        return None
    return cn_to_int(m.group(1))


# ---------------------------------------------------------------------------
# 模块清单
# ---------------------------------------------------------------------------
def builtin_modules(stage: str, subject: str) -> list[dict]:
    table = MODULE_MAP.get((stage, subject)) or {}
    return [{"name": k, "goal": v} for k, v in table.items()]


def clamp_pages(n: int) -> int:
    return min(max(int(n), PAGES_MIN), PAGES_MAX)


def pages_for_unit(hours: float | None, requested: int | None = None) -> tuple[int, str]:
    """算一个单元该出多少页，并带上「这个数是怎么来的」。

    页数不是全局常数：2 课时的巩固课与 3 课时的新授课不该同一个页数。
    规则：``课时 × PAGES_PER_HOUR``，再夹到模板契约 8~30。
    用户/句子显式给了页数则照办（不算，只夹）。
    """
    if requested:
        return clamp_pages(requested), "指定值"
    h = float(hours) if hours else HOURS_FALLBACK
    raw = round(h * PAGES_PER_HOUR)
    n = clamp_pages(raw)
    basis = f"{h:g} 课时 × {PAGES_PER_HOUR:g} 页/课时 = {raw} 页"
    if n != raw:
        basis += f"（夹到模板契约 {PAGES_MIN}~{PAGES_MAX}）"
    return n, basis


def clamp_weeks(weeks: int, notes: list[str]) -> int:
    if weeks >= WEEKS_MAX_EXCLUSIVE:
        notes.append(f"总周期 {weeks} 周超出模板契约（< {WEEKS_MAX_EXCLUSIVE} 周），"
                     f"已按 {WEEKS_MAX_EXCLUSIVE - 1} 周为一个阶段规划；"
                     f"学不完的模块请再跑一次本链路")
        return WEEKS_MAX_EXCLUSIVE - 1
    if weeks <= 0:
        notes.append(f"总周期 {weeks} 不合法，按 1 周处理（请用 --weeks 指定）")
        return 1
    return weeks


def args_term(args) -> str | None:
    """``--term long|short`` → 长期/短期（也接受直接写中文）。"""
    v = getattr(args, "term", None)
    if not v:
        return None
    v = str(v).strip().lower()
    if v in ("short", "short_term", "短期"):
        return "短期"
    if v in ("long", "long_term", "长期"):
        return "长期"
    return None


def _weeks_from(args, text: str) -> tuple[int | None, str]:
    """返回 ``(周数, 来源)``；没给就是 ``(None, "")``。"""
    if getattr(args, "weeks", None):
        return int(args.weeks), "--weeks"
    if getattr(args, "days", None):
        return max(1, -(-int(args.days) // 7)), "--days"
    weeks = extract_weeks(text)
    if weeks:
        return weeks, "句子里的周期"
    days = extract_days(text)
    if days:
        return max(1, -(-days // 7)), "句子里的天数"   # 向上取整：10 天 → 2 周
    return None, ""


def build_spec(text: str, args) -> FlowSpec:
    """把一句话 + CLI 覆盖拼成 FlowSpec（不发请求、不选模块）。"""
    notes: list[str] = []
    if args.stage:
        stage, stage_known = args.stage, True
    else:
        hit = extract_stage(text)
        stage, stage_known = (hit, True) if hit else ("高中", False)

    subject, subject_known = (args.subject, True) if args.subject else guess_subject(text)
    level = args.level or extract_level(text)

    # ---- 周期与任务类型（长期 / 短期）----
    # 优先级：--weeks/--days > 句子里写的周期 > 周期类型的默认值。
    # 「没写周期」不再静默按 10 周，而是先判长期/短期，再取各自默认值，并记来源。
    forced = args_term(args)                          # --term 指定的类型（最高优先）
    said = extract_term(text)                        # 句子里明说的类型
    work = extract_work(text)
    single = is_single_text(text, work)
    effort = extract_effort(text)
    intensity = extract_intensity(text)
    if single and not intensity:
        intensity = "单篇"

    weeks, weeks_source = _weeks_from(args, text)
    deadline = extract_deadline(text)
    if weeks is None and forced:
        weeks = default_weeks_for(forced)          # --term 已定类型，用该类型默认
        weeks_source = "按--term默认"
    elif weeks is None and intensity == "一次课":
        # 「突击/简单了解一下/今天要」＝一次课解决，不排成几周
        weeks, weeks_source = 1, "一次课强度（突击/简单了解…，按 1 周最小闭环）"
    elif weeks is None and single:
        weeks, weeks_source = 1, "单篇课文（按 1 周最小闭环）"
    elif weeks is None and deadline:
        weeks, weeks_source = 1, f"近期期限「{deadline}」（按 1 周排）"
    elif weeks is None and effort == "轻":
        weeks, weeks_source = 1, "轻量意图（简单/了解一下，按 1 周排）"
    if weeks is None:
        term = forced or said or "长期"
        weeks = default_weeks_for(term)
        # 没让用户填周数：先给临时值占位，S0b 让模型读完模块规模后估算并覆盖。
        # 注意：句子里出现「系统/突击」只定类型，**不锁周数**——周数仍由模型估。
        weeks_source = ("按--term默认" if forced
                        else f"待模型估算（按{term}任务）")
    else:
        light = single or effort == "轻"
        term = forced or said or ("短期" if light else term_for_weeks(weeks))
        if not forced and term_for_weeks(weeks) != term:
            notes.append(f"周期 {weeks} 周与「{term}」说法不一致，以用户明说的 {term} 为准")
        elif forced and term_for_weeks(weeks) != term:
            notes.append(f"--term 指定「{term}」但周期 {weeks} 周通常算"
                         f"{term_for_weeks(weeks)}，按用户指定执行")
    weeks = clamp_weeks(weeks, notes)

    # 页数不设默认值：只有用户明说（--pages / 句子里写「12 页」）才固定，
    # 其余一律交给 S4a 按「一页一个教学动作」数出来（契约边界 8~30）。
    pages = args.pages or extract_pages(text)
    pages_source = "用户指定" if pages else ""
    if pages is not None and not (PAGES_MIN <= pages <= PAGES_MAX):
        pages = clamp_pages(pages)
        notes.append(f"页数越界，已夹到 {pages}（模板契约 {PAGES_MIN}~{PAGES_MAX}）")

    focus = work or extract_focus(text, stage, subject)
    if focus:
        notes.append(f"本次聚焦：{focus}" + ("（单篇课文）" if single else ""))
    if single:
        notes.append("范围：单篇课文（1 周 / 1 个单元，不做整模块铺开）")
    if effort:
        notes.append(f"投入强度：{effort}" + ("（课时按最小闭环封顶）" if effort == "轻" else ""))

    return FlowSpec(
        raw=text, stage=stage, stage_known=stage_known,
        focus=focus,
        work=work, single_text=single, effort=effort,
        intensity=intensity, pages_source=pages_source,
        subject=subject, subject_known=subject_known,
        level=level,
        weeks=weeks,
        term=term,
        term_source=("用户指定(--term)" if forced
                     else ("句子关键词" if said else "按周期推算")),
        weeks_source=weeks_source,
        # 周课时不写死：哪怕是一次课，也由 S0b 按「这一次课能讲完多少」来估。
        hours_per_week=args.hours_per_week or DEFAULT_HOURS_PER_WEEK,
        hours_source=("--hours-per-week" if getattr(args, "hours_per_week", None)
                      else "待模型估算"),
        pages=pages,
        units_per_run=getattr(args, "units", None) or 0,   # 0 = 自动
        notes=notes,
    )


def attach_modules(spec: FlowSpec, modules: list[dict], source: str) -> None:
    spec.modules = modules
    spec.source = source


#: 强度菜单：(键, 说明, 应用函数所需参数)
INTENSITY_MENU = [
    ("一节课解决", "1 周（课时由模型按一节课容量估，篇幅由内容决定）"),
    ("一周铺开", "1 周 / 3 课时（一周内分几次讲完）"),
    ("系统学", "周期交给模型估算（按模块规模排 2~15 周）"),
]


def _needs_intensity(spec: FlowSpec) -> bool:
    """强度是否还没定：没有关键词、也没写周期。"""
    return (not spec.intensity
            and not spec.weeks_source.startswith(("--weeks", "--days", "句子", "近期期限")))


def ask_intensity(spec: FlowSpec, interactive: bool) -> None:
    """关键词模糊时问一句：这次要按「一节课 / 一周 / 系统」哪种强度排。"""
    if not _needs_intensity(spec):
        return
    if not interactive:
        spec.notes.append("强度未明说：交模型估算周期（可用 --weeks / --units / --pages 指定）")
        return
    print()
    print("⚠️ 看不出这次要「一次课解决」还是「系统学」。")
    for i, (name, desc) in enumerate(INTENSITY_MENU, 1):
        print(f"   {i}. {name} —— {desc}")
    try:
        raw = input(f"   请选择 [1-{len(INTENSITY_MENU)}，回车=3 系统学]：").strip()
    except EOFError:
        return
    idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(INTENSITY_MENU) else 2
    name = INTENSITY_MENU[idx][0]
    if name == "一节课解决":
        spec.intensity, spec.weeks, spec.hours_per_week = "一次课", 1, 2
        spec.weeks_source, spec.hours_source = "用户选择：一次课", "待模型估算"
        spec.term, spec.term_source = "短期", "用户选择"
    elif name == "一周铺开":
        spec.intensity, spec.weeks, spec.hours_per_week = "一周", 1, 3
        spec.weeks_source, spec.hours_source = "用户选择：一周", "待模型估算"
        spec.term, spec.term_source = "短期", "用户选择"
    else:
        spec.intensity = "系统"
    spec.notes.append(f"强度由用户选择：{name}")


def needs_estimate(spec: FlowSpec) -> bool:
    """周期或周课时是否还等模型给（用户没明说、也没 --term 硬定）。"""
    return ("待模型估算" in spec.weeks_source
            or "待模型估算" in spec.hours_source)


def apply_estimate(spec: FlowSpec, weeks: int | None, hours: int | None,
                   basis: str, forced_term: str | None = None) -> None:
    """把估算结果落回 FlowSpec（只覆盖「待模型估算」的字段）。"""
    notes: list[str] = []
    if "待模型估算" in spec.weeks_source and weeks:
        spec.weeks = clamp_weeks(int(weeks), notes)
        spec.weeks_source = "模型估算"
        # 用户明说过类型（--term 或「突击/系统」）就尊重它，只估算周数
        if not forced_term and spec.term_source not in ("用户指定(--term)", "句子关键词"):
            spec.term = term_for_weeks(spec.weeks)
            spec.term_source = "按周期推算"
    if "待模型估算" in spec.hours_source and hours:
        h = max(1, min(int(hours), 24))
        # 一次课只设「上限保护」（一节课塞不进 5 课时），不是默认值：
        # 具体几课时仍由模型按内容估。
        if spec.intensity in ("一次课", "单篇") and h > 4:
            notes.append(f"一次课课时上限保护：{h} → 4（模型估得超过一节课容量）")
            h = 4
        spec.hours_per_week = h
        spec.hours_source = "模型估算"
    spec.notes.extend(notes)
    if basis:
        spec.notes.append(f"周期估算依据：{basis}")


def budget_hours(spec: FlowSpec) -> float:
    """按口径算净课时预算：周 × 课时/周 × (1 - buffer)，向下取整到 0.5。"""
    raw = spec.weeks * spec.hours_per_week * (1 - BUFFER_RATIO)
    return int(raw * 2) / 2


def _ask_stage(spec: FlowSpec) -> bool:
    """交互式让用户选学段。返回是否已确定。"""
    print()
    print("⚠️ 没识别出学段。")
    print("   " + "  ".join(f"{i}. {s}" for i, s in enumerate(STAGES, 1)))
    for _ in range(3):
        try:
            raw = input(f"   请选择学段 [1-{len(STAGES)}，回车默认 3(高中)]：").strip()
        except EOFError:
            return False
        if not raw:
            spec.notes.append("学段未识别，用户确认采用默认「高中」")
            spec.stage_known = True
            return True
        if raw in STAGES:
            spec.stage = raw
            spec.stage_known = True
            spec.notes.append(f"学段由用户选择：{raw}")
            return True
        if raw.isdigit() and 1 <= int(raw) <= len(STAGES):
            spec.stage = STAGES[int(raw) - 1]
            spec.stage_known = True
            spec.notes.append(f"学段由用户选择：{spec.stage}")
            return True
        print(f"   ✗ 没这个选项：{raw!r}（输数字或学段名）")
    return False


def _ask_subject(spec: FlowSpec) -> bool:
    """交互式让用户确认/修正学科。返回是否已确定。"""
    detected = spec.subject
    print()
    print("⚠️ 未在标准学科表里匹配到学科。")
    if detected == "通用":
        print("   也没能从这句话里推断出学科名。")
    else:
        print(f"   推断结果：{detected}（仅供参考，可能是句中的残词）")
    try:
        raw = input("   回车采用该值；或直接输入正确学科名（如 天文学 / 物理学）：").strip()
    except EOFError:
        return False
    if raw:
        spec.notes.append(f"学科由用户修正：{detected} → {raw}")
        spec.subject = raw
    else:
        spec.notes.append(f"学科「{detected}」为残句推断，用户确认采用")
    spec.subject_known = True
    return True


def resolve_identity(spec: FlowSpec, interactive: bool = True,
                     accept_detected: bool = False) -> None:
    """学段 / 学科未确定时，让用户亲自确认或修正。

    为什么这两项不做静默兜底：
      · 学段决定模块清单与难度基线（小学/初中/高中的同一学科是完全不同的课）；
      · 学科进的是 teacher_planner 的「你是一名{{学科}}教研组长」，
        错了整门课的章节方向就错了——而后面四个步骤全部据此渲染。
    猜错的代价是几分钟与一整份废稿，所以宁可停下来问一句。

    交互式：逐项询问；非交互（管道/CI）：一次报全，要求 --stage / --subject
    或 --accept-detected。
    """
    problems: list[str] = []
    NL = chr(10)

    # ---------------- 学段 ----------------
    if not spec.stage_known:
        if accept_detected:
            spec.notes.append(f"学段未识别，已用 --accept-detected 采用默认「{spec.stage}」")
            spec.stage_known = True
        elif interactive and sys.stdin.isatty():
            if not _ask_stage(spec):
                problems.append(f"学段：没识别出 → 用 --stage 指定（可选 {'/'.join(STAGES)}）")
        else:
            problems.append(f"学段：没识别出 → 用 --stage 指定（可选 {'/'.join(STAGES)}）")

    # ---------------- 学科 ----------------
    if not spec.subject_known:
        if accept_detected:
            spec.notes.append(f"学科「{spec.subject}」为残句推断，已用 --accept-detected 接受")
            spec.subject_known = True
        elif interactive and sys.stdin.isatty():
            if not _ask_subject(spec):
                problems.append(f"学科：未命中标准学科表（推断为「{spec.subject}」，仅供参考）"
                                f" → 用 --subject 指定")
        else:
            problems.append(f"学科：未命中标准学科表（推断为「{spec.subject}」，仅供参考）"
                            f" → 用 --subject 指定")

    if problems:
        msg = "未能确定「学段/学科」，链路不对这两项做静默猜测："
        for p in problems:
            msg += NL + "   · " + p
        msg += NL + "   确认当前值即可继续：--accept-detected"
        raise SystemExit(msg)


#: 选模块时要从原句里剔掉的噪音（包装词/周期/语气），避免干扰关键词匹配
_MODULE_NOISE = re.compile(
    r"我想|想学|想要|希望|帮我|给我|安排|规划|计划|定制|系统|学习|学完|学会|掌握|"
    r"提高|提升|补齐|过一遍|突击|冲刺|速成|集中|突破|强化|辅导|补课|课程|"
    r"零基础|入门|基础|初级|中等|进阶|竞赛|"
    r"小学|初中|高中|大学|考研|"
    r"[0-9一二两三四五六七八九十]+\s*(?:个)?\s*(?:周|星期|天|月|年)|"
    r"每周|课时|小时|分钟|页|"
    r"[\s，。,.;；:：、！!？?（）()《》\[\]\"'“”‘’]+")

#: 模块名的常见别名（内置表里的名字不一定等于用户口语）
_MODULE_ALIASES = {
    "古诗文阅读": ["文言文", "文言", "古文", "古诗", "诗词", "诗歌鉴赏", "实词", "虚词",
                   "断句", "翻译"],
    "现代文阅读": ["现代文", "散文", "小说", "论述类", "阅读题", "阅读理解"],
    "语言文字运用": ["语用", "病句", "成语", "词语", "修辞", "衔接", "标点"],
    "写作": ["作文", "议论文", "记叙文", "审题", "立意", "论证"],
    "文言文阅读": ["文言文", "文言", "古文", "实词", "虚词", "翻译"],
}


def _grams(text: str) -> set[str]:
    """把中文串切成 2 字窗口（不跨标点），用作轻量关键词匹配。"""
    out: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def guess_module(spec: FlowSpec, text: str) -> tuple[str | None, str, int, bool]:
    """从用户原话里猜模块（猜不出返回 ``(None, "")``）。

    为什么需要：``--no-input`` 以前直接取清单里的**第 1 个**，于是
    「我想突击文言文」会被排成「现代文阅读」的散文课。这里先按原句匹配，
    匹配不到才走菜单/第 1 个兜底。
    """
    cleaned = _MODULE_NOISE.sub(" ", text or "")
    for word in (spec.stage, spec.subject):          # 学段/学科不参与模块判断
        if word:
            cleaned = cleaned.replace(word, " ")
    grams = _grams(cleaned)
    if not grams:
        return None, "", 0, False

    scored: list[tuple[int, str, list[str]]] = []
    for m in spec.modules:
        name, goal = m["name"], m.get("goal", "")
        hay = name + " " + goal
        hits = [g for g in grams if g in hay]
        hits += [a for a in _MODULE_ALIASES.get(name, []) if a in cleaned]
        scored.append((len(set(hits)), name, sorted(set(hits))))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 1:
        return None, "", 0, False
    score, best, hits = scored[0]
    tied = len(scored) > 1 and scored[1][0] == score
    why = "命中 " + "、".join(hits[:5]) + ("（与其它模块同分）" if tied else "")
    return best, why, score, tied


def choose_module(spec: FlowSpec, requested: str | None,
                  interactive: bool = True, text: str = "") -> str:
    """从模块清单里选定一个。``requested`` 支持名字子串匹配。

     优先级：``--module`` > 交互菜单 > 清单第 1 个（记 notes）。
    「原句说了学什么」由 ``s0_scope`` 先做关键词/模型判定（见 ``guess_module``）。
    """
    names = [m["name"] for m in spec.modules]
    if not names:
        raise ValueError("模块清单为空，无法选择")

    if requested:
        hit = [n for n in names if requested in n or n in requested]
        if not hit:
            raise ValueError(f"没有模块 {requested!r}；可选：{names}")
        spec.module = hit[0]
        spec.goal = next(m.get("goal", "") for m in spec.modules if m["name"] == hit[0])
        return spec.module

    if interactive and sys.stdin.isatty():
        print("\n可选学习模块：")
        for i, m in enumerate(spec.modules, 1):
            print(f"  {i}. {m['name']} —— {m.get('goal', '')}")
        try:
            raw = input(f"请选择模块 [1-{len(names)}，回车默认 1]：").strip()
            idx = int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(names) else 0
        except EOFError:                  # 管道/被重定向时没有输入源，不兜住会崩
            idx = 0
            spec.notes.append(f"交互被中断，默认选第 1 个模块「{names[0]}」")
    else:
        idx = 0
        if len(names) > 1:
            spec.notes.append(f"原话未指向具体模块，默认选第 1 个「{names[0]}」"
                              f"（可用 --module 指定其它）")

    spec.module = names[idx]
    spec.goal = next(m.get("goal", "") for m in spec.modules if m["name"] == names[idx])
    return spec.module


def planner_variables(spec: FlowSpec) -> dict:
    """S1 的 5 个变量（与 teacher_planner 的 inputs: 契约一一对应）。"""
    return {
        "学科": spec.subject,
        "学习目标": spec.goal or f"系统掌握{spec.module}",
        "总周期": f"{spec.weeks}周",
        "每周可投入时间": f"{spec.hours_per_week}课时",
        "学生水平": spec.level,
    }
