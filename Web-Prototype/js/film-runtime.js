/* ─────────────────────────────────────────────────────────────────────
 * film-runtime.js — 烘焙视频的 scrub 运行时（baked-video / frame-scrub）
 *
 * 唯一事实源是构建产物，页面不维护第二份时间常量：
 *   build/timeline.json       帧率、帧数、段落边界、停帧、播放曲线
 *   build/motion-budget.json  交付格式与控制器的自动选择结果
 *
 * 加载方式（踩过坑，别改回去）：createFrameAnimator 来自
 * assets/interactive-motion.ts（由 tsc 编译进 js/vendor/），它是 ESM，
 * 所以这里也是 ESM —— 但**页面不直接加载这个文件**。
 *
 * 原因：<script type="module"> 在 file:// 下会被浏览器直接拒载（模块要求
 * CORS 同源，本地文件算不透明源），脚本根本不会执行，data-film 也就不被设置，
 * 双击打开 HTML 时画面完全不动。而 fetch('build/timeline.json') 在 file://
 * 下同样被 CORS 拦掉 —— 两个坑同时踩。
 *
 * 所以页面加载的是 tools/build_classic.py 生成的经典脚本版本
 * （js/film-runtime.classic.js，内联了 vendor 并去掉 export/import），
 * 时间轴由 build/timeline.js 以 window.OIL_TIMELINE 注入。
 * 本文件仍是唯一事实源；改了它要重跑 python tools/build_classic.py。
 * 与 landing.js（经典脚本）之间照旧用 window.OilMotionFilm 通信。
 *
 * 与渲染器的分工（runtime.md）：
 *   baked-video — 视频自带完整画面，直接由 <video> 显示，不抠色、不合成背景。
 *   程序侧      — 位移、缩放、文字层、进度条；不重复做画面内的形变。
 *
 * frame-scrub 的两条纪律（runtime.md）：
 *   1. 输入事件只更新目标值，实际渲染集中在 requestAnimationFrame。
 *      → 由 createFrameAnimator 负责（带阻尼与反向）。
 *   2. 每次只提交最新整数目标帧，丢弃过时 seek。
 *      → animator 的 render 回调只在**整数帧变化时**触发；这里只在那时写
 *        currentTime，所以堆积的 seek 天然被丢掉。
 *   视频是全关键帧（-g 1），所以每次 seek 都不必从关键帧解码，
 *   见 build/media/compile.json 的 allFramesAreKeyframes。
 * ─────────────────────────────────────────────────────────────────── */
import { createFrameAnimator } from './vendor/interactive-motion.js';

const TIMELINE_URL = 'build/timeline.json';
const POSTER_SRC = 'build/media/final/poster.png';
/* 桌面与移动只在会话初始化时选一次；分页、反向与 resize 期间不换 src
   （runtime.md）。两个文件同为 844×486，只有 CRF 不同，所以移动端用低码率那份。 */
const MEDIA_SRC = {
  desktop: 'build/media/final/motion-baked-desktop.mp4',
  mobile: 'build/media/final/motion-baked-mobile.mp4',
};
const REDUCED_MQ = '(prefers-reduced-motion: reduce)';
const NARROW_MQ = '(max-width: 980px)';

const film = {
  ready: false,
  failed: false,
  reason: '',
  p: 0,
  frame: 0,
  pRequested: null,
  timeline: null,
  video: null,
  animator: null,
  /** 钉到某个进度。未就绪时是空操作。 */
  set() {},
  destroy() {},
};
window.OilMotionFilm = film;

const reduced = () => window.matchMedia(REDUCED_MQ).matches;
const narrow = () => window.matchMedia(NARROW_MQ).matches;

function fail(reason) {
  if (film.failed) return;
  film.failed = true;
  film.ready = false;
  film.reason = reason;
  console.warn('[film] 回退静态画面：', reason);
  document.documentElement.dataset.film = 'static';
  window.dispatchEvent(new CustomEvent('film:failed', { detail: { reason } }));
}

function validateTimeline(data) {
  if (data.schemaVersion !== 1) throw new Error(`timeline schemaVersion ${data.schemaVersion}`);
  if (!Number.isFinite(data.frameCount) || data.frameCount < 1) throw new Error('timeline frameCount 无效');
  if (!Number.isFinite(data.frameDuration) || data.frameDuration <= 0) throw new Error('timeline frameDuration 无效');
  return data;
}

async function loadTimeline() {
  /* 优先用注入的那份：build/timeline.js 会把同一份数据写成 window.OIL_TIMELINE。
     file:// 下 fetch 会被 CORS 拦掉，只能靠它；走服务器时两者都在，
     用同一份以免两份数据打架。 */
  if (window.OIL_TIMELINE) return validateTimeline(window.OIL_TIMELINE);
  const res = await fetch(TIMELINE_URL, { cache: 'force-cache' });
  if (!res.ok) throw new Error(`timeline ${res.status}`);
  return validateTimeline(await res.json());
}

