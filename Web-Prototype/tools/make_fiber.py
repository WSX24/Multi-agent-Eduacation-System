#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# make_fiber.py — 生成可无缝平铺的分形纤维板（确定性、可复现）
#
# 为什么必须程序生成，不能靠一张 AI 贴图缩放：
#   物理上，把纸放大 2 倍，纤维也应当大 2 倍。要做到这一点，贴图在每一级
#   缩放上都必须有**那一级的细节**。用同一张 AI 贴图改 background-size 是
#   做不到的——把 1152px 的图塞进 34px 的平铺格，得到的是一块平色，不是
#   更细的纤维。原型 v4 的"三级自相似纸"就是这个问题：
#   最细那一层（34px 基准）实际上是一块接近纯色的底，还一直是全不透明，
#   于是中段缩放时它会透出来，把纸压平。
#
#   fBm（分形布朗噪声）在数学上就是自相似的：任意缩放层级上都有对应的
#   频率成分。所以它既是唯一正确的答案，也是唯一能真正"无限放大"的材质。
#
# 可无缝平铺的做法：每个倍频的格点频率都整除图幅（TILE 的因子），
# 插值时按模回绕，于是左右/上下边界天然接得上。
#
# 用法：
#   python tools/make_fiber.py --out source/paper-fiber.png --report qa/paper-fiber.json
# ═══════════════════════════════════════════════════════════════════════
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

TILE = 1024          # 图幅，必须是所有倍频频率的整数倍（见 OCTAVE_FREQS）
OCTAVE_FREQS = [16, 32, 64, 128, 256, 512]  # 全部整除 TILE，且都是 TILE/STRETCH_X 的倍数。
                                            # 去掉 4 与 8 两个低频：它们在图幅尺度的起伏
                                            # 会变成整块纸的明暗斑，实测块均亮极差 24.9 级，
                                            # 读起来就是「纸没铺满」。纤维要的是高频颗粒。
OCTAVE_GAIN = 0.70    # 每高一层幅度乘这个数。
                      # 0.5 时能量几乎全在低频，看上去是斑块而不是颗粒；
                      # 实测块内标准差只有 1.0，纸像一块平色。
SEED = 20260926
STRETCH_X = 2.0       # 横向拉伸：纸纤维有轻微方向性，各向同性的噪声更像砂纸
GRAIN_ALPHA = 0.42    # 最深处的不透明度。叠在纸上最暗处约 245→213，
                      # 均值约降到 227；再高会把纸压成灰（过不了亮纸门）。
# 双向颗粒：噪声低于中位的像素用暗色，高于中位的用白色，透明度按偏离中位的幅度。
# 单向压暗的纤维是行不通的：纸已经 241，任何纯暗色的叠加都会把整幅拉暗
# （实测单向 alpha 0.13 就把均亮从 241 压到 229）。双向之后暗色与白色互相抵消，
# 均值几乎不动，但局部有了起伏 —— 这才是"看得见颗粒又不压暗纸"的做法。
DARK = (196, 197, 202)   # 暗颗粒，刻意接近纸色；用近黑会立刻压暗整幅
LIGHT = (255, 254, 252)  # 亮颗粒


def smoothstep(t: np.ndarray) -> np.ndarray:
    return t * t * (3.0 - 2.0 * t)


def octave(freq_x: int, freq_y: int, rng: np.random.Generator) -> np.ndarray:
    """一层可平铺的值噪声。格点频率整除图幅，所以按模回绕即可无缝。"""
    lattice = rng.random((freq_y, freq_x)).astype(np.float64)

    ys = np.arange(TILE, dtype=np.float64) * freq_y / TILE
    xs = np.arange(TILE, dtype=np.float64) * freq_x / TILE
    y0 = np.floor(ys).astype(int) % freq_y
    x0 = np.floor(xs).astype(int) % freq_x
    y1 = (y0 + 1) % freq_y
    x1 = (x0 + 1) % freq_x
    fy = smoothstep(ys - np.floor(ys))[:, None]
    fx = smoothstep(xs - np.floor(xs))[None, :]

    v00 = lattice[np.ix_(y0, x0)]
    v01 = lattice[np.ix_(y0, x1)]
    v10 = lattice[np.ix_(y1, x0)]
    v11 = lattice[np.ix_(y1, x1)]
    top = v00 * (1 - fx) + v01 * fx
    bot = v10 * (1 - fx) + v11 * fx
    return top * (1 - fy) + bot * fy


