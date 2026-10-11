#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""函数图像的几何计算 —— 把坐标算术从模型手里拿走。

## 为什么需要这个模块

实测一次 P3 单页调用的隐藏推理（`runs/reasoning_capture.txt`）：

    推理 50,209 字符 / 正文 2,884 字符  =  17 : 1
    其中 74%（37,152 字符）是手算坐标
    4,386 个数字、1,580 个等号/乘除号、sy 提及 158 次、xMath 45 次

推理原文里能看到完整的"人肉绘图流程"：

    设 SVG 绘图区域 x from 560 to 1180 … 映射 sx = 560 + xMath/6*620
    取曲线 y = 0.35*x² + 0.5 … x=-3: y=3.65 … x=-2: y=1.9 …
    P 取在 x=2：sx = 560+103.33*5 = 1076.67，sy = 640-1.9*96 = 457.6
    切线斜率 f'(2) = 0.7x = 1.4，SVG 中 dy/dx = -1.4*(96/103.33) = -1.3006
    取 dx = ±90 → dy = -117

**这是一件确定性的算术工作，被塞给了概率模型。** 后果是一个二选一：

    reasoning=low   它手算 → 几何对，但 74% 的时间和 7 倍 token 烧在这里
    reasoning=none  它不算 → 快，但几何崩（实测：割线全部汇聚到曲线端点
                              而不过自称的 P，曲线画成 S 形，坐标轴都丢了）

本模块把这段算术拿走：采样、求导、坐标映射、直线裁剪全部由代码完成，
直接给出可用的 SVG 坐标。模型只负责"选哪个函数、标哪几个点、写什么文案、
怎么排版"——那才是它该干的。

## 定位

放在 PPT-Agent 而不是 skill 目录：skill 是打包的第三方产物
（`attribution_guard.py` 会校验 LICENSE 摘要），往里加文件会在升级时被冲掉，
而且它是"我们这条链路"的工具，不是 skill 协议的一部分。

## 用法::

    from plot_math import plot_geometry, format_geometry
    geo = plot_geometry(expr="0.35*x**2 + 0.5", domain=(-3, 3),
                        area=(560, 140, 1180, 620),
                        point=2.0, secants=[0.0, 1.0, 1.5])
    print(format_geometry(geo))

    python plot_math.py --selftest     # 离线自检，不调 API
