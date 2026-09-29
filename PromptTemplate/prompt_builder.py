#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prompt 模板库加载器 —— 教学多智能体专用

职责：加载 YAML 模板 → 填充变量 → 拼接 Few-shot / CoT → 输出最终 Prompt。

用法示例::

    from pathlib import Path
    from prompt_builder import PromptLib

    lib = PromptLib(Path(__file__).parent)

    prompt = lib.build(
        "teacher",                                # 角色模板
        {"学科": "初中数学", "知识点": "因式分解", "学生水平": "基础"},
        cot="math_steps",                          # 追加显式分步 CoT
    )
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[reportMissingModuleSource]  # PyYAML 无类型存根

# 匹配 {{任意非花括号内容}}，支持中文占位符（如 {{学科}}、{{学生水平}}）
_VAR_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")

# YAML 文件顶部的元数据键，不属于内容本体（加载 cot 片段时需过滤）
_META_KEYS = {"name", "label", "version", "description"}


# ────────────────────────── 输入契约 ──────────────────────────
# 模板为「输出」写足了口径（课时计量口径 / 时限计量口径），却没有为「输入」
# 声明类型与单位。后果是数值字段拿到自由文本时**不报错、静默降级**：
#   星期可投入时间 = "每周三小时" → 乘法失效，模型自己猜一个数
#   「上一章测验结果」= "还行"   → 数值分支（≥85% 升档）无法命中，却不告警
# 因此把类型与单位写进模板的 inputs: 块，并让入口预检按它校验。
#
# 类型取值：
#   text     仅查非空（默认，未声明时即为此类型）
#   number   纯数字，无单位；可声 min/max（闭区间）与 min_exclusive/max_exclusive（开区间）
#   quantity 数字 + 单位，形如 4周 / 4 周；也接受裸数字（单位已在模板中声明）；
#            **必须为正数**，区间声明同上
#   percent  形如「85%」或「<label> 85%」，取值默认限定 0~100，或命中 allow 里的哨兵值
#   enum     必须命中 values 之一
#
# 闭/开区间的区分不能省：产品口径写的是「总周期**小于** 16 周」，若用 max: 16
# 则 16 周本身也会放行——差一个边界的口径就不是同一个口径。
_NUMBER_BODY = r"-?\d+(?:\.\d+)?"
_BARE_NUMBER_RE = re.compile(rf"^{_NUMBER_BODY}$")
_QUANTITY_RE = re.compile(rf"^({_NUMBER_BODY})\s*(.*)$")
# 百分比只允许无符号数字，故不用 _NUMBER_BODY（它带可选负号）
_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")


def _check_bounds(num: float, rule: dict, unit: str = "") -> str:
    """闭区间与开区间一起查；返回问题描述，空串表示通过。"""
    lo, hi = rule.get("min"), rule.get("max")
    lo_x, hi_x = rule.get("min_exclusive"), rule.get("max_exclusive")
    if lo is not None and num < lo:
        return f"不得小于 {lo:g}{unit}，实际 {num:g}{unit}"
    if hi is not None and num > hi:
        return f"不得大于 {hi:g}{unit}，实际 {num:g}{unit}"
    if lo_x is not None and num <= lo_x:
        return f"必须大于 {lo_x:g}{unit}，实际 {num:g}{unit}"
    if hi_x is not None and num >= hi_x:
        return f"必须小于 {hi_x:g}{unit}，实际 {num:g}{unit}"
    return ""


def _check_quantity(value: str, rule: dict) -> str:
    """返回问题描述，空串表示通过。"""
    unit = str(rule.get("unit", ""))
    m = _QUANTITY_RE.match(value)
    if not m:
        shown = f"4{unit}" if unit else "4"
        return f"需要「数字+单位」，形如 {shown}，实际 {value!r}"
    tail = m.group(2).strip()
    if tail and unit and tail != unit:
        return f"单位应为「{unit}」，实际 {value!r}"
    num = float(m.group(1))
    # 时长/数量类字段出现零或负数一定是调用方出错，不以 min 声明与否为转移
    if num <= 0:
        return f"必须为正数，实际 {value!r}"
    return _check_bounds(num, rule, unit)


def _check_percent(value: str, rule: dict) -> str:
    """只认「85%」或「<label> 85%」两种形状，不做“含 % 就算数”的宽放。

    旧实现用 search() 只要串里有百分号就放行，于是「下降了50%」「abc 99% def」
    这类自由文本会穿过预检——而下游是拿它做数值分支的。
    """
    allowed = [str(a) for a in (rule.get("allow") or [])]
    if value in allowed:
        return ""
    label = str(rule.get("label") or "")
    body = value[len(label):].strip() if label and value.startswith(label) else value
    m = _PERCENT_RE.match(body)
    if not m:
        shown = f"{label} 85%" if label else "85%"
        hint = f"，或为 {allowed} 之一" if allowed else ""
        return f"需要百分比，形如 {shown}{hint}，实际 {value!r}"
    lo, hi = rule.get("min", 0), rule.get("max", 100)
    num = float(m.group(1))
    if not lo <= num <= hi:
        return f"百分比需在 {lo:g}%~{hi:g}% 之间，实际 {value!r}"
    return ""


