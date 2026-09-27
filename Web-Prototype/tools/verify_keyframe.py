#!/usr/bin/env python3
"""白底关键帧验收与归一化。

背景路线是「白底画在纯白上 → 页面也是纯白 → 背景自然消失」，所以这张图不需要
alpha，也不需要抠色。但它必须满足三条硬条件，否则页面上会露出一块灰方块：

  1. 四角与整圈边距必须是纯白，且方差极低（没有渐变、暗角、投影、光晕）。
  2. 主体必须完整落在画布内，四周留出足够余量（下蹲与落地挤压需要形变空间）。
  3. 近白像素归一化到 #FFFFFF，与页面的 --bg 精确一致。

第 3 条不是抠图：背景依然留在图里，只是把它对齐到页面背景色。像素增量 <= 5/255，
肉眼不可见，但能保证不会因为 #FDFDFC 与 #FFFFFF 的差而露出一条边。

用法：
  python3 tools/verify_keyframe.py source/K0.png            # 只验收
  python3 tools/verify_keyframe.py source/K0.png --normalize # 验收并就地归一化
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from PIL import Image

WHITE = 255
NEAR_WHITE = 250          # 归一化阈值：所有通道都 >= 250 才算「背景白」
SUBJECT_DELTA = 12        # 与纯白的通道差超过它就认为是主体像素
BORDER = 0.06             # 检查的背景边距厚度（占画布比例）

# 主体必须落在这些范围内（见 source/identity-bible.md）
LIMITS = {"x_min": 0.10, "x_max": 0.90, "y_min": 0.05, "y_max": 0.95}
# 脚底接触线的合理区间
FEET_MIN, FEET_MAX = 0.84, 0.94


def analyze(path: Path) -> dict:
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()

    corners = {(0, 0): px[0, 0], (w - 1, 0): px[w - 1, 0],
               (0, h - 1): px[0, h - 1], (w - 1, h - 1): px[w - 1, h - 1]}

    # 背景边距统计
    b = max(1, int(min(w, h) * BORDER))
    border_vals, near_white = [], 0
    for y in range(h):
        for x in range(w):
            if x < b or x >= w - b or y < b or y >= h - b:
                r, g, bl = px[x, y]
                border_vals.append((r + g + bl) / 3)
                if min(r, g, bl) >= NEAR_WHITE:
                    near_white += 1

    # 主体 bbox 与逐行宽度（用于找脚底线）
    minx, maxx, miny, maxy = w, -1, h, -1
    rows_with_subject = []
    for y in range(h):
        row_has = False
        for x in range(w):
            r, g, bl = px[x, y]
            if (WHITE - r) > SUBJECT_DELTA or (WHITE - g) > SUBJECT_DELTA or (WHITE - bl) > SUBJECT_DELTA:
                row_has = True
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
        if row_has:
            rows_with_subject.append(y)

    return {
        "size": (w, h), "corners": corners, "border_mean": statistics.fmean(border_vals),
        "border_stdev": statistics.pstdev(border_vals),
        "border_min": min(border_vals), "near_white_ratio": near_white / len(border_vals),
        "bbox": (minx, miny, maxx, maxy),
        "frac": {"x0": minx / w, "x1": maxx / w, "y0": miny / h, "y1": maxy / h},
        "feet": (maxy / h) if maxy >= 0 else None,
        "width_ratio": (maxx - minx + 1) / w if maxx >= 0 else 0,
    }


def report(a: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    print(f"尺寸            {a['size'][0]}x{a['size'][1]}")
    print("四角            " + "  ".join(f"{k}={v}" for k, v in a["corners"].items()))
    print(f"背景边距        均值 {a['border_mean']:.2f}  标准差 {a['border_stdev']:.3f}  "
          f"最小 {a['border_min']:.1f}  近白占比 {a['near_white_ratio']*100:.2f}%")
    f = a["frac"]
    print(f"主体 bbox       x {f['x0']:.3f}..{f['x1']:.3f}  y {f['y0']:.3f}..{f['y1']:.3f}"
          f"  横向占宽 {a['width_ratio']:.3f}")
    print(f"脚底接触线      {a['feet']:.3f}" if a["feet"] else "脚底接触线      未检测到主体")

    if a["border_min"] < NEAR_WHITE:
        problems.append(f"背景边距出现非白像素（最暗 {a['border_min']:.1f}）：有渐变、暗角或投影")
    if a["border_stdev"] > 1.5:
        problems.append(f"背景不平（标准差 {a['border_stdev']:.2f}）：有渐变或噪点")
    if f["x0"] < LIMITS["x_min"] or f["x1"] > LIMITS["x_max"]:
        problems.append(f"主体横向越界（{f['x0']:.3f}..{f['x1']:.3f}），"
                        f"要求落在 {LIMITS['x_min']}..{LIMITS['x_max']}")
    if f["y0"] < LIMITS["y_min"] or f["y1"] > LIMITS["y_max"]:
        problems.append(f"主体纵向越界（{f['y0']:.3f}..{f['y1']:.3f}），"
                        f"要求落在 {LIMITS['y_min']}..{LIMITS['y_max']}")
    if a["width_ratio"] > 0.72:
        problems.append(f"主体过大（横向占宽 {a['width_ratio']:.2f}）：下蹲与落地挤压没有形变余量")
    if a["feet"] and not (FEET_MIN <= a["feet"] <= FEET_MAX):
        problems.append(f"脚底接触线 {a['feet']:.3f} 不在 {FEET_MIN}..{FEET_MAX}")
    return (not problems), problems


def normalize(path: Path, threshold: int = NEAR_WHITE) -> int:
    im = Image.open(path).convert("RGB")
    px = im.load()
    w, h = im.size
    changed = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            if min(r, g, b) >= threshold and (r, g, b) != (WHITE, WHITE, WHITE):
                px[x, y] = (WHITE, WHITE, WHITE)
                changed += 1
    im.save(path, "PNG", optimize=True)
    return changed


def fit(path: Path, out: Path, width_ratio: float, height_ratio: float,
        feet: float, canvas_size: tuple[int, int] | None = None
        ) -> tuple[float, tuple[int, int, int, int]]:
    """把主体重新排版到规格内的位置。

    生图模型很爱把主体画满整张画布，而契约要求主体占宽 <=0.60、脚在
