#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器框自动修复 —— 把「元素超出容器」这类纯几何错交给代码改。

## 为什么需要它

`svg_quality_checker` 报的 blocking 分两类，性质完全不同：

| 类别 | 性质 | 有唯一解吗 |
|---|---|---|
| `<text> exceeds <g> data-pptx-bounds` | **纯几何**：声明的容器没包住自己的内容 | ✅ 有——把容器扩到包住内容 |
| `<g> overlaps <g>` | **布局**：两个同级模块抢同一块区域 | ❌ 没有——要重划版面 |

第一类的解，仓库里其实早就算出来了。`staged._solve_bounds_errors` 的 docstring：

    「这两类都是**纯几何算术**，而且报错文本自带全部输入。模型自己算不准——
      实测同一个错误连修 3 轮不过。所以由代码解出答案，把『把容器扩到 ≥X px』
      ……直接告诉模型。」

**但它只是"告诉模型"，让模型花一次完整调用（~80s）去改一个数字。**

实测代价（2026-10-08 12 页 deck）：18 次模型调用里 **8 次**是 P4 逐页返工这类错。
而每次调用的往返 ≈ 80s——**约 640s 花在"改一个数字"上**。

本模块把那一步也接管：直接用代码改，改完让 checker 复验，没变好就回滚。

## 安全设计

**绝不盲改**。流程是「快照 → 改 → 复验 → 保留或回滚」：

    1. 记下要改的文件原文
    2. 只改「超出容器」类（重叠类一概不碰）
    3. 重跑 checker
    4. blocking 数减少 → 保留；没减少 → **回滚原文**，交模型

所以最坏情况是"白改一次"（代价 ~1s 的 checker），绝不会把 deck 改坏。
回滚之后报告也会跟着重跑，保证导出门读到的是与当前文件一致的结论。

## 只扩不缩

容器扩大会挤压同级模块——那正是**重叠类**错的来源。所以：
- 只对报出超界的那个 `<g>` 做**外扩**（并集 + 4px 余量）
- 一旦因此产生新重叠，第 3 步的复验会发现（blocking 没减少）→ 回滚
- 内容超出的是 **root viewBox**（`container_kind == "canvas"`）时**跳过**：
  那不是"某个容器框画小了"，而是内容整体溢出了画布，必须重排，代码无从下手

## 拆行（第四条策略）

扩框 / 挪元素都解不了的「超长单行文字」——容器两边被邻居挡死、文字本身比容器宽——
代码会把单行 ``<text>`` 拆成多行 ``<tspan>``（仅 CJK 主导、水平溢出、单行、拆完放得下
才动手）。这是**内容改动**，比改数字风险高，所以条件最严、顺序放最后，仍由复验回滚兜底。

用法::

    from bounds_fix import expand_bounds_for_overflow
    new_svg, notes = expand_bounds_for_overflow(svg_text, checker_messages)

    python bounds_fix.py --selftest
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Dict, List, Sequence, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

# 与 staged._BOUNDS_RE 同源：报错文本自带全部输入，直接复用它的字段名。
# 放在这里而不是 import staged，是为了让本模块能独立自检（不拖进 agent.py）。
_BOUNDS_RE = re.compile(
    r"<(?P<tag>text|g|image)\b[^>]*>.*?exceeds\s+(?:<g\s+id=\"(?P<owner_id>[^\"]+)\"[^>]*>\s*data-pptx-bounds"
    r"|the\s+root\s+viewBox)\s+on\s+the\s+(?P<axis>horizontal|vertical)\s+axis:\s*"
    r"content\s*\((?P<cx1>[-\d.]+),\s*(?P<cy1>[-\d.]+)\)-\((?P<cx2>[-\d.]+),\s*(?P<cy2>[-\d.]+)\),\s*"
    r"(?P<container_kind>container|canvas)\s*\((?P<kx1>[-\d.]+),\s*(?P<ky1>[-\d.]+)\)-\((?P<kx2>[-\d.]+),\s*(?P<ky2>[-\d.]+)\)",
    re.DOTALL,
)
_GTAG_RE = re.compile(r"<g\b[^>]*>")
_ID_ATTR_RE = re.compile(r'\bid="([^"]*)"')
_BOUNDS_ATTR_RE = re.compile(r'(\bdata-pptx-bounds=")([^"]*)(")')
_VIEWBOX_RE = re.compile(
    r'viewBox="\s*([-\d.]+)[\s,]+([-\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"')

#: 外扩余量。checker 自己建议“两边各留 ≥4 px”，与它一致。
MARGIN = 4.0
#: 「挪元素」策略允许的最大位移。超过就不挪，交模型。
#:
#: 为什么要这个阀（实测，2026-10-08 项目 ..._174710）：
#:   它把 04 页底部的公式 `y − f(x₀) = f'(x₀)(x − x₀)` **横移了 112.8px**，
#:   正好挪到红色切线经过的地方 —— bounds 合规了，但文字被线穿过。
#:   而 svg_quality_checker **不检查同一模块内元素之间的重叠**，所以这个
#:   副作用它一声不响。用一个它看得见的错换一个它看不见的错，是净损失。
MAX_SHIFT = 40.0
#: 碰撞判定用的最小重叠面积（px²）。太小的是擦拭式接触，不算冲突。
MIN_COLLISION_AREA = 60.0


def _fmt(v: float) -> str:
    r = round(v, 1)
    return str(int(r)) if abs(r - round(r)) < 1e-9 else f"{r:.1f}"


def read_group_bounds(svg: str, gid: str) -> Tuple[float, float, float, float] | None:
    """读某个 ``<g id=gid>`` 的 ``data-pptx-bounds``，返回 ``(x1, y1, x2, y2)``。

    属性格式是 ``x y w h``（左上角 + 宽高），这里一律换成交集更好算的 ``x1y1x2y2``。
    """
    for tag in _GTAG_RE.finditer(svg):
        m_id = _ID_ATTR_RE.search(tag.group(0))
        if not m_id or m_id.group(1) != gid:
            continue
        m_b = _BOUNDS_ATTR_RE.search(tag.group(0))
        if not m_b:
            return None
        parts = m_b.group(2).split()
        if len(parts) != 4:
            return None
        x, y, w, h = (float(v) for v in parts)
        return (x, y, x + w, y + h)
    return None


