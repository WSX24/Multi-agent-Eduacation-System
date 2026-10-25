#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词反锚定检查的 CLI 外壳（扫描逻辑在 core/promptlint.py）。

用法::

    python tools/prompt_lint.py                    # 扫 promptpack/templates
    python tools/prompt_lint.py --dir <某目录>      # 扫别的模板目录
    python tools/prompt_lint.py --prompt <file>     # 扫一份渲染后的提示词

退出码：0 干净，1 有命中（可直接挂到 CI / 提交前检查）。
flow.py 启动时也会调一次同样的扫描；这里给的是「不改代码也能查」的入口。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core.promptlint import scan_templates, scan_text          # noqa: E402

DEFAULT_DIR = HERE / "promptpack" / "templates"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地提示词模板的反锚定检查")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="模板目录（默认 promptpack/templates）")
    ap.add_argument("--prompt", default=None, help="改为扫一份渲染后的提示词文件")
    args = ap.parse_args(argv)

    if args.prompt:
        p = Path(args.prompt)
        hits = scan_text(p.read_text(encoding="utf-8"), p.name)
        where = str(p)
    else:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"目录不存在：{d}")
            return 2
        hits, where = scan_templates(d), str(d)

    if not hits:
        print(f"✓ 反锚定检查通过（{where}）：未发现示例页数")
        return 0
    print(f"✗ 发现 {len(hits)} 处会变成隐式锚点的页数示例：")
    for h in hits:
        print("   " + h)
    print("\n改法：把示例数字换成 <阿拉伯数字> 这类占位，并写明「不存在标准页数」。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
