#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt 模板库 × LangChain —— 一次完整学习闭环的「真实调用场景」

分支归属：本文件是**「怎么调用模板库」的端到端示例**，只随 `PromptTemplateV3.0`
分支发布，**不合并回 `main`**（它是演示/教学材料，不是库本体）。
没有 Key 也能先看请求体：`python PromotTemplate.py --dry-run`（或 `--cot-table`）——
模型客户端是惰性创建的，导入与渲染都不碰网络。

跑一遍真实学生会经历的事情，每一步都是**真 API 调用**，没有 mock：

    阶段 0  入口预检      输入不合契约就地拒绝，不浪费 token
    阶段 1  M1 规划       teacher_planner → 骨架 + 门控蓝图，过闸门，不过就回投重做
    阶段 2  门控判定      后端硬约束判「能否进下一章」，绝不问模型
    阶段 3  M2 单元渲染   teacher_unit → 按上一章学情渲染单个单元
    阶段 3B 材料题专项     teacher_material + source_analysis → 结论+依据的分点作答（文理各一套材料）
    阶段 4  助教批改       assistant → 解析【得分】，分母必须等于系统传入的满分
    阶段 4A 写作构思       teacher_outline + essay_outline → 审题→立意→提纲→素材→首尾（不写全文）
    阶段 4B 作文批改       assistant_essay → 文科轨道；闸门复用库自带校验器
    阶段 5  督学提醒       supervisor → 基于本周学习数据
    阶段 6  学生追问       qa → 多轮 history，模板当 system 提示

CoT 片段按「学科轨道 + 用途」自动选（COT_TABLE / cot_for）：理科讲解挂 math_steps，
文科讲解挂 text_reading，批改自查理科 self_check / 文科 self_check_humanities，
材料题挂 source_analysis（阶段 3B）、写作构思挂 essay_outline（阶段 4A）——
后两个片段此前“备而未接”（库里没人用），2026-09-30 补上对应阶段。
阶段 3B 用**专用角色** teacher_material（输出契约是「结论 + 依据」的作答体），
阶段 4A 用 teacher_outline（输出契约是「中心句 + 提纲 + 素材」的构思体）——
两者都不再借 teacher 的 {{知识点}} 槽位硬塞内容：teacher 的契约是教学模块，
借它渲染必然产出讲解体（V2.0-说明 缺口 #1 与第十节）。
答疑刻意不挂 —— CoT 要求「写出推理并给出最终答案」，与 qa 的「引导不喂答案」直接对撞。

四条真实工程约束落在代码里（不是注释里的口号）：

  1. 变量只能有一个主人
     模板库管「结构 + Few-shot + CoT」，LangChain 管变量。
     `{{学科}}` 恰好就是 jinja2 的变量语法，所以零转换对接：
     `PromptTemplate.from_template(raw, template_format="jinja2")`。
     反面教材：把 "{subject}" 当值传给模板库 —— 模板库里没传的变量会残留成
     `{{need_example}}`，被 LangChain 当转义符吃掉，模型收到 `IF {need_example} == false:`。

  2. 入口预检必须在调用前
     实测：把「学生答案」留空，模型会拿旁边的「参考答案」当学生答案，
     给出「10/10、判定对、置信度高」——学生没作答却拿满分。
     字段缺失是调用方的错，靠提示词兜不住，必须 backend 拦。

  3. 闸门在后端，不在指令层
     照抄 Few-shot、得分分母不对，这类可程序化检测的问题，
     指令层修不干净（实测 bio 用例首轮 3/3 照抄），靠「回投问题 + 重做」兜底。

  4. 观测与留痕
     每次调用的延迟、token、原始输出都记账落盘，出问题能回放。

用法::

    python PromotTemplate.py --dry-run              # 只组装 Prompt，不调 API，先看要发什么
    python PromotTemplate.py                        # 真跑全链路
    python PromotTemplate.py --answer 白卷          # 换学生答案（见 --help）
    python PromotTemplate.py --answer 字段缺失      # 看入口预检如何拦下调用
    python PromotTemplate.py --scenario science     # 换回理科场景（默认文科）
    python PromotTemplate.py --no-gate              # 关掉回投重试，看首轮原始输出
    python PromotTemplate.py --cot off              # 不挂 CoT 片段，对比挂与不挂的差别
    python PromotTemplate.py --cot-table            # 只看 CoT 选择表（哪个学科挂哪个片段）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError

# ======================================================================
# 0. 环境与基础设施
# ======================================================================
load_dotenv()

# 模板库已 `pip install -e .`（py-modules 包含 prompt_builder / quality_gate /
# demo_course_flow / eval_generic），所以下面四个都能直接导入，不需 sys.path 动手。
# LIB_DIR 只用于两件事：给 PromptLib 指根目录、给 fewshot_unit_titles 找示例文件。
# 用相对本文件的位置算出来，**不写绝对路径**：本文件会作为「怎么调用模板库」的示例
# 随分支发布，别人的仓库目录不可能也叫 C:\ClaudeWorkPlace\自建Agent。
# 本文件在 <repo>/LangChain开发/ 下，所以往上一级就是仓库根。
LIB_DIR = Path(__file__).resolve().parent.parent / "PromptTemplate"

# 兜底：只在本机没装包（fresh clone）时才把库目录挂上 sys.path，
# 装了包就走正常导入 —— 也是为了让 Pylance 能静态解析（它不执行 sys.path 这行）。
if not importlib.util.find_spec("quality_gate"):
    sys.path.insert(0, str(LIB_DIR))

from prompt_builder import PromptLib                                  # noqa: E402
from quality_gate import require_inputs                                # noqa: E402
from eval_generic import (                                             # noqa: E402
    ASSISTANT_SECTIONS, ESSAY_SECTIONS, MATERIAL_SECTIONS, OUTLINE_SECTIONS,
    outline_paragraphs, split_by_names, split_points, validate_essay,
    validate_material, validate_outline,
)
from demo_course_flow import (                                         # noqa: E402
    can_unlock, course_status, next_unlocked_unit, parse_blueprint, unit_variables,
)

lib = PromptLib(LIB_DIR)

# ---- 模型客户端：**惰性**创建 ----
# 为何不在这里直接 ChatOpenAI(...)：本文件同时是「怎么调用模板库」的端到端示例，
# 而 --dry-run / --cot-table 的卖点恰好是「不花钱、不要 Key 就能看到请求体」。
# 导入即建客户端会让没配 Key 的读者连请求体都看不到（实测报
# openai.OpenAIError: Missing credentials）。所以客户端推迟到真 invoke 时才建。
MODEL_NAME = "deepseek-flash"                     # 与 Chained_Call/Memory 等文件保持一致
BASE_URL = "https://api.deepseek.com/v1"
_LLM = None


def get_llm():
    """按需创建客户端；没 Key 时给可行动的报错，而不是导入就炸。"""
    global _LLM
    if _LLM is None:
        key = os.getenv("DEEPSEEK_API_KEY")        # 不写会去读 OPENAI_API_KEY，换项目就串
        if not key:
            raise SystemExit(
                "未找到 DEEPSEEK_API_KEY（请在 .env 或环境变量里配置）。\n"
                "只想看要发给模型的原文则不需要 Key：\n"
                "    python PromotTemplate.py --dry-run\n"
                "    python PromotTemplate.py --cot-table"
            )
        _LLM = ChatOpenAI(model=MODEL_NAME, base_url=BASE_URL, api_key=key,
                          temperature=0.7, timeout=120)
    return _LLM


