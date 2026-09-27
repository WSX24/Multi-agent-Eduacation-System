#!/usr/bin/env python3
"""把「与画布边缘连通的近白背景」刷成纯白，不动主体内部的浅色面。

为什么不能用简单的阈值刷白：
  本形象身上有 (245,240,237)、(252,250,251) 这类浅色面板，而视频输出的背景
  在 240–250 之间——两者在取值上重叠，任何单一阈值都会把主体一起吃掉。

为什么洪泛是对的：
  背景是**与画布四边连通**的区域；主体内部的浅色面板被更暗的轮廓线包住，
  与边缘不连通。所以从边缘出发做四连通洪泛，只会命中真正的背景。

这仍然不是抠图：背景留在图里，只是被对齐到页面的 --bg（纯白）。
用法：
  python3 tools/whiten_background.py source/K0.png --threshold 234
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

from PIL import Image

WHITE = 255


def whiten(path: Path, threshold: int, out: Path) -> tuple[int, int]:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()

    def candidate(c) -> bool:
        return min(c) >= threshold

    seen = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()

    def push(x: int, y: int) -> None:
        i = y * w + x
        if seen[i]:
            return
        if not candidate(px[x, y]):
            return
        seen[i] = 1
        queue.append((x, y))

    for x in range(w):
        push(x, 0)
        push(x, h - 1)
    for y in range(h):
        push(0, y)
        push(w - 1, y)

    while queue:
        x, y = queue.popleft()
        px[x, y] = (WHITE, WHITE, WHITE)
        if x > 0:
            push(x - 1, y)
        if x < w - 1:
            push(x + 1, y)
        if y > 0:
            push(x, y - 1)
        if y < h - 1:
            push(x, y + 1)

    filled = sum(seen)
    im.save(out, "PNG", optimize=True)
    return filled, w * h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--threshold", type=int, default=234,
                    help="近白判定：所有通道 >= 此值才算背景候选（默认 234）")
    ap.add_argument("--out", type=Path, default=None, help="默认就地覆盖")
    args = ap.parse_args()

    target = args.out or args.image
    filled, total = whiten(args.image, args.threshold, target)
    print(f"洪泛刷白：{filled} / {total} px（{100*filled/total:.1f}%）-> {target}")

    # 复验
    im = Image.open(target).convert("RGB")
    px = im.load()
    w, h = im.size
    b = max(1, int(min(w, h) * 0.06))
    worst = 255
    for y in range(h):
        for x in range(w):
            if x < b or x >= w - b or y < b or y >= h - b:
                worst = min(worst, min(px[x, y]))
    print(f"复验：边距最暗值 = {worst}  {'[OK]' if worst >= 250 else '[NG] 仍有非白边距'}")
    return 0 if worst >= 250 else 2


if __name__ == "__main__":
    raise SystemExit(main())
