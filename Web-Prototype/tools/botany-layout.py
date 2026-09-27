#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# botany-layout.py — 一次性排布草稿工具（**不是**构建步骤）
#
# 输出 landing.html 尾部「草与花」那一整块 SVG 的草稿，供人工审阅后
# **粘贴**进 landing.html。落地后的 landing.html 里的 SVG 标记才是唯一事实源；
# 本脚本不参与任何构建，跑与不跑都不影响页面。
#
# 之所以用脚本排布：45 个 <use> 的 x 抖动、缩放、旋转、墨色浓淡手写易错。
# 形状模板是手绘的（质量关键，见 TEMPLATES）。固定种子，输出可复现。
#
#   python tools/botany-layout.py --svg
#   python tools/botany-layout.py --draft qa/botany-draft.html --h 180
#
# ── 两个必须记住的陷阱（都实测过）────────────────────────────────────
# 1. **CSS 选择器穿不进 <use> 的影子树**。`.botany .petal { … }` 对模板里的
#    元素完全无效：路径会退回默认的 fill:black，整条草带糊成一块黑。
#    模板元素的描边/填充/动画只能写在模板自己的 style 里，
#    用 var() 从 <use> 上继承 —— custom property 是唯一能穿过影子树的东西。
# 2. **不能再给 <use> 加 CSS transform 动画**。放置用的
#    transform="translate() rotate() scale()" 是表现属性，优先级低于 CSS 动画，
#    一动画就被整个覆盖，草会全部塌回原点。所以风摆动画挂在模板 <g> 上，
#    它自己的坐标原点就是草根，transform-origin: 50% 100% 正好绕根转。
# ═══════════════════════════════════════════════════════════════════════
import argparse
import random

W = 1440          # viewBox 宽
H = 180           # viewBox 高（地面在 y=H）
SEED = 20260927   # 固定种子：草是「设计过的随机」，不是每次刷新换一张

INK = ('fill:none;stroke:var(--botany-ink);stroke-linecap:round;'
       'stroke-linejoin:round;stroke-width:var(--pen,1);'
       # 自画：每根草叶带 pathLength="1"，所以 dasharray:1 对每一笔都等于全长，
       # 不需要 JS 去量路径长度。和首屏「一条自己生长的墨线」是同一套语言。
       'stroke-dasharray:1;stroke-dashoffset:var(--dash,0);'
       'transition:stroke-dashoffset 760ms var(--ease-standard) calc(var(--i,0)*26ms);'
       # 风摆：绕草根往复，各丛周期与相位都不同，才不会像整齐划一的仪仗队
       'animation:botany-sway var(--dur,6.4s) ease-in-out var(--delay,0s) infinite alternate;'
       'animation-play-state:var(--play,running);'
       'transform-box:fill-box;transform-origin:50% 100%')
PETAL = ('fill:var(--botany-bloom);fill-opacity:.20;'
         'stroke:var(--botany-bloom);stroke-opacity:.72;'
         'stroke-width:calc(var(--pen,1)*.9);stroke-dashoffset:var(--dash,0);'
         'transition:stroke-dashoffset 700ms var(--ease-standard) 420ms')
GRAIN = ('fill:var(--botany-bloom);fill-opacity:.22;'
         'stroke:var(--botany-bloom);stroke-opacity:.58;'
         'stroke-width:calc(var(--pen,1)*.8);stroke-dashoffset:var(--dash,0);'
         'transition:stroke-dashoffset 700ms var(--ease-standard) 460ms')
PISTIL = 'fill:var(--botany-bloom);stroke:none'
SPRAY = ('stroke:var(--botany-ink);stroke-opacity:.7;'
         'stroke-dashoffset:var(--dash,0);'
         'transition:stroke-dashoffset 760ms var(--ease-standard) 80ms')
# 花头整朵最后浮现：茎先画出来，花再开
HEAD = ('opacity:var(--head,1);'
        'transition:opacity 420ms var(--ease-standard) 460ms')

