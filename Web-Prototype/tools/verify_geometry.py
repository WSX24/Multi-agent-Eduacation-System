#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# verify_geometry.py — 首屏的**几何**硬门（交并比）
#
# 为什么需要它：
#   亮纸占比 / p95 / 过曝 / 紫色面积这些门限量的是**色调与明度**。
#   「整幅画被画到了错的位置」在这四条上完全看不出来 —— 画在错的地方，
#   仍然是一张合格的纸。实测：css/base.css 的全局 `img, svg { max-width:100% }`
#   把墨层的坐标压成 0.667 倍并居中，九张门限截图全部通过，而墨覆盖率从
#   4.5% 掉到 1.8%。是靠草稿台与真实页面互相对照才发现的。
#
# 这个工具补上那一块：把构建产物里的采样点按**同一个相机变换**投到屏幕坐标，
# 光栅化成"理论上墨应该在哪"的掩模，再与截图里实际是墨的像素求交并比。
#
#   IoU 接近 1  → 几何一致
#   IoU 接近 0  → 画在别处（或没画出来）
#
# 它不替代色调门，两者是正交的：色调门管"纸对不对"，几何门管"画在哪"。
#
# 用法：
#   python tools/verify_geometry.py qa/page-p0.62.png --p 0.62 --report qa/geo-0.62.json
#   python tools/verify_geometry.py qa/page-p0.62.png --p 0.62 --exclude-top 80
# ═══════════════════════════════════════════════════════════════════════
import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

BUILD_PATH = Path('build/line-runtime-data.js')

# 判定"是墨"的亮度上限。取值靠阈值扫描定：笔画在 s=0.5 时只有约 2px 宽，
# 抗锯齿把多数像素与纸混到 180 以上，所以 160 会把尾部整段判为"没有墨"
# （实测 p=1.00 时 IoU 只有 0.51、面积比 0.57）。
# 205 能把抗锯齿边缘算进来，同时远低于纸（235–245）与稿纸格（约 231）。
INK_LUMA_MAX = 205

# 门限。下面这组数字来自阈值扫描（qa/geometry-calibration.md）：
#   正确几何：IoU 0.77–0.84，面积比 0.99–1.19，重心为对角线的 0.5%–2%
#   注入故障（重新引入 max-width）：IoU 0.014–0.029，重心偏差 197–219px
# 两者之间留了很大余量，所以门限不敏感。
IOU_MIN = 0.65
AREA_RATIO_MIN, AREA_RATIO_MAX = 0.75, 1.45
# 重心用**相对**门限：绝对像素数随缩放变化，没有意义。
CENTROID_MAX_FRAC = 0.03      # 不超过理论包围盒对角线的 3%
# 理论墨像素少于这个数时不判 IoU（p≈0 时画面本来就没有墨）。
MIN_EXPECTED_PX = 400


def load_runtime(path: Path) -> dict:
    text = path.read_text(encoding='utf-8')
    m = re.search(r'window\.OIL_LINE_RUNTIME\s*=\s*(\{.*\})\s*;', text, re.S)
    if not m:
        raise SystemExit(f'{path} 里找不到 window.OIL_LINE_RUNTIME')
    return json.loads(m.group(1))


# ── 与运行时逐行对应的相机与路径数学 ───────────────────────────────────
def scale_at(p, C):
    if p <= C['turn']:
        return C['s0'] * (C['sMax'] / C['s0']) ** (p / C['turn'])
    return C['sMax'] * (C['sEnd'] / C['sMax']) ** ((p - C['turn']) / (1 - C['turn']))


def index_at(p, RT):
    """进度 -> 采样点序号（浮点）。构建期已折进速度补偿与可见长度加权。"""
    lut = RT['sampleAt']
    n = len(lut) - 1
    x = min(1.0, max(0.0, p)) * n
    k = min(n - 1, int(math.floor(x)))
    f = x - k
    return lut[k] * (1 - f) + lut[k + 1] * f


def tip_at(p, RT):
    N = RT['samples']
    pts, ws = RT['points'], RT['widths']
    kf = index_at(p, RT)
    kI = min(N, max(0, int(math.floor(kf))))
    f = min(1.0, max(0.0, kf - kI))
    a, b = pts[kI], pts[min(N, kI + 1)]
    return {
        'k': kI, 'f': f,
        'x': a[0] + (b[0] - a[0]) * f,
        'y': a[1] + (b[1] - a[1]) * f,
        'w': ws[kI] * (1 - f) + ws[min(N, kI + 1)] * f,
    }


