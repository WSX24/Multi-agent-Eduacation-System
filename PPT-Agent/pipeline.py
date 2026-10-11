#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PPT 生成链路 —— 一句话 → 提示词模板 → LLM 规划 → PPT-Agent → PPTX。

入口与 agent.py **同形**，只是把中间两步接上了：

    # 平常的调法（直接交给 agent，agent 自己临场发挥）
    python agent.py --profile quick "做一份 8 页的《向量数据库入门》PPT"

    # 本链路（同一句话，但中间先过提示词模板，再交给 LLM 产出大纲，最后才交给 agent）
    python pipeline.py "做一份 8 页的《向量数据库入门》PPT"

    ┌─────────────┐  ┌─────────────┐  ┌──────────┐  ┌────────────┐  ┌──────────┐
    │ 一句话       │→│ S0 槽位抽取 │→│ S1 模板   │→│ S2 LLM 规划 │→│ S3 落盘   │
    │ (同 agent.py)│  │ 模板的字段   │  │ 渲染提示词 │  │ + 闸门+阶梯  │  │ + 交接    │
    └─────────────┘  └─────────────┘  └──────────┘  └────────────┘  └──────────┘
                                                                          ↓
                                                        S4 agent.py --task-file → PPTX

## 角色档案（ROLE_PROFILES）

链路本身与模板解耦：每个模板一份 ``RoleProfile``，写清它的**输入字段、取值域、
默认值、用哪个校验器、哪几个字段参与校验**。换模板 = 换 ``ROLE`` 一行常量，
S0/S1/S2/S3/S4 全跟着走。

    teacher_ppt  课件（教学）  学科 / 课件主题 / 学生水平 / 页数要求 / 单元内容
    ppt_general  通用 PPT      主题 / 受众 / 场景 / 页数要求 / 核心重点 / 边界 / 风格偏好

两个模板**不是**同一个东西换名字，差异是硬性的（见 PROFILE 定义处的注释）。

## 为什么要分 S0 这一步（关键）

``agent.py`` 收的是**一句话**；而模板的 inputs 契约是若干**结构化字段**，
其中有的还是 enum，取值域直接决定提示词里「条件分支指令」走哪一支。两者之间
必须有一次槽位抽取。抽取分工遵循本仓库一贯原则：
**确定性的交给代码，概率性的才给模型。**

    ① 规则抽取（0 成本、可解释、可复现）
       《》→ 主题   「关于 X 的」→ 主题   N 页/十页 → 页数
       enum 词与同义词 → enum 字段   学科名 → 学科   重点/不展开 → 重点/边界
    ② 规则完全抽不到「主题」（唯一必填项）时，才花一次 LLM 兜底（结果仍由代码复校）
    ③ 显式 CLI flag 永远覆盖前两者
    ④ 「单元内容」这类**只能来自上游数据**的字段，永不从句子抽，只认 --unit-file

每个字段的来源都会打印出来并写进报告——验收时能看到「这句话被理解成了什么」。
规则抽不到主题就**直接拒绝**，不自拟：模板把主题钉死为不可改写项。

## 无人值守是这个模块的第一设计目标

链路上**不存在任何等待人工的停点**。三处最容易变成「挂着等人」的地方：

  ① **规划闸门不过** → 不是停下来让人改，而是走**自动升级阶梯**，最后走
     **降级交接**（把缺陷清单转成给执行体的中文指令，一起交给 PPT-Agent）。
     理由：PPT-Agent 的 P2 还有一道 validate 硬门，它能吃掉一部分上游缺陷；
     而整条链路停住等一个人，代价是整个任务不作数。
  ② **agent.py 读 stdin** → 子进程 ``stdin=DEVNULL``。agent.py 有 3 处 input()
     （命令确认 / BLOCKING 门 / 缺任务时提问），任何一个在无人环境下都会永久挂起；
     DEVNULL 让它们变成可捕获的 EOFError，回到模型的观察里，而不是死等。
  ③ **API 抖动** → 规划阶段对网络异常也重试（不是只有校验失败才重试）。

## 升级阶梯（plan_ppt）

    第 1 轮  temperature=0.7   常规生成
    第 2 轮  temperature≈0.4   回投缺陷 + 降温
    第 3 轮  temperature≈0.2   回投缺陷 + 再降温
    ...      逐轮降温至 0      格式类缺陷主要是采样噪声，降温比「原样重掷」有效
    收尾      全轮不过 → 取**历轮里缺陷最少的那一份**做降级交接

  取「缺陷最少的一份」而不是「最后一份」：最后一轮温度最低但可能恰好更差。

## 判定与退出码

    status=ok                  规划全部通过，PPTX 产出
    status=degraded            规划带缺陷降级交接，PPTX 仍产出（上游可据此决定要不要复跑）
    status=planned[_degraded]  只跑到 S3（--plan-only）
    status=explained           只跑到 S1（--explain），用于验收「润色后的提示词」
    status=failed              输入不合法 / 规划全轮 API 失败 / PPT-Agent 未出片

    退出码：ok / degraded / planned* / explained 都是 0；failed 非 0。
    每次运行都写 ``runs/report_<stamp>.json``，最后一行打 ``RUN_REPORT {...}``，
    供上游自动化直接消费。

用法::

    # 一条龙（当前默认角色 = teacher_ppt 课件）
    python pipeline.py "做一份 8 页的《质量守恒定律》课件"
    python pipeline.py "初中化学 8 页课件，主题《质量守恒定律》，学生水平基础"
    python pipeline.py --unit-file unit.md "做一份 10 页的《光合作用》课件"

    # 验收：只看 S0 解析结果 + S1 润色后的完整提示词（不调 LLM、不产片）
    python pipeline.py "做一份 8 页的《质量守恒定律》课件" --explain

    # 换角色（ppt_general 的额外字段用 --set 传）
    python pipeline.py "做一份 12 页的《年度复盘》PPT" --role ppt_general --set 场景=汇报 --set 受众=老板

    # 只做规划 / 续跑底稿 / 离线自检
    python pipeline.py "..." --plan-only
    python pipeline.py --from-plan runs/plan_20261007_142835.md
    python pipeline.py --check
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# —— 控制台编码统一（与 agent.py 一致，Windows 下必须）——
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

import click
import yaml

# ===========================================================================
# 路径：本文件在 PPT-Agent/ 下，提示词模板库是它的兄弟目录
# ===========================================================================
HERE = Path(__file__).resolve().parent
PROMPT_LIB_DIR = HERE.parent / "PromptTemplate"

if str(PROMPT_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(PROMPT_LIB_DIR))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from prompt_builder import PromptLib                      # noqa: E402
from quality_gate import require_inputs                   # noqa: E402
from eval_generic import (PPT_SECTIONS, validate_ppt,     # noqa: E402
                          validate_ppt_general)
from unit_bridge import unit_output_to_ppt_content        # noqa: E402

# agent.py 的 .env 加载器：DEEPSEEK_API_KEY / PPT_AGENT_PROVIDER 都在里面
from agent import get_python_command, load_env_files, resolve_skill_dir  # noqa: E402
# staged.py 的旋钮常量：单点定义，避免两边字符串各写一份而迹移
from staged import (MAX_PAGE_REPAIRS_ENV, REASONING_CHOICES,  # noqa: E402
                    STAGE_REASONING_ENV)

#: agent.py 的执行模式与 profile。
#: **这两个不是独立旋钮**——staged 引擎跑的是 Default 路由的骨架（project_manager /
#: design_spec / spec_lock），而 quick 路由的协议明文写着「不产出 spec/lock」
#: （workflows/routing.md："Once selected, Quick is the complete runtime and never
#: loads generate-pptx.md"；quick-generate.md："No root design_spec.md, spec_lock.md"）。
#: 所以 staged + quick 是**自相矛盾**的组合：模型被告诉「不需要 spec_lock」，
#: 引擎却强求它并通过 project_manager validate——表现为「反复修同一个错误」。
AGENT_MODES = ("staged", "react")
AGENT_PROFILES = ("quick", "default")
AGENT_PAIRING = {"staged": "default", "react": "quick"}


def resolve_agent_pair(mode: str, profile: Optional[str], extra_args=()) -> tuple:
    """定下 (mode, profile) 并拦住矛盾组合。返回 ``(mode, profile, note)``。

    三条规矩：
      ① 没显式给 profile → 按 mode 自动配对（staged→default，react→quick），不报错；
      ② 显式给了矛盾组合（staged + quick）→ **直接拒绝**，而不是静默跑一半再炸；
      ③ ``--agent-args`` 里再传 --mode/--profile 会与这里的参数打架，也拒绝。
    """
    joined = " ".join(extra_args)
    for flag in ("--mode", "--profile"):
        if flag in joined:
            raise ValueError(
                f"--agent-args 里不能再传 {flag}：模式与 profile 请用 "
                f"pipeline 的 --agent-mode / --agent-profile 指定，"
                f"否则两边会打架。")
    note = ""
    if profile is None:
        profile = AGENT_PAIRING[mode]
        note = f"按 mode={mode} 自动配对 profile={profile}"
    if mode == "staged" and profile == "quick":
        raise ValueError(
            "--agent-mode staged 不能配 --agent-profile quick：\n"
            "  staged 引擎跑的是 Default 路由的骨架（project_manager init →\n"
            "  scaffold-spec → scaffold-lock → validate），必须有 design_spec.md / spec_lock.md；\n"
            "  而 quick 路由的协议明写「不产出 spec/lock」（quick-generate.md:25），\n"
            "  预载的 quick-generate.md 里连 baseline/(user) 规则都没有（0 命中），\n"
            "  模型只能瞎猜 → 表现为「反复修同一个 validate 错误」。\n"
            "  选一个：\n"
            "    · python pipeline.py \"...\"                            # staged + default（默认）\n"
            "    · python pipeline.py \"...\" --agent-mode react           # react + quick")
    return mode, profile, note


RUNS_DIR = HERE / "runs"
SENTINEL = "无"

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# 升级阶梯：逐轮降温。格式类缺陷（模块改名 / 页号断 / 漏配图）主要是采样噪声，
# 降温比「同样的温度再掷一次」收敛得快；最后一档 0.0 是确定性输出。
TEMPERATURE_LADDER = (1.0, 0.55, 0.28, 0.0)
BACKOFF_SECONDS = (0.0, 3.0, 8.0, 15.0)

PAGES_MIN, PAGES_MAX = 8, 30


# ===========================================================================
# 取值域与同义词
# ===========================================================================
# —— teacher_ppt：学生水平 5 档（取值域必须与模板「条件分支指令」的档位一一对应）——
LEVELS = ("入门", "基础", "中等", "进阶", "竞赛")
LEVEL_SYNONYMS = {
    # 长词优先匹配，所以「零基础」必须排在「基础」前面（_match_enum 按长度降序）
    "零基础": "入门", "没有任何基础": "入门", "完全不懂": "入门",
    "初学者": "基础", "新手": "基础", "小白": "基础",
    "中级": "中等", "普通": "中等", "一般": "中等",
    "提高": "进阶", "强化": "进阶", "拔高": "进阶",
    "奥赛": "竞赛", "竞赛班": "竞赛", "尖子": "竞赛",
}

