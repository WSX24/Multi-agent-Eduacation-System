#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# verify_scene.py — 「白纸世界」关键帧 / 成片帧的自动硬门
#
# 事实源：source/scene-bible.md §4 的检查表。门限写在那份文件里，
# 本脚本只负责测量与判定，不自己发明标准。
#
# 用法：
#   python tools/verify_scene.py <图片或视频> [--fps 1] [--report qa/x.json]
#   python tools/verify_scene.py source/K0-scene.png
#   python tools/verify_scene.py source/seg-03-stations.mp4 --fps 2 --allow-red 0.02
#
# 全部检查都是**逐帧**的，不是抽样。视频按 --fps 抽帧后逐帧判定，
# 输出最差帧与它出现的时间，便于直接定位。
#
# ── 2026-09-26 校准记录（为什么门限不是「最暗必须 ≥12」）─────────────────
# 第一版门限把「不得纯黑 / 不得纯白」写成绝对极值门（darkest >= 12,
# brightest <= 252），实测 K0 直接判死：最暗 0、最亮 255。
# 复核后确认那是**门限错了，不是画面错了**：
#   · identity-bible §3 明确记载机器人轮廓线实测 (0,0,8)，近黑勾边是已验收
#     的画风；用绝对极值门等于禁止机器人的轮廓线。
#   · identity-bible §3 也记载旧流程会把背景洪泛刷成 255 纯白。
# 所以改成**面积占比门**：极暗/极亮像素各自占比不得超过阈值，
# 这样「一条细勾边」通过，「压成纯黑的一整块」被拦下。
# 同理，彩色门从「饱和度」改成「色相家族」：机器人的 #6080A0 是全片唯一
# 允许的第二个彩色（sat 0.40），用饱和度门会把主体自己判死。
# ═══════════════════════════════════════════════════════════════════════
import argparse
import json
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    print("需要 Pillow：python -m pip install -r <oil-motion>/scripts/requirements.txt", file=sys.stderr)
    raise SystemExit(1)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("需要 numpy：技能的 compile / production_gate 也依赖它", file=sys.stderr)
    raise SystemExit(1)


# ── 门限（与 scene-bible.md §4 一一对应，改这里必须同时改那份文件）──────
PURPLE_HUE = (250.0, 305.0)   # 紫色家族：#a855f7 的色相约 274
PURPLE_MAX_COVER = 0.08       # 紫色面积占比上限（scene-bible §1「面积纪律」）
SLATE_HUE = (185.0, 250.0)    # 机器人壳体的 slate 蓝灰（#6080A0 色相约 210）
SLATE_MAX_COVER = 0.10        # 主体允许的占比上限
FORBIDDEN = {                 # 除紫与 slate 外，一律不许出现
    'red':    [(345.0, 360.0), (0.0, 15.0)],
    'orange': [(15.0, 45.0)],
    'yellow': [(45.0, 70.0)],
    'green':  [(70.0, 170.0)],
    'cyan':   [(170.0, 185.0)],
}
FORBIDDEN_MAX_COVER = 0.01    # 单一禁用色相家族的占比上限
SAT_THRESHOLD = 0.18          # 判定「有彩色倾向」的饱和度下限
NEAR_BLACK_FRAC_MAX = 0.015   # 极暗像素（min 通道 <12）占比上限
NEAR_WHITE_FRAC_MAX = 0.02    # 过曝像素（min 通道 >250）占比上限