def ribbon_polygon(RT, k, tail):
    """与 js/line-runtime.js 的 ribbon() 同构：左偏移正向 + 末端半圆帽
    + 右偏移反向 + 起端半圆帽，得到一个简单闭合环。"""
    pts, ws, ns = RT['points'], RT['widths'], RT['normals']
    cap = RT['ribbon']['capSteps']
    L, R = [], []
    for j in range(0, k + 1):
        h = ws[j] / 2
        L.append((pts[j][0] + ns[j][0] * h, pts[j][1] + ns[j][1] * h))
        R.append((pts[j][0] - ns[j][0] * h, pts[j][1] - ns[j][1] * h))

    def cap_pts(x, y, h, nx, ny, tx, ty, out):
        l = math.hypot(tx, ty) or 1.0
        tx, ty = tx / l, ty / l
        sgn = 1 if out else -1
        res = []
        for s in range(1, cap):
            ang = math.pi * s / cap
            cx, cy = math.cos(ang) * sgn, math.sin(ang) * sgn
            res.append((x + h * (nx * cx + tx * cy), y + h * (ny * cx + ty * cy)))
        return res

    if tail is not None:
        L.append((tail['x'] + tail['nx'] * tail['h'], tail['y'] + tail['ny'] * tail['h']))
        R.append((tail['x'] - tail['nx'] * tail['h'], tail['y'] - tail['ny'] * tail['h']))
        end = cap_pts(tail['x'], tail['y'], tail['h'], tail['nx'], tail['ny'],
                      tail['tx'], tail['ty'], True)
    else:
        j = min(len(pts) - 1, k)
        a, b = pts[max(0, j - 1)], pts[min(len(pts) - 1, j + 1)]
        end = cap_pts(pts[j][0], pts[j][1], ws[j] / 2, ns[j][0], ns[j][1],
                      b[0] - a[0], b[1] - a[1], True)
    poly = L + end + list(reversed(R))
    j0 = 0
    a0, b0 = pts[max(0, j0 - 1)], pts[min(len(pts) - 1, j0 + 1)]
    poly += cap_pts(pts[j0][0], pts[j0][1], ws[j0] / 2, ns[j0][0], ns[j0][1],
                    b0[0] - a0[0], b0[1] - a0[1], False)
    return poly


def expected_mask(RT, p, W, H, exclude_top):
    """把"理论上墨应该在哪"光栅化成布尔掩模。"""
    C, A = RT['camera'], RT['anchor']
    SC = RT['path']['sheetCentre']
    t = tip_at(p, RT)
    s = scale_at(p, C)
    bias = A['bias']
    ax = t['x'] + (SC['x'] - t['x']) * bias
    ay = t['y'] + (SC['y'] - t['y']) * bias
    tx, ty = W / 2 - ax * s, H / 2 - ay * s

    N = RT['samples']
    kGlow = int(round(N * RT['colour']['glowFrom']))
    th = {'x': t['x'], 'y': t['y'],
          'h': t['w'] / 2,
          'nx': RT['normals'][t['k']][0], 'ny': RT['normals'][t['k']][1],
          'tx': RT['points'][min(N, t['k'] + 1)][0] - RT['points'][t['k']][0],
          'ty': RT['points'][min(N, t['k'] + 1)][1] - RT['points'][t['k']][1]}

    polys = []
    if t['k'] < kGlow:
        polys.append(ribbon_polygon(RT, t['k'], th))
    else:
        polys.append(ribbon_polygon(RT, kGlow, None))
        polys.append(ribbon_polygon(RT, t['k'], th))

    img = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(img)
    for poly in polys:
        d.polygon([(x * s + tx, y * s + ty) for x, y in poly], fill=255)
    arr = np.asarray(img) > 0
    if exclude_top:
        arr[:exclude_top, :] = False
    return arr, (tx, ty, s)