def build() -> np.ndarray:
    rng = np.random.default_rng(SEED)
    total = np.zeros((TILE, TILE), dtype=np.float64)
    amp, norm = 1.0, 0.0
    for f in OCTAVE_FREQS:
        fx, fy = int(f / STRETCH_X), f
        if TILE % fx or TILE % fy:      # 不整除就无法无缝平铺，直接报错而不是猜
            raise SystemExit(f'倍频 {f} 派生的格点频率 {fx}x{fy} 不能整除图幅 {TILE}')
        total += amp * octave(fx, fy, rng)
        norm += amp
        amp *= OCTAVE_GAIN
    n = total / norm
    # 拉到 0..1 并轻微拉对比，让纤维可见但不脏
    n = (n - n.min()) / max(n.max() - n.min(), 1e-9)
    n = np.clip((n - 0.5) * 1.35 + 0.5, 0, 1)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description='生成可无缝平铺的分形纤维板')
    ap.add_argument('--out', required=True)
    ap.add_argument('--report')
    args = ap.parse_args()

    n = build()
    dev = n - 0.5
    alpha = (np.abs(dev) * 2 * GRAIN_ALPHA * 255).clip(0, 255).astype(np.uint8)
    rgb = np.zeros((TILE, TILE, 4), dtype=np.uint8)
    dark = dev < 0
    for ch in range(3):
        rgb[..., ch] = np.where(dark, DARK[ch], LIGHT[ch]).astype(np.uint8)
    rgb[..., 3] = alpha
    Image.fromarray(rgb, 'RGBA').save(args.out)

    # 无缝自检：左右/上下边界的一阶差分应当与内部同量级
    dx_edge = float(np.abs(n[:, 0] - n[:, -1]).mean())
    dy_edge = float(np.abs(n[0, :] - n[-1, :]).mean())
    dx_inner = float(np.abs(np.diff(n, axis=1)).mean())
    dy_inner = float(np.abs(np.diff(n, axis=0)).mean())
    seamless = dx_edge < dx_inner * 3 and dy_edge < dy_inner * 3

    print(f'== 分形纤维板 → {args.out} ==')
    print(f'图幅 {TILE}×{TILE}  倍频 {OCTAVE_FREQS}  横向拉伸 {STRETCH_X}:1')
    print(f'种子 {SEED}  峰值不透明度 {GRAIN_ALPHA}  双向：暗 {DARK} / 亮 {LIGHT}')
    print(f'颗粒覆盖率 {(alpha > 0).mean() * 100:.0f}%  平均不透明度 {alpha.mean() / 255:.3f}')
    print(f'噪声 均值 {n.mean():.4f}  标准差 {n.std():.4f}  '
          f'最暗 {n.min():.4f}  最亮 {n.max():.4f}')
    print(f'接缝自检 横 {dx_edge:.5f} vs 内部 {dx_inner:.5f} · '
          f'纵 {dy_edge:.5f} vs 内部 {dy_inner:.5f} → '
          f'{"无缝" if seamless else "有缝，需返工"}')

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            'output': args.out, 'tile': TILE, 'octaveFreqs': OCTAVE_FREQS,
            'octaveGain': OCTAVE_GAIN, 'seed': SEED, 'stretchX': STRETCH_X,
            'grainAlpha': GRAIN_ALPHA, 'dark': DARK, 'light': LIGHT,
            'noise': {'mean': float(n.mean()), 'std': float(n.std()),
                      'min': float(n.min()), 'max': float(n.max())},
            'grainAlphaMean': float(alpha.mean() / 255),
            'grainCoverage': float((alpha > 0).mean()),
            'twoSided': True,
            'seam': {'dxEdge': dx_edge, 'dxInner': dx_inner,
                     'dyEdge': dy_edge, 'dyInner': dy_inner, 'seamless': seamless},
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'→ {args.report}')

    return 0 if seamless else 1


if __name__ == '__main__':
    raise SystemExit(main())