"""
from __future__ import annotations

import argparse
import ast
import math
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# —— 控制台编码统一（与 agent.py / pipeline.py 一致）——
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

# ===========================================================================
# 一、安全表达式求值
# ===========================================================================
# 为什么不用 eval：表达式来自模型输出的 JSON，属于**不可信输入**。
# 裸 eval 会执行任意代码（`__import__('os').system(...)` 之类）。
# 这里走 AST 白名单：只放行数值运算与白名单函数，其余一律拒绝。
_ALLOWED_FUNCS: Dict[str, Callable[..., float]] = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "sqrt": math.sqrt, "exp": math.exp,
    "log": math.log, "ln": math.log, "log10": math.log10, "log2": math.log2,
    "abs": abs, "pow": pow, "floor": math.floor, "ceil": math.ceil,
}
_ALLOWED_CONSTS: Dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Constant,
    ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.USub, ast.UAdd,
)

# 模型很可能写数学记号而不是 Python 记号，先归一化
_SUPERSCRIPT = {"²": "**2", "³": "**3", "⁴": "**4", "⁻": "-", "¹": "**1"}
_NORMALIZE = (("^", "**"), ("×", "*"), ("·", "*"), ("÷", "/"),
              ("−", "-"), ("–", "-"), ("—", "-"), ("π", "pi"))
# 根号要单独处理：`√x` 得变成 `sqrt(x)`，而 `√(` 只需去掉符号。
# 直接换成 "sqrt" 会产出 `sqrtx` 这种非法式（早期版本就这么错过）。
_SQRT_RE = re.compile(r"√\s*([A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?)")


def normalize_expr(expr: str) -> str:
    """把常见的数学写法归一成 Python 表达式。"""
    out = (expr or "").strip()
    out = out.replace("√(", "sqrt(")
    out = _SQRT_RE.sub(r"sqrt(\1)", out)
    for src, dst in _NORMALIZE:
        out = out.replace(src, dst)
    for src, dst in _SUPERSCRIPT.items():
        out = out.replace(src, dst)
    # `2x` / `3sin(x)` 这类省略乘号的写法：数字后紧跟字母或左括号
    out = re.sub(r"(?<=\d)(?=[A-Za-z(])", "*", out)
    return out


def compile_expr(expr: str, var: str = "x") -> Callable[[float], float]:
    """把表达式编译成 ``f(x) -> float``（``var`` 可指定自变量名，参数曲线用 ``t``）。
    **白名单 AST，拒绝任意代码。**

    抛 ``ValueError`` 而不是静默返回 None：调用方（staged）会捕获它，
    降级回"不带几何块"的老路径，模型自己想办法。
    """
    src = normalize_expr(expr)
    if not src:
        raise ValueError("函数表达式为空")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"函数表达式无法解析：{src!r}（{exc.msg}）") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"表达式含不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCS \
                and node.id not in _ALLOWED_CONSTS and node.id != var:
            raise ValueError(f"表达式含未定义的标识符：{node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise ValueError("只允许调用白名单内的数学函数")
            if node.keywords:
                raise ValueError("数学函数不接受关键字参数")

    code = compile(tree, "<expr>", "eval")

    def f(v: float) -> float:
        try:
            return float(eval(code, {"__builtins__": {}}, {**ALLOWED_VARS, var: float(v)}))
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            raise ValueError(f"f({v}) 求值失败：{exc}") from exc

    return f


ALLOWED_VARS: Dict[str, float] = {**_ALLOWED_CONSTS, **_ALLOWED_FUNCS}


# ===========================================================================
# 二、几何计算
# ===========================================================================
def derivative_at(f: Callable[[float], float], x: float, h: float = 1e-5) -> float:
    """中心差分求导。sympy 未安装，数值微分对本场景（教学曲线）精度绰绰有余。"""
    return (f(x + h) - f(x - h)) / (2 * h)


def _clip_line_to_rect(p: Tuple[float, float], q: Tuple[float, float],
                       rect: Tuple[float, float, float, float]
                       ) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """把线段 ``p→q`` 裁剪到矩形 ``(x0,y0,x1,y1)``（Liang-Barsky）。

    切线要画满绘图区、但不能探到框外；裁剪靠参数 t 的两个边界 t0/t1 一次算出，
    不用逐轴分情况讨论。
    """
    x0, y0, x1, y1 = rect
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for edge_p, edge_q in ((-dx, p[0] - x0), (dx, x1 - p[0]),
                           (-dy, p[1] - y0), (dy, y1 - p[1])):
        if edge_p == 0:
            if edge_q < 0:
                return None                      # 平行于该边且在框外
            continue
        t = edge_q / edge_p
        if edge_p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if t0 > t1:
        return None
    return ((p[0] + t0 * dx, p[1] + t0 * dy), (p[0] + t1 * dx, p[1] + t1 * dy))


def _fmt(v: float) -> str:
    """SVG 坐标保留 1 位小数，整数值去掉 ``.0``（省 token 也更干净）。"""
    r = round(v, 1)
    return str(int(r)) if abs(r - round(r)) < 1e-9 else f"{r:.1f}"


@dataclass
class PlotGeometry:
    expr: str
    domain: Tuple[float, float]
    y_range: Tuple[float, float]
    area: Tuple[float, float, float, float]     # SVG 绘图区 x0,y0,x1,y1
    scale_x: float                              # 每个数学单位的 SVG 像素
    scale_y: float
    curve_d: str = ""                           # 曲线 path 的 d
    samples: int = 0
    #: 曲线的 SVG 采样点。标注避让需要它（从 curve_d 反解析不如直接留着）。
    curve_points: List[Tuple[float, float]] = field(default_factory=list)
    axes: Dict[str, str] = field(default_factory=dict)
    point: Optional[Dict[str, float]] = None     # 第一个切点 P（向后兼容）
    tangent: Optional[Dict[str, object]] = None  # 第一条切线（向后兼容）
    points: List[Dict[str, object]] = field(default_factory=list)  # 所有切点（含各自切线）
    secants: List[Dict[str, object]] = field(default_factory=list)
    sharp: Optional[Dict[str, float]] = None     # 尖点（|x| 类，不可导，无切线）
    notes: List[str] = field(default_factory=list)

    def to_svg_x(self, x: float) -> float:
        x0, _, x1, _ = self.area
        a, b = self.domain
        return x0 + (x - a) / (b - a) * (x1 - x0)

    def to_svg_y(self, y: float) -> float:
        _, y0, _, y1 = self.area
        lo, hi = self.y_range
        return y1 - (y - lo) / (hi - lo) * (y1 - y0)


def plot_geometry(
    expr: str,
    domain: Sequence[float],
    area: Sequence[float],
    *,
    point: Optional[float] = None,
    points: Optional[Sequence[float]] = None,
    sharp: Optional[float] = None,
    secants: Sequence[float] = (),
    samples: int = 65,
    y_range: Optional[Sequence[float]] = None,
    pad_ratio: float = 0.10,
) -> PlotGeometry:
    """算出函数图像的全部几何。

    ``domain`` = 数学定义域 ``(xmin, xmax)``；``area`` = SVG 绘图区 ``(x0,y0,x1,y1)``
    （左上角 + 右下角，屏幕坐标 y 向下）。``point`` = 单个切点的 x；``points`` = 多个
    切点的 x 列表（一页要画多条切线时用，如「正负单调」「陡峭」）。``secants`` = 各割线
    另一端 Q 的 x（都连到第一个切点）。``sharp`` = 尖点 x（|x| 这类不可导点，只画割线不画切线）。
    ``y_range`` 不给就按采样结果自动留白。

    全程只做算术，不做任何"估计"——这是本模块存在的全部理由。
    """
    f = compile_expr(expr)
    a, b = float(domain[0]), float(domain[1])
    if not a < b:
        raise ValueError(f"定义域必须递增：{domain}")
    x0, y0, x1, y1 = (float(v) for v in area)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"绘图区必须是左上-右下的矩形：{area}")
    n = max(9, int(samples))

    # 尖点 x 强制加入采样点：保证 |x| 这类函数在尖点处是精确折点（而不是被采样抹圆）
    sample_xs = [a + (b - a) * i / (n - 1) for i in range(n)]
    if sharp is not None:
        sx0 = float(sharp)
        if not a <= sx0 <= b:
            raise ValueError(f"尖点 x={sx0} 不在定义域 [{a}, {b}] 内")
        if not any(abs(x - sx0) < 1e-9 for x in sample_xs):
            sample_xs.append(sx0)
            sample_xs.sort()

    pts = [(x, 0.0) for x in sample_xs]
    ys: List[float] = []
    for i, (x, _) in enumerate(pts):
        try:
            y = f(x)
        except ValueError:
            y = float("nan")
        if not math.isfinite(y):
            raise ValueError(f"f({x:.4g}) 不是有限值，无法绘图（检查定义域或表达式）")
        ys.append(y)
        pts[i] = (x, y)

    if y_range is not None:
        lo, hi = float(y_range[0]), float(y_range[1])
        if not lo < hi:
            raise ValueError(f"y_range 必须递增：{y_range}")
    else:
        ymin, ymax = min(ys), max(ys)
        span = (ymax - ymin) or max(1.0, abs(ymax) * 0.2)
        lo, hi = ymin - span * pad_ratio, ymax + span * pad_ratio

    geo = PlotGeometry(
        expr=normalize_expr(expr), domain=(a, b), y_range=(lo, hi),
        area=(x0, y0, x1, y1),
        scale_x=(x1 - x0) / (b - a), scale_y=(y1 - y0) / (hi - lo),
        samples=len(pts),
    )

    # —— 曲线 path：折线采样（65 点在教学曲线上肉眼无敌；比手写贝塞尔可靠）——
    geo.curve_points = [(geo.to_svg_x(x), geo.to_svg_y(y)) for x, y in pts]
    geo.curve_d = "M" + " L".join(
        f"{_fmt(sx)} {_fmt(sy)}" for sx, sy in geo.curve_points)

    # —— 坐标轴：x 轴画在 y=0 处（超出 y_range 就贴底边），y 轴画在 x=0 处 ——
    ax_y = geo.to_svg_y(min(max(0.0, lo), hi))
    ay_x = geo.to_svg_x(min(max(0.0, a), b))
    geo.axes = {
        "x_axis": f"({_fmt(x0)}, {_fmt(ax_y)}) → ({_fmt(x1)}, {_fmt(ax_y)})",
        "y_axis": f"({_fmt(ay_x)}, {_fmt(y1)}) → ({_fmt(ay_x)}, {_fmt(y0)})",
        "origin": f"({_fmt(geo.to_svg_x(0.0 if a <= 0 <= b else a))}, {_fmt(ax_y)})",
    }

    # —— 尖点：不可导、无切线，坐标单独给出（secants 的公共端点）——
    if sharp is not None:
        sx0 = float(sharp)
        geo.sharp = {
            "x": sx0, "y": f(sx0),
            "svg_x": geo.to_svg_x(sx0), "svg_y": geo.to_svg_y(f(sx0)),
        }

    # —— 切点与切线（points 支持多个切点；point 是单点简写）——
    if points is not None:
        point_list = [float(p) for p in points]
    elif point is not None:
        point_list = [float(point)]
    else:
        point_list = []

    geo.points = []
    for px in point_list:
        if not a <= px <= b:
            raise ValueError(f"切点 x={px} 不在定义域 [{a}, {b}] 内")
        py = f(px)
        sx, sy = geo.to_svg_x(px), geo.to_svg_y(py)
        slope = derivative_at(f, px)
        # 切线方向换算到屏幕坐标：dy_screen = -slope * (scale_y / scale_x) * dx_screen
        k = -slope * (geo.scale_y / geo.scale_x) if geo.scale_x else 0.0
        far = (x0 + x1) * 2.0
        seg = _clip_line_to_rect((sx - far, sy - k * far), (sx + far, sy + k * far),
                                 (x0, y0, x1, y1))
        geo.points.append({
            "x": px, "y": py, "svg_x": sx, "svg_y": sy, "slope": slope,
            "slope_screen": k,
            "tangent_points": (f"({_fmt(seg[0][0])}, {_fmt(seg[0][1])}) → "
                               f"({_fmt(seg[1][0])}, {_fmt(seg[1][1])})") if seg else None,
        })
        if not seg:
            geo.notes.append(f"切点 x={px} 的切线裁剪后为空，检查斜率或绘图区")

    # 向后兼容：point / tangent 指向第一个切点
    if geo.points:
        first = geo.points[0]
        geo.point = {"x": first["x"], "y": first["y"],
                     "svg_x": first["svg_x"], "svg_y": first["svg_y"],
                     "slope": first["slope"]}
        geo.tangent = {
            "slope_math": first["slope"],
            "slope_screen": first["slope_screen"],
            "points": first["tangent_points"],
        }

    # —— 割线：公共端点 = 第一个切点（或尖点），端点都取自同一条采样曲线 ——
    if secants:
        anchor = geo.points[0] if geo.points else (geo.sharp if geo.sharp else None)
        if anchor:
            px, py = anchor["x"], anchor["y"]
            sx, sy = anchor["svg_x"], anchor["svg_y"]
            for qx in secants:
                qx = float(qx)
                if not a <= qx <= b:
                    geo.notes.append(f"割线端点 Q(x={qx}) 不在定义域内，已跳过")
                    continue
                qy = f(qx)
                geo.secants.append({
                    "x": qx, "y": qy,
                    "svg_x": geo.to_svg_x(qx), "svg_y": geo.to_svg_y(qy),
                    "segment": (f"({_fmt(geo.to_svg_x(qx))}, {_fmt(geo.to_svg_y(qy))}) → "
                                f"({_fmt(sx)}, {_fmt(sy)})"),
                    "secant_slope": (qy - py) / (qx - px) if abs(qx - px) > 1e-12 else None,
                })
        else:
            geo.notes.append("给了割线但没给切点 point 或尖点 sharp，割线需要公共端点，已忽略")

    return geo


# ===========================================================================
# 三点五、标注位置避让
# ===========================================================================
# 为什么需要它（实测 2026-10-08）：`none` 跑出来的图，曲线/切线/坐标轴都是对的，
# 但标注重叠严重——`Q4` 和 `P` 挤在一起、`Q3` 压在曲线和割线上。
# 而 `svg_quality_checker` **不检查同一模块内元素之间的重叠**，所以它报“错误 0”。
#
# 这类“摆放”工作看起来像需要推理，其实**输入是完备的**：曲线、切线、割线、
# 坐标轴的坐标都在本模块里，标注指向哪个点也是给定的。剩下的就是几何避让，
# 属于确定性计算。既然它难住了关推理的模型，就该由代码做。

#: 候选方位。**顺序即优先级**——前面的更符合标注习惯（点在右上/右侧是惯例）。
_LABEL_DIRS: Tuple[str, ...] = (
    "up-right", "right", "up", "down-right", "down", "left", "up-left", "down-left",
)
#: 避让时的搜索距离（依次尝试）。第一步就失败时拉远再试。
_LABEL_GAPS: Tuple[float, ...] = (1.0, 1.8, 2.8)


@dataclass
class LabelPlacement:
    """一个标注的推荐位置。``x``/``y`` 是 SVG 基线坐标，直接用作 ``<text>``。"""
    text: str
    x: float
    y: float
    side: str = ""
    crowded: bool = False        # 所有候选都避不开时置位，调用方可据此降级


def _seg_hits_box(p, q, box) -> bool:
    """线段是否与矩形相交（含端点在内）。直接复用裁剪器：裁剪结果非空即相交。"""
    return _clip_line_to_rect(p, q, box) is not None


def _label_box(x: float, y: float, w: float, size: float):
    """基线坐标 → 包围盒。上下界用 checker 的公式（−0.85×size / +0.35×size）。"""
    return (x, y - 0.85 * size, x + w, y + 0.35 * size)


def _candidate_baseline(ax: float, ay: float, w: float, size: float,
                        direction: str, gap: float):
    """相对锚点 ``(ax, ay)`` 按方位算出候选基线坐标。"""
    if direction == "right":
        return (ax + gap, ay + 0.35 * size)
    if direction == "left":
        return (ax - gap - w, ay + 0.35 * size)
    if direction == "up":
        return (ax - w / 2.0, ay - gap)
    if direction == "down":
        return (ax - w / 2.0, ay + gap + 0.7 * size)
    if direction == "up-right":
        return (ax + gap, ay - gap)
    if direction == "up-left":
        return (ax - gap - w, ay - gap)
    if direction == "down-right":
        return (ax + gap, ay + gap + 0.7 * size)
    return (ax - gap - w, ay + gap + 0.7 * size)


def plot_obstacles(geo: PlotGeometry) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """把曲线、切线、割线、坐标轴拆成线段列表，作为标注的避让对象。"""
    segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    pts = geo.curve_points
    for i in range(len(pts) - 1):
        segs.append((pts[i], pts[i + 1]))

    def _pair(text: str):
        m = re.findall(r"\(([-\d.]+), ([-\d.]+)\)", text or "")
        return [((float(a), float(b)), (float(c), float(d))) for (a, b), (c, d) in
                zip(m, m[1:])]

    for p in geo.points:
        if p.get("tangent_points"):
            segs.extend(_pair(p["tangent_points"]))
    for s in geo.secants:
        segs.extend(_pair(s.get("segment", "")))
    for key in ("x_axis", "y_axis"):
        segs.extend(_pair(geo.axes.get(key, "")))
    return segs


def suggest_labels(
    geo: PlotGeometry,
    specs: Optional[Sequence[Tuple[str, Tuple[float, float]]]] = None,
    *,
    size: float = 18.0,
    gap: float = 10.0,
) -> List[LabelPlacement]:
    """给一组标注算出互不打架、不压曲线的位置。

    ``specs`` 是 ``[(标注文字, 锚点 SVG 坐标)]``；不传就自动用经典组合
    （P + Q1..Qn），因为“割线趋近切线”这类图的标注集是固定的。

    算法：每个标注按 8 个方位 × 3 个距离枚候选，逐个打分
    （压线段 / 压已放置标注 / 出绘图区 各记重罚，方位优先级做弱偏好），
    取最优。全部候选都不过时取最不差的并把 ``crowded`` 置位——
    宁可告诉调用方“这里挤”，也不要默默压上去。
    """
    if specs is None:
        specs = []
        for i, p in enumerate(geo.points, 1):
            label = "P" if len(geo.points) == 1 else f"P{i}"
            specs.append((label, (p["svg_x"], p["svg_y"])))
        if geo.sharp:
            specs.append(("尖点", (geo.sharp["svg_x"], geo.sharp["svg_y"])))
        for i, s in enumerate(geo.secants, 1):
            specs.append((f"Q{i}", (s["svg_x"], s["svg_y"])))
    if not specs:
        return []

    obstacles = plot_obstacles(geo)
    x0, y0, x1, y1 = geo.area
    placed: List[Tuple[float, float, float, float]] = []
    out: List[LabelPlacement] = []

    for text, (ax, ay) in specs:
        w = _text_width_svg(text, size)
        best = None
        for dir_rank, direction in enumerate(_LABEL_DIRS):
            for step, mult in enumerate(_LABEL_GAPS):
                bx, by = _candidate_baseline(ax, ay, w, size, direction, gap * mult)
                box = _label_box(bx, by, w, size)
                pen = dir_rank * 1.0 + step * 6.0          # 方位/距离的弱偏好
                if box[0] < x0 or box[1] < y0 or box[2] > x1 or box[3] > y1:
                    pen += 400.0                            # 出绘图区
                for p, q in obstacles:
                    if _seg_hits_box(p, q, box):
                        pen += 120.0                        # 压线（曲线/切线/割线/轴）
                for b in placed:
                    ow = min(box[2], b[2]) - max(box[0], b[0])
                    oh = min(box[3], b[3]) - max(box[1], b[1])
                    if ow > 0 and oh > 0:
                        pen += 200.0                        # 压其它标注
                if best is None or pen < best[0]:
                    best = (pen, bx, by, direction)
        pen, bx, by, direction = best
        placed.append(_label_box(bx, by, w, size))
        out.append(LabelPlacement(text=text, x=bx, y=by, side=direction,
                                  crowded=pen >= 120.0))
    return out


def _text_width_svg(text: str, size: float) -> float:
    """估文字宽度（SVG px）。CJK ≈ 1em，其余 ≈ 0.55em。与 bounds_fix 同一口径。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff"
              or "\u3000" <= c <= "\u303f" or "\uff00" <= c <= "\uffef")
    return cjk * size + (len(text) - cjk) * size * 0.55