def main() -> int:
    ap = argparse.ArgumentParser(description='首屏几何硬门（交并比）')
    ap.add_argument('image')
    ap.add_argument('--p', type=float, required=True, help='该截图对应的进度')
    ap.add_argument('--build', default=str(BUILD_PATH))
    ap.add_argument('--exclude-top', type=int, default=0,
                    help='排除顶部若干行的像素（固定顶栏会盖住墨）。'
                         '推荐改用 ?chrome=0 直接从 DOM 上关掉装饰层')
    ap.add_argument('--compare',
                    help='同一进度的另一张截图（通常是带装饰层的）。'
                         '用来量"页面装饰层把墨压淡了多少"：'
                         '报告 墨像素比 = 本图 / 对照图。')
    ap.add_argument('--compare-min', type=float, default=0.75,
                    help='墨像素比下限（默认 0.75）')
    ap.add_argument('--report')
    args = ap.parse_args()

    img_path = Path(args.image)
    if not img_path.exists():
        print(f'找不到 {img_path}', file=sys.stderr)
        return 2
    RT = load_runtime(Path(args.build))

    rgb = np.asarray(Image.open(img_path).convert('RGB'), dtype=np.float32)
    H, W = rgb.shape[:2]
    luma = rgb.mean(axis=2)

    actual = luma < INK_LUMA_MAX
    expect, tf = expected_mask(RT, args.p, W, H, args.exclude_top)
    if args.exclude_top:
        actual[:args.exclude_top, :] = False

    inter = int((actual & expect).sum())
    union = int((actual | expect).sum())
    a_n, e_n = int(actual.sum()), int(expect.sum())
    degenerate = e_n < MIN_EXPECTED_PX
    iou = inter / union if union else 0.0
    ratio = a_n / e_n if e_n else 0.0

    def centroid(mask):
        ys, xs = np.nonzero(mask)
        return (xs.mean(), ys.mean()) if len(xs) else (float('nan'), float('nan'))

    acx, acy = centroid(actual)
    ecx, ecy = centroid(expect)
    dist = math.hypot(acx - ecx, acy - ecy)
    # 相对门限：以理论包围盒的对角线为尺度
    ys, xs = np.nonzero(expect)
    diag = math.hypot(xs.max() - xs.min(), ys.max() - ys.min()) if len(xs) else 0.0
    dist_frac = dist / diag if diag > 0 else 0.0

    bad = []
    if degenerate:
        # p≈0：画面本来就不该有墨。只要求实测墨也不多。
        if a_n > MIN_EXPECTED_PX:
            bad.append(f'理论墨只有 {e_n} 像素，实测却有 {a_n} —— 多画了东西')
    else:
        if iou < IOU_MIN:
            bad.append(f'交并比 {iou:.3f} < {IOU_MIN}')
        if dist_frac > CENTROID_MAX_FRAC:
            bad.append(f'重心偏差 {dist:.1f}px（对角线的 {dist_frac:.1%}）'
                       f' > {CENTROID_MAX_FRAC:.0%}')
        if not (AREA_RATIO_MIN <= ratio <= AREA_RATIO_MAX):
            bad.append(f'墨面积比 {ratio:.2f} 不在 {AREA_RATIO_MIN}–{AREA_RATIO_MAX}')

    print(f'== 几何硬门 {img_path}  p={args.p} ==')
    print(f'画布 {W}×{H}  相机 translate=({tf[0]:.1f}, {tf[1]:.1f}) scale={tf[2]:.3f}'
          + (f'  排除顶部 {args.exclude_top} 行' if args.exclude_top else ''))
    if degenerate:
        print(f'理论墨像素 {e_n}（少于 {MIN_EXPECTED_PX}，p 接近 0，不判交并比）'
              f'  实测墨像素 {a_n}')
    else:
        print(f'实测墨像素 {a_n}   理论墨像素 {e_n}   面积比 {ratio:.3f}')
        print(f'交并比 IoU {iou:.4f}（门限 {IOU_MIN}）')
        print(f'重心  实测 ({acx:.1f}, {acy:.1f})  理论 ({ecx:.1f}, {ecy:.1f})  '
              f'偏差 {dist:.2f}px = 对角线的 {dist_frac:.2%}')

    cmp_note = None
    if args.compare:
        cpath = Path(args.compare)
        if not cpath.exists():
            bad.append(f'对照图 {cpath} 不存在')
        else:
            crgb = np.asarray(Image.open(cpath).convert('RGB'), dtype=np.float32)
            cmask = crgb.mean(axis=2) < INK_LUMA_MAX
            if args.exclude_top:
                cmask[:args.exclude_top, :] = False
            cn = int(cmask.sum())
            cmp_note = cn / a_n if a_n else float('inf')
            print(f'对照 {cpath}  墨像素 {cn}   墨像素比 {cmp_note:.3f}'
                  f'（下限 {args.compare_min}）')
            if cmp_note < args.compare_min:
                bad.append(f'装饰层把墨压掉太多：墨像素比 {cmp_note:.3f} < {args.compare_min}')

    print()
    if bad:
        print('几何硬门：不通过')
        for b in bad:
            print(f'  · {b}')
        print('  IoU 接近 0 通常意味着整幅画被画到了别处 —— 优先查')
        print('  css/base.css 的全局 `img, svg { max-width: 100% }` 之类')
        print('  会改变 SVG 坐标系的规则。')
    else:
        print('几何硬门：通过')

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            'image': str(img_path), 'p': args.p, 'excludeTop': args.exclude_top,
            'canvas': [W, H], 'camera': {'tx': tf[0], 'ty': tf[1], 'scale': tf[2]},
            'actualInkPixels': a_n, 'expectedInkPixels': e_n, 'areaRatio': ratio,
            'iou': iou, 'centroidActual': [acx, acy], 'centroidExpected': [ecx, ecy],
            'centroidDistancePx': dist, 'centroidDistanceFrac': dist_frac,
            'expectedDiagonalPx': diag, 'degenerate': degenerate,
            'compareInkRatio': cmp_note, 'passed': not bad, 'problems': bad,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'→ {args.report}')
    return 0 if not bad else 1


if __name__ == '__main__':
    raise SystemExit(main())
