import { onBeforeUnmount, onMounted, ref } from 'vue'

const FRAME_COUNT = 241
const FRAME_DURATION = 1 / 24
const FILM_HEIGHT = '320vh'

const rotorSchedule = [
  { inA: null, inB: null, outA: 0.12, outB: 0.2 },
  { inA: 0.16, inB: 0.24, outA: 0.28, outB: 0.36 },
  { inA: 0.32, inB: 0.4, outA: 0.44, outB: 0.52 },
  { inA: 0.48, inB: 0.56, outA: 0.6, outB: 0.68 },
  { inA: 0.64, inB: 0.72, outA: 0.74, outB: 0.8 },
]

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value))

const clamp01 = (value: number) => clamp(value, 0, 1)

const smooth = (start: number, end: number, value: number) => {
  const t = clamp01((value - start) / (end - start))
  return t * t * (3 - 2 * t)
}

function smoothDamp(
  current: number,
  target: number,
  velocity: number,
  smoothTime: number,
  maxSpeed: number,
  deltaTime: number,
): [number, number] {
  const safeTime = Math.max(0.0001, smoothTime)
  const omega = 2 / safeTime
  const x = omega * deltaTime
  const decay = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x)
  const change = clamp(current - target, -maxSpeed * safeTime, maxSpeed * safeTime)
  const limitedTarget = current - change
  const temp = (velocity + omega * change) * deltaTime
  let nextVelocity = (velocity - omega * temp) * decay
  let nextPosition = limitedTarget + (change + temp) * decay

  if ((target - current > 0) === (nextPosition > target)) {
    nextPosition = target
    nextVelocity = 0
  }

  return [nextPosition, nextVelocity]
}

