/* ─────────────────────────────────────────────────────────────────────
 * line-runtime.js — 「一线」滚动驱动的落笔与推轨运行时
 *
 * 只读**构建产物**：build/line-runtime-data.js
 *   由 tools/build_line.py 从 source/line-spec.js 生成，包含等弧长采样点、
 *   切线法线、笔宽、屏幕速度补偿积分表，以及其余渲染参数。
 *   SKILL.md：时间值必须由最终编译结果生成，不手工抄写。本路线没有编译视频，
 *   等价的东西就是这些几何采样 —— 它们能由路径与曲线唯一推出，属于派生量。
 *
 *   在此之前运行时用 SVG 的 getTotalLength / getPointAtLength 在启动时现算
 *   （每采样点一次调用），等于每次打开页面都把编译做一遍。现在挪到构建期。
 *   顺带：不再需要插探针 path，也不再依赖 SVGGeometryElement 的测量 API。
 *
 * 逻辑与来源：
 *   渲染方式（单一闭合轮廓带做变宽度、笔尖插值、固定偏置锚点、分形纸层）
 *   逐行来自已验收的草稿台 qa/sandbox/line-prototype.html，
 *   但那个文件里的算法都经过真页面的硬门验证（qa/integration.md §5）。
 *
 * set(p) 是纯函数：同样的 p 得到同样的画面，所以往回滚就是往回画。
 * 本文件不跑 rAF，由 js/landing.js 在滚动时驱动 —— 只有一个时钟。
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var api = {
    ready: false,
    failed: false,
    reason: '',
    p: 0,
    set: function () {},
    stats: null,
  };
  window.OilMotionLine = api;

  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
  function smooth(a, b, x) {
    var t = clamp01((x - a) / (b - a));
    return t * t * (3 - 2 * t);
  }
  function f2(v) { return v.toFixed(2); }

  function fail(reason) {
    api.failed = true;
    api.ready = false;
    api.reason = reason;
    console.warn('[line] 回退静态：', reason);
    document.documentElement.dataset.line = 'static';
    window.dispatchEvent(new CustomEvent('line:failed', { detail: { reason: reason } }));
  }

  function init() {
    var RT = window.OIL_LINE_RUNTIME;
    if (!RT) {
      return fail('读不到 build/line-runtime-data.js —— 先跑 python tools/build_line.py');
    }

    var stage = document.getElementById('filmPin');
    var ink = document.getElementById('ink');
    var rUnder = document.getElementById('rUnder');
    var rInk = document.getElementById('rInk');
    var rGlow = document.getElementById('rGlow');
    var pFiber = document.getElementById('pFiber');
    var pMacro = document.getElementById('pMacro');
    if (!stage || !ink || !rInk) return fail('首屏缺少 #filmPin / #ink / #rInk');

    var C = RT.camera, A = RT.anchor, RB = RT.ribbon, PA = RT.paper;
    var U = RT.underlay, CO = RT.colour;
    var N = RT.samples;
    var LEN = RT.length;

    // 颜色与格线仍然落成 CSS 变量，样式表里不再各写一份取值
    var root = document.documentElement.style;
    root.setProperty('--ink-color', CO.ink);
    root.setProperty('--violet', CO.violet);
    root.setProperty('--rule', PA.gridRule);

    ink.setAttribute('viewBox', RT.path.viewBox);
    var vb = RT.path.viewBox.trim().split(/\s+/).map(Number);
    ink.style.width = vb[2] + 'px';
    ink.style.height = vb[3] + 'px';

    // 构建产物直接就是数组，不再做任何几何测量。
    // sampleAt = 进度 -> 采样点序号，由构建期折进两件事：
    //   屏幕速度补偿、可见长度加权（回描旅行不占滚动时间）。
    var pts = RT.points, ws = RT.widths, ns = RT.normals, sampleAt = RT.sampleAt;
    var ARC_STEPS = sampleAt.length - 1;
    var kGlow = Math.round(N * RT.colour.glowFrom);

    function scaleAt(p) {
      return p <= C.turn
        ? C.s0 * Math.pow(C.sMax / C.s0, p / C.turn)
        : C.sMax * Math.pow(C.sEnd / C.sMax, (p - C.turn) / (1 - C.turn));
    }
    function indexAt(p) {
      var x = clamp01(p) * ARC_STEPS;
      var k = Math.min(ARC_STEPS - 1, Math.floor(x));
      var f = x - k;
      return sampleAt[k] * (1 - f) + sampleAt[k + 1] * f;
    }

    /* ── 轮廓带：变宽度用单一闭合多边形实现 ──────────────────────────
       绝不能按宽度分桶：那样一条线会被切成几十段彼此错开的短线，
       看上去就是「同时有几条线」。多边形顺序必须是简单闭合环：
         L 正向 → 末端半圆帽 → R 反向 → 起端半圆帽（两处帽端都不反转）。 */
    function geoAt(idx, wMul) {
      var j = Math.min(N, Math.max(0, idx));
      var a = pts[Math.max(0, j - 1)], b = pts[Math.min(N, j + 1)];
      var dx = b[0] - a[0], dy = b[1] - a[1];
      return { x: pts[j][0], y: pts[j][1], h: ws[j] * wMul / 2,
               nx: ns[j][0], ny: ns[j][1], tx: dx, ty: dy };
    }
    function capPoints(g, out) {
      var sgn = out ? 1 : -1, res = [];
      var l = Math.hypot(g.tx, g.ty) || 1;
      var tx = g.tx / l, ty = g.ty / l;
      for (var k = 1; k < RB.capSteps; k++) {
        var ang = Math.PI * k / RB.capSteps;
        var cx = Math.cos(ang) * sgn, cy = Math.sin(ang) * sgn;
        res.push([g.x + g.h * (g.nx * cx + tx * cy),
                  g.y + g.h * (g.ny * cx + ty * cy)]);
      }
      return res;
    }
    function ribbon(from, to, capStart, capEnd, wMul, tail) {
      if (to <= from && !tail) return '';
      var L = [], R = [], j;
      for (j = from; j <= to; j++) {
        var h = ws[j] * wMul / 2;
        L.push([pts[j][0] + ns[j][0] * h, pts[j][1] + ns[j][1] * h]);
        R.push([pts[j][0] - ns[j][0] * h, pts[j][1] - ns[j][1] * h]);
      }
      var endGeo = tail
        ? { x: tail.x, y: tail.y, h: tail.h * wMul, nx: tail.nx, ny: tail.ny,
            tx: tail.tx, ty: tail.ty }
        : geoAt(to, wMul);
      if (tail) {
        L.push([tail.x + tail.nx * tail.h * wMul, tail.y + tail.ny * tail.h * wMul]);
        R.push([tail.x - tail.nx * tail.h * wMul, tail.y - tail.ny * tail.h * wMul]);
      }
      if (!L.length) return '';
      var d = 'M ' + f2(L[0][0]) + ' ' + f2(L[0][1]);
      for (j = 1; j < L.length; j++) d += ' L ' + f2(L[j][0]) + ' ' + f2(L[j][1]);
      if (capEnd) {
        var ce = capPoints(endGeo, true);
        for (j = 0; j < ce.length; j++) d += ' L ' + f2(ce[j][0]) + ' ' + f2(ce[j][1]);
      }
      for (j = R.length - 1; j >= 0; j--) d += ' L ' + f2(R[j][0]) + ' ' + f2(R[j][1]);
      if (capStart) {
        var cs = capPoints(geoAt(from, wMul), false);
        for (j = 0; j < cs.length; j++) d += ' L ' + f2(cs[j][0]) + ' ' + f2(cs[j][1]);
      }
      return d + ' Z';
    }

    /* 笔尖插值到采样点之间：不插值就会按采样间距跳步，在放大的地方很明显。 */
    function tipAt(p) {
      var kf = indexAt(p);
      var kI = Math.min(N, Math.max(0, Math.floor(kf)));
      var f = Math.min(1, Math.max(0, kf - kI));
      if (kI >= N || f < 1e-4) {
        var g = geoAt(kI, 1);
        return { kI: kI, tail: null, x: g.x, y: g.y, w: g.h * 2 };
      }
      var a = pts[kI], b = pts[kI + 1];
      var nx = ns[kI][0] * (1 - f) + ns[kI + 1][0] * f;
      var ny = ns[kI][1] * (1 - f) + ns[kI + 1][1] * f;
      var nl = Math.hypot(nx, ny) || 1;
      nx /= nl; ny /= nl;
      var w = ws[kI] * (1 - f) + ws[kI + 1] * f;
      var x = a[0] + (b[0] - a[0]) * f, y = a[1] + (b[1] - a[1]) * f;
      return { kI: kI, x: x, y: y, w: w,
               tail: { x: x, y: y, h: w / 2, nx: nx, ny: ny,
                       tx: b[0] - a[0], ty: b[1] - a[1] } };
    }

    function set(p) {
      p = clamp01(p);
      api.p = p;
      var t = tipAt(p);
      var k = t.kI;

      if (k < kGlow) {
        rInk.setAttribute('d', ribbon(0, k, true, true, 1, t.tail));
        rGlow.setAttribute('d', '');
      } else {
        rInk.setAttribute('d', ribbon(0, kGlow, true, false, 1, null));
        rGlow.setAttribute('d', ribbon(kGlow, k, false, true, 1, t.tail));
      }
      rInk.style.opacity = p > 0.002 ? '1' : '0';

      var kU = U.default === 'off' ? 0 : Math.round(indexAt(clamp01(p + U.lead)));
      rUnder.setAttribute('d', kU > 1 ? ribbon(0, kU, true, true, U.widthMul) : '');
      rUnder.style.opacity = U.default === 'off' ? '0' : String(U.opacity);

      // ── 摄影机：锚点 = 笔尖与固定偏置点的加权 ──────────────────────
      // bias 必须是常量。笔尖在画面上的位移 = (tip − 纸心) · bias · s，
      // 路径项正比于 bias；若 bias 随进度变，尾部会按构造快数倍
      // （实测 3.9 倍，见 line-spec.js 的 anchor 说明）。
      var w = A.bias;
      var ax = t.x + (RT.path.sheetCentre.x - t.x) * w;
      var ay = t.y + (RT.path.sheetCentre.y - t.y) * w;
      var s = scaleAt(p);
      var vw = stage.clientWidth, vh = stage.clientHeight;
      var tx = vw / 2 - ax * s, ty = vh / 2 - ay * s;

      ink.style.transform = 'translate3d(' + f2(tx) + 'px,' + f2(ty) + 'px,0) scale(' + s.toFixed(4) + ')';

      var ft = PA.fiberTile * s, gt = PA.gridTile * s;
      pFiber.style.backgroundSize =
        ft.toFixed(1) + 'px ' + ft.toFixed(1) + 'px, ' +
        gt.toFixed(1) + 'px ' + gt.toFixed(1) + 'px, ' +
        gt.toFixed(1) + 'px ' + gt.toFixed(1) + 'px';
      pFiber.style.backgroundPosition = tx.toFixed(1) + 'px ' + ty.toFixed(1) + 'px';
      pMacro.style.backgroundSize = (PA.macroTile * s).toFixed(1) + 'px auto';
      pMacro.style.backgroundPosition = tx.toFixed(1) + 'px ' + ty.toFixed(1) + 'px';
      pMacro.style.opacity = (1 - smooth(PA.macroFadeIn, PA.macroFadeOut, s)).toFixed(3);
    }

    api.set = set;
    api.ready = true;
    api.stats = {
      length: LEN, samples: N, arcSteps: ARC_STEPS, glowFrom: CO.glowFrom,
      sourceSha256: RT.sourceSha256, builtFrom: RT.builtFrom,
    };
    document.documentElement.dataset.line = 'vector';
    set(0);
    window.dispatchEvent(new CustomEvent('line:ready'));
  }

  try {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  } catch (e) {
    fail('运行时初始化异常：' + (e && e.stack ? e.stack : e));
  }
})();
