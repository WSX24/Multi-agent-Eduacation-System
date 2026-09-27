/* ─────────────────────────────────────────────────────────────────────
 * film-runtime.js — 时间轴驱动的媒体运行时（baked-video / segment-playback）
 *
 * 唯一事实源是构建产物，页面不维护第二份时间常量：
 *   build/timeline.json                  帧率、段落边界、停帧、播放曲线
 *   build/motion-budget.json             交付格式与控制器的自动选择结果
 *
 * 为什么是 module：`createSegmentPlayer` 来自 assets/interactive-motion.ts
 * （由 tsc 编译进 js/vendor/），它是 ESM。用 <script type="module"> 加载它，
 * 再用 window.OilMotionFilm 与 landing.js（经典脚本）通信，
 * 避免把整个 landing.js 改成模块。
 *
 * 与渲染器的分工（runtime.md）：
 *   baked-video  —— 视频自带完整画面，直接由 <video> 显示，不抠色、不合成背景。
 *   程序侧        —— 位移、缩放、旋转、相机、文字层、徽章；不重复做形变。
 * ─────────────────────────────────────────────────────────────────── */
import { createSegmentPlayer } from './vendor/interactive-motion.js';

const TIMELINE_URL = 'build/timeline.json';
/* 交付媒体是单一版本。编译器的产出分 desktop/mobile 两版，但两者都是 960×960
   （只有 CRF 不同），而移动端最大 CSS 尺寸 186px × DPR3 = 558 设备像素，
   960 的源绰绰有余；桌面端最坏 358px × DPR2 = 716，也仍在 960 之内。
   所以合并为一个文件，省一半体积。重编码记录见 build/media/post-encode.json */
const MEDIA_SRC = 'build/media/final/motion-baked.mp4';
const POSTER_SRC = 'build/media/final/poster.png';
const REDUCED_MQ = '(prefers-reduced-motion: reduce)';

const film = {
  ready: false,        // 时间轴 + 媒体就绪，可以触发
  failed: false,       // 任一步失败 -> 页面回退静态图
  playing: false,
  p: 0,                // 0→1，整段进度；landing.js 的编排读它
  stateId: 'K0',
  timeline: null,
  player: null,
  video: null,
  /** 触发一次整段。未就绪或已在播放时是空操作。 */
  trigger() {},
  destroy() {},
};
window.OilMotionFilm = film;

const reduced = () => window.matchMedia(REDUCED_MQ).matches;

function fail(reason) {
  if (film.failed) return;
  film.failed = true;
  film.ready = false;
  console.warn('[film] 回退静态画面：', reason);
  // 解除页面锁并让调用方知道要显示静态降级；绝不露出半成品媒体。
  window.dispatchEvent(new CustomEvent('film:failed', { detail: { reason } }));
}

async function loadTimeline() {
  const res = await fetch(TIMELINE_URL, { cache: 'force-cache' });
  if (!res.ok) throw new Error(`timeline ${res.status}`);
  const data = await res.json();
  if (data.schemaVersion !== 1) throw new Error(`timeline schemaVersion ${data.schemaVersion}`);
  if (!Array.isArray(data.segments) || !data.segments.length) throw new Error('timeline 无段落');
  if (!Array.isArray(data.states) || data.states.length !== data.segments.length + 1) {
    throw new Error('timeline states 数量必须等于 segments 数量加一');
  }
  return data;
}

function mountVideo() {
  const host = document.getElementById('rigPortrait');
  if (!host) throw new Error('找不到 #rigPortrait');

  const video = document.createElement('video');
  video.className = 'rig-video';
  video.muted = true;                 // 浏览器允许自动播放的前提
  video.playsInline = true;
  video.setAttribute('playsinline', '');
  video.preload = 'metadata';
  video.poster = POSTER_SRC;
  video.setAttribute('aria-hidden', 'true');
  video.disablePictureInPicture = true;
  video.src = MEDIA_SRC;
  // 媒体层在静态图之上；静态图就是降级画面，两者叠在同一几何位置上。
  host.appendChild(video);
  return video;
}

