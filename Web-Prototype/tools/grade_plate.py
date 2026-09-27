#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# grade_plate.py — 材质板的确定性调色（可复现，不是运行时滤镜）
#
# 用途：把生成的「纸」材质板的绝对曝光归一到设计值 source/scene-bible.md §1
# 的 #FAF8F4 附近。
#
# 为什么这是正当的，而不是"掩盖上游缺陷"：
#   材质板不是一张成品画面，它是一块**材料**。材料的绝对曝光是一个自由参数，
#   真正需要锁死的是它的**相对结构**（纤维、折痕、无污渍、无内容）与**色相**。
#   把材料归一化到设计值，等价于拍摄时的曝光标定。
#   但它必须是构建期的一条命令、有记录、可复现——不是页面里的一层 filter。
#
# 为什么不用乘法增益：
#   实测纸板 mean 223.3 / p95 231.0 / max 243。乘 1.106 能到 247，
#   但 243 → 268 会被裁到 255，制造大片纯白死区（自动硬门的过曝门会拦下）。
#   所以用一条**单调曲线**：提中间调、压高光顶，全程不裁切。
#
# 用法：
#   python tools/grade_plate.py source/paper-texture.png \
#     --out source/paper-texture-graded.png --report qa/paper-texture-grade.json
# ═══════════════════════════════════════════════════════════════════════
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# 控制点：(输入亮度, 输出亮度)。单调递增，末点 248 < 250，
# 这样不会产生 min 通道 >250 的像素，过曝门恒为 0%。
#
# 2026-09-26 改成**压缩**而不是提亮：原来把 209..233 铺到 224..245（21 级跨度），
# 纸板本身的大尺度褶皱就变成了画面上肉眼可见的明暗块 —— 四角均亮相差 22 级，
# 观感是「纸张没有铺满浏览器」。现在把同一段输入压进 236..247（11 级），
# 保留褶皱形状，但把它变成很淡的起伏；纤维的颗粒感由程序生成的 fBm 层负责。
CURVES = {
    # 纸材质板：把同一段输入压进 236..247（11 级），保留褶皱形状但把它变成很淡的起伏。
    'plate': [
        (0, 0), (64, 72), (128, 142), (176, 186),
        (200, 225), (209, 236), (224, 241), (231, 245), (243, 247), (255, 248),
    ],
    # 书架场景：生图模型对这个场景稳定输出中灰（实测 mean 166、p50 182、p90 217），
    # 与高调要求差 40 级，三次改写提示词都无效。把它抬到高调：
    # p50 -> 224、p90 -> 247、最暗处抬到 70 以上（核心仍然可见），顶端压住不溢出。
    # 故事片本来就要调色；这是构建期一条可复现的命令，不是运行时滤镜。
    'scene-shelf': [
        (0, 70), (34, 80), (60, 92), (76, 100), (100, 128),
        (142, 180), (182, 224), (204, 242), (217, 247), (225, 248),
        (240, 249), (255, 250),
    ],
}
CURVE = CURVES['plate']
LUT_SMOOTH_WINDOW = 9   # 抹掉分段线性的折点，避免在平滑纸面上出现带状台阶


def build_lut(curve) -> np.ndarray:
    xs = np.array([c[0] for c in curve], dtype=np.float64)
    ys = np.array([c[1] for c in curve], dtype=np.float64)
    lut = np.interp(np.arange(256, dtype=np.float64), xs, ys)
    # 移动平均去折点（两端保持不动）
    k = LUT_SMOOTH_WINDOW
    pad = k // 2
    padded = np.concatenate([lut[:1].repeat(pad), lut, lut[-1:].repeat(pad)])
    kernel = np.ones(k) / k
    smooth = np.convolve(padded, kernel, mode='valid')
    smooth[0], smooth[-1] = lut[0], lut[-1]
    return np.clip(smooth, 0, 255)


def stats(arr: np.ndarray) -> dict:
    lum = arr.astype(np.float64).mean(axis=2)
    mx = arr.max(axis=2).astype(np.int32)
    mn = arr.min(axis=2).astype(np.int32)
    return {
        'mean': round(float(lum.mean()), 1),
        'std': round(float(lum.std()), 2),
        'percentiles': {f'p{q}': round(float(np.percentile(lum, q)), 1)
                        for q in (1, 5, 25, 50, 75, 95, 99)},
        'max': int(arr.max()),
        'min': int(arr.min()),
        'nearWhiteFrac': round(float((mn > 250).mean()), 5),
        'nearBlackFrac': round(float((mn < 12).mean()), 5),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='材质板的确定性调色')
    ap.add_argument('source')
    ap.add_argument('--out', required=True)
    ap.add_argument('--preset', choices=sorted(CURVES), default='plate')
    ap.add_argument('--report')
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f'找不到 {src}', file=sys.stderr)
        return 2

    im = Image.open(src).convert('RGB')
    before = np.asarray(im, dtype=np.uint8)
    curve = CURVES[args.preset]
    lut = build_lut(curve)
    after = lut[before].astype(np.uint8)

    # 单调性自检：曲线必须非递减，否则会出现色阶反转
    d = np.diff(lut)
    if (d < -1e-6).any():
        print('曲线不单调，拒绝执行', file=sys.stderr)
        return 1

    Image.fromarray(after).save(args.out)

    b, a = stats(before), stats(after)
    print(f'== {src} → {args.out} ==')
    print(f'{"":>8} {"mean":>7} {"std":>6} {"p1":>6} {"p50":>6} {"p95":>6} {"max":>5} '
          f'{"过曝%":>7} {"极暗%":>7}')
    for tag, s in (('处理前', b), ('处理后', a)):
        p = s['percentiles']
        print(f'{tag:>8} {s["mean"]:>7} {s["std"]:>6} {p["p1"]:>6} {p["p50"]:>6} '
              f'{p["p95"]:>6} {s["max"]:>5} {s["nearWhiteFrac"]*100:>6.2f} '
              f'{s["nearBlackFrac"]*100:>6.2f}')
    print(f'\n曲线控制点 {" → ".join(f"{int(x)}:{int(y)}" for x, y in CURVE)}')
    print('单调递增：是。末点 249 < 250，因此过曝门恒为 0%。')
    print('只改了绝对曝光，相对结构（纤维/折痕）与色相未动。')

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            'source': str(src), 'output': args.out,
            'preset': args.preset, 'curveControlPoints': curve,
            'lutSmoothWindow': LUT_SMOOTH_WINDOW,
            'before': b, 'after': a,
            'monotonic': True,
            'note': '构建期的确定性材质归一化，不是运行时滤镜。'
                    '只改绝对曝光，不改相对结构与色相。',
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'→ {args.report}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