def set_group_bounds(svg: str, gid: str, rect: Tuple[float, float, float, float]) -> str:
    """把某个 ``<g id=gid>`` 的 bounds 改写成 ``rect``（``x1y1x2y2``）。"""
    x1, y1, x2, y2 = rect
    payload = f"{_fmt(x1)} {_fmt(y1)} {_fmt(x2 - x1)} {_fmt(y2 - y1)}"

    def repl_tag(match: re.Match) -> str:
        tag = match.group(0)
        m_id = _ID_ATTR_RE.search(tag)
        if not m_id or m_id.group(1) != gid:
            return tag
        return _BOUNDS_ATTR_RE.sub(lambda mm: mm.group(1) + payload + mm.group(3), tag, count=1)

    return _GTAG_RE.sub(repl_tag, svg)


# 报错文本里元素坐标的写法是 `(x=56, y=126; text='…')` —— **没有引号**。
# （早期版本按 `x="56" y="126"` 写，于是一个也匹配不到，静默跳过全目标。）
_OVERFLOW_ELEM_RE = re.compile(
    r"<(?P<tag>text|image|g)>\s*\(x=(?P<x>[-\d.]+),\s*y=(?P<y>[-\d.]+)")
_TEXT_TAG_RE = re.compile(r"<text\b[^>]*>")
_XY_RE = re.compile(r'\bx="([-\d.]+)"\s+y="([-\d.]+)"')


_OVERLAP_RE = re.compile(
    r"<g\s+id=\"(?P<a>[^\"]+)\"\s*>\s*data-pptx-bounds\s+overlaps\s+<g\s+id=\"(?P<b>[^\"]+)\"\s*>\s*"
    r"data-pptx-bounds\s+by\s+(?P<w>[\d.]+)px\s*x\s*(?P<h>[\d.]+)px"
)


def _contains(outer, inner, tol: float = 1.0) -> bool:
    return (outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1]
            and outer[2] + tol >= inner[2] and outer[3] + tol >= inner[3])


def _extract_group(svg: str, gid: str) -> Tuple[int, int] | None:
    """返回 ``<g id=gid>…</g>`` 的首尾下标（含），用配平扫描，能处理嵌套 <g>。"""
    start = None
    for mt in _GTAG_RE.finditer(svg):
        mid = _ID_ATTR_RE.search(mt.group(0))
        if mid and mid.group(1) == gid:
            start = mt.start()
            break
    if start is None:
        return None
    depth = 0
    for tok in re.finditer(r"<g\b[^>]*>|</g>", svg[start:]):
        text = tok.group(0)
        if text.startswith("</"):
            depth -= 1
            if depth == 0:
                return (start, start + tok.end())
        elif text.rstrip().endswith("/>") and depth == 0:
            return (start, start + tok.end())
        else:
            depth += 1
    return None


def nest_contained_root_groups(
    svg: str, errors: Sequence[str]
) -> Tuple[str, List[str]]:
    """第三条策略：把「完全被另一个 root <g> 包住」的模块嵌进去，消除 root 级重叠。

    为什么需要它（实测 2026-10-08）：模型把一个子标注（`annotation-tangent`）
    声明成了 panel 的同级模块，而它整体落在 panel 的 bounds 内。checker 的规则是
    “ordinary direct-root module zones 必须不相交”，于是报重叠。
    这不是“两个模块真的抢地盘”，而是**层级错**——把它嵌进外层就没了。

    只处理**完全包含**的情形（部分重叠无法靠嵌套消除，需重划版面，交模型）。
    嵌套会改变 z-order，但插到外层末尾与它原本“紧跟在外层之后”几乎等价；
    且外层 bounds 本来就已包含它，不会产生新的超界。
    仍然由“改完复验、不好就回滚”兜底。
    """
    out = svg
    notes: List[str] = []
    for msg in errors:
        m = _OVERLAP_RE.search(msg)
        if not m:
            continue
        a, b = m.group("a"), m.group("b")
        ra, rb = read_group_bounds(out, a), read_group_bounds(out, b)
        if ra is None or rb is None:
            continue
        if _contains(rb, ra):
            inner, outer = a, b
        elif _contains(ra, rb):
            inner, outer = b, a
        else:
            continue
        span_in = _extract_group(out, inner)
        span_out = _extract_group(out, outer)
        if span_in is None or span_out is None:
            notes.append(f"跳过 {inner}↔{outer}：拿不到完整的 <g> 片段")
            continue
        if span_in[0] < span_out[0]:
            notes.append(f"跳过 {inner}↔{outer}：嵌套顺序相反，不猜")
            continue
        block = out[span_in[0]:span_in[1]]
        rest = out[:span_in[0]] + out[span_in[1]:]
        # 重新定位外层的 </g>（上面的删除使下标前移）
        shift = span_in[1] - span_in[0]
        span_out2 = (span_out[0], span_out[1] - shift) if span_out[0] > span_in[0] else span_out
        close = rest.rfind("</g>", span_out2[0], span_out2[1])
        if close < 0:
            notes.append(f"跳过 {inner}↔{outer}：找不到外层的 </g>")
            continue
        out = rest[:close] + "\n  " + block + "\n" + rest[close:]
        notes.append(f"嵌套：<g id=\"{inner}\"> 移进 <g id=\"{outer}\">（消除 root 级重叠）")
    return out, notes


_TEXT_TAG_FULL_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.DOTALL)
_RECT_RE = re.compile(r'<rect\b[^>]*\bx="([-\d.]+)"[^>]*\by="([-\d.]+)"[^>]*\bwidth="([-\d.]+)"[^>]*\bheight="([-\d.]+)"')
_CIRCLE_RE = re.compile(r'<circle\b[^>]*\bcx="([-\d.]+)"[^>]*\bcy="([-\d.]+)"[^>]*\br="([-\d.]+)"')
_LINE_RE = re.compile(r'<line\b[^>]*\bx1="([-\d.]+)"[^>]*\by1="([-\d.]+)"[^>]*\bx2="([-\d.]+)"[^>]*\by2="([-\d.]+)"')
_FS_RE = re.compile(r'\bfont-size="([-\d.]+)"')
_COORD_RE = re.compile(r"[-\d.]+")


