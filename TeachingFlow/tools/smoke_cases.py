#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""意图层用例：一句话 → FlowSpec 的期望值（不调模型、不产 run 目录）。

覆盖我们改过的判定：强度（一次课/系统/模糊）、单篇课文、期限、聚焦主题、
显式周期、页数/课时覆盖。跑法::

    python tools/smoke_cases.py          # 全部用例
    python tools/smoke_cases.py -v       # 打印每个字段的实际值

退出码：0 全过，1 有失败。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core import scope                                  # noqa: E402

BASE = dict(stage=None, subject=None, accept_detected=False, level=None, module=None,
            weeks=None, days=None, term=None, hours_per_week=None, pages=None, units=None)


def args(**kw):
    d = dict(BASE)
    d.update(kw)
    return SimpleNamespace(**d)


#: (用例名, 原话, CLI 覆盖, 期望字段)
CASES: list[tuple[str, str, dict, dict]] = [
    # —— 强度：一次课 ——
    ("单篇+简单 → 一次课", "我想简单学一下《祥林嫂》这篇课文", {},
     dict(intensity="一次课", single_text=True, work="祥林嫂", focus="祥林嫂",
          weeks=1, term="短期", pages=None)),
    ("期限+突击 → 一次课", "我今天突击一下生物的光合作用", {},
     dict(intensity="一次课", single_text=False, focus="光合作用", weeks=1, term="短期")),
    ("考前+限期", "明天就要考牛顿第二定律", {},
     dict(intensity="一次课", focus="牛顿第二定律", weeks=1, term="短期")),
    ("浅尝", "我想浅尝一下《祝福》", {},
     dict(intensity="一次课", single_text=True, work="祝福", weeks=1)),
    ("速通+十天", "十天速通高中化学实验", dict(stage="高中", subject="化学"),
     dict(intensity="一次课", weeks=2, term="短期")),

    # —— 显式周期优先 ——
    ("两周突击不压成一周", "我想用2周突击高中语文文言文", dict(stage="高中", subject="语文"),
     dict(intensity="一次课", weeks=2, focus="文言文")),
    ("两个月 → 8 周长期", "帮我用两个月学完初中数学函数", dict(stage="初中", subject="数学"),
     dict(weeks=8, term="长期", intensity="")),
    ("半年 → 夹到 15 周", "半年内把高中物理力学过一遍", dict(stage="高中", subject="物理"),
     dict(weeks=15, term="长期")),

    # —— 系统 / 模糊 ——
    ("系统学 → 待估算", "我想系统学习高中语文", dict(stage="高中", subject="语文"),
     dict(intensity="系统", needs_estimate=True, pages=None)),
    ("模糊 → 待追问/估算", "我想学高中语文", dict(stage="高中", subject="语文"),
     dict(intensity="", needs_estimate=True)),

    # —— 聚焦主题（不含强度词） ——
    ("完形填空", "我想练练英语完形填空", dict(stage="高中", subject="英语"),
     dict(focus="完形填空", intensity="")),
    ("二次函数", "帮我讲讲二次函数", dict(stage="初中", subject="数学"),
     dict(focus="二次函数")),

    # —— 显式覆盖 ——
    ("--pages 覆盖", "我想简单学一下《祥林嫂》这篇课文", dict(pages=20, stage="高中", subject="语文"),
     dict(pages=20, pages_source="用户指定")),
    ("--hours-per-week 覆盖", "我想简单学一下《祥林嫂》这篇课文",
     dict(hours_per_week=1, stage="高中", subject="语文"),
     dict(hours_per_week=1, hours_source="--hours-per-week")),
    ("--weeks 覆盖强度", "我今天突击生物的光合作用",
     dict(weeks=3, stage="高中", subject="生物"),
     dict(weeks=3, weeks_source="--weeks")),
    ("--term short 无周期", "我想学高中语文", dict(term="short", stage="高中", subject="语文"),
     dict(term="短期", weeks=2, weeks_source="按--term默认")),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="意图层用例（不调模型）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印实际值")
    a = ap.parse_args(argv)

    ok_n = 0
    for name, text, over, want in CASES:
        spec = scope.build_spec(text, args(**over))
        got = {k: getattr(spec, k) for k in want if k != "needs_estimate"}
        if "needs_estimate" in want:
            got["needs_estimate"] = scope.needs_estimate(spec)
        bad = {k: (v, got.get(k)) for k, v in want.items() if got.get(k) != v}
        if bad:
            print(f"✗ {name}  ←「{text}」")
            for k, (w, g) in bad.items():
                print(f"    {k}: 期望 {w!r}，实际 {g!r}")
            print(f"    实际全量: {got}")
        else:
            ok_n += 1
            print(f"✓ {name}" + (f"   {got}" if a.verbose else ""))

    total = len(CASES)
    print(f"\n{ok_n}/{total} 通过")
    return 0 if ok_n == total else 1


if __name__ == "__main__":
    sys.exit(main())