# ── 形状模板 ───────────────────────────────────────────────────────────
# 基点一律在 (0,0)，向上生长（y 为负）。每片草叶是一条三次贝塞尔：
# 起笔在基点、梢头收在一个点 —— 沿用首屏那支笔的笔法。
TEMPLATES = {
    # 长扫叶：一笔到底，负责整体的「风感」与天际线高低差
    'pa': ['M0 0C2-32 12-60 36-84',
           'M-3 0C-4-24-11-44-24-60'],
    'pb': ['M0 0C-3-36-16-64-42-88',
           'M4 0C6-26 14-46 28-62'],
    'pc': ['M0 0C7-38 24-70 8-106',
           'M-4 0C-6-30-14-54-30-72'],
    'pd': ['M0 0C3-34 12-60 6-88',
           'M-2 0C-3-26-8-48-20-64',
           'M4 0C7-22 12-40 6-58'],
    # 中等丛：主力密度
    'qa': ['M0 0C-2-24-6-44-14-62',
           'M2 0C4-20 9-38 18-52',
           'M-2 0C-5-15-10-26-18-36',
           'M4 0C7-13 12-22 20-29',
           'M1 0C1-17 0-31-2-45'],
    'qb': ['M0 0C-2-15-8-28-17-39',
           'M2 0C1-17-2-31-11-45',
           'M-1 0C-6-13-13-22-22-28',
           'M3 0C5-11 9-20 13-27'],
    'qd': ['M0 0C2-18 10-32 22-44',
           'M-3 0C-5-16-11-28-20-38',
           'M2 0C1-14-1-26-6-38',
           'M4 0C6-12 9-22 6-34',
           'M-1 0C-1-14-4-26-10-34'],
    # 短丛：压住地平线，别让底部漏空
    'qc': ['M0 0C-1-9-3-18-7-27',
           'M2 0C4-7 7-14 11-20',
           'M-2 0C-4-7-8-13-13-16',
           'M1 0C2-9 5-16 8-23',
           'M-1 0C-2-7-4-14-4-21',
           'M3 0C5-6 8-11 12-15'],
    # 花茎与花头：茎一笔，叶是一个闭合的梭形轮廓
    'sf': ['M0 0C2-40 6-80 10-126',
           'M3-46C9-50 15-54 19-62C14-60 7-58 3-56'],
    'sk': ['M0 0C-2-38-6-78-9-120',
           'M-2-50C-8-54-14-58-18-66C-13-64-7-62-2-60'],
    'sw': ['M0 0C3-42 7-84 10-124'],
    # 郁金香：茎梢向右弯（低头），花冠是个倒钟，轮廓与所有放射状花都不同
    'st': ['M0 0C2-40 8-72 20-98',
           'M3-44C9-48 15-52 19-60C14-58 7-56 3-54'],
    'sb': ['M0 0C-2-34-5-68-7-104',
           'M-1-42C-7-46-13-50-17-58C-12-56-6-54-1-52'],
    'sx': ['M0 0C3-16 8-32 6-52'],
    'sp': ['M0 0C2-36 5-72 7-102',
           'M7-102C9-112 13-120 18-126',
           'M7-102C7-114 6-122 4-130',
           'M7-102C10-110 16-116 23-120',
           'M7-102C5-112 2-120-4-126',
           'M7-102C8-112 9-120 11-128'],
}


# 麦穗的着粒点：直接对茎的三次贝塞尔 B(t) 求值拿到，所以在穗轴上不会飘
WHEAT_NODES = [(5.5, -69.0), (6.6, -81.4), (7.6, -93.7), (8.6, -105.9), (9.5, -118.0)]


def petals(n, cy, rx, ry):
    """花瓣绕中心 n 等分。宽度必须满足 n*2rx < 2π*cy，否则相邻花瓣叠满，
    整朵花缩成一个紫色实心饼 —— 第一版就是这么糊掉的。"""
    return ''.join(
        f'<ellipse pathLength="1" cx="0" cy="-{cy}" rx="{rx}" ry="{ry}" '
        f'style="{PETAL}" transform="rotate({i * 360 / n:.0f})"/>' for i in range(n))


