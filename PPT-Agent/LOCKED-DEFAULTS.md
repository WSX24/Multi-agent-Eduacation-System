# 锁定的默认值（改动前必须问用户）

> 建立于 2026-10-19。违反这条会造成 4~5 倍的无声变慢，或版面质量回退。

## 1. 推理强度

| 阶段 | 值 | 位置 |
|---|---|---|
| P3 逐页写 SVG | `none` | `router.py --reasoning-exec` 默认 |
| P4 修复 | `low` | `router.py --reasoning-repair` 默认 |

**背景（实测 30 单）**

| 配置 | P3 秒/页 | 12 页 P3 |
|---|---|---|
| none | 6~9 | ~90s |
| low | 23~41 | 240~500s |
| 未设（继承全局 low） | 44~87 | 500~870s |

慢的原因是隐藏推理 token：12 页能烧 14 万输出 token，其中 ~85% 看不见。

**为什么 none 够用**：坐标、字数上限、函数图像几何全部由代码算好喂给模型；
「用几何图形拼人物」这类丑图已由 P3 红线规则（`FIGURE_RED_LINE`）挡住，
与推理强度无关——不要用提高推理来治版面问题。

## 2. 已知的不一致（待用户决定，勿擅自修改）

- `router.py`：exec=`none`、repair=`low` ✅ 符合要求
- `pipeline.py`：两个参数默认 `None` → 继承 agent 全局 `low` → **P3 慢 4 倍**
- `agent.py` 直接跑：同上

即：只有走 `router.py` 是快的。直接跑 `pipeline.py` / `agent.py` 会吃掉这个差距。

## 3. 配图相关默认（同样未经同意勿改）

| 参数 | 值 | 含义 |
|---|---|---|
| `--image-source` | agent/pipeline `off`；router `auto` | 是否配图 |
| `--image-review` | `score` | 候选评分后定稿 |
| `--image-strict` | `True` | 相关性闸门：对不上就丢弃，宁可不配图 |