# 用一个 Runnable 代理把「建客户端」推到 invoke 时；这样 build_chain() 与
# --dry-run 的 render_request()（只取 chain.steps[0] 渲染）都不需要 Key。
llm = RunnableLambda(lambda payload: get_llm().invoke(payload))


# 值得重试的 HTTP 状态：限流 / 服务端抽风 / 网关抖动。
# 其余 4xx 都是确定性失败（402 欠费、401 鉴权、400 请求非法），
# 重试只是把同一个错误再烧一遍钱和 30 秒。
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def classify_error(e: Exception) -> tuple[bool, str]:
    """区分「值得重试」与「重试也没用」。

    真实触发过的例子：DeepSeek 余额耗尽返回 402 Insufficient Balance。
    原实现对任何异常都退避重试 2 次，等于白等 2+4 秒再报同一个错；
    现在的行为是第一次就带着可行动的说明放弃：告诉运维去充值，而不是「调用失败」。
    """
    if isinstance(e, APIStatusError):
        code = getattr(e, "status_code", None)
        if code in RETRYABLE_STATUS:            # 先判可重试，别被下面的 <500 先命中
            return True, f"HTTP {code}（限流/服务端抖动）"
        if code is not None and code < 500:
            hint = {401: "鉴权失败，检查 API Key",
                    402: "账户余额不足，需充值",
                    403: "无权限",
                    404: "模型名或路径不存在"}.get(code, "请求本身非法")
            return False, f"HTTP {code}（{hint}）"
        return True, f"HTTP {code}"
    if isinstance(e, (APITimeoutError, APIConnectionError)):
        return True, type(e).__name__
    return True, type(e).__name__          # 未知异常给一次机会


@dataclass
class Row:
    """一次调用的账单。"""
    stage: str
    attempt: int = 1
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    error: str = ""


class Meter(BaseCallbackHandler):
    """LangChain 回调：给每次 LLM 调用记账。

    生产里这一步就是 LangSmith（本项目 .env 已开 LANGCHAIN_TRACING_V2=true，
    tags 会直接成为 trace 上的标签），这里同时落到本地表便于终端打印。
    """

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self._t0: dict[str, float] = {}

    def on_llm_start(self, serialized, prompts, *, run_id=None, tags=None,
                     metadata=None, **kw):
        self._t0[str(run_id)] = time.perf_counter()
        # 注意：tags 里 LangChain 会先塞自己的自动标签（seq:step:2 这类），
        # 所以阶段名走 metadata；tags 仅作兜底。
        stage = (metadata or {}).get("stage") or (tags or ["?"])[-1]
        self.rows.append(Row(stage=stage))

    def on_llm_end(self, response, *, run_id=None, **kw):
        row = self.rows[-1]
        row.seconds = time.perf_counter() - self._t0.pop(str(run_id), time.perf_counter())
        usage = (response.llm_output or {}).get("token_usage") or {}
        if not usage:  # LangChain 新版把用量放在 message.usage_metadata
            try:
                usage = response.generations[0][0].message.usage_metadata or {}
            except (IndexError, AttributeError):
                usage = {}
        row.prompt_tokens = usage.get("prompt_tokens", 0)
        row.completion_tokens = usage.get("completion_tokens", 0)

    def on_llm_error(self, error, *, run_id=None, **kw):
        row = self.rows[-1]
        row.seconds = time.perf_counter() - self._t0.pop(str(run_id), time.perf_counter())
        row.error = f"{type(error).__name__}: {error}"


METER = Meter()
STAGES: list[tuple[str, str]] = []          # (阶段名, 模型原始输出)，用于落盘
# (阶段名, 第几次尝试, [(消息类型, 内容)]) —— 真实发送出去的消息。
# dry-run 用的 render_request 是“将要发”，这里记的是“已经发”：重试回投、
# history 拼接这些只在真跑里存在的东西，只有这里能如实留下。
#
# 必须带“第几次尝试”：只看阶段名的话，重试后的那次会盖掉重试前那次，
# 而“回投了什么问题”恰恰是诊断行为变化的关键。
REQUESTS: list[tuple[str, int, list[tuple[str, str]]]] = []
PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")   # 模板库的 {{中文名}} 占位符


# ======================================================================
# 0B. CoT 片段的选择：按「学科轨道 + 用途」挑，不再全库硬套 math_steps
# ======================================================================
# 片段本体在 PromptTemplate/cot/snippets.yaml，共 7 个。此前只有 math_steps 被任何
# 代码引用过（还只在文档示例和测试里），文科 4 个片段属于“备而未接”。
# 选择规则写进代码而不是文档，是为了让“某个片段没人用”能被发现：cot_for 会记账，
# 跑完由 report_cot_usage 打印使用情况。
HUMANITIES_KEYS = ("语文", "历史", "政治", "地理", "英语")

# (轨道, 用途) → 片段名。**缺键是刻意不挂，不是漏配** —— 理由写在 NO_COT_REASON。
COT_TABLE: dict[tuple[str, str], str] = {
    ("science", "explain"): "math_steps",         # 讲解：审题→选方法→计算→检验→结论
    ("science", "selfcheck"): "self_check",       # 批改自查：代入检验 / 特殊值检验
    ("science", "material"): "source_analysis",   # 材料题：实验数据/图表同样走「设问→分层→提取→联系→分点」
    ("humanities", "explain"): "text_reading",    # 阅读讲解：定位→圈词→语境→依据回填
    ("humanities", "material"): "source_analysis",  # 材料题：设问→分层→提取→背景→分点
    ("humanities", "write"): "essay_outline",     # 写作构思：审题→立意→提纲→素材
    ("humanities", "selfcheck"): "self_check_humanities",   # 批改自查：回文/回问/落地/体例
}

# 这些用途刻意不挂 CoT。理由必须写下：否则后人只会看到“查表没命中”，以为是漏配。
NO_COT_REASON: dict[tuple[str, str], str] = {
    ("science", "guide"): "答疑契约是「引导不喂答案」，CoT 却要求「写出推理并给出最终答案」，直接对撞",
    ("humanities", "guide"): "同上：答疑契约「引导不喂答案」，与解题/分析 CoT 对撞",
    ("science", "plan"): "规划产出的是 YAML 蓝图，分步解题的 CoT 只会把字段撑变形",
    ("humanities", "plan"): "同上：规划产出 YAML 蓝图，不走解题/分析 CoT",
    ("science", "nudge"): "督学产出的是提醒语，不含解题过程，挂了只会凑字数",
    ("humanities", "nudge"): "同上：督学不产出解题过程",
    ("science", "write"): "理科轨道没有作文/写作构思这类任务，硬挂只会把讲解撑成写作提纲",
}

COT_MODE = "auto"                       # main() 按 --cot 覆盖
COT_USED: dict[str, list[str]] = {}     # 片段名 → 用它的阶段


def track_of(subject: str) -> str:
    """学科 → 轨道。默认理科：库里理科资产最全，判错代价也更小。"""
    return "humanities" if any(k in subject for k in HUMANITIES_KEYS) else "science"


def pick_cot(subject: str, purpose: str) -> str | None:
    """纯查表：按轨道 + 用途给片段名，没有就是 None。不记账，供 --dry-run 预览用。"""
    if COT_MODE == "off":
        return None
    return COT_TABLE.get((track_of(subject), purpose))