def _text_width(text: str, size: float) -> float:
    """估算文字宽度。CJK ≈ 1em，拉丁/数字 ≈ 0.55em。

    不读 text_calibration.json：本模块要能独立自检，而且碰撞判定只需要量级
    正确（宁可估宽一点、保守跳过），不需要像素级精度。
    """
    cjk = sum(1 for c in text
              if "\u4e00" <= c <= "\u9fff" or "\u3000" <= c <= "\u303f"
              or "\uff00" <= c <= "\uffef" or "\u0370" <= c <= "\u03ff")
    return cjk * size * 1.0 + (len(text) - cjk) * size * 0.55


def element_boxes(svg: str) -> List[Tuple[float, float, float, float]]:
    """粗略提取可供碰撞判定的元素包围盒。

    只收能算的：rect / circle / line / text（估算字宽）/ path（取所有坐标点的包围盒）。
    宁可**保守**（path 的包围盒是曲线整体范围，比真实占有面积大），因为本函数的
    用途是“别把文字挪到别人身上”，多拦一次比漏拦一次便宜。

    text 的上下边界用 checker 自己的公式：baseline − 0.85×size 到 + 0.35×size。
    """
    boxes: List[Tuple[float, float, float, float]] = []
    for m in _RECT_RE.finditer(svg):
        x, y, w, h = (float(v) for v in m.groups())
        boxes.append((x, y, x + w, y + h))
    for m in _CIRCLE_RE.finditer(svg):
        cx, cy, r = (float(v) for v in m.groups())
        boxes.append((cx - r, cy - r, cx + r, cy + r))
    for m in _LINE_RE.finditer(svg):
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        boxes.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    for m in _TEXT_TAG_FULL_RE.finditer(svg):
        attrs, body = m.group(1), m.group(2)
        xy = _XY_RE.search(attrs)
        if not xy:
            continue
        boxes.append(_text_tag_box(attrs, body, float(xy.group(1)), float(xy.group(2))))
    for m in re.finditer(r'<path\b[^>]*\bd="([^"]*)"', svg):
        nums = [float(v) for v in _COORD_RE.findall(m.group(1))]
        if len(nums) >= 4:
            xs, ys = nums[0::2], nums[1::2]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _overlap_area(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _same_box(a, b, tol: float = 1.5) -> bool:
    return all(abs(a[i] - b[i]) < tol for i in range(4))


def _text_tag_box(attrs: str, body: str, x: float, y: float):
    """按**本模块自己的估算器**算一个 <text> 的包围盒。

    关键：碰撞判定与“排除自己”必须用**同一套估算**。早期版本用 checker 给的
    精确 content 矩形去排除自身，而我这里的字宽是估算的——两者一对不上（实测
    77px vs 116px），目标元素就被当成障碍物，于是**永远跳过，策略形同虚设**。
    """
    fs = _FS_RE.search(attrs)
    size = float(fs.group(1)) if fs else 18.0
    w = _text_width(re.sub(r"<[^>]+>", "", body).strip(), size)
    return (x, y - 0.85 * size, x + w, y + 0.35 * size)


def _locate_text_box(svg: str, x: float, y: float):
    """按 x/y 找到那个 <text> 并返回它的估算包围盒；找不到返回 None。"""
    for m in _TEXT_TAG_FULL_RE.finditer(svg):
        xy = _XY_RE.search(m.group(1))
        if not xy:
            continue
        if abs(float(xy.group(1)) - x) < 0.6 and abs(float(xy.group(2)) - y) < 0.6:
            return _text_tag_box(m.group(1), m.group(2), float(xy.group(1)), float(xy.group(2)))
    return None


def _needed_shift(axis: str, content, container, margin: float) -> float:
    """内容超出容器多少（正数 = 需要沿该轴**正方向**移动多少）。

    两个方向都要判：只判“超出右/下边”是不够的——内容完全没超时，旧写法会
    回落到“往左/上移”那个分支，算出一个例如 96px 的假位移（测试踩到）。
    返回 0 表示该轴本来就没问题。
    """
    if axis == "vertical":
        if content[3] > container[3]:        # 下边超出 → 往上挪
            return content[3] + margin - container[3]
        if content[1] < container[1]:        # 上边超出 → 往下挪
            return -(container[1] - content[1] + margin)
        return 0.0
    if content[2] > container[2]:            # 右边超出 → 往左挪
        return content[2] + margin - container[2]
    if content[0] < container[0]:            # 左边超出 → 往右挪
        return -(container[0] - content[0] + margin)
    return 0.0


def shift_content_into_container(
    svg: str, errors: Sequence[str], margin: float = MARGIN,
    max_shift: float = MAX_SHIFT,
) -> Tuple[str, List[str]]:
    """第二条策略：扩不动容器时，把越界的那个元素挪回容器内。

    为什么需要它（实测）：`header` 容器下边紧贴着 `plot`，把它扩到能装住内容就
    会与 `plot` 重叠 —— 一个超界错换成两个重叠错，复验发现没变好而回滚。
    而当时模型真正用的修法是**把文字往上挪**（y 126 → 120）。

    ## 两道安全边界（都是因为踩过坑才加的）

    ① **位移阀值**：`|delta| > max_shift` 就不挪，交模型。
       实测事故：它把 04 页底部的公式横移了 112.8px，正好挪到红色切线经过的地方。
    ② **目标位置碰撞检查**：先估算挪完之后该元素的包围盒，与图中其它元素
       （rect/circle/line/text/path）比对；重叠面积超过阀值就不挪。

    没有这两道边界会怎样：bounds 合规了，但文字被线穿过 / 压在其它字上。
    而 `svg_quality_checker` **不检查同一模块内元素之间的重叠**，所以它一声不响 ——
    结果是“用一个它看得见的错换一个它看不见的错”。

    只挪单个元素（报错文本自带它的 x/y），且**只挪报错的那个轴**。
    外层仍是“改完复验、不好就回滚”，所以最坏情况仍是白改一次。
    """
    out = svg
    notes: List[str] = []
    for msg in errors:
        m = _BOUNDS_RE.search(msg)
        if not m or m.group("container_kind") != "container":
            continue
        elem = _OVERFLOW_ELEM_RE.search(msg)
        if not elem or elem.group("tag") != "text":
            continue                      # 只挪文字；image/g 的位置含义不同，不猜
        ex, ey = float(elem.group("x")), float(elem.group("y"))
        content = (float(m.group("cx1")), float(m.group("cy1")),
                   float(m.group("cx2")), float(m.group("cy2")))
        container = (float(m.group("kx1")), float(m.group("ky1")),
                     float(m.group("kx2")), float(m.group("ky2")))
        axis = m.group("axis")
        delta = _needed_shift(axis, content, container, margin)
        if abs(delta) < 0.05:
            continue

        # —— 边界 ①：位移阀值 ————————————————————————
        if abs(delta) > max_shift:
            notes.append(f"跳过：需位移 {_fmt(delta)}px，超过阀值 {_fmt(max_shift)}px"
                         f"（大位移极可能压到其它元素），交模型")
            continue

        # —— 边界 ②：碰撞检查 ————————————————————————
        # 移动对象用本模块自己的估算器定位（与障碍物同一套，否则排除不了自身）
        own = _locate_text_box(out, ex, ey)
        if own is None:
            notes.append(f"跳过：定位不到 x={_fmt(ex)} y={_fmt(ey)} 的 <text> 包围盒")
            continue
        moved = (own[0] - delta if axis == "horizontal" else own[0],
                 own[1] - delta if axis == "vertical" else own[1],
                 own[2] - delta if axis == "horizontal" else own[2],
                 own[3] - delta if axis == "vertical" else own[3])
        hits = [(box, area) for box in element_boxes(out)
                if not _same_box(box, own)
                for area in [_overlap_area(moved, box)]
                if area >= MIN_COLLISION_AREA]
        if hits:
            worst = max(hits, key=lambda x: x[1])
            notes.append(f"跳过：挪完会与另 {len(hits)} 个元素重叠"
                         f"（最大 {_fmt(worst[1])}px²）—— checker 看不见这类重叠，"
                         f"不能拿它换掉一个超界错，交模型")
            continue

        hit: List[str] = []

        def repl(mt: re.Match) -> str:
            tag = mt.group(0)
            xy = _XY_RE.search(tag)
            if not xy or abs(float(xy.group(1)) - ex) > 0.6 or abs(float(xy.group(2)) - ey) > 0.6:
                return tag
            hit.append(tag)
            nx, ny = ex, ey
            if axis == "vertical":
                ny = round(ey - delta, 1)
            else:
                nx = round(ex - delta, 1)
            new = re.sub(r'\bx="[-\d.]+"', f'x="{_fmt(nx)}"', tag, count=1)
            return re.sub(r'\by="[-\d.]+"', f'y="{_fmt(ny)}"', new, count=1)

        out2 = _TEXT_TAG_RE.sub(repl, out)
        if not hit:
            notes.append(f"跳过：找不到 x={_fmt(ex)} y={_fmt(ey)} 的那个 <text>")
            continue
        out = out2
        axis_cn = "纵向" if axis == "vertical" else "横向"
        notes.append(f"挪文字：{axis_cn}位移 {_fmt(delta)}px"
                     f"（{_fmt(ex)},{_fmt(ey)} → "
                     f"{_fmt(ex) if axis == 'vertical' else _fmt(ex - delta)},"
                     f"{_fmt(ey - delta) if axis == 'vertical' else _fmt(ey)}）")
    return out, notes


def canvas_rect(svg: str) -> Tuple[float, float, float, float] | None:
    """读根 viewBox，返回 ``(x1, y1, x2, y2)``；读不到返回 None。"""
    m = _VIEWBOX_RE.search(svg)
    if not m:
        return None
    x, y, w, h = (float(v) for v in m.groups())
    return (x, y, x + w, y + h)


def expand_bounds_for_overflow(
    svg: str, errors: Sequence[str], margin: float = MARGIN
) -> Tuple[str, List[str]]:
    """把报「超出容器」的那些 ``<g>`` 的 bounds 扩到包住内容。

    返回 ``(新 svg, 人读的改动说明)``。**找不到目标或无需改动时原样返回**——
    调用方靠“文本有没有变”判断要不要写盘。

    两个设计选择：
      · **两轴都扩到包住内容**（不只报错的那个轴）。报错文本里的 content 矩形
        是完整的，而“完全落在容器区间内”才满足 checker；只修报错轴会留下
        残余溢出（当前在 5% 阈值下不报，下次阈值变了就是新麻烦）。
      · **夹取到画布**。余量会把框向外推 4px；容器本来就贴着画布边时，
        推出去反而造出一个“内容溢出画布”的新错（该错误本模块解不了，
        因为画布不能扩）。所以外扩后一律与 viewBox 取交集。
    """
    wants: Dict[str, List[Tuple[float, float, float, float]]] = {}
    notes: List[str] = []

    for msg in errors:
        m = _BOUNDS_RE.search(msg)
        if not m:
            continue
        if m.group("container_kind") != "container":
            notes.append("跳过：内容溢出的是画布 root viewBox（不是某个容器框画小了），需重排")
            continue
        gid = m.group("owner_id")
        if not gid:
            continue
        wants.setdefault(gid, []).append((
            float(m.group("cx1")), float(m.group("cy1")),
            float(m.group("cx2")), float(m.group("cy2")),
        ))

    canvas = canvas_rect(svg)
    out = svg
    for gid, contents in wants.items():
        old = read_group_bounds(out, gid)
        if old is None:
            notes.append(f"跳过 {gid}：找不到它的 data-pptx-bounds 属性")
            continue
        new = old
        for cx1, cy1, cx2, cy2 in contents:
            new = (min(new[0], cx1 - margin), min(new[1], cy1 - margin),
                   max(new[2], cx2 + margin), max(new[3], cy2 + margin))
        if canvas is not None:
            clamped = (max(new[0], canvas[0]), max(new[1], canvas[1]),
                       min(new[2], canvas[2]), min(new[3], canvas[3]))
            if clamped != new:
                # 夹取后如果**仍然包不住内容**，说明溢出已经超出画布能扩的范围，
                # 这不是“容器框画小了”而是内容整体太大 — 写一个半吊子 bounds
                # 只会浪费一次写盘与回滚（复验必然发现没变好）。直接跳过。
                if any(c[0] < clamped[0] or c[1] < clamped[1]
                       or c[2] > clamped[2] or c[3] > clamped[3] for c in contents):
                    notes.append(f"跳过 {gid}：内容超出画布可用范围，扩框包不住（需重排或改小内容）")
                    continue
                notes.append(f"{gid}: 外扩被画布夹取 "
                             f"({_fmt(new[0])},{_fmt(new[1])})-({_fmt(new[2])},{_fmt(new[3])})"
                             f" → ({_fmt(clamped[0])},{_fmt(clamped[1])})-"
                             f"({_fmt(clamped[2])},{_fmt(clamped[3])})")
                new = clamped
        if new[0] >= new[2] or new[1] >= new[3]:
            notes.append(f"跳过 {gid}：区间退化，内容无法被容器包住")
            continue
        if new == old:
            continue
        out = set_group_bounds(out, gid, new)
        notes.append(f"{gid}: ({_fmt(old[0])},{_fmt(old[1])})-({_fmt(old[2])},{_fmt(old[3])})"
                     f" → ({_fmt(new[0])},{_fmt(new[1])})-({_fmt(new[2])},{_fmt(new[3])})")
    return out, notes


# ---------------------------------------------------------------------------
# 第四条策略：超长单行文字自动拆行（reflow）
# ---------------------------------------------------------------------------
#: 拆行行距（相对 font-size 的倍数）。checker 的 text 盒是 baseline −0.85×size
#: 到 +0.35×size（高 1.2×size），行距给 1.5×size 留足余量。
REFLOW_LINE_GAP = 1.5


def _is_cjk_dominant(text: str) -> bool:
    """CJK 占比 ≥ 60% 才逐字拆（拉丁/公式按字断会破坏语义，交模型）。"""
    if not text:
        return False

    def _cjk(c: str) -> bool:
        return ("\u4e00" <= c <= "\u9fff"
                or "\u3000" <= c <= "\u303f"
                or "\uff00" <= c <= "\uffef")
    return sum(1 for c in text if _cjk(c)) / len(text) >= 0.6


def _read_text_content(svg: str, x: float, y: float):
    """定位 ``<text x y>``，返回 ``(纯文本, font_size, 是否含 tspan)``；找不到返回 None。"""
    for m in _TEXT_TAG_FULL_RE.finditer(svg):
        xy = _XY_RE.search(m.group(1))
        if not xy:
            continue
        if abs(float(xy.group(1)) - x) < 0.6 and abs(float(xy.group(2)) - y) < 0.6:
            attrs, body = m.group(1), m.group(2)
            fs = _FS_RE.search(attrs)
            size = float(fs.group(1)) if fs else 18.0
            has_tspan = "<tspan" in body
            text = re.sub(r"<[^>]+>", "", body)
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                         ("&quot;", '"'), ("&#39;", "'")):
                text = text.replace(a, b)
            return text, size, has_tspan
    return None


