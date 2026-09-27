/* ─────────────────────────────────────────────────────────────────────
 * mistakes.js — 错题库 + 一键解答
 *   · renders the mistake bank from 助教 Agent data (8 items)
 *   · 「一键解答」streams the 答疑 Agent walkthrough: 错因 → 正解 → 同类题
 *   · inline follow-up chat with the 答疑 Agent, context-aware
 * ─────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var LEVEL = {
    weak:   { label: '未掌握', badge: 'badge--danger', tags: 'weak' },
    review: { label: '待复习', badge: 'badge--warn',   tags: 'review' },
    done:   { label: '已掌握', badge: 'badge--success', tags: 'done' }
  };

  var DATA = [
    {
      id: 'q1', subject: '数据结构 · 循环队列', level: 'weak', wrongs: 2, when: '3 天前',
      question: '循环队列中，写出判满与判空的条件，并说明两种实现各自的代价。',
      cause: '你把判满写成 front == rear，和判空条件冲突了。这是概念级错误——不是笔误，所以同类题做对前会一直留在题库。',
      steps: [
        ['先说你错在哪', 'front == rear 既表示空也表示满，程序无法区分这两个状态。真正的矛盾不在公式，而在「用位置关系表达状态」这个思路本身。'],
        ['解法 A · 空一格法', '牺牲一个存储单元：判空 rear == front，判满 (rear + 1) % n == front。容量是 n-1，代价是浪费一个格子。'],
        ['解法 B · size 计数法', '另存一个 size：入队 size++，出队 size--，判满 size == n，判空 size == 0。不浪费空间，代价是多维护一个变量。'],
        ['工厂里怎么选', '容量紧张、读写频繁用 size 计数；只关心队列是否存在，用空一格法更少状态。两种都要能默写。'],
        ['记忆锚点', '「位置关系只能表达两种状态中的一种」——所以要么借一个格子，要么借一个计数器。']
      ],
      same: [
        '若循环队列容量为 8、采用空一格法，最多能存放多少个元素？',
        '用 rear 与 length 两个变量写出判满表达式，并说明 length 的取值范围。',
        '双端队列能否用空一格法判满？如果不能，给出可行方案。'
      ]
    },
    {
      id: 'q2', subject: '线性代数 · 特征值', level: 'weak', wrongs: 1, when: '5 天前',
      question: '求矩阵 A = [[2,1],[1,2]] 的特征值与对应的特征向量，并说明特征向量的方向。',
      cause: '特征值算对了，特征向量只写了「倍数关系」没给方向，被判定为不完整。助教标注：概念半懂。',
      steps: [
        ['错因定位', '你把特征向量写成 k(1,1) 却没说 k≠0，也没有给出归一化后的方向向量。'],
        ['标准步骤', '先解 det(A − λI) = 0 得到 λ₁ = 3、λ₂ = 1；再分别代入 (A − λI)x = 0 解出基础解系。'],
        ['写答案的方式', 'λ = 3 对应特征向量方向 (1,1)/√2；λ = 1 对应 (1,−1)/√2。归一化后方向唯一，避免被扣分。']
      ],
      same: [
        '矩阵 [[1,2],[2,1]] 的特征值与特征向量方向分别是？',
        '若 λ 是 A 的特征值，证明 λ² 是 A² 的特征值。',
        '判断：对称矩阵的特征向量一定两两正交。'
      ]
    },
    {
      id: 'q3', subject: '机器学习 · 梯度下降', level: 'review', wrongs: 1, when: '1 周前',
      question: '学习率过大与过小分别会导致什么现象？给出三种判断学习率是否合适的方法。',
      cause: '只答出「震荡」与「收敛慢」，漏了「合适的学习率」判断方法，属于知识面缺口。',
      steps: [
        ['补全现象', '过大：损失震荡甚至发散；过小：收敛极慢，且容易停在高原区。'],
        ['三种判断方法', '看前 20 步损失曲线是否单调下降；看梯度范数是否在同一量级；用学习率扫描（1e-1 到 1e-5 各跑 100 步）对比。'],
        ['工程建议', '先用扫描找到量级，再上余弦退火，比手动调参稳定得多。']
      ],
      same: [
        '为什么加入动量项能缓解震荡？用一句话解释。',
        '学习率扫描时，损失曲线出现「先降后升」说明什么？',
        '写出余弦退火的学习率表达式。'
      ]
    },
    {
      id: 'q4', subject: '数据结构 · 链表', level: 'weak', wrongs: 2, when: '2 天前',
      question: '原地反转单链表，要求空间复杂度 O(1)。',
      cause: '指针赋值顺序错误：先改 cur.next 再保存 next，导致后半段链表整体丢失。这类错误你犯了两次。',
      steps: [
        ['错因定位', '反转的本质是「先存后改」：必须先把 next 存下来，再改 cur.next 的指向。'],
        ['正确顺序', 'nxt = cur.next; cur.next = prev; prev = cur; cur = nxt; 四步一步都不能换序。'],
        ['边界情况', '空链表与单节点链表直接返回头节点；循环结束返回 prev，不是 cur。']
      ],
      same: [
        '反转链表的前 k 个节点，其余部分保持原序。',
        '判断链表是否有环，并返回入环点。',
        '合并两个有序链表，要求原地复用节点。'
      ]
    },
    {
      id: 'q5', subject: '高等数学 · 中值定理', level: 'review', wrongs: 1, when: '1 周前',
      question: '用拉格朗日中值定理证明：当 x > 0 时，ln(1+x) < x。',
      cause: '构造辅助函数后没有验证闭区间连续、开区间可导两个条件，证明被判定为不严谨。',
      steps: [
        ['补齐条件', '令 f(t) = ln(1+t)，在 [0, x] 上连续、在 (0, x) 内可导，两个条件缺一不可。'],
        ['套用定理', '存在 ξ ∈ (0, x) 使 f(x) − f(0) = f′(ξ)·x，即 ln(1+x) = x/(1+ξ)。'],
        ['收口', '因为 ξ > 0，所以 1/(1+ξ) < 1，于是 ln(1+x) < x。']
      ],
      same: [
        '证明：当 x > 0 时，e^x > 1 + x。',
        '用柯西中值定理证明不等式 (b−a)/b < ln(b/a) < (b−a)/a。',
        '判断：中值定理中的 ξ 是否唯一？举例说明。'
      ]
    },
    {
      id: 'q6', subject: '计算机网络 · TCP', level: 'review', wrongs: 1, when: '2 周前',
      question: '简述 TCP 慢启动与拥塞避免的转换条件，并说明超时后窗口如何变化。',
      cause: '漏掉「快重传触发时窗口减半而非归 1」，把两种降窗情形混为一谈。',
      steps: [
        ['转换条件', '慢启动：cwnd 每 RTT 翻倍，直到达到 ssthresh；此后进入拥塞避免，每个 RTT 加 1。'],
        ['两种降窗', '超时：ssthresh = cwnd/2，cwnd 归 1，重新慢启动。快重传：ssthresh = cwnd/2，cwnd = ssthresh，直接进入拥塞避免。'],
        ['记忆锚点', '「超时归 1，快重传减半」——差别就在要不要重走一遍慢启动。']
      ],
      same: [
        '为什么快重传需要收到 3 个重复 ACK？',
        '画出 cwnd 随 RTT 变化的锯齿曲线，并标注 ssthresh。',
        'BBR 与 Reno 在拥塞判断上的根本差异是什么？'
      ]
    },
    {
      id: 'q7', subject: '数据结构 · 动态数组', level: 'review', wrongs: 1, when: '1 周前',
      question: '动态数组扩容倍数取 2 与 1.5 的差异是什么？为什么工程上更常用 1.5？',
      cause: '只从时间复杂度回答，没有涉及「内存复用」这一层，被判定为回答不完整。',
      steps: [
        ['复杂度相同', '两种倍数的均摊插入复杂度都是 O(1)，这不是差异点。'],
        ['真正的差异', '取 1.5 时，释放的旧块有可能被下一次扩容复用（因为 2 倍增长下新块总是大于此前所有已释放块之和）。'],
        ['结论', '取 1.5 更容易命中内存分配器的空闲块，减少碎片与系统调用。']
      ],
      same: [
        '固定增量扩容（每次 +10）的均摊复杂度是多少？证明。',
        '为什么缩小容量通常要等到使用率低于 1/4？',
        '实现一个支持缩容的动态数组，写出扩容与缩容的触发条件。'
      ]
    },
    {
      id: 'q8', subject: '编译原理 · 文法分析', level: 'done', wrongs: 2, when: '2 周前',
      question: '给定文法，计算各非终结符的 FIRST 集与 FOLLOW 集。',
      cause: '早期把 ε 的处理弄错；最近两次同类题全对，已标记为掌握。',
      steps: [
        ['回顾错点', 'ε 属于 FIRST(A) 时，FOLLOW(A) 要并入 FIRST(后继符号)。'],
        ['当前状态', '同类题连续两次全对，答疑 Agent 已把这道题移出复习队列。']
      ],
      same: [
        '构造该文法的 LL(1) 分析表。',
        '判断该文法是否为 LL(1)，若不是指出冲突。'
      ]
    }
  ];

  var list = document.getElementById('mistakeList');
  var current = DATA[0];

  /* ── List ───────────────────────────────────────────────────────── */
  DATA.forEach(function (item, i) {
    var row = document.createElement('div');
    row.className = 'mistake-row';
    row.setAttribute('data-id', item.id);
    row.setAttribute('data-filter-item', '');
    row.setAttribute('data-tags', LEVEL[item.level].tags);
    row.setAttribute('tabindex', '0');
    row.setAttribute('role', 'button');
    row.innerHTML =
      '<span class="avatar">' + String(i + 1).padStart(2, '0') + '</span>' +
      '<span style="flex:1;min-width:0">' +
        '<span class="mistake-q" style="display:block">' + item.question + '</span>' +
        '<span class="caption" style="display:block;margin-top:6px">' + item.subject + ' · 错误 ' + item.wrongs + ' 次 · ' + item.when + '</span>' +
      '</span>' +
      '<span class="badge ' + LEVEL[item.level].badge + '">' + LEVEL[item.level].label + '</span>';
    row.addEventListener('click', function () { select(item, row); });
    row.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(item, row); }
    });
    list.appendChild(row);
  });

  /* ── Answer panel ───────────────────────────────────────────────── */
  var aSubject = document.getElementById('aSubject');
  var aLevel = document.getElementById('aLevel');
  var aQuestion = document.getElementById('aQuestion');
  var aCause = document.getElementById('aCause');
  var aBody = document.getElementById('aBody');
  var aSameWrap = document.getElementById('aSameWrap');
  var aSame = document.getElementById('aSame');
  var solveBtn = document.getElementById('solveBtn');
  var qaLog = document.getElementById('qaLog');

  function select(item, row) {
    current = item;
    document.querySelectorAll('.mistake-row').forEach(function (r) { r.classList.remove('is-active'); });
    if (row) row.classList.add('is-active');
    aSubject.textContent = item.subject;
    aLevel.className = 'badge ' + LEVEL[item.level].badge;
    aLevel.textContent = LEVEL[item.level].label + ' · 错 ' + item.wrongs + ' 次';
    aQuestion.textContent = item.question;
    aCause.textContent = item.cause;
    aBody.innerHTML = '<p class="body-sm body-muted">点「一键解答」，它会先指出你错在哪一步，再给完整解法，最后附同类型题。</p>';
    aSameWrap.hidden = true;
    aSame.innerHTML = '';
    solveBtn.disabled = false;
    solveBtn.innerHTML = '<svg class="icon icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg>一键解答';
    if (qaLog) {
      qaLog.innerHTML = '<p class="body-sm body-muted">已切到「' + item.subject + '」。问我这道题的任意一步。</p>';
    }
  }

  function renderSame(item) {
    aSameWrap.hidden = false;
    item.same.forEach(function (q, i) {
      var li = document.createElement('li');
      li.innerHTML = '<span>' + String(i + 1).padStart(2, '0') + '</span><span>' + q + '</span>';
      li.style.animation = reduced ? 'none' : 'chip-in 260ms var(--ease-standard)';
      aSame.appendChild(li);
    });
  }

  solveBtn.addEventListener('click', function () {
    var item = current;
    solveBtn.disabled = true;
    solveBtn.textContent = '答疑 Agent 正在解答…';
    aBody.innerHTML = '<span class="skeleton" style="display:block;height:12px;width:60%"></span>' +
      '<span class="skeleton" style="display:block;height:12px;width:88%;margin-top:8px"></span>';
    aSame.innerHTML = '';
    aSameWrap.hidden = true;

    window.setTimeout(function () {
      aBody.innerHTML = '';
      var i = 0;
      (function next() {
        if (i >= item.steps.length) {
          var tail = document.createElement('p');
          tail.className = 'body-sm body-muted';
          tail.style.marginTop = '12px';
          tail.textContent = '以上步骤由答疑 Agent 生成。做对同类题后，这道题会自动从错题库移出。';
          aBody.appendChild(tail);
          renderSame(item);
          solveBtn.disabled = false;
          solveBtn.innerHTML = '<svg class="icon icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg>重新解答';
          if (window.zhishuToast) window.zhishuToast('已解答并列出错因，3 道同类型题已生成');
          return;
        }
        var step = item.steps[i];
        var el = document.createElement('div');
        el.className = 'answer-step';
        el.innerHTML = '<span class="n">' + String(i + 1).padStart(2, '0') + '</span>' +
          '<span><b style="font-weight:600;display:block">' + step[0] + '</b>' + step[1] + '</span>';
        el.style.animation = reduced ? 'none' : 'chip-in 260ms var(--ease-standard)';
        aBody.appendChild(el);
        i++;
        window.setTimeout(next, reduced ? 0 : 240);
      })();
    }, reduced ? 0 : 700);
  });

  document.getElementById('moreSame').addEventListener('click', function () {
    var item = current;
    var extra = [
      '把这 3 道题变成填空题，用于 3 天后的第 2 次复习。',
      '给出一道会暴露同样错因的干扰题，并标注陷阱位置。',
      '用生活类比重新表述这个知识点，控制在 80 字以内。'
    ];
    extra.forEach(function (q, i) {
      var li = document.createElement('li');
      li.innerHTML = '<span>' + String(item.same.length + i + 1).padStart(2, '0') + '</span><span>' + q + '</span>';
      aSame.appendChild(li);
    });
    if (window.zhishuToast) window.zhishuToast('又生成 3 道，已排入 3 天后的复习');
  });

  /* ── Follow-up chat with 答疑 Agent ─────────────────────────────── */
  var qaInput = document.getElementById('qaInput');
  var qaSend = document.getElementById('qaSend');

  function replyFor(text) {
    if (text.indexOf('size') !== -1 || text.indexOf('计数') !== -1) {
      return '因为 size 计数法把「状态」从位置关系里拆了出来：判空看 size == 0，判满看 size == n，两个条件互不干扰，也不需要浪费一个格子。代价只是多维护一个变量，而现代 CPU 上一次自增几乎免费。';
    }
    if (text.indexOf('换一种') !== -1 || text.indexOf('讲法') !== -1) {
      return '换成停车场的例子：车位是环形排的，你把「还有没有空位」记在门口的牌子上（size 计数法），就不用靠两辆车的位置去猜。空一格法相当于永远留一个车位不放车，用「没法停」来表示「满了」。';
    }
    if (text.indexOf('考试') !== -1 || text.indexOf('考') !== -1) {
      return '考试里两种写法都可能被要求。看到「牺牲一个存储单元」就是空一格法；看到「不允许浪费空间」就是 size 计数法。答题时先写判空条件，再写判满条件，顺序反了容易被判逻辑不完整。';
    }
    return '收到。就这道循环队列判满来说，关键只有一句：位置关系只能表达两种状态中的一种。你是想继续挖实现细节，还是让我出一组题验证一下？';
  }

  function ask(text) {
    var q = (text || '').trim();
    if (!q) return;
    var user = document.createElement('p');
    user.style.cssText = 'background:var(--border-soft);border:1px solid var(--border);border-radius:var(--radius-md);padding:9px 12px;font-size:14px';
    user.textContent = q;
    qaLog.appendChild(user);
    qaInput.value = '';
    qaInput.style.height = 'auto';

    var pending = document.createElement('p');
    pending.className = 'body-sm body-muted';
    pending.textContent = '答疑 Agent 正在回答…';
    qaLog.appendChild(pending);
    qaLog.scrollTop = qaLog.scrollHeight;

    window.setTimeout(function () {
      pending.className = 'body-sm';
      pending.style.color = 'var(--fg)';
      pending.textContent = replyFor(q);
      qaLog.scrollTop = qaLog.scrollHeight;
    }, reduced ? 0 : 620);
  }

  qaSend.addEventListener('click', function () { ask(qaInput.value); });
  qaInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(qaInput.value); }
  });

  select(DATA[0], list.querySelector('.mistake-row'));
})();
