# -*- coding: utf-8 -*-
"""课程交付件的命名与目录口径。

一次 run 就是一门课（``runs/<日期>/<用户原话>_<时分秒>/``）。章与单元是**父子关系**，
所以目录也照父子排——章建目录，单元是章目录里的文件，而不是把两级压成
``第1章-第1单元-名字.pptx`` 一条扁平的字符串::

    <课程>/第1章-散文阅读入门/
        第1单元-散文的文本细读与圈点勾画.pptx
        第1单元-散文的文本细读与圈点勾画-讲稿.md
        第1单元-散文的文本细读与圈点勾画-页面说明.md
        第2单元-散文的情感线索与主旨概括.pptx
    <课程>/第2章-小说阅读/
        第1单元-人物形象的分析与概括.pptx

章序取蓝图的 ``chapter.order``（缺则从 ``ch01`` 里取数字），单元序取
``unit_index + 1``（章内序号，与 S2 打印的「第 1/2 个」同一口径）。
"""
from __future__ import annotations

import re
from pathlib import Path

#: Windows 文件名非法字符（中文标点、空格都保留，标题里本来就有《背影》这类书名号）
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')

#: 交付件后缀标签（与 pptx 同 stem，方便「三件一起拷」）
NARRATION_SUFFIX = "-讲稿"
PAGES_SUFFIX = "-页面说明"


def _clean(text: str, fallback: str = "") -> str:
    s = _BAD_CHARS.sub("", str(text or "")).strip().rstrip(". ")
    return s or fallback


def chapter_no(target: dict) -> int:
    """章序：优先蓝图给的真实 order，兜底从 chapter_id（ch01）里取数字。"""
    order = target.get("chapter_order")
    if isinstance(order, (int, float)) and order > 0:
        return int(order)
    m = re.search(r"\d+", str(target.get("chapter_id") or ""))
    return int(m.group(0)) if m else 1


def unit_no(target: dict) -> int:
    """单元序：章内第几个（1 起）。优先 unit_index，兜底从 unit_id（ch01-u2）取。"""
    idx = target.get("unit_index")
    if isinstance(idx, int) and idx >= 0:
        return idx + 1
    m = re.search(r"-u(\d+)", str(target.get("unit_id") or ""), re.I)
    return int(m.group(1)) if m else 1


def chapter_dirname(target: dict) -> str:
    """章目录名：``第1章-散文阅读入门``（章标题缺失时只留 ``第1章``）。"""
    title = _clean(target.get("chapter_title"))
    head = f"第{chapter_no(target)}章"
    return f"{head}-{title}" if title else head


def unit_label(target: dict) -> str:
    """单元标签：``第2单元-散文的情感线索与主旨概括``（章内序号，不重复章号）。"""
    return f"第{unit_no(target)}单元-{_clean(target.get('unit_title'), '未命名单元')}"


def chapter_dir(run_dir: Path, target: dict) -> Path:
    """课程根目录下的章目录（交付件的父目录）。"""
    return Path(run_dir) / chapter_dirname(target)


def deliverable_path(run_dir: Path, target: dict) -> Path:
    """交付件路径：``<课程>/第N章-章名/第M单元-单元名.pptx``。"""
    return chapter_dir(run_dir, target) / f"{unit_label(target)}.pptx"


def old_output_roots(run_dir: Path, target: dict) -> list[Path]:
    """旧版布局（``05_ppt_<uid>/**``）里已产出的 pptx，用于老 run 的补交付。"""
    root = Path(run_dir) / f"05_ppt_{target.get('unit_id')}"
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.pptx") if not p.name.startswith("~$"))


def narration_path(pptx: Path) -> Path:
    return pptx.with_name(f"{pptx.stem}{NARRATION_SUFFIX}.md")


def pages_path(pptx: Path) -> Path:
    return pptx.with_name(f"{pptx.stem}{PAGES_SUFFIX}.md")