HEADS = {
    'sf': f'<g style="{HEAD}" transform="translate(10 -126)">{petals(5, 6.4, 2.9, 5.6)}'
          f'<circle r="1.8" style="{PISTIL}"/></g>',
    'sk': f'<g style="{HEAD}" transform="translate(-9 -120)">{petals(8, 8.6, 2.5, 7.6)}'
          f'<circle r="2.2" style="{PISTIL}"/></g>',
    # 备用的麦穗（**当前未使用**）。原来 x=352 那根花茎用的是它，用户指出
    # 「不是花而是一根螺旋条」——两个原因：① 麦粒全串在茎轴上只交替转 ±34°，
    # 相邻两颗互相交叠（已修成分列穗轴两侧）；② 但修好之后在 180 高的草带里，
    # 整支穗只有约 11px 宽，无论怎么排都仍然读成一条带状物。
    # 这个尺寸下麦穗不可辨认，所以换成郁金香；模板留着，以后要非花质地可从这拿。
    #
    # 麦穗的历史版本：第一版把 6 颗麦粒全串在茎轴上、只交替转 ±34°，
    # 相邻两颗互相交叠 —— 渲染出来是一条波浪状的紫色带子（1:1 像素图上看得最清楚），
    # 完全不像麦穗。真麦穗的粒是**分列在穗轴两侧**、向外张开的，所以这里把
    # 每颗粒沿垂直茎轴的方向偏移 ±2.8，再朝外倾 38°：粒的基部正好落在穗轴上。
    'sw': '<g style="' + HEAD + '">' + ''.join(
        f'<ellipse pathLength="1" rx="2.2" ry="4.6" style="{GRAIN}" '
        f'transform="translate({nx + s * 2.8:.1f} {ny}) rotate({s * 38})"/>'
        for nx, ny in WHEAT_NODES for s in (-1, 1)) +
        f'<ellipse pathLength="1" rx="2.0" ry="4.2" style="{GRAIN}" '
        f'transform="translate(10 -128.5)"/></g>',
    'sb': '<g style="' + HEAD + '" transform="translate(-7 -104)">'
          f'<path pathLength="1" d="M0 0C-2-5-2-11 0-14C2-11 2-5 0 0" style="{PETAL}"/>'
          f'<path pathLength="1" d="M0 0C4-4 7-9 6-13C2-12-1-6 0 0" style="{PETAL}"/></g>',
    # 花冠：杯身两笔到底，杯口三个浅弧（左瓣 / 中凹 / 右瓣）。
    # 用和别的花同一套 PETAL 描边+淡紫填充，所以它一眼是「同一支笔画的」。
    'st': '<g style="' + HEAD + '" transform="translate(20 -98) rotate(12)">'
          # 杯身 + 杯口三瓣
          f'<path pathLength="1" style="{PETAL}" d="M-9-22C-9-12-5-3 0 0C5-3 9-12 9-22'
          f'C9-26 5-28.5 3-25.5C1-23-1-23-3-25.5C-5-28.5-9-26-9-22Z"/>'
          # 前瓣：从左侧凹口下到底再回到右侧凹口。
          # 少了这一笔，杯子里是空的，远看像个玻璃罩而不是花。
          f'<path pathLength="1" style="{PETAL}" d="M-3-25.5C-2-16-1-8 0 0'
          f'C1-8 2-16 3-25.5"/></g>',
    'sx': f'<g style="{HEAD}" transform="translate(6 -52)">{petals(5, 5.6, 2.9, 5.0)}'
          f'<circle r="1.5" style="{PISTIL}"/></g>',
    'sp': '<g style="' + HEAD + '" transform="translate(11 -128)">' + ''.join(
        f'<path pathLength="1" d="M0 0C{dx * 0.4:.1f} -4 {dx * 0.8:.1f} -9 {dx:.1f} -15" '
        f'style="{SPRAY}"/>' for dx in (-7, -4, -1.5, 1.5, 4, 7)) + '</g>',
}

