/* ─────────────────────────────────────────────────────────────────────
 * landing.js — 欢迎页首屏的滚动编排
 *
 * 首屏现在只有一件事：把滚动位置变成进度 p，交给 js/film-runtime.js 钉到成片的
 * 对应帧上，再按 p 控制文字层的进出。
 *
 * 与上一版的差别（不是简化，是换了驱动模型）：
 *   · 删掉了自时钟 TAKE_MS 与锁滚动。上一版是「滚一下 = 按播放键，然后整段
 *     按自己的时钟跑完，期间锁住页面」；本版滚轮位置本身就是进度，
 *     输入停止画面就停住。
 *   · 删掉了媒体耦合（window.OilMotionFilm）。本版没有任何媒体。
 *   · 删掉了机器人 / 书架 / 拖拽投喂 / 涟漪尘土 —— 本版没有角色与道具。
 *     这些交互的语义已经由「落笔」本身承担。
 *   · 媒体时间轴不在这里 —— 那是 build/timeline.json（由编译器生成）。
 *     本文件里的 REVEAL 常量是**页面文字层**的进出时机，不是媒体时间轴：
 *     它是页面编排，唯一来源就是下面那一处，别处不再写第二份。
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var film = document.getElementById('film');
  if (!film) return;

  var bar = document.getElementById('filmBar');
  var cta = document.getElementById('filmCta');
  var final = document.getElementById('sayFinal');
  var filmRt = window.OilMotionFilm;
  /* ── 文案编排的唯一来源 ──────────────────────────────────────────────
     用户 2026-09-26 指定：先 AI Teaching Platform，然后依次变换
     课堂定制 → 个性化辅导 → 智教答疑 → 随时学习，末幕四句一起 + 欢迎加入 + 按钮。
     **每一句都有自己的进出场动画**：进场从下往上抬入，退场继续向上退出 ——
     进出是两个方向，不是同一个位移来回。

     窗口是进度 p 的区间（p 由滚动位置给出，全片 10 秒 → 0.10 约等于 1 秒）。
     这不是媒体时间轴（那是 build/timeline.json），是页面编排，只在这里定义。 */
  var SAY_RISE = 30;        // 进场位移 px（从下往上）
  var SAY_EXIT = -26;       // 退场位移 px（继续向上）
  var SAY_ROTOR = [
    { key: 'brand', inA: null, inB: null, outA: 0.12, outB: 0.20 },  // p=0 已在场
    { key: 'p1',    inA: 0.16, inB: 0.24, outA: 0.28, outB: 0.36 },  // 课堂定制
    { key: 'p2',    inA: 0.32, inB: 0.40, outA: 0.44, outB: 0.52 },  // 个性化辅导
    { key: 'p3',    inA: 0.48, inB: 0.56, outA: 0.60, outB: 0.68 },  // 智教答疑
    { key: 'p4',    inA: 0.64, inB: 0.72, outA: 0.74, outB: 0.80 },  // 随时学习
  ];
  var FINAL_IN_A = 0.78, FINAL_IN_B = 0.90;   // 末幕整体浮现
  var FINAL_STAGGER = 0.022;                  // 末幕内逐行错峰
  var CUE_OUT_BY = 0.05;                      // 滚动提示消失
  var REVEAL = { cueOutBy: CUE_OUT_BY };

  var rotorEls = Array.prototype.slice.call(document.querySelectorAll('.say-item'));
  var staggerEls = Array.prototype.slice.call(document.querySelectorAll('#sayFinal [data-stagger]'));

  /* 滚动行程：走完全片所需的滚动距离。3.2 屏。这是本片的编排参数
     （体验量），不是媒体时间轴；唯一来源就是这一处。 */
  var FILM_VH = 320;
  film.style.setProperty('--film-h', FILM_VH + 'vh');

  var flatMQ = window.matchMedia('(max-width: 980px)');
  var reducedMQ = window.matchMedia('(prefers-reduced-motion: reduce)');
  function flat() { return flatMQ.matches || reducedMQ.matches; }

  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
  function smooth(a, b, x) {
    var t = clamp01((x - a) / (b - a));
    return t * t * (3 - 2 * t);
  }

  var current = -1;
  var raf = 0;
  var failed = false;

  /* QA 钩子 —— 只在 URL 显式带 ?p= 时生效，正常访问一律不走这里。
     无头浏览器无法发滚轮事件，没有这个钩子就无法对首屏取证。
     与旧版 film-runtime 的 ?film= 是同一个约定。 */
  var qaP = null;
  if (location.search) {
    var q = new URLSearchParams(location.search);
    if (q.has('p')) qaP = clamp01(parseFloat(q.get('p')) || 0);
    /* ?chrome=0 把顶栏、字标、滚动提示、进度条全部关掉。
       给 tools/verify_geometry.py 用：那几条装饰里也有深色像素
       （字标 #0f0f10、进度条 --accent 亮度约 101），会被算成"墨"，
       把几何门的重心与交并比带偏。几何门只该量画面本身。
       正常访问不走这里。 */
    if (q.get('chrome') === '0') document.documentElement.dataset.chrome = 'off';
    /* ?hide=text|video（可逗号组合）：把某一层藏掉再截图，用于把文字与影片分开量。
       影片里本来就有大量深灰结构，不藏掉就量不出文字的位置、颜色和渐变。
       与 ?chrome=0 / ?p= 同一类 QA 钩子，正常访问不走。 */
    var hide = (q.get('hide') || '').split(',');
    if (hide.indexOf('text') >= 0) document.documentElement.dataset.hideText = '1';
    if (hide.indexOf('video') >= 0) document.documentElement.dataset.hideVideo = '1';
  }

  /* p 的来源：仅滚动位置。首屏是文档第一节，所以直接量「pin 走完了多少」，
     而不是用 window.scrollY —— 这样即使前面将来插了别的内容也不会错位。 */
  function progress() {
    // 用 rect 而不是 offsetTop：offsetTop 会被有 transform 的祖先影响
    var top = film.getBoundingClientRect().top + window.scrollY;
    var travel = film.offsetHeight - window.innerHeight;
    if (travel <= 0) return 1;
    return clamp01((window.scrollY - top) / travel);
  }

  function paint(p) {
    // 依次变换的五句：各自算可见度 t 与位移 y
    for (var i = 0; i < rotorEls.length && i < SAY_ROTOR.length; i++) {
      var cfg = SAY_ROTOR[i], el = rotorEls[i];
      var tIn = cfg.inA === null ? 1 : smooth(cfg.inA, cfg.inB, p);
      var tOut = smooth(cfg.outA, cfg.outB, p);
      var t = tIn * (1 - tOut);
      el.style.setProperty('--t', t.toFixed(3));
      el.style.setProperty('--y',
        ((1 - tIn) * SAY_RISE + tOut * SAY_EXIT).toFixed(1) + 'px');
      el.style.setProperty('--sc', (0.985 + t * 0.015).toFixed(4));
    }

    // 末幕：整体浮现 + 逐行错峰
    var tf = smooth(FINAL_IN_A, FINAL_IN_B, p);
    if (final) final.style.setProperty('--tf', tf.toFixed(3));
    for (var k = 0; k < staggerEls.length; k++) {
      var tk = smooth(FINAL_IN_A + k * FINAL_STAGGER,
                      FINAL_IN_B + k * FINAL_STAGGER, p);
      staggerEls[k].style.setProperty('--t', tk.toFixed(3));
      staggerEls[k].style.setProperty('--y', ((1 - tk) * 16).toFixed(1) + 'px');
    }
    // 不可见的按钮不该能点，所以指针事件跟着揭示状态走
    if (cta) cta.classList.toggle('is-live', tf > 0.85);

    var cue = 1 - smooth(0, REVEAL.cueOutBy, p);
    film.style.setProperty('--cue', cue.toFixed(3));
    if (bar) bar.style.transform = 'scaleX(' + p.toFixed(4) + ')';
  }

  function apply() {
    raf = 0;
    var p = (failed || flat()) ? 1 : (qaP !== null ? qaP : progress());
    if (p === current) return;
    current = p;
    paint(p);
    if (filmRt && filmRt.ready) filmRt.set(p);
  }

  function schedule() { if (!raf) raf = requestAnimationFrame(apply); }

  /* 摊平模式（窄屏 / prefers-reduced-motion）：不绑定滚动，直接呈现最终状态。
     只画一帧，不跑循环，也不做视差。 */
  function settle() {
    current = -1;
    if (failed || flat()) {
      film.style.setProperty('--cue', '0');
      rotorEls.forEach(function (el) { el.style.setProperty('--t', '0'); el.style.setProperty('--y', '0px'); });
      if (final) final.style.setProperty('--tf', '1');
      staggerEls.forEach(function (el) { el.style.setProperty('--t', '1'); el.style.setProperty('--y', '0px'); });
      if (cta) cta.classList.add('is-live');
      if (bar) bar.style.transform = 'scaleX(1)';
      if (filmRt && filmRt.ready) filmRt.set(1);
      current = 1;
      return;
    }
    schedule();
  }

  /* 运行时失败（唯一事实源缺失等）：静态降级 —— 文字层全部呈现、进度满格、
     不锁滚动。次要动画绝不阻塞导航。 */
  /* 运行时失败（唯一事实源缺失、浏览器不支持等）：**摊平成静态首屏**，
     而不是留一条 1000vh 的空白纸让人滚过去。次要动效绝不阻塞导航。 */
  window.addEventListener('film:failed', function () {
    failed = true;
    film.classList.add('is-static');
    settle();
  });
  /* 就绪时把去重状态清掉再重放一次：apply() 里有 `p === current 就 return`，
     所以「模块未就绪时先设过一次初值」这种情况会把真正的 set 挡掉。 */
  window.addEventListener('film:ready', function () { current = -1; schedule(); });

  window.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('resize', settle);
  window.addEventListener('orientationchange', settle);
  flatMQ.addEventListener('change', settle);
  reducedMQ.addEventListener('change', settle);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(settle);

  /* film-runtime 是 module（异步执行），它的 fetch 失败会在自己的执行期发出
     film:failed，而那时本文件的监听可能还没注册 —— 事件会被错过。
     所以启动时必须再查一次状态。 */
  if (filmRt && filmRt.failed) {
    failed = true;
    film.classList.add('is-static');
  }

  if (qaP !== null) {
    // QA 模式不绑定滚动，直接钉在指定进度等待取证
    /* 就绪时把去重状态清掉再重放一次：apply() 里有 `p === current 就 return`，
     所以「模块未就绪时先设过一次初值」这种情况会把真正的 set 挡掉。 */
  window.addEventListener('film:ready', function () { current = -1; schedule(); });
    schedule();
  } else {
    settle();
  }

  /* ── 锚点导航（沿用 window.scrollTo，不用 scrollIntoView ──────────
     scrollIntoView 会连带滚动预览容器，把整页位置带偏。） */
  Array.prototype.forEach.call(document.querySelectorAll('[data-scroll-to]'), function (link) {
    link.addEventListener('click', function (e) {
      var el = document.getElementById(link.getAttribute('data-scroll-to'));
      if (!el) return;
      e.preventDefault();
      window.scrollTo({
        top: el.getBoundingClientRect().top + window.scrollY - 40,
        behavior: reducedMQ.matches ? 'auto' : 'smooth',
      });
    });
  });
})();

