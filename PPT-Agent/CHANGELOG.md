# PPT-Agent 优化改动记录

> 日期：2026-10-03
> 范围：`agent.py`、`staged.py`
> 目标：解决「staged 架构比原 ReAct 更慢、更贵、经常产不出 PPT」的问题。

---

## 一、问题诊断

staged 架构上线后，实测 4 页《向量数据库入门》任务出现三种失败/浪费模式：

### 1. Communication trace 误判（导致导出被拒）
- **现象**：`svg_to_pptx` 报 `--quick-generate requires a passing final SVG quality report`，校验器报：
  ```
  [ERROR] Communication trace: ... missing on Slide 01, 03.
  ```
- **根因**：P2.5 文本适配阶段，模型偶发往 `design_spec.md` 的 §IX 里新增
  `### Part 2`、`#### Slide 01 — 长句拆行` 这类标题（"修正记录"段落）。
  skill 校验器把每个 `#### Slide NN` 标题都当成正式页面，发现缺 `Audience move` /
  `Relationships` → 判 blocking → 拒绝导出。
- **性质**：非确定性（同样任务，模型有时直接改原文=成功，有时新增段落=失败）。

### 2. P4 反复修复（最大的时间黑洞）
- **现象**：P4 VERIFY 一轮烧 457s（6 次模型调用），占整轮 722s 的 62%。
- **根因**：P3 写出的 SVG 文字超界 → 校验打回 → 模型反复改坐标。而文字宽度
  计算是像素级算术，恰恰最烧推理 token。

### 3. P2.5 文本适配循环（白烧钱）
- **现象**：P2.5 让模型反复"拆行/缩写"计划文案，实测 3 轮改不动，白烧 61s +
  3 次调用，最后还残留超宽。
- **根因**：拆行需要推理，low 推理下模型缩不动；而这本来就是 P3 的字数上限
  能兜住的，根本不用在计划层修。

### 4. 关思考（thinking disabled）的不确定性
- **现象**：一刀切关思考后，模型"精确遵循规则"能力丢失，非确定性地崩：
  fill spec 乱加 `## forbidden` 行、写页用图标引用、修复时复读机。
- **结论**：`disabled` 不适合 staged（每个阶段都要精确遵循 schema/规则）。

---

## 二、改动清单

### `agent.py`

| 位置 | 改动 | 原因 |
|---|---|---|
| `PPTAgent.__init__` | `thinking` 默认 `"default"`；`reasoning_effort` 默认 `"low"` | 不再一刀切关思考；`low` 是 README 实测「0 错误 + 快 15% + 质量持平」的安全折中 |
| CLI `--thinking` | 默认值 `"default"` | 同上 |
| CLI `--reasoning-effort` | 默认值 `"low"`（原 `None`） | 同上 |

### `staged.py`

| 位置 | 改动 | 原因 |
|---|---|---|
| `FIT_PROMPT` | 加「硬性红线」段 + `{integrity}` 段 | 禁止模型新增 `Slide NN` 标题/Part 2 段落（*注：此 prompt 现已随 P2.5 重写而废弃，见下） |
| `SPEC_REPAIR_PROMPT` | 加「Slide NN 编号唯一性」修正要点 | 教模型遇到 communication trace 报错时删多余块而非补字段 |
| 新增 `_outline_integrity_errors()` | 复刻 skill 校验逻辑 + 检测 Slide 编号重复 | 修 communication trace 误判 |
| 新增 `_per_role_char_budget()` | 从标定表算每字号单行最大汉字数 | 把像素算术从模型手里拿给代码 |
| `PAGE_PROMPT` 第 2 条 | 「内容要充实」→「内容简洁、宁短勿长」 | 降低超界概率 |
| `PAGE_PROMPT` 第 4 条 | 明确禁止 `<use data-icon>` 图标引用 | 避免 "icon not found" blocking 错误 |
| `PAGE_PROMPT` | 新增 `{char_budgets}` 字数上限段 | P3 写页时直接给每行字数硬约束 |
| `REPAIR_PROMPT` | 加「icon not found → 手画原生图形」处理 | 修复时能正确响应图标缺失 |
| `MAX_PAGE_REPAIRS` | `3` → `2` | 减少修复烧钱 |
| `phase_fit_plan()` | **重写**：砍掉模型修复循环，只做纯代码检查 | 消除 P2.5 白烧的 61s |
| `phase_execute()` | 传 `char_budgets` | 让 P3 拿到字数上限 |

---

## 三、核心思路（为什么这样改）

