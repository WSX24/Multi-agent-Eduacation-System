#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# ascii_view.py — 把关键帧/成片帧降采样成字符图，用于「无图像通道」时
# 判断构图。它不替代人眼验收，只让构图错误（中轴偏移、主体遮挡、
# 大面积暗块、亮纸不足）在纯文本里可见。
#
# 用法：
#   python tools/ascii_view.py source/K0-scene.png
#   python tools/ascii_view.py source/seg-01-desk.mp4 --fps 1 --cols 72
#
# 字符含义（按亮度）：
#   '#' 极暗(<40)  '=' 暗(40-80)  '+' 中(80-140)  '-' 亮(140-200)
#   '.' 很亮(200-240)  ' ' 近白(>240)
#   紫/彩色像素另用 'V'(紫) 'C'(其他彩色) 标出，便于一眼看出彩色落在哪里。
# ═══════════════════════════════════════════════════════════════════════
import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

RAMP = [(40, '#'), (80, '='), (140, '+'), (200, '-'), (240, '.'), (256, ' ')]


def load(path: Path, fps: float):
    if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}:
        with Image.open(path) as im:
            return [(0.0, np.asarray(im.convert('RGB'), dtype=np.float32))]
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)],
                           capture_output=True, text=True, check=True)
    w, h = (int(x) for x in probe.stdout.strip().split(','))
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'fps={fps}',
                          '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                         capture_output=True, check=True).stdout
    stride = w * h * 3
    return [(i / fps, np.frombuffer(raw[i * stride:(i + 1) * stride], dtype=np.uint8)
             .reshape(h, w, 3).astype(np.float32))
            for i in range(len(raw) // stride)]


def downsample(rgb: np.ndarray, cols: int, rows: int, pool: str = 'min'):
    """pool='mean' 时细线会被平均掉（一条 2px 宽的线落进 8px 的格子里就消失了）。
    pool='min' 取格内最暗值，细线会被保留 —— 这是判断线稿疏密唯一可用的方式。"""
    h, w, _ = rgb.shape
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, cols + 1).astype(int)
    out = np.zeros((rows, cols, 3), dtype=np.float32)
    for j in range(rows):
        for i in range(cols):
            blk = rgb[ys[j]:max(ys[j] + 1, ys[j + 1]), xs[i]:max(xs[i] + 1, xs[i + 1])]
            flat = blk.reshape(-1, 3)
            if pool == 'min':
                out[j, i] = flat[flat.mean(axis=1).argmin()]
            else:
                out[j, i] = flat.mean(axis=0)
    return out


def render(block: np.ndarray) -> list[str]:
    lum = block.mean(axis=2)
    mx = block.max(axis=2)
    mn = block.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    r, g, b = block[:, :, 0], block[:, :, 1], block[:, :, 2]
    hue = np.zeros_like(mx)
    d = np.maximum(mx - mn, 1e-6)
    m = (mx == r); hue[m] = (60 * ((g - b) / d) % 360)[m]
    m = (mx == g) & (mx != r); hue[m] = (60 * ((b - r) / d) + 120)[m]
    m = (mx == b) & (mx != r) & (mx != g); hue[m] = (60 * ((r - g) / d) + 240)[m]
    purple = (hue >= 250) & (hue < 305) & (sat > 0.18)
    colored = (sat > 0.18) & ~purple

    lines = []
    for j in range(block.shape[0]):
        row = []
        for i in range(block.shape[1]):
            if purple[j, i]:
                row.append('V')
            elif colored[j, i]:
                row.append('C')
            else:
                v = lum[j, i]
                row.append(next(ch for t, ch in RAMP if v < t))
        lines.append(''.join(row))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('source')
    ap.add_argument('--cols', type=int, default=72)
    ap.add_argument('--rows', type=int, default=24)
    ap.add_argument('--fps', type=float, default=1.0)
    ap.add_argument('--max-frames', type=int, default=6)
    ap.add_argument('--pool', choices=['min', 'mean'], default='min',
                    help='格子取值：min 保留细线（默认），mean 看整体明暗')
    ap.add_argument('--guides', action='store_true', default=True,
                    help='标出 x=0.5 的中轴与 0.47/0.53 容差带')
    args = ap.parse_args()

    path = Path(args.source)
    frames = load(path, args.fps)
    if len(frames) > args.max_frames:
        idx = np.linspace(0, len(frames) - 1, args.max_frames).astype(int)
        frames = [frames[i] for i in idx]

    print(f'== {path}  {args.cols}x{args.rows} 字符图 ==')
    print("图例: '#'<40  '='40-80  '+'80-140  '-'140-200  '.'200-240  ' '>240  "
          "V=紫  C=其他彩色")
    axis = args.cols // 2
    for t, rgb in frames:
        block = downsample(rgb, args.cols, args.rows, args.pool)
        print(f'\n--- t={t:.2f}s ---')
        if args.guides:
            guide = [' '] * args.cols
            guide[axis] = '|'
            print('   ' + ''.join(guide) + '   <- x=0.5 中轴')
        for line in render(block):
            print('   ' + line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
