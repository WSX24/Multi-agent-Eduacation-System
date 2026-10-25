# -*- coding: utf-8 -*-
"""提示词反锚定扫描（被 flow 启动时自动跑一次，也被 tools/prompt_lint.py 复用）。

背景：`promptpack/templates/slide_plan.yaml` 的输出格式曾经写成
`页数：12`（配一段凑巧等于 12 的理由），模型直接把它当默认值抄回来。
示例即隐式锚点——这类回归肉眼很难在长 YAML 里发现，所以用机器扫。

只扫 ``sections[].body`` 与顶层零散字段；YAML 注释不会进提示词，跳过。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

#: 会被模型当成「标准答案」的页数写法。`页数 = 封面 1 页 + …` 这类**算法**不算：
#: 它前面是等号或加号，不是冒号，且逐项相加不等于总数。
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("示例页数（页数：N）", re.compile(r"页数\s*[：:]\s*\d+")),
    ("示例总数（共 N 页）", re.compile(r"共\s*\d+\s*页")),
    ("示例总数（总计/一共 N 页）", re.compile(r"(?:总计|一共|总共)\s*\d+\s*页")),
]


def scan_text(text: str, where: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):             # 注释不进提示词
            continue
        for label, rx in PATTERNS:
            for m in rx.finditer(line):
                hits.append(f"{where}:{lineno}  {label}  → {m.group(0)!r}")
    return hits


def scan_templates(path: Path) -> list[str]:
    hits: list[str] = []
    for f in sorted(Path(path).glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for sec in data.get("sections") or []:
            hits += scan_text(str(sec.get("body") or ""), f"{f.name}·{sec.get('title')}")
        for key in ("instructions", "preamble", "suffix"):
            if data.get(key):
                hits += scan_text(str(data[key]), f"{f.name}·{key}")
    return hits
