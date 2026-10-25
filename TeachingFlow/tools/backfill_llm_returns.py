#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 `LLM返回/`：把已有 run 里散落的 prompt / result 配对归档。

用途：`LLM返回/` 是后加的功能，早先跑出来的 run 目录里只有
``01_plan.prompt.md`` / ``01_plan.result.md`` 这类散件。这个脚本按调用顺序
把它们配对写进 ``<run>/LLM返回/NNN_标签.md``，格式与在线归档一致。

用法::

    python tools/backfill_llm_returns.py runs/20261024/我想在今天突击生物的光合作用_204313
    python tools/backfill_llm_returns.py "<run 目录>" --force   # 覆盖已归档的同名文件
    python tools/backfill_llm_returns.py --latest               # 最近一次 run

说明：回填的文件头部 model/temperature 记为「回填（未知）」，因为历史产物没存这些元信息。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core.artifacts import Run                       # noqa: E402

#: (文件名前缀, 标签模板, 步骤名, 排序权重)
SPECS: list[tuple[re.Pattern, str, str, int]] = [
    (re.compile(r"^00a_module$"), "S0a-模块判定", "s0_module", 10),
    (re.compile(r"^00_scope$"), "S0-模块切分", "s0_scope", 15),
    (re.compile(r"^00b_estimate$"), "S0b-周期估算", "s0_estimate", 20),
    (re.compile(r"^01_plan$"), "S1-学习方案", "s1_plan", 30),
    (re.compile(r"^03_unit_(.+)$"), "S3-单元教学·{tag}", "s3_unit", 40),
    (re.compile(r"^04a_pages_(.+)$"), "S4a-页数决策·{tag}", "s4_pages", 50),
    (re.compile(r"^04_outline_(.+)$"), "S4-课件大纲·{tag}", "s4_outline", 60),
]

_PROMPT_MARK = re.compile(r"^#\s*提示词\s*$", re.M)


def _prompt_body(prompt_file: Path) -> str:
    """取 prompt.md 里「实际发给模型」的那段（去掉我们加的头部元信息）。"""
    text = prompt_file.read_text(encoding="utf-8")
    m = _PROMPT_MARK.search(text)
    return (text[m.end():] if m else text).strip()


def collect(run_dir: Path) -> list[tuple[int, int, str, str, str, str]]:
    """返回 [(权重, 序号, 标签, 步骤, prompt, result)]，按调用顺序排好。"""
    out: list[tuple[int, int, str, str, str, str]] = []
    results = sorted(p for p in run_dir.glob("*.result.md"))
    for i, res in enumerate(results):
        stem = res.name[: -len(".result.md")]
        for rx, label, step, order in SPECS:
            m = rx.match(stem)
            if not m:
                continue
            tag = m.group(1) if m.groups() else ""
            prompt_file = run_dir / f"{stem}.prompt.md"
            prompt = _prompt_body(prompt_file) if prompt_file.is_file() else "（历史 run 未保留提示词）"
            out.append((order, i, label.format(tag=tag), step,
                        prompt, res.read_text(encoding="utf-8")))
            break
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把已有 run 的 prompt/result 回填成 LLM返回/")
    ap.add_argument("run_dir", nargs="?", help="run 目录")
    ap.add_argument("--latest", action="store_true", help="用 runs/ 下最近修改的 run")
    ap.add_argument("--force", action="store_true", help="覆盖同名已归档文件")
    args = ap.parse_args(argv)

    if args.latest or not args.run_dir:
        runs = [p for p in (HERE / "runs").glob("*/*") if p.is_dir()]
        if not runs:
            print("没有可回填的 run")
            return 2
        run_dir = max(runs, key=lambda p: p.stat().st_mtime)
    else:
        run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"目录不存在：{run_dir}")
        return 2

    items = collect(run_dir)
    if not items:
        print(f"没找到 *.result.md：{run_dir}")
        return 1

    run = Run(run_dir.name, resume=run_dir)
    started = 0
    idx_path = run_dir / Run.LLM_DIR / "index.json"
    if idx_path.is_file():
        import json
        try:
            started = max((it.get("n") or 0) for it in json.loads(idx_path.read_text(encoding="utf-8")))
        except ValueError:
            started = 0

    written = 0
    for n, (order, _, label, step, prompt, raw) in enumerate(items, started + 1):
        d = run_dir / Run.LLM_DIR
        if not args.force and (d / f"{n:03d}_").parent.exists():
            same = list(d.glob(f"{n:03d}_*"))
            if same:
                print(f"   跳过（已存在）：{same[0].name}")
                continue
        p = run.record_llm(n, label, step, prompt, raw,
                           {"model": "回填（未知）", "temperature": "—",
                            "mock": False, "mode": "回填"})
        print(f"   ✓ {p.name}")
        written += 1

    print(f"\n{run_dir}\n  回填 {written} 份 → {Run.LLM_DIR}/"
          f"（index.json 已更新，共 {started + len(items)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