def _check_number(value: str, rule: dict) -> str:
    if not _BARE_NUMBER_RE.match(value):
        return f"需要纯数字，实际 {value!r}"
    return _check_bounds(float(value), rule)


def _check_enum(value: str, values: list) -> str:
    # 必须 str 化：YAML 会把裸写的 true/false 解析成布尔 True/False，
    # 而运行时变量表里它们是字符串 "true"/"false"。
    allowed = [str(v) for v in values]
    if not allowed or value in allowed:
        return ""
    return f"取值必须是 {' / '.join(allowed)} 之一，实际 {value!r}"


@dataclass(frozen=True)
class InputProblem:
    """一处输入问题。``kind`` 为 ``missing``（缺项/空值）或 ``type``（类型/单位不符）。"""

    name: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.name}：{self.detail}"


def _check_one(name: str, value: str, rule: dict) -> InputProblem | None:
    """按声明校验单个变量；返回 None 表示通过。"""
    if not value:
        return InputProblem(name, "missing", "缺失或为空")
    kind = rule.get("type", "text")
    if kind == "quantity":
        detail = _check_quantity(value, rule)
    elif kind == "percent":
        detail = _check_percent(value, rule)
    elif kind == "enum":
        detail = _check_enum(value, rule.get("values") or [])
    elif kind == "number":
        detail = _check_number(value, rule)
    else:
        detail = ""
    return InputProblem(name, "type", detail) if detail else None


