# Pilot 状态 — 方向 2「一页之内」滚动一镜到底

日期：2026-09-26　阶段：**Pilot 未通过**（K0 未验收，seg-01-desk 未生成，`pilot/approval.json` 不存在）

---

## 1. 这一轮改了什么

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `source/concept-contract.yaml` | 重写 | 方向 2。`time_control: scrub`、`aspect_ratio: 16:9`、`clip_continuity: chain`；末尾附**作废清单与沉没成本** |
| `source/storyboard.md` | 重写 | 5 关键帧 / 4 段推轨的分镜头表 + 需签收的 4 件事 |
| `source/motion-brief.yaml` | 重写 | 参数空间、手势策略、clip_chain、帧准备与硬门 |
| `source/scene-bible.md` | **新建** | 白纸世界的材料、光、中轴、可验证锚点、绝对禁止 + **门限校准记录** |
| `source/prompt-K0-scene.txt` | **新建** | K0 生成提示词（已迭代 3 版） |
| `tools/verify_scene.py` | **新建** | 逐帧自动硬门（纸白 / 配色 / 中轴 / 极值占比） |
| `tools/ascii_view.py` | **新建** | 无图像通道时把画面降采样成字符图，用于判断构图 |
| `build/motion-budget.json` | 重跑 | `baked-video` + `frame-scrub` |

**未动**（有意保留）：`source/identity-bible.md`（机器人仍出现在第 1、4 段）、
`assets/robot-k0.png`、`source/reference/robot-user-ref.png`。

---

## 2. 预算结果（真实数字）

```
python <oil-motion>/scripts/motion_budget.py \
  --frames 388 --display 1152x648 --dpr 1 --source 1152x648 \
  --driver scroll --time-control scrub --parameter-space linear \
  --background-owner video --scroll-pages 4 --strict
```

| 项 | 值 |
| --- | --- |
| `delivery.selected` | `baked-video`（原因码 `background-baked-into-video`、`long-linear-sequence`） |
| `runtime.controller` | `frame-scrub` |
| 帧数 | 388（4 段 × 97 帧 @24fps ≈ 16.2s） |
| `requiredCell` | 1152×648（= display × DPR 1） |
| `sourceCheck` | 通过，比例 1.00，无放大 |

**分辨率是当前唯一的硬约束**。同一份母版在三组目标下的结果：

| 目标显示 × DPR | requiredCell | sourceCheck | 结论 |
| --- | --- | --- | --- |
| 1152×648 @ DPR 1 | 1152×648 | **通过** (1.00) | 当前唯一成立的目标 |
| 1440×810 @ DPR 1 | 1440×810 | 失败 (0.80) | 需要放大 1.25× |
| 1152×648 @ DPR 2 | 2304×1296 | 失败 (0.50) | 需要放大 2× |

**这个报告会失效**：它按关键帧母版 1152×648 算出。视频模型的输出尺寸必须
`ffprobe` 实测后再重跑预算（旧版实测请求 1024×768、实际返回 1088×832，
说明会吸附尺寸，不能假设）。

---

## 3. K0 三次尝试与自动硬门结果

工具：`tools/verify_scene.py`（逐帧）。门限在 `source/scene-bible.md §4`。

| 尝试 | 亮纸% (≥35) | p95 (≥232) | 均亮 (≥170) | 紫 ≤8% | 禁用色相 ≤1% | 中轴 0.47–0.53 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **12.6** ✗ | **221** ✗ | **144** ✗ | 0.72 | **orange 30.2%** ✗ | 0.456 ✗ | 不通过 |
| 2 | 36.3 ✓ | 242 ✓ | 183 ✓ | **0.00** ✗ | 0.03 ✓ | **0.719** ✗ | 不通过 |
| 3 | **24.1** ✗ | 253 ✓ | **167** ✗ | **0.00** ✗ | **orange 18.0%** ✗ | **0.744** ✗ | 不通过 |

三次尝试的对照图：`qa/K0-pilot-attempts.png`
（attempt 1/2/3 原图与 4:3 母版都在 `qa/K0-scene-attempt*.png`，当前源在
`source/K0-scene.png`）

**两次门限校准（是门限错了，不是画面错了，记在 scene-bible §4 里）**：
1. 第一版写「最暗 ≥12 / 最亮 ≤252」→ 直接把已验收的画风判死，因为
   `identity-bible §3` 记载机器人轮廓线实测 `(0,0,8)`，近黑勾边是画风的一部分。
   改成**面积占比门**（极暗/极亮像素占比上限），细勾边通过，整块压黑被拦下。
2. 第一版写「饱和度 >0.25 即算彩色」→ 机器人壳体 `#6080A0` 饱和度 0.40，
   等于禁止主体自己。改成**色相家族门**：允许紫与 slate 蓝灰，禁止橙/黄/绿/青/红。

**一条事后补上的核心门**：只查「有没有橙色」抓不到 attempt 1 的真正问题
（主色 `#A09080`，平均亮度 150——一间昏暗的褐色房间）。所以补了
**亮纸占比**与**亮度 95 分位**两条门，它们才是「白纸世界」的直接证据。

---

## 4. 当前的系统性问题（三次都栽在同一处）

1. **中轴始终偏右**（0.62–0.74），与提示词的对称要求相反。三次都出现，说明
   不是提示词措辞问题，而是**参考图的构图在牵引**：`robot-user-ref.png`
   （602×595）里的主体位置会让模型把重点放到右侧。
2. **暖褐色反复回来**（attempt 1 和 3）。同一个参考图的背景是暖粉/米色调，
   作为参考图会带出色偏。
3. **紫色两次归零**（attempt 2、3）。提示词里为了压住「紫色不许泛滥」写了
   `very faint` 与 `under 5%`，模型直接选择不画。需要把「必须有」写死。

**待验证的假设**：把 `--image` 参考图去掉，只靠文本描述机器人，palette 与
构图是否立刻正常。这是一次生成就能验证的对照实验，尚未执行。

---

## 5. 阻断项

**无法自证**：本会话没有图像通道（`read` 返回「模型不支持图像」），
所有视觉判断只能靠 `tools/ascii_view.py` 的字符图与 `verify_scene.py` 的
数值代理指标。**代理指标不能替代人眼验收**，而 `qa.md` 要求 Pilot 的
视觉验收结论必须由人给出。

因此 Pilot 卡在这里：K0 需要**你看一眼** `qa/K0-pilot-attempts.png`，
回答两件事：

1. 三张里有没有一张的「教室 + 白纸 + 机器人 + 中轴走道」是**方向对的**，
   只是需要修色/修构图？（有 → 我按那张续做；没有 → 换生图路线）
2. 要不要试一次**不带参考图**的对照生成？参考图 `robot-user-ref.png` 本身
   是暖色调、主体偏右的，怀疑它就是「暖褐回来 + 中轴偏右」的共同原因。
   去掉它可能让 palette 与构图一次就对，代价是机器人的形象由文本重建，
   需要你对照 `source/identity-bible.md` 判断像不像。

---

## 6. 下一步（Pilot 通过后）

```
K1 ← seg-01-desk 的实际尾帧
K2 ← seg-02-page 的实际尾帧
K3 ← seg-03-stations 的实际尾帧
K4 ← seg-04-return 的实际尾帧
```

每段：`--mode keyframe --first-frame <上一段尾帧> --aspect-ratio 16:9`，
逐段过 `verify_scene.py`（seg-03 加 `--allow-red 0.02`），再走 `production_gate.py`
的 `verify-chain` + `verify-output-chain` 两道硬门。