**把「算术」从模型手里拿给代码。** SVG 超界/图标缺失/结构破坏这些校验失败的
根因，都是模型要做"精确计算或精确遵循"，而这恰恰最烧推理 token。

- **字数上限**：代码用 `text_measure` 标定表算好"body 每行 ≤44 字、title ≤24 字…"
  直接喂给模型，模型只需"数字数"，不需要推理 → P3 一次写对 → P4 不用修。
- **结构完整性**：代码直接复刻 skill 的 communication trace 校验，P2.5 纯代码
  检测，发现重复 Slide 编号/缺必填行才调模型修一次。
- **推理强度**：`low` 保留够用的基础推理（填 spec 不乱加行、能数字数），
  又比全开省大量 token；`disabled` 则太激进（连"别乱加行"都守不住）。

---

## 四、效果数据（4 页《向量数据库入门》，同机同模型）

| 指标 | 原 ReAct | staged 初版（思考全开） | **现在** |
|---|---|---|---|
| 总耗时 | 402s | 722s | **203s** |
| 模型调用 | 25 次 | 13 次 | **7 次** |
| 输入 tokens | 2,206,501 | 469,737 | **196,438** |
| 输出 tokens | 93,357 | 177,767 | **48,971** |
| P4 修复 | — | 457s / 6 次 | **1.8s / 0 次** |
| P2.5 适配 | — | 61s / 3 次 | **0s / 0 次** |
| 产出 | 能 | 经常失败 | ✅ 0 错误，4 页全出 |

**结论**：现在 staged 全面优于原 ReAct——耗时快一半、输入 token 省九成、
输出 token 省一半，且稳定产出。

---

## 五、用法（默认即最优）

```bash
# 什么都不用加，默认 = low 推理 + 字数上限 + 结构自检
python agent.py --profile quick "做一份 8 页的《向量数据库入门》PPT"

# 体检
python agent.py --check
```

### 可调旋钮

| 参数 | 默认 | 说明 |
|---|---|---|
| `--reasoning-effort low` | low | 默认，稳定 + 快 15% |
| `--reasoning-effort none` | — | 关思考，快 6~8 倍，但仅适合文字极少的简单 deck（勿用于正式任务） |
| `--thinking default` | default | 跟随提供商；复杂密集排版可改回 `enabled` |

---

## 六、遗留/待办

1. ~~`FIT_PROMPT` 与 `FIT_ROUNDS` 死代码~~ —— 已于 2026-10-04 清理。
2. P2 FREEZE 的 `fill_spec` 仍是模型填脚手架，`low` 推理下偶发乱加 `## forbidden`
   行导致 validate 失败（触发一次 spec_repair 兜底）。如需更稳，可考虑代码生成
   spec_lock 的固定章节，模型只填值。
3. 字数上限目前按"整页可用宽度 × 0.9"估算，多栏布局时仍可能偏宽；如需精细，
   可在 `_per_role_char_budget()` 里按列宽分别计算。

---

## 2026-10-04：提速探索与负优化回退

> 范围：`agent.py`、`staged.py`
> 目标：在「203s / 4 页」基础上进一步提速。

### 已落地且保留的改动（中性 / 正向）

| 改动 | 说明 |
|---|---|
| 死代码清理 | 删除 `FIT_PROMPT`、`FIT_ROUNDS`（原遗留待办第 1 条） |
| `run_command_checked` | `run_command` 拆出结构化退出码 `(exit_code, output)`，`_run_checked` 不再用 `"exit_code=0" in output` 字符串匹配 |
| `_spec_role_args` 去重 | `phase_freeze` 复用该方法，消除手写 roles 列表重复 |
| 公共 helper | `_canvas_size` / `_calibration_roles` / `_usable_width`，消除两处 text_calibration.json 解析与 usable 宽度计算重复 |
| 模型覆盖参数 | `call_model` / `_ask` / `_ask_files` / `_render_page` 支持单次覆盖 `model` / `reasoning_effort` / `thinking`（局部参数、线程安全） |
| 统计加锁 | `time_model` 归集统一进 `call_model`（锁内），修复并发下漏记 |

### 三个「以为能提速、实测更差」的负优化（已回退 / 默认关闭）

| 尝试 | 实测现象 | 结论 |
|---|---|---|
| P3 逐页并行（`--parallel-pages N`） | 4 路并行总耗时反而更长 | 思考型模型单请求已吃满算力，并发只是切分吞吐 + 前缀缓存失效 → 默认改回串行（1） |
| P4 修复关思考（`REPAIR_REASONING_EFFORT="none"`） | 修复快但质量下降，18 个错误的页面 2 轮修不完，导出被拒 | 关思考丢的是排版算术能力，试错比推理更贵 → **已彻底回退** |
| execute 阶段用 `deepseek-v4-pro` | 8 页 P3 耗时 1029s、推理 9.6 万 chunk，8 页全错 | pro 在精确坐标排版上反复纠结（复读机），质量反而不稳定 → execute 勿用 pro |