y=0.90、四周留白。因为背景已经是验证过的纯白，重新排版只是
    缩放 + 居中贴回纯白画布，不涉及抠图、不引入色边，
    而且是确定性的，不用再花一次生成额度。

    canvas_size 给定时，输出用这个尺寸的画布（可以比源图大）。
    例如把用户给的 602×595 参考图重新排成 1024×1024 母版。
    返回值里的 scale > 1 表示主体被放大了，调用方要报出来。
    """
    im = Image.open(path).convert("RGB")
    sw0, sh0 = im.size
    px = im.load()
    minx, miny, maxx, maxy = sw0, sh0, -1, -1
    for y in range(sh0):
        for x in range(sw0):
            r, g, b = px[x, y]
            if (WHITE - r) > SUBJECT_DELTA or (WHITE - g) > SUBJECT_DELTA or (WHITE - b) > SUBJECT_DELTA:
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    if maxx < 0:
        raise SystemExit("找不到主体，无法 --fit")

    cw, ch = canvas_size or (sw0, sh0)
    sw, sh = maxx - minx + 1, maxy - miny + 1
    scale = min(width_ratio * cw / sw, height_ratio * ch / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    subject = im.crop((minx, miny, maxx + 1, maxy + 1)).resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("RGB", (cw, ch), (WHITE, WHITE, WHITE))
    left = round(cw * 0.5 - nw / 2)
    top = round(ch * feet - nh)
    canvas.paste(subject, (left, top))
    canvas.save(out, "PNG", optimize=True)
    return scale, (left, top, nw, nh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--normalize", action="store_true",
                    help="把近白像素对齐到纯白，与页面 --bg 精确一致")
    ap.add_argument("--near-white", type=int, default=NEAR_WHITE,
                    help="归一化阈值（所有通道 >= 此值才算背景白）。"
                         "生图模型常在背景上加 235–249 的浅灰，默认 250 抓不到；"
                         "调到 232 可以消掉那种灰底，而本形象最亮的面板是 216，仍有 16 级余量")
    ap.add_argument("--fit", action="store_true",
                    help="把主体重新排版到规格内的位置（纯白底上的确定性缩放+居中）")
    ap.add_argument("--fit-width", type=float, default=0.58, help="目标横向占宽")
    ap.add_argument("--fit-height", type=float, default=0.78, help="目标纵向占高")
    ap.add_argument("--fit-feet", type=float, default=0.90, help="脚底接触线目标位置")
    ap.add_argument("--fit-canvas", default=None,
                    help="输出画布尺寸 WxH（如 1024x1024）；不给就沿用源图尺寸")
    args = ap.parse_args()

    canvas_size = None
    if args.fit_canvas:
        cw, ch = args.fit_canvas.lower().split("x")
        canvas_size = (int(cw), int(ch))

    print("=== 排版前 ===")
    ok, problems = report(analyze(args.image))

    if args.fit:
        scale, box = fit(args.image, args.image, args.fit_width,
                         args.fit_height, args.fit_feet, canvas_size)
        warn = "  <-- 注意：主体被放大了" if scale > 1.05 else ""
        print(f"\n=== --fit：缩放 {scale:.3f}，贴到 left={box[0]} top={box[1]} "
              f"尺寸 {box[2]}x{box[3]}{warn} ===")
        print("\n=== 排版后 ===")
        ok, problems = report(analyze(args.image))

    if args.normalize and not problems:
        n = normalize(args.image, args.near_white)
        print(f"\n归一化：{n} 个近白像素（阈值 {args.near_white}）对齐到 #FFFFFF")
        print("\n=== 归一化后 ===")
        ok, problems = report(analyze(args.image))
    elif args.normalize:
        print("\n有阻断项，跳过归一化")

    if problems:
        print("\n[NG] 阻断项：")
        for p in problems:
            print("  [NG]", p)
        return 2
    print("\n[OK] 关键帧验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
