/* ─────────────────────────────────────────────────────────────────────
 * shell.js — injects the shared 智塾 app chrome (sidebar + topbar) so
 * every screen stays consistent. Page-level content stays static HTML.
 * Page contract:
 *   <body data-page="courses" data-crumb="我的课程 / 课程详情">
 *   <div class="app" id="app" data-nav="closed">
 *     <aside class="sidebar"></aside>
 *     <div class="main">
 *       <header class="topbar"></header>
 *       <main class="page"> … </main>
 *     </div>
 *   </div>
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var I = {
    generate: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    open:     '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.7 2.6 15.3 0 18M12 3c-2.6 2.7-2.6 15.3 0 18"/>',
    courses:  '<path d="M4 4h6v16H4zM14 4h6v16h-6z"/>',
    learning: '<circle cx="12" cy="12" r="9"/><path d="M10 8.5 16 12l-6 3.5z"/>',
    agents:   '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
    report:   '<path d="M4 4v16h16"/><path d="M8 17v-5M12 17V8M16 17v-7"/>',
    mistakes: '<path d="M7 4h10v16l-5-3.6L7 20z"/>',
    search:   '<circle cx="11" cy="11" r="7"/><path d="M16 16l4 4"/>',
    menu:     '<path d="M4 7h16M4 12h16M4 17h16"/>',
    bell:     '<path d="M6 9a6 6 0 1 1 12 0v4l1.6 2.6H4.4L6 13z"/><path d="M10 19a2 2 0 0 0 4 0"/>',
    robot:    '<rect x="5.5" y="3.5" width="13" height="11" rx="4.5"/><path d="M9 8.5h.01M15 8.5h.01M9.5 17.5h5M12 14.5v3M4 18.5h16"/>'
  };

  function icon(name, cls) {
    return '<svg class="icon ' + (cls || '') + '" viewBox="0 0 24 24" aria-hidden="true">' + (I[name] || '') + '</svg>';
  }

  var NAV = [
    { group: '学习', items: [
      { key: 'generate', href: 'generate.html', label: '生成课堂', icon: 'generate' },
      { key: 'open',     href: 'open-courses.html', label: '公开课', icon: 'open' },
      { key: 'courses',  href: 'courses.html', label: '我的课程', icon: 'courses', count: 12 },
      { key: 'learning', href: 'learning.html', label: '正在学习', icon: 'learning', count: 3 }
    ]},
    { group: 'Agent 运行', items: [
      { key: 'agents', href: 'agents.html', label: '运行态看板', icon: 'agents' }
    ]},
    { group: '个人中心', items: [
      { key: 'report',   href: 'report.html', label: '学情分析报告', icon: 'report' },
      { key: 'mistakes', href: 'mistakes.html', label: '错题库', icon: 'mistakes', count: 8 }
    ]}
  ];

  function sidebar() {
    var nav = NAV.map(function (section) {
      var items = section.items.map(function (item) {
        return '<a class="nav-item" data-nav="' + item.key + '" href="' + item.href + '">' +
          icon(item.icon) + '<span>' + item.label + '</span>' +
          (item.count ? '<span class="count">' + item.count + '</span>' : '') + '</a>';
      }).join('');
      return '<div class="nav-label">' + section.group + '</div>' + items;
    }).join('');

    return '' +
      '<a class="brand" href="courses.html">' +
        '<span class="brand-mark">' + icon('robot', 'icon-lg') + '</span>' +
        '<span class="brand-name">智塾<small>AI TEACHING PLATFORM</small></span>' +
      '</a>' +
      '<nav class="nav">' + nav + '</nav>' +
      '<div class="side-foot">' +
        '<div class="nudge">' +
          '<p>“今天先做 20 分钟，比计划 2 小时更值钱。”</p>' +
          '<span class="caption">督学 Agent · 每日一句</span>' +
        '</div>' +
        '<div class="side-user">' +
          '<span class="avatar avatar--accent">小T</span>' +
          '<span style="flex:1;min-width:0">' +
            '<span style="display:block;font-size:13px">小 T</span>' +
            '<span class="caption">长期班 · 连续 12 天</span>' +
          '</span>' +
        '</div>' +
      '</div>';
  }

  function topbar() {
    var crumb = (document.body.getAttribute('data-crumb') || '我的课程').split('/');
    var html = crumb.map(function (part, i) {
      var text = part.trim();
      return i === crumb.length - 1 ? '<b>' + text + '</b>' : text;
    }).join('<span aria-hidden="true">/</span>');

    return '' +
      '<button class="icon-btn menu-btn" id="menuBtn" type="button" aria-label="打开导航">' + icon('menu') + '</button>' +
      '<div class="breadcrumb">智塾<span aria-hidden="true">/</span>' + html + '</div>' +
      '<label class="search">' + icon('search', 'icon-sm') +
        '<input type="search" id="globalSearch" placeholder="搜索课程 / 错题 / 知识点" aria-label="全局搜索">' +
      '</label>' +
      '<a class="icon-btn" href="report.html" aria-label="学习简报">' + icon('bell') + '</a>';
  }

  var side = document.querySelector('.sidebar');
  if (side) side.innerHTML = sidebar();
  document.querySelectorAll('.topbar').forEach(function (bar) { bar.innerHTML = topbar(); });

  /* Global search: routes to 公开课 with the query, where filtering is real. */
  var search = document.getElementById('globalSearch');
  if (search) {
    search.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var q = search.value.trim();
      window.location.href = 'open-courses.html' + (q ? '?q=' + encodeURIComponent(q) : '');
    });
  }
})();
