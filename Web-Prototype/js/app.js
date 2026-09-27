/* ─────────────────────────────────────────────────────────────────────
 * app.js — 智塾 application shell
 * Shared behaviour for every signed-in screen:
 *   · sidebar drawer (mobile) + automatic current-page highlight
 *   · tabs, single-select chip groups, text filtering
 *   · progress bars, stat count-up, toasts, copy-to-clipboard, dialogs
 * Convention: <body data-page="courses"> drives the nav highlight.
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ── Sidebar drawer ─────────────────────────────────────────────── */
  var app = document.getElementById('app');
  var menuBtn = document.getElementById('menuBtn');
  if (app && menuBtn) {
    menuBtn.addEventListener('click', function () {
      app.setAttribute('data-nav', app.getAttribute('data-nav') === 'open' ? 'closed' : 'open');
    });
    app.addEventListener('click', function (e) {
      if (app.getAttribute('data-nav') === 'open' && !e.target.closest('.sidebar') && !e.target.closest('#menuBtn')) {
        app.setAttribute('data-nav', 'closed');
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') app.setAttribute('data-nav', 'closed');
    });
  }

  /* ── Current page highlight ─────────────────────────────────────── */
  var page = document.body.getAttribute('data-page');
  if (page) {
    document.querySelectorAll('[data-nav]').forEach(function (el) {
      if (el.getAttribute('data-nav') === page) el.setAttribute('aria-current', 'page');
    });
  }

  /* ── Toast ──────────────────────────────────────────────────────── */
  var host = null;
  function toast(message) {
    if (!host) {
      host = document.createElement('div');
      host.className = 'toast-host';
      host.setAttribute('role', 'status');
      document.body.appendChild(host);
    }
    var el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = '<span class="dot dot--live"></span><span></span>';
    el.lastElementChild.textContent = message;
    host.appendChild(el);
    window.setTimeout(function () {
      el.style.transition = 'opacity 160ms linear';
      el.style.opacity = '0';
      window.setTimeout(function () { el.remove(); }, 180);
    }, 2600);
  }
  window.zhishuToast = toast;

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-toast]');
    if (t) toast(t.getAttribute('data-toast'));
  });

  /* ── Tabs ───────────────────────────────────────────────────────── */
  document.querySelectorAll('[data-tabs]').forEach(function (group) {
    var tabs = group.querySelectorAll('[data-tab]');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        var key = tab.getAttribute('data-tab');
        tabs.forEach(function (other) {
          other.setAttribute('aria-selected', String(other === tab));
        });
        document.querySelectorAll('[data-panel]').forEach(function (panel) {
          if (panel.getAttribute('data-panel-group') && panel.getAttribute('data-panel-group') !== group.getAttribute('data-tabs')) return;
          panel.hidden = panel.getAttribute('data-panel') !== key;
        });
      });
    });
  });

  /* ── Single-select chip groups ──────────────────────────────────── */
  document.querySelectorAll('[data-chip-group]').forEach(function (group) {
    var chips = Array.prototype.slice.call(group.querySelectorAll('.chip'));
    chips.forEach(function (chip) {
      chip.setAttribute('aria-pressed', chip.hasAttribute('data-selected') ? 'true' : 'false');
      chip.addEventListener('click', function () {
        var multi = group.hasAttribute('data-multi') && group.getAttribute('data-multi') !== 'false';
        if (!multi) {
          chips.forEach(function (o) { o.setAttribute('aria-pressed', 'false'); });
          chip.setAttribute('aria-pressed', 'true');
        } else {
          chip.setAttribute('aria-pressed', chip.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
        }
        if (group.hasAttribute('data-filter-target')) applyFilters(group.getAttribute('data-filter-target'));
      });
    });
  });

  /* ── Filtering (chips + free text) ──────────────────────────────── */
  function applyFilters(scopeSelector) {
    var scope = document.querySelector(scopeSelector);
    if (!scope) return;
    var items = scope.querySelectorAll('[data-filter-item]');
    var active = [];
    document.querySelectorAll('[data-filter-target="' + scopeSelector + '"] .chip[aria-pressed="true"]').forEach(function (c) {
      if (c.getAttribute('data-filter-value')) active.push(c.getAttribute('data-filter-value'));
    });
    var input = document.querySelector('[data-filter-input="' + scopeSelector + '"]');
    var q = input ? input.value.trim().toLowerCase() : '';
    var shown = 0;
    items.forEach(function (item) {
      var tags = (item.getAttribute('data-tags') || '').split(/\s+/);
      var okChip = !active.length || active.some(function (v) { return tags.indexOf(v) !== -1; });
      var text = item.textContent.toLowerCase();
      var okText = !q || text.indexOf(q) !== -1;
      var visible = okChip && okText;
      item.hidden = !visible;
      if (visible) shown++;
    });
    var empty = scope.querySelector('[data-empty-state]') || document.querySelector('[data-empty-state]');
    if (empty) empty.hidden = shown !== 0;
    var count = document.querySelector('[data-result-count="' + scopeSelector + '"]');
    if (count) count.textContent = shown + ' 门';
  }
  window.zhishuApplyFilters = applyFilters;

  document.querySelectorAll('[data-filter-input]').forEach(function (input) {
    var scope = input.getAttribute('data-filter-input');
    input.addEventListener('input', function () { applyFilters(scope); });
  });

  /* ── Progress bars ──────────────────────────────────────────────── */
  document.querySelectorAll('[data-progress]').forEach(function (bar) {
    var value = Math.max(0, Math.min(100, parseFloat(bar.getAttribute('data-progress')) || 0));
    var fill = bar.querySelector('span');
    if (!fill) return;
    fill.style.width = '0%';
    if (reduced) { fill.style.width = value + '%'; return; }
    window.setTimeout(function () { fill.style.width = value + '%'; }, 120);
  });

  /* ── Stat count-up ──────────────────────────────────────────────── */
  document.querySelectorAll('[data-count-to]').forEach(function (el) {
    var to = parseFloat(el.getAttribute('data-count-to'));
    var suffix = el.getAttribute('data-count-suffix') || '';
    var decimals = parseInt(el.getAttribute('data-count-decimals') || '0', 10);
    if (reduced || !isFinite(to)) { el.textContent = to.toFixed(decimals) + suffix; return; }
    var start = performance.now();
    var dur = 700;
    (function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - t, 3);
      el.textContent = (to * eased).toFixed(decimals) + suffix;
      if (t < 1) requestAnimationFrame(step);
    })(start);
  });

  /* ── Copy ───────────────────────────────────────────────────────── */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-copy]');
    if (!btn) return;
    var value = btn.getAttribute('data-copy');
    var done = function () { toast('已复制到剪贴板'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, fallback);
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = value;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (err) { toast('复制失败，请手动选择'); }
      ta.remove();
    }
  });

  /* ── Dialogs ────────────────────────────────────────────────────── */
  document.addEventListener('click', function (e) {
    var open = e.target.closest('[data-dialog-open]');
    if (open) {
      var dlg = document.getElementById(open.getAttribute('data-dialog-open'));
      if (dlg && dlg.showModal) { dlg.showModal(); return; }
      if (dlg) dlg.setAttribute('open', '');
    }
    var close = e.target.closest('[data-dialog-close]');
    if (close) {
      var d = close.closest('dialog');
      if (d && d.close) d.close(); else if (d) d.removeAttribute('open');
    }
  });

  /* ── Composer auto-grow ─────────────────────────────────────────── */
  document.querySelectorAll('[data-autogrow]').forEach(function (ta) {
    var grow = function () {
      ta.style.height = 'auto';
      ta.style.height = Math.min(140, ta.scrollHeight) + 'px';
    };
    ta.addEventListener('input', grow);
    grow();
  });
})();