/* ══ 结尾那丛草与花 ═══════════════════════════════════════════════════
   只做两件事：进视口时让它长出来（草叶沿自己的路径画出来），离屏时把风摆暂停。

   为什么不用 IntersectionObserver：实测在无头 Chrome 里，本页的 IO 只在
   载入那一帧投递一次，之后滚动不再回调（同一个会话里换个最小页面就正常），
   取证不可复现，也没法证明它在真浏览器里一定准时。而这一页本来就是
   「滚动位置即状态」的驱动模型（见首屏的 progress()），这里沿用同一个模型：
   自己量 rect。少一个 API，行为可测、可复现。

   降级原则与首屏一致：**次要动画绝不阻塞内容**。CSS 的默认状态就是「长好的」，
   本文件只负责先按回去（.is-armed）再放出来（.is-grown）。所以 JS 挂了不会
   让草消失 —— 它只是一片安静的、完整的草。

   QA 钩子：?grow=1 直接长好并冻结所有摆动。风摆是无限动画，不冻结的话
   每次截图都扠在不同相位上，取证不可复现。 */
(function () {
  'use strict';

  var botany = document.querySelector('.botany');
  if (!botany) return;

  var reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)');
  var revealed = false, offstage = null;

  /* 固定状态：长好 + 冻住。 */
  if (new URLSearchParams(location.search).get('grow') === '1') {
    botany.classList.add('is-grown', 'is-frozen');
    return;
  }

  /* 开了「减少动效」就完全不进生长流程：一直保持 CSS 的最终状态。 */
  if (reduceMQ.matches) return;

  botany.classList.add('is-armed');

  function grow() { revealed = true; botany.classList.add('is-grown'); }

  function update() {
    var r = botany.getBoundingClientRect();
    var vh = window.innerHeight || 0;
    /* 揭示线放在视口上方 12% 处：草刚探进画面就开始长，而不是等到它占满屏幕。
       同时量 top 与 bottom，这样从锚点直接跳到底部、或把窗口拉高也成立。 */
    if (!revealed && r.top < vh * 0.88 && r.bottom > 0) grow();
    /* 离屏（上下各留 80px 余量）就停掉 40 多条无限摆动，别在看不见的地方烧合成器。 */
    var off = r.bottom < -80 || r.top > vh + 80;
    if (off !== offstage) { offstage = off; botany.classList.toggle('is-offstage', off); }
  }

  window.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  /* hashchange：从别处跳到 #cta 时，滚动可能发生在监听器绑定之前 */
  window.addEventListener('hashchange', update);
  update();   // 载入时可能已经在视口里（从 #cta 锚点进来、或窗口很高）
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(update);

  /* 运行中把「减少动效」打开：立刻长好并停摆，不留半截动画。 */
  reduceMQ.addEventListener('change', function () {
    if (!reduceMQ.matches) return;
    botany.classList.remove('is-armed');
    grow();
  });
})();