### 保留的可用旋钮

| 参数 | 默认 | 说明 |
|---|---|---|
| `--parallel-pages N` | 1 | 并行生成；仅适合「输出少、并发友好」的模型/端点 |
| `--plan-model` / `--execute-model` | = `--model` | 按阶段换模型；execute 阶段**勿用思考型强模型** |

### 教训

**提速手段必须区分「模型/端点是否并发友好」「任务是否依赖推理」。**
对 DeepSeek 思考型模型 + 精确排版任务：**串行 + `low` 推理是实测最优**，
「并行」「关思考」「换强模型」均为负优化。

---

## 2026-10-10：导出失败根治——超长文字自动拆行 + 缩短指引

> 范围：`bounds_fix.py`、`staged.py`、`pipeline.py`
> 目标：根治「svg_to_pptx 导出失败」（blocking 错 = 文字超出容器），不再“总出现”。

### 问题诊断

12 页《导数的几何意义》导出被 release gate 拦下：`09_challenge.svg` 的
`challenge-box`（宽 484px）里塞了一行 28 字提示，18px 字号下每字约 19.1px，
水平溢出 25.3%（阈值 5%）→ 判 blocking。

为什么旧代码救不了：容器右边紧贴 `plot-panel`（x=544）、左边已到 x=56 边距，
所以「扩框」撞邻居回滚、「挪位」也放不下 28 字；唯一解是「拆行 / 缩短」，但
`bounds_fix` 只改坐标数字不改内容，模型 P4 又倾向整页重写 → 修复轮次用完，
导出失败。这类「文字超长 + 容器被邻居挡死」是数学课件（布局最密）的高发形态。

### 改动清单

| 文件 | 改动 | 原因 |
|---|---|---|
| `bounds_fix.py` | 新增 `reflow_overflow_text` 第四条策略：超长单行文字拆成多行 `<tspan>` | 把「拆行」这个确定性操作从模型手里拿给代码，0 次模型调用 |
| `bounds_fix.py` | 五道安全边界：仅水平溢出 / 仅单行（无 tspan）/ 仅 CJK 主导 / 拆完不超容器下边界 / 不压下方文字 | 拆行是内容改动，宁可少拆不多拆，避免换一个 checker 看不见的重叠错 |
| `staged.py` | `_auto_fix_bounds` 策略链接上第四条（嵌套→挪→扩→**拆行**） | 复用「快照→改→复验→回滚」框架 |
| `staged.py` | 新增 `_FIT_CHARS_RE`，`_solve_bounds_errors` 解析报错自带的 `≈N chars fit in W px`，给出「缩短到 ≤N 字」指引 | 拆不了的（多行挤一起）给模型明确算术指引，不再瞎扩框 |
| `pipeline.py` | self_check ⑬ 覆盖拆行策略 | 自检文案与实现对齐 |

### 核心思路

延续「把算术从模型手里拿给代码」：
- **拆行**是确定性的（报错自带每字像素、容器宽度），代码直接改，复验回滚兜底；
- **缩短文字**是语义操作、代码不能安全自动做，于是把 checker 已算好的
  「只放得下 N 字」直接喂给修复提示，让模型照着改。

### 验证

- `python bounds_fix.py --selftest`：新增 ⑯ 拆行策略、⑰ 四道安全边界，全绿；
- `python pipeline.py --check`：⑬ 扩到「扩框/挪元素/拆行三条策略」，整体 ✅；
- 真实 `09_challenge.svg` 实测：拆行策略正确拒绝（撞 y=660 结论行），
  `_solve_bounds_errors` 正确吐出「缩短到 ≤25 字」。

---

## 2026-10-11：几何绘图能力大扩展——从「只会 y=f(x)」到「覆盖高中几何」

> 范围：`plot_math.py`、`staged.py`、`agent.py`
> 目标：根治「函数图像之外画不了 / 辅助几何全手画」——切点飘、弦端点不在曲线上、切线飞。

### 问题诊断

`plot_math` 原来只能画 `y=f(x)`（M/L 折线）。实测（圆锥曲线专题、导数专题）：