# ===========================================================================
# 四、给模型的文本块
# ===========================================================================
def format_geometry(geo: PlotGeometry) -> str:
    """把几何整理成一段**直接可用**的文本，塞进 P3 的页面提示词。

    措辞上刻意强调"已由代码算好、直接用、不要自己再算"——实测模型会很自然地
    想复核一遍，而那正是我们要消灭的 74% 推理。
    """
    a, b = geo.domain
    lo, hi = geo.y_range
    x0, y0, x1, y1 = geo.area
    L = [
        "【函数图像几何 —— 已由代码算好，直接使用，不要自己重算】",
        f"f(x) = {geo.expr}   定义域 x ∈ [{_fmt(a)}, {_fmt(b)}]",
        f"绘图区 (SVG 屏幕坐标) ({_fmt(x0)}, {_fmt(y0)}) → ({_fmt(x1)}, {_fmt(y1)})",
        f"映射：x 每单位 {geo.scale_x:.2f}px，y 每单位 {geo.scale_y:.2f}px；"
        f"y 轴显示范围 [{_fmt(lo)}, {_fmt(hi)}]",
        "",
        f"曲线 path（{geo.samples} 点采样，直接用作 <path d=\"…\">）：",
        geo.curve_d,
        "",
        f"x 轴：{geo.axes.get('x_axis')}",
        f"y 轴：{geo.axes.get('y_axis')}",
        f"原点：{geo.axes.get('origin')}",
    ]
    if geo.points:
        for i, p in enumerate(geo.points, 1):
            label = "P" if len(geo.points) == 1 else f"P{i}"
            L += [
                "",
                f"切点 {label}：x={_fmt(p['x'])} → SVG ({_fmt(p['svg_x'])}, {_fmt(p['svg_y'])})"
                f"   导数 f'(x)={_fmt(p['slope'])}",
            ]
            if p.get("tangent_points"):
                L.append(f"切线（{label} 处，已裁到绘图区边界，直接连线）：{p['tangent_points']}")
    if geo.sharp:
        L += [
            "",
            f"尖点：x={_fmt(geo.sharp['x'])} → SVG ({_fmt(geo.sharp['svg_x'])}, {_fmt(geo.sharp['svg_y'])})"
            "（此处不可导、无切线，只画左右割线）",
        ]
    if geo.secants:
        anchor_desc = "第一个切点" if geo.points else ("尖点" if geo.sharp else "P")
        L += ["", f"割线端点（每条割线都连到{anchor_desc}）："]
        for i, s in enumerate(geo.secants, 1):
            slope = s.get("secant_slope")
            L.append(f"  Q{i}: x={_fmt(s['x'])} → SVG ({_fmt(s['svg_x'])}, {_fmt(s['svg_y'])})"
                     + (f"   割线斜率 {slope:.3f}" if slope is not None else ""))
    if geo.point:
        L += ["",
              "说明：曲线上所有点、切点、割线端点都取自**同一条**采样曲线，"
              "所以割线必然过 P、切线必然与曲线相切。请只负责配色、线型、标签与排版。"]

    # —— 标注推荐位置：避让曲线/切线/割线/轴与彼此 ——
    # 实测（2026-10-08，exec=none）：不给人话，模型会把 Q4 叠在 P 上、Q3 压在割线上；
    # 而 checker 不检查同模块内元素重叠，于是报“错误 0”却难看。这里直接把坐标算好。
    marks = suggest_labels(geo)
    if marks:
        L += ["", "标注推荐位置（已代码避让曲线/切线/割线/轴与其它标注，直接用作 <text x= y=>）："]
        for mk in marks:
            flag = "  ⚠️ 此处较挤，可自行微调" if mk.crowded else ""
            L.append(f"  {mk.text:>4}：x={_fmt(mk.x)}  y={_fmt(mk.y)}   放在锚点{mk.side}"
                     f"（font-size 建议 18）{flag}")
    if geo.notes:
        L += ["", "提示：" + "；".join(geo.notes)]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 圆锥曲线（原来只能画 y=f(x) 函数图像，而椭圆/双曲线不是函数）
# ---------------------------------------------------------------------------
@dataclass
class ConicGeometry:
    """圆锥曲线几何：参数方程曲线 + 焦点 + 顶点（全部 SVG 坐标）。"""
    ctype: str                                    # ellipse / hyperbola / parabola
    a: float
    b: float
    p: float
    center_math: Tuple[float, float]
    view: Tuple[float, float, float, float]        # xmin, xmax, ymin, ymax
    area: Tuple[float, float, float, float]        # SVG 绘图区
    scale: float                                   # 等比例（椭圆不变形）
    curves: List[str] = field(default_factory=list)
    curve_points: List[List[Tuple[float, float]]] = field(default_factory=list)
    foci: List[Tuple[float, float]] = field(default_factory=list)
    vertices: List[Tuple[float, float]] = field(default_factory=list)
    axes: Dict[str, str] = field(default_factory=dict)
    points: List[Dict[str, object]] = field(default_factory=list)    # 曲线上的点 {name,t,math,svg}
    chords: List[Dict[str, object]] = field(default_factory=list)    # 弦 {name,seg}
    tangents: List[Dict[str, object]] = field(default_factory=list)  # 点处切线 {name,seg}
    free_points: List[Dict[str, object]] = field(default_factory=list)   # 自由点 {name,math,svg}
    lines: List[Dict[str, object]] = field(default_factory=list)         # 线 {name,seg}
    intersections: List[Dict[str, object]] = field(default_factory=list)  # 交点 {name,svg}
    ext_tangents: List[Dict[str, object]] = field(default_factory=list)   # 过外点切线 {name,touch,seg}
    notes: List[str] = field(default_factory=list)

    def to_svg(self, x: float, y: float) -> Tuple[float, float]:
        """数学坐标 → SVG 坐标（等比例，数学 y 向上）。"""
        xmin, xmax, ymin, ymax = self.view
        vcx, vcy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        x0, y0, x1, y1 = self.area
        acx, acy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        return (acx + (x - vcx) * self.scale, acy - (y - vcy) * self.scale)


def _conic_tangent_params(ct, aa, bb, pp, X, Y):
    """过外点 P=(X,Y)（相对中心/顶点）的切线切点参数列表 ``[(t, branch), ...]``。

    circle/ellipse 解析（``A·cosθ + B·sinθ = 1``）；hyperbola 令 ``u=e^t`` 解二次；
    parabola 直接解二次。返回空 = 点在该曲线内、无切线。
    """
    if ct in ("circle", "ellipse"):
        bb2 = aa if ct == "circle" else bb
        a_, b_ = X / aa, Y / bb2
        r = math.hypot(a_, b_)
        if r < 1 - 1e-9:
            return []
        if abs(r - 1) < 1e-9:
            return [(math.atan2(b_, a_), 1)]
        phi = math.atan2(b_, a_)
        dd = math.acos(min(1.0, 1.0 / r))
        return [(phi + dd, 1), (phi - dd, 1)]
    if ct == "hyperbola":
        out = []
        for branch in (1, -1):
            p_, q_ = branch * X / aa, Y / bb
            # (p_−q_)u² − 2u + (p_+q_) = 0，u = e^t > 0
            a2, c2 = p_ - q_, p_ + q_
            if abs(a2) < 1e-12:
                u = c2 / 2.0
                if u > 1e-9:
                    out.append((math.log(u), branch))
                continue
            disc = 4.0 - 4.0 * a2 * c2
            if disc < -1e-12:
                continue
            disc = max(disc, 0.0)
            for u in ((2.0 + math.sqrt(disc)) / (2.0 * a2),
                      (2.0 - math.sqrt(disc)) / (2.0 * a2)):
                if u > 1e-9:
                    out.append((math.log(u), branch))
        return out
    # parabola（右开口 y²=2p(x−h)）：(p/2)u² − Y u + X = 0
    a2 = pp / 2.0
    disc = Y * Y - 4.0 * a2 * X
    if disc < -1e-12:
        return []
    disc = max(disc, 0.0)
    return [((Y + math.sqrt(disc)) / (2.0 * a2), 1),
            ((Y - math.sqrt(disc)) / (2.0 * a2), 1)]