def cot_for(subject: str, purpose: str, stage: str) -> tuple[str | None, str]:
    """查片段 + 记账，返回 (片段名, 给用户看的一行说明)。"""
    name = pick_cot(subject, purpose)
    if name:
        COT_USED.setdefault(name, []).append(stage)
    track = track_of(subject)
    if COT_MODE == "off":
        note = "cot=关（--cot off）"
    elif name:
        note = f"cot={name}（{track}·{purpose}）"
    else:
        note = f"cot=—（{NO_COT_REASON.get((track, purpose), '无对应片段')}）"
    return name, note


def report_cot_usage() -> None:
    """CoT 片段使用情况：挂上的、以及仍然备而未接的，都摆出来。"""
    print("\n【CoT 片段使用情况】")
    names = list(dict.fromkeys(COT_TABLE.values()))
    for n in names:
        stages = COT_USED.get(n)
        print(f"  {'✓' if stages else '·'} {n:22}"
              + ("被 " + "、".join(stages) + " 使用" if stages else "本次未使用"))
    print("  · zero_shot             本次未使用（有更专用的片段时不用它兜底）")
    unused = [n for n in names if n not in COT_USED]
    if unused and COT_MODE != "off":
        print(f"  仍未接上的 {len(unused)} 个：{unused}")
        print("  原因：它们属于另一条学科轨道（每个场景只挂本轨道的片段），不是漏配；")
        print("  另有阶段 3 的学科由蓝图决定——文科场景要先真跑一次 M1 拿到同科蓝图。")


def print_cot_table() -> None:
    """打印完整的 CoT 选择表（含本场景跑不到的轨道）。

    为什么要这个：本场景的单元是生物（理科），所以“文科讲解”这条路永远不会被
    触发——如果不把它摆出来，读者只能看到“text_reading 未使用”，
    分不清是“没接线”还是“没场景”。
    """
    print("CoT 选择表：学科轨道 × 用途 → 片段")
    print(f"  文科关键词 {'、'.join(HUMANITIES_KEYS)}；命中任一 → humanities，否则 science（默认）\n")
    for track in ("science", "humanities"):
        print(f"  [{track}]")
        for purpose in ("explain", "material", "write", "selfcheck"):
            name = COT_TABLE.get((track, purpose))
            print(f"    {purpose:<11} → {name or '—（本轨道无此用途的片段）'}")
    print("\n  刻意不挂 CoT 的用途（缺的是有意为之，不是漏配）：")
    for (track, purpose), why in NO_COT_REASON.items():
        if purpose in ("guide", "plan", "nudge", "write"):
            print(f"    [{track}] {purpose:<6} — {why}")


def build_chain(role: str, *, fewshot=True, cot=None, style="one_shot"):
    """把模板库的 Prompt 包成 LangChain Runnable。

    style:
      one_shot  一次问答，模板当 human 消息（与库自带评测器的送法一致）
      gate      额外挂 feedback 消息位，供闸门「回投问题 + 重做」
      chat      模板当 system 提示，对话历史由调用方给（多轮答疑用）
    """
    raw = lib.build(role, {}, fewshot=fewshot, cot=cot)   # 传空 dict：占位符原样保留
    # llm 是惰性代理（见 get_llm）：这里只是把它接进链，不建客户端、不需要 Key。
    if style == "gate":
        tpl = ChatPromptTemplate.from_messages(
            [("human", raw), MessagesPlaceholder("feedback", optional=True)],
            template_format="jinja2",
        )
    elif style == "chat":
        tpl = ChatPromptTemplate.from_messages(
            [("system", raw), MessagesPlaceholder("history")],
            template_format="jinja2",
        )
    else:
        tpl = ChatPromptTemplate.from_messages([("human", raw)], template_format="jinja2")
    return tpl | llm | StrOutputParser()


def call(stage: str, role: str, chain, values: dict, *, extra: dict | None = None,
         retries: int = 2) -> str:
    """真调一次模型：入口预检 → 调用 → 记账 → 留痕。

    预检在**这里**做，而不是各调用点各写一遍 —— 缺字段/值超域一律抛 ValueError，
    绝不把「缺输入」交给模型填（见文件头约束 2）。
    """
    require_inputs(lib, role, values)          # 缺项 / 类型不符 → 阻断，不产生调用
    payload = {**values, **(extra or {})}

    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            # 留痕：记录本次**实际**发给模型的消息（失败也记，才知道重试时发了什么）
            try:
                msgs = render_request(chain, payload)
                REQUESTS.append((stage, attempt,
                                 [(type(m).__name__, m.content) for m in msgs]))
            except Exception as e:                  # noqa: BLE001
                REQUESTS.append((stage, attempt,
                                 [("渲染失败", f"{type(e).__name__}: {e}")]))
            text = chain.invoke(payload, config={
                "callbacks": [METER],
                "tags": [stage],                       # 落到 LangSmith trace 的标签
                "metadata": {"stage": stage},         # 本地记账用（tags 会被污染）
            })
            METER.rows[-1].attempt = attempt
            METER.rows[-1].error = ""              # 重试成功则抹掉上一次的失败痕迹
            STAGES.append((stage, text))
            print(f"  ✓ {stage}｜{len(text)} 字符｜{METER.rows[-1].seconds:.1f}s")
            return text
        except Exception as e:                 # noqa: BLE001  网络/限流/超时/欠费都在这
            last = e
            retryable, why = classify_error(e)
            if METER.rows:
                METER.rows[-1].attempt = attempt
                METER.rows[-1].error = f"{why}: {str(e)[:80]}"
            print(f"  ✗ {stage} 第 {attempt} 次失败：[{why}] {str(e)[:100]}")
            if not retryable:
                # 确定性失败：不重试，直接带上可行动的说明向上报
                raise RuntimeError(f"{stage} 放弃重试：{why}") from e
            if attempt < retries:
                time.sleep(2 * attempt)        # 退避重试，别把限流打成雪崩
    raise RuntimeError(f"{stage} 调用失败（已重试 {retries} 次）：{last}")


def verify_placeholder_parity() -> None:
    """启动自检：模板库的 `{{中文名}}` 必须与 LangChain 认到的变量 1:1。

    这是「变量只有一个主人」的护栏。任何一方改了模板语法，这里立刻炸，
    而不是等到线上发现某个 `{need_example}` 混进了 Prompt。
    """
    print("模板库 × LangChain 占位符对齐自检")
    bad = []
    for role in ("teacher", "teacher_planner", "teacher_unit", "teacher_material",
                 "teacher_outline", "assistant", "assistant_essay", "supervisor", "qa"):
        raw = lib.build(role, {}, fewshot=True)
        tpl = ChatPromptTemplate.from_messages([("human", raw)], template_format="jinja2")
        mine = set(PLACEHOLDER_RE.findall(raw))
        lc = set(tpl.input_variables)
        flag = "✓" if mine == lc else "✗"
        if mine != lc:
            bad.append(role)
        print(f"  {flag} {role:16} 模板库 {len(mine):2} 个 / LangChain {len(lc):2} 个")
    if bad:
        raise SystemExit(f"占位符不对齐，先修模板再跑：{bad}")
    print()


# ======================================================================
# 1. 场景数据（模拟平台侧的真实输入）
# ======================================================================
# 两套场景，`--scenario` 切。默认文科：文理科的差别不只是知识点，还决定挂哪个
# CoT 片段（见 COT_TABLE）——文科单元讲解走 text_reading，理科走 math_steps。
# 两套都留着，才看得出“同一个阶段、换学科、CoT 真的跟着换”。
STUDENT = {"姓名": "小林", "水平": "基础"}

