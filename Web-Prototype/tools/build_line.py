#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# build_line.py — 由 source/line-spec.js 生成运行时数据
#
#   source/line-spec.js      （人手编辑的**源**）
#        ↓  python tools/build_line.py
#   build/line-runtime-data.js （**派生**，页面只读这个）
#
# 为什么必须分开：
#   SKILL.md 要求「时间值必须由最终编译结果生成，不手工抄写」。本路线没有
#   编译视频，等价的东西就是**几何采样**：等弧长采样点、切线法线、笔宽、
#   以及屏幕速度补偿的积分表。这些都能从路径与曲线唯一地推出来，属于派生量。
#
#   在此之前运行时用 SVG 的 getTotalLength / getPointAtLength 在启动时现算
#   （每个采样点一次调用，520 次），等于把「编译」放在每次打开页面时做一遍。
#   现在挪到构建期，运行时只读数组。
#
#   顺带的好处：运行时不再需要往 DOM 里插一个探针 path，也不再依赖
#   SVGGeometryElement 的测量 API。
#
# 用法：
#   python tools/build_line.py                       # 生成 build/line-runtime-data.js
#   python tools/build_line.py --check               # 只校验源的哈希是否与产物一致
#   python tools/build_line.py --report qa/line-build.json
# ═══════════════════════════════════════════════════════════════════════
import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

SPEC_PATH = Path('source/line-spec.js')
OUT_PATH = Path('build/line-runtime-data.js')

# 曲线离散化容差（纸坐标单位）。纸是 2400×1848，所以 0.5 单位 ≈ 0.02% 幅宽，
# 在最大缩放 1.6× 下约 0.8 设备像素 —— 比笔宽（约 4 单位）小一个量级。
FLATTEN_TOL = 0.5

# 回描段在进度分配里的权重。0 会让回描完全瞬移（画面跳），1 等于不区分。
# 0.12 是折中：回描仍然看得见笔在走，但不占掉可观的时间。
RETRACE_WEIGHT = 0.12


# ── 读源 ───────────────────────────────────────────────────────────────
def load_spec(path: Path) -> tuple[dict, str]:
    """source/line-spec.js 的形状是 `window.OIL_LINE_SPEC = {…};`，花括号里是
    合法 JSON（由 json.dumps 生成）。这里按 JSON 解析：**不接受** JS 专有写法
    （注释、裸键、尾逗号），遇到就报错而不是猜 —— 猜错的代价是产物和源不一致。"""
    text = path.read_text(encoding='utf-8')
    m = re.search(r'window\.OIL_LINE_SPEC\s*=\s*(\{.*\})\s*;', text, re.S)
    if not m:
        raise SystemExit(f'{path} 里找不到 `window.OIL_LINE_SPEC = {{…}};`')
    body = m.group(1)
    try:
        return json.loads(body), hashlib.sha256(body.encode('utf-8')).hexdigest()
    except json.JSONDecodeError as e:
        raise SystemExit(
            f'{path} 的花括号内容不是合法 JSON（{e}）。\n'
            '该文件必须保持纯 JSON —— 不要在里面写注释、裸键或尾逗号。\n'
            '说明文字写在 "_" 字段里，它本来就是数据的一部分。')


# ── 路径解析与离散化 ───────────────────────────────────────────────────
TOKEN = re.compile(r'([MLCZmlcz])|(-?\d*\.?\d+(?:e-?\d+)?)')


def parse_path(d: str):
    """支持 M / L / C（本项目的路径只用到这三种，多了就报错而不是忽略）。"""
    tokens = []
    for m in TOKEN.finditer(d):
        tokens.append(m.group(1) if m.group(1) else float(m.group(2)))
    segs, i, cur, start = [], 0, None, None
    while i < len(tokens):
        cmd = tokens[i]
        if not isinstance(cmd, str):
            raise SystemExit(f'路径解析出错：位置 {i} 处期待命令，得到 {cmd}')
        i += 1
        if cmd == 'M':
            cur = (tokens[i], tokens[i + 1]); i += 2
            start = cur
        elif cmd == 'L':
            nxt = (tokens[i], tokens[i + 1]); i += 2
            segs.append(('L', cur, nxt)); cur = nxt
        elif cmd == 'C':
            c1 = (tokens[i], tokens[i + 1])
            c2 = (tokens[i + 2], tokens[i + 3])
            nxt = (tokens[i + 4], tokens[i + 5]); i += 6
            segs.append(('C', cur, c1, c2, nxt)); cur = nxt
        elif cmd in 'ZC':
            raise SystemExit(f'路径里出现了本工具不支持的命令 {cmd}（只支持 M/L/C）')
        else:
            raise SystemExit(f'路径里出现了未处理的命令 {cmd}')
    return segs


def cubic(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
        u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
    )


def poly_len(pts):
    return sum(math.dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1))


def flatten(segs):
    """把每段离散成折线。直线一段即可；三次曲线按控制多边形长度定步数。"""
    out = []
    for s in segs:
        if s[0] == 'L':
            out.append([s[1], s[2]])
        else:
            _, p0, p1, p2, p3 = s
            rough = math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
            steps = max(8, min(600, int(math.ceil(rough / FLATTEN_TOL))))
            line = [cubic(p0, p1, p2, p3, k / steps) for k in range(steps + 1)]
            out.append(line)
    return out


