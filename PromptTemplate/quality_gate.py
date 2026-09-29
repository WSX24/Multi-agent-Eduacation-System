#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量闸门 —— 把「LLM 声明、后端校验、不过就回投重试」做成可复用设施

本项目反复验证出的架构结论：

    LLM 的概率性行为**不能靠提示词变成确定行为**。
    凡是硬约束（格式合规、不得照抄、门控未达标不得放行），
    都必须由后端校验；校验不过就把问题回投给模型重做。

本模块提供三件事：

1. ``copy_hits`` —— **照抄检测**。算法化（最长公共子串），不靠人工维护关键词表。
   之前用手写 token 列表做判据，既不通用又制造过误判（把「单元名」当成抄袭信号）。
2. ``QualityGate`` —— 通用「生成 → 校验 → 回投重试」闸门。
3. ``blank_inputs`` / ``invalid_inputs`` / ``require_inputs`` —— **入口预检**。
   调用前的确定性拒绝，不让两类调用方错误进入模型：
   （a）**输入缺失**——模型会编造内容填补空白；
   （b）**输入类型/单位不符**——如把「每周三小时」传给要吃数字的字段，
   模型会自己猜一个数，而且前后不一致。

用法::

    from quality_gate import QualityGate, load_fewshot_refs

    gate = QualityGate(client, "deepseek-chat")
    refs = load_fewshot_refs(lib, "teacher_sprint")
    result = gate.generate(prompt, checks=my_validator, copy_refs=refs)
    if not result.passed:
        ...  # result.failures 里是未通过项，result.copy_hits 是抄袭证据
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 中文语境下，超过这个长度的逐字重合基本不可能是巧合
DEFAULT_MIN_COMMON = 15

# 模板强制要求一字不差的部分：Markdown 表格行、分节标题。
# 必须先从两侧剔掉，否则闸门会把「正确的格式」判成抄袭，
# 于是永远不收敛——**过严的闸门比没有闸门更糟**。
_TABLE_LINE_RE = re.compile(r"^[ 	]*\|.*\|[ 	]*$", re.M)
_SECTION_HEAD_RE = re.compile(r"【[^】]{1,14}】")
_PLACEHOLDER_RE = re.compile(r"<[^>\n]{1,24}>")


def strip_scaffolding(text: str) -> str:
    """剔掉模板要求的结构性外壳，只留实质内容，供照抄检测使用。

    三类都算外壳：Markdown 表格行、分节标题、``<占位符>``。
    前两类是格式要求，第三类是「骨架示例」的挖空位——都不含实质内容。
    占位符若被原样抄进输出，由 ``placeholder_leaks`` 单独负责检出。
    """
    text = _TABLE_LINE_RE.sub("", text)
    text = _SECTION_HEAD_RE.sub("", text)
    return _PLACEHOLDER_RE.sub("", text)


def placeholder_leaks(text: str) -> list[str]:
    """检出输出里残留的 ``<占位符>``（骨架示例特有的失败模式）。"""
    return sorted(set(_PLACEHOLDER_RE.findall(text)))


_NON_WORD_RE = re.compile(r"[\W_]+")


def normalize(text: str) -> str:
    """剔外壳 + 去标点空白，只留字词。

    必须去标点：骨架示例剔除占位符后会剩下「M1：。M2：。M3：。」这类
    纯标点残渣，n-gram 会把它们当成大段重合，制造假阳性。
    照抄检测关心的是「字词是否照搬」，不是标点。
    """
    return _NON_WORD_RE.sub("", strip_scaffolding(text))


def copy_hits(raw: str, refs: list[str], min_common: int = DEFAULT_MIN_COMMON) -> list[str]:
    """返回 ``raw`` 与参考文本（Few-shot 示例）逐字重合的片段。

    用最小公共子串的 n-gram 求交，不依赖任何手工词表：
    先取每个参考文本的全部 n-gram 存进集合，再扫描 raw 的 n-gram 是否命中，
    最后把连续命中合并成片段（便于人读）。

    这替代了之前手写的「特有算式/词句」列表——那种做法既不通用，
    又会把「单元标题」这类正常重复误判成抄袭。
    """
    ref_grams: set[str] = set()
    for r in refs:
        t = normalize(r)
        ref_grams.update(t[i:i + min_common] for i in range(len(t) - min_common + 1))
    if not ref_grams:
        return []

    target = normalize(raw)
    hits: list[str] = []
    i = 0
    n = len(target)
    while i <= n - min_common:
        if target[i:i + min_common] in ref_grams:
            # 尽量向右延伸，拿到完整重合片段
            j = i + min_common
            while j < n and target[j - min_common + 1:j + 1] in ref_grams:
                j += 1
            hits.append(target[i:j])
            i = j
        else:
            i += 1

    # 去重、去掉被包含的短片段，按长度降序
    uniq: list[str] = []
    for h in sorted(set(hits), key=len, reverse=True):
        if not any(h in u for u in uniq):
            uniq.append(h)
    return uniq