SCENARIOS: dict[str, dict] = {
    "humanities": {
        "学科": "初中语文",
        "学习目标": "掌握文言虚词「之」的常见用法，能在具体句子里判别并说明理由",
        "总周期": "3周",
        "每周可投入时间": "3课时",
        # 批改用的题目来自题库（系统侧给定），**不能**从模型的作业输出里反解
        # ——有标准答案才谈得上判分，这是助教 Agent 的前提。
        "错题": "「予独爱莲之出淤泥而不染」一句中，「之」的用法是什么？",
        "参考答案": "结构助词，用在主语「莲」与谓语「出淤泥」之间，取消句子独立性，不译。",
        "本题满分": 5,
        "学生答案": {
            "全对": "「之」是结构助词，用在主语「莲」和谓语「出淤泥」之间，取消句子独立性，不译。",
            "答偏": "「之」是代词，指代前面的莲花。",
            "白卷": "（学生未作答）",
            "答非所问": "我觉得这篇文章把莲花写得很美，作者很喜欢莲花。",
            "字段缺失": "",        # 系统没把作答传进来 —— 应被入口预检拦下
        },
        "默认用例": "答偏",
        "薄弱点候选列表": "虚词用法判别、主谓之间取消独立性、代词与助词混淆、翻译时漏译",
        "学习数据": "本周登录 4 天，完成作业 9 题，文言实词题正确率 61%，"
                    "第1章测验正确率 68%，错题集中在虚词用法判别",
        "待复习知识点": "「而」的转折与承接、常见实词一词多义",
        "知识点链": "文言虚词「之」→ 主谓之间 → 取消句子独立性",
        # 阶段 3B 用的材料题：文理各一套材料，验「同一个片段、两种形态的材料」
        "材料": "【甲】予独爱莲之出淤泥而不染，濯清涟而不妖。（周敦颐《爱莲说》）\n"
                "【乙】予谓菊，花之隐逸者也；牡丹，花之富贵者也；莲，花之君子者也。（同上）",
        "材料题设问": "两则材料都写莲，作者借莲寄托了什么？请结合材料分点作答。",
        "追问": [
            "老师，「莲之出淤泥而不染」里的「之」如果删掉，句子还成立吗？",
            "那「何陋之有」里的「之」也是这个用法吗？怎么区分？",
        ],
    },
    "science": {
        "学科": "初中物理",
        "学习目标": "掌握浮力与阿基米德原理，能独立完成浮力计算题",
        "总周期": "4周",
        "每周可投入时间": "4课时",
        "错题": "一个物体在空气中重 6 N，浸没在水中时弹簧测力计的示数为 4 N。求物体受到的浮力。",
        "参考答案": "F浮 = G - F示 = 6 N - 4 N = 2 N，方向竖直向上。",
        "本题满分": 5,
        "学生答案": {
            "全对": "F浮 = G - F示 = 6 N - 4 N = 2 N，方向竖直向上。",
            "符号错": "F浮 = 6 + 4 = 10 N。",
            "白卷": "（学生未作答）",
            "答非所问": "我觉得浮力跟物体的质量有关，质量越大浮力越大。",
            "字段缺失": "",
        },
        "默认用例": "符号错",
        "薄弱点候选列表": "浮力方向判断、单位书写、公式变形、受力分析",
        "学习数据": "本周登录 5 天，完成作业 12 题，浮力计算题正确率 58%，"
                    "第1章测验正确率 72%，错题集中在单位书写",
        "待复习知识点": "力的示意图、二力平衡条件",
        "知识点链": "浮力方向 → 阿基米德原理 → 受力平衡",
        "材料": "某同学用同一物体做实验：空气中弹簧测力计示数 6.0 N；浸没在水中示数 4.0 N；"
                "浸没在盐水中示数 3.6 N。",
        "材料题设问": "根据材料，比较该物体在水中与盐水中受到的浮力大小，并说明理由。",
        "追问": [
            "老师，浮力的方向为什么一定是竖直向上的？",
            "那如果物体是漂在水面上的，浮力还等于 G 减 F示 吗？",
        ],
    },
}

SCEN: dict = SCENARIOS["humanities"]        # main() 按 --scenario 覆盖


def qa_vars() -> dict:
    return {"学科": SCEN["学科"], "学生水平": STUDENT["水平"],
            "知识点链": SCEN["知识点链"]}


# ======================================================================
# 2. 各阶段
# ======================================================================
def fewshot_unit_titles() -> set[str]:
    """Few-shot 示例里出现过的单元标题 —— 照抄检测的参考集。

    比对业务字段而不是整段文本，理由见 stage1_plan.checks 的注释。
    """
    path = LIB_DIR / "fewshots" / "teacher_planner.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    titles: set[str] = set()
    for ex in data.get("examples", []):
        try:
            course = parse_blueprint(ex.get("output", ""))
        except Exception:                           # noqa: BLE001
            continue
        titles |= {u["title"] for c in course["chapters"] for u in c.get("units", [])}
    return titles


def v_plan() -> dict:
    """阶段 1 的输入变量。抽成函数是为了让 --dry-run 能导出**同一份**请求体。"""
    return {
        "学科": SCEN["学科"],
        "学习目标": SCEN["学习目标"],
        "总周期": SCEN["总周期"],
        "每周可投入时间": SCEN["每周可投入时间"],
        "学生水平": STUDENT["水平"],
    }


def v_grade(answer_key: str) -> dict:
    """阶段 4 的输入变量。"""
    return {
        "学科": SCEN["学科"],
        "学生姓名": STUDENT["姓名"],
        "学生水平": STUDENT["水平"],
        "错题": SCEN["错题"],
        "学生答案": SCEN["学生答案"][answer_key],
        "参考答案": SCEN["参考答案"],
        "本题满分": SCEN["本题满分"],         # 分母的唯一来源，绝不交给模型编
        "薄弱点候选列表": SCEN["薄弱点候选列表"],
    }


def v_supervise() -> dict:
    """阶段 5 的输入变量。"""
    return {
        "学生姓名": STUDENT["姓名"],
        "学生水平": STUDENT["水平"],
        "学习数据": SCEN["学习数据"],
        "待复习知识点": SCEN["待复习知识点"],
    }


# ---- 文科轨道（阶段 4B）：作文批改 ----
# 与阶段 4 的客观题批改并列：同一个「批改」职责，但判分模型不同。
# 客观题是「对/错 + 得分/满分」，作文是「分维度 + 等级」——拿 assistant 去批作文
# 会硬套对错，所以走 assistant_essay（主观题评分模型）。
ESSAY_TITLE = "以「这也是课堂」为题，写一篇记叙文"
ESSAY_REQUIREMENT = "600 字左右"
ESSAY_DIMENSIONS = {"内容与立意": 15, "结构与条理": 10, "语言与表达": 10,
                    "细节与描写": 5}
ESSAY_TOTAL = 40                     # 【总分】分母的唯一来源，与阶段 4 同理
ESSAY_TEXT = """那天放学，我看见一位老奶奶在路边摔倒了。我赶紧跑过去把她扶起来。
老奶奶说谢谢我。我说不用谢。然后我就回家了。回到家我把这件事告诉了妈妈，妈妈夸我做得对。
我觉得帮助别人很快乐。第二天我到学校，把这件事告诉了同桌。同桌说我很棒。我听了很高兴。
我想以后还要多做好事。"""


def v_essay() -> dict:
    """阶段 4B 的输入变量。维度与总分由调用方给定——评分标准不能交给模型假定。"""
    return {
        "学科": "初中语文",
        "学生姓名": STUDENT["姓名"],
        "学生水平": "中等",
        "题目": ESSAY_TITLE,
        "学生作文": ESSAY_TEXT,
        "字数要求": ESSAY_REQUIREMENT,
        "评分维度": "、".join(f"{k} {v}" for k, v in ESSAY_DIMENSIONS.items()),
        "总分": ESSAY_TOTAL,
    }