def conic_geometry(
    ctype: str, *, a: float = 0.0, b: float = 0.0, p: float = 0.0,
    center: Sequence[float] = (0.0, 0.0),
    view: Sequence[float], area: Sequence[float],
    samples: int = 65,
    points: Sequence[dict] = (),
    chords: Sequence[Sequence[str]] = (),
    tangents: Sequence[str] = (),
    free_points: Sequence[dict] = (),
    lines: Sequence[dict] = (),
    intersections: Sequence[dict] = (),
    ext_tangents: Sequence[dict] = (),
) -> ConicGeometry:
    """算圆锥曲线的参数方程 path + 焦点 + 顶点（等比例，不变形）。

    为什么需要它（实测 2026-10-11 圆锥曲线专题）：`plot_math` 原来只能画 y=f(x)
    函数图像，而椭圆、双曲线**不是函数**（一个 x 对应两个 y）——系统根本没有画
    圆锥曲线的能力，模型只能手画，画出来是斜直线或任意贝塞尔，完全不像。

    ``ctype`` = ellipse / hyperbola / parabola；``a``/``b`` = 椭圆半轴或双曲线实/虚半轴、
    ``p`` = 抛物线焦准距；``center`` = 中心（数学坐标）；``view`` = 数学视窗
    ``[xmin,xmax,ymin,ymax]``；``area`` = SVG 绘图区 ``[x左,y上,x右,y下]``。
    """
    ct = str(ctype).strip().lower()
    if ct not in ("circle", "ellipse", "hyperbola", "parabola"):
        raise ValueError(f"type={ctype!r} 不是支持的曲线（circle/ellipse/hyperbola/parabola）")
    h, k = float(center[0]), float(center[1])
    if not (isinstance(view, (list, tuple)) and len(view) == 4):
        raise ValueError("view 应为 [xmin, xmax, ymin, ymax]")
    xmin, xmax, ymin, ymax = (float(v) for v in view)
    if not (xmax > xmin and ymax > ymin):
        raise ValueError(f"view 必须递增：{view}")
    if not (isinstance(area, (list, tuple)) and len(area) == 4):
        raise ValueError("area 应为 [x左, y上, x右, y下]")
    x0, y0, x1, y1 = (float(v) for v in area)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"area 必须是左上-右下矩形：{area}")
    n = max(17, int(samples))
    # 等比例缩放：否则椭圆会被拉成不同形状
    scale = min((x1 - x0) / (xmax - xmin), (y1 - y0) / (ymax - ymin))
    geo = ConicGeometry(
        ctype=ct, a=float(a), b=float(b), p=float(p),
        center_math=(h, k), view=(xmin, xmax, ymin, ymax),
        area=(x0, y0, x1, y1), scale=scale,
    )

    def _path(pts_math):
        svg = [geo.to_svg(x, y) for x, y in pts_math]
        geo.curve_points.append(svg)
        return "M" + " L".join(f"{_fmt(sx)} {_fmt(sy)}" for sx, sy in svg)

    if ct in ("circle", "ellipse"):
        aa = float(a)
        bb = aa if ct == "circle" else float(b)
        if not (aa > 0 and bb > 0):
            raise ValueError("圆需要 r>0（用 a 填半径）；椭圆需要 a>0 且 b>0")
        ts = [2 * math.pi * i / (n - 1) for i in range(n)]
        geo.curves.append(_path([(h + aa * math.cos(t), k + bb * math.sin(t)) for t in ts]))
        c = math.sqrt(max(aa * aa - bb * bb, 0.0))
        if ct == "circle":
            geo.foci = [geo.to_svg(h, k)]        # 圆心
        else:
            geo.foci = [geo.to_svg(h - c, k), geo.to_svg(h + c, k)]
        geo.vertices = [geo.to_svg(h - aa, k), geo.to_svg(h + aa, k),
                        geo.to_svg(h, k - bb), geo.to_svg(h, k + bb)]
    elif ct == "hyperbola":
        aa, bb = float(a), float(b)
        if not (aa > 0 and bb > 0):
            raise ValueError("双曲线需要 a>0（实半轴）且 b>0（虚半轴）")
        ymax_rel = max(abs(ymin - k), abs(ymax - k))
        T = math.asinh(ymax_rel / bb) if ymax_rel > 0 else 1.0
        half = max(9, n // 2)
        for sign in (1.0, -1.0):
            ts = [-T + 2 * T * i / (half - 1) for i in range(half)]
            geo.curves.append(_path([
                (h + sign * aa * math.cosh(t), k + bb * math.sinh(t)) for t in ts]))
        c = math.sqrt(aa * aa + bb * bb)
        geo.foci = [geo.to_svg(h - c, k), geo.to_svg(h + c, k)]
        geo.vertices = [geo.to_svg(h - aa, k), geo.to_svg(h + aa, k)]
    else:  # parabola（右开口 y² = 2p(x−h)）
        pp = float(p)
        if not pp > 0:
            raise ValueError("抛物线需要 p>0（焦准距，右开口）")
        ys = [ymin - k + (ymax - ymin) * i / (n - 1) for i in range(n)]
        geo.curves.append(_path([(h + (yy * yy) / (2 * pp), k + yy) for yy in ys]))
        geo.foci = [geo.to_svg(h + pp / 2.0, k)]
        geo.vertices = [geo.to_svg(h, k)]

    # —— 曲线上的点（参数 t）、弦、点处切线（实测模型自画这些会飘离曲线）——
    def _pt_at(t_val: float, branch: int = 1):
        if ct in ("circle", "ellipse"):
            th = math.radians(t_val)
            bb = float(a) if ct == "circle" else float(b)
            return (h + float(a) * math.cos(th), k + bb * math.sin(th))
        if ct == "hyperbola":
            return (h + branch * float(a) * math.cosh(t_val),
                    k + float(b) * math.sinh(t_val))
        return (h + float(p) * t_val * t_val / 2.0, k + float(p) * t_val)

    def _slope_at(t_val: float, branch: int = 1):
        """该点数学切线斜率；返回 None 表示竖直切线。"""
        aa, pp = float(a), float(p)
        bb = aa if ct == "circle" else float(b)
        if ct in ("circle", "ellipse"):
            th = math.radians(t_val)
            den = aa * math.sin(th)
            return None if abs(den) < 1e-9 else -bb * math.cos(th) / den
        if ct == "hyperbola":
            den = branch * aa * math.sinh(t_val)
            return None if abs(den) < 1e-9 else bb * math.cosh(t_val) / den
        return None if abs(t_val) < 1e-9 else 1.0 / t_val

    by_name: Dict[str, Dict[str, object]] = {}
    for spec in points:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "")
        t_val = float(spec.get("t") or 0)
        branch = int(spec.get("branch") or 1)
        mx, my = _pt_at(t_val, branch)
        sx, sy = geo.to_svg(mx, my)
        rec: Dict[str, object] = {"name": name, "t": t_val, "branch": branch,
                                  "math": (mx, my), "svg": (sx, sy)}
        geo.points.append(rec)
        if name:
            by_name[name] = rec
    for pair in chords:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            continue
        p1, p2 = by_name.get(str(pair[0])), by_name.get(str(pair[1]))
        if p1 and p2:
            geo.chords.append({"name": f"{p1['name']}{p2['name']}",
                               "seg": (p1["svg"], p2["svg"])})
        else:
            geo.notes.append(f"弦 {pair} 引用了未定义的点，已跳过")
    far = (x1 - x0) + (y1 - y0)
    for tname in tangents:
        prec = by_name.get(str(tname))
        if not prec:
            geo.notes.append(f"切线引用了未定义的点 {tname}，已跳过")
            continue
        m = _slope_at(prec["t"], prec["branch"])
        sx, sy = prec["svg"]
        if m is None:                      # 竖直切线
            seg = _clip_line_to_rect((sx, sy - far), (sx, sy + far), (x0, y0, x1, y1))
        else:                              # 等比例下屏幕斜率 = −数学斜率
            k_screen = -m
            seg = _clip_line_to_rect((sx - far, sy - k_screen * far),
                                     (sx + far, sy + k_screen * far), (x0, y0, x1, y1))
        geo.tangents.append({"name": prec["name"], "seg": seg})

    # —— 自由点 / 线 / 交点 / 过外点的切线 ——
    def _line_hit(seg1, seg2):
        (ax, ay), (bx, by) = seg1
        (cx, cy), (dx, dy) = seg2
        den = (ax - bx) * (cy - dy) - (ay - by) * (cx - dx)
        if abs(den) < 1e-9:
            return None
        return (((ax * by - ay * bx) * (cx - dx) - (ax - bx) * (cx * dy - cy * dx)) / den,
                ((ax * by - ay * bx) * (cy - dy) - (ay - by) * (cx * dy - cy * dx)) / den)

    for spec in free_points:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "")
        at = spec.get("at") or spec.get("xy")
        if not (isinstance(at, (list, tuple)) and len(at) == 2):
            geo.notes.append(f"自由点 {name} 缺 at=[x,y]，已跳过")
            continue
        mx, my = float(at[0]), float(at[1])
        rec: Dict[str, object] = {"name": name, "math": (mx, my),
                                  "svg": geo.to_svg(mx, my)}
        geo.free_points.append(rec)
        if name:
            by_name[name] = rec

    line_reg: Dict[str, tuple] = {}
    for spec in lines:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "")
        through = spec.get("through") or []
        if not (isinstance(through, (list, tuple)) and len(through) == 2):
            geo.notes.append(f"线 {name} 的 through 应为 [点名, 点名]，已跳过")
            continue
        p1, p2 = by_name.get(str(through[0])), by_name.get(str(through[1]))
        if not (p1 and p2):
            geo.notes.append(f"线 {name} 引用了未定义的点，已跳过")
            continue
        geo.lines.append({"name": name, "seg": (p1["svg"], p2["svg"]),
                          "arrow": bool(spec.get("arrow"))})
        line_reg[name] = (p1["svg"], p2["svg"])

    for spec in intersections:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "")
        of = spec.get("of") or []
        if not (isinstance(of, (list, tuple)) and len(of) == 2):
            continue
        l1, l2 = line_reg.get(str(of[0])), line_reg.get(str(of[1]))
        if not (l1 and l2):
            geo.notes.append(f"交点 {name} 引用了未定义的线，已跳过")
            continue
        hit = _line_hit(l1, l2)
        if hit is None:
            geo.notes.append(f"交点 {name}：两线平行，无交点")
            continue
        rec = {"name": name, "svg": hit}
        geo.intersections.append(rec)
        if name:
            by_name[name] = rec

    for spec in ext_tangents:
        if not isinstance(spec, dict):
            continue
        pname = str(spec.get("point") or "")
        prec = by_name.get(pname)
        if not prec or "math" not in prec:
            geo.notes.append(f"外点切线引用了未定义的点 {pname}，已跳过")
            continue
        X, Y = prec["math"][0] - h, prec["math"][1] - k
        params = _conic_tangent_params(ct, float(a), float(b), float(p), X, Y)
        if not params:
            geo.notes.append(f"外点 {pname} 在该曲线内，无切线")
            continue
        names = list(spec.get("names") or [])
        touch_names = list(spec.get("touch") or [])
        psv = prec["svg"]
        for i, (tv, br) in enumerate(params):
            tx, ty = _pt_at(tv, br)
            tsx, tsy = geo.to_svg(tx, ty)
            dx_, dy_ = tsx - psv[0], tsy - psv[1]
            n_ = math.hypot(dx_, dy_) or 1.0
            seg = _clip_line_to_rect((tsx - dx_ / n_ * far, tsy - dy_ / n_ * far),
                                     (tsx + dx_ / n_ * far, tsy + dy_ / n_ * far),
                                     (x0, y0, x1, y1))
            geo.ext_tangents.append({
                "name": names[i] if i < len(names) else f"{pname}切线{i + 1}",
                "touch_name": touch_names[i] if i < len(touch_names) else "",
                "touch": (tsx, tsy), "seg": seg})

    ox, oy = geo.to_svg(0.0, 0.0)
    geo.axes = {
        "x_axis": f"({_fmt(x0)}, {_fmt(oy)}) → ({_fmt(x1)}, {_fmt(oy)})",
        "y_axis": f"({_fmt(ox)}, {_fmt(y1)}) → ({_fmt(ox)}, {_fmt(y0)})",
    }
    return geo


def format_conic(geo: ConicGeometry) -> str:
    """把圆锥曲线几何整理成给模型的文本块（直接可用，不要自己重算）。"""
    names = {"circle": "圆", "ellipse": "椭圆", "hyperbola": "双曲线", "parabola": "抛物线"}
    if geo.ctype == "parabola":
        param = f"p={_fmt(geo.p)}（焦准距，右开口 y²=2p(x−h)）"
    elif geo.ctype == "circle":
        param = f"r={_fmt(geo.a)}（半径）"
    else:
        param = f"a={_fmt(geo.a)}　b={_fmt(geo.b)}（数学单位）"
    L = [
        "【圆锥曲线几何 —— 已由代码算好，直接使用，不要自己重算】",
        f"类型：{names.get(geo.ctype, geo.ctype)}　{param}　中心 ({_fmt(geo.center_math[0])}, "
        f"{_fmt(geo.center_math[1])})",
        f"视窗：x∈[{_fmt(geo.view[0])}, {_fmt(geo.view[1])}]　y∈[{_fmt(geo.view[2])}, "
        f"{_fmt(geo.view[3])}]　→ 绘图区 ({_fmt(geo.area[0])}, {_fmt(geo.area[1])})-"
        f"({_fmt(geo.area[2])}, {_fmt(geo.area[3])})，等比例 scale={geo.scale:.3f}",
        "",
        f"曲线 path（{len(geo.curves)} 段，直接用作 <path d=\"…\" fill=\"none\">）：",
    ]
    for i, d in enumerate(geo.curves, 1):
        L.append(f"  段{i}: {d}")
    if geo.foci:
        L.append("")
        L.append("焦点（在曲线内/上，用 <circle> 标出并标注 F）：")
        for i, (fx, fy) in enumerate(geo.foci, 1):
            L.append(f"  F{i}: ({_fmt(fx)}, {_fmt(fy)})")
    if geo.vertices:
        L.append("顶点：")
        for i, (vx, vy) in enumerate(geo.vertices, 1):
            L.append(f"  A{i}: ({_fmt(vx)}, {_fmt(vy)})")
    if geo.points:
        L.append("")
        L.append("曲线上的点（已精确落在曲线上，直接用作 <circle> 并标注文字）：")
        for p in geo.points:
            px_, py_ = p["svg"]
            L.append(f"  {p['name']}（t={_fmt(p['t'])}）：({_fmt(px_)}, {_fmt(py_)})")
    if geo.chords:
        L.append("弦（两端点精确在曲线上，直接用作 <line>）：")
        for c in geo.chords:
            (cax, cay), (cbx, cby) = c["seg"]
            L.append(f"  {c['name']}：({_fmt(cax)}, {_fmt(cay)}) → ({_fmt(cbx)}, {_fmt(cby)})")
    if geo.tangents:
        L.append("切线（在点处、已裁到绘图区，直接用作 <line>）：")
        for tg in geo.tangents:
            if tg["seg"]:
                (tax, tay), (tbx, tby) = tg["seg"]
                L.append(f"  {tg['name']} 处切线：({_fmt(tax)}, {_fmt(tay)}) → "
                         f"({_fmt(tbx)}, {_fmt(tby)})")
            else:
                L.append(f"  {tg['name']} 处切线：代码未能裁出线段（检查视窗）")
    if geo.free_points:
        L.append("")
        L.append("自由点（已算好 SVG 坐标，直接用作 <circle> 并标注）：")
        for p in geo.free_points:
            fx, fy = p["svg"]
            L.append(f"  {p['name']}：({_fmt(fx)}, {_fmt(fy)})")
    if geo.lines:
        L.append("线段（两端点，直接用作 <line>；arrow=true 的末端加箭头）：")
        for ln in geo.lines:
            (lax, lay), (lbx, lby) = ln["seg"]
            tag = "　[向量·末端加箭头]" if ln.get("arrow") else ""
            L.append(f"  {ln['name']}：({_fmt(lax)}, {_fmt(lay)}) → ({_fmt(lbx)}, {_fmt(lby)}){tag}")
    if geo.intersections:
        L.append("交点（两线交点，已算好，直接用作 <circle> 并标注）：")
        for it in geo.intersections:
            ix, iy = it["svg"]
            L.append(f"  {it['name']}：({_fmt(ix)}, {_fmt(iy)})")
    if geo.ext_tangents:
        L.append("过外点的切线（切点 + 切线都已算好，直接照抄）：")
        for et in geo.ext_tangents:
            ttx, tty = et["touch"]
            label = str(et["name"])
            if et.get("touch_name"):
                label += f"（切点 {et['touch_name']}）"
            if et["seg"]:
                (eax, eay), (ebx, eby) = et["seg"]
                L.append(f"  {label}：切点 ({_fmt(ttx)}, {_fmt(tty)})，"
                         f"切线 ({_fmt(eax)}, {_fmt(eay)}) → ({_fmt(ebx)}, {_fmt(eby)})")
            else:
                L.append(f"  {label}：切点 ({_fmt(ttx)}, {_fmt(tty)})，切线裁剪失败")
    L += [
        "",
        f"x 轴：{geo.axes.get('x_axis')}",
        f"y 轴：{geo.axes.get('y_axis')}",
        "说明：曲线已按等比例算好（椭圆不会被拉变形），坐标直接照抄；你只负责配色、线型、标注文字与排版。",
    ]
    if geo.notes:
        L.append("提示：" + "；".join(geo.notes))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 立体几何（3D → 2D 正交投影；高中立体几何的顶点/棱/面）