# —— ppt_general：受众 / 场景 ——
AUDIENCES = ("老板", "客户", "同事", "学生", "投资人", "评审专家")
SCENARIOS = ("汇报", "路演", "教学", "答辩", "产品介绍")
AUDIENCE_SYNONYMS = {
    "领导": "老板", "老总": "老板", "高管": "老板", "管理层": "老板", "上级": "老板",
    "甲方": "客户", "用户": "客户", "客户方": "客户", "业主": "客户",
    "团队": "同事", "组内": "同事", "伙伴": "同事",
    "学员": "学生", "同学": "学生", "初学者": "学生", "新手": "学生", "小白": "学生",
    "投资": "投资人", "资本": "投资人", "投行": "投资人",
    "专家": "评审专家", "评委": "评审专家", "答辩委员": "评审专家",
}
SCENARIO_SYNONYMS = {
    "述职": "汇报", "周报": "汇报", "月报": "汇报", "总结会": "汇报", "复盘": "汇报",
    "融资": "路演", "招商": "路演", "推介": "路演",
    # 教学（原「培训」改名）：培训/授课等旧说法全部归到它，保证旧句不失效
    "培训": "教学", "授课": "教学", "讲课": "教学", "课程": "教学", "上课": "教学",
    "讲解": "教学", "内训": "教学", "分享会": "教学",
    "开题": "答辩", "中期检查": "答辩", "结题": "答辩", "毕业设计": "答辩",
    "产品发布": "产品介绍", "新品": "产品介绍", "发布会": "产品介绍",
}

# —— 学科：teacher_ppt 的「学科」字段。允许「学段+学科」拼成「初中化学」——
SUBJECTS = ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理",
            "思想政治", "道德与法治", "信息技术", "通用技术", "计算机", "科学",
            "音乐", "美术", "体育", "编程")
_STAGES = ("小学", "初中", "高中", "大学", "职高", "中专", "中职")
_SUBJECT_RE = re.compile(
    r"(?:(%s)\s*)?" % "|".join(_STAGES)
    + r"(%s)" % "|".join(sorted(SUBJECTS, key=len, reverse=True))
)


# ===========================================================================
# 角色档案
# ===========================================================================
@dataclass(frozen=True)
class RoleProfile:
    """一个模板在链路里的全部接口面。

    ``fields`` 的**顺序**就是回显表与落盘的顺序；``extras`` 是角色专属的规则抽取器
    （签名 ``(text, spans) -> (values, sources, spans)``），因为「受众/场景」和
    「学科/学生水平」这类字段没法用同一套正则抽。
    """
    role: str
    label: str
    topic_field: str
    fields: tuple
    enums: dict                       # 字段 -> (取值元组, 同义词表)
    defaults: dict
    case_fields: tuple                # 传给校验器的字段（只放能硬验的）
    validator: Callable
    sections: tuple
    skeleton: str                     # 人读的结构骨架说明
    locked: tuple = ()                # 只能来自上游数据、永不从句子抽的字段
    extras: Callable = None           # 角色专属抽取器


def _extract_subject(text: str, spans: list) -> tuple:
    """teacher_ppt 专属：从句子里认出「学科」（含「初中化学」这种学段+学科写法）。"""
    m = _SUBJECT_RE.search(text)
    if not m:
        return {}, {}, spans
    return ({"学科": (m.group(1) or "") + m.group(2)},
            {"学科": f"规则:{m.group(0).strip()}"},
            spans + [m.span()])


def _extract_focus_boundary_style(text: str, spans: list) -> tuple:
    """ppt_general 专属：核心重点 / 边界 / 风格偏好。"""
    values, sources = {}, {}
    m = _FOCUS_RE.search(text)
    if m:
        got = _LEAD_VERB_RE.sub("", m.group(1).strip())
        if len(got) >= 2:
            values["核心重点"], sources["核心重点"] = got, "规则:重点XX"
            spans = spans + [m.span()]
    m = _BOUNDARY_RE.search(text)
    if m:
        got = _LEAD_VERB_RE.sub("", m.group(1).strip())
        if len(got) >= 2:
            values["边界"], sources["边界"] = got, f"规则:{m.group(0)[:4]}"
            spans = spans + [m.span()]
    for keywords, canonical in STYLE_KEYWORDS:
        found = next((kw for kw in keywords if kw in text), None)
        if found:
            i = text.find(found)
            values["风格偏好"], sources["风格偏好"] = canonical, f"规则:{found}"
            spans = spans + [(i, i + len(found))]
            break
    return values, sources, spans


ROLE_PROFILES: dict = {
    # ── 课件（教学）：仓库既有的 teacher_ppt，本链路的当前默认 ──────────────
    # 与 ppt_general 的差异是硬性的，不是改个名：
    #   首模块        【课件主题】      vs 【主题与受众】
    #   校验器        validate_ppt      vs validate_ppt_general
    #   结构骨架      固定「导入→展开→小结」（无场景分支）
    #   分档维度      学生水平 5 档     vs 受众身份 6 种
    #   旁白          100-250 字讲稿（配音用，无上限校验）vs 80-200 字演讲提示
    #   专项字段      学科 / 单元内容    vs 核心重点 / 边界 / 风格偏好
    "teacher_ppt": RoleProfile(
        role="teacher_ppt",
        label="课件（教学）",
        topic_field="课件主题",
        fields=("学科", "课件主题", "学生水平", "页数要求", "单元内容"),
        enums={"学生水平": (LEVELS, LEVEL_SYNONYMS)},
        defaults={"学科": "通用", "学生水平": "基础", "页数要求": 10,
                  "单元内容": SENTINEL},
        # 学生水平必须进 case：它就是「条件分支指令」的档位，
        # 不传进去则 validate_ppt 的档位落地断言（入门≤２条 / 中等带「可能的提问」/
        # 进阶含变式 / 竞赛含挑战页）全部不会跑，模板那段档位差异就又变成纯提示词。
        case_fields=("课件主题", "页数要求", "学生水平"),
        validator=validate_ppt,
        sections=tuple(PPT_SECTIONS),
        skeleton="导入 → 展开 → 小结（固定，不随输入变）",
        # 单元内容只能来自上游 teacher_unit 的渲染结果；从一句自然语言里
        # 「抽」出单元内容只可能是编造，而模板纪律 8 明令禁止编造。
        locked=("单元内容",),
        extras=_extract_subject,
    ),
    # ── 通用 PPT：汇报 / 路演 / 教学 / 答辩 / 产品介绍 ──────────────────────
    "ppt_general": RoleProfile(
        role="ppt_general",
        label="通用PPT",
        topic_field="主题",
        fields=("主题", "受众", "场景", "页数要求", "核心重点", "边界", "风格偏好"),
        enums={"受众": (AUDIENCES, AUDIENCE_SYNONYMS),
               "场景": (SCENARIOS, SCENARIO_SYNONYMS)},
        defaults={"受众": "学生", "场景": "教学", "页数要求": 10,
                  "核心重点": SENTINEL, "边界": SENTINEL, "风格偏好": SENTINEL},
        case_fields=("主题", "受众", "场景", "页数要求"),
        validator=validate_ppt_general,
        sections=("主题与受众", "整体结构", "逐页大纲", "视觉建议"),
        skeleton="汇报 现状→问题→原因→方案→计划 / 路演 痛点→产品→优势→数据→愿景 / "
                 "教学 概念→原理步骤→示例→练习→小结 / 答辩 背景→方法→结果→讨论→结论 / "
                 "产品介绍 使用场景→痛点→功能→优势→价格",
        extras=_extract_focus_boundary_style,
    ),
}

# ⚙️ 当前链路使用的模板。换回通用 PPT 就把这里改成 "ppt_general"（或用 --role）。
ROLE = "teacher_ppt"


# ===========================================================================
# 抽取用正则
# ===========================================================================
# 主题抽取的优先级：书名号/引号 → 关于X的 → 裸句残差 → LLM 兜底
_TOPIC_BRACKETS = (re.compile(r"《([^》]{1,60})》"),
                   re.compile(r"「([^」]{1,60})」"),
                   re.compile(r"[“\"]([^”\"]{1,60})[”\"]"))
_TOPIC_HINTS = (re.compile(r"关于\s*([^，,。；;：:\n]{2,40}?)\s*的"),
                re.compile(r"围绕\s*([^，,。；;：:\n]{2,40}?)\s*的"),
                re.compile(r"主题(?:是|为|：|:)\s*([^，,。；;：:\n]{2,40})"))
_PAGES_RE = re.compile(r"(?:共\s*)?(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*页")
_FOCUS_RE = re.compile(r"(?:核心重点|重点|聚焦|突出|强调)[：:是为]?\s*([^。；;，,\n]{2,60})")
_BOUNDARY_RE = re.compile(r"(?:边界|不展开|不涉及|不要讲|不讲|不包含|跳过)[：:]?\s*([^。；;，,\n]{2,60})")
# 重点/边界 抓到的短语常带一个动词头（“重点**讲**检索和生成”），剔掉
_LEAD_VERB_RE = re.compile(r"^(?:讲解|讲清|讲|说|写|是|为|要|做|谈|聊聊)\s*")

STYLE_KEYWORDS = (
    (("科技感", "科技", "未来感", "未来", "赛博", "极客", "数字化"), "科技感"),
    (("学术", "严谨", "规范", "论文风"), "学术严谨"),
    (("创意", "活泼", "有趣", "生动", "轻松"), "创意活泼"),
    (("商务", "简洁", "简约", "干练"), "商务简洁"),
)

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}

# 裸句兜底时从两端剥的包装词（不动句子中间，免得“我的世界”这类主题被吃掉）
_LEAD_WRAP_RE = re.compile(
    r"^(?:\s*(?:帮我|麻烦|辛苦|请您|请|需要|我要|我想|给我|关于|基于|围绕|针对|"
    r"帮我做|做|生成|制作|创建|出|来|弄|搞|写|整理|一份|一个|一套|一页|的|了|给)\s*)+")
_TAIL_WRAP_RE = re.compile(
    r"(?:\s*(?:PPT|ppt|Ppt|课件|幻灯片|演示文稿|演示|讲稿|讲义|的|了|吧|呢|啊)\s*)+$")

# 范文【示例输入】里的字段标签（自检解析用；只认已知标签，别用「下一行带冒号」切段）
def _example_label_re(profile: RoleProfile) -> re.Pattern:
    return re.compile(r"^(%s)：" % "|".join(profile.fields), re.M)