ESSAY_CASE = {"总分": ESSAY_TOTAL, "维度": ESSAY_DIMENSIONS}


# ---- 阶段 3B：材料题专项（teacher_material + source_analysis 片段）----
def v_material() -> dict:
    """阶段 3B 的输入变量：材料与设问**各自独立**，不再拼进一个内容槽位。

    角色换成 teacher_material 之后，模板的两个槽位就是 {{材料}} 与 {{设问}}；
    此前借 teacher 时只能把两者拼成一段塞进 {{知识点}}，模型分不清「要讲的材料」
    与「要答的题」，输出也就落在讲解体上（V2.0-说明 缺口 #1）。
    """
    return {
        "学科": SCEN["学科"],
        "学生水平": STUDENT["水平"],
        "材料": SCEN["材料"],
        "设问": SCEN["材料题设问"],
    }


# ---- 文科轨道（阶段 4A）：写作构思，工序在 4B 批改之前 ----
def v_outline() -> dict:
    """阶段 4A 的输入变量：作文题**独立成变量**，不再拼进 {{知识点}}。

    角色换成 teacher_outline 之后，模板的槽位就是 {{题目}}；此前借 teacher 时
    只能把「构思方法 + 作文题」拼成一段塞进 {{知识点}}，产出的是讲解体（同缺口 #1）。
    """
    return {
        "学科": "初中语文",
        "学生水平": STUDENT["水平"],
        "题目": ESSAY_TITLE,
    }


def stage1_plan(use_gate: bool = True) -> dict:
    """M1 规划：出骨架 + 门控蓝图，并用后端闸门校验。"""
    print("\n【阶段 1】M1 规划（teacher_planner）")
    values = v_plan()
    chain = build_chain("teacher_planner", fewshot=True, style="gate")

    def checks(raw: str) -> list[tuple[str, bool, str]]:
        """闸门检查项：(名称, 是否通过, 说明)。全部可程序化判定。

        照抄检测刻意**不用** quality_gate.copy_check（通用 n-gram 重合）——
        实测它会误报：YAML 的字段名与枚举值（estimated_hours / homework_count /
        difficulty 基础 / lesson_status pending …）是任何合规蓝图都必带的 schema，
        归一化后整段 schema 会串成一个 158 字的「公共子串」。
        库自己的规划评测器也是因此改成只比对**单元标题**重合率（eval_planner.py:241）。
        结论：通用照抄检测管自然语言输出，YAML 蓝图要比对业务字段。
        """
        out: list[tuple[str, bool, str]] = []
        try:                                        # H1+H2：蓝图可解析且全是 pending
            course = parse_blueprint(raw)
            out.append(("蓝图可解析 / 规划期未偷跑内容", True, ""))
        except Exception as e:                      # noqa: BLE001
            out.append(("蓝图可解析 / 规划期未偷跑内容", False, str(e)[:200]))
            return out

        ref = fewshot_unit_titles()
        gen = [u["title"] for c in course["chapters"] for u in c.get("units", [])]
        dup = [t for t in gen if t in ref]
        ratio = len(dup) / len(gen) if gen else 0
        out.append(("非照抄 Few-shot（单元标题重合<50%）", ratio < 0.5,
                    f"{len(dup)}/{len(gen)} 个单元标题与示例相同"
                    + (f"：{dup[:3]}" if dup else "")))
        return out

    feedback: list = []
    for attempt in range(1, (2 if use_gate else 0) + 1):
        raw = call(f"1-规划-第{attempt}轮", "teacher_planner", chain, values,
                   extra={"feedback": feedback})
        result = checks(raw)
        for name, ok, why in result:
            print(f"    {'✓' if ok else '✗'} {name}" + (f" — {why}" if why else ""))
        if all(ok for _, ok, _ in result):
            print("    闸门通过")
            return parse_blueprint(raw)

        if attempt > 1 or not use_gate:
            raise SystemExit("闸门未通过且已无重试额度")
        # 回投：既给模型的原文（续上下文），也给具体不合格项
        print("    闸门未过 → 把问题回投给模型重做")
        feedback = [
            AIMessage(content=raw),
            HumanMessage(content="你的输出存在以下问题，请修正后重新输出完整结果：\n"
                                 + "\n".join(f"- {n}：{w}" for n, ok, w in result if not ok)),
        ]
    raise SystemExit("unreachable")


def stage2_gate(course: dict) -> dict:
    """门控判定：后端硬约束，模型无权判定学生是否通过。"""
    print("\n【阶段 2】门控判定（后端硬约束，不问模型）")
    chapters = [c["id"] for c in course["chapters"]]
    print(f"  蓝图共 {len(chapters)} 章：{chapters}")

    # 首考 58% 未达标 → 锁住；补完薄弱点重考 72% → 解锁
    for rate in (0.58, 0.72):
        progress = {"ch01": {"rate": rate, "weak": ["单位书写"],
                             "units_done": [u["id"] for u in course["chapters"][0]["units"]]}}
        ok, why = can_unlock(course, "ch02", progress)
        print(f"  第1章正确率 {rate:.0%} → ch02 {'解锁' if ok else '锁住'}：{why}")

    # 带着已解锁的状态继续
    progress = {
        "ch01": {"rate": 0.72, "weak": ["单位书写"],
                 "units_done": [u["id"] for u in course["chapters"][0]["units"]]},
    }
    print("  当前关卡地图：")
    for line in course_status(course, progress):
        print("    " + line)
    return progress


def stage3_render_unit(course: dict, progress: dict) -> str:
    """M2 单元渲染：一次只渲染一个单元，难度由上一章学情驱动。"""
    print("\n【阶段 3】M2 单元渲染（teacher_unit）")
    target = next_unlocked_unit(course, progress)
    if not target:
        raise SystemExit("没有可渲染的单元：课程已完结或未解锁")
    chapter_id, idx = target

    values = unit_variables(course, chapter_id, idx, progress)
    print(f"  目标单元：{chapter_id} / 第 {idx + 1} 个｜{values['本单元名称']}"
          f"｜{values['本单元课时']} 课时｜上一章 {values['上一章测验结果']}")
    # 刻意不挂 Few-shot：消融实验显示格式合规率不因此变化，却能省约 40% token，
    # 并消除同题撞车抄袭的风险（库自带的 demo 也是这么调的）。
    # CoT 与 Few-shot 是两回事：CoT 管「怎么推」，不多带一份学科材料，所以照挂。
    cot, note = cot_for(values["学科"], "explain", "3-单元渲染")
    print(f"  {note}")
    chain = build_chain("teacher_unit", fewshot=False, cot=cot)
    return call("3-单元渲染", "teacher_unit", chain, values)