def _would_hit_other_text(
    svg: str, x: float, y: float, size: float, dy: float, n_lines: int
) -> bool:
    """拆出的新行（第 2..n 行）的 baseline 区间里，是否已存在别的 <text>。

    只查新行的垂直区间：第一行就是原文字位置，本就不与任何文字重叠；新增风险
    只来自往下长出的行。水平方向不再另判——新行与原文字同 x、同宽，而原文字所在
    行已无障碍物（否则它早就在 P3 报重叠）。
    """
    lo = y + dy - 0.85 * size                      # 第二行顶部
    hi = y + (n_lines - 1) * dy + 0.35 * size      # 最后一行底部
    for m in _TEXT_TAG_FULL_RE.finditer(svg):
        xy = _XY_RE.search(m.group(1))
        if not xy:
            continue
        ox, oy = float(xy.group(1)), float(xy.group(2))
        if abs(ox - x) < 0.6 and abs(oy - y) < 0.6:
            continue
        if lo - 2 < oy < hi + 2:
            return True
    return False


def _rewrite_text_wrapped(svg: str, x: float, y: float, lines: List[str], dy: float) -> str:
    """把定位到的单行 <text> 重写为「首行直接文本 + 后续 tspan」（checker 推荐形态）。"""
    for m in _TEXT_TAG_FULL_RE.finditer(svg):
        xy = _XY_RE.search(m.group(1))
        if not xy:
            continue
        if abs(float(xy.group(1)) - x) < 0.6 and abs(float(xy.group(2)) - y) < 0.6:
            attrs = m.group(1)
            first = lines[0]
            tspans = "".join(
                f'<tspan x="{_fmt(x)}" dy="{_fmt(dy)}">{ln}</tspan>'
                for ln in lines[1:]
            )
            return svg[:m.start()] + f"<text{attrs}>{first}{tspans}</text>" + svg[m.end():]
    return svg