def resample(lines, n):
    """等弧长重采样成 n+1 个点。返回 (点列表, 总长)。"""
    # 先把所有折线接成一条，并记录每段的累计长度
    flat = []
    for line in lines:
        for k, p in enumerate(line):
            if flat and k == 0 and flat[-1][0] == p:
                continue
            flat.append(p)
    cum = [0.0]
    for k in range(1, len(flat)):
        cum.append(cum[-1] + math.dist(flat[k - 1], flat[k]))
    total = cum[-1]
    if total <= 0:
        raise SystemExit('路径总长为 0')

    pts, j = [], 0
    for i in range(n + 1):
        target = total * i / n
        while j < len(cum) - 2 and cum[j + 1] < target:
            j += 1
        span = cum[j + 1] - cum[j]
        f = 0.0 if span <= 0 else (target - cum[j]) / span
        a, b = flat[j], flat[j + 1]
        pts.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return pts, total


def width_at(t, W):
    w = W['base']
    w *= (1 - W['startTaper']['depth']) + W['startTaper']['depth'] * min(1, t * W['startTaper']['rate'])
    w *= (1 - W['endTaper']['depth']) + W['endTaper']['depth'] * min(1, (1 - t) * W['endTaper']['rate'])
    w *= 1 + W['rhythm']['depth'] * abs(math.sin(t * math.pi * W['rhythm']['count']))
    return w


