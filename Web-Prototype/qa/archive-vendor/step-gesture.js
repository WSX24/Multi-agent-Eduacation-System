export function createStepGestureAdapter(options) {
    const threshold = Math.max(1, options.threshold ?? 40);
    const idleMilliseconds = Math.max(16, options.idleMilliseconds ?? 140);
    let accumulated = 0;
    let lockedDirection = 0;
    let programmaticNavigation = false;
    let releaseTimer = 0;
    let destroyed = false;
    const clearRelease = () => {
        if (releaseTimer)
            window.clearTimeout(releaseTimer);
        releaseTimer = 0;
    };
    const reset = () => {
        clearRelease();
        accumulated = 0;
        lockedDirection = 0;
    };
    const scheduleRelease = () => {
        clearRelease();
        releaseTimer = window.setTimeout(reset, idleMilliseconds);
    };
    return {
        feed(delta) {
            if (destroyed ||
                programmaticNavigation ||
                !Number.isFinite(delta) ||
                delta === 0)
                return;
            const direction = delta < 0 ? -1 : 1;
            if (lockedDirection === direction) {
                scheduleRelease();
                return;
            }
            if (lockedDirection === -direction) {
                accumulated = 0;
                lockedDirection = 0;
            }
            if (accumulated && Math.sign(accumulated) !== direction)
                accumulated = 0;
            accumulated += delta;
            scheduleRelease();
            if (Math.abs(accumulated) < threshold)
                return;
            accumulated = 0;
            lockedDirection = direction;
            options.onStep(direction);
        },
        setProgrammaticNavigation(active) {
            programmaticNavigation = active;
            if (active)
                reset();
        },
        reset,
        destroy() {
            destroyed = true;
            reset();
        },
    };
}
