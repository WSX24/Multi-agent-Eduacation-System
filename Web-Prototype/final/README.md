# final/ — 交付物清单

**这个目录故意不放文件副本。** 本路线没有编译产物、没有打包步骤，交付物就是
页面本身；把主资源和运行时再拷一份到这里，只会变成第二份事实源
（改了一处忘了另一处），正是本项目一直在避免的东西。

所以这里是一份清单 + 可复现命令，不是副本。

## 交付物（就是这些文件，没有别的）

| 文件 | 角色 |
| --- | --- |
| `landing.html` | 首屏宿主。`#film` 一节是唯一的画面容器 |
| `css/landing.css` | 首屏与正文字体/排布。纸、墨、文字层、摊平降级 |
| `js/line-runtime.js` | 运行时：把进度 p 变成画面（纸、墨、摄影机） |
| `js/landing.js` | 编排：滚动 → p，文字层进出，锚点导航 |
| `build/line-runtime-data.js` | **运行时数据（派生）**：等弧长采样点 / 法线 / 笔宽 / 速度补偿表 + 渲染参数。页面只读它 |
| `source/line-spec.js` | **人手编辑的源**：路径 / 笔宽 / 缩放 / 锚点 / 纸层 / 行程 / 文字时序 |
| `source/paper-fiber.png` | 程序生成的无缝分形纤维板 |
| `source/paper-texture-graded.png` | AI 纸材质板（已调色） |

首屏之后再接 `#agents` / `#loop` / `#cta` 三节与页脚，与首屏无关。

## 静态降级（不需要额外资源）

- `prefers-reduced-motion: reduce` 或视口 ≤980px → 摊平为静态排布，只渲染一帧
- 运行时失败（事实源缺失等）→ 加 `.is-static`，同样摊平，文字与 CTA 全部呈现
- 载入 `source/line-spec.js` 失败是唯一的硬依赖失败点，且已被上面的降级覆盖

**没有媒体可以失败** —— 不加载视频、没有图集、不 seek。这是本路线相对前三版
最实质的差别。

## 运行时数据怎么重新生成

```bash
python tools/build_line.py            # 由 source/line-spec.js 生成 build/line-runtime-data.js
python tools/build_line.py --check    # 校验产物与源是否一致（改了源忘了重建会被拦下）
```

页面**不读** `source/line-spec.js`。源给人改，产物给页面读。

## 两张位图怎么重新生成（都是确定性的）

```bash
python tools/make_fiber.py --out source/paper-fiber.png --report qa/paper-fiber.json
python tools/grade_plate.py source/paper-texture.png   --out source/paper-texture-graded.png --report qa/paper-texture-grade.json
```

`source/paper-texture.png` 是 AI 生成的纸材质原图（一次性资产，留档）。
`tools/grade_plate.py` 的曲线与 `tools/make_fiber.py` 的频率/种子都是写死的，
所以重跑得到同一张图。

## 验收命令

```bash
python -m http.server 8807
# 首屏六帧（?p= 是无头取证的钩子，正常访问不走它）
chrome --headless=new --hide-scrollbars --virtual-time-budget=6000   --window-size=1600,900 --screenshot=qa/page-p0.62.png   "http://localhost:8807/landing.html?p=0.62"
python tools/verify_scene.py qa/page-p0.62.png --no-axis
# 几何门要单独取一张把装饰层关掉的图
chrome --headless=new --hide-scrollbars --virtual-time-budget=6000   --window-size=1600,900 --screenshot=qa/geo-p0.62.png   "http://localhost:8807/landing.html?p=0.62&chrome=0"
python tools/verify_geometry.py qa/geo-p0.62.png --p 0.62   --compare qa/page-p0.62.png
```

两条门管正交的两件事，都要跑：

| 工具 | 管什么 | 曾经漏掉什么 |
| --- | --- | --- |
| `verify_scene.py` | **色调**：亮纸占比 / p95 / 过曝 / 紫色面积 | 漏掉「画在错的位置」—— 那仍然是一张合格的纸 |
| `verify_geometry.py` | **几何**：交并比 / 面积比 / 重心偏差 | 补上上面那条 |

`--compare` 拿带装饰层的同进度截图做对照，量「装饰层把墨压淡了多少」
（门限：墨像素比 ≥0.75）。带装饰的截图不能直接喂给几何门：字标（#0f0f10）
与进度条（--accent，亮度约 101）都会被算成墨，重心会被带偏 30–50px。
所以 `?chrome=0` 把装饰层从 DOM 上关掉，几何门只在画面本身上跑。

校准过程与阈值扫描记录在 `qa/geometry-calibration.md`。

几何门有限定：只适用于**正常模式**。摊平模式（窄屏 / reduced-motion）下
`.film-pin` 不是 sticky、高度由内容决定，相机的取景基准不同，本工具不适用。