function wirePlayer(timeline, video) {
  const player = createSegmentPlayer({
    video,
    segments: timeline.segments,
    states: timeline.states,
    frameDuration: timeline.frameDuration,
    initialState: timeline.initialState,
    reducedMotion: reduced(),
    onStateChange(state) {
      const segment = timeline.segments[state - 1];
      film.stateId = timeline.states[state] ? timeline.states[state].id : film.stateId;
      if (segment) film.p = timeline.states[state].hold / timeline.segments.at(-1).hold;
      window.dispatchEvent(new CustomEvent('film:state', { detail: { state, id: film.stateId } }));
    },
    onError(error) { fail(error); },
  });
  return player;
}

/** 把 currentTime 映射成 0→1 的整段进度。总时长取最后一段的 hold。 */
function progressFrom(video, timeline) {
  const total = timeline.segments.at(-1).hold || video.duration || 1;
  if (!isFinite(total) || total <= 0) return 0;
  return Math.min(1, Math.max(0, video.currentTime / total));
}

async function init() {
  const host = document.getElementById('rigPortrait');
  if (!host) return;

  // 减少动态效果：完全不初始化媒体，静态图就是最终画面。
  if (reduced()) {
    document.documentElement.dataset.film = 'static';
    return;
  }

  try {
    const timeline = await loadTimeline();
    film.timeline = timeline;
    const video = mountVideo();
    film.video = video;

    await new Promise((resolve, reject) => {
      // loadedmetadata 之前不得 seek 或播放（runtime.md）
      video.addEventListener('loadedmetadata', resolve, { once: true });
      video.addEventListener('error', () => reject(new Error('媒体加载失败')), { once: true });
      if (video.readyState >= 1) resolve();
    });

    film.player = wirePlayer(timeline, video);

    film.trigger = () => {
      if (!film.ready || film.playing) return;
      film.playing = true;
      host.classList.add('is-media');
      try {
        film.player.goTo(timeline.states.at(-1).id);
      } catch (error) { fail(error); }
    };

    film.ready = true;
    document.documentElement.dataset.film = 'media';
    window.dispatchEvent(new CustomEvent('film:ready'));
  } catch (error) {
    fail(error);
  }
}

/** 每帧只读一次 currentTime，写进 film.p；不在事件回调里反复写 DOM。 */
function tick() {
  if (film.ready && film.video && film.playing) {
    film.p = progressFrom(film.video, film.timeline);
    if (film.video.ended || film.video.currentTime >= film.timeline.segments.at(-1).hold - 0.01) {
      film.p = 1;
    }
  }
  requestAnimationFrame(tick);
}

// 页面切后台暂停，回来时以当前媒体时间继续（runtime.md）
document.addEventListener('visibilitychange', () => {
  if (!film.video || !film.playing) return;
  if (document.hidden) film.video.pause();
  else film.video.play().catch(() => {});
});

/* QA 钩子 —— 只在 URL 显式带 ?film= 时生效，正常访问一律不走这里。
   无头浏览器无法发滚轮事件，没有这个钩子就无法对「触发之后」的画面取证。
   用法：?film=2.5 强制显示媒体层并停在 2.5s（暂停），用于截图与逐帧验收；
        ?film=0    停在首帧，用于验证与静态图的几何是否重合。 */
const qa = new URLSearchParams(location.search);
if (qa.has('film')) {
  window.addEventListener('film:ready', () => {
    const host = document.getElementById('rigPortrait');
    const t = Math.max(0, parseFloat(qa.get('film')) || 0);
    film.playing = true;
    host.classList.add('is-media');
    const seek = () => {
      film.video.currentTime = Math.min(t, film.timeline.segments.at(-1).hold);
      film.video.pause();
      film.p = progressFrom(film.video, film.timeline);
      window.dispatchEvent(new CustomEvent('film:qa-seek'));
    };
    film.video.play().then(seek).catch(seek);
  });
}

init();
requestAnimationFrame(tick);