def reflow_overflow_text(
    svg: str, errors: Sequence[str], margin: float = MARGIN,
    line_gap: float = REFLOW_LINE_GAP,
) -> Tuple[str, List[str]]:
    """第四条策略：把「超长单行文字」拆成多行 <tspan>。

    只处理同时满足下面全部条件的情况，否则原样返回（交模型）：
      · 报错是「<text> 超出容器」且为**水平轴**（垂直溢出拆行会越拆越糟）
      · 目标文字是**单行**（不含 <tspan>；已分行的不猜）
      · 文字以 **CJK 为主**（拉丁/公式逐字断会破坏语义）
      · 拆完后最后一行仍落在容器下边界之内（放不下说明要缩短，不是拆行）
      · 新行不与下方其它文字重叠（checker 看不见同模块重叠，不能换一个隐藏错）

    拆行是**内容改动**，比扩框/挪元素风险高，所以放策略顺序最后、条件最严。
    外层仍是「快照 → 改 → 复验 → 回滚」。
    """
    out = svg
    notes: List[str] = []
    for msg in errors:
        m = _BOUNDS_RE.search(msg)
        if (not m or m.group("container_kind") != "container"
                or m.group("axis") != "horizontal"):
            continue
        elem = _OVERFLOW_ELEM_RE.search(msg)
        if not elem or elem.group("tag") != "text":
            continue
        ex, ey = float(elem.group("x")), float(elem.group("y"))
        kx2 = float(m.group("kx2"))
        ky2 = float(m.group("ky2"))

        content = _read_text_content(out, ex, ey)
        if content is None:
            notes.append(f"跳过：定位不到 x={_fmt(ex)} y={_fmt(ey)} 的 <text>")
            continue
        text, size, has_tspan = content
        if has_tspan:
            continue            # 已分行，不猜
        text = text.strip()
        if not text or not _is_cjk_dominant(text):
            notes.append(f"跳过：非 CJK 主导的文字不逐字拆（拆拉丁/公式有语义风险）")
            continue

        avail = kx2 - ex - margin
        chars_per_line = int(avail / size) if size > 0 else 0
        if chars_per_line <= 0:
            notes.append(f"跳过：可用宽度 {_fmt(avail)}px 装不下一个字")
            continue
        lines = [text[i:i + chars_per_line] for i in range(0, len(text), chars_per_line)]
        if len(lines) <= 1:
            continue            # 估算与 checker 不符，不猜

        dy = size * line_gap
        last_baseline = ey + (len(lines) - 1) * dy
        if last_baseline + 0.35 * size > ky2:
            notes.append(f"跳过：拆成 {len(lines)} 行会超出容器下边界（需缩短文字，交模型）")
            continue
        if _would_hit_other_text(out, ex, ey, size, dy, len(lines)):
            notes.append(f"跳过：拆行会压到下方其它文字（checker 看不见这类重叠）")
            continue

        out = _rewrite_text_wrapped(out, ex, ey, lines, dy)
        notes.append(f"拆行：单行 {len(text)} 字 → {len(lines)} 行"
                     f"（每行 ≤{chars_per_line} 字，行距 {_fmt(dy)}px）")
    return out, notes