def stage3b_material(use_gate: bool = True) -> str:
    """材料题专项：交出一份「结论 + 依据」的分点作答，并由校验器把关。

    文理两套材料共用同一个片段，因为 COT_TABLE 把 (science|humanities, material)
    都指向 source_analysis：理科材料题是实验数据，文科是文本/史料，五步骨架一样。

    为什么这里需要闸门：材料题最典型的走形不是格式错，而是**有结论没依据**
    （或依据指不回材料）——看上去像答案，实际不可核对。
    「每点是否带依据、依据能不能指回材料」是可程序化判定的，所以回投重做，
    而不是靠人拿眼看（同阶段 4／4B 的做法）。
    """
    print("\n【阶段 3B】材料题专项（teacher_material，结论+依据的分点作答）")
    values = v_material()
    cot, note = cot_for(values["学科"], "material", "3B-材料题")
    print(f"  {note}")
    chain = build_chain("teacher_material", fewshot=True, cot=cot, style="gate")
    case = {"材料": values["材料"], "设问": values["设问"]}

    feedback: list = []
    for attempt in range(1, (2 if use_gate else 0) + 1):
        raw = call(f"3B-材料题-第{attempt}轮", "teacher_material", chain, values,
                   extra={"feedback": feedback})
        checks = validate_material(raw, case)
        failed = [c for c in checks if not c[1]]
        _, bodies = split_by_names(raw, MATERIAL_SECTIONS)
        n_points = len(split_points(bodies.get("作答", "")))
        print(f"    质检 {len(checks) - len(failed)}/{len(checks)}（{n_points} 个作答点）")
        if not failed:
            print("    闸门通过（3 模块齐全，每点依据都指回材料）")
            return raw
        for name, _, detail in failed:
            print(f"    ✗ {name}  {detail}")
        if attempt > 1 or not use_gate:
            print(f"    ⚠ 闸门未过但已无重试额度：{[c[0] for c in failed]}")
            return raw
        print("    闸门未过 → 回投：修正后重做")
        feedback = [
            AIMessage(content=raw),
            HumanMessage(content="你的输出存在以下问题，请修正后重新输出完整结果：\n"
                                 + "\n".join(f"- {c[0]}：{c[2]}" for c in failed)),
        ]
    raise SystemExit("unreachable")


def stage4_grade(answer_key: str, use_gate: bool = True) -> dict:
    """助教批改：模板出结构，后端解析并核对分母。"""
    print(f"\n【阶段 4】助教批改（assistant）｜用例：{answer_key}")
    values = v_grade(answer_key)
    cot, note = cot_for(values["学科"], "selfcheck", "4-批改")
    print(f"  {note}")
    chain = build_chain("assistant", fewshot=True, cot=cot, style="gate")

    feedback: list = []
    for attempt in range(1, (2 if use_gate else 0) + 1):
        raw = call(f"4-批改-第{attempt}轮", "assistant", chain, values,
                   extra={"feedback": feedback})
        parsed = parse_grade(raw)
        print(f"    解析结果：{parsed}")
        missing = [s for s in ASSISTANT_SECTIONS if s not in parsed["sections"]]
        problems = []
        if missing:
            problems.append(f"缺少模块：{missing}")
        if parsed["full"] != SCEN["本题满分"]:
            problems.append(f"【得分】分母 {parsed['full']} ≠ 传入满分 {SCEN['本题满分']}")
        if not problems:
            print("    闸门通过（6 模块齐全，分母与传入值一致）")
            return parsed
        if attempt > 1 or not use_gate:
            print(f"    ⚠ 闸门未过但已无重试额度：{problems}")
            return parsed
        print(f"    闸门未过 → 回投：{problems}")
        feedback = [
            AIMessage(content=raw),
            HumanMessage(content="你的输出存在以下问题，请修正后重新输出完整结果：\n"
                                 + "\n".join(f"- {p}" for p in problems)),
        ]
    raise SystemExit("unreachable")


def parse_grade(raw: str) -> dict:
    """把模板的自然语言输出解析成结构化结果。

    刻意不用 PydanticOutputParser：模板的 6 模块格式是**契约**，
    硬塞 JSON 会和提示词打架，两边都变形。生产里就是「模板定格式、后端解析」。
    """
    found, bodies = split_by_names(raw, ASSISTANT_SECTIONS)
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", bodies.get("得分", ""))
    verdict = re.search(r"(部分正确|正确|错误|^对|^错|无法判定)", bodies.get("答案判定", "").strip())
    low_conf = bool(re.search(r"低|不确定|人工复核", bodies.get("置信度", "")))
    return {
        "score": float(m.group(1)) if m else None,
        "full": float(m.group(2)) if m else None,
        "verdict": verdict.group(1) if verdict else None,
        "low_confidence": low_conf,
        "sections": found,
        "raw": raw,
    }


def stage4a_outline(use_gate: bool = True) -> str:
    """写作构思：交一份可执行的写作方案（构思体），并由校验器把关。

    工序上排在 4B 之前：先帮学生把「怎么想」想清楚，再批改他写的成稿。

    为什么这里需要闸门：构思最容易走形的地方不是格式，而是
    ①**写成教案**（借 teacher 渲染时必然发生）、②**立意骑墙**、
    ③**提纲写成段落实文**（看上去像构思，实际不能用）。三条都可程序化判定。
    """
    print("\n【阶段 4A】写作构思（teacher_outline，审题→立意→提纲→素材→首尾）")
    values = v_outline()
    cot, note = cot_for("初中语文", "write", "4A-写作构思")
    print(f"  {note}")
    chain = build_chain("teacher_outline", fewshot=True, cot=cot, style="gate")
    case = {"题目": values["题目"]}

    feedback: list = []
    for attempt in range(1, (2 if use_gate else 0) + 1):
        raw = call(f"4A-写作构思-第{attempt}轮", "teacher_outline", chain, values,
                   extra={"feedback": feedback})
        checks = validate_outline(raw, case)
        failed = [c for c in checks if not c[1]]
        _, bodies = split_by_names(raw, OUTLINE_SECTIONS)
        print(f"    质检 {len(checks) - len(failed)}/{len(checks)}"
              f"（提纲 {len(outline_paragraphs(bodies.get('提纲', '')))} 段）")
        if not failed:
            print("    闸门通过（5 模块齐全，提纲标了重点，素材都写明证明了什么）")
            return raw
        for name, _, detail in failed:
            print(f"    ✗ {name}  {detail}")
        if attempt > 1 or not use_gate:
            print(f"    ⚠ 闸门未过但已无重试额度：{[c[0] for c in failed]}")
            return raw
        print("    闸门未过 → 回投：修正后重做")
        feedback = [
            AIMessage(content=raw),
            HumanMessage(content="你的输出存在以下问题，请修正后重新输出完整结果：\n"
                                 + "\n".join(f"- {c[0]}：{c[2]}" for c in failed)),
        ]
    raise SystemExit("unreachable")


def stage4b_essay(use_gate: bool = True) -> dict:
    """作文批改（文科轨道）：闸门直接复用库自带的校验器，不另写一份断言。

    `validate_essay` 与 `eval_essay.py` 用的是同一套 13 项检查（维度之和=总分、
    等级与总分占比自洽、亮点/逐条问题必须引原文、不代写全文…）。自己再写一遍的
    后果是两套判据慢慢漂移，然后“评测过了、线上没过”。
    """
    print("\n【阶段 4B】作文批改（assistant_essay，文科轨道）")
    values = v_essay()
    cot, note = cot_for(values["学科"], "selfcheck", "4B-作文批改")
    print(f"  {note}｜维度 {values['评分维度']}｜总分 {ESSAY_TOTAL}")
    chain = build_chain("assistant_essay", fewshot=True, cot=cot, style="gate")

    feedback: list = []
    for attempt in range(1, (2 if use_gate else 0) + 1):
        raw = call(f"4B-作文批改-第{attempt}轮", "assistant_essay", chain, values,
                   extra={"feedback": feedback})
        checks = validate_essay(raw, ESSAY_CASE)
        failed = [(n, d) for n, ok, d in checks if not ok]
        print(f"    闸门：{len(checks) - len(failed)}/{len(checks)} 项通过")
        for n, d in failed:
            print(f"      ✗ {n}" + (f" — {d}" if d else ""))
        if not failed:
            print("    闸门通过（7 模块齐全，维度之和=总分，等级与占比自洽）")
            return {"checks": checks, "raw": raw}
        if attempt > 1 or not use_gate:
            print(f"    ⚠ 闸门未过但已无重试额度：{[n for n, _ in failed]}")
            return {"checks": checks, "raw": raw}
        print(f"    闸门未过 → 回投 {len(failed)} 条问题")
        feedback = [
            AIMessage(content=raw),
            HumanMessage(content="你的输出存在以下问题，请修正后重新输出完整结果：\n"
                                 + "\n".join(f"- {n}：{d}" for n, d in failed)),
        ]
    raise SystemExit("unreachable")


