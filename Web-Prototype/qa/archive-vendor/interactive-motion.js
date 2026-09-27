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
export function createFrameAnimator(options) {
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
export function createCssSpriteRenderer(element, columns, rows) {
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
export function createSegmentPlayer(options) {
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