# 会被排布的花茎模板（按这个顺序写进 <defs>）。
# 'sw'（麦穗）刻意不在列：见 HEADS 里的说明，这个尺寸读不出来。
STALK_TPL = ['sf', 'sk', 'st', 'sb', 'sx', 'sp']
GRASS = ['qa', 'qb', 'qc', 'qd']
LONG = ['pa', 'pb', 'pc', 'pd']

# 远层 → 近层：同一条地平线，纵深只能靠墨色浓淡和笔尖粗细区分（空气透视）。
LAYERS = [
    # 名称, 丛数, 浓度, 缩放, 笔宽, 模板池
    ('back',  11, 0.22, 0.90, 0.55, ['qa', 'qb', 'qc']),
    ('near',  13, 0.60, 1.00, 0.95, ['qa', 'qb', 'qc', 'qd']),
]
LONG_LAYER = (10, 0.70, 1.00, 0.95)     # 长扫叶：撑起天际线的起伏

# 花的高度要错落：7 支高的撑起天际线，4 支矮的埋在草里 —— 有高低才像草甸。
STALKS = [
    (150,  'sf', 1.08, 1.05),
    (352,  'st', 1.05, 0.98),
    (596,  'sk', 1.14, 1.10),
    (828,  'sp', 0.86, 0.80),
    (1042, 'sb', 0.98, 0.90),
    (1218, 'sf', 1.20, 1.10),
    (1348, 'sk', 0.80, 0.78),
]
LOW_FLOWERS = [
    (86,   'sx', 1.00, 0.78),
    (486,  'sx', 1.18, 0.84),
    (712,  'sx', 0.86, 0.72),
    (1128, 'sx', 1.26, 0.86),
]

def num(v):
    """把 1.0 写成 1、0.50 写成 .5 —— 标记要短，肉眼要好读。"""
    s = f'{v:.2f}'.rstrip('0').rstrip('.')
    return s if not s.startswith('0.') else s[1:]


def build_uses():
    rng = random.Random(SEED)
    out, idx = [], 0
    for name, n, op, sc, pen, pool in LAYERS:
        out.append(f'      <!-- {name} 层：{n} 丛普通草 -->')
        for i in range(n):
            x = (i + 0.5) * (W / n) + rng.uniform(-(W / n) * 0.34, (W / n) * 0.34)
            x = max(8, min(W - 8, x))
            env = 1.0 + 0.18 * abs(x / W - 0.5) * 2   # 中段略矮、两端略高
            s = sc * rng.uniform(0.74, 1.26) * env
            rot = rng.uniform(-7.0, 7.0)
            out.append(f'      <use href="#{rng.choice(pool)}" '
                       f'transform="translate({x:.0f} {H}) rotate({rot:.1f}) scale({s:.2f})" '
                       f'style="opacity:{op * rng.uniform(0.82, 1.0):.2f};--pen:{pen:.2f};'
                       f'--i:{idx};--dur:{rng.uniform(3.8, 7.0):.1f}s;'
                       f'--delay:{-rng.uniform(0, 7):.1f}s;--amp:{rng.uniform(1.8, 3.2):.1f}deg"/>')
            idx += 1
        out.append('')

    n, op, sc, pen = LONG_LAYER
    out.append(f'      <!-- 长扫叶：{n} 笔，撑起天际线的高低差 -->')
    for i in range(n):
        x = (i + 0.5) * (W / n) + rng.uniform(-46, 46)
        x = max(10, min(W - 10, x))
        s = sc * rng.uniform(0.76, 1.08)
        rot = rng.uniform(-9.0, 9.0)
        out.append(f'      <use href="#{rng.choice(LONG)}" '
                   f'transform="translate({x:.0f} {H}) rotate({rot:.1f}) scale({s:.2f})" '
                   f'style="opacity:{op * rng.uniform(0.72, 1.0):.2f};--pen:{pen:.2f};'
                   f'--i:{idx};--dur:{rng.uniform(4.4, 7.6):.1f}s;'
                   f'--delay:{-rng.uniform(0, 7):.1f}s;--amp:{rng.uniform(2.6, 4.2):.1f}deg"/>')
        idx += 1
    out.append('')

    out.append('      <!-- 花茎：7 支高的，紫色只出现在这里 -->')
    for x, t, s, pen in STALKS:
        rot = rng.uniform(-3.0, 3.0)
        out.append(f'      <use href="#{t}" transform="translate({x} {H}) '
                   f'rotate({rot:.1f}) scale({s:.2f})" '
                   f'style="--pen:{pen:.2f};--i:{idx};--dur:{rng.uniform(3.6, 6.0):.1f}s;'
                   f'--delay:{-rng.uniform(0, 6):.1f}s;--amp:{rng.uniform(3.0, 4.6):.1f}deg"/>')
        idx += 1
    out.append('')
    out.append('      <!-- 矮花：埋在草里，让花有高低两层 -->')
    for x, t, sc, pen in LOW_FLOWERS:
        rot = rng.uniform(-8.0, 8.0)
        out.append(f'      <use href="#{t}" transform="translate({x} {H}) '
                   f'rotate({rot:.1f}) scale({sc:.2f})" '
                   f'style="--pen:{pen:.2f};--i:{idx};--dur:{rng.uniform(3.0, 4.8):.1f}s;'
                   f'--delay:{-rng.uniform(0, 5):.1f}s;--amp:{rng.uniform(3.6, 5.2):.1f}deg"/>')
        idx += 1
    return out