def stage5_supervise(progress: dict) -> str:
    """督学提醒：输入是行为数据，不是对话。"""
    print("\n【阶段 5】督学提醒（supervisor）")
    values = v_supervise()
    _, note = cot_for(SCEN["学科"], "nudge", "5-督学提醒")
    print(f"  {note}")
    chain = build_chain("supervisor", fewshot=True)
    return call("5-督学提醒", "supervisor", chain, values)


def stage6_followup() -> list[str]:
    """学生追问：模板当 system 提示，历史消息由调用方维护。

    这就是「一个 Prompt 模板 + 一段对话状态」。要落到多用户/可持久化，
    换成 LangChain 的 RunnableWithMessageHistory 即可，模板侧不用改。
    """
    print("\n【阶段 6】学生追问（qa，多轮）")
    _, note = cot_for(SCEN["学科"], "guide", "6-答疑")
    print(f"  {note}")
    # qa 模板现在自带【本次提问】段，所以**当前这一问**走变量，历史只因携带上一轮；
    # 两者分工不重叠（否则同一条提问会同时出现在历史与任务段里）。
    chain = build_chain("qa", fewshot=True, style="chat")
    history: list = []
    replies: list[str] = []
    for i, question in enumerate(SCEN["追问"], 1):
        # 追问阶段的知识点链由题目上下文给出，不由模型自己猜
        values = dict(qa_vars(), 学生提问=question)
        reply = call(f"6-答疑-第{i}轮", "qa", chain, values, extra={"history": list(history)})
        history += [HumanMessage(content=question), AIMessage(content=reply)]
        replies.append(reply)
    print(f"  对话历史累计 {len(history)} 条消息")
    return replies


