/* ─────────────────────────────────────────────────────────────────────
 * agents.js — 四 Agent 运行态看板
 * Advances the task queues for real: each tick finishes the running task
 * of every board and promotes the next queued one. Counters, status
 * badges and dots stay in sync. Pausing freezes the queues on screen.
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var boards = Array.prototype.slice.call(document.querySelectorAll('.agent-board'));
  var toggle = document.getElementById('toggleRun');
  var stepOnce = document.getElementById('stepOnce');
  var runState = document.getElementById('runState');
  var runDelta = document.getElementById('runDelta');
  var statDone = document.getElementById('statDone');
  var statQueued = document.getElementById('statQueued');
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var LABEL = { queued: '排队', running: '进行中', done: '已完成' };
  var running = !reduced;
  var timer = null;
  var done = 121;

  function setState(task, state) {
    task.setAttribute('data-state', state);
    var dot = task.querySelector('[data-task-dot]');
    var badge = task.querySelector('[data-task-badge]');
    if (dot) {
      dot.className = 'dot' + (state === 'running' ? ' dot--live dot--pulse' : state === 'done' ? ' dot--live' : ' dot--idle');
    }
    if (badge) {
      badge.className = 'badge' + (state === 'done' ? ' badge--success' : '');
      badge.textContent = LABEL[state] || state;
    }
  }

  function refreshCounters() {
    var queued = document.querySelectorAll('.task[data-state="queued"]').length;
    statQueued.textContent = queued;
    statDone.textContent = done;
    boards.forEach(function (board) {
      var q = board.querySelectorAll('.task[data-state="queued"]').length;
      var isRunning = board.querySelector('.task[data-state="running"]');
      var status = board.querySelector('[data-board-status]');
      var count = board.querySelector('[data-board-count]');
      if (status) {
        if (!status.dataset.orig) status.dataset.orig = status.textContent;
        status.textContent = isRunning ? status.dataset.orig : (q ? '待命' : '空闲');
      }
      board.setAttribute('data-state', isRunning ? 'busy' : 'idle');
      if (count) count.textContent = count.textContent.replace(/排队 \d+/, '排队 ' + q);
    });
  }

  function tick(silent) {
    var advanced = 0;
    boards.forEach(function (board) {
      var current = board.querySelector('.task[data-state="running"]');
      if (current) { setState(current, 'done'); done++; advanced++; }
      var next = board.querySelector('.task[data-state="queued"]');
      if (next) { setState(next, 'running'); advanced++; }
    });
    refreshCounters();
    if (!silent && advanced && window.zhishuToast) window.zhishuToast('已推进一轮：4 个 Agent 各完成 1 项并接收下 1 项');
  }

  function loop() {
    if (timer) window.clearInterval(timer);
    if (!running) return;
    timer = window.setInterval(function () { tick(true); }, 3200);
  }

  toggle.addEventListener('click', function () {
    running = !running;
    toggle.textContent = running ? '暂停全部 Agent' : '恢复全部 Agent';
    runState.className = 'badge ' + (running ? 'badge--success' : 'badge--warn');
    runState.textContent = running ? '运行中' : '已暂停';
    runDelta.textContent = running ? '自动推进 · 每 3 秒一轮' : '队列停在原地 · 学习数据不再采集';
    if (window.zhishuToast) window.zhishuToast(running ? '四个 Agent 已恢复运行' : '已暂停全部 Agent');
    loop();
  });

  stepOnce.addEventListener('click', function () { tick(false); });

  refreshCounters();
  loop();
})();