# 落花：x, 下落时长, 相位(负延迟), 大小, 透明度, 摆动, 起始/结束旋转角, 翻转周期, 翻转相位
# 相位在载入时就散开（延迟/时长 = 18%–66%），所以一进来天上就有花在落。
# ⚠ --o 必须挂在与 @keyframes petal-fall 同一个元素上（.petal-lane）：
#   custom property 只向下继承，挂在子元素 .petal 上的 --o，父层取不到，
#   会全部退回默认值 —— 那样每片花瓣的透明度就一模一样了。
PETALS = [
    # x%, 下落秒, 相位(负延迟), 边长px, 不透明度, 左右摆幅px, 起/止旋转角, 翻转周期s, 翻转相位s
    # ⚠ x + 摆幅 + 瓣宽 必须 < 100%，否则窄屏上花瓣会擦出右边缘被 overflow 裁掉
    #   （几何上确实越界，只是看不见 —— 那种「到边上突然缺一块」很难查）。
    (8,  21, -7,  13, .54, 11, -20,  40, 3.6, -0.9),
    (18, 26, -15, 10, .46,  9,  10, -34, 4.4, -2.6),
    (29, 17, -3,  15, .58, 12, -40,  22, 3.0, -1.7),
    (40, 23, -11, 11, .48, 10,  26, -18, 3.9, -3.1),
    (51, 29, -19,  9, .40,  8, -12,  46, 4.8, -0.4),
    (62, 20, -5,  13, .52, 12,  34, -26, 3.3, -2.2),
    (72, 25, -13, 10, .46, 10, -30,  16, 4.1, -1.3),
    (82, 19, -9,  12, .50, 11,  20, -38, 3.5, -2.9),
    (91, 27, -17,  9, .42,  8, -26,  30, 4.6, -1.0),
]
def petal_lanes():
    """落花。**HTML 层，不是 SVG 层**：花瓣要从视口顶端一路落到草里，
    跨度远大于 180px 的草带，放进 SVG 就只能在带子里掉。
    .petal-lane 高度撑满整条通道，所以只要一个 translateY 百分比就能落到底，
    不必知道容器有多少像素 —— 这是既不用 JS 量高度、又不用动画 top 的写法。"""
    out = ['  <div class="petal-fall">']
    for x, fall, delay, size, op, wob, r0, r1, flut, fdel in PETALS:
        out.append(f'    <span class="petal-lane" '
                   f'style="--x:{x}%;--fall:{fall}s;--delay:{delay}s;--o:{op}">')
        out.append(f'      <i class="petal" style="--size:{size}px;--wob:{wob}px;'
                   f'--r0:{r0}deg;--r1:{r1}deg;--flutter:{flut}s;--fdelay:{fdel}s"></i>')
        out.append('    </span>')
    out.append('  </div>')
    return out


