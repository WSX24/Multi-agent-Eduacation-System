/* ─────────────────────────────────────────────────────────────────────
 * 草稿台的驱动脚本。
 *
 * 这个文件**不实现任何渲染逻辑** —— 它只做两件事：
 *   1. 把 p 喂给与 landing.html 相同的运行时（js/line-runtime.js）
 *   2. 把数值打出来 / 输出速度诊断表
 *
 * 之所以独立成文件而不是内联在 HTML 里：内联时那些 `\n` 转义在命令传递中
 * 被吃过两次，每次都在字符串里留下真实换行导致语法错误。落到文件里就没有
 * 这一层转义了。
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var qa = new URLSearchParams(location.search);
  var qaP = qa.has('p') ? Math.min(1, Math.max(0, parseFloat(qa.get('p')) || 0)) : null;
  var UNDER = qa.get('under');
  if (['lead', 'full', 'off'].indexOf(UNDER) === -1) UNDER = null;
  if (qa.get('hud') === '1') document.body.classList.add('hud-on');

  var film = document.getElementById('film');
  var hud = document.getElementById('hud');
  var RT = window.OIL_LINE_RUNTIME;
  var line = window.OilMotionLine;
  var NL = String.fromCharCode(10);

  function fatal(msg) {
    document.body.classList.add('fatal-on');
    document.getElementById('fatal').textContent = msg;
  }

  if (!RT) {
    fatal('读不到 build/line-runtime-data.js' + NL + NL +
          '先跑 python tools/build_line.py');
    return;
  }
  // 草稿台允许 ?under= 覆盖底稿模式；正式页面固定用事实源里的默认值。
  if (UNDER) RT.underlay.default = UNDER;

  /* 运行时是普通 <script>，它在 DOMContentLoaded 之前不会 init，
     所以这里**不能**同步检查 line.ready —— 那样一定误判成失败并 return，
     页面就停在 fatal 框上（第一版就是这样）。改为等 line:ready。 */
  if (line && line.failed) { fatal('运行时失败：' + line.reason); return; }
  if (line && line.ready) boot();
  else window.addEventListener('line:ready', boot);

  var ARC = RT ? RT.sampleAt.length - 1 : 0;
  var N = RT ? RT.samples : 0;

  function indexAt(p) {
    var x = Math.min(1, Math.max(0, p)) * ARC;
    var k = Math.min(ARC - 1, Math.floor(x)), f = x - k;
    return RT.sampleAt[k] * (1 - f) + RT.sampleAt[k + 1] * f;
  }
  function scaleAt(p) {
    var C = RT.camera;
    return p <= C.turn
      ? C.s0 * Math.pow(C.sMax / C.s0, p / C.turn)
      : C.sMax * Math.pow(C.sEnd / C.sMax, (p - C.turn) / (1 - C.turn));
  }
  function tipAt(p) {
    var kf = indexAt(p);
    var kI = Math.min(N, Math.max(0, Math.floor(kf)));
    var fr = Math.min(1, Math.max(0, kf - kI));
    var a = RT.points[kI], b = RT.points[Math.min(N, kI + 1)];
    return { kI: kI, x: a[0] + (b[0] - a[0]) * fr, y: a[1] + (b[1] - a[1]) * fr,
             w: RT.widths[kI] * (1 - fr) + RT.widths[Math.min(N, kI + 1)] * fr };
  }

  function paint(p) {
    line.set(p);
    hud.textContent = [
      'p       ' + p.toFixed(4),
      'zoom    ' + scaleAt(p).toFixed(3),
      '已画    采样点 ' + indexAt(p).toFixed(0) + ' / ' + N,
      '采样    ' + N + ' 点（间距 ' + (RT.length / N).toFixed(2) + ' 单位）',
      '底稿    ' + RT.underlay.default,
      '源哈希  ' + RT.sourceSha256.slice(0, 16) + '…'
    ].join(NL);
  }

  function boot() {
    var current = -1, raf = 0;
    function apply() {
      raf = 0;
      var p = qaP !== null ? qaP : (function () {
        var travel = film.offsetHeight - window.innerHeight;
        return travel > 0 ? Math.min(1, Math.max(0, window.scrollY / travel)) : 0;
      })();
      if (p === current) return;
      current = p;
      paint(p);
    }
    function schedule() { if (!raf) raf = requestAnimationFrame(apply); }
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', function () { current = -1; schedule(); });
    apply();
    if (qa.get('diag') === '1') buildDiag();
  }

  /* ── 诊断表 ──────────────────────────────────────────────────────────
     两个不同的量别搞混：渲染里锚点恒在屏幕中心，所以 |Δ(a·s)| 是
     **相机平移速度**（"整张纸在滑"），而 |Δ((tip−纸心)·bias·s)| 才是
     **笔尖在画面上的速度**（"笔尖在纸上狂奔"）。 */
  function buildDiag() {
    document.body.classList.add('diag-on');
    var SC = RT.path.sheetCentre, bias = RT.anchor.bias;

    function isRetrace(p) {
      var t = tipAt(p), rad = t.w * 1.2;
      for (var j = 0; j < t.kI - 4; j++) {
        if (Math.hypot(RT.points[j][0] - t.x, RT.points[j][1] - t.y) < rad) return true;
      }
      return false;
    }

    var STEPS = 200, rows = [], prev = null, runs = [], open = null;
    for (var i = 0; i <= STEPS; i++) {
      var p = i / STEPS, t = tipAt(p), s = scaleAt(p), rt = isRetrace(p);
      var cam = { x: (t.x + (SC.x - t.x) * bias) * s, y: (t.y + (SC.y - t.y) * bias) * s };
      var tip = { x: (t.x - SC.x) * bias * s, y: (t.y - SC.y) * bias * s };
      if (rt && open === null) open = p;
      if (!rt && open !== null) { runs.push([open, p]); open = null; }
      if (prev) {
        rows.push({ p: p, s: s,
          cam: Math.hypot(cam.x - prev.cam.x, cam.y - prev.cam.y),
          tip: Math.hypot(tip.x - prev.tip.x, tip.y - prev.tip.y),
          du: (indexAt(p) - indexAt((i - 1) / STEPS)) * (RT.length / N), rt: rt });
      }
      prev = { cam: cam, tip: tip };
    }
    if (open !== null) runs.push([open, 1]);

    function stat(arr) {
      var mean = arr.reduce(function (a, b) { return a + b; }, 0) / arr.length;
      return { min: Math.min.apply(null, arr), max: Math.max.apply(null, arr), mean: mean };
    }
    var cam = stat(rows.map(function (r) { return r.cam; }));
    var tip = stat(rows.map(function (r) { return r.tip; }));
    var out = ['  p     zoom   相机平移   笔尖画面   已画增量  重描',
               '               px/1%p    px/1%p      单位'];
    for (var j = 0; j < rows.length; j += 8) {
      var r = rows[j];
      out.push(' ' + r.p.toFixed(3) + '  ' + r.s.toFixed(3) + '  ' +
               r.cam.toFixed(1).padStart(8) + '  ' + r.tip.toFixed(1).padStart(8) + '  ' +
               r.du.toFixed(0).padStart(9) + '  ' + (r.rt ? '是' : ''));
    }
    out.push('');
    out.push('相机平移 均' + cam.mean.toFixed(1) + ' 最快' + cam.max.toFixed(1) +
             '（' + (cam.max / cam.mean).toFixed(2) + '×）最慢' + cam.min.toFixed(1) +
             '（' + (cam.min / cam.mean).toFixed(2) + '×）');
    out.push('笔尖画面 均' + tip.mean.toFixed(1) + ' 最快' + tip.max.toFixed(1) +
             '（' + (tip.max / tip.mean).toFixed(2) + '×）最慢' + tip.min.toFixed(1) +
             '（' + (tip.min / tip.mean).toFixed(2) + '×）');
    out.push('重描总占比 ' +
             (runs.reduce(function (a, b) { return a + (b[1] - b[0]); }, 0) * 100).toFixed(1) + '%');
    runs.forEach(function (b) {
      out.push('  重描 p ' + b[0].toFixed(3) + ' → ' + b[1].toFixed(3));
    });
    document.getElementById('diag').textContent = out.join(NL);
  }
})();