def load_fewshot_refs(lib, name: str) -> list[str]:
    """读取某个 Few-shot 文件里全部示例输出，作为照抄检测的参考文本。"""
    path: Path = lib.root / "fewshots" / f"{name}.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [ex.get("output", "") for ex in data.get("examples", []) if ex.get("output")]


# ────────────────── YAML 蓝图专用的照抄检测 ──────────────────
# 为什么通用 copy_hits 不能用在 YAML 蓝图上（实测踩过）：
#   蓝图的字段名与枚举值（estimated_hours / homework_count / difficulty 基础 /
#   lesson_status pending …）是**任何合规蓝图都必带的 schema**。归一化去掉标点后，
#   整段 schema 会串成一个 150+ 字的「公共子串」，于是闸门 100% 误报、
#   回投重做也永远修不好——**过严的闸门比没有闸门更糟**。
# 正确做法：只比对**业务字段**（章标题 / 单元标题），schema 不参与。
#   库自己的规划评测器（eval_planner.py）一直是这么做的，本函数把它抽出来共享，
#   避免下游用户再踩一次，也避免两处各写一份而逐渐偏离。

BLUEPRINT_FIELDS = ("units",)          # 默认只比单元标题；传 ("chapters", "units") 连章标题一起比


def blueprint_titles(raw: str, fields: tuple[str, ...] = BLUEPRINT_FIELDS) -> list[str]:
    """从 M1 输出里取出可比对的业务字段（章/单元标题）。

    解析失败（蓝图缺失或非法）返回空表，由调用方决定是否另行报错——
    本函数只负责抽字段，不承担结构校验。
    """
    marker = raw.find("course:")
    if marker < 0:
        return []
    try:
        data = yaml.safe_load(raw[marker:])
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    titles: list[str] = []
    if "chapters" in fields:
        titles += [c["title"] for c in data.get("chapters", []) if c.get("title")]
    if "units" in fields:
        for c in data.get("chapters", []):
            titles += [u["title"] for u in c.get("units", []) if u.get("title")]
    return titles


def copy_check_yaml(raw: str, name: str, lib, *,
                    fields: tuple[str, ...] = BLUEPRINT_FIELDS,
                    min_ratio: float = 0.5):
    """**YAML 蓝图版**照抄检测：只比业务字段的重合率，与 ``copy_check`` 同形状。

    判定口径：生成蓝图里的章节/单元标题，落在 Few-shot 示例同名集合里的比例
    必须 ``< min_ratio``（默认 50%）。同题撞车时这个比例会跑到 100%。

    返回 ``(检查项名, 是否通过, 说明)``，可直接塞进 ``QualityGate.generate(checks=...)``。
    """
    refs = load_fewshot_refs(lib, name)
    ref_titles: set[str] = set()
    for r in refs:
        ref_titles.update(blueprint_titles(r, fields))

    gen = blueprint_titles(raw, fields)
    name_of = "章/单元标题" if len(fields) > 1 else "单元标题"
    if not gen:
        return (f"非照抄 Few-shot({name})", False,
                f"未能从输出中取出{name_of}（蓝图缺失或不可解析）")

    dup = [t for t in gen if t in ref_titles]
    ratio = len(dup) / len(gen)
    detail = (f"{len(dup)}/{len(gen)} 个{name_of}与示例相同"
              + (f"：{dup[:3]}" if dup else ""))
    return (f"非照抄 Few-shot({name})", ratio < min_ratio, detail)


def copy_check(raw: str, name: str, lib, min_common: int = DEFAULT_MIN_COMMON):
    """生成一条可塞进校验器的检查项：是否照抄了指定 Few-shot。"""
    refs = load_fewshot_refs(lib, name)
    hits = copy_hits(raw, refs, min_common)
    return (
        f"非照抄 Few-shot({name})",
        not hits,
        (f"与示例逐字重合 {len(hits)} 处，最长 {max((len(h) for h in hits), default=0)} 字："
         + " ／ ".join(h[:40] for h in hits[:2])) if hits else "",
    )


def blank_inputs(lib, role: str, variables: dict) -> list[str]:
    """**入口预检**：模板需要、但调用方没给或给了空值的变量。

    为什么必须在入口拦：评测实测发现，把「学生答案」留空时，模型会拿旁边
    的「参考答案」当学生答案，给出「10/10、判定对、置信度高」并编造
    「你正确标注了方向」这类不存在的内容——**学生没作答却拿满分**。

    这类问题靠提示词兜不住（模板里已写明「任一项缺失不得猜测、直接标低置信度」，
    模型照样无视）。字段缺失属于调用方的错误，应当在调用前就拒绝。

    只负责「缺不缺」；数值字段的单位/取值域是否合规由 ``invalid_inputs`` 负责。

    用法::

        blanks = blank_inputs(lib, "assistant", variables)
        if blanks:
            raise ValueError(f"缺少必要输入：{blanks}")
    """
    return sorted(p.name for p in lib.check_inputs(role, variables) if p.kind == "missing")