# ======================================================================
# 3. 主流程
# ======================================================================
def report(out_dir: Path) -> None:
    """时间线 + 成本表 + 落盘。失败的部分链路也要留下痕迹。"""
    print("\n" + "=" * 72)
    print("调用账单")
    print("=" * 72)
    print(f"  {'阶段':<20}{'轮次':<6}{'prompt':>8}{'completion':>12}{'耗时':>9}")
    total_p = total_c = 0
    total_s = 0.0
    for r in METER.rows:
        total_p += r.prompt_tokens
        total_c += r.completion_tokens
        total_s += r.seconds
        print(f"  {r.stage:<20}{r.attempt:<6}{r.prompt_tokens:>8}"
              f"{r.completion_tokens:>12}{r.seconds:>8.1f}s"
              + (f"  ⚠ {r.error[:40]}" if r.error else ""))
    print(f"  {'合计':<20}{len(METER.rows):<6}{total_p:>8}{total_c:>12}{total_s:>8.1f}s")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 输入与输出写在同一个文件里：出问题时不用去别处对时间线。
    # 每个阶段取**最后一次尝试**的消息 —— 那一次才是产出这段输出的那次。
    last_msgs: dict[str, list[tuple[str, str]]] = {}
    retried: list[tuple[str, int, list[tuple[str, str]]]] = []
    for stage, attempt, msgs in REQUESTS:
        last_msgs[stage] = msgs
        if attempt > 1:                 # 判定看 attempt，不靠阶段名里的字样猜
            retried.append((stage, attempt, msgs))
    for i, (stage, text) in enumerate(STAGES, 1):
        safe = re.sub(r"[^\w\-]+", "_", stage)
        blocks = [f"# {stage}\n"]
        msgs = last_msgs.get(stage)
        if msgs:
            blocks.append(f"\n## 输入（实际发给模型的消息，共 {len(msgs)} 条）\n")
            for kind, content in msgs:
                blocks.append(f"\n### 【{kind}】\n\n{content}\n")
        else:
            blocks.append("\n## 输入\n\n（未记录：调用时留痕失败）\n")
        blocks.append(f"\n## 输出（模型原文，{len(text)} 字符）\n\n{text}\n")
        (out_dir / f"{i:02d}_{safe}.md").write_text("".join(blocks), encoding="utf-8")
    # 被闸门打回、回投后重做的那几轮：它们的输出不在 STAGES 里（没通过），
    # 但“回投了什么问题”正是解释输出为何变样的关键，单独留一份。
    if retried:
        parts = ["# 重试回投的请求\n\n下列轮次的输出被闸门判不合格，问题回投后重做；"
                 "此处保留重做时发出去的请求。\n"]
        for stage, attempt, msgs in retried:
            parts.append(f"\n## {stage}（第 {attempt} 次尝试）\n")
            for kind, content in msgs:
                parts.append(f"\n### 【{kind}】\n\n{content}\n")
        (out_dir / "99_重试回投.md").write_text("".join(parts), encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps({
        "calls": [r.__dict__ for r in METER.rows],
        "total_seconds": round(total_s, 1),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    report_cot_usage()
    print(f"\n  原始输出已落盘：{out_dir}")


def soft(fn, *a, **kw):
    """非关键阶段降级：失败就记账跳过，不打断整条链路。

    关键路径（规划/渲染/批改）失败必须中断——它们产出的是学生要用的东西；
    督学提醒、追问答疑属于锦上添花，欠费/超时不该让前面的成果白跑。
    生产里这里应该是告警 + 重试队列，而不是静默吞掉。
    """
    try:
        return fn(*a, **kw)
    except Exception as e:                          # noqa: BLE001
        print(f"  ⚠ {fn.__name__} 降级跳过：{e}")
        return None


def render_request(chain, payload: dict) -> list:
    """把链首的 PromptTemplate 渲染成「将发给模型的消息列表」，不调 LLM。

    这是回答「我到底发了什么」的唯一可靠方式：走的是和真实调用**同一条**
    模板渲染路径（含 jinja2 变量替换、fewshot 拼接、feedback/history 消息），
    差别仅是最后一步没发出去。
    """
    return chain.steps[0].format_messages(**payload)   # steps[0] = PromptTemplate


def _model_output_of(text: str) -> str:
    """从阶段落盘文件里只取**模型输出**那一段。

    阶段文件现在同时含「输入」与「输出」，而输入里嵌着 Few-shot 示例——那份示例
    也带 `course:`（库为防照抄刻意用了别的学科）。整份文件丢给 parse_blueprint，
    解析到的是示例那份，不是模型这次产出的那份：看着像成功，其实在验错东西。
    """
    marker = "## 输出（模型原文"
    if marker in text:
        return text.split(marker, 1)[1].split("\n", 1)[1]
    return text


def demo_course() -> tuple[dict, str]:
    """取一份**真实形状**的蓝图，返回 (course, 来源说明)。

    优先级：与当前场景**同科**的上次真跑输出 > 任意上次真跑 > Few-shot 示例。
    为什么同科优先：文科场景拿物理蓝图去渲染 M2，导出的是别的学科的 prompt，
    而本工具的全部意义是「所见即所发」。不同科时会在来源说明里显式标注 ⚠。

    为什么不能直接用 Few-shot 示例当默认：那份示例刻意用别的学科（库为防照抄
    而这么写的），照它映射出的 M2 变量会是「初中生物·光合作用」，与本次场景不符。
    """
    found: list[tuple[Path, dict]] = []
    for f in sorted((Path(__file__).parent / "runs").glob("*/01_1-规划-第1轮.md"),
                    reverse=True):
        try:
            found.append((f, parse_blueprint(_model_output_of(f.read_text(encoding="utf-8")))))
        except Exception:                           # noqa: BLE001
            continue
    for f, course in found:
        if course["course"].get("subject") == SCEN["学科"]:
            return course, f"上次真跑（同科）：{f.parent.name}"
    if found:
        f, course = found[0]
        return course, (f"上次真跑：{f.parent.name}｜⚠ 学科不一致"
                        f"（{course['course'].get('subject')} ≠ {SCEN['学科']}）"
                        "，本次导出仅演示形状；跑一次真调用后即会一致")
    path = LIB_DIR / "fewshots" / "teacher_planner.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    course = parse_blueprint(data["examples"][0]["output"])
    return course, (f"Few-shot 示例（尚未真跑过，学科是 "
                    f"{course['course'].get('subject')}，与本次场景不符）")


def dump_requests(out_dir: Path, answer_key: str) -> None:
    """导出每个阶段的完整请求体 + 变量清单。"""
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    # M2 的变量依赖蓝图，用真实蓝图 + 模拟学情映射出 12 个变量
    course, source = demo_course()
    ch1 = course["chapters"][0]
    progress = {ch1["id"]: {"rate": 0.72, "weak": ["单位书写"],
                            "units_done": [u["id"] for u in ch1["units"]]}}
    unit_values = unit_variables(course, course["chapters"][1]["id"], 0, progress)
    print(f"  （蓝图来源：{source}）")

    plan = [
        ("1-规划", "teacher_planner", v_plan(), True,
         pick_cot(SCEN["学科"], "plan"), "gate", {}),
        ("3-单元渲染", "teacher_unit", unit_values, False,
         pick_cot(unit_values["学科"], "explain"), "one_shot", {}),
        ("3B-材料题", "teacher_material", v_material(), True,
         pick_cot(SCEN["学科"], "material"), "gate", {}),
        ("4-批改", "assistant", v_grade(answer_key), True,
         pick_cot(SCEN["学科"], "selfcheck"), "gate", {}),
        ("4A-写作构思", "teacher_outline", v_outline(), True,
         pick_cot("初中语文", "write"), "gate", {}),
        ("4B-作文批改", "assistant_essay", v_essay(), True,
         pick_cot("初中语文", "selfcheck"), "gate", {}),
        ("5-督学提醒", "supervisor", v_supervise(), True,
         pick_cot(SCEN["学科"], "nudge"), "one_shot", {}),
        ("6-答疑", "qa", dict(qa_vars(), 学生提问=SCEN["追问"][0]), True,
         pick_cot(SCEN["学科"], "guide"), "chat",
         {"history": []}),   # 第一轮无历史：当前提问走变量，不进历史，避免同一句出现两次
    ]
    all_vars = {}
    for i, (stage, role, values, fs, cot, style, extra) in enumerate(plan, 1):
        chain = build_chain(role, fewshot=fs, cot=cot, style=style)
        msgs = render_request(chain, {**values, **extra})
        total = sum(len(m.content) for m in msgs)

        blocks = [f"# {stage}\n\n"
                  f"- role(模板): {role}\n- fewshot: {fs}\n- cot: {cot}\n"
                  f"- 消息送法: {style}\n- 消息条数: {len(msgs)}\n"
                  f"- 总字符: {total}\n"]
        for m in msgs:
            blocks.append(f"\n{'=' * 70}\n【{type(m).__name__}】\n{'=' * 70}\n{m.content}\n")
        (prompts_dir / f"{i:02d}_{stage}.md").write_text("".join(blocks), encoding="utf-8")

        all_vars[stage] = {k: v for k, v in values.items()}
        if cot:                      # 记账：dry-run 也要能看出哪些片段真被用上了
            COT_USED.setdefault(cot, []).append(stage)
        print(f"  {stage:12} {role:16} 消息 {len(msgs)} 条｜{total:>6} 字符"
              f"｜cot={cot or '—'}")

    (prompts_dir / "变量清单.json").write_text(
        json.dumps(all_vars, ensure_ascii=False, indent=2), encoding="utf-8")
    report_cot_usage()
    print(f"\n  变量清单：{prompts_dir / '变量清单.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="模板库 × LangChain 真实调用场景")
    ap.add_argument("--dry-run", action="store_true", help="只组装 Prompt，不调 API")
    ap.add_argument("--answer", default=None,
                    help="学生答案用例（默认取该场景的典型错解；可选值随 --scenario 变）")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="humanities",
                    help="场景：humanities＝初中语文（默认）· science＝初中物理")
    ap.add_argument("--no-gate", action="store_true", help="关闭闸门回投重试")
    ap.add_argument("--cot", choices=["auto", "off"], default="auto",
                    help="auto＝按学科轨道自动选 CoT 片段；off＝不挂，对比差别")
    ap.add_argument("--cot-table", action="store_true",
                    help="只打印 CoT 选择表就退出（不调 API）")
    args = ap.parse_args()

    global COT_MODE, SCEN
    COT_MODE = args.cot
    SCEN = SCENARIOS[args.scenario]
    answer_key = args.answer or SCEN["默认用例"]
    if answer_key not in SCEN["学生答案"]:
        raise SystemExit(f"--answer 只能是：{sorted(SCEN['学生答案'])}"
                         f"（当前场景 {args.scenario}）")
    if args.cot_table:
        print_cot_table()
        return
    verify_placeholder_parity()
    use_gate = not args.no_gate

    if args.dry_run:
        out_dir = (Path(__file__).parent / "runs"
                   / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_dryrun"))
        print("DRY RUN：导出将发给模型的完整请求体，不发请求\n")
        dump_requests(out_dir, answer_key)
        print(f"\n  完整请求体已导出：{out_dir / 'prompts'}")
        print("  真跑请去掉 --dry-run")
        return

    print("\n" + "=" * 72)
    print(f"真实调用场景开始｜学生 {STUDENT['姓名']}（{STUDENT['水平']}）"
          f"｜用例 {answer_key}｜闸门 {'开' if use_gate else '关'}")
    print("=" * 72)

    failed = False
    out_dir = Path(__file__).parent / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        course = stage1_plan(use_gate)
        progress = stage2_gate(course)
        stage3_render_unit(course, progress)
        stage3b_material(use_gate)                     # 材料题：接上 source_analysis + 闸门
        stage4_grade(answer_key, use_gate)
        stage4a_outline(use_gate)                       # 写作构思：接上 essay_outline + 闸门
        stage4b_essay(use_gate)                        # 文科轨道，与阶段 4 并列
        soft(stage5_supervise, progress)               # 非关键：失败不阻断
        soft(stage6_followup)
    except RuntimeError as e:
        # 关键路径（规划/渲染/批改）挂了：不再往下烧 token，但账单和已产出内容仍要落盘
        failed = True
        print(f"\n[关键阶段失败，中断链路] {e}")
    finally:
        report(out_dir)
    sys.exit(3 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        # 入口预检的失败长得就是这样：调用前拦下，一个 token 都没花
        print(f"\n[入口预检拦截] {e}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