# ── 世界档位 ─────────────────────────────────────────────────────────
# 「禁用橙黄」这条门限来自纸世界（白纸 + 灰墨 + 紫），那里橙黄确实不该出现。
# 但「一本书的光」这个世界的主体就是一本米色的书、台面也带一点暖调 ——
# 实测 81.8% 的画面落在暖色相里。用同一把尺子会把合法画面判死。
#
# 正确的工具不是「禁止暖色相」，而是区分「淡米色」与「褐色」：
#   淡米色 = 暖 + 低饱和 + 高亮度（合法材质）
#   褐色   = 暖 + 高饱和 + 低亮度（要么是深色木头，要么是画面被压暗）
# 实测该世界：暖色相 81.8%，其中饱和<0.10 的占 71.3%；
# 「暖 + sat>0.35 + 亮度<160」只有 1.20%。
PROFILES = {
    'paper': {
        'purpleMax': 0.08, 'slateMax': 0.10,
        'forbiddenMax': 0.01, 'checkBrown': False,
        'paperFracMin': 0.35, 'paperP95Min': 232, 'meanLumaMin': 170,
    },
    'book-light': {
        # 一本书的光：近白略暖的台面 + 米色书 + 深蓝灰核心 + 淡紫光
        'purpleMax': 0.08, 'slateMax': 0.10,
        'forbiddenMax': 0.01, 'checkBrown': True,
        'brownMax': 0.03,          # 暖 + sat>0.35 + 亮度<160
        'paperFracMin': 0.60, 'paperP95Min': 235, 'meanLumaMin': 210,
    },
}
BROWN_SAT = 0.35
BROWN_LUMA = 160.0
# 「白纸世界」的核心门限。2026-09-26 加到工具里，因为第一版 K0 的实测是
# 主色 #A09080 / 平均亮度 150 —— 一间昏暗的褐色房间，不是白纸。
# 只查「有没有橙色」抓不到这个问题，必须直接量「画面里有多少是亮纸」。
PAPER_LUMA_MIN = 200          # 算作「纸」的最低平均亮度
PAPER_SAT_MAX = 0.15          # 算作「纸」的最高饱和度（避开木桌/暖光过饱和）
PAPER_FRAC_MIN = 0.35         # 亮纸像素占比下限
PAPER_P95_MIN = 232           # 亮度 95 分位下限：画面里必须真的存在「纸白」
# 为什么要同时要这两条：只查占比会迫使一半画面都是白（把有环境光遮蔽的
# 房间误杀），只查最亮值会被一个小白点骗过去。占比 + 分位一起才能证明
# 「大片纸 + 真的白」两件事同时成立。
MEAN_LUMA_MIN = 170           # 全图平均亮度下限（高调画面）
# 材质板（纯纸面贴图）判据：不是「有多白」，而是「有多均匀、多空白」。
PLATE_LUMA_STD_MAX = 14.0     # 亮度标准差上限
PLATE_DARK_FRAC_MAX = 0.005   # 极暗像素占比上限（板上不许有内容）
PLATE_CHROMA_MAX = 0.005      # 彩色像素占比上限
AXIS_MIN, AXIS_MAX = 0.47, 0.53
AXIS_WARN = 0.02              # 离边界多近算「擦线」，需要人眼复核
CORRIDOR_MIN_GAP = 0.15       # 中轴空走道相对两侧的对比度下限