def invalid_inputs(lib, role: str, variables: dict):
    """**入口预检（类型层）**：格式不合契约的变量，返回 ``InputProblem`` 列表。

    与 ``blank_inputs`` 互补。缺项是「没给」，本函数管的是「给了但不对」——
    模板为输出写足了口径，输入却没有类型与单位时，数值字段会静默降级：

        {{总周期}} × {{每周可投入时间}}   传「每周三小时」→ 乘法失效
        正确率 ≥ 85% → 升档               传「还行」→ 分支不命中，不告警

    两种失败都不报错，只是结果悄悄变差，因此必须在调用前拦住。
    """
    return lib.invalid_inputs(role, variables)


def require_inputs(lib, role: str, variables: dict) -> dict:
    """入口预检的强制版：缺项或格式不符立即抛 ``ValueError``，否则原样返回变量表。

    供链路代码一行接入（``require_inputs(lib, role, v)``），保证「输入不可用」
    在**所有**模板调用点都是同一个确定性行为，而不是各调用方各写一遍。
    错误信息一次报全（缺项 + 类型问题），不让学生看到二次试错。
    """
    problems = lib.check_inputs(role, variables)
    if not problems:
        return variables
    lines = [f"    ・{p}" for p in problems]
    raise ValueError(
        f"输入不合契约（{role}），已阻断调用：\n" + "\n".join(lines)
    )


@dataclass
class GateResult:
    raw: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    attempts: int = 1
    error: str = ""

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and not self.failures

    def report(self) -> str:
        if self.error:
            return f"[闸门] 调用失败：{self.error}"
        if self.passed:
            return f"[闸门] 通过（尝试 {self.attempts} 次）"
        lines = [f"[闸门] 未通过（尝试 {self.attempts} 次）："]
        lines += [f"   ✗ {n}  {d}" for n, _, d in self.failures]
        return "\n".join(lines)


class QualityGate:
    """生成 → 校验 → 把未通过项回投给模型重做的通用闸门。"""

    def __init__(self, client, model: str, *, max_retry: int = 1,
                 temperature: float = 0.7):
        self.client = client
        self.model = model
        self.max_retry = max_retry
        self.temperature = temperature

    def _call(self, messages: list[dict]) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature)
        return resp.choices[0].message.content or ""

    def generate(self, prompt: str, checks, *, copy_refs: list[str] | None = None,
                 min_common: int = DEFAULT_MIN_COMMON) -> GateResult:
        """``checks(raw) -> list[(名称, 是否通过, 说明)]``。

        copy_refs 非空时会额外追加一条照抄检查。
        """
        messages = [{"role": "user", "content": prompt}]
        result = GateResult(raw="")
        for attempt in range(1, self.max_retry + 2):
            try:
                raw = self._call(messages)
            except Exception as e:                      # noqa: BLE001
                result.error = f"{type(e).__name__}: {e}"
                result.attempts = attempt
                return result

            result.raw = raw
            result.attempts = attempt
            result.checks = list(checks(raw))
            if copy_refs:
                hits = copy_hits(raw, copy_refs, min_common)
                result.checks.append((
                    "非照抄 Few-shot",
                    not hits,
                    (f"逐字重合 {len(hits)} 处，最长 {max((len(h) for h in hits), default=0)} 字："
                     + " ／ ".join(h[:40] for h in hits[:2])) if hits else "",
                ))
            if result.passed:
                return result

            if attempt > self.max_retry:
                break
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    "你的输出存在以下问题，请修正后重新输出完整结果：\n"
                    + "\n".join(f"- {n}：{d}" for n, _, d in result.failures)},
            ]
        return result


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 自测：拿 Few-shot 去撞自己，必须报出大量重合；拿无关文本则应干净
    lib = None
    from prompt_builder import PromptLib
    lib = PromptLib(Path(__file__).parent)

    refs = load_fewshot_refs(lib, "teacher_sprint")
    print(f"teacher_sprint 参考文本 {len(refs)} 段，共 {sum(len(r) for r in refs)} 字符")

    self_hits = copy_hits(refs[0], refs)
    print(f"自我对照（必然照抄）：命中 {len(self_hits)} 处，最长 {max(map(len, self_hits))} 字")

    unrelated = "学生在实验中观察到，光照强度增大时气泡产生速率随之上升，说明光合作用速率与光照强度正相关。"
    print(f"无关文本：命中 {len(copy_hits(unrelated, refs))} 处（应为 0）")