# ===========================================================================
# S0 · 一句话 → 模板的字段
# ===========================================================================
@dataclass
class ParsedInputs:
    """一句自然语言解析出来的字段 + 每个字段的来源。"""
    values: dict
    sources: dict
    warnings: list = field(default_factory=list)

    def table(self, fields: tuple) -> str:
        # 按**显示宽度**对齐：汉字占 2 格，用 len() 补空格会歪。
        # 值要截断：「单元内容」是一整篇教学材料，原样打出会把控制台刷屏。
        lines = [f"   {_pad(k, 12)}= {_pad(_clip_disp(str(self.values.get(k, '（未解析）')), 34), 36)}"
                 f"← {self.sources.get(k, '?')}" for k in fields]
        lines += [f"   ⚠️ {w}" for w in self.warnings]
        return "\n".join(lines)


def _disp_width(s: str) -> int:
    """终端显示宽度：全角/宽字符按 2 格算。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def _clip_disp(s: str, limit: int) -> str:
    """按显示宽度截断，截了就在后面标出原长度（长值需让人知道它被截过）。"""
    s = s.replace("\n", " ⏎ ")
    if _disp_width(s) <= limit:
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > limit - 2:
            break
        out.append(ch)
        w += cw
    return "".join(out) + f"…（共 {len(s)} 字）"


def _cn_to_int(s: str) -> Optional[int]:
    """阿拉伯数字原样；汉字数字支持到 99（八 / 十 / 十二 / 二十 / 二十五）。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return (_CN_DIGITS.get(head, 1) if head else 1) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    return _CN_DIGITS.get(s)


def _match_enum(text: str, enum_values, synonyms) -> Optional[tuple]:
    """返回 ``(归一值, 命中的词, span)``；命中不了返回 None。长词优先。"""
    for kw, val in sorted(synonyms.items(), key=lambda kv: -len(kv[0])):
        i = text.find(kw)
        if i >= 0:
            return val, kw, (i, i + len(kw))
    for val in sorted(enum_values, key=len, reverse=True):
        i = text.find(val)
        if i >= 0:
            return val, val, (i, i + len(val))
    return None


def _drop_spans(text: str, spans) -> str:
    out, pos = [], 0
    for s, e in sorted(spans):
        if s < pos:
            continue
        out.append(text[pos:s])
        pos = e
    out.append(text[pos:])
    return "".join(out)


def _edge_strip(s: str) -> str:
    """反复从两端剥包装词。只动两端，不动句子中间。"""
    while True:
        before = s
        s = _LEAD_WRAP_RE.sub("", s)
        s = _TAIL_WRAP_RE.sub("", s)
        s = s.strip(" 　，,、。；;：:！!？?（）()[]「」《》“”\"'")
        if s == before:
            return s


_FILL_LINE = {
    "enum": "   {f}: 从 {vals} 里选一个；没提到填「{default}」。",
    "number": "   {f}: {lo}~{hi} 的整数；没提到填 {default}。",
    "locked": "   {f}: 固定填「{default}」（它只能来自上游的教学材料，不从这句话里抽）。",
    "text": "   {f}: 字符串；没提到填「{default}」。",
}


def _slot_fill_prompt(profile: RoleProfile) -> str:
    """按角色契约生成槽位抽取指令。**不手写第二份字段表**——从 profile 派生，
    否则加了字段忘了改提示词，抽出来的东西对不上模板。"""
    lines = []
    for f in profile.fields:
        if f == profile.topic_field:
            lines.append(f"   {f}: 字符串。这份材料讲什么。**必须来自原句**，"
                         f"不得自拟、不得改写成别的题目；原句里确实没有就填「无」。")
        elif f in profile.locked:
            lines.append(_FILL_LINE["locked"].format(f=f, default=profile.defaults[f]))
        elif f in profile.enums:
            vals = " / ".join(profile.enums[f][0])
            lines.append(_FILL_LINE["enum"].format(
                f=f, vals=vals, default=profile.defaults[f]))
        elif f == "页数要求":
            lines.append(_FILL_LINE["number"].format(
                f=f, lo=PAGES_MIN, hi=PAGES_MAX, default=profile.defaults[f]))
        else:
            lines.append(_FILL_LINE["text"].format(f=f, default=profile.defaults[f]))
    return ("从下面这句话里抽取 PPT 制作的输入参数，只输出一个 JSON 对象。\n\n"
            "字段与取值：\n" + "\n".join(lines)
            + "\n\n用户这句话：\n{sentence}\n\n"
            "只输出 JSON，不要解释、不要代码块、不要多余文字：\n"
            + json.dumps({f: profile.defaults[f] for f in profile.fields},
                         ensure_ascii=False))


def _sanitize_fill(got: dict, profile: RoleProfile) -> dict:
    """把任何来源（LLM 兜底 / 未来其它填充器）的抽取结果卡到模板契约里。

    为什么不只依赖生成方自己做校验：``llm_fill`` 是**注入的可调用对象**，
    parse_task 无法假定它守规矩。硬约束必须由消费方再验一次——这与
    quality_gate 的立场一致：模型声明不等于合规。
    """
    out: dict = {}
    for f in profile.fields:
        if f in profile.locked:
            continue                       # 锁死字段永不被填充器改写
        val = str(got.get(f) or "").strip()
        if not val or val in (SENTINEL, "未提及", "None"):
            continue
        if f in profile.enums:
            if val in profile.enums[f][0]:
                out[f] = val
        elif f == "页数要求":
            try:
                n = int(float(val))
                if PAGES_MIN <= n <= PAGES_MAX:
                    out[f] = n
            except (TypeError, ValueError):
                pass
        else:
            out[f] = val
    return out


def llm_slot_fill(client, model: str, profile: RoleProfile, sentence: str) -> dict:
    """规则抽不到主题时的兜底。返回补到的字段（可能为空 dict）。"""
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0.0,
            messages=[{"role": "user",
                       "content": _slot_fill_prompt(profile).format(sentence=sentence)}])
        raw = resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001 —— 兜底失败就当作没抽到，不阻断
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_task(task: str, profile: RoleProfile, explicit: Optional[dict] = None,
               llm_fill=None) -> ParsedInputs:
    """S0：把一句话拆成模板要的那些字段。

    ``explicit`` 是用户显式传的 CLI 覆盖（只放**真的传了**的），优先级最高。
    ``llm_fill`` 形如 ``fn(sentence) -> dict``，只在规则抽不到主题时才会被调用。
    """
    explicit = {k: v for k, v in (explicit or {}).items() if v is not None}
    text = (task or "").strip()
    values: dict = {}
    sources: dict = {}
    warnings: list = []
    spans: list = []

    # ---- 主题：① 书名号 / 引号 ----
    for rx in _TOPIC_BRACKETS:
        m = rx.search(text)
        if m:
            values[profile.topic_field] = m.group(1).strip()
            sources[profile.topic_field] = "规则:《》"
            spans.append(m.span())
            break

    # ---- 主题：② “关于 X 的” ----
    if profile.topic_field not in values:
        for rx in _TOPIC_HINTS:
            m = rx.search(text)
            if m:
                values[profile.topic_field] = m.group(1).strip()
                sources[profile.topic_field] = "规则:关于X的"
                spans.append(m.span())
                break

    # ---- 页数（所有角色通用）----
    if "页数要求" in profile.fields:
        m = _PAGES_RE.search(text)
        if m:
            n = _cn_to_int(m.group(1))
            if n:
                spans.append(m.span())
                if not (PAGES_MIN <= n <= PAGES_MAX):
                    clamped = min(max(n, PAGES_MIN), PAGES_MAX)
                    warnings.append(f"页数 {n} 越界（模板契约 {PAGES_MIN}~{PAGES_MAX}），已夹到 {clamped}")
                    n = clamped
                values["页数要求"], sources["页数要求"] = n, f"规则:{m.group(0).strip()}"

    # ---- enum 字段（受众/场景/学生水平…）：取值域来自 profile，不写死 ----
    for f, (enum_values, synonyms) in profile.enums.items():
        hit = _match_enum(text, enum_values, synonyms)
        if hit:
            values[f], sources[f], spans = hit[0], f"规则:{hit[1]}", spans + [hit[2]]

    # ---- 角色专属抽取器（ppt_general 的重点/边界/风格；teacher_ppt 的学科）----
    if profile.extras is not None:
        extra_v, extra_s, spans = profile.extras(text, spans)
        values.update(extra_v)
        sources.update(extra_s)

    # ---- 主题：③ 裸句残差（把已识别的片段挖掉，两端剥包装词）----
    if profile.topic_field not in values:
        cand = _edge_strip(_drop_spans(text, spans))
        if len(cand) >= 2 and re.search(r"[\w\u4e00-\u9fff]", cand):
            values[profile.topic_field] = cand
            sources[profile.topic_field] = "规则:裸句提取"

    # ---- 主题：④ LLM 兜底（只在规则完全抽不到时）----
    if profile.topic_field not in values and llm_fill is not None:
        got = _sanitize_fill(dict(llm_fill(text) or {}), profile)
        topic = got.pop(profile.topic_field, None)
        if topic:
            values[profile.topic_field] = topic
            sources[profile.topic_field] = "LLM兜底"
        for k, v in got.items():
            if k not in values and explicit.get(k) is None:
                values[k], sources[k] = v, "LLM兜底"

    # ---- 显式 flag 覆盖 > 规则/LLM > 默认 ----
    for f in profile.fields:
        given = explicit.get(f)
        if given is not None and str(given).strip() != "":
            values[f], sources[f] = given, "flag"
        elif f not in values:
            if f == profile.topic_field:
                continue                        # 主题无默认，缺了就报错
            if f in profile.locked:
                values[f], sources[f] = profile.defaults[f], "固定默认"
            else:
                values[f], sources[f] = profile.defaults[f], "默认"
                warnings.append(f"未提及「{f}」，按默认「{profile.defaults[f]}」处理")

    # ---- 归一化 ----
    if "页数要求" in values:
        try:
            values["页数要求"] = int(values["页数要求"])
        except (TypeError, ValueError):
            values["页数要求"] = profile.defaults["页数要求"]
            sources["页数要求"] = "默认(页数无法解析)"
    for f, v in list(values.items()):
        if str(v).strip() in ("", "None"):
            values[f] = profile.defaults.get(f, SENTINEL)
            sources[f] = "默认(空值)"

    # 只把「真没提到」的默认值报成提醒，锁死字段的固定值不算（它本来就不该从句子抽）
    return ParsedInputs(values=values, sources=sources, warnings=warnings)


# ===========================================================================
# S1 · 渲染
# ===========================================================================
@dataclass
class PlanRequest:
    """一次链路输入：模板字段表 + 角色档案。"""
    profile: RoleProfile
    values: dict

    def variables(self) -> dict:
        return {f: str(self.values[f]) for f in self.profile.fields}

    def case(self) -> dict:
        return {f: self.values[f] for f in self.profile.case_fields}

    def brief(self) -> str:
        topic = self.values.get(self.profile.topic_field, "")
        tail = " · ".join(f"{f}={self.values[f]}" for f in self.profile.fields
                          if f != self.profile.topic_field)
        return f"《{topic}》 · {tail}"