class PromptLib:
    """模板库入口：加载 / 构建 / 自查。"""

    def __init__(self, root: str | Path | None = None):
        # 默认使用 prompt_builder.py 所在目录（即模板库根目录）
        self.root = Path(root) if root else Path(__file__).parent
        self.templates = self._load_dir("templates")
        self.fewshots = self._load_dir("fewshots")
        raw_cot = self._load_yaml(self.root / "cot" / "snippets.yaml")
        self.cot = {k: v for k, v in raw_cot.items() if k not in _META_KEYS}

    # ---------------- 加载 ----------------
    def _load_dir(self, subdir: str) -> dict:
        data = {}
        for p in sorted((self.root / subdir).glob("*.yaml")):
            data[p.stem] = self._load_yaml(p)
        return data

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        # 文件不存在时返回空表，而不是抛 FileNotFoundError：
        # 允许“最小模板目录”（只建 templates/ 就能用，不必连 cot/ 也建上）。
        if not path.exists():
            return {}
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # ---------------- 构建 ----------------
    def build(
        self,
        role: str,
        variables: dict | None = None,
        fewshot: bool | str = True,
        cot: str | None = None,
        fewshot_count: int | None = None,
    ) -> str:
        """组装完整 Prompt。

        参数
        ----
        role:
            模板名（如 ``"teacher"``）。
        variables:
            变量表，用于填充 ``{{占位符}}``。
        fewshot:
            ``False`` 不追加示例；``True`` 自动使用与 role 同名的 fewshot 文件；
            传入字符串可指定其它 fewshot 文件名。
        cot:
            CoT 片段名（``zero_shot`` / ``math_steps`` / ``self_check``），
            ``None`` 表示不追加。
        fewshot_count:
            最多拼接几个示例（None = 全部）。
        """
        variables = variables or {}
        if role not in self.templates:
            raise KeyError(f"未找到模板 '{role}'，可选: {sorted(self.templates)}")

        text = self._render_template(self.templates[role])
        text = self._fill(text, variables)

        if fewshot:
            fs_name = role if fewshot is True else fewshot
            if fs_name in self.fewshots:
                text += self._render_fewshots(fs_name, fewshot_count)
            elif fewshot is not True:
                # 显式指定了不存在的 fewshot 文件才报错；自动模式静默跳过
                raise KeyError(f"未找到 Few-shot '{fs_name}'，可选: {sorted(self.fewshots)}")

        if cot:
            if cot not in self.cot:
                raise KeyError(f"未找到 CoT 片段 '{cot}'，可选: {sorted(self.cot)}")
            text += "\n\n" + self.cot[cot].strip()

        return text.strip()

    # ---------------- 渲染 ----------------
    def _render_template(self, t: dict) -> str:
        """渲染模板正文。

        优先使用 ``sections``（有序小节，可承载任意自定义字段，如
        branch_rule / 难度自适应规则 / 输出纪律）；缺省时回退到旧式
        ``system`` 字典（role / goal / abilities / restrictions / ...）。
        """
        if t.get("sections"):
            blocks: list[str] = []
            for sec in t["sections"]:
                lines = [f"【{sec['title']}】"]
                if sec.get("body"):
                    lines.append(str(sec["body"]).rstrip())
                for key, prefix in (("do", "你能做："), ("dont", "你不能做："), ("list", None)):
                    items = sec.get(key) or []
                    if not items:
                        continue
                    if prefix:
                        lines.append(prefix)
                    lines += [f"- {x}" for x in items]
                blocks.append("\n".join(lines))
            return "\n\n".join(blocks) + "\n"

        sys = t.get("system", t)
        lines: list[str] = []

        if sys.get("role"):
            lines += ["【角色设定】", sys["role"]]

        if sys.get("goal"):
            lines += ["", "【你的目标】", sys["goal"]]

        abilities = sys.get("abilities") or []
        restrictions = sys.get("restrictions") or []
        if abilities or restrictions:
            lines += ["", "【能力边界】"]
            if abilities:
                lines.append("你能做：")
                lines += [f"- {a}" for a in abilities]
            if restrictions:
                lines.append("你不能做：")
                lines += [f"- {r}" for r in restrictions]

        if sys.get("audience"):
            lines += ["", "【对话对象】", sys["audience"]]

        if sys.get("tone"):
            lines += ["", "【语气与风格】", sys["tone"]]

        if sys.get("output_format"):
            lines += ["", "【输出格式】", sys["output_format"].rstrip()]

        return "\n".join(lines) + "\n"

    def _render_fewshots(self, name: str, count: int | None = None) -> str:
        if name not in self.fewshots:
            raise KeyError(f"未找到 Few-shot '{name}'，可选: {sorted(self.fewshots)}")
        examples = self.fewshots[name].get("examples", [])
        if count is not None:
            examples = examples[:count]

        blocks = [
            f"【示例输入】{ex.get('input', '')}\n【示例输出】\n{ex.get('output', '').rstrip()}"
            for ex in examples
        ]
        if not blocks:
            return ""
        # 表头必须区分「模仿格式」与「照搬内容」，否则模型会把示例当答案模板抄
        return (
            "\n\n以下为示例，仅用于演示输出格式、字段结构与风格。"
            "禁止照搬其中的学科、章节名、单元名、知识点与数值——"
            "示例只是格式参照，具体内容必须根据本次任务的输入重新生成：\n\n"
            + "\n\n".join(blocks)
            + "\n"
        )

    @staticmethod
    def _fill(text: str, variables: dict) -> str:
        def repl(m: re.Match) -> str:
            key = m.group(1)
            # 未提供的变量保留原占位符，便于自查遗漏
            return str(variables[key]) if key in variables else m.group(0)

        return _VAR_PATTERN.sub(repl, text)

    # ---------------- 自查工具 ----------------
    def list_roles(self) -> list[str]:
        return sorted(self.templates)

    def list_fewshots(self) -> list[str]:
        return sorted(self.fewshots)

    def list_cot(self) -> list[str]:
        return sorted(self.cot)

    def missing_vars(self, role: str, variables: dict | None = None) -> set[str]:
        """返回当前变量表下仍未被填充的占位符（不含 Few-shot / CoT 中的）。"""
        variables = variables or {}
        text = self._render_template(self.templates[role])
        return {m.group(1) for m in _VAR_PATTERN.finditer(text)} - set(variables)

    # ---------------- 输入契约 ----------------
    def input_spec(self, role: str) -> dict:
        """模板声明的输入契约（``inputs:`` 块）；未声明的变量视为 ``text``。"""
        return self.templates[role].get("inputs") or {}

    def check_inputs(self, role: str, variables: dict | None = None) -> list[InputProblem]:
        """按输入契约校验变量表，返回**全部**问题（不抛异常，便于一次报全）。

        与 ``missing_vars`` 的区别：后者只比对占位符有没有被替换，
        本方法还会按 ``inputs:`` 声明的类型/单位/取值域做实质校验。
        调用方若不传某变量，其类型规则不生效，只会被记为 missing。
        """
        variables = variables or {}
        spec = self.input_spec(role)
        problems: list[InputProblem] = []
        for name in sorted(self.missing_vars(role, {})):
            raw = variables.get(name, "")
            problem = _check_one(name, str(raw).strip(), spec.get(name) or {})
            if problem:
                problems.append(problem)
        return problems

    def invalid_inputs(self, role: str, variables: dict | None = None) -> list[InputProblem]:
        """只返回类型/单位/取值域不符的问题（缺项由 ``blank_inputs`` 负责）。"""
        return [p for p in self.check_inputs(role, variables) if p.kind == "type"]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lib = PromptLib(Path(__file__).parent)
    print(lib.build(
        "teacher",
        {"学科": "初中数学", "知识点": "一元二次方程", "学生水平": "基础"},
        cot="math_steps",
    ))
