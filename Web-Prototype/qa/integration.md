# 整合记录 — 把「一线」接进真实页面

日期：2026-09-26。原型验证通过后，把运行时从 `pilot/line-prototype.html` 搬进
`landing.html`，让真实页面成为唯一能跑的地方。

---

## 1. 为什么必须做这一步

在此之前，运行时逻辑活在 `pilot/line-prototype.html` 里，而 `js/` 里留着一份
**过期且跑不通**的实现：`js/film-runtime.js` 读 `build/timeline.json` 与
`build/media/final/motion-baked.mp4`，两个都已被归档；`js/landing.js` 还在跑
`TAKE_MS = 4600` 的自时钟 + 锁滚动 + 机器人 + 书架。

也就是说：**事实源说的是 scrubbed 的一条墨线，真实页面跑的是两周前抛弃的
方案**。这正是本项目一直在讲的那类错误——同一件事有两处定义。

另外，SKILL.md 的 Pilot 定义要求「真实页面最终位置挂载」，而当时没做。
所以这一步同时补上了 Pilot 的最后一环。

## 2. 改了什么

| 文件 | 动作 |
| --- | --- |
| `js/line-runtime.js` | **新建**。运行时：把 p 变成画面。逻辑逐行来自原型（已验收），只换宿主 |
| `js/line-runtime.js` 的 API | `window.OilMotionLine = { ready, failed, reason, p, set(p), stats }`。`set` 是纯函数，同样 p 得同样画面 → 可逆按构造成立 |
| `js/landing.js` | **重写**，574 → 约 130 行。只做三件事：滚动→p、文字层进出、锚点导航 |
| `landing.html` | `#film` 一节换成纸 + 墨 + 文字层；脚本换成 `line-spec.js` → `line-runtime.js` → `landing.js` |
| `css/landing.css` | 首屏部分重写。删掉 `.rig*` / `.shelf` / `.book*` / `.anchor*` / `.film-floor` / `.film-cam` / `.film-grid` / `@keyframes ripple-out|puff-out-*`；新增 `.paper*` / `.ink` / `.ribbon*` / 新的 `.film-say` |
| `qa/archive-direction1/` | `film-runtime.js`、旧的 `layout-probe.html` |
| `qa/archive-vendor/` | `js/vendor/` 三个文件（只被旧 film-runtime 使用） |

删掉的交互（**这是内容层面的删除，需要知道**）：机器人、书架六本书、
拖拽投喂、知识核心徽章、落地涟漪与尘土、`#heroHint` 提示。
它们的语义现在由「落笔」本身承担；`assets/robot-k0.png` 与
`source/identity-bible.md` 保留留档，但首屏不再引用。

## 3. 运行时的降级链

| 条件 | 行为 |
| --- | --- |
| 正常运行 | 滚动 → p → 画面。无媒体可加载、无 seek、无 autoplay 限制 |
| `prefers-reduced-motion` 或 ≤980px | 摊平：不绑滚动，只渲染一帧 p=1，字标/CTA 全部呈现 |
| `line-spec.js` 缺失 | 运行时 `fail()` → `.is-static` → 摊平为静态首屏，**不留一条 1000vh 的空白纸** |

## 4. 整合时抓到的问题（都修了）

1. **顶栏把画面推成纯白死区。** `.land-top` 的背景是 `--bg #ffffff` 的 76%
   叠在已经 239 的纸上 → 251，实测顶栏那一条占画面 **7.65%**，超过过曝门限 2%。
   改用纸自己的色调 `#faf8f4` 72%。
2. **文字层的白色雾同样过曝。** `rgba(255,255,255,.94)` 的径向雾把纸推到 255。
   改成用**纸自己的色调** `rgba(250,248,244,.62)` —— 雾的作用是把纤维与格线
   抹平，不是把纸提亮，用纸色就同时做到且不溢出。这是一条值得复用的经验：
   **在亮底上做可读性垫层，要用底色本身，不要用白。**
3. **`line:failed` 事件被漏掉。** `line-runtime.js` 是 `defer`，它在自己的执行期
   就发出 `line:failed`，而 `landing.js`（下一个 defer）的监听还没注册。
   → `landing.js` 启动时补查 `OilMotionLine.failed`。
4. **`progress()` 用 `offsetTop` 不可靠**（会被有 transform 的祖先影响）
   → 改成 `getBoundingClientRect().top + scrollY`。

## 5. 验收结果（真实页面，不是原型）

`?p=` 是 QA 钩子，与旧版 `?film=` 同一约定，正常访问不走它。