function mountVideo(host) {
  const video = document.createElement('video');
  video.className = 'film-video';
  video.muted = true;                  // 浏览器允许在无手势下 seek/播放的前提
  video.playsInline = true;
  video.setAttribute('playsinline', '');
  video.preload = 'auto';
  video.poster = POSTER_SRC;
  video.setAttribute('aria-hidden', 'true');
  video.disablePictureInPicture = true;
  video.src = narrow() ? MEDIA_SRC.mobile : MEDIA_SRC.desktop;
  host.appendChild(video);
  return video;
}

async function init() {
  const host = document.getElementById('filmFrame');
  if (!host) return;

  // 减少动态效果：完全不初始化媒体，静态 poster 就是最终画面。
  if (reduced()) {
    document.documentElement.dataset.film = 'static';
    return;
  }

  try {
    const timeline = await loadTimeline();
    film.timeline = timeline;
    const video = mountVideo(host);
    film.video = video;

    await new Promise((resolve, reject) => {
      // loadedmetadata 之前不得 seek（runtime.md）
      video.addEventListener('loadedmetadata', resolve, { once: true });
      video.addEventListener('error', () => reject(new Error('媒体加载失败')), { once: true });
      if (video.readyState >= 1) resolve();
    });
    /* 真实落点只能从 seeked 事件读 —— 在 render() 里读 currentTime 读到的是
       上一次 seek 之前的值（seek 是异步的），会得出"seek 没生效"的假结论。 */
    let seeks = 0;
    video.addEventListener('seeked', () => {
      host.dataset.have = video.currentTime.toFixed(4);
      host.dataset.seeks = String(++seeks);
    });

    // 一次性接上 poster 与媒体的交接：媒体有画面之后再把 poster 淡出
    host.classList.add('is-media');

    film.animator = createFrameAnimator({
      frameCount: timeline.frameCount,
      /* 阻尼要快。太慢的话 scrub 会"拖"，而且实测收敛不到目标帧
         （smoothTime 0.09 / maxSpeed 436 时，p=1.00 只走到 92/217）。
         scrub 的手感应当是"跟着滚轮走"，不是"慢慢追过去"。 */
      smoothTime: 0.06,
      maxSpeed: timeline.frameCount * 6,
      reducedMotion: false,
      initialFrame: 0,
      render(frame) {
        film.frame = frame;
        film.p = timeline.frameCount > 1 ? frame / (timeline.frameCount - 1) : 0;
        const t = frame * timeline.frameDuration;
        // QA 观察点：把当前帧与目标时间写进 DOM。
        // 无头环境读不到 video.currentTime（那是属性不是特性），没有这个就无法
        // 判断「没到目标帧」还是「到了但没画出来」。与项目既有的 ?p= 是同一个约定。
        host.dataset.frame = String(frame);
        host.dataset.want = t.toFixed(4);
        host.dataset.rs = String(video.readyState);
        // 只在这里 seek：animator 保证 frame 是整数且只在变化时回调，所以
        // 过时的 seek 不会堆积。容差 1ms 避免同一帧重复写 currentTime。
        if (Math.abs(video.currentTime - t) > 0.001) {
          try { video.currentTime = t; } catch (e) { /* seek 被中断，下一帧会重试 */ }
        }
      },
    });

    /* set 在未就绪时也要记住请求 —— 调用方（landing.js）通常会在模块还没
       loadedmetadata 的时候就先设一次初值，如果这里直接丢弃，那个初值就永远
       补不回来了（实测：帧一直停在 0）。 */
    film.set = (p) => {
      const v = Math.min(1, Math.max(0, p));
      film.pRequested = v;
      if (!film.ready || !film.animator) return;
      film.animator.setProgress(v);
    };

    /* QA 专用直跳：绕过阻尼，直接 seek 到目标帧。
       阻尼在真实浏览器里由约 60 次/秒的 rAF 驱动，收敛很快；但无头环境用
       --virtual-time-budget 时 rAF tick 会被饿死（实测 p=1.00 只走到 175/217 帧），
       于是取证不精确。带 ?p= 时走直跳，正常滚动仍走阻尼。
       与项目既有的 ?chrome=0 是同一类 QA 钩子。 */
    const qaDirect = new URLSearchParams(location.search).has('p');
    film.set = (p) => {
      const v = Math.min(1, Math.max(0, p));
      film.pRequested = v;
      if (!film.ready || !film.animator) return;
      if (qaDirect) {
        const frame = Math.round(v * (film.timeline.frameCount - 1));
        film.animator.setProgress(v);
        film.animator.destroy();
        film.frame = frame;
        film.p = v;
        const t = frame * film.timeline.frameDuration;
        host.dataset.frame = String(frame);
        host.dataset.want = t.toFixed(4);
        try { video.currentTime = t; } catch (e) { /* 忽略 */ }
        return;
      }
      film.animator.setProgress(v);
    };

    film.ready = true;
    if (typeof film.pRequested === 'number') film.set(film.pRequested);
    document.documentElement.dataset.film = 'media';
    window.dispatchEvent(new CustomEvent('film:ready'));
  } catch (error) {
    fail(error && error.message ? error.message : String(error));
  }
}

init();