export function useLandingMotion() {
  const film = ref<HTMLElement | null>(null)
  const filmFrame = ref<HTMLElement | null>(null)
  const video = ref<HTMLVideoElement | null>(null)
  const botanyHost = ref<HTMLElement | null>(null)
  const isStatic = ref(true)

  let narrowQuery: MediaQueryList | undefined
  let reducedQuery: MediaQueryList | undefined
  let botany: HTMLElement | null = null
  let scrollRaf = 0
  let frameRaf = 0
  let framePosition = 0
  let frameVelocity = 0
  let targetFrame = 0
  let lastFrame = -1
  let lastFrameTime = 0
  let videoReady = false
  let destroyed = false
  let botanyRevealed = false
  let botanyOffstage: boolean | null = null

  const progress = () => {
    if (!film.value) return 0
    const top = film.value.getBoundingClientRect().top + window.scrollY
    const travel = film.value.offsetHeight - window.innerHeight
    if (travel <= 0) return 1
    return clamp01((window.scrollY - top) / travel)
  }

  const paintCopy = (value: number) => {
    const root = film.value
    if (!root) return

    const rotorItems = root.querySelectorAll<HTMLElement>('.say-item')
    rotorSchedule.forEach((item, index) => {
      const element = rotorItems[index]
      if (!element) return
      const enter = item.inA === null ? 1 : smooth(item.inA, item.inB!, value)
      const leave = smooth(item.outA, item.outB, value)
      const opacity = enter * (1 - leave)
      element.style.setProperty('--t', opacity.toFixed(3))
      element.style.setProperty('--y', `${((1 - enter) * 30 + leave * -26).toFixed(1)}px`)
      element.style.setProperty('--sc', (0.985 + opacity * 0.015).toFixed(4))
    })

    const final = root.querySelector<HTMLElement>('.say-final')
    const staggerItems = root.querySelectorAll<HTMLElement>('.say-final [data-stagger]')
    const finalOpacity = smooth(0.78, 0.9, value)
    final?.style.setProperty('--tf', finalOpacity.toFixed(3))
    staggerItems.forEach((element, index) => {
      const opacity = smooth(0.78 + index * 0.022, 0.9 + index * 0.022, value)
      element.style.setProperty('--t', opacity.toFixed(3))
      element.style.setProperty('--y', `${((1 - opacity) * 16).toFixed(1)}px`)
    })

    root.querySelector('.film-cta')?.classList.toggle('is-live', finalOpacity > 0.85)
    root.style.setProperty('--cue', (1 - smooth(0, 0.05, value)).toFixed(3))
    root.querySelector<HTMLElement>('#filmBar')?.style.setProperty(
      'transform',
      `scaleX(${value.toFixed(4)})`,
    )
  }

  const renderFrame = (frame: number) => {
    const element = video.value
    const normalizedFrame = Math.round(clamp(frame, 0, FRAME_COUNT - 1))
    if (!element || !videoReady || normalizedFrame === lastFrame) return
    lastFrame = normalizedFrame
    element.currentTime = normalizedFrame * FRAME_DURATION
  }

  const runFrameAnimator = (now: number) => {
    frameRaf = 0
    if (destroyed || isStatic.value || !videoReady) return

    const deltaTime = lastFrameTime
      ? Math.min((now - lastFrameTime) / 1000, 1 / 30)
      : 1 / 60
    lastFrameTime = now
    ;[framePosition, frameVelocity] = smoothDamp(
      framePosition,
      targetFrame,
      frameVelocity,
      0.06,
      FRAME_COUNT * 6,
      deltaTime,
    )
    renderFrame(framePosition)

    if (Math.abs(targetFrame - framePosition) > 0.002 || Math.abs(frameVelocity) > 0.002) {
      frameRaf = requestAnimationFrame(runFrameAnimator)
    }
  }

  const scheduleFrameAnimator = () => {
    if (!frameRaf) frameRaf = requestAnimationFrame(runFrameAnimator)
  }

  const updateFromScroll = () => {
    scrollRaf = 0
    if (isStatic.value) return
    const value = progress()
    paintCopy(value)
    targetFrame = value * (FRAME_COUNT - 1)
    scheduleFrameAnimator()
  }

  const scheduleScrollUpdate = () => {
    if (!scrollRaf && !isStatic.value) scrollRaf = requestAnimationFrame(updateFromScroll)
  }

  const setStaticMode = (staticMode: boolean) => {
    isStatic.value = staticMode
    film.value?.classList.toggle('is-static', staticMode)
    if (staticMode) {
      if (scrollRaf) cancelAnimationFrame(scrollRaf)
      if (frameRaf) cancelAnimationFrame(frameRaf)
      scrollRaf = 0
      frameRaf = 0
      paintCopy(1)
      video.value?.pause()
      videoReady = false
      lastFrame = -1
      filmFrame.value?.classList.remove('is-media')
      if (video.value?.getAttribute('src')) {
        video.value.removeAttribute('src')
        video.value.load()
      }
      return
    }

    paintCopy(progress())
    if (video.value && !video.value.getAttribute('src')) {
      videoReady = false
      lastFrame = -1
      video.value.src = '/build/media/final/motion-baked-desktop.mp4'
      video.value.load()
    }
    scheduleScrollUpdate()
  }

  const updateMotionMode = () => {
    setStaticMode(Boolean(narrowQuery?.matches || reducedQuery?.matches))
  }

  const onVideoLoaded = () => {
    if (destroyed || isStatic.value) return
    videoReady = true
    filmFrame.value?.classList.add('is-media')
    framePosition = 0
    frameVelocity = 0
    lastFrame = -1
    targetFrame = progress() * (FRAME_COUNT - 1)
    scheduleFrameAnimator()
  }

  const onVideoError = () => {
    if (destroyed || isStatic.value) return
    videoReady = false
    filmFrame.value?.classList.remove('is-media')
    setStaticMode(true)
  }

  const updateBotany = () => {
    if (!botany) return
    const rect = botany.getBoundingClientRect()
    const viewportHeight = window.innerHeight
    if (!botanyRevealed && rect.top < viewportHeight * 0.88 && rect.bottom > 0) {
      botanyRevealed = true
      botany.classList.add('is-grown')
    }
    const offstage = rect.bottom < -80 || rect.top > viewportHeight + 80
    if (offstage !== botanyOffstage) {
      botanyOffstage = offstage
      botany.classList.toggle('is-offstage', offstage)
    }
  }

  const onReducedMotionChange = () => {
    updateMotionMode()
    if (reducedQuery?.matches && botany) {
      botany.classList.remove('is-armed')
      botany.classList.add('is-grown', 'is-frozen')
    }
  }

  const scrollBotanyIntoView = () => updateBotany()
  const onNavigationClick = (event: Event) => {
    const clickedElement = event.target
    if (!(clickedElement instanceof Element)) return
    const link = clickedElement.closest<HTMLAnchorElement>('[data-scroll-to]')
    const targetId = link?.dataset.scrollTo
    const target = targetId ? document.getElementById(targetId) : null
    if (!link || !target) return
    event.preventDefault()
    window.scrollTo({
      top: target.getBoundingClientRect().top + window.scrollY - 40,
      behavior: reducedQuery?.matches ? 'auto' : 'smooth',
    })
  }

  onMounted(() => {
    if (!film.value || !video.value) return
    film.value.style.setProperty('--film-h', FILM_HEIGHT)

    narrowQuery = window.matchMedia('(max-width: 980px)')
    reducedQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    narrowQuery.addEventListener('change', updateMotionMode)
    reducedQuery.addEventListener('change', onReducedMotionChange)
    video.value.addEventListener('loadeddata', onVideoLoaded)
    video.value.addEventListener('error', onVideoError)
    window.addEventListener('scroll', scheduleScrollUpdate, { passive: true })
    window.addEventListener('resize', scheduleScrollUpdate)
    window.addEventListener('orientationchange', scheduleScrollUpdate)
    document.addEventListener('click', onNavigationClick)

    botany = botanyHost.value?.querySelector<HTMLElement>('.botany') ?? null
    if (botany) {
      const freezeBotany = new URLSearchParams(window.location.search).get('grow') === '1'
      if (freezeBotany) {
        botany.classList.add('is-grown', 'is-frozen')
      } else if (!reducedQuery.matches) {
        botany.classList.add('is-armed')
        window.addEventListener('scroll', scrollBotanyIntoView, { passive: true })
        window.addEventListener('resize', scrollBotanyIntoView)
        window.addEventListener('hashchange', scrollBotanyIntoView)
        updateBotany()
      }
    }

    updateMotionMode()
    if (isStatic.value) paintCopy(1)
  })

  onBeforeUnmount(() => {
    destroyed = true
    if (scrollRaf) cancelAnimationFrame(scrollRaf)
    if (frameRaf) cancelAnimationFrame(frameRaf)
    narrowQuery?.removeEventListener('change', updateMotionMode)
    reducedQuery?.removeEventListener('change', onReducedMotionChange)
    video.value?.removeEventListener('loadeddata', onVideoLoaded)
    video.value?.removeEventListener('error', onVideoError)
    window.removeEventListener('scroll', scheduleScrollUpdate)
    window.removeEventListener('resize', scheduleScrollUpdate)
    window.removeEventListener('orientationchange', scheduleScrollUpdate)
    window.removeEventListener('scroll', scrollBotanyIntoView)
    window.removeEventListener('resize', scrollBotanyIntoView)
    window.removeEventListener('hashchange', scrollBotanyIntoView)
    document.removeEventListener('click', onNavigationClick)
    if (video.value) {
      video.value.pause()
      video.value.removeAttribute('src')
      video.value.load()
    }
  })

  return { film, filmFrame, video, botanyHost, isStatic }
}