| p | 亮纸% | p95 | 均亮 | 过曝% | 紫% | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| 0.00 | 99.5 | 246 | 240 | 0.00 | 0.00 | 通过 |
| 0.20 | 98.6 | 247 | 238 | 0.00 | 0.00 | 通过 |
| 0.40 | 97.8 | 245 | 237 | 0.00 | 0.00 | 通过 |
| 0.62 | 97.6 | 245 | 230 | 0.00 | 0.00 | 通过 |
| 0.80 | 97.7 | 246 | 238 | 0.00 | 0.40 | 通过 |
| 1.00 | 98.0 | 246 | 237 | 0.01 | 0.90 | 通过 |
| 移动端 390×844（摊平） | 96.9 | 245 | 234 | 0.03 | 1.85 | 通过 |
| reduced-motion | 97.6 | 246 | 237 | 0.01 | 0.90 | 通过 |
| 降级页（事实源缺失） | 99.1 | 247 | 241 | 0.01 | 0.00 | 通过 |

p=0.80 与 p=1.00 的紫色来自**页面自己的 CTA 按钮**（`--accent #9333ea`），
不是媒体里的紫；两者同族，这正是合同要求的「媒体与页面唯一的视觉接口」。

DOM 层核对（`?p=1`）：`data-line=vector`、`#rInk` 有 `d`、
`#rGlow` 有 `d`、`#rUnder` 无 `d`（底稿默认关）、`#filmBar` 为 `scaleX(1)`、
`#filmCta` 有 `is-live`。

## 6. 构建产物（2026-09-26 补上）

在此之前，页面直接读 `source/line-spec.js`，运行时用 SVG 的
`getTotalLength` / `getPointAtLength` **在启动时现算**几何（每采样点一次调用），
等于每次打开页面都把「编译」做一遍。

现在分成两件东西：

| | |
| --- | --- |
| `source/line-spec.js` | 人手编辑的**源**（路径、曲线参数、锚点、纸层、时序） |
| `build/line-runtime-data.js` | **派生**产物：等弧长采样点、切线法线、笔宽、屏幕速度补偿积分表，以及其余渲染参数。页面只读它 |
| `tools/build_line.py` | 构建。支持 `--check`：校验产物与源是否一致 |

`--check` 是防「改了源忘了重建」的闸 —— 实测改一个数不重建会被它拦下。

**几何交叉校验**（`qa/length-probe.html`）：把构建产物的路径交给浏览器自己的
`getTotalLength`，与 Python 离散化的结果比对：

```
build.length            28360.0852
browser.getTotalLength  28360.0820
长度相对差              0.0000%
端点最大偏差            0.000 单位
```

两者一致，说明构建产物的几何可信。

## 7. 整合后抓到的最严重的一个 bug：全局 `max-width` 把墨层压变形

`css/base.css` 有一条全局规则：

```css
img, svg { display: block; max-width: 100%; }
```

墨层 `.ink` 的内联宽度是 2400px，被它压到容器宽 1600px。于是 viewBox
2400×1860 在 1600×1860 的盒子里按 `meet` 缩放 0.667 并居中 ——
**整幅画的坐标全错**：landing 上 p=0.62 的墨覆盖率只有 1.84%，
而草稿台同一进度是 4.54%。

修法：`.ink { max-width: none; }`。修好后 landing 与草稿台的墨像素从
「差 39k」变成「只差 6371，且全部落在顶栏遮住的那一条（y<80）」。

**这个 bug 从整合起就存在，而九张门限截图全部通过。** 门限量的是
亮纸占比、p95、过曝、紫色面积 —— 「画在错的位置」仍然是一张合格的纸，
所以它们全都看不见。是靠草稿台与真实页面**互相对照**才暴露的。

两条后续处置：
1. 草稿台现在也加载 `css/tokens.css` 与 `css/base.css`，不再因为
   「少加载了一个样式表」而掩盖页面级问题。
2. ~~门限需要补一条几何检查~~ —— **已做**：`tools/verify_geometry.py`。
   它把构建产物里的采样点按同一个相机变换投到屏幕坐标、光栅化成
   理论上墨应该在的掩模，再与截图里的墨像素求交并比。

   用故障复现验证过它确实补上了盲区（同一张 p=0.62 的截图）：

   | | 正常 | 故障（重新引入 max-width） |
   | --- | --- | --- |
   | `verify_scene` 色调门 | 通过 | **通过** ← 盲区 |
   | `verify_geometry` IoU | 0.925 | **0.015** |
   | 重心偏差 | 6.7 px | **269 px** |
   | 墨面积比 | 1.04 | **0.45** |

   限定：只适用于正常模式。摊平模式（窄屏 / reduced-motion）下 `.film-pin`
   不是 sticky、高度由内容决定，相机取景基准不同，本工具不适用。

## 8. 仍未做

1. **真实滚轮手感验收**：上面用的是 `?p=` 钩子，无头浏览器发不出滚轮事件。
   滚动映射本身是 `scrollY / (height - innerHeight)`，没有别的逻辑，
   但手感必须由人滚过才算。
2. ~~两套宿主~~ —— **已收拢**：草稿台移到 `qa/sandbox/`，而且**不再自己实现渲染**
   （改为加载与 landing.html 相同的运行时），所以几何只有一份定义。
   它只是「无页面装饰的宿主」；`?diag=1` 的速度诊断表也在那里。
