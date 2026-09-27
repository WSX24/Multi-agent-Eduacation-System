/* 生成物 —— 由 tools/build_classic.py 生成，不要手改。源头：js/vendor/interactive-motion.js + js/film-runtime.js */
/* 输入摘要 7b1582989012f32f */
(function () {
'use strict';

/* ── 以下来自 js/vendor/interactive-motion.js（ESM 的 export 已去掉）── */
(function () {
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const wrap = (value, length) => ((value % length) + length) % length;
const shortestCircularDelta = (from, to, frameCount) => {
    let delta = wrap(to, frameCount) - wrap(from, frameCount);
    if (delta > frameCount / 2)
        delta -= frameCount;
    if (delta < -frameCount / 2)
        delta += frameCount;
    return delta;
};
const smoothDamp = (current, target, velocity, smoothTime, maxSpeed, deltaTime) => {
    const safeTime = Math.max(0.0001, smoothTime);
    const omega = 2 / safeTime;
    const x = omega * deltaTime;
    const decay = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x);
    const originalTarget = target;
    const maxChange = maxSpeed * safeTime;
    const change = clamp(current - target, -maxChange, maxChange);
    const limitedTarget = current - change;
    const temp = (velocity + omega * change) * deltaTime;
    let nextVelocity = (velocity - omega * temp) * decay;
    let nextPosition = limitedTarget + (change + temp) * decay;
    if ((originalTarget - current > 0) ===
        (nextPosition > originalTarget)) {
        nextPosition = originalTarget;
        nextVelocity = 0;
    }
    return [nextPosition, nextVelocity];
};
function createFrameAnimator(options) {
    const frameCount = Math.max(1, Math.floor(options.frameCount));
    const circular = options.circular ?? false;
    const smoothTime = options.smoothTime ?? 0.11;
    const maxSpeed = options.maxSpeed ?? frameCount * 2;
    const reducedMotion = options.reducedMotion ?? false;
    let position = clamp(options.initialFrame ?? 0, 0, frameCount - 1);
    let target = position;
    let velocity = 0;
    let lastFrame = -1;
    let lastTime = 0;
    let raf = 0;
    let destroyed = false;
    const normalizeFrame = (frame) => circular
        ? wrap(frame, frameCount)
        : clamp(frame, 0, frameCount - 1);
    const render = () => {
        const frame = Math.round(normalizeFrame(position));
        if (frame !== lastFrame) {
            options.render(frame);
            lastFrame = frame;
        }
    };
    const loop = (now) => {
        raf = 0;
        if (destroyed)
            return;
        const deltaTime = lastTime
            ? Math.min((now - lastTime) / 1000, 1 / 30)
            : 1 / 60;
        lastTime = now;
        if (reducedMotion) {
            position = target;
            velocity = 0;
        }
        else {
            [position, velocity] = smoothDamp(position, target, velocity, smoothTime, maxSpeed, deltaTime);
        }
        render();
        if (Math.abs(target - position) > 0.002 || Math.abs(velocity) > 0.002) {
            raf = requestAnimationFrame(loop);
        }
        else if (circular) {
            position = wrap(position, frameCount);
            target = position;
        }
    };
    const schedule = () => {
        if (!raf && !destroyed)
            raf = requestAnimationFrame(loop);
    };
    render();
    return {
        setTarget(frame) {
            const normalized = normalizeFrame(frame);
            target = circular
                ? position + shortestCircularDelta(position, normalized, frameCount)
                : normalized;
            schedule();
        },
        setDirection(x, y, startAngle = -Math.PI * 0.75) {
            const angle = Math.atan2(y, x);
            const turn = Math.PI * 2;
            const normalized = ((angle - startAngle + turn) % turn) / turn;
            this.setTarget(normalized * frameCount);
        },
        setProgress(progress) {
            this.setTarget(clamp(progress, 0, 1) * (frameCount - 1));
        },
        getCurrentFrame() {
            return normalizeFrame(position);
        },
        destroy() {
            destroyed = true;
            if (raf)
                cancelAnimationFrame(raf);
            raf = 0;
        },
    };
}
function createCssSpriteRenderer(element, columns, rows) {
    const safeColumns = Math.max(1, Math.floor(columns));
    const safeRows = Math.max(1, Math.floor(rows));
    element.style.backgroundSize = `${safeColumns * 100}% ${safeRows * 100}%`;
    return (frame) => {
        const column = frame % safeColumns;
        const row = Math.floor(frame / safeColumns);
        const x = safeColumns === 1 ? 0 : (column / (safeColumns - 1)) * 100;
        const y = safeRows === 1 ? 0 : (row / (safeRows - 1)) * 100;
        element.style.backgroundPosition = `${x}% ${y}%`;
    };
}
const segmentRate = (segment, time) => {
    const curve = segment.curve ?? { type: "constant", rate: 1 };
    if (curve.type === "constant")
        return Math.max(0.1, curve.rate);
    const span = Math.max(0.0001, segment.hold - segment.start);
    const progress = clamp((time - segment.start) / span, 0, 1);
    const edgeWeight = Math.cos(Math.PI * progress) ** 2;
    return Math.max(0.1, curve.midRate + (curve.edgeRate - curve.midRate) * edgeWeight);
};
const validateSegments = (segments) => {
    if (!segments.length)
        throw new Error("segments 不能为空");
    segments.forEach((segment, index) => {
        if (!(segment.start <= segment.hold && segment.hold < segment.endExclusive)) {
            throw new Error(`segment ${index} 必须满足 start <= hold < endExclusive`);
        }
        if (index > 0 && segment.start < segments[index - 1].endExclusive) {
            throw new Error(`segment ${index} 与上一段时间范围重叠`);
        }
    });
};
function createSegmentPlayer(options) {
    validateSegments(options.segments);
    const video = options.video;
    const maxState = options.segments.length;
    const frameDuration = Math.max(0.001, options.frameDuration);
    const reducedMotion = options.reducedMotion ?? false;
    const states = options.states ?? [
        { id: "state-0", hold: options.segments[0].start },
        ...options.segments.map((segment, index) => ({
            id: segment.to ?? `state-${index + 1}`,
            hold: segment.hold,
        })),
    ];
    if (states.length !== maxState + 1) {
        throw new Error("states 数量必须等于 segments 数量加一");
    }
    const stateIds = states.map((state) => state.id);
    if (new Set(stateIds).size !== stateIds.length || stateIds.some((id) => !id)) {
        throw new Error("timeline state id 必须非空且唯一");
    }
    if (Math.abs(states[0].hold - options.segments[0].start) > frameDuration / 2) {
        throw new Error("初始 state hold 必须对应第一段 start");
    }
    options.segments.forEach((segment, index) => {
        if (segment.from && segment.from !== states[index].id) {
            throw new Error(`segment ${index} 的 from 与 states 顺序不一致`);
        }
        if (segment.to && segment.to !== states[index + 1].id) {
            throw new Error(`segment ${index} 的 to 与 states 顺序不一致`);
        }
        if (Math.abs(states[index + 1].hold - segment.hold) > frameDuration / 2) {
            throw new Error(`segment ${index} 的 hold 与目标 state 不一致`);
        }
    });
    const resolveState = (state) => {
        if (typeof state === "string") {
            const index = stateIds.indexOf(state);
            if (index < 0)
                throw new Error(`未知 timeline state id：${state}`);
            return index;
        }
        return clamp(Math.round(state), 0, maxState);
    };
    let currentState = resolveState(options.initialState ?? 0);
    let targetState = currentState;
    let running = false;
    let destroyed = false;
    let runToken = 0;
    let scheduledHandle = 0;
    let scheduledWithVideo = false;
    let lastTick = 0;
    const stateTime = (state) => states[state].hold;
    const cancelScheduled = () => {
        if (!scheduledHandle)
            return;
        if (scheduledWithVideo) {
            video.cancelVideoFrameCallback?.(scheduledHandle);
        }
        else {
            cancelAnimationFrame(scheduledHandle);
        }
        scheduledHandle = 0;
    };
    const schedule = (direction, callback) => {
        cancelScheduled();
        if (direction === 1 && video.requestVideoFrameCallback) {
            scheduledWithVideo = true;
            scheduledHandle = video.requestVideoFrameCallback(callback);
        }
        else {
            scheduledWithVideo = false;
            scheduledHandle = requestAnimationFrame(callback);
        }
    };
    const segmentForTime = (time, direction) => {
        if (direction === 1) {
            return (options.segments.find((segment) => time < segment.hold + frameDuration / 2) ?? options.segments[maxState - 1]);
        }
        for (let index = maxState - 1; index >= 0; index -= 1) {
            if (time > options.segments[index].start - frameDuration / 2) {
                return options.segments[index];
            }
        }
        return options.segments[0];
    };
    const removeBoundaryGap = (time, direction, targetTime) => {
        if (direction === 1) {
            for (let index = 1; index < maxState; index += 1) {
                const previousHold = options.segments[index - 1].hold;
                const nextStart = options.segments[index].start;
                if (targetTime > previousHold + frameDuration / 2 &&
                    time >= previousHold - frameDuration / 2 &&
                    time < nextStart)
                    return nextStart;
            }
        }
        else {
            for (let index = maxState - 1; index > 0; index -= 1) {
                const previousHold = options.segments[index - 1].hold;
                const nextStart = options.segments[index].start;
                if (targetTime <= previousHold + frameDuration / 2 &&
                    time > previousHold &&
                    time <= nextStart + frameDuration / 2)
                    return previousHold;
            }
        }
        return time;
    };
    const settle = () => {
        cancelScheduled();
        video.pause();
        video.currentTime = stateTime(targetState);
        currentState = targetState;
        running = false;
        lastTick = 0;
        options.onStateChange?.(currentState);
    };
    const startRun = () => {
        const token = ++runToken;
        cancelScheduled();
        video.pause();
        const targetTime = stateTime(targetState);
        const initialTime = removeBoundaryGap(video.currentTime, targetTime >= video.currentTime ? 1 : -1, targetTime);
        if (initialTime !== video.currentTime)
            video.currentTime = initialTime;
        if (reducedMotion || Math.abs(targetTime - video.currentTime) <= frameDuration / 2) {
            settle();
            return;
        }
        const direction = targetTime > video.currentTime ? 1 : -1;
        running = true;
        lastTick = 0;
        const tick = (now) => {
            scheduledHandle = 0;
            if (destroyed || token !== runToken)
                return;
            const boundedTime = removeBoundaryGap(video.currentTime, direction, targetTime);
            if (boundedTime !== video.currentTime)
                video.currentTime = boundedTime;
            const reached = direction === 1
                ? video.currentTime >= targetTime - frameDuration / 2
                : video.currentTime <= targetTime + frameDuration / 2;
            if (reached) {
                settle();
                return;
            }
            const segment = segmentForTime(video.currentTime, direction);
            const rate = segmentRate(segment, video.currentTime);
            if (direction === 1) {
                video.playbackRate = rate;
            }
            else {
                video.pause();
                const delta = lastTick
                    ? Math.min((now - lastTick) / 1000, 1 / 30)
                    : 1 / 60;
                video.currentTime = Math.max(targetTime, video.currentTime - rate * delta);
            }
            lastTick = now;
            schedule(direction, tick);
        };
        if (direction === 1) {
            const segment = segmentForTime(video.currentTime, direction);
            video.playbackRate = segmentRate(segment, video.currentTime);
            void video
                .play()
                .then(() => {
                if (token !== runToken || destroyed)
                    return;
                schedule(direction, tick);
            })
                .catch((error) => {
                if (token !== runToken || destroyed)
                    return;
                running = false;
                options.onError?.(error);
            });
        }
        else {
            schedule(direction, tick);
        }
    };
    const seekInitialState = () => {
        if (destroyed)
            return;
        video.currentTime = stateTime(currentState);
    };
    if (video.readyState >= 1)
        seekInitialState();
    else
        video.addEventListener("loadedmetadata", seekInitialState, { once: true });
    const handleVideoError = (event) => {
        if (destroyed)
            return;
        cancelScheduled();
        video.pause();
        running = false;
        lastTick = 0;
        options.onError?.(video.error ?? event);
    };
    video.addEventListener("error", handleVideoError);
    return {
        goTo(state) {
            if (destroyed)
                return;
            targetState = resolveState(state);
            startRun();
        },
        step(direction) {
            this.goTo(targetState + direction);
        },
        cancel() {
            runToken += 1;
            cancelScheduled();
            video.pause();
            running = false;
            lastTick = 0;
        },
        getState() {
            return {
                currentState,
                currentStateId: states[currentState].id,
                targetState,
                targetStateId: states[targetState].id,
                playing: running,
                currentTime: video.currentTime,
            };
        },
        destroy() {
            this.cancel();
            destroyed = true;
            video.removeEventListener("loadedmetadata", seekInitialState);
            video.removeEventListener("error", handleVideoError);
        },
    };
}

window.OilMotionVendor = { createFrameAnimator, createCssSpriteRenderer, createSegmentPlayer };
})();

/* ── 以下来自 js/film-runtime.js ── */
(function () {
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
const { createFrameAnimator, createCssSpriteRenderer, createSegmentPlayer } = window.OilMotionVendor;
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

})();

})();
