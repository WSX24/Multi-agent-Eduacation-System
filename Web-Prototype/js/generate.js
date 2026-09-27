/* ─────────────────────────────────────────────────────────────────────
 * generate.js — 生成课堂
 *   · composer: send a goal, get a streamed reply from the 教师 Agent
 *   · option buttons under the input map to canned agent behaviours
 *   · teaching cycle (短期 / 长期) and difficulty shape the strategy note
 *   · LLM model picker + AI teacher rail with a real create-image dialog
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var log = document.getElementById('chatLog');
  var input = document.getElementById('composerInput');
  var sendBtn = document.getElementById('sendBtn');
  var strategyNote = document.getElementById('strategyNote');
  var teacherName = document.getElementById('chatTeacherName');
  var teacherNote = document.getElementById('chatTeacherNote');
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var state = { teacher: '阿砚', teacherMeta: '默认教师 · 拆解型', model: '智塾 3 Pro', cycle: 'long', level: 'adv', busy: false };

  /* ── Rail sync ──────────────────────────────────────────────────── */
  function syncHeader() {
    teacherName.textContent = state.teacher + ' · 教师 Agent';
    teacherNote.textContent = state.teacherMeta + ' · 模型 ' + state.model;
  }

  function setStrategy(explicit) {
    var text = {
      short: '短期任务：大纲与讲解一次生成完，不保留骨架',
      long: '长期任务：讲解按需生成，难度可随时微调'
    }[state.cycle];
    if (strategyNote) strategyNote.textContent = text + ' · 难度' + (state.level === 'adv' ? '进阶' : '入门');
    if (explicit === 'toast' && window.zhishuToast) window.zhishuToast(text);
  }

  /* ── Message rendering ──────────────────────────────────────────── */
  function addMessage(opts) {
    var wrap = document.createElement('div');
    wrap.className = 'msg' + (opts.role === 'user' ? ' msg--user' : '');
    var avatar = opts.role === 'user' ? '小T' : state.teacher.slice(0, 1);
    wrap.innerHTML =
      '<span class="avatar" aria-hidden="true">' + avatar + '</span>' +
      '<div class="msg-body">' +
        '<div class="msg-author"><b>' + (opts.role === 'user' ? '你' : state.teacher) + '</b>' +
        '<span class="caption">' + (opts.note || (opts.role === 'user' ? '刚刚' : '教师 Agent')) + '</span></div>' +
        '<div class="msg-text" data-text></div>' +
      '</div>';
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    return wrap.querySelector('[data-text]');
  }

  function stream(target, text, rest, done) {
    if (reduced) {
      target.innerHTML = '<p>' + text + '</p>' + (rest || '');
      log.scrollTop = log.scrollHeight;
      if (done) done();
      return;
    }
    var caret = '<span class="stream-caret"></span>';
    var i = 0;
    (function tick() {
      i += 3;
      target.innerHTML = '<p>' + text.slice(0, i) + caret + '</p>';
      log.scrollTop = log.scrollHeight;
      if (i < text.length) { window.setTimeout(tick, 18); return; }
      target.innerHTML = '<p>' + text + '</p>' + (rest || '');
      log.scrollTop = log.scrollHeight;
      if (done) done();
    })();
  }

  /* ── Canned agent answers ───────────────────────────────────────── */
  function planCard() {
    return '<div class="plan-card" style="margin-top:12px"><ol>' +
      '<li><span>U1</span><span>线性结构 · 数组 / 链表 / 栈与队列</span></li>' +
      '<li><span>U2</span><span>树与递归 · 遍历 / 二叉搜索树 / 堆</span></li>' +
      '<li><span>U3</span><span>图与搜索 · 遍历 / 最短路径</span></li>' +
      '<li><span>U4</span><span>动态规划 · 线性 DP / 背包</span></li>' +
      '</ol></div>';
  }

  function answer(prompt) {
    var p = prompt.toLowerCase();
    if (prompt.indexOf('换一种讲法') !== -1) {
      return {
        note: '教师 Agent · 已调整讲法',
        text: '换个入口：先给你一个会出错的实现。',
        rest: '<p class="body-sm body-muted">用数组做队列，每次出队都搬一次数据——先别看复杂度，你说这段代码在 10 万条数据时会发生什么？把结论说出来，我再补线性结构的取舍表。</p>'
      };
    }
    if (prompt.indexOf('讲解') !== -1) {
      return {
        note: '教师 Agent · 生成 U1 讲解',
        text: 'U1 拆成 3 节，每节末尾一道题。',
        rest: '<div class="plan-card" style="margin-top:12px"><ol>' +
          '<li><span>1.1</span><span>数组与动态扩容 · 讲 12 分钟 + 1 题</span></li>' +
          '<li><span>1.2</span><span>链表：单链、双链与循环 · 讲 15 分钟 + 2 题</span></li>' +
          '<li><span>1.3</span><span>栈与队列的工程用法 · 讲 10 分钟 + 2 题</span></li>' +
          '</ol></div>' +
          '<p class="body-sm body-muted" style="margin-top:12px">已生成 1.1 的讲解文字稿。视频按你的播放习惯在今晚 20:00 前生成，避免白做。</p>'
      };
    }
    if (prompt.indexOf('题') !== -1) {
      return {
        note: '教师 Agent · 生成练习',
        text: '5 道进阶题，做完自动交助教 Agent 批改。',
        rest: '<div class="plan-card" style="margin-top:12px"><ol>' +
          '<li><span>01</span><span>给定循环队列，写出判满与判空的三种写法</span></li>' +
          '<li><span>02</span><span>单链表原地反转，要求 O(1) 额外空间</span></li>' +
          '<li><span>03</span><span>用两个栈实现队列，摊还复杂度分析</span></li>' +
          '<li><span>04</span><span>动态数组扩容倍数取 2 与 1.5 的差异</span></li>' +
          '<li><span>05</span><span>找出下面链表操作的时间复杂度上界</span></li>' +
          '</ol></div>' +
          '<p class="body-sm body-muted" style="margin-top:12px">提交后助教 Agent 会给出逐题批注与薄弱点，错题自动进你的错题库。</p>'
      };
    }
    if (prompt.indexOf('大纲') !== -1) {
      return {
        note: '教师 Agent · 已重排大纲',
        text: state.cycle === 'short'
          ? '按 21 天一次成型，排成 4 单元 14 章节，讲解与视频随大纲一起生成。'
          : '按长期班重排：骨架 4 单元，当前单元做实，后面保持骨架。',
        rest: planCard() + '<p class="body-sm body-muted" style="margin-top:12px">' +
          (state.cycle === 'short' ? '短期任务不做滚动生成，注意中途改难度会作废已生成内容。' : '长期任务里，第 3 单元被卡住时我会重排 4 单元的难度，不影响已生成的 1—2 单元。') + '</p>'
      };
    }
    return {
      note: '教师 Agent',
      text: '可以。先确认两件事，再往下走。',
      rest: '<p class="body-sm body-muted">你现在每天能稳定拿出多少分钟？以及这门课是为了考试、面试，还是为了把工作里的问题解决掉？这两个答案会决定我把练习压到多密。</p>'
    };
  }

  function respond(prompt) {
    if (state.busy) return;
    state.busy = true;
    sendBtn.disabled = true;
    var target = addMessage({ role: 'agent', note: '教师 Agent · 正在生成…' });
    target.innerHTML = '<span class="skeleton" style="display:inline-block;width:180px;height:12px"></span>';
    window.setTimeout(function () {
      var a = answer(prompt);
      target.parentNode.querySelector('.msg-author .caption').textContent = a.note;
      stream(target, a.text, a.rest, function () {
        state.busy = false;
        sendBtn.disabled = false;
        markProduced(a.note);
      });
    }, reduced ? 0 : 520);
  }

  function markProduced(note) {
    var map = [
      { match: '讲解', selector: 'U1 讲解' },
      { match: '练习', selector: '练习题' },
      { match: '重排大纲', selector: '大纲' }
    ];
    map.forEach(function (m) {
      if (note.indexOf(m.match) === -1) return;
      document.querySelectorAll('.rail-block').forEach(function (block) {
        var row = Array.prototype.slice.call(block.querySelectorAll('.row')).filter(function (r) {
          return r.textContent.indexOf(m.selector) === 0;
        })[0];
        if (!row) return;
        var badge = row.querySelector('.badge');
        badge.className = 'badge badge--success';
        badge.textContent = '已生成';
      });
    });
  }

  function send(text) {
    var value = (text !== undefined ? text : input.value).trim();
    if (!value || state.busy) return;
    addMessage({ role: 'user', note: '刚刚' }).innerHTML = value;
    input.value = '';
    input.style.height = 'auto';
    respond(value);
  }

  sendBtn.addEventListener('click', function () { send(); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  document.querySelectorAll('[data-prompt]').forEach(function (chip) {
    chip.addEventListener('click', function () { send(chip.getAttribute('data-prompt')); });
  });

  document.getElementById('clearChat').addEventListener('click', function () {
    log.innerHTML = '';
    var t = addMessage({ role: 'agent', note: '教师 Agent' });
    t.innerHTML = '<p>对话已清空。说一个目标，我重新给一份大纲。</p>';
    if (window.zhishuToast) window.zhishuToast('对话已清空');
  });

  /* ── Chips: cycle + level (composer and rail stay in sync) ──────── */
  function paint(selector, key, value) {
    document.querySelectorAll(selector).forEach(function (el) {
      var v = el.getAttribute(key);
      el.setAttribute('aria-pressed', String(v === value));
    });
  }

  document.querySelectorAll('[data-cycle]').forEach(function (chip) {
    chip.addEventListener('click', function () {
      state.cycle = chip.getAttribute('data-cycle');
      paint('[data-cycle]', 'data-cycle', state.cycle);
      paint('[data-cycle-card]', 'data-cycle-card', state.cycle);
      setStrategy('toast');
    });
  });
  document.querySelectorAll('[data-cycle-card]').forEach(function (card) {
    card.addEventListener('click', function () {
      state.cycle = card.getAttribute('data-cycle-card');
      paint('[data-cycle]', 'data-cycle', state.cycle);
      paint('[data-cycle-card]', 'data-cycle-card', state.cycle);
      setStrategy('toast');
    });
  });
  document.querySelectorAll('[data-level]').forEach(function (chip) {
    chip.addEventListener('click', function () {
      state.level = chip.getAttribute('data-level');
      paint('[data-level]', 'data-level', state.level);
      setStrategy();
    });
  });

  /* ── Models ─────────────────────────────────────────────────────── */
  document.querySelectorAll('[data-model]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      state.model = btn.getAttribute('data-model');
      paint('[data-model]', 'data-model', state.model);
      syncHeader();
      if (window.zhishuToast) window.zhishuToast('已切换模型：' + state.model);
    });
  });

  /* ── Teachers ───────────────────────────────────────────────────── */
  function selectTeacher(card) {
    document.querySelectorAll('#teacherList .teacher-card[data-teacher]').forEach(function (c) {
      c.setAttribute('aria-pressed', String(c === card));
    });
    state.teacher = card.getAttribute('data-teacher');
    state.teacherMeta = card.getAttribute('data-meta');
    syncHeader();
  }
  document.querySelectorAll('#teacherList .teacher-card[data-teacher]').forEach(function (card) {
    card.addEventListener('click', function () { selectTeacher(card); });
  });

  document.getElementById('tVoicePreview').addEventListener('click', function () {
    var voice = document.getElementById('tVoice').value;
    if (window.zhishuToast) window.zhishuToast('试听：' + voice + '（原型仅做提示）');
  });

  document.getElementById('teacherForm').addEventListener('submit', function (e) {
    e.preventDefault();
    var name = document.getElementById('tName');
    var err = document.getElementById('tNameError');
    if (name.value.trim().length < 2) {
      err.textContent = '名字至少 2 个字';
      err.hidden = false;
      name.focus();
      return;
    }
    err.hidden = true;
    var gender = document.querySelector('#tGender .chip[aria-pressed="true"]');
    var style = document.querySelector('#tStyle .chip[aria-pressed="true"]');
    var voice = document.getElementById('tVoice').value;
    var meta = '自建教师 · ' + (style ? style.textContent.trim() : '拆解型') + ' · ' + voice +
      (gender ? ' · ' + gender.textContent.trim() : '');

    var card = document.createElement('button');
    card.className = 'teacher-card';
    card.type = 'button';
    card.setAttribute('data-teacher', name.value.trim());
    card.setAttribute('data-meta', meta);
    card.setAttribute('aria-pressed', 'false');
    card.innerHTML =
      '<span class="avatar avatar--lg" aria-hidden="true">' + name.value.trim().slice(0, 1) + '</span>' +
      '<span style="flex:1;min-width:0"><span class="name">' + name.value.trim() + '</span>' +
      '<span class="meta">' + meta + '</span></span>';
    card.addEventListener('click', function () { selectTeacher(card); });

    var list = document.getElementById('teacherList');
    list.insertBefore(card, document.getElementById('addTeacherBtn'));
    selectTeacher(card);
    document.getElementById('createTeacherDialog').close();
    if (window.zhishuToast) window.zhishuToast('已创建教师形象：' + name.value.trim());
    this.reset();
  });

  setStrategy();
  syncHeader();
})();
