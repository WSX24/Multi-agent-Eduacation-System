#!/usr/bin/env python3
"""视频段验收：锚点锁定、背景连续性、肢体分离度。

Pilot 与母版硬门的自动化部分（人工仍需看接触表）。判定依据：

  1. 分辨率   至少覆盖最大 CSS 尺寸 × 目标 DPR。
  2. 锚点锁定 脚底接触线在整段里不得漂移；这是链条与程序定位的前提。
  3. 背景连续 四周边距必须持续是纯白（无渐变、无投影、无暗色物）。
  4. 肢体分离 中段每行分段数应 >=2（左臂|躯干|右臂 在手臂区应为 3）。
               粘连说明模型把手臂糊进了躯干，“肢体能摆动”不成立。
  5. 动作到位 身高必须真的下降（下蹲）或升降符合该段预期。

用法：
  python3 tools/verify_segment.py source/seg-01-charge.mp4 \
      --expect-compress --expect-feet 0.898 --min-width-px 716
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

WHITE = 255
NEAR_WHITE = 244


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=True).stdout
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    return info


def extract(path: Path, count: int, dest: Path) -> list[Path]:
    total = int(probe(path).get("nb_frames", "0") or 0)
    if total <= 0:
        raise SystemExit("读不到帧数")
    idx = [round(i * (total - 1) / (count - 1)) for i in range(count)]
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for n, i in enumerate(idx):
        f = dest / f"f{n:02d}_n{i}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                        "-vf", f"select='eq(n\\,{i})'", "-frames:v", "1", str(f)],
                       check=True)
        files.append(f)
    return files


def frame_stats(f: Path) -> dict:
    im = Image.open(f).convert("RGB")
    w, h = im.size
    px = im.load()

    def isbg(c):
        return min(c) >= NEAR_WHITE

    b = max(1, int(min(w, h) * 0.06))
    border = [sum(px[x, y]) / 3
              for y in range(h) for x in range(w)
              if x < b or x >= w - b or y < b or y >= h - b]

    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if not isbg(px[x, y]):
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y

    def segments_at(fy: float) -> int:
        y = min(h - 1, int(fy * h))
        xs = [x for x in range(w) if not isbg(px[x, y])]
        if not xs:
            return 0
        n = 1
        for i in range(1, len(xs)):
            if xs[i] - xs[i - 1] > 3:
                n += 1
        return n

    return {
        "w": w, "h": h,
        "border_mean": statistics.fmean(border), "border_min": min(border),
        "bbox": (minx, miny, maxx, maxy),
        "x0": minx / w, "x1": maxx / w, "width": (maxx - minx + 1) / w,
        "cx": (minx + maxx) / 2 / w,
        "head": miny / h, "feet": maxy / h, "stature": (maxy - miny) / h,
        "segs": {fy: segments_at(fy) for fy in (0.45, 0.55, 0.65, 0.80)},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--expect-feet", type=float, default=0.898)
    ap.add_argument("--feet-tol", type=float, default=0.015)
    ap.add_argument("--max-width", type=float, default=0.76,
                    help="主体最大占宽。媒体是 1:1 且中心在 0.50，"
                         "所以 0.76 对应 x 0.12..0.88，仍在画布内；"
                         "这个上限只为保护形变余量，不是画布边界")
    ap.add_argument("--min-width-px", type=int, default=716,
                    help="最大 CSS 尺寸 × 目标 DPR")
    ap.add_argument("--border-min", type=float, default=246.0)
    ap.add_argument("--expect-compress", action="store_true",
                    help="该段应让身高下降（下蹲 / 落地挤压）")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    info = probe(args.video)
    print(f"== {args.video}")
    print(f"   {info.get('width')}x{info.get('height')}  {info.get('r_frame_rate')}  "
          f"{info.get('nb_frames')} 帧  {info.get('duration')}s")

    tmp = Path(tempfile.mkdtemp(prefix="segqa-"))
    problems: list[str] = []
    try:
        files = extract(args.video, args.samples, tmp)
        stats = [frame_stats(f) for f in files]

        print(f"\n   {'帧':>4} {'占宽':>6} {'中心x':>6} {'头顶':>6} {'脚底':>6} "
              f"{'身高':>6} {'边距min':>8}  分段@0.45/0.55/0.65/0.80")
        for f, s in zip(files, stats):
            print(f"   {f.name.split('_')[0]:>4} {s['width']:6.3f} {s['cx']:6.3f} "
                  f"{s['head']:6.3f} {s['feet']:6.3f} {s['stature']:6.3f} "
                  f"{s['border_min']:8.1f}  "
                  + "/".join(str(s['segs'][k]) for k in (0.45, 0.55, 0.65, 0.80)))

        w = int(info.get("width", 0))
        if w < args.min_width_px:
            problems.append(f"分辨率 {w}px 低于需要的 {args.min_width_px}px")

        feet = [s["feet"] for s in stats]
        drift = max(feet) - min(feet)
        if drift > args.feet_tol:
            problems.append(f"脚底接触线漂移 {drift:.3f}（上限 {args.feet_tol}）："
                            f"{min(feet):.3f}..{max(feet):.3f}")
        elif abs(statistics.fmean(feet) - args.expect_feet) > args.feet_tol:
            problems.append(f"脚底接触线均值 {statistics.fmean(feet):.3f} "
                            f"偏离期望 {args.expect_feet}")

        cx = [s["cx"] for s in stats]
        if max(cx) - min(cx) > 0.05:
            problems.append(f"水平中心漂移 {max(cx)-min(cx):.3f}")

        worst_border = min(s["border_min"] for s in stats)
        if worst_border < args.border_min:
            problems.append(f"背景边距出现非白像素（最暗 {worst_border:.1f}）："
                            f"有渐变、投影或暗色物")

        widest = max(s["width"] for s in stats)
        if widest > args.max_width:
            problems.append(f"主体最宽 {widest:.3f} 超过上限 {args.max_width}")

        # 手臂区（0.55 与 0.65）分段数：低于 2 说明手臂糊进躯干
        merged = [f"{k}={s['segs'][k]}" for s in stats for k in (0.55, 0.65)
                  if s["segs"][k] < 2]
        if merged:
            problems.append("手臂与躯干粘连（分段数 <2）：" + ", ".join(merged[:8]))

        if args.expect_compress:
            drop = stats[0]["stature"] - stats[-1]["stature"]
            if drop < 0.05:
                problems.append(f"身高只下降 {drop:.3f}：下蹲/挤压幅度不足")
            else:
                print(f"\n   身高压缩 {stats[0]['stature']:.3f} → "
                      f"{stats[-1]['stature']:.3f}（下降 {drop:.3f}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if args.report:
        import json
        args.report.write_text(json.dumps({"video": str(args.video), "probe": info,
                                           "problems": problems},
                                          ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")

    if problems:
        print("\n[NG] 阻断项：")
        for p in problems:
            print("  [NG]", p)
        return 2
    print("\n[OK] 视频段自动验收通过（仍需人工看接触表）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