# ---------------------------------------------------------------------------
@dataclass
class Solid3D:
    """3D 顶点/棱/面投影后的 2D 坐标。"""
    azimuth: float
    elevation: float
    scale: float
    vertices: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    screens: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    depths: Dict[str, float] = field(default_factory=dict)
    edges: List[Dict[str, object]] = field(default_factory=list)
    faces: List[Dict[str, object]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


_SOLID_PRESETS = ("cube", "tetrahedron", "octahedron", "prism", "pyramid")


def _solid_preset(preset: str, *, n: int = 4, radius: float = 2.0,
                  height: float = 3.0, size: float = 2.0):
    """生成常见立体的 ``(vertices, edges, faces)``。

    ``n`` 只对 prism/pyramid 有效（底面边数）；顶点名：正 n 棱柱/棱锥下底 A1..An、上底 B1..Bn（棱锥顶点 P）。
    """
    p = str(preset).strip().lower()
    if p == "cube":
        s = float(size) / 2.0
        v = {"A": [-s, -s, -s], "B": [s, -s, -s], "C": [s, s, -s], "D": [-s, s, -s],
             "E": [-s, -s, s], "F": [s, -s, s], "G": [s, s, s], "H": [-s, s, s]}
        e = [["A", "B"], ["B", "C"], ["C", "D"], ["D", "A"],
             ["E", "F"], ["F", "G"], ["G", "H"], ["H", "E"],
             ["A", "E"], ["B", "F"], ["C", "G"], ["D", "H"]]
        f = [{"of": ["A", "B", "C", "D"]}, {"of": ["E", "F", "G", "H"]},
             {"of": ["A", "B", "F", "E"]}, {"of": ["C", "D", "H", "G"]},
             {"of": ["B", "C", "G", "F"]}, {"of": ["A", "D", "H", "E"]}]
        return v, e, f
    if p == "tetrahedron":
        s = float(size) / 2.0
        v = {"A": [s, s, s], "B": [s, -s, -s], "C": [-s, s, -s], "D": [-s, -s, s]}
        e = [["A", "B"], ["A", "C"], ["A", "D"], ["B", "C"], ["B", "D"], ["C", "D"]]
        f = [{"of": ["A", "B", "C"]}, {"of": ["A", "B", "D"]},
             {"of": ["A", "C", "D"]}, {"of": ["B", "C", "D"]}]
        return v, e, f
    if p == "octahedron":
        s = float(size) / 2.0
        v = {"A": [s, 0, 0], "B": [-s, 0, 0], "C": [0, s, 0],
             "D": [0, -s, 0], "E": [0, 0, s], "F": [0, 0, -s]}
        e = [["A", "C"], ["A", "D"], ["A", "E"], ["A", "F"],
             ["B", "C"], ["B", "D"], ["B", "E"], ["B", "F"],
             ["C", "E"], ["C", "F"], ["D", "E"], ["D", "F"]]
        f = [{"of": ["A", "C", "E"]}, {"of": ["C", "B", "E"]},
             {"of": ["B", "D", "E"]}, {"of": ["D", "A", "E"]},
             {"of": ["A", "F", "C"]}, {"of": ["C", "F", "B"]},
             {"of": ["B", "F", "D"]}, {"of": ["D", "F", "A"]}]
        return v, e, f
    nn = max(3, int(n))
    r, hh = float(radius), float(height)
    lower, upper = {}, {}
    for i in range(nn):
        th = 2 * math.pi * i / nn + math.pi / nn
        x, y = r * math.cos(th), r * math.sin(th)
        lower[f"A{i + 1}"] = [x, y, -hh / 2.0]
        upper[f"B{i + 1}"] = [x, y, hh / 2.0]
    if p == "prism":
        v = {**lower, **upper}
        e = [[f"A{i + 1}", f"A{(i + 1) % nn + 1}"] for i in range(nn)]
        e += [[f"B{i + 1}", f"B{(i + 1) % nn + 1}"] for i in range(nn)]
        e += [[f"A{i + 1}", f"B{i + 1}"] for i in range(nn)]
        f = [{"of": [f"A{i + 1}" for i in range(nn)]},
             {"of": [f"B{i + 1}" for i in range(nn)]}]
        f += [{"of": [f"A{i + 1}", f"A{(i + 1) % nn + 1}",
                       f"B{(i + 1) % nn + 1}", f"B{i + 1}"]} for i in range(nn)]
        return v, e, f
    v = {**lower, "P": [0.0, 0.0, hh]}
    e = [[f"A{i + 1}", f"A{(i + 1) % nn + 1}"] for i in range(nn)]
    e += [[f"A{i + 1}", "P"] for i in range(nn)]
    f = [{"of": [f"A{i + 1}" for i in range(nn)]}]
    f += [{"of": [f"A{i + 1}", f"A{(i + 1) % nn + 1}", "P"]} for i in range(nn)]
    return v, e, f


def solid3d_geometry(
    vertices: Optional[Dict[str, Sequence[float]]] = None, *,
    edges: Sequence = (),
    faces: Sequence[dict] = (),
    preset: Optional[str] = None,
    n: int = 4, radius: float = 2.0, height: float = 3.0, size: float = 2.0,
    azimuth: float = 30.0, elevation: float = 20.0,
    projection: str = "orthographic", distance: float = 8.0,
    scale: float = 80.0, origin: Sequence[float] = (640.0, 400.0),
) -> Solid3D:
    """把 3D 顶点/棱/面投影成 2D SVG 坐标（正交投影，面按深度排序）。

    为什么需要它（高中立体几何）：平面内的圆锥曲线/函数图像已经能画，但
    棱柱、棱锥、正方体这类 3D 图形，模型手画很容易把“看得见/看不见的棱”
    画错、比例也不对。这里用正交投影把 3D 坐标算好。

    ``vertices`` = ``{{"A": [x,y,z], ...}}``；``edges`` = ``[["A","B"], ...]``；
    ``faces`` = ``[{{"of": ["A","B","C","D"], "fill": "#..."}}, ...]``（可选）；
    ``azimuth``/``elevation`` = 相机方位角/仰角（度）；``scale`` = px/单位；
    ``origin`` = 投影原点的 SVG 位置。
    """
    if preset:
        _pv, _pe, _pf = _solid_preset(str(preset), n=n, radius=radius,
                                      height=height, size=size)
        vertices = vertices or _pv
        edges = edges or _pe
        faces = faces or _pf
    if not isinstance(vertices, dict) or not vertices:
        raise ValueError("solid3d 需要非空 vertices（{点名: [x,y,z]}）或 preset")
    geo = Solid3D(azimuth=float(azimuth), elevation=float(elevation), scale=float(scale))
    az = math.radians(float(azimuth))
    el = math.radians(float(elevation))
    ox, oy = float(origin[0]), float(origin[1])
    sc = float(scale)
    proj_mode = str(projection).strip().lower()
    dist = float(distance)
    right = (math.cos(az), -math.sin(az), 0.0)
    up = (math.sin(az) * math.sin(el), math.cos(az) * math.sin(el), math.cos(el))
    fwd = (math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), -math.sin(el))

    for name, xyz in vertices.items():
        if not (isinstance(xyz, (list, tuple)) and len(xyz) == 3):
            geo.notes.append(f"顶点 {name} 应为 [x,y,z]，已跳过")
            continue
        x, y, z = (float(v) for v in xyz)
        sx_ = x * right[0] + y * right[1] + z * right[2]
        sy_ = x * up[0] + y * up[1] + z * up[2]
        dp_ = x * fwd[0] + y * fwd[1] + z * fwd[2]
        if proj_mode == "perspective":
            _den = dist - dp_
            factor = dist / _den if _den > 0.2 else 1.0
        else:
            factor = 1.0
        geo.vertices[str(name)] = (x, y, z)
        geo.screens[str(name)] = (ox + sx_ * sc * factor, oy - sy_ * sc * factor)
        geo.depths[str(name)] = dp_

    for pair in edges:
        if isinstance(pair, dict):
            of, arrow = pair.get("of") or [], bool(pair.get("arrow"))
        elif isinstance(pair, (list, tuple)) and len(pair) == 2:
            of, arrow = list(pair), False
        else:
            continue
        if len(of) != 2:
            continue
        n1, n2 = str(of[0]), str(of[1])
        if n1 in geo.screens and n2 in geo.screens:
            geo.edges.append({"name": f"{n1}{n2}", "ends": (n1, n2), "arrow": arrow,
                              "seg": (geo.screens[n1], geo.screens[n2]),
                              "depth": (geo.depths[n1] + geo.depths[n2]) / 2.0})
        else:
            geo.notes.append(f"棱 {n1}-{n2} 引用了未定义顶点，已跳过")
    for spec in faces:
        if not isinstance(spec, dict):
            continue
        of = spec.get("of") or []
        pts = [geo.screens.get(str(n)) for n in of]
        if len(of) < 3 or any(p is None for p in pts):
            geo.notes.append(f"面 {of} 顶点不足或未定义，已跳过")
            continue
        geo.faces.append({"of": [str(n) for n in of], "pts": pts,
                          "fill": str(spec.get("fill") or ""),
                          "depth": sum(geo.depths[str(n)] for n in of) / len(of)})
    # 面按深度排序：远的先画，近的盖住远的
    geo.faces.sort(key=lambda f: float(f["depth"]), reverse=True)

    # 棱的可见性：棱中点若落在某个“更近的面”的投影内 → 隐藏（虚线）
    def _pt_in_poly(px_, py_, poly):
        inside = False
        j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > py_) != (yj > py_)) and \
                    (px_ < (xj - xi) * (py_ - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    for e in geo.edges:
        (ex1, ey1), (ex2, ey2) = e["seg"]
        mx, my = (ex1 + ex2) / 2.0, (ey1 + ey2) / 2.0
        n1_, n2_ = e.get("ends", ("", ""))
        hidden = False
        for f in geo.faces:
            of = f["of"]
            if n1_ in of and n2_ in of:
                continue                      # 该面含这条棱，不算遮挡
            if f["depth"] < float(e["depth"]) - 1e-6 and _pt_in_poly(mx, my, f["pts"]):
                hidden = True
                break
        e["hidden"] = hidden
    return geo


def format_solid3d(geo: Solid3D) -> str:
    """把 3D 投影几何整理成给模型的文本块（直接可用，不要自己重算）。"""
    L = [
        "【立体几何（3D）—— 已由代码正交投影成 2D 坐标，直接使用，不要自己重算】",
        f"相机：方位角 {_fmt(geo.azimuth)}°　仰角 {_fmt(geo.elevation)}°　"
        f"缩放 {_fmt(geo.scale)}px/单位",
        "",
        "顶点（3D → SVG 2D，直接用作 <circle> 并标注）：",
    ]
    for name, (sx, sy) in geo.screens.items():
        L.append(f"  {name}：({_fmt(sx)}, {_fmt(sy)})")
    if geo.edges:
        L.append("")
        L.append("棱（直接用作 <line>；hidden=true 的用虚线描边、或调低不透明度）：")
        for e in geo.edges:
            (x1_, y1_), (x2_, y2_) = e["seg"]
            tag = "【看不见·虚线】" if e.get("hidden") else ""
            if e.get("arrow"):
                tag += "[向量·末端加箭头]"
            L.append(f"  {e['name']}{tag}：({_fmt(x1_)}, {_fmt(y1_)}) → ({_fmt(x2_)}, {_fmt(y2_)})")
    if geo.faces:
        L.append("")
        L.append("面（已按深度排序，远的先画，直接用作 <polygon points=\"…\">）：")
        for f in geo.faces:
            pts = " ".join(f"{_fmt(px_)},{_fmt(py_)}" for px_, py_ in f["pts"])
            L.append(f"  {'-'.join(f['of'])}：{pts}")
    L.append("说明：3D 坐标已正交投影成 2D、面已按深度排序；坐标直接照抄，你只负责配色与标注。")
    if geo.notes:
        L.append("提示：" + "；".join(geo.notes))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 参数曲线 x(t), y(t)（函数图像之外的任意参数方程）
# ---------------------------------------------------------------------------
@dataclass
class ParamCurve:
    """参数曲线 x(t), y(t) 的几何（等比例映射）。"""
    x_expr: str
    y_expr: str
    t_range: Tuple[float, float]
    view: Tuple[float, float, float, float]
    area: Tuple[float, float, float, float]
    scale: float
    curve_d: str = ""
    curve_points: List[Tuple[float, float]] = field(default_factory=list)
    axes: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_svg(self, x: float, y: float) -> Tuple[float, float]:
        xmin, xmax, ymin, ymax = self.view
        vcx, vcy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        x0, y0, x1, y1 = self.area
        acx, acy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        return (acx + (x - vcx) * self.scale, acy - (y - vcy) * self.scale)


def param_curve_geometry(
    x_expr: str, y_expr: str, t_range: Sequence[float], *,
    view: Sequence[float], area: Sequence[float], samples: int = 129,
) -> ParamCurve:
    """算参数曲线 ``(x(t), y(t))`` 的折线 path（等比例，不变形）。

    为什么需要它：``function_plot`` 只能画 y=f(x)，``conic`` 只认四种圆锥曲线；
    像摆线 `x=t-sin t, y=1-cos t`、心形线、双纽线这类高中拓展曲线，只能靠参数方程。
    """
    fx = compile_expr(str(x_expr), var="t")
    fy = compile_expr(str(y_expr), var="t")
    if not (isinstance(t_range, (list, tuple)) and len(t_range) == 2):
        raise ValueError("t_range 应为 [tmin, tmax]")
    t0, t1 = float(t_range[0]), float(t_range[1])
    if not t1 > t0:
        raise ValueError(f"t_range 必须递增：{t_range}")
    if not (isinstance(view, (list, tuple)) and len(view) == 4):
        raise ValueError("view 应为 [xmin, xmax, ymin, ymax]")
    xmin, xmax, ymin, ymax = (float(v) for v in view)
    if not (xmax > xmin and ymax > ymin):
        raise ValueError(f"view 必须递增：{view}")
    if not (isinstance(area, (list, tuple)) and len(area) == 4):
        raise ValueError("area 应为 [x左, y上, x右, y下]")
    x0, y0, x1, y1 = (float(v) for v in area)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"area 必须是左上-右下矩形：{area}")
    n = max(17, int(samples))
    scale = min((x1 - x0) / (xmax - xmin), (y1 - y0) / (ymax - ymin))
    geo = ParamCurve(x_expr=normalize_expr(x_expr), y_expr=normalize_expr(y_expr),
                     t_range=(t0, t1), view=(xmin, xmax, ymin, ymax),
                     area=(x0, y0, x1, y1), scale=scale)
    ts = [t0 + (t1 - t0) * i / (n - 1) for i in range(n)]
    pts: List[Tuple[float, float]] = []
    for t in ts:
        px, py = fx(t), fy(t)
        if not (math.isfinite(px) and math.isfinite(py)):
            geo.notes.append(f"t={_fmt(t)} 处非有限值，已跳过")
            continue
        pts.append(geo.to_svg(px, py))
    if len(pts) < 2:
        raise ValueError("参数曲线采样点不足（检查表达式或 t 范围）")
    geo.curve_points = pts
    geo.curve_d = "M" + " L".join(f"{_fmt(sx)} {_fmt(sy)}" for sx, sy in pts)
    ox, oy = geo.to_svg(0.0, 0.0)
    geo.axes = {
        "x_axis": f"({_fmt(x0)}, {_fmt(oy)}) → ({_fmt(x1)}, {_fmt(oy)})",
        "y_axis": f"({_fmt(ox)}, {_fmt(y1)}) → ({_fmt(ox)}, {_fmt(y0)})",
    }
    return geo


def format_param_curve(geo: ParamCurve) -> str:
    L = [
        "【参数曲线几何 —— 已由代码算好，直接使用，不要自己重算】",
        f"x(t) = {geo.x_expr}　y(t) = {geo.y_expr}　"
        f"t ∈ [{_fmt(geo.t_range[0])}, {_fmt(geo.t_range[1])}]",
        f"视窗：x∈[{_fmt(geo.view[0])}, {_fmt(geo.view[1])}]　"
        f"y∈[{_fmt(geo.view[2])}, {_fmt(geo.view[3])}]，等比例 scale={geo.scale:.3f}",
        "",
        f"曲线 path（{len(geo.curve_points)} 点采样，直接用作 <path d=\"…\" fill=\"none\">）：",
        geo.curve_d,
        "",
        f"x 轴：{geo.axes.get('x_axis')}",
        f"y 轴：{geo.axes.get('y_axis')}",
    ]
    if geo.notes:
        L.append("提示：" + "；".join(geo.notes))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 旋转体（母线绕竖直轴旋转；立体几何进阶）
# ---------------------------------------------------------------------------
@dataclass
class Revolution:
    """旋转体线框：几条母线 + 几个纬线圆（均已投影成 2D path）。"""
    profile: List[Tuple[float, float]]
    azimuth: float
    elevation: float
    scale: float
    paths: List[Dict[str, str]] = field(default_factory=list)   # {name, d}
    notes: List[str] = field(default_factory=list)


def revolution_geometry(
    profile: Sequence[Sequence[float]], *,
    segments: int = 48, meridians: Sequence[float] = (0.0, 90.0, 180.0, 270.0),
    ring_count: int = 3,
    azimuth: float = 30.0, elevation: float = 20.0,
    scale: float = 60.0, origin: Sequence[float] = (640.0, 400.0),
) -> Revolution:
    """母线 ``profile`` 绕竖直轴（z 轴）旋转成旋转体（线框：几条母线 + 几个纬线圆）。

    为什么需要它：圆柱/圆锥/圆台/球这类旋转体，手画很难把纬线椭圆画准。
    ``profile`` = ``[(半径 r, 高度 h), ...]``（母线在轴的一侧）；``meridians`` = 要画哪几个
    方位角的母线（度）；``ring_count`` = 画几条纬线圆。
    """
    prof = [(float(r), float(h)) for r, h in profile]
    if len(prof) < 2:
        raise ValueError("profile 至少需要 2 个 (r, h) 点")
    geo = Revolution(profile=prof, azimuth=float(azimuth),
                     elevation=float(elevation), scale=float(scale))
    az = math.radians(float(azimuth))
    el = math.radians(float(elevation))
    ox, oy = float(origin[0]), float(origin[1])
    sc = float(scale)
    right = (math.cos(az), -math.sin(az), 0.0)
    up = (math.sin(az) * math.sin(el), math.cos(az) * math.sin(el), math.cos(el))

    def proj(x, y, z):
        return (ox + (x * right[0] + y * right[1] + z * right[2]) * sc,
                oy - (x * up[0] + y * up[1] + z * up[2]) * sc)

    n_seg = max(8, int(segments))
    for phi_deg in meridians:
        phi = math.radians(float(phi_deg))
        pts = [proj(r * math.cos(phi), r * math.sin(phi), h) for r, h in prof]
        d = "M" + " L".join(f"{_fmt(sx)} {_fmt(sy)}" for sx, sy in pts)
        geo.paths.append({"name": f"母线 φ={_fmt(float(phi_deg))}°", "d": d})
    rc = max(2, int(ring_count))
    idxs = sorted(set(int(round(i * (len(prof) - 1) / (rc - 1))) for i in range(rc)))
    for ii in idxs:
        r, h = prof[ii]
        if r <= 0:
            continue
        cpts = [proj(r * math.cos(2 * math.pi * j / n_seg),
                     r * math.sin(2 * math.pi * j / n_seg), h)
                for j in range(n_seg + 1)]
        d = "M" + " L".join(f"{_fmt(sx)} {_fmt(sy)}" for sx, sy in cpts)
        geo.paths.append({"name": f"纬线 r={_fmt(r)} h={_fmt(h)}", "d": d})
    return geo


def format_revolution(geo: Revolution) -> str:
    prof_txt = "　".join(f"({_fmt(r)}, {_fmt(h)})" for r, h in geo.profile)
    L = [
        "【旋转体几何 —— 已由代码算好，直接使用，不要自己重算】",
        f"母线（r,h）：{prof_txt}",
        f"相机：方位角 {_fmt(geo.azimuth)}°　仰角 {_fmt(geo.elevation)}°　缩放 {_fmt(geo.scale)}px/单位",
        "",
        "母线/纬线（各是一条完整 path，直接用作 <path d=\"…\" fill=\"none\">）：",
    ]
    for p in geo.paths:
        L.append(f"  {p['name']}：{p['d']}")
    L.append("说明：母线绕轴旋转，纬线圆已投影成椭圆；坐标为 SVG 像素，直接照抄。")
    if geo.notes:
        L.append("提示：" + "；".join(geo.notes))
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 公式排版（分式/极限/根号）——模型手拼必然错位（实测：分子分母同 y 重叠）
# ---------------------------------------------------------------------------
def formula_layout(
    template: str, *, x: float, y: float, font_size: float = 24, gap: float = 16,
    lhs: str = "", lim_sub: str = "", numerator: str = "", denominator: str = "",
    radicand: str = "", index: str = "",
) -> str:
    """排版数学公式，代码算全部坐标，返回可直接照抄的文本块。

    为什么需要它（实测 2026-10-11 第 5 页）：模型手拼分式时把**分子与分母放在同一
    个 y、同一行**，直接重叠；分数线也没盖住分子。公式是「算出来的东西」，
    必须由代码定位。

    template：
      · ``limit_fraction``  左端 + lim + 下标 + 分式（导数定义式）
      · ``fraction``        左端(可选) + 分式
      · ``sqrt``            左端(可选) + 根号（可带 ``index`` 次） + 被开方数

    ``x``/``y`` = 公式左侧基线起点（y = 主线基线）；``font_size`` = 正文字号。
    """
    tmpl = str(template).strip().lower()
    if tmpl not in ("limit_fraction", "fraction", "sqrt"):
        raise ValueError(f"formula.template={template!r} 不是支持的模板"
                         "（limit_fraction/fraction/sqrt）")
    fs = float(font_size)
    gp = float(gap)
    out: List[str] = ["【公式排版 —— 已由代码算好，直接使用，不要自己重算】"]

    def _frac(left_x: float, num: str, den: str) -> float:
        """在 left_x 处排一个分式，返回它的右边界 x。"""
        nw = _text_width_svg(num, fs)
        dw = _text_width_svg(den, fs)
        fw = max(nw, dw, fs)
        cx = left_x + fw / 2.0
        bar_y = y - fs * 0.12
        out.append(f"分子「{num}」：x={_fmt(cx)}  y={_fmt(bar_y - fs * 0.35)}  "
                   f"font-size={_fmt(fs)}  text-anchor=middle")
        out.append(f"分母「{den}」：x={_fmt(cx)}  y={_fmt(bar_y + fs * 1.0)}  "
                   f"font-size={_fmt(fs)}  text-anchor=middle")
        out.append(f"分数线：({_fmt(left_x - 2)}, {_fmt(bar_y)}) → "
                   f"({_fmt(left_x + fw + 2)}, {_fmt(bar_y)})  stroke-width=2")
        return left_x + fw

    if tmpl == "limit_fraction":
        if not numerator or not denominator:
            raise ValueError("limit_fraction 需要 numerator 与 denominator")
        out.append(f"左端「{lhs}」：x={_fmt(x)}  y={_fmt(y)}  font-size={_fmt(fs)}（基线、左对齐）")
        cur = x + _text_width_svg(lhs, fs)
        if lim_sub or True:
            cur += gp
            lim_w = _text_width_svg("lim", fs)
            sub_size = fs * 0.6
            sub_w = _text_width_svg(lim_sub, sub_size)
            block = max(lim_w, sub_w)
            lcx = cur + block / 2.0
            out.append(f"lim：x={_fmt(lcx)}  y={_fmt(y)}  font-size={_fmt(fs)}  text-anchor=middle")
            if lim_sub:
                out.append(f"lim 下标「{lim_sub}」：x={_fmt(lcx)}  y={_fmt(y + fs * 0.55)}  "
                           f"font-size={_fmt(sub_size)}  text-anchor=middle")
            cur += block + gp
        _frac(cur, numerator, denominator)
    elif tmpl == "fraction":
        if not numerator or not denominator:
            raise ValueError("fraction 需要 numerator 与 denominator")
        cur = x
        if lhs:
            out.append(f"左端「{lhs}」：x={_fmt(x)}  y={_fmt(y)}  font-size={_fmt(fs)}（基线、左对齐）")
            cur = x + _text_width_svg(lhs, fs) + gp
        _frac(cur, numerator, denominator)
    else:  # sqrt
        if not radicand:
            raise ValueError("sqrt 需要 radicand（被开方数）")
        cur = x
        if lhs:
            out.append(f"左端「{lhs}」：x={_fmt(x)}  y={_fmt(y)}  font-size={_fmt(fs)}（基线、左对齐）")
            cur = x + _text_width_svg(lhs, fs) + gp
        rw = _text_width_svg(radicand, fs)
        top_y = y - fs * 0.95
        bot_y = y + fs * 0.25
        hook = (f"M{_fmt(cur)} {_fmt(y - fs * 0.35)} L{_fmt(cur + fs * 0.3)} {_fmt(bot_y)} "
                f"L{_fmt(cur + fs * 0.62)} {_fmt(top_y)} L{_fmt(cur + rw + fs * 0.5)} {_fmt(top_y)}")
        out.append(f"根号：<path d=\"{hook}\" fill=\"none\" stroke-width=2/>")
        out.append(f"被开方数「{radicand}」：x={_fmt(cur + fs * 0.78)}  y={_fmt(y)}  "
                   f"font-size={_fmt(fs)}（基线、左对齐）")
        if index:
            out.append(f"根指数「{index}」：x={_fmt(cur - fs * 0.1)}  y={_fmt(top_y - fs * 0.1)}  "
                       f"font-size={_fmt(fs * 0.6)}  text-anchor=middle")
    out.append("说明：坐标已算好，直接照抄；你只负责配色与字号统一。")
    return "\n".join(out)


# ===========================================================================
# 四、离线自检
# ===========================================================================
def self_check() -> None:
    print("🔎 plot_math 自检\n")

    # ① 数学正确性：抛物线的切点与切线
    geo = plot_geometry("0.35*x^2 + 0.5", (-3, 3), (560, 140, 1180, 620),
                        point=2.0, secants=[0.0, 1.0, 1.5])
    assert abs(geo.point["slope"] - 0.7 * 2.0) < 1e-6, geo.point["slope"]
    # 切点必须落在曲线上：把切点代回曲线 path 的采样点验证
    px, py = geo.point["x"], geo.point["y"]
    assert abs(py - (0.35 * px ** 2 + 0.5)) < 1e-12
    sx = geo.to_svg_x(px)
    assert 560 <= sx <= 1180 and 140 <= geo.to_svg_y(py) <= 620
    print(f"   ① 切点/导数 OK：f'(2)={geo.point['slope']:.4f}，P → SVG "
          f"({_fmt(geo.point['svg_x'])}, {_fmt(geo.point['svg_y'])})")

    # ② 切线确实与曲线相切：切线在偏移 d 处与曲线的偏差应为二阶小量，
    #    且 |偏差|/d² 应稳定在 |f''|/2 = 0.35（抛物线）。
    k = geo.tangent["slope_math"]
    err = []
    for d in (0.5, 0.25, 0.125):
        curve_y = 0.35 * (2 + d) ** 2 + 0.5
        line_y = py + k * d
        err.append(abs(curve_y - line_y) / (d * d))
    assert max(err) - min(err) < 0.05, f"切线二阶误差不稳定：{err}"
    assert abs(err[-1] - 0.35) < 0.01, f"二阶误差系数应 ≈ f''/2 = 0.35，实得 {err[-1]:.4f}"
    print(f"   ② 切线相切性 OK：|偏差|/d² ≈ {err[-1]:.4f}（= f''/2 = 0.35）")

    # ③ 割线端点必须也落在曲线上（这正是快速版崩掉的地方）
    for i, s in enumerate(geo.secants, 1):
        expect = 0.35 * s["x"] ** 2 + 0.5
        assert abs(s["y"] - expect) < 1e-12, (i, s)
    print(f"   ③ {len(geo.secants)} 条割线端点全部落在曲线上 OK")

    # ④ 曲线 path 能解析且点数正确
    import re as _re
    assert _re.fullmatch(r"M[\d.\- ]+( L[\d.\- ]+)*", geo.curve_d), "path 语法异常"
    assert geo.curve_d.count("L") + 1 == geo.samples
    print(f"   ④ 曲线 path OK：{geo.samples} 点折线，{len(geo.curve_d)} 字符")

    # ⑤ 安全：危险表达式必须被拒
    for bad in ("__import__('os').system('echo hi')", "open('x')", "x.__class__",
                "lambda: 1", "[i for i in range(3)]"):
        try:
            compile_expr(bad)
            raise AssertionError(f"危险表达式未被拒绝：{bad}")
        except ValueError:
            pass
    print("   ⑤ 表达式白名单 OK：5 类危险输入全部拒绝")

    # ⑥ 数学记号归一化（模型常写 x^2 / 2x / √x / sin）
    for raw, want in (("x^2", "x**2"), ("2x", "2*x"), ("x²", "x**2"),
                      ("√x", "sqrt(x)"), ("√(x+1)", "sqrt(x+1)"),
                      ("3sin(x)", "3*sin(x)")):
        got = normalize_expr(raw)
        assert got == want, f"归一化错：{raw!r} → {got!r}，期望 {want!r}"
    for raw, x, want in (("2x + 1", 3, 7), ("x²", 3, 9), ("√x", 9, 3),
                         ("x^3 - 2x", 2, 4)):
        got = compile_expr(raw)(x)
        assert abs(got - want) < 1e-9, f"求值错：{raw!r}({x}) = {got}，期望 {want}"
    print("   ⑥ 记号归一化 OK：^ ² √ 2x 3sin(x) 均正确且可求值")

    # ⑦ 单调函数不炸；y_range 自动留白合理
    g2 = plot_geometry("exp(x)/10", (0, 3), (100, 100, 700, 400))
    assert g2.y_range[0] < min(1 / 10, math.exp(3) / 10) and g2.y_range[1] > math.exp(3) / 10
    assert g2.point is None and g2.secants == []
    print(f"   ⑦ 无切点场景 OK：y_range 自动 = [{g2.y_range[0]:.3f}, {g2.y_range[1]:.3f}]")

    # ⑧ 越界/非法输入必须报 ValueError（调用方据此降级）
    for kwargs, why in (
        (dict(domain=(3, -3)), "定义域倒序"),
        (dict(domain=(-3, 3), area=(0, 0, 0, 100)), "零宽绘图区"),
        (dict(domain=(-3, 3), point=9.0), "切点越界"),
        (dict(domain=(-3, 3), point=None, secants=(1.0,)), "只给割线不给切点"),
    ):
        base = dict(expr="x**2", domain=(-3, 3), area=(0, 0, 100, 100))
        base.update(kwargs)
        try:
            g = plot_geometry(**base)
            assert why.startswith("只给割线"), f"{why} 未被拒绝"
            assert g.notes, f"{why} 未记 note"
        except ValueError:
            assert not why.startswith("只给割线"), f"{why} 不该被拒绝"
    print("   ⑧ 非法输入 OK：倒序定义域 / 零宽区域 / 越界切点 均报错，缺切点记 note")

    # ⑨ 标注避让：互不重叠、不压曲线/切线/割线/轴、不出绘图区
    #    这是 `none` 下最难的一块：实测模型会把 Q4 叠在 P 上、Q3 压在割线上，
    #    而 checker 不查同模块内元素重叠，所以它报“错误 0”。
    marks = suggest_labels(geo)
    assert [m.text for m in marks] == ["P", "Q1", "Q2", "Q3"], [m.text for m in marks]
    boxes = [_label_box(m.x, m.y, _text_width_svg(m.text, 18.0), 18.0) for m in marks]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ow = min(boxes[i][2], boxes[j][2]) - max(boxes[i][0], boxes[j][0])
            oh = min(boxes[i][3], boxes[j][3]) - max(boxes[i][1], boxes[j][1])
            assert not (ow > 0 and oh > 0), f"标注重叠：{marks[i].text} / {marks[j].text}"
    segs = plot_obstacles(geo)
    for mk, box in zip(marks, boxes):
        for p, q in segs:
            assert not _seg_hits_box(p, q, box), f"标注「{mk.text}」压在线上"
    ax0, ay0, ax1, ay1 = geo.area
    for box in boxes:
        assert ax0 <= box[0] and ay0 <= box[1] and box[2] <= ax1 and box[3] <= ay1, \
            f"标注出绘图区：{box}"
    assert not any(m.crowded for m in marks), [m.text for m in marks if m.crowded]
    print(f"   ⑨ 标注避让 OK：{len(marks)} 个标注互不重叠、不压任何线、均在绘图区内")

    # ⑩ 避让确实在“做事”：三个标注堆在同一个锚点时应该散开
    stacked = suggest_labels(geo, [("A", (900.0, 300.0)), ("B", (900.0, 300.0)),
                                       ("C", (900.0, 300.0))])
    bs = [_label_box(m.x, m.y, _text_width_svg(m.text, 18.0), 18.0) for m in stacked]
    for i in range(len(bs)):
        for j in range(i + 1, len(bs)):
            ow = min(bs[i][2], bs[j][2]) - max(bs[i][0], bs[j][0])
            oh = min(bs[i][3], bs[j][3]) - max(bs[i][1], bs[j][1])
            assert not (ow > 0 and oh > 0), f"同锚点标注没散开：{stacked[i]} / {stacked[j]}"
    print(f"   ⑩ 避让有效 OK：3 个同锚点标注自动散到 {[m.side for m in stacked]}")

    # ⑪ 多切点：points 列表给多个切点，各自算切点 + 切线（正负单调/陡峭类页面）
    g3 = plot_geometry("0.35*x**2 + 0.5", (-3, 3), (560, 140, 1180, 620),
                       points=[-2.0, 0.0, 2.0])
    assert len(g3.points) == 3, f"应算出 3 个切点，实际 {len(g3.points)}"
    for i, p in enumerate(g3.points):
        expect_y = 0.35 * p["x"] ** 2 + 0.5
        assert abs(p["y"] - expect_y) < 1e-12, (i, p)
        assert p.get("tangent_points"), f"切点 {i} 没切线"
        assert abs(p["slope"] - 0.7 * p["x"]) < 1e-6, f"切点 {i} 斜率错"
    # point / tangent 向后兼容指向第一个切点
    assert g3.point is not None and abs(g3.point["x"] - (-2.0)) < 1e-12
    assert g3.tangent is not None and g3.tangent["slope_math"] == g3.points[0]["slope"]
    # 多切点默认标注 P1/P2/P3
    marks3 = suggest_labels(g3)
    assert [m.text for m in marks3] == ["P1", "P2", "P3"], [m.text for m in marks3]
    print("   ⑪ 多切点 OK：3 个切点各自算切点+切线，point/tangent 向后兼容指向 P1")

    # ⑫ 尖点：|x| 在 x=0 不可导，只画割线不画切线
    g4 = plot_geometry("abs(x)", (-4, 4), (560, 140, 1180, 620),
                       sharp=0.0, secants=[-2.0, 2.0])
    assert g4.sharp is not None and abs(g4.sharp["x"]) < 1e-12, g4.sharp
    assert g4.points == [] and g4.tangent is None, "尖点不该有切线"
    assert any(abs(px - g4.sharp["svg_x"]) < 1e-6 for px, _ in g4.curve_points), "采样点没包含尖点"
    assert len(g4.secants) == 2
    assert abs(g4.secants[0]["secant_slope"] - (-1.0)) < 1e-6, g4.secants[0]
    assert abs(g4.secants[1]["secant_slope"] - 1.0) < 1e-6, g4.secants[1]
    print("   ⑫ 尖点 OK：|x| 尖点精确、无切线、左右割线斜率 ±1")

    # ⑬ 圆锥曲线：椭圆/双曲线/抛物线（原来只能画 y=f(x)，这些不是函数）
    ell = conic_geometry("ellipse", a=3, b=2, center=(0, 0),
                         view=[-5, 5, -4, 4], area=[660, 140, 1230, 620])
    assert len(ell.curves) == 1 and len(ell.curve_points[0]) == 65, "椭圆应为 1 段 65 点"
    assert len(ell.foci) == 2 and len(ell.vertices) == 4, "椭圆应有 2 焦点 + 4 顶点"
    assert abs((ell.foci[0][0] + ell.foci[1][0]) / 2 - ell.to_svg(0, 0)[0]) < 1e-6, "焦点不对称"
    hyp = conic_geometry("hyperbola", a=2, b=1.5, center=(0, 0),
                         view=[-6, 6, -5, 5], area=[660, 140, 1230, 620])
    assert len(hyp.curves) == 2, "双曲线应有 2 支"
    para = conic_geometry("parabola", p=2, center=(0, 0),
                          view=[-1, 6, -5, 5], area=[660, 140, 1230, 620])
    assert len(para.curves) == 1 and len(para.foci) == 1, "抛物线 1 段 1 焦点"
    print("   ⑬ 圆锥曲线 OK：椭圆(2焦点)/双曲线(2支)/抛物线(1焦点)，等比例不变形")

    # ⑭ 圆锥曲线的点/弦/切线：实测模型自画这些会飘离曲线（弦端点偏离 0.68≠1）
    ce = conic_geometry("ellipse", a=3, b=2, view=[-5, 5, -4, 4], area=[660, 140, 1230, 620],
                        points=[{"name": "A", "t": 30}, {"name": "B", "t": 150}],
                        chords=[["A", "B"]], tangents=["A", "B"])
    for p in ce.points:
        mx, my = p["math"]
        assert abs(mx * mx / 9 + my * my / 4 - 1) < 1e-9, f"{p['name']} 不在椭圆上"
    assert len(ce.chords) == 1 and len(ce.tangents) == 2, (ce.chords, ce.tangents)
    assert all(t["seg"] for t in ce.tangents), "切线裁剪失败"
    print("   ⑭ 圆锥曲线点/弦/切线 OK：点精确在曲线上、弦连两端点、切线过切点")

    # ⑮ 自由点/线/交点/过外点切线（实测模型自画交点、切线都会错）
    g5 = conic_geometry("ellipse", a=3, b=2, view=[-5, 5, -4, 4], area=[660, 140, 1230, 620],
                        points=[{"name": "A", "t": 30}],
                        free_points=[{"name": "P", "at": [2, 3]}, {"name": "C", "at": [-4, -3]},
                                     {"name": "D", "at": [4, -3]}],
                        lines=[{"name": "l1", "through": ["C", "D"]},
                               {"name": "l2", "through": ["A", "P"]}],
                        intersections=[{"name": "Q", "of": ["l1", "l2"]}],
                        ext_tangents=[{"point": "P", "names": ["t1", "t2"]}])
    assert len(g5.intersections) == 1, "交点没算出来"
    assert len(g5.ext_tangents) == 2, "过外点应有 2 条切线"
    psvg = g5.free_points[0]["svg"]
    for tg in g5.ext_tangents:
        (tx1, ty1), (tx2, ty2) = tg["seg"]
        dist = abs((ty2 - ty1) * psvg[0] - (tx2 - tx1) * psvg[1] + tx2 * ty1 - ty2 * tx1)
        dist /= math.hypot(ty2 - ty1, tx2 - tx1) or 1.0
        assert dist < 0.1, f"切线不过外点：{dist}"
    print("   ⑮ 自由点/线/交点/外点切线 OK：交点算出、过外点 2 条切线且都过该点")

    # ⑯ 圆 + 四种曲线的过外点切线（circle/ellipse 解析、hyperbola/parabola 解二次）
    for _ctype, _kw in (("circle", {"a": 2}), ("ellipse", {"a": 3, "b": 2}),
                        ("hyperbola", {"a": 2, "b": 1.5}), ("parabola", {"p": 2})):
        gc = conic_geometry(_ctype, view=[-6, 6, -5, 5], area=[660, 140, 1230, 620],
                            free_points=[{"name": "P", "at": [3, 4]}],
                            ext_tangents=[{"point": "P", "names": ["t1", "t2"]}], **_kw)
        assert len(gc.ext_tangents) == 2, f"{_ctype} 应有 2 条切线，实得 {len(gc.ext_tangents)}"
        _pp = gc.free_points[0]["svg"]
        for _tg in gc.ext_tangents:
            (ex1, ey1), (ex2, ey2) = _tg["seg"]
            _d = abs((ey2 - ey1) * _pp[0] - (ex2 - ex1) * _pp[1] + ex2 * ey1 - ey2 * ex1)
            _d /= math.hypot(ey2 - ey1, ex2 - ex1) or 1.0
            assert _d < 0.1, f"{_ctype} 切线不过外点：{_d}"
    print("   ⑯ 圆/椭圆/双曲线/抛物线 过外点切线 OK：各 2 条且都精确过外点")

    # ⑰ 立体几何（3D 正交投影）
    _cube = {"A": [0, 0, 0], "B": [2, 0, 0], "C": [2, 2, 0], "D": [0, 2, 0],
             "E": [0, 0, 2], "F": [2, 0, 2], "G": [2, 2, 2], "H": [0, 2, 2]}
    g7 = solid3d_geometry(
        _cube,
        edges=[["A", "B"], ["B", "C"], ["C", "D"], ["D", "A"],
               ["E", "F"], ["F", "G"], ["G", "H"], ["H", "E"],
               ["A", "E"], ["B", "F"], ["C", "G"], ["D", "H"]],
        faces=[{"of": ["A", "B", "C", "D"]}, {"of": ["E", "F", "G", "H"]}],
        azimuth=30, elevation=20, scale=80, origin=[640, 400])
    assert len(g7.screens) == 8 and len(g7.edges) == 12 and len(g7.faces) == 2, "3D 投影不完整"
    assert g7.screens["E"][1] < g7.screens["A"][1], "z 向上方向错"
    assert g7.faces[0]["depth"] >= g7.faces[-1]["depth"], "面没按深度排序"
    print("   ⑰ 立体几何(3D) OK：8 顶点投影 + 12 棱 + 面按深度排序，z 向上正确")

    # ⑰b 隐藏棱判定（完整 6 面时，后面的棱应为虚线）
    g7b = solid3d_geometry(
        _cube,
        edges=[["A", "B"], ["B", "C"], ["C", "D"], ["D", "A"], ["E", "F"], ["F", "G"],
               ["G", "H"], ["H", "E"], ["A", "E"], ["B", "F"], ["C", "G"], ["D", "H"]],
        faces=[{"of": ["A", "B", "C", "D"]}, {"of": ["E", "F", "G", "H"]},
               {"of": ["A", "B", "F", "E"]}, {"of": ["C", "D", "H", "G"]},
               {"of": ["B", "C", "G", "F"]}, {"of": ["A", "D", "H", "E"]}],
        azimuth=30, elevation=20, scale=80, origin=[640, 400])
    _hid = [e["name"] for e in g7b.edges if e.get("hidden")]
    assert _hid, "隐藏棱未判定出来"
    assert all(e.get("hidden") is not None for e in g7b.edges), "有棱没判可见性"
    print(f"   ⑰b 隐藏棱 OK：{len(_hid)} 条判为虚线（{', '.join(_hid)}）")

    # ⑱ 参数曲线（函数/圆锥曲线之外的任意参数方程，如摆线）
    pc = param_curve_geometry("t - sin(t)", "1 - cos(t)", [0, 12.566],
                              view=[-1, 14, -1, 3], area=[660, 140, 1230, 620])
    assert len(pc.curve_points) == 129 and pc.curve_d.startswith("M"), "参数曲线采样异常"
    assert "曲线 path" in format_param_curve(pc), "格式化异常"
    print("   ⑱ 参数曲线 OK：摆线 129 点采样、path 生成正常")

    # ⑲ 3D 预设 + 透视 + 向量箭头
    for _pre, _cnt in (("cube", (8, 12, 6)), ("tetrahedron", (4, 6, 4)),
                       ("octahedron", (6, 12, 8)), ("prism", (12, 18, 8)),
                       ("pyramid", (5, 8, 5))):
        _g = solid3d_geometry(preset=_pre, n=6 if _pre == "prism" else 4)
        assert (len(_g.screens), len(_g.edges), len(_g.faces)) == _cnt, (_pre, len(_g.screens))
    _gp = solid3d_geometry(preset="cube", projection="perspective", distance=6)
    assert len(_gp.screens) == 8, "透视投影异常"
    _ca = conic_geometry("ellipse", a=3, b=2, view=[-5, 5, -4, 4], area=[660, 140, 1230, 620],
                         free_points=[{"name": "A", "at": [0, 0]}, {"name": "B", "at": [2, 1]}],
                         lines=[{"name": "v", "through": ["A", "B"], "arrow": True}])
    assert _ca.lines[0].get("arrow") is True, "向量箭头丢了"
    print("   ⑲ 3D 预设(cube/正四面体/正八面体/棱柱/棱锥) + 透视 + 向量箭头 全部 OK")

    # ⑳ 旋转体（圆柱：母线 [[0,0],[2,0],[2,3],[0,3]]）
    rv = revolution_geometry([[0, 0], [2, 0], [2, 3], [0, 3]], ring_count=3)
    assert len(rv.paths) >= 5, f"应有 4 母线 + 至少 1 纬线，实得 {len(rv.paths)}"
    assert all(p["d"].startswith("M") for p in rv.paths), "path 生成异常"
    print(f"   ⑳ 旋转体 OK：圆柱母线+纬线 {len(rv.paths)} 条 path")

    # ㉑ 公式排版：分式/极限式/根号（实测模型手拼分子分母同 y 重叠）
    f1 = formula_layout("limit_fraction", lhs="f′(x₀) =", lim_sub="Δx→0",
                        numerator="f(x₀+Δx) − f(x₀)", denominator="Δx", x=96, y=278)
    assert "分子" in f1 and "分母" in f1 and "分数线" in f1, f1
    _num_y = float(re.search(r"分子.*?y=([\d.]+)", f1).group(1))
    _den_y = float(re.search(r"分母.*?y=([\d.]+)", f1).group(1))
    _bar_y = float(re.search(r"分数线：\([-\d.]+, ([\d.]+)\)", f1).group(1))
    assert _num_y < _bar_y < _den_y, f"分子/分数线/分母 y 顺序错：{_num_y}, {_bar_y}, {_den_y}"
    f2 = formula_layout("sqrt", radicand="x+1", x=100, y=200)
    assert "根号" in f2 and "被开方数" in f2, f2
    print("   ㉑ 公式排版 OK：分式（分子<分数线<分母）/根号，坐标不再重叠")

    print("\n✅ plot_math 自检通过：切点/切线/割线几何、path 语法、表达式白名单、记号归一化、"
          "越界拒绝、标注避让、多切点、尖点、圆锥曲线(圆/椭圆/双曲/抛 + 点/弦/切线/交点/外点切线)、"
          "立体几何(预设/透视/隐藏棱)、参数曲线、向量箭头、旋转体、公式排版 全部正常")


def main() -> None:
    ap = argparse.ArgumentParser(description="函数图像几何计算（给 P3 用）")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--expr"); ap.add_argument("--domain", default="-3,3")
    ap.add_argument("--area", default="560,140,1180,620")
    ap.add_argument("--point", type=float); ap.add_argument("--secants", default="")
    args = ap.parse_args()
    if args.selftest or not args.expr:
        self_check()
        return
    geo = plot_geometry(
        args.expr, tuple(float(v) for v in args.domain.split(",")),
        tuple(float(v) for v in args.area.split(",")),
        point=args.point,
        secants=[float(v) for v in args.secants.split(",") if v.strip()])
    print(format_geometry(geo))


if __name__ == "__main__":
    main()
