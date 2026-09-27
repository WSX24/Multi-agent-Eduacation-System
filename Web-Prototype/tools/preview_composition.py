#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# preview_composition.py — 直接看「镜头里是什么」
#
# 为什么需要它：
#   从截图降采样成字符图看不出构图 —— 108×26 的格子铺在 1600×900 上，
#   每格 15px，一条 4px 宽的线在格内被平均掉，整条消失。
#   于是「画面里到底画了什么」这件事从来没被真正看过，只能靠数值代理
#   （墨覆盖率、结构量）猜 —— 而数值代理对"乱"完全无感。
#
#   这个工具不再经过截图：它读 build/line-runtime-data.js，
#   用与运行时同一个相机变换把采样点投到屏幕，把**中心线**光栅化成掩模，
#   再按字符网格取「格内有没有墨」，所以线不会消失。
#
# 用法：
#   python tools/preview_composition.py --p 0.62 --cols 120
#   python tools/preview_composition.py --all            # 六个进度一起看
#   python tools/preview_composition.py --p 0.62 --stats  # 加读数
# ═══════════════════════════════════════════════════════════════════════
import argparse
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import importlib.util

_spec = importlib.util.spec_from_file_location(
    'vg', Path(__file__).with_name('verify_geometry.py'))
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)


def centreline_screen(RT, p, W, H):
    """把已画部分的**中心线**投到屏幕坐标，返回折线点列（屏幕像素）。"""
    C, A = RT['camera'], RT['anchor']
    SC = RT['path']['sheetCentre']
    t = vg.tip_at(p, RT)
    s = vg.scale_at(p, C)
    bias = A['bias']
    ax = t['x'] + (SC['x'] - t['x']) * bias
    ay = t['y'] + (SC['y'] - t['y']) * bias
    tx, ty = W / 2 - ax * s, H / 2 - ay * s
    N = RT['samples']
    pts = RT['points']
    k = t['k']
    out = [(pts[j][0] * s + tx, pts[j][1] * s + ty) for j in range(0, k + 1)]
    if t['f'] > 1e-4:
        out.append((t['x'] * s + tx, t['y'] * s + ty))
    return out, s


def raster(RT, p, W, H):
    """中心线掩模（1px）。"""
    line, s = centreline_screen(RT, p, W, H)
    img = Image.new('L', (W, H), 0)
    if len(line) > 1:
        ImageDraw.Draw(img).line([(x, y) for x, y in line], fill=255, width=1)
    elif len(line) == 1:
        img.putpixel((int(line[0][0]), int(line[0][1])), 255)
    return np.asarray(img) > 0, s


def ascii_frame(mask, cols, rows):
    """格内**取最大值**（有没有墨），不是取平均 —— 这样线不会消失。"""
    H, W = mask.shape
    ys = np.linspace(0, H, rows + 1).astype(int)
    xs = np.linspace(0, W, cols + 1).astype(int)
    out = []
    for j in range(rows):
        r = ''
        for i in range(cols):
            blk = mask[ys[j]:max(ys[j] + 1, ys[j + 1]), xs[i]:max(xs[i] + 1, xs[i + 1])]
            r += '#' if blk.any() else ('.' if False else ' ')
        out.append(r)
    return out


def legibility(mask, cols, rows):
    """几个能反映"乱"的量。
     crossings：每行/每列被墨穿过的格数（多 = 线在画面上来回穿插）
     coverage ：墨格占全部格的比例
     components：把墨格按 8 邻域连起来之后的连通块数（多 = 碎）
    """
    H, W = mask.shape
    cell_h, cell_w = H / rows, W / cols
    grid = np.zeros((rows, cols), dtype=bool)
    for j in range(rows):
        for i in range(cols):
            blk = mask[int(j * cell_h):max(int(j * cell_h) + 1, int((j + 1) * cell_h)),
                       int(i * cell_w):max(int(i * cell_w) + 1, int((i + 1) * cell_w))]
            grid[j, i] = blk.any()
    row_cross = [int(grid[j].sum()) for j in range(rows)]
    col_cross = [int(grid[:, i].sum()) for i in range(cols)]
    # 8 邻域连通块
    seen = np.zeros_like(grid)
    comps = 0
    for j in range(rows):
        for i in range(cols):
            if not grid[j, i] or seen[j, i]:
                continue
            comps += 1
            stack = [(j, i)]
            seen[j, i] = True
            while stack:
                a, b = stack.pop()
                for dj in (-1, 0, 1):
                    for di in (-1, 0, 1):
                        na, nb = a + dj, b + di
                        if 0 <= na < rows and 0 <= nb < cols and grid[na, nb] and not seen[na, nb]:
                            seen[na, nb] = True
                            stack.append((na, nb))
    return {
        'inkCells': int(grid.sum()),
        'coverage': float(grid.mean()),
        'maxRowCross': max(row_cross) if row_cross else 0,
        'meanRowCross': float(np.mean(row_cross)),
        'maxColCross': max(col_cross) if col_cross else 0,
        'components': comps,
        'rowCross': row_cross,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--p', type=float, default=0.62)
    ap.add_argument('--all', action='store_true', help='跑六个进度')
    ap.add_argument('--cols', type=int, default=118)
    ap.add_argument('--rows', type=int, default=0, help='默认按 16:9 自动算')
    ap.add_argument('--w', type=int, default=1600)
    ap.add_argument('--h', type=int, default=900)
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--build', default=str(vg.BUILD_PATH))
    args = ap.parse_args()

    RT = vg.load_runtime(Path(args.build))
    W, H = args.w, args.h
    cols = args.cols
    rows = args.rows or max(8, int(round(cols * H / W / 2.1)))   # 字符高宽比约 2.1

    ps = [0.00, 0.20, 0.40, 0.62, 0.80, 1.00] if args.all else [args.p]
    for p in ps:
        mask, s = raster(RT, p, W, H)
        L = legibility(mask, cols, rows)
        print(f'===== p = {p:.2f}   zoom {s:.3f}×   已画采样点 '
              f'{vg.index_at(p, RT):.0f} / {RT["samples"]} =====')
        if args.stats:
            print(f'  墨格 {L["inkCells"]}/{cols * rows}（{L["coverage"] * 100:.1f}%）  '
                  f'行内最多穿过 {L["maxRowCross"]} 格（均 {L["meanRowCross"]:.1f}）  '
                  f'列内最多 {L["maxColCross"]} 格  连通块 {L["components"]}')
        for line in ascii_frame(mask, cols, rows):
            print('  ' + line)
        print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