def normals_of(pts):
    out = []
    n = len(pts) - 1
    for i in range(len(pts)):
        a = pts[max(0, i - 1)]
        b = pts[min(n, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        l = math.hypot(dx, dy) or 1.0
        out.append((-dy / l, dx / l))
    return out


# ── 屏幕速度补偿的积分表 ───────────────────────────────────────────────
def visible_weight(pts, ws, seg_len):
    """每个采样点对"画面变化"的贡献权重。
    沿已有线回描（等宽笔画下看不见）不产生新墨，但它占弧长 —— 若按总弧长分配
    进度，用户会滚了一大段而画面没动静（手感发滞）。
    判据：当前点与任何更早采样点的距离小于笔宽的 0.8 倍 → 认为在回描。"""
    n = len(pts)
    w = [1.0] * n
    for i in range(4, n):
        rad = ws[i] * 0.8
        for j in range(0, i - 4):
            dx = pts[j][0] - pts[i][0]
            dy = pts[j][1] - pts[i][1]
            if dx * dx + dy * dy < rad * rad:
                w[i] = RETRACE_WEIGHT
                break
    return w


def sample_at_lut(pts, ws, C, steps):
    """把「进度 p -> 采样点序号」压成一张表，运行时直接查，不再自己算。
    同时折进两件事：
      1. 屏幕速度补偿（d已画/dp 正比于 1/缩放），否则同一格滚动在最深处更快；
      2. 可见长度加权，否则回描旅行会占掉滚动时间。"""
    n = len(pts) - 1
    seg = [0.0] * n
    for i in range(n):
        seg[i] = math.dist(pts[i], pts[i + 1])
    total_arc = sum(seg) or 1.0
    vw = visible_weight(pts, ws, seg)
    # 可见弧长：每段取两端权重的较小值（保守）
    vis = [seg[i] * min(vw[i], vw[i + 1]) for i in range(n)]
    total_vis = sum(vis) or 1.0

    def scale_at(p):
        if p <= C['turn']:
            return C['s0'] * (C['sMax'] / C['s0']) ** (p / C['turn'])
        return C['sMax'] * (C['sEnd'] / C['sMax']) ** ((p - C['turn']) / (1 - C['turn']))

    # 步骤 1：进度 -> 可见长度的累计（含速度补偿的权重）
    cum = [0.0]
    for k in range(1, steps + 1):
        a, b = scale_at((k - 1) / steps), scale_at(k / steps)
        cum.append(cum[-1] + 2 / (a + b))
    tot = cum[-1]
    cum = [c / tot for c in cum]

    # 步骤 2：可见长度 -> 采样点序号
    vcum = [0.0]
    for i in range(n):
        vcum.append(vcum[-1] + vis[i])
    vcum = [v / total_vis for v in vcum]

    lut, j = [], 0
    for k in range(steps + 1):
        target = cum[k]
        while j < n - 1 and vcum[j + 1] < target:
            j += 1
        span = vcum[j + 1] - vcum[j]
        fr = 0.0 if span <= 0 else (target - vcum[j]) / span
        lut.append(j + fr)
    return lut, {
        'totalArc': total_arc, 'totalVisible': total_vis,
        'retraceFrac': 1.0 - total_vis / total_arc,
    }


def arc_map(C, steps):
    def scale_at(p):
        if p <= C['turn']:
            return C['s0'] * (C['sMax'] / C['s0']) ** (p / C['turn'])
        return C['sMax'] * (C['sEnd'] / C['sMax']) ** ((p - C['turn']) / (1 - C['turn']))

    cum = [0.0]
    for k in range(1, steps + 1):
        a = scale_at((k - 1) / steps)
        b = scale_at(k / steps)
        cum.append(cum[-1] + 2 / (a + b))     # 梯形积分 1/s
    total = cum[-1]
    return [v / total for v in cum]


# ── 主流程 ─────────────────────────────────────────────────────────────
def build(spec):
    P, W, C, CO = spec['path'], spec['width'], spec['camera'], spec['colour']
    N = int(spec['samples'])
    segs = parse_path(P['d'])
    lines = flatten(segs)
    pts, total = resample(lines, N)
    ws = [width_at(i / N, W) for i in range(N + 1)]
    ns = normals_of(pts)
    lut, visinfo = sample_at_lut(pts, ws, C, 512)
    return {
        'sourceSha256': hashlib.sha256(
            json.dumps(spec, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'builtFrom': str(SPEC_PATH),
        'note': (
            '由 tools/build_line.py 生成，不要手改。改参数请改 source/line-spec.js 后重跑。'
            '本文件是**派生**产物：等弧长采样点、切线法线、笔宽、屏幕速度补偿积分表。'
        ),
        'path': {'viewBox': P['viewBox'], 'sheetCentre': P['sheetCentre'], 'd': P['d']},
        'length': round(total, 4),
        'samples': N,
        'segmentsParsed': len(segs),
        'flattenTolerance': FLATTEN_TOL,
        'points': [[round(x, 1), round(y, 1)] for x, y in pts],
        'widths': [round(w, 3) for w in ws],
        'normals': [[round(x, 3), round(y, 3)] for x, y in ns],
        'sampleAt': [round(v, 4) for v in lut],
        'visibility': {k: round(v, 5) for k, v in visinfo.items()},
        'kGlow': None,
        'colour': CO,
        'ribbon': spec['ribbon'],
        'camera': C,
        'anchor': spec['anchor'],
        'underlay': spec['underlay'],
        'paper': spec['paper'],
        'pageReveal': spec['pageReveal'],
        'scroll': spec['scroll'],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='由 line-spec.js 生成运行时数据')
    ap.add_argument('--out', default=str(OUT_PATH))
    ap.add_argument('--report')
    ap.add_argument('--check', action='store_true',
                    help='只校验：产物是否存在、其 sourceSha256 是否与当前源一致')
    args = ap.parse_args()

    if not SPEC_PATH.exists():
        print(f'找不到 {SPEC_PATH}', file=sys.stderr)
        return 2
    spec, body_sha = load_spec(SPEC_PATH)

    if args.check:
        out = Path(args.out)
        if not out.exists():
            print(f'未通过：{out} 不存在 —— 先跑 python tools/build_line.py', file=sys.stderr)
            return 1
        text = out.read_text(encoding='utf-8')
        m = re.search(r'window\.OIL_LINE_RUNTIME\s*=\s*(\{.*\})\s*;', text, re.S)
        built = json.loads(m.group(1)) if m else {}
        if built.get('sourceSha256') == hashlib.sha256(
                json.dumps(spec, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest():
            print(f'通过：{out} 与 {SPEC_PATH} 一致')
            return 0
        print(f'未通过：{out} 是旧产物，源已改动 —— 重跑 python tools/build_line.py',
              file=sys.stderr)
        return 1

    data = build(spec)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '/* 由 tools/build_line.py 生成，不要手改。改参数请改 source/line-spec.js。 */\n'
        'window.OIL_LINE_RUNTIME = ' + json.dumps(data, ensure_ascii=False, separators=(',', ':')) + ';\n',
        encoding='utf-8')

    print(f'== 构建 ==')
    print(f'源        {SPEC_PATH}（路径 {len(spec["path"]["d"])} 字符，'
          f'{data["segmentsParsed"]} 段，离散容差 {FLATTEN_TOL}）')
    print(f'产物      {out}（{out.stat().st_size / 1024:.1f} KB）')
    print(f'路径总长  {data["length"]:.2f} 纸坐标单位')
    print(f'采样      {data["samples"] + 1} 点（等弧长，间距 {data["length"] / data["samples"]:.2f} 单位）')
    print(f'笔宽      {min(data["widths"]):.2f} – {max(data["widths"]):.2f} 单位')
    print(f'进度映射  {len(data["sampleAt"])} 步（速度补偿 + 可见长度加权）')
    print(f'回描占比  {data["visibility"]["retraceFrac"] * 100:.1f}%'
          f'（总弧长 {data["visibility"]["totalArc"]:.0f}，可见 '
          f'{data["visibility"]["totalVisible"]:.0f}）')
    print(f'源哈希    {data["sourceSha256"][:16]}…')

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps({
            'source': str(SPEC_PATH), 'output': str(out),
            'sourceSha256': data['sourceSha256'],
            'length': data['length'], 'samples': data['samples'],
            'visibility': data['visibility'],
            'segmentsParsed': data['segmentsParsed'],
            'flattenTolerance': FLATTEN_TOL,
            'widthRange': [min(data['widths']), max(data['widths'])],
            'bytes': out.stat().st_size,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'→ {args.report}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