def build_plan_prompt(lib: PromptLib, req: PlanRequest, fewshot: bool = True) -> str:
    """渲染模板提示词。入口预检不过直接抛，不烧 API。"""
    variables = require_inputs(lib, req.profile.role, req.variables())
    return lib.build(req.profile.role, variables, fewshot=fewshot)


# ===========================================================================
# S2 · 规划：调 LLM → 闸门 → 升级阶梯 → 降级兜底
# ===========================================================================
@dataclass
class PlanOutcome:
    text: str = ""
    attempts: int = 0
    checks: list = field(default_factory=list)
    prompt: str = ""
    ok: bool = False
    degraded: bool = False
    error: str = ""
    temperatures: list = field(default_factory=list)
    api_errors: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        """当前保留的缺陷列表 ``[(name, False, detail), ...]``。"""
        return [c for c in self.checks if not c[1]]

    @property
    def passed_count(self) -> int:
        return len(self.checks) - len(self.failures)

    def defect_lines(self) -> list:
        return [f"{name}：{detail or '未通过'}" for name, _, detail in self.failures]


REPAIR_INSTRUCTION = """你的输出存在以下问题，请修正后重新输出**完整**结果：

{defects}

修正要求（逐条对应，不要只改措辞）：
1. 4 个模块必须齐全、顺序不变、都非空，模块名逐字用原文要求的起头词。
2. 页号从 1 连续编到最后一页，不跳号、不重复、不缺少任何一页。
3. 每一页都必须同时有：页标题、至少 1 条要点、1 行「配图：」、1 行「旁白：」。
4. 「旁白：」按要求字数写足，是能照着讲的内容，不说套话、不复读要点。
5. 【整体结构】必须给出逻辑线，并与逐页大纲的页序一致。
6. 页数必须与第 1 个模块里声明的页数一致。
本次是最后一次机会，请直接输出完整大纲，不要解释。"""


def ladder_temperature(round_index: int, base: float) -> float:
    """第 ``round_index`` 轮（0 起）用多少温度。逐轮降温，不高于用户给的 base。"""
    if round_index < len(TEMPERATURE_LADDER):
        return min(base, TEMPERATURE_LADDER[round_index])
    return 0.0


def plan_ppt(
    lib: PromptLib,
    req: PlanRequest,
    client,
    model: str,
    *,
    retries: int = 3,
    temperature: float = 0.7,
    fewshot: bool = True,
    verbose: bool = True,
) -> PlanOutcome:
    """S2：规划 + 闸门 + 升级阶梯。**永不因校验不过而中断**，最差返回降级底稿。"""
    profile = req.profile
    out = PlanOutcome()
    out.prompt = build_plan_prompt(lib, req, fewshot=fewshot)
    if verbose:
        print(f"📝 提示词已渲染：{len(out.prompt):,} 字符"
              f"（{profile.role}，fewshot={'on' if fewshot else 'off'}）")

    messages = [{"role": "user", "content": out.prompt}]
    best: Optional[tuple] = None  # (raw, failures, checks, temp) —— 缺陷最少的一份

    for attempt in range(retries + 1):
        out.attempts = attempt + 1
        temp = ladder_temperature(attempt, temperature)
        out.temperatures.append(temp)

        raw = ""
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=temp)
            raw = resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权异常在无人值守下必须自己扛
            err = f"{type(exc).__name__}: {exc}"
            out.api_errors.append({"attempt": out.attempts, "temperature": temp, "error": err})
            if verbose:
                print(f"   ⚠️ 第 {attempt + 1} 轮 API 异常：{err}")
            if attempt < retries:
                time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                continue
            break

        if not raw.strip():
            checks = []
            failures = [("模型返回空", False, "回复内容为空")]
        else:
            checks = profile.validator(raw, req.case())
            failures = [c for c in checks if not c[1]]

        if verbose:
            print(f"   第 {attempt + 1} 轮 temp={temp}："
                  f"{len(checks) - len(failures) if checks else 0}/{len(checks)} 项通过，"
                  f"{len(raw):,} 字")
            for name, _, detail in failures:
                print(f"     ✗ {name}  {detail}")

        if not failures:
            out.text, out.checks, out.ok = raw, checks, True
            return out

        # 记录历轮中最优的一份：**不是最后一份**——最后一轮温度最低，但可能恰好更差
        if best is None or len(failures) < len(best[1]):
            best = (raw, failures, checks, temp)

        if attempt >= retries:
            break

        messages += [
            {"role": "assistant", "content": raw or "(空回复)"},
            {"role": "user", "content": REPAIR_INSTRUCTION.format(
                defects="\n".join(f"- {n}：{d or '未通过'}" for n, _, d in failures))},
        ]
        if BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]:
            time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])

    # —— 阶梯走完仍未通过：降级交接，用历轮最优底稿 ——
    if best is not None:
        out.text, failures, out.checks, _ = best
        out.degraded = True
        out.error = (f"规划 {out.passed_count}/{len(out.checks)} 项通过，"
                     f"{len(failures)} 项缺陷降级交接（已尝试 {out.attempts} 轮，"
                     f"温度 {out.temperatures}）")
    else:
        out.error = ("规划失败：全部轮次均为 API 异常。"
                     + (f"最后一条：{out.api_errors[-1]['error']}" if out.api_errors else ""))
    return out


# ===========================================================================
# S3 · 落盘 + 交接文本
# ===========================================================================
HANDOFF_HEADER = """【自动链路交接说明】

本任务是上游「PPT 规划」环节自动产出的，规划校验发现以下 {n} 项缺陷未能在
规划阶段修好，请在 P1/P2 规划时**自行修正这些点**（不要照抄缺陷）：

{defects}

以上缺陷只影响体例与完整性，不改变主题与内容取向。以下为规划正文。
───────────────
"""


def build_handoff_task(plan_text: str, outcome: Optional[PlanOutcome]) -> str:
    """决定交给 PPT-Agent 的任务文本。

    干净的大纲原样给；带缺陷的大纲把**缺陷清单转成给执行体的中文指令**再给。
    不把校验器原文直接塞过去：那是格式化报告，对执行体是噪声。
    """
    if outcome is None or not outcome.failures:
        return plan_text
    defects = outcome.defect_lines()
    return HANDOFF_HEADER.format(
        n=len(defects),
        defects="\n".join(f"- {d}" for d in defects),
    ) + plan_text