def svg_block():
    """内联 SVG：defs 里是手绘形状模板，<use> 是排布。
    内联而不是 <img>：要继承页面令牌（--botany-*），也要能被页面 CSS 驱动；
    而且 file:// 下外部 SVG 走 fetch 会被拦，与首屏脚本同一个理由。"""
    defs = []
    for key in GRASS + LONG + STALK_TPL:
        defs.append(f'    <g id="{key}" style="{INK}">')
        defs += [f'      <path pathLength="1" d="{d}"/>' for d in TEMPLATES[key]]
        if key in HEADS:
            defs.append(f'      {HEADS[key]}')
        defs.append('    </g>')
    return '\n'.join([
        '  <!-- 页面最底部的一条草线，草根就长在文档底边上（本块是 body 的最后一块）。',
        '       原来它上方还有一条页脚（智塾 · AI TEACHING PLATFORM / 教师 · 助教 ·',
        '       答疑 · 督学），与顶栏字标重复，已整块删除。',
        '       笔法与首屏同源：墨色线稿 + 品牌紫的花，不引入新色相',
        '       （DESIGN-HANDOFF.md 禁止新增色系）。',
        '       标记的排布由 tools/botany-layout.py 生成后粘贴在此，此文件即唯一事实源。',
        '       草：viewBox 1440×180 + slice，等比缩放永不拉伸；高度取',
        '       max(118px, 12.5vw)，所以 ≥944px 视口下比例正好、零裁切，窄屏有意裁两侧',
        '       取中间一段特写。落花：HTML 层，要从视口顶端一路落到草里，跨度远大于草带。',
        '       两者都在 css/landing.css，动效在 js/landing.js。 -->',
        '  <div class="botany" aria-hidden="true">',
    ] + petal_lanes() + [
        '    <svg class="botany-ink" viewBox="0 0 %d %d" preserveAspectRatio="xMidYMax slice"' % (W, H),
        '         focusable="false" role="presentation">',
        '      <defs>',
    ] + defs + [
        '      </defs>',
    ] + build_uses() + [
        '    </svg>',
        '  </div>',
    ])


DRAFT = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>botany draft</title>
<link rel="stylesheet" href="../css/tokens.css">
<link rel="stylesheet" href="../css/base.css">
<link rel="stylesheet" href="../css/landing.css">
<style>
  html, body { margin: 0; background: #fff; }
  /* 草稿只补两件真实页面里由别的规则提供的东西：
     1) .botany 本来靠 body 的最后一块定位，这里给它一点上方内容，才看得出落花通道；
     2) 草稿没有 JS，直接长好的状态 —— 与真实页面「JS 挂了」时一致。 */
  .draft-above { height: __ABOVE__px; }
  .botany { --botany-h: __H__px; }
</style></head>
<body class="landing">
<div class="draft-above"></div>
__SVG__
</body></html>
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--draft')
    ap.add_argument('--svg', action='store_true')
    ap.add_argument('--h', type=int, default=180)
    ap.add_argument('--above', type=int, default=200, help='草稿里草带上方留多少像素')
    a = ap.parse_args()
    if a.draft:
        html = (DRAFT.replace('__SVG__', svg_block())
                     .replace('__H__', str(a.h)).replace('__ABOVE__', str(a.above)))
        open(a.draft, 'w', encoding='utf-8').write(html)
        print('wrote', a.draft)
    elif a.svg:
        print(svg_block())
    else:
        print('--svg 或 --draft 二选一')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
