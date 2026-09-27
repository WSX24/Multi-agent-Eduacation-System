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
from pathlib import Path

import yaml  # type: ignore[reportMissingModuleSource]  # PyYAML 无类型存根

# 匹配 {{任意非花括号内容}}，支持中文占位符（如 {{学科}}、{{学生水平}}）
_VAR_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")

# YAML 文件顶部的元数据键，不属于内容本体（加载 cot 片段时需过滤）
_META_KEYS = {"name", "label", "version", "description"}


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


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lib = PromptLib(Path(__file__).parent)
    print(lib.build(
        "teacher",
        {"学科": "初中数学", "知识点": "一元二次方程", "学生水平": "基础"},
        cot="math_steps",
    ))