def load_frames(path: Path, fps: float):
    """返回 [(时间秒, RGB ndarray)]。图片只有一帧，时间记 0。"""
    if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}:
        with Image.open(path) as im:
            return [(0.0, np.asarray(im.convert('RGB'), dtype=np.float32))]

    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, check=True)
    w, h = (int(x) for x in probe.stdout.strip().split(','))
    raw = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'fps={fps}',
         '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
        capture_output=True, check=True).stdout
    stride = w * h * 3
    out = []
    for i in range(len(raw) // stride):
        buf = np.frombuffer(raw[i * stride:(i + 1) * stride], dtype=np.uint8)
        out.append((i / fps, buf.reshape(h, w, 3).astype(np.float32)))
    if not out:
        raise SystemExit(f'抽帧失败：{path}')
    return out


def hue(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    d = np.maximum(mx - mn, 1e-6)
    h = np.zeros_like(mx)
    m = (mx == r)
    h[m] = (60 * ((g - b) / d) % 360)[m]
    m = (mx == g) & (mx != r)
    h[m] = (60 * ((b - r) / d) + 120)[m]
    m = (mx == b) & (mx != r) & (mx != g)
    h[m] = (60 * ((r - g) / d) + 240)[m]
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return h, sat


def in_ranges(h, sat, ranges, sat_min=SAT_THRESHOLD):
    mask = np.zeros(h.shape, dtype=bool)
    for lo, hi in ranges:
        mask |= (h >= lo) & (h < hi)
    return mask & (sat > sat_min)


def measure(rgb: np.ndarray) -> dict:
    h, w, _ = rgb.shape
    hh, sat = hue(rgb)
    lum = rgb.mean(axis=2)
    mn = rgb.min(axis=2)

    colored = sat > SAT_THRESHOLD
    purple = in_ranges(hh, sat, [PURPLE_HUE])
    slate = in_ranges(hh, sat, [SLATE_HUE])
    forbid = {name: float(in_ranges(hh, sat, rng).mean())
              for name, rng in FORBIDDEN.items()}
    other = float((colored & ~purple & ~slate).mean())

    # 中轴：两个独立代理指标
    #  (1) 空走道 —— 下半幅逐列边缘能量的局部最小值所在的 x
    band = lum[int(h * 0.62):, :]
    edge = np.abs(np.diff(band, axis=1)).mean(axis=0)
    edge = np.concatenate([[edge[0]], edge])
    lo, hi = int(w * 0.20), int(w * 0.80)
    zone = edge[lo:hi]
    corridor_x = (lo + int(np.argmin(zone))) / w
    corridor_gap = 1 - (zone.min() / max(zone.mean(), 1e-6))
    #  (2) 中心物件 —— 上半幅「墨迹最密」处的水平重心（黑板/算式所在）
    upper = lum[:int(h * 0.55), :]
    col = upper.mean(axis=0)
    ink = np.clip(np.percentile(col, 90) - col, 0, None)
    ink_x = float((np.arange(w) * ink).sum() / max(ink.sum(), 1e-6)) / w
    #  (3) 左右对称度 —— 一点透视的房间应当在水平方向近似对称
    mirror = float(np.abs(lum - lum[:, ::-1]).mean())

    return {
        'size': [w, h],
        'aspect': round(w / h, 4),
        'purpleCover': round(float(purple.mean()), 5),
        'slateCover': round(float(slate.mean()), 5),
        'otherChromaCover': round(other, 5),
        'forbidden': {k: round(v, 5) for k, v in forbid.items()},
        'brownCover': round(float((((hh >= 15) & (hh < 70)) & (sat > BROWN_SAT)
                                   & (lum < BROWN_LUMA)).mean()), 5),
        'nearBlackFrac': round(float((mn < 12).mean()), 5),
        'nearWhiteFrac': round(float((mn > 250).mean()), 5),
        'darkest': int(rgb.min()),
        'brightest': int(rgb.max()),
        'meanLuma': round(float(lum.mean()), 1),
        'paperFrac': round(float(((lum > PAPER_LUMA_MIN) & (sat < PAPER_SAT_MAX)).mean()), 5),
        'paperP95': round(float(np.percentile(lum, 95)), 1),
        'lumaStd': round(float(lum.std()), 2),
        'corridorX': round(float(corridor_x), 4),
        'corridorGap': round(float(corridor_gap), 4),
        'inkCentroidX': round(ink_x, 4),
        'mirrorDelta': round(mirror, 2),
    }


def judge(m: dict, args):
    P = PROFILES[args.profile]
    bad, warn = [], []
    if m['purpleCover'] > P['purpleMax']:
        bad.append(f"紫色面积 {m['purpleCover']:.2%} > {P['purpleMax']:.0%}")
    if m['slateCover'] > P['slateMax']:
        bad.append(f"slate 蓝灰面积 {m['slateCover']:.2%} > {P['slateMax']:.0%}")
    if P['checkBrown']:
        # 「一本书的光」世界：不禁止暖色相（米色的书、略暖的台面都是合法材质），
        # 只禁止褐色 —— 暖 + 高饱和 + 暗。见上方 PROFILES 的说明。
        if m['brownCover'] > P['brownMax']:
            bad.append(f"褐色面积 {m['brownCover']:.2%} > {P['brownMax']:.0%}"
                       "（暖色相里又暗又饱和的部分，要么是深色木头要么是画面被压暗）")
        for name in ('red', 'green', 'cyan'):
            cov = m['forbidden'][name]
            if cov > P['forbiddenMax']:
                bad.append(f"禁用色相 {name} 面积 {cov:.2%} > {P['forbiddenMax']:.2%}")
    else:
        if m['otherChromaCover'] > FORBIDDEN_MAX_COVER:
            bad.append(f"未分类彩色面积 {m['otherChromaCover']:.2%} > {FORBIDDEN_MAX_COVER:.0%}")
        for name, cov in m['forbidden'].items():
            cap = args.allow_red if (name == 'red' and args.allow_red) else FORBIDDEN_MAX_COVER
            if cov > cap:
                hint = '' if name != 'red' else '（红笔只在 seg-03 允许，且需传 --allow-red）'
                bad.append(f"禁用色相 {name} 面积 {cov:.2%} > {cap:.2%}{hint}")
    if m['nearBlackFrac'] > NEAR_BLACK_FRAC_MAX:
        bad.append(f"极暗像素占比 {m['nearBlackFrac']:.2%} > {NEAR_BLACK_FRAC_MAX:.2%}（画面被压成纯黑）")
    if m['nearWhiteFrac'] > NEAR_WHITE_FRAC_MAX:
        bad.append(f"过曝像素占比 {m['nearWhiteFrac']:.2%} > {NEAR_WHITE_FRAC_MAX:.2%}（出现纯白死区）")
    if m['paperFrac'] < P['paperFracMin']:
        bad.append(f"亮纸占比 {m['paperFrac']:.1%} < {P['paperFracMin']:.0%}"
                   "（画面不是「白纸」，而是偏暗或偏褐；这是本片最核心的一条）")
    if not args.plate and m['paperP95'] < P['paperP95Min']:
        bad.append(f"亮度 95 分位 {m['paperP95']:.0f} < {P['paperP95Min']}"
                   "（全画面没有一处真正的纸白，纸被整体压成中灰）")
    if args.plate:
        # 材质板：不要求「有纸白」（绝对曝光可在合成时归一化），
        # 只要求「均匀、空白、无内容」。判据换成离散度与暗部占比。
        if m['lumaStd'] > PLATE_LUMA_STD_MAX:
            bad.append(f"材质板不够均匀：亮度标准差 {m['lumaStd']:.1f} > {PLATE_LUMA_STD_MAX}"
                       "（有可见纹理块、污渍或照明不均）")
        if m['nearBlackFrac'] > PLATE_DARK_FRAC_MAX:
            bad.append(f"材质板出现内容：极暗像素 {m['nearBlackFrac']:.2%} > {PLATE_DARK_FRAC_MAX:.2%}"
                       "（板上被画了东西）")
        if m['otherChromaCover'] > PLATE_CHROMA_MAX:
            bad.append(f"材质板出现彩色：{m['otherChromaCover']:.2%} > {PLATE_CHROMA_MAX:.2%}")
    if m['meanLuma'] < P['meanLumaMin']:
        bad.append(f"平均亮度 {m['meanLuma']:.0f} < {P['meanLumaMin']}（低调整体偏暗，应为高调）")

    if args.axis and not args.plate:
        for label, v in (('中心物件重心', m['inkCentroidX']), ('空走道', m['corridorX'])):
            if not (AXIS_MIN <= v <= AXIS_MAX):
                bad.append(f"中轴 {label} x={v:.3f} 不在 {AXIS_MIN}–{AXIS_MAX}")
        if m['corridorGap'] < CORRIDOR_MIN_GAP:
            bad.append(f"中轴走道不明显：对比度 {m['corridorGap']:.3f} < {CORRIDOR_MIN_GAP}")
        # 擦线提醒：进了容差但离 0.5 超过 2% 的，交给人眼复核
        for label, v in (('中心物件重心', m['inkCentroidX']), ('空走道', m['corridorX'])):
            if AXIS_MIN <= v <= AXIS_MAX and abs(v - 0.5) > AXIS_WARN:
                warn.append(f"中轴 {label} x={v:.3f} 在容差内但偏离中心 "
                            f"{abs(v-0.5)*100:.1f}%，请人眼确认构图是否真的居中")
    return bad, warn


MANUAL = [
    '中轴消失点是否真的在画面中心（脚本的两个 x 只是代理指标，人眼定案）',
    '材料：一眼能否指认出「纸 / 墨 / 木」中的至少两种',
    '是否误入霓虹数据走廊 / 赛博朋克 / 写实 CGI（scene-bible §5 第 1–3 条）',
    '画面里是否有任何可读的文字、数字、UI、水印（第 5 条）',
    '机器人的身份锚点是否与 identity-bible §2/§3 一致（仅 K0 / K4 需要）',
    '16:9 裁切上下边界有没有切到构图锚点（合同「安全裁切策略」第 4 条）',
]


def main() -> int:
    ap = argparse.ArgumentParser(description='白纸世界关键帧/成片帧自动硬门')
    ap.add_argument('source')
    ap.add_argument('--fps', type=float, default=1.0, help='视频抽帧率（默认每秒 1 帧）')
    ap.add_argument('--allow-red', type=float, default=0.0,
                    help='允许红笔笔迹的面积上限，只有 seg-03 传（如 0.02）')
    ap.add_argument('--profile', choices=sorted(PROFILES), default='paper',
                    help="世界档位：paper（白纸+灰墨+紫，默认）或 "
                         "book-light（一本书的光：近白略暖台面 + 米色书 + 淡紫光）。"
                         "两者对暖色的判据不同：paper 直接禁橙黄，"
                         "book-light 只禁「暖+高饱和+暗」的褐色。")
    ap.add_argument('--report')
    ap.add_argument('--no-axis', dest='axis', action='store_false', default=True,
                    help='跳过中轴门。只对「纯材质帧」（纸面贴图等）使用，'
                         '对构图帧一律必须保留中轴门')
    ap.add_argument('--plate', action='store_true',
                    help='材质板模式：跳过中轴与 p95 门，改用均匀度/空白度判据')
    args = ap.parse_args()

    path = Path(args.source)
    if not path.exists():
        print(f'找不到 {path}', file=sys.stderr)
        return 2

    results = []
    for t, rgb in load_frames(path, args.fps):
        m = measure(rgb)
        m['t'] = round(t, 3)
        m['problems'], m['warnings'] = judge(m, args)
        results.append(m)
    worst = max(results, key=lambda m: len(m['problems']))

    print(f'== {path} ==')
    print(f'帧数 {len(results)}  尺寸 {worst["size"][0]}x{worst["size"][1]}  宽高比 {worst["aspect"]}')
    hdr = (f'{"t":>7} {"亮纸%":>7} {"p95":>5} {"均亮":>5} {"紫%":>6} {"slate%":>7} '
           f'{"其他彩%":>8}')
    if PROFILES[args.profile]['checkBrown']:
        hdr += f' {"褐色%":>7}'
    hdr += f' {"极暗%":>7} {"过曝%":>7}'
    print(hdr)
    for m in results:
        flag = '  <<< 不通过' if m['problems'] else ('  ~ 需人眼' if m['warnings'] else '')
        line = (f'{m["t"]:>7.2f} {m["paperFrac"]*100:>6.1f} {m["paperP95"]:>5.0f} '
                f'{m["meanLuma"]:>5.0f} '
                f'{m["purpleCover"]*100:>5.2f} {m["slateCover"]*100:>6.2f} '
                f'{m["otherChromaCover"]*100:>7.2f}')
        if PROFILES[args.profile]['checkBrown']:
            line += f' {m["brownCover"]*100:>6.2f}'
        line += (f' {m["nearBlackFrac"]*100:>6.2f} {m["nearWhiteFrac"]*100:>6.2f}{flag}')
        print(line)

    print()
    if worst['problems']:
        print(f'自动硬门：不通过（最差帧 t={worst["t"]}s）')
        for p in worst['problems']:
            print(f'  · {p}')
    else:
        print('自动硬门：全部通过（逐帧）')
    for w in worst['warnings']:
        print(f'  ~ {w}')

    print('\n仍须人眼定案（脚本不代替验收）：')
    for item in MANUAL:
        print(f'  · {item}')

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            'source': str(path), 'fps': args.fps, 'frames': results,
            'worst': worst, 'passed': not worst['problems'], 'manualReview': MANUAL,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n→ {args.report}')

    return 0 if not worst['problems'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