def save_plan(req: PlanRequest, outcome: PlanOutcome, model: str,
              handoff: str) -> tuple:
    """落盘两份：``plan_*.md``（纯大纲，供续跑/审计）与 ``task_*.md``（实际交给执行体）。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = RUNS_DIR / f"plan_{stamp}.md"
    task_path = RUNS_DIR / f"task_{stamp}.md"
    plan_path.write_text(outcome.text, encoding="utf-8")
    task_path.write_text(handoff, encoding="utf-8")

    meta = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "role": req.profile.role,
        "inputs": req.variables(),
        "attempts": outcome.attempts,
        "temperatures": outcome.temperatures,
        "checks_passed": outcome.passed_count,
        "checks_total": len(outcome.checks),
        "degraded": outcome.degraded,
        "failures": [{"name": n, "detail": d} for n, _, d in outcome.failures],
        "api_errors": outcome.api_errors,
        "prompt_chars": len(outcome.prompt),
        "plan_chars": len(outcome.text),
    }
    plan_path.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan_path, task_path


def load_plan(path: Path) -> tuple:
    """读回一份落盘大纲 → ``(文本, sidecar 里的输入表, sidecar 里的 role)``。"""
    text = path.read_text(encoding="utf-8")
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.is_file():
        return text, None, None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return text, meta.get("inputs") or None, meta.get("role") or None


def _sniff_role(text: str) -> Optional[str]:
    """从大纲文本的模块名反推它属于哪个角色。

    为什么需要：《role 字段》是后加的，**早期 sidecar 里没有它**，而两个角色的
    首模块名是互斥的（【课件主题】 vs 【主题与受众】），拿当前 CLI 角色去复检
    一份老底稿会得到一堆无关的「模块缺失」。有 sidecar 就信 sidecar，
    没有就用模块名兜底，再没有才用 CLI 角色。
    """
    for name, prof in ROLE_PROFILES.items():
        if all(f"【{s}】" in text for s in prof.sections):
            return name
    return None


def _fewshot_cases(profile: RoleProfile) -> list:
    """读 ``fewshots/<role>.yaml`` 的范文，连【示例输入】的字段一并解析出来。

    刻意用**解析**而不是另抄一份输入表：范文改了，自检跟着改；抄一份迟早漂移。
    每份范文有自己的输入，所以校验时要用它自己的 case，而不是当前 req 的。
    """
    path = PROMPT_LIB_DIR / "fewshots" / f"{profile.role}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rx = _example_label_re(profile)
    cases = []
    for i, ex in enumerate(data.get("examples") or [], 1):
        text = str(ex.get("input") or "")
        marks = list(rx.finditer(text))
        case: dict = {}
        for j, m in enumerate(marks):
            stop = marks[j + 1].start() if j + 1 < len(marks) else len(text)
            case[m.group(1)] = text[m.end():stop].strip()
        case["_output"] = str(ex.get("output") or "")
        cases.append((f"范文{i}", case))
    return cases


# ===========================================================================
# S4 · 执行：交给 PPT-Agent（子进程，输出实时透传，stdin 关闭）
# ===========================================================================
def run_ppt_agent(
    task_path: Path,
    *,
    mode: str = "staged",
    profile: str = "default",
    skill_dir: Optional[str] = None,
    output_root: Optional[str] = None,
    extra_args: tuple = (),
    stage_env: Optional[dict] = None,
    verbose: bool = True,
) -> tuple:
    """调用 ``agent.py --task-file``。返回 ``(returncode, 全部输出)``。

    ``mode`` / ``profile`` 由 ``resolve_agent_pair`` 定下（它已经拦住了矛盾组合），
    这里只负责如实拼进去。

    无人值守的两个保障，都只靠**外部调用参数**，不改 agent.py：

    · ``stdin=DEVNULL`` —— 真正兜底的那个。agent.py 里有 3 处 ``input()``：
      命令确认 / ask_user 的 BLOCKING 门 / 缺任务时提问。
      任何一处读到真实 stdin 都会**永久挂起**；DEVNULL 让它们抛 EOFError，
      被工具层的 except 接住、回到模型的观察里，而不是死等。
      这三处在 staged 模式下其实都不可达（staged 只调 ``agent._ask()``，
      不进 ReAct 工具循环），但 react 模式或未来改动下会重新变得可达。
    · ``--task-file`` —— 绕开「缺任务时提问」那个 input()。
    """
    cmd = [get_python_command(), str(HERE / "agent.py"), "--task-file", str(task_path),
           "--mode", mode, "--profile", profile]
    if skill_dir:
        cmd += ["--skill-dir", str(skill_dir)]
    if output_root:
        cmd += ["--output-root", str(output_root)]
    cmd += list(extra_args)

    if verbose:
        print(f"\n🚀 {subprocess.list2cmdline(cmd)}\n" + "─" * 72)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    # 分阶段推理强度 / 修复轮数走环境变量：它们是 StagedRunner 的旋钮，
    # agent.py 的命令行没有这两个开关，而 pipeline 不改 agent.py。
    env.update({k: v for k, v in (stage_env or {}).items() if v is not None})
    proc = subprocess.Popen(
        cmd, cwd=str(HERE), env=env,
        stdin=subprocess.DEVNULL,               # ← 绝不等人
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    chunks: list = []
    assert proc.stdout is not None
    for line in proc.stdout:
        chunks.append(line)
        if verbose:
            sys.stdout.write(line)
            sys.stdout.flush()
    proc.wait()
    return proc.returncode, "".join(chunks)


def find_pptx(output_root: Optional[Path], since: float) -> Optional[Path]:
    """找本次运行新产出的 PPTX：优先 mtime 在开工之后的，兜底取最新。"""
    candidates: list = []
    bases = [output_root] if output_root else []
    try:
        bases.append(resolve_skill_dir(None) / "projects")
    except FileNotFoundError:
        pass
    env_root = os.getenv("PPT_AGENT_OUTPUT_ROOT")
    if env_root:
        bases.append(Path(env_root).expanduser())
    for base in bases:
        if base and base.is_dir():
            candidates.extend(base.rglob("*.pptx"))
    if not candidates:
        return None
    fresh = [p for p in candidates if p.stat().st_mtime >= since - 5]
    pool = fresh or candidates
    return max(pool, key=lambda p: p.stat().st_mtime)


# ===========================================================================
# 运行报告：无人值守链路的对外接口
# ===========================================================================
def write_report(report: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RUNS_DIR / f"report_{stamp}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    RUNS_DIR.joinpath("last_report.json").write_text(payload, encoding="utf-8")
    return path


def emit_report(report: dict, path: Path, verbose: bool) -> None:
    """最后一行给人和机器各一份：人能读的摘要 + 一行可 grep 的 JSON。"""
    if verbose:
        print("\n" + "═" * 72)
        print(f"status   = {report['status']}")
        if report.get("role"):
            print(f"角色     = {report['role']}（{report.get('role_label', '')}）")
        if report.get("brief"):
            print(f"输入     = {report['brief']}")
        if report.get("sentence"):
            print(f"原句     = {report['sentence']}")
        if report.get("error"):
            print(f"错误     = {report['error']}")
        if report.get("prompt_path"):
            print(f"提示词   = {report['prompt_path']}")
        if report.get("plan"):
            p = report["plan"]
            print(f"规划     = {p['checks_passed']}/{p['checks_total']} 项"
                  f"，{p['attempts']} 轮，温度 {p['temperatures']}"
                  + ("  ⚠️ 降级交接" if p["degraded"] else ""))
            for d in p.get("defects", []):
                print(f"           ✗ {d}")
            print(f"大纲     = {p['path']}")
        if report.get("ppt"):
            print(f"产物     = {report['ppt'].get('pptx') or '（未找到，见上方 Final Answer）'}")
        print(f"报告     = {path}")
    print("RUN_REPORT " + json.dumps(
        {k: report.get(k) for k in ("status", "role", "seconds", "brief", "error")}
        | {"plan": (report.get("plan") or {}).get("path"),
           "prompt": report.get("prompt_path"),
           "pptx": (report.get("ppt") or {}).get("pptx")},
        ensure_ascii=False))


# ===========================================================================
# 离线自检：不调 API
# ===========================================================================
def self_check(lib: PromptLib, profile: Optional[RoleProfile] = None) -> None:
    profiles = [profile] if profile else list(ROLE_PROFILES.values())
    print("🔎 链路离线自检（不调 API）\n")

    for idx, prof in enumerate(profiles, 1):
        tag = f"{prof.role}"
        # ① 档案字段必须与模板占位符**完全一致**（否则 S0 抽的字段对不上模板）
        placeholders = lib.missing_vars(prof.role, {})
        assert set(placeholders) == set(prof.fields), \
            f"[{tag}] 档案字段与模板占位符不一致：档案={sorted(prof.fields)}，" \
            f"模板={sorted(placeholders)}"
        assert prof.topic_field in prof.fields, f"[{tag}] topic_field 不在 fields 里"
        assert set(prof.case_fields) <= set(prof.fields), f"[{tag}] case_fields 越界"

        # ② 提示词能渲染（fewshot 开）
        case = {
            "teacher_ppt": {"学科": "初中化学", "课件主题": "质量守恒定律",
                            "学生水平": "基础", "页数要求": 8, "单元内容": "无"},
            "ppt_general": {"主题": "新能源汽车智能座舱产品介绍", "受众": "客户",
                            "场景": "产品介绍", "页数要求": 9,
                            "核心重点": "语音交互准确率", "边界": "不展开电池技术",
                            "风格偏好": "科技感"},
        }[prof.role]
        req = PlanRequest(profile=prof, values=case)
        prompt = build_plan_prompt(lib, req)
        for frag in ("【角色设定】", "【输出格式】", "【输出纪律】") + prof.sections:
            assert frag in prompt, f"[{tag}] 提示词缺片段：{frag}"
        assert case[prof.topic_field] in prompt, f"[{tag}] 主题没进提示词"
        print(f"   ① [{tag}] 档案与模板对齐 + 提示词渲染 OK：{len(prompt):,} 字符")

        # ③ 入口预检：非法 enum / 越界页数 / 空主题都要被拦
        bad = dict(case)
        bad[prof.topic_field] = ""
        bad["页数要求"] = 100
        for f, (vals, _) in prof.enums.items():
            bad[f] = "路人甲乙"
        kinds = {p.name for p in lib.check_inputs(prof.role, bad)}
        assert prof.topic_field in kinds and "页数要求" in kinds, \
            f"[{tag}] 入口预检漏检：{sorted(kinds)}"
        assert set(prof.enums) <= kinds, f"[{tag}] enum 越域没被拦：{sorted(kinds)}"
        print(f"   ② [{tag}] 入口预检 OK：空主题 / 越界页数 / "
              f"{len(prof.enums)} 个 enum 越域均被拦")

        # ④ 校验器：范文必须满分通过它**自己**的校验器（否则等于教模型写错）
        cases = _fewshot_cases(prof)
        assert cases, f"[{tag}] 没读到 fewshots/{prof.role}.yaml 的范文"
        checks: list = []
        for name, f_case in cases:
            checks = prof.validator(
                f_case["_output"],
                {k: f_case[k] for k in prof.case_fields if k in f_case},
                check_contamination=False)
            failed = [n for n, ok, _ in checks if not ok]
            assert not failed, f"[{tag}] {name} 未通过自己的校验器：{failed}"
        print(f"   ③ [{tag}] 校验器 OK：{len(cases)} 份范文满分通过"
              f"（{len(checks)} 项断言）")

        # ⑤ 闸门方向性：注入缺陷必须被检出（否则闸门等于没装）
        base = cases[0][1]["_output"]
        case0 = {k: cases[0][1][k] for k in prof.case_fields if k in cases[0][1]}
        injections = [
            ("模块齐全", base.replace(f"【{prof.sections[-1]}】", "【排版建议】"), case0),
            ("每页都有配图与旁白", base.replace("配图：", "", 1), case0),
        ]
        # 角色专属的「从提示词请求变成硬约束」项。这两条都曾经只是写在提示词里、
        # 没有任何断言：结构骨架用 any(…"→") 就能骗过，档位差异完全没人查。
        if prof.role == "teacher_ppt":
            injections.append(("整体结构落地", re.sub(
                r"2\. 【整体结构】[^\n]*",
                "2. 【整体结构】三部分：背景 → 内容 → 结论。", base), case0))
            mid = next((c for _, c in cases if c.get("学生水平") == "中等"), None)
            assert mid, "范文里没有中等档样本，档位断言验不了"
            injections.append(("中等档", mid["_output"].replace("可能的提问", "课后想一想"),
                               {k: mid[k] for k in prof.case_fields if k in mid}))
        if prof.role == "ppt_general":
            injections.append(("场景骨架落地", re.sub(
                r"2\. 【整体结构】[^\n]*",
                "2. 【整体结构】三部分：开头 → 中间 → 结尾。", base), case0))
        for expect, sample, c in injections:
            got = [n for n, ok, _ in prof.validator(
                sample, c, check_contamination=False) if not ok]
            assert any(expect in g for g in got), f"[{tag}] 未检出：{expect}（实得 {got}）"
        print(f"   ④ [{tag}] 闸门 OK：{len(injections)} 类注入缺陷全部检出")

        # ⑥ S0 解析：每个角色拿它自己的句式验一遍
        samples = {
            "teacher_ppt": [
                ("做一份 8 页的《质量守恒定律》课件", {"课件主题": "质量守恒定律", "页数要求": 8}),
                ("初中化学课件，9 页，主题《质量守恒定律》，学生水平基础",
                 {"学科": "初中化学", "课件主题": "质量守恒定律",
                  "页数要求": 9, "学生水平": "基础"}),
                ("给零基础的学生做一份十页的《光合作用》课件",
                 {"课件主题": "光合作用", "页数要求": 10, "学生水平": "入门"}),
                ("高中数学 12 页课件，讲《导数的几何意义》，要竞赛难度的",
                 {"学科": "高中数学", "课件主题": "导数的几何意义",
                  "页数要求": 12, "学生水平": "竞赛"}),
            ],
            "ppt_general": [
                ("做一份 8 页的《向量数据库入门》PPT", {"主题": "向量数据库入门", "页数要求": 8}),
                ("帮我生成一份关于新能源汽车智能座舱的 10 页 PPT，给投资人看，路演用",
                 {"主题": "新能源汽车智能座舱", "页数要求": 10,
                  "受众": "投资人", "场景": "路演"}),
                ("做一份 9 页的《年度复盘》PPT，不要展开财务细节，边界：只讲业务",
                 {"页数要求": 9, "边界": "只讲业务"}),
            ],
        }[prof.role]
        for sentence, expect in samples:
            got = parse_task(sentence, prof).values
            for k, v in expect.items():
                assert got.get(k) == v, \
                    f"[{tag}] S0 解析错：{sentence!r}\n   {k} 期望 {v!r}，实得 {got.get(k)!r}"
        # 默认值：句子没提到的非锁定字段必须落到档案默认值
        bare = parse_task(samples[0][0], prof)
        for f in prof.fields:
            if f == prof.topic_field or f in prof.locked:
                continue
            if f not in samples[0][1]:
                assert bare.values[f] == prof.defaults[f], \
                    f"[{tag}] {f} 默认值应为 {prof.defaults[f]!r}，实得 {bare.values[f]!r}"
        print(f"   ⑤ [{tag}] S0 解析 OK：{len(samples)} 种句式 + 默认值落位")

        # ⑦ flag 覆盖 > 规则；锁定字段永不从句子抽
        forced = parse_task("做一份 8 页的《X》课件", prof, {"页数要求": 15})
        assert forced.values["页数要求"] == 15 and forced.sources["页数要求"] == "flag"
        for f in prof.locked:
            assert forced.sources[f] == "固定默认", f"[{tag}] {f} 不该被抽取"
        print(f"   ⑥ [{tag}] flag 覆盖 OK + "
              f"{len(prof.locked)} 个锁定字段（{','.join(prof.locked) or '无'}）未被抽取")

    # —— 跨角色的公共性质 ——
    any_prof = ROLE_PROFILES[ROLE]
    clamped = parse_task("做一份 50 页的《X》课件", any_prof)
    assert clamped.values["页数要求"] == PAGES_MAX and clamped.warnings, "越界没夹取"
    blank = parse_task("帮我做一份课件吧", any_prof)
    assert not blank.values.get(any_prof.topic_field), "空主题没被拦"
    fallback = parse_task("帮我做一份课件吧", ROLE_PROFILES["teacher_ppt"],
                          llm_fill=lambda s: {"课件主题": "兜底主题", "学生水平": "非法档位"})
    assert fallback.values["课件主题"] == "兜底主题"
    assert fallback.values["学生水平"] == "基础", "LLM 兜底的 enum 校验失效"
    print(f"   ⑦ 边界 OK：50页→{PAGES_MAX}（告警）/ 空主题拦下 / "
          f"LLM 兜底的非法 enum 已退回默认")

    # ⑧ 升级阶梯：单调不升且收敛到 0；降级交接能生成
    temps = [ladder_temperature(i, 0.7) for i in range(5)]
    assert temps[0] == 0.7 and temps == sorted(temps, reverse=True), f"阶梯不单调：{temps}"
    assert temps[-1] == 0.0, f"阶梯未收敛到 0：{temps}"
    print(f"   ⑧ 升级阶梯 OK：{temps}")

    prof = ROLE_PROFILES[ROLE]
    good = _fewshot_cases(prof)[0][1]["_output"]
    degraded = PlanOutcome(text=good, degraded=True,
                           checks=prof.validator(good.replace("配图：", "", 1),
                                                 check_contamination=False))
    handoff = build_handoff_task(degraded.text, degraded)
    assert "【自动链路交接说明】" in handoff and handoff.endswith(degraded.text)
    assert build_handoff_task(good, None) == good, "干净大纲不应被加头"
    print(f"   ⑨ 降级交接 OK：{len(degraded.failures)} 项缺陷已转成执行体指令")

    # ⑩ agent 配对守卫：这是本文件里唯一会去「纠正调用方」的地方，必须验方向
    auto = {"staged": "default", "react": "quick"}
    for mode, want in auto.items():
        got_mode, got_prof, note = resolve_agent_pair(mode, None)
        assert (got_mode, got_prof) == (mode, want) and note, \
            f"自动配对错：{mode} -> {got_prof}（应为 {want}）"
    for mode, prof in (("staged", "quick"), ("react", "quick"), ("staged", "default")):
        expect_reject = (mode, prof) == ("staged", "quick")
        try:
            resolve_agent_pair(mode, prof)
            assert not expect_reject, f"矛盾组合 {mode}+{prof} 本应被拒"
        except ValueError:
            assert expect_reject, f"{mode}+{prof} 不该被拒"
    for bad in (("--profile", "quick"), ("--mode", "react")):
        try:
            resolve_agent_pair("staged", None, bad)
            raise AssertionError(f"--agent-args 里塞 {bad[0]} 应被拒")
        except ValueError:
            pass
    print(f"   ⑩ agent 配对 OK：{auto} 自动配对；staged+quick 被拒；"
          f"--agent-args 里的 --mode/--profile 被拒")

    # ⑩b 分阶段推理的三个旋钮（P3 快 / P4 会判断 靠它）
    assert set(STAGE_REASONING_ENV) == {"plan", "exec", "repair"}, STAGE_REASONING_ENV
    from staged import StagedRunner as _SR
    _r = _SR(types.SimpleNamespace(tools=None, skill_dir=Path("."), python_cmd="python"))
    assert (_r.plan_reasoning, _r.exec_reasoning, _r.repair_reasoning) == (None, None, None)
    os.environ["PPT_STAGE_REASONING_REPAIR"] = "low"
    try:
        _r2 = _SR(types.SimpleNamespace(tools=None, skill_dir=Path("."), python_cmd="python"))
        assert _r2.repair_reasoning == "low" and \
            (_r2.repair_reasoning or _r2.exec_reasoning) == "low", "P4 修复没拿到单独旋钮"
    finally:
        os.environ.pop("PPT_STAGE_REASONING_REPAIR", None)
    print("   ⑩b 分阶段推理 OK：plan/exec/repair 三档；P4 未设时继承 exec（默认行为不变）")

    # ⑪ 讲稿备注：旁白能进 PPTX 的**唯一通道**（任务 → notes/total.md → 切分 → --with-notes）
    from staged import StagedRunner, extract_narrations
    outline = (
        "3. 【逐页大纲】\n"
        "第1页（封面）：质量守恒定律\n· 要点\n配图：x\n旁白：第一页的讲稿。\n\n"
        "第2页（内容）：实验验证\n· 要点\n配图：y\n旁白：第二页的讲稿，长一点。\n"
    )
    got = extract_narrations(outline)
    assert got == ["第一页的讲稿。", "第二页的讲稿，长一点。"], f"旁白抽取错：{got}"
    assert extract_narrations("做一份 8 页的《X》课件") == [], "无旁白时不该凭空造"

    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / "svg_output").mkdir()
        # 先放一个上轮残留的旧讲稿：scripts/README.md 说得很清楚，
        # --no-notes 存在的理由就是「防 notes/ 里的旧文件被嵌进新 PPTX」。
        # 本次要开备注，就必须自己保证目录里只剩本轮讲稿。
        (proj / "notes").mkdir()
        (proj / "notes" / "stale.md").write_text("上一轮的旧讲稿", encoding="utf-8")
        fake = types.SimpleNamespace(
            project_path=proj,
            plan={"roster": [{"file": "01_cover.svg"}, {"file": "02_body.svg"}]},
            narrations=got,
        )
        n = StagedRunner._write_notes_total(fake)
        left = sorted(p.name for p in (proj / "notes").glob("*.md"))
        assert n == 2 and left == ["total.md"], f"旧讲稿没清干净：{left}"
        body = (proj / "notes" / "total.md").read_text(encoding="utf-8")
        for frag in ("# 01_cover", "# 02_body", "\n---\n", got[0], got[1]):
            assert frag in body, f"total.md 缺片段：{frag!r}"
    print("   ⑪ 讲稿备注 OK：旁白抽取 + total.md 格式（# stem / --- 分隔）+ 清理旧文件")

    # ⑫ 函数图像几何：把坐标算术从模型手里拿走（推理 74% 花在这里）
    from staged import plot_page_figure
    import plot_math
    ok_txt, ok_err = plot_page_figure({
        "kind": "function_plot", "expr": "0.35*x^2 + 0.5", "domain": [-3, 3],
        "area": [560, 140, 1180, 620], "point": 2.0, "secants": [0, 1, 1.5]})
    assert ok_err == "" and "切点 P" in ok_txt and "割线端点" in ok_txt, ok_err
    # 标注推荐位置必须一并给出（实测 none 下模型会把 Q4 叠在 P 上）
    assert "标注推荐位置" in ok_txt and "Q3" in ok_txt, ok_txt[-400:]
    assert ok_txt.count("x=") >= 4, "标注坐标没给全"
    # 割线斜率必须依次逼近切线斜率——这正是这张图要教的东西，也是最容易算错的地方
    slopes = [float(m) for m in re.findall(r"割线斜率 ([\d.]+)", ok_txt)]
    assert len(slopes) == 3 and slopes == sorted(slopes), slopes
    assert abs(slopes[-1] - 1.4) < abs(slopes[0] - 1.4), "割线没有向切线斜率收敛"
    # 声明错误时必须**降级**（返回空文本 + 原因），不能抛异常把整份 deck 拦下
    for bad in ({"kind": "function_plot", "expr": "x^^2oops", "domain": [-2, 2],
                 "area": [0, 0, 100, 100]},
                {"kind": "function_plot", "expr": "x**2", "domain": [-2, 2]},
                {"kind": "function_plot", "expr": "x**2", "domain": [-2, 2],
                 "area": [0, 0, 100, 100], "point": 99}):
        txt, err = plot_page_figure(bad)
        assert txt == "" and err, f"非法 figure 没有降级：{bad} → {txt[:40]!r} {err!r}"
    assert plot_page_figure(None) == ("", "") and plot_page_figure({"kind": "none"}) == ("", "")
    # 多切点：points 列表 → 每个切点各自算切线（正负单调 / 陡峭类页面）
    multi_txt, multi_err = plot_page_figure({
        "kind": "function_plot", "expr": "x**3 - 3*x", "domain": [-3, 3],
        "area": [700, 220, 1180, 560], "points": [-1.8, 0.0, 1.8]})
    assert multi_err == "" and "切点 P1" in multi_txt and "切点 P3" in multi_txt, multi_err
    assert multi_txt.count("切线（P") == 3, multi_txt[-600:]
    # 尖点：abs(x) + sharp=0 → 无切线，只画左右割线
    sharp_txt, sharp_err = plot_page_figure({
        "kind": "function_plot", "expr": "abs(x)", "domain": [-4, 4],
        "area": [700, 220, 1180, 560], "sharp": 0.0, "secants": [-2.5, 2.5]})
    assert sharp_err == "" and "尖点" in sharp_txt and "切线（" not in sharp_txt, sharp_err
    # 圆锥曲线：椭圆/双曲线/抛物线（它们不是函数，原来只能手画成斜直线）
    ell_txt, ell_err = plot_page_figure({
        "kind": "conic", "type": "ellipse", "a": 3, "b": 2, "center": [0, 0],
        "view": [-5, 5, -4, 4], "area": [660, 140, 1230, 620]})
    assert ell_err == "" and "圆锥曲线几何" in ell_txt and "焦点" in ell_txt, ell_err
    print("   ⑫ 函数图像几何 OK：割线斜率收敛到切线 1.4 + 多切点 P1~P3 + 尖点 + 圆锥曲线 + 标注推荐位置；"
          "3 类非法声明 + 缺省均降级不中断")

    # ⑬ 容器框自动修复：P4 的第一道闸，不花模型调用
    #    （实测：18 次调用里 8 次是逐页返工这类纯几何错，每次 ~80s）
    import bounds_fix
    svg0 = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<g id="h" data-pptx-bounds="0 0 100 50">'
            '<text x="0" y="60" font-size="20">x</text></g></svg>')
    over_err = ("<text> (x=0, y=60; text='x') exceeds <g id=\"h\"> data-pptx-bounds "
                "on the vertical axis: content (0.0, 43.0)-(6.0, 67.0), "
                "container (0.0, 0.0)-(100.0, 50.0)")
    expanded, n_exp = bounds_fix.expand_bounds_for_overflow(svg0, [over_err])
    assert bounds_fix.read_group_bounds(expanded, "h")[3] >= 67.0 and n_exp, n_exp
    shifted, n_shift = bounds_fix.shift_content_into_container(svg0, [over_err])
    assert re.search(r'<text x="0" y="39"', shifted), f"挪位不对：{shifted}"
    assert n_shift, n_shift
    # 重叠类一概不碰（那是布局错，无唯一解，必须交模型）
    overlap_err = ('<g id="a"> data-pptx-bounds overlaps <g id="b"> data-pptx-bounds '
                   'by 5.0px x 5.0px')
    assert bounds_fix.expand_bounds_for_overflow(svg0, [overlap_err])[0] == svg0
    assert bounds_fix.shift_content_into_container(svg0, [overlap_err])[0] == svg0
    # 第四条策略：超长单行文字拆成多行 tspan
    reflow_svg = ('<svg viewBox="0 0 1280 720">'
                  '<g id="cb" data-pptx-bounds="56 556 484 126">'
                  '<text x="90" y="634" font-size="18">'
                  '提示：先写切线，再由凸性判断它与曲线的高低，误差方向自然出来</text>'
                  '</g></svg>')
    reflow_err = ("<text> (x=90, y=634; text='…') exceeds <g id=\"cb\"> data-pptx-bounds "
                  "on the horizontal axis: content (90.0, 618.7)-(662.4, 640.3), "
                  "container (56.0, 556.0)-(540.0, 682.0)")
    reflowed, n_reflow = bounds_fix.reflow_overflow_text(reflow_svg, [reflow_err])
    assert '<tspan' in reflowed and n_reflow, n_reflow
    print("   ⑬ 容器框自动修复 OK：扩框/挪元素/拆行三条策略均生效（挪 60→39），重叠类不碰")

    # ⑭ 返工的内容完整性闸门（实测事故：返工把“三步列表”压成 78px 窄条、文字全删，
    #    而 checker 只查结构，报的是错误 0——内容损失静默通过）
    from staged import content_loss, visible_texts
    full = ('<svg><text x="0" y="0">固定P点，Q沿曲线滑动</text>'
            '<text x="0" y="20">割线PQ随Q转动</text>'
            '<text x="0" y="40">Q趋近P时割线趋于切线</text></svg>')
    gutted = '<svg><text x="0" y="0">1</text><text x="0" y="20">2</text></svg>'
    lost = content_loss(full, gutted)
    assert len(lost) == 3, lost
    # 合法改写（把长句改短以避让超界）不能误报——否则闸门会阻断正当修复
    shorter = ('<svg><text x="0" y="0">固定P点</text>'
               '<text x="0" y="20">割线PQ随Q转动</text>'
               '<text x="0" y="40">Q趋近P时割线趋于切线</text></svg>')
    assert content_loss(full, shorter) == [], content_loss(full, shorter)
    assert content_loss(full, full) == []
    assert len(visible_texts(full)) == 3
    print(f"   ⑭ 内容完整性闸门 OK：删 3 条→检出 3 条；合法改短/原样→不误报")

    print(f"\n✅ 自检通过（当前角色 ROLE={ROLE!r}）：档案对齐 / S0 解析 / S1 渲染 / "
          f"入口预检 / S2 校验器 / 升级阶梯 / 降级交接 / agent 配对 / 讲稿备注 / "
          f"图像几何 / 容器框自动修复 均正常")


# ===========================================================================
# CLI
# ===========================================================================
@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("task", required=False)
# —— S0 输入：一句话（与 agent.py 同形）——
# 位置参数 task 是主入口；下面这些 flag 是**显式覆盖**。
# 它们默认 None 而不是实际默认值：只有这样代码才能区分「用户真传了」
# 和「click 填的默认值」，否则 flag 会永远压过句子里抽出来的值。
@click.option("--role", type=click.Choice(sorted(ROLE_PROFILES)),
              default=None, help=f"使用的提示词模板角色。默认 {ROLE}。")
@click.option("--topic", "--主题", "topic", default=None,
              help="主题 / 课件主题（覆盖句子里的《》抽取；禁模型自拟）。")
@click.option("--pages", "--页数", "pages", type=int, default=None,
              help="页数（模板契约 8~30）。默认 10。")
@click.option("--subject", "--学科", "subject", default=None,
              help="学科（teacher_ppt）。默认 通用。")
@click.option("--level", "--学生水平", "level", default=None,
              help="学生水平：入门/基础/中等/进阶/竞赛（teacher_ppt）。默认 基础。")
@click.option("--unit-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="单元内容文件（teacher_ppt）：上游 teacher_unit 的教学材料。")
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE",
              help="覆盖任意模板字段，可重复。如 --set 场景=汇报 --set 受众=老板。")
# —— 链路控制 ——
@click.option("--from-plan", type=click.Path(exists=True, dir_okay=False), default=None,
              help="复用已落盘的大纲续跑（自动复检，不过则降级交接）。")
@click.option("--plan-only", is_flag=True, default=False,
              help="只做 S1-S3 规划（预演/省钱探路），不调 PPT-Agent。")
@click.option("--explain", is_flag=True, default=False,
              help="只做 S0 解析 + S1 渲染提示词并落盘，不调 LLM、不产片。")
@click.option("--check", "do_check", is_flag=True, default=False,
              help="离线自检：档案对齐 + S0 解析 + 提示词渲染 + 校验器审计，不调 API。")
# —— S2 模型 ——
@click.option("--model", default=DEFAULT_MODEL, show_default=True,
              help="规划阶段模型（DeepSeek OpenAI 兼容端点）。")
@click.option("--base-url", default=DEFAULT_BASE_URL, show_default=True)
@click.option("--api-key", default=None, help="默认从 .env 的 DEEPSEEK_API_KEY 读取。")
@click.option("--temperature", type=float, default=0.7, show_default=True,
              help="首轮温度；后续轮次按升级阶梯自动降温至 0。")
@click.option("--retries", type=int, default=3, show_default=True,
              help="规划阶段的自动重试轮数（含降温与回投缺陷）。")
@click.option("--no-fewshot", is_flag=True, default=False,
              help="渲染提示词时不拼接 fewshot（对照实验用）。")
# —— S4 执行 ——
@click.option("--skill-dir", default=None, help="透传给 agent.py。")
@click.option("--output-root", default=None, help="透传给 agent.py 的落盘根目录。")
@click.option("--agent-mode", type=click.Choice(AGENT_MODES), default="staged",
              show_default=True, help="agent.py 的执行模式。")
@click.option("--agent-profile", type=click.Choice(AGENT_PROFILES), default=None,
              help="agent.py 的 profile。默认按 agent-mode 自动配对（staged→default，react→quick）。")
@click.option("--agent-args", default="", help='透传给 agent.py 的其它参数，如 --parallel-pages 3。')
@click.option("--reasoning-plan", type=click.Choice(REASONING_CHOICES), default=None,
              help="规划侧（P1/P2）推理强度。默认沿用 agent.py 全局设置。")
@click.option("--reasoning-exec", type=click.Choice(REASONING_CHOICES), default=None,
              help="执行侧（P3/P4）推理强度。⚠️ 实测 none 会导致交付失败，见 --help 长文。")
@click.option("--reasoning-repair", type=click.Choice(REASONING_CHOICES), default=None,
              help="仅 P4 修复的推理强度（未设则继承 --reasoning-exec）。这是「P3 快 + P4 会判断」那个组合。")
@click.option("--max-page-repairs", type=int, default=None,
              help="P4 逐页修复轮数上限（默认 2）。0 = 不修直接导出——但导出门仍会判 blocking。")
@click.option("--quiet", is_flag=True, default=False, help="减少输出。")
def main(task, role, topic, pages, subject, level, unit_file, sets,
         from_plan, plan_only, explain, do_check, model, base_url, api_key,
         temperature, retries, no_fewshot, skill_dir, output_root,
         agent_mode, agent_profile, agent_args,
         reasoning_plan, reasoning_exec, reasoning_repair, max_page_repairs, quiet):
    """PPT 生成链路：一句话 → 提示词模板 → LLM 规划 → PPT-Agent → PPTX。

    入口与 agent.py 同形：位置参数就是那句自然语言，中间由模板润色。

    全程无人值守：规划不过闸门时自动降温重试，仍不过则以「缺陷清单 + 底稿」降级交接，
    交给 PPT-Agent 继续产片；只有 PPT-Agent 本身失败才返回非 0。

    \b
    示例：
      python pipeline.py "做一份 8 页的《质量守恒定律》课件"
      python pipeline.py "初中化学 9 页课件，主题《质量守恒定律》，学生水平基础"
      python pipeline.py --unit-file unit.md "做一份 10 页的《光合作用》课件"
      python pipeline.py "..." --explain          # 只看解析+润色后的提示词
      python pipeline.py "..." --plan-only        # 只做规划，不产片
      python pipeline.py "..." --agent-mode react   # 换成 react+quick
      python pipeline.py "..." --reasoning-exec none --reasoning-repair low  # P3 快、P4 会判断
      python pipeline.py --from-plan runs/plan_xxx.md
      python pipeline.py --check                  # 离线自检
    """
    load_env_files()
    lib = PromptLib(PROMPT_LIB_DIR)

    if do_check:
        self_check(lib, ROLE_PROFILES[role] if role else None)
        return

    verbose = not quiet
    started = time.time()
    active = ROLE_PROFILES[role or ROLE]
    report: dict = {"started_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "failed", "sentence": task or None,
                    "role": active.role, "role_label": active.label}

    def bail(msg: str, code: int, status: str = "failed"):
        report["status"], report["error"] = status, msg
        report["seconds"] = round(time.time() - started, 1)
        emit_report(report, write_report(report), verbose)
        raise SystemExit(code)

    # —— 先定 (mode, profile) 并拦住矛盾组合 ——
    # 放在最前面：矛盾组合应该在**花掉任何一次模型调用之前**就被拒绝，
    # 而不是等规划跑完、进了 P2 才发现预载文档与骨架不匹配。
    extra_args = tuple(agent_args.split()) if agent_args.strip() else ()
    try:
        agent_mode, agent_profile, pairing_note = resolve_agent_pair(
            agent_mode, agent_profile, extra_args)
    except ValueError as exc:
        bail(str(exc), 2)
    report["agent"] = {"mode": agent_mode, "profile": agent_profile,
                       "reasoning_plan": reasoning_plan,
                       "reasoning_exec": reasoning_exec,
                       "reasoning_repair": reasoning_repair,
                       "max_page_repairs": max_page_repairs}
    stage_env = {
        STAGE_REASONING_ENV["plan"]: reasoning_plan,
        STAGE_REASONING_ENV["exec"]: reasoning_exec,
        STAGE_REASONING_ENV["repair"]: reasoning_repair,
        MAX_PAGE_REPAIRS_ENV: None if max_page_repairs is None else str(max_page_repairs),
    }
    if reasoning_plan or reasoning_exec or reasoning_repair or max_page_repairs is not None:
        print(f"   分阶段：规划侧={reasoning_plan or '全局默认'}  "
              f"执行侧={reasoning_exec or '全局默认'}  "
              f"P4修复={reasoning_repair or '继承执行侧'}  "
              f"P4轮数={max_page_repairs if max_page_repairs is not None else '默认2'}")

    # 只保留**真的传了**的覆盖项（click 默认全是 None，所以能区分）
    explicit: dict = {}
    if topic is not None:
        explicit[active.topic_field] = topic
    if pages is not None:
        explicit["页数要求"] = pages
    if subject is not None:
        explicit["学科"] = subject
    if level is not None:
        explicit["学生水平"] = level
    if unit_file is not None:
        raw_unit = Path(unit_file).read_text(encoding="utf-8").strip()
        # 单元输出（teacher_unit 的 10 模块）→ PPT 的「单元内容」：筛 5 个教学内容模块，
        # 并带上【课件大纲】作页骨架参照（与 demo_course_flow.build_ppt_prompt 同一套逻辑）。
        explicit["单元内容"] = (unit_output_to_ppt_content(raw_unit)
                            if raw_unit else SENTINEL)
    for pair in sets:
        if "=" not in pair:
            print(f"⚠️ 忽略 --set {pair!r}：应为 KEY=VALUE")
            continue
        k, _, v = pair.partition("=")
        explicit[k.strip()] = v.strip()

    unknown = set(explicit) - set(active.fields)
    if unknown:
        print(f"⚠️ --set/-flag 里有 {active.role} 不认识的字段：{sorted(unknown)}"
              f"（该角色的字段是 {list(active.fields)}）")

    # 懒建 client：--explain / 纯规则解析 都不该因为缺 key 而失败
    _cache: dict = {}

    def get_client():
        if "c" not in _cache:
            key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
            if not key:
                _cache["c"] = None
            else:
                from openai import OpenAI
                _cache["c"] = OpenAI(api_key=key, base_url=base_url)
        return _cache["c"]

    def llm_fill(sentence: str) -> dict:
        c = get_client()
        if c is None:
            return {}
        print("   ↳ 规则未抽到主题，回落一次 LLM 槽位抽取")
        return llm_slot_fill(c, model, active, sentence)

    # ---------- 入口：新规划 / 复用底稿 ----------
    if from_plan:
        plan_path = Path(from_plan).resolve()
        plan_text, meta_inputs, meta_role = load_plan(plan_path)
        # 角色优先级：显式 --role > sidecar.role > 模块名反查 > 当前默认
        if role is not None:
            profile, origin = active, "--role 指定"
        elif meta_role and meta_role in ROLE_PROFILES:
            profile, origin = ROLE_PROFILES[meta_role], "sidecar"
        elif (sniffed := _sniff_role(plan_text)):
            profile, origin = ROLE_PROFILES[sniffed], "模块名反查"
        else:
            profile, origin = active, "默认"
        report["role"], report["role_label"] = profile.role, profile.label

        print(f"📄 复用底稿：{plan_path}")
        print(f"   角色：{profile.role}（{profile.label}），来源：{origin}")

        values = None
        if meta_inputs:
            values = {**profile.defaults, **meta_inputs}
        if explicit:
            values = {**(values or profile.defaults), **explicit}
        if values is None:
            print("   ⚠️ 无 sidecar 输入表，跳过复检（无法构造 case）")

        outcome: Optional[PlanOutcome] = None
        if values is not None:
            req = PlanRequest(profile=profile, values=values)
            checks = profile.validator(plan_text, req.case())
            failures = [c for c in checks if not c[1]]
            outcome = PlanOutcome(text=plan_text, checks=checks, attempts=1,
                                  degraded=bool(failures), ok=not failures,
                                  error="" if not failures else "复用底稿未过校验")
            print(f"   🔎 复检：{len(checks) - len(failures)}/{len(checks)} 项通过")
            for n, _, d in failures:
                print(f"     ✗ {n}  {d}")
            if failures:
                print("   ⚠️ 底稿带缺陷 → 自动降级交接，不停机")
            report["brief"] = req.brief()
            report["inputs"] = req.variables()
        report["plan"] = {
            "path": str(plan_path), "reused": True, "attempts": 1, "temperatures": [],
            "checks_passed": outcome.passed_count if outcome else len(plan_text),
            "checks_total": len(outcome.checks) if outcome else 0,
            "degraded": bool(outcome and outcome.degraded),
            "defects": outcome.defect_lines() if outcome else [],
            "model": "reused",
        }
        task_path = plan_path
    else:
        # ---------- S0 一句话 → 模板字段 ----------
        if not task and active.topic_field not in explicit:
            bail(f"缺任务描述：给一句自然语言（如 '做一份 8 页的《X》课件'），"
                 f"或用 --topic / --from-plan。", 2)

        parsed = parse_task(task or "", active, explicit,
                            llm_fill=None if explain else llm_fill)
        report["inputs"] = parsed.values
        report["input_sources"] = parsed.sources
        report["input_warnings"] = parsed.warnings

        if verbose:
            print(f"\n【S0 解析】{task!r}" if task else "\n【S0 解析】（无句子，全部来自 flag）")
            print(parsed.table(active.fields))
            print(f"   角色：{active.role}（{active.label}）· 结构骨架：{active.skeleton}")

        if not parsed.values.get(active.topic_field):
            bail(f"没能从这句话里认出「{active.topic_field}」——模板禁止模型自拟主题，"
                 f"请写清楚（推荐用《》括起来，如 '做一份 8 页的《X》课件'）。", 2)

        req = PlanRequest(profile=active, values=parsed.values)
        report["brief"] = req.brief()
        try:
            prompt = build_plan_prompt(lib, req, fewshot=not no_fewshot)
        except ValueError as exc:      # 输入不合契约：确定性拒绝，不进模型
            bail(str(exc), 2)

        if verbose:
            print(f"\n【S1 润色】{active.role} 模板渲染 {len(prompt):,} 字符"
                  f"（fewshot={'off' if no_fewshot else 'on'}）")

        # --explain：把润色后的提示词落盘给人看，到此为止
        if explain:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            prompt_path = RUNS_DIR / f"prompt_{stamp}.txt"
            prompt_path.write_text(
                f"# 角色\n{active.role}（{active.label}）\n\n"
                + f"# 一句话输入\n{task or ''}\n\n"
                + f"# S0 解析\n{parsed.table(active.fields)}\n\n"
                + "# S1 渲染后的完整提示词（发给 LLM 的就是它）\n" + prompt + "\n",
                encoding="utf-8")
            report["status"] = "explained"
            report["prompt_path"] = str(prompt_path)
            report["prompt_chars"] = len(prompt)
            report["seconds"] = round(time.time() - started, 1)
            if verbose:
                print(f"   提示词已落盘：{prompt_path}")
            emit_report(report, write_report(report), verbose)
            return

        c = get_client()
        if c is None:
            bail("未找到 API Key（PPT-Agent/.env 的 DEEPSEEK_API_KEY）", 2)

        print(f"\n【S2 规划】模型 {model}，最多 {retries + 1} 轮（逐轮降温）")
        outcome = plan_ppt(lib, req, c, model, retries=retries,
                           temperature=temperature, fewshot=not no_fewshot,
                           verbose=verbose)

        if not outcome.text.strip():
            report["plan"] = {
                "path": None, "attempts": outcome.attempts,
                "temperatures": outcome.temperatures, "checks_passed": 0,
                "checks_total": 0, "degraded": False, "defects": [],
                "api_errors": outcome.api_errors, "model": model,
            }
            bail(outcome.error or "规划产出为空", 3)

        handoff = build_handoff_task(outcome.text, outcome)
        plan_path, task_path = save_plan(req, outcome, model, handoff)
        report["plan"] = {
            "path": str(plan_path), "task_path": str(task_path), "reused": False,
            "attempts": outcome.attempts, "temperatures": outcome.temperatures,
            "checks_passed": outcome.passed_count, "checks_total": len(outcome.checks),
            "degraded": outcome.degraded, "defects": outcome.defect_lines(),
            "api_errors": outcome.api_errors, "model": model,
        }
        line = (f"✅ 规划 {outcome.passed_count}/{len(outcome.checks)} 项通过"
                f"（{outcome.attempts} 轮）") if outcome.ok else f"⚠️ {outcome.error}"
        print(f"\n【S3 落盘】{line}\n   大纲：{plan_path}")
        if outcome.degraded:
            print(f"   交接：{task_path}（已把 {len(outcome.failures)} 项缺陷转成执行体指令）")

    # ---------- S4 ----------
    if plan_only:
        report["status"] = "planned" if (outcome and outcome.ok) else "planned_degraded"
        report["seconds"] = round(time.time() - started, 1)
        emit_report(report, write_report(report), verbose)
        return

    print(f"\n【S4 执行】交给 PPT-Agent（mode={agent_mode} profile={agent_profile}）")
    if pairing_note:
        print(f"   {pairing_note}")

    root = Path(output_root).expanduser().resolve() if output_root else None
    code, output = run_ppt_agent(task_path, mode=agent_mode, profile=agent_profile,
                                 skill_dir=skill_dir, output_root=output_root,
                                 extra_args=extra_args, stage_env=stage_env,
                                 verbose=verbose)

    pptx = find_pptx(root, started)
    report["ppt"] = {"exit_code": code, "pptx": str(pptx) if pptx else None,
                     "output_tail": output[-2000:]}
    report["seconds"] = round(time.time() - started, 1)

    if code != 0 or pptx is None:
        report["status"] = "failed"
        report["error"] = (f"PPT-Agent 退出码 {code}" if code != 0
                           else "PPT-Agent 正常退出但未找到 .pptx")
        emit_report(report, write_report(report), verbose)
        raise SystemExit(1)

    # 出片了：规划带缺陷的标 degraded，让上游自己决定要不要复跑
    report["status"] = "degraded" if (outcome and outcome.degraded) else "ok"
    emit_report(report, write_report(report), verbose)


if __name__ == "__main__":
    main()