- **弦端点不在椭圆上**：模型手画弦，端点代入椭圆方程得 0.68 ≠ 1（在椭圆内部）；
- **切线飞了**：切线不过切点（差 68px），或画成圆心高度的割线；
- **曲线画成斜直线/任意贝塞尔**：椭圆/双曲线不是函数，没有代码能力就只能手画；
- **立体几何**：正方体的棱、可见/不可见全靠手画，比例和遮挡都错。

根因一句话：**凡是超出 `plot_math` 能力范围的几何，模型自己算，必然错。**

### 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `plot_math.py` | `plot_geometry` 加 **多切点 `points`** + **尖点 `sharp`** | 覆盖「正负单调」「陡峭」「\|x\| 尖点」 |
| `plot_math.py` | 新增 **`conic_geometry`**：圆/椭圆/双曲线/抛物线（参数方程 + **等比例** + 焦点/顶点） | 椭圆不会变形 |
| `plot_math.py` | conic 加 **曲线上的点 `points`（参数 t）/ 弦 `chords` / 点处切线 `tangents` / 自由点 `free_points` / 线段 `lines`（可带 `arrow`）/ 交点 `intersections` / 过外点切线 `ext_tangents`** | 端点全部精确算好 |
| `plot_math.py` | 新增 **`param_curve_geometry`**：任意参数方程 `x(t), y(t)` | 摆线/心形线等 |
| `plot_math.py` | 新增 **`solid3d_geometry`**：3D 顶点/棱/面 → **正交/透视投影** + **面深度排序** + **隐藏棱判定** + 预设 | 正方体/正四面体/正八面体/棱柱/棱锥 |
| `plot_math.py` | 新增 **`revolution_geometry`**：母线绕轴旋转 → 母线 + 纬线圆 | 圆柱/圆锥/圆台/球 |
| `plot_math.py` | `compile_expr` 支持自定义自变量名（`var="t"`） | 参数曲线用 |
| `staged.py` | `plot_page_figure` 支持 `kind` ∈ function_plot / conic / solid3d / parametric / revolution | 按 kind 分派 |
| `staged.py` | 新增 **`figure_completeness_errors`** + `phase_plan` 打回重填 | 见「三层防线」 |
| `staged.py` | PLAN_PROMPT 五种 kind 的字段说明 + 硬性红线 | 告诉模型必须用哪个 |

### 三层防线

1. **代码能力**（`plot_math`）：坐标全部由代码算，模型照抄；

2. **提示词红线**（PLAN_PROMPT）：出现「切点/切线」必须 `function_plot`；出现「圆/椭圆/双曲线/抛物线」必须 `conic`；出现「正方体/棱柱/立体」必须 `solid3d`；出现「摆线」必须 `parametric`；出现「旋转体」必须 `revolution`；

3. **代码校验**（`figure_completeness_errors`）：P1 出 plan 后扫 roster，凡「内容涉及某类图形、但 figure 未声明对应 kind」的页 → **打回 P1 重填一次**（改善就采纳，没改善保留原计划）。

### 关键实测数据

| 能力 | 验证 |
|---|---|
| 圆的切线 | 切点精确在圆上，切线过切点（模型原来画在圆心高度） |
| 过外点切线（圆/椭圆/双曲/抛） | 各 2 条，切线到外点距离 **0.00px** |
| 弦端点 | 代入椭圆方程 = **1.000**（原来是 0.68） |
| 3D 隐藏棱 | 正方体从上前方看，背面 3 条棱正确判为虚线 |
| 旋转体 | 圆柱母线 + 纬线圆投影正常 |

### 验证

- `python plot_math.py --selftest`：**20 项**全绿（切点/切线/割线、多切点、尖点、圆/椭圆/双曲/抛外点切线、圆锥曲线点/弦/切线/交点、3D 预设/透视/隐藏棱、参数曲线、向量箭头、旋转体）；
- `python pipeline.py --check`：全程通过（⑫ 图像几何覆盖 conic）。

### 中途试过又回退的（便于追溯）

- **圆切线 / 公式排版**：先做了一版（`circle_tangent`、`limit_fraction`）后回退，本轮的 `conic`（含圆）是更完整的替代；
- **`--temperature` 默认 0.4**：试过降波动，回退为 `None`（用提供商默认）；
- **P1/P3 分工实验**（P1 写完整文案、P3 照抄）：导致字体/图像混乱，已回退——结论是「文案与排版强耦合，P1 看不到排版细节，不能提前定死文案」。

### 教训

**几何绘图的价值全在坐标正确性，而模型的坐标算术不可靠——凡是「算出来的东西」（交点、切点、端点、投影），都必须由代码算、模型照抄。** 这是本文件从始至终的主线（「把算术从模型手里拿给代码」）在几何领域的延伸。