# ===========================================================================
# 离线自检
# ===========================================================================
_SAMPLE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <g id="header" data-pptx-bounds="56 36 1168 90">
    <text x="56" y="76" font-size="44">割线斜率的表达式</text>
    <text x="56" y="120" font-size="18">微分中 Δx 表示自变量的增量</text>
  </g>
  <g id="figure" data-pptx-bounds="406 132 798 522"><rect x="406" y="132" width="798" height="522"/></g>
</svg>"""

_ERR = (
    '<text> (x=56, y=120; text=\'微分中 Δx 表示自变量的增量\') exceeds '
    '<g id="header"> data-pptx-bounds on the vertical axis: '
    'content (56.0, 110.7)-(493.6, 132.3), container (56.0, 36.0)-(1224.0, 126.0), '
    'overflow horizontal 0.0%, vertical 7.0% (error above 5%); expand the root module bounds'
)


def self_check() -> None:
    print("🔎 bounds_fix 自检\n")

    # ① 读/写回环：属性格式是 x y w h，读出来要换算成 x1y1x2y2
    got = read_group_bounds(_SAMPLE, "header")
    assert got == (56.0, 36.0, 1224.0, 126.0), got
    back = set_group_bounds(_SAMPLE, "header", (10.0, 20.0, 110.0, 220.0))
    assert 'data-pptx-bounds="10 20 100 200"' in back, back[:200]
    assert read_group_bounds(back, "header") == (10.0, 20.0, 110.0, 220.0)
    print("   ① 读写回环 OK：x y w h ↔ x1y1x2y2 换算正确")

    # ② 只动目标 <g>，别的 <g> 一字不改
    assert 'id="figure" data-pptx-bounds="406 132 798 522"' in back
    print("   ② 局部性 OK：只有报错的 <g> 被改，兄弟模块原样保留")

    # ③ 真实报错：内容底 132.3 > 容器底 126 → 必须扩到 ≥ 136.3
    new, notes = expand_bounds_for_overflow(_SAMPLE, [_ERR])
    rect = read_group_bounds(new, "header")
    assert rect is not None and rect[3] >= 132.3, f"没包住内容：{rect}"
    assert rect[3] >= 132.3 + 3.9, f"余量不足：{rect}"     # +4px 余量
    assert rect[2] == 1224.0, f"水平方向不该被无谓扩大：{rect}"
    assert any("header" in n for n in notes), notes
    print(f"   ③ 扩框 OK：底部 126 → {_fmt(rect[3])}（含 4px 余量），"
          f"水平未变（只有垂直越界）")

    # ④ 幂等：再跑一次不应再变（解已满足）
    again, notes2 = expand_bounds_for_overflow(new, [_ERR])
    assert again == new and not notes2, "第二次不该再改"
    print("   ④ 幂等 OK：已满足的 bounds 不会被反复扩大")

    # ⑤ 多条报错指向同一个 <g> → 并集一次算完（不跨画布）
    err2 = _ERR.replace('on the vertical axis: content (56.0, 110.7)-(493.6, 132.3)',
                        'on the horizontal axis: content (56.0, 110.7)-(1240.0, 132.3)')
    merged, _ = expand_bounds_for_overflow(_SAMPLE, [_ERR, err2])
    rect = read_group_bounds(merged, "header")
    assert rect[2] >= 1240.0 and rect[3] >= 132.3, f"并集没算全：{rect}"
    print(f"   ⑤ 多报错并集 OK：水平 {_fmt(rect[2])} / 垂直 {_fmt(rect[3])} 同时满足")

    # ⑥ 画布级溢出必须跳过（没有容器框可扩，要重排）
    canvas_err = _ERR.replace('<g id="header"> data-pptx-bounds', "the root viewBox") \
                     .replace("container (56.0, 36.0)-(1224.0, 126.0)", "canvas (0.0, 0.0)-(1280.0, 720.0)")
    out, notes = expand_bounds_for_overflow(_SAMPLE, [canvas_err])
    assert out == _SAMPLE and any("root viewBox" in n for n in notes), notes
    print("   ⑥ 画布级溢出 OK：跳过并说明原因（需重排，不是扩框能解）")

    # ⑦ 重叠类报错一概不碰（那是布局问题，没有唯一解）
    overlap = ('<g id="header"> data-pptx-bounds overlaps <g id="figure"> data-pptx-bounds '
               'by 16.0px x 240.0px; keep ordinary direct-root module zones disjoint')
    out, notes = expand_bounds_for_overflow(_SAMPLE, [overlap])
    assert out == _SAMPLE and not notes, "重叠类不该被本模块处理"
    print("   ⑦ 重叠类 OK：完全不碰（交模型，因为要重划版面）")

    # ⑧ 目标不存在时安全退出
    out = set_group_bounds(_SAMPLE, "不存在的id", (0, 0, 1, 1))
    assert out == _SAMPLE
    assert read_group_bounds(_SAMPLE, "不存在的id") is None
    print("   ⑧ 容错 OK：找不到的 <g> / 缺 bounds 属性均安全返回，不抛异常")

    # ⑨ 画布夹取：容器贴着画布左边（x1=0），外扩不得造出负数坐标
    edge = _SAMPLE.replace('id="header" data-pptx-bounds="56 36 1168 90"',
                           'id="header" data-pptx-bounds="0 0 1280 60"')
    edge_err = _ERR.replace("content (56.0, 110.7)-(493.6, 132.3)",
                            "content (0.0, 110.7)-(493.6, 132.3)")
    new_edge, notes_edge = expand_bounds_for_overflow(edge, [edge_err])
    rect = read_group_bounds(new_edge, "header")
    assert rect[0] >= 0.0 and rect[1] >= 0.0, f"外扩超出画布：{rect}"
    assert rect[3] >= 132.3, f"夹取后没包住内容：{rect}"
    assert any("夹取" in n for n in notes_edge), notes_edge
    print(f"   ⑨ 画布夹取 OK：x1 被夹在 0（未变负），底部仍扩到 {_fmt(rect[3])}")

    # ⑩ 内容超出画布可用范围 → 扩框包不住 → 必须跳过（写半吊子修复只会浪费一轮）
    wide = _ERR.replace("content (56.0, 110.7)-(493.6, 132.3)",
                        "content (56.0, 110.7)-(1400.0, 132.3)")
    out, notes = expand_bounds_for_overflow(_SAMPLE, [wide])
    assert out == _SAMPLE, "包不住时不该写入"
    assert any("包不住" in n for n in notes), notes
    print("   ⑩ 包不住则跳过 OK：内容超出画布时不动文件，说明原因")

    # ⑪ 第二条策略：扩不动就挪越界元素（只挪报错的那个轴）
    #     样例里的文字得真的在 y=126，否则“匹配不上”是正确行为而不是 bug（踩过）。
    sample_at_126 = _SAMPLE.replace('y="120" font-size="18"', 'y="126" font-size="18"')
    err_el = ("<text> (x=56, y=126; text='微分中 Δx 表示自变量的增量') exceeds "
              "<g id=\"header\"> data-pptx-bounds on the vertical axis: "
              "content (56.0, 110.7)-(493.6, 132.3), container (56.0, 36.0)-(1224.0, 126.0)")
    moved, notes = shift_content_into_container(sample_at_126, [err_el])
    my = re.search(r'<text x="56" y="([-\d.]+)" font-size="18"', moved)
    assert my, "没找到目标 <text>"
    new_y = float(my.group(1))
    # 需要把底部 132.3 压进容器底 126 并留 4px → 上移 10.3
    assert abs(new_y - 115.7) < 0.2, f"位移不对：y={new_y}（期望 ~115.7）"
    assert '<text x="56" y="76"' in moved, "不该动到不相关的文字"
    assert any("挪文字" in n for n in notes), notes
    print(f"   ⑪ 挪元素 OK：y 126 → {new_y}（上移 10.3px），其它文字未被处理")

    # ⑫ 坐标对不上时必须说明原因而不是默默改错位置
    bad_fmt = err_el.replace("y=126", "y=999")
    _, notes = shift_content_into_container(sample_at_126, [bad_fmt])
    assert any("定位不到" in n for n in notes), notes
    print("   ⑫ 坐标对不上 OK：不猜位置，明确报定位不到")

    # ⑬ 第三条策略：把被完全包含的 root 模块嵌进外层（层级错）
    nested_svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">\n'
                  '  <g id="panel" data-pptx-bounds="620 146 620 494">\n'
                  '    <rect x="620" y="146" width="620" height="494"/>\n'
                  '  </g>\n'
                  '  <g id="ann" data-pptx-bounds="1104 234 116 62">\n'
                  '    <text x="1104" y="256">切线</text>\n'
                  '  </g>\n'
                  '</svg>')
    ovl = ('<g id="panel"> data-pptx-bounds overlaps <g id="ann"> data-pptx-bounds '
           'by 116.0px x 62.0px; keep ordinary direct-root module zones disjoint')
    out, notes = nest_contained_root_groups(nested_svg, [ovl])
    i_p, i_a, close = out.find('id="panel"'), out.find('id="ann"'), out.rfind("</g>")
    assert i_p < i_a < close, f"没有嵌进去：{out}"
    assert any("嵌套" in n for n in notes), notes
    # 删除时要保证原文本其余部分不丢
    assert 'rect x="620"' in out and '切线' in out and out.count("<g") == out.count("</g>")
    # 部分重叠（不构成包含）不能强行嵌套——那需要重划版面
    partial = '<svg viewBox="0 0 100 100"><g id="a" data-pptx-bounds="0 0 60 60"/><g id="b" data-pptx-bounds="40 40 60 60"/></svg>'
    err_p = ('<g id="a"> data-pptx-bounds overlaps <g id="b"> data-pptx-bounds '
             'by 20.0px x 20.0px')
    out_p, notes_p = nest_contained_root_groups(partial, [err_p])
    assert out_p == partial and not notes_p, f"部分重叠不该被嵌套：{notes_p}"
    print("   ⑬ 嵌套策略 OK：完全包含→嵌入且标签配平；部分重叠→不碰（需重划版面）")

    # ⑭ 「挪元素」的两道安全边界（都是因为踩过坑才加的）
    big_err = ("<text> (x=1016, y=590; text='y - f(x0)') exceeds <g id=\"pg\"> "
               "data-pptx-bounds on the horizontal axis: "
               "content (1016.0, 573.0)-(1330.0, 597.0), "
               "container (620.0, 146.0)-(1240.0, 640.0)")
    big_svg = ('<svg viewBox="0 0 1280 720"><g id="pg" data-pptx-bounds="620 146 620 494">'
               '<path d="M700 600 L1180 300"/></g></svg>')
    out_b, notes_b = shift_content_into_container(big_svg, [big_err])
    assert out_b == big_svg and any("阀值" in n for n in notes_b), notes_b
    print("   ⑭-① 位移阀值 OK：94px 大位移（实测会把公式挪到切线上）被拦，交模型")

    small_err = ("<text> (x=200, y=190; text='ABCDEFG') exceeds <g id=\"box\"> "
                 "data-pptx-bounds on the horizontal axis: "
                 "content (200.0, 173.0)-(316.0, 197.0), "
                 "container (100.0, 100.0)-(300.0, 200.0)")
    blocked = ('<svg viewBox="0 0 1280 720"><g id="box" data-pptx-bounds="100 100 200 100">'
               '<rect x="100" y="100" width="200" height="100"/>'
               '<line x1="150" y1="180" x2="290" y2="180"/>'
               '<text x="200" y="190" font-size="20">ABCDEFG</text></g></svg>')
    out_c, notes_c = shift_content_into_container(blocked, [small_err])
    assert out_c == blocked and any("重叠" in n for n in notes_c), notes_c
    print("   ⑭-② 碰撞检查 OK：小位移但目标位置压着线条 → 不挪"
          "（checker 看不见这类重叠，不能拿它换超界错）")

    clean = ('<svg viewBox="0 0 1280 720"><g id="box" data-pptx-bounds="100 100 200 100">'
             '<text x="200" y="190" font-size="20">ABCDEFG</text></g></svg>')
    out_d, notes_d = shift_content_into_container(clean, [small_err])
    assert out_d != clean and 'x="180"' in out_d, (out_d, notes_d)
    print("   ⑭-③ 正常挪动 OK：20px 位移且无障碍 → 允许（200→180）")

    # ⑮ 位移方向判定：内容没超界时必须返回 0（旧写法会算出一个假位移）
    box_ok = (100.0, 100.0, 300.0, 200.0)
    assert _needed_shift("horizontal", (150.0, 120.0, 280.0, 180.0), box_ok, 4.0) == 0.0
    assert _needed_shift("horizontal", (50.0, 120.0, 280.0, 180.0), box_ok, 4.0) < 0
    assert _needed_shift("horizontal", (150.0, 120.0, 320.0, 180.0), box_ok, 4.0) > 0
    assert _needed_shift("vertical", (150.0, 120.0, 280.0, 180.0), box_ok, 4.0) == 0.0
    print("   ⑮ 方向判定 OK：未超界→0，左/上超→负，右/下超→正")

    # ⑯ 第四条策略：超长单行中文文字 → 自动拆成多行 tspan
    reflow_svg = ('<svg viewBox="0 0 1280 720">'
                  '<g id="challenge-box" data-pptx-bounds="56 556 484 126">'
                  '<text x="116" y="596" font-size="22">用 x = 0 处的切线估计 e</text>'
                  '<text x="90" y="634" font-size="18" fill="#5B6478">'
                  '提示：先写切线，再由凸性判断它与曲线的高低，误差方向自然出来</text>'
                  '</g></svg>')
    reflow_err = ('<text> (x=90, y=634; text=\'提示：先写切线，再由凸性判断它与曲线的高低，误差方向自然出来\') '
                  'exceeds <g id="challenge-box"> data-pptx-bounds on the horizontal axis: '
                  'content (90.0, 618.7)-(662.4, 640.3), container (56.0, 556.0)-(540.0, 682.0), '
                  'overflow horizontal 25.3%, vertical 0.0% (error above 5%); '
                  'expand the root module bounds into available non-overlapping space; '
                  'otherwise reflow the text; ≈19.1 px per CJK char at 18px incl. headroom; '
                  '≈25 chars fit in 484 px')
    rf, notes = reflow_overflow_text(reflow_svg, [reflow_err])
    assert rf != reflow_svg and '<tspan' in rf, (rf, notes)
    assert '提示：先写切线，再由凸性判断它与曲线的高低' in rf, rf
    assert '<tspan x="90" dy="27">' in rf, rf
    assert 'y="596" font-size="22"' in rf, '标题不该被改动'
    assert any('拆行' in n for n in notes), notes
    print("   ⑯ 拆行策略 OK：28 字单行 → 24+4 两行 tspan，标题原样保留")

    # ⑰ 拆行的四道安全边界（都该不碰）
    vert_err = reflow_err.replace('on the horizontal axis', 'on the vertical axis')
    out_v, notes_v = reflow_overflow_text(reflow_svg, [vert_err])
    assert out_v == reflow_svg and not notes_v, notes_v

    tspan_svg = reflow_svg.replace(
        '>提示：先写切线，再由凸性判断它与曲线的高低，误差方向自然出来</text>',
        '>提示：先写切线，再由凸性判断<tspan x="90" dy="24">误差方向自然出来</tspan></text>')
    out_t, notes_t = reflow_overflow_text(tspan_svg, [reflow_err])
    assert out_t == tspan_svg, notes_t

    latin_svg = reflow_svg.replace('提示：先写切线，再由凸性判断它与曲线的高低，误差方向自然出来',
                                   'y = f(x0) + tangent line equation of the curve at x0')
    out_l, notes_l = reflow_overflow_text(latin_svg, [reflow_err])
    assert out_l == latin_svg and any('非 CJK' in n for n in notes_l), notes_l

    short_svg = reflow_svg.replace('data-pptx-bounds="56 556 484 126"',
                                   'data-pptx-bounds="56 556 484 60"')
    short_err = reflow_err.replace('container (56.0, 556.0)-(540.0, 682.0)',
                                   'container (56.0, 556.0)-(540.0, 616.0)')
    out_s, notes_s = reflow_overflow_text(short_svg, [short_err])
    assert out_s == short_svg and any('超出容器下边界' in n for n in notes_s), notes_s
    print("   ⑰ 拆行安全边界 OK：垂直溢出 / 已有tspan / 非CJK / 超出下边界 均不拆")

    print("\n✅ bounds_fix 自检通过：读写换算、局部性、扩框+余量、幂等、并集、"
          "画布级跳过、重叠类不碰、画布夹取、挪元素及两道安全边界、嵌套、方向判定、"
          "坐标解析、容错 全部正常")


def main() -> None:
    ap = argparse.ArgumentParser(description="容器框自动修复（给 P4 用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    self_check()


if __name__ == "__main__":
    main()
