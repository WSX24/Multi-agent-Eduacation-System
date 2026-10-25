# TeachingFlow 教学流水线 · 使用说明

> 一句话需求 → 课程范围 → 学习方案 → 单元教学 → 课件大纲 → **PPTX + 讲稿 + 页面说明**。
> 每一步的模型输出都落盘，可复查、可续跑、可单独重跑某一步。
>
> 设计与取舍的来龙去脉见 [`../CHAIN.md`](../CHAIN.md)；本文只讲**是什么 / 怎么用 / 生成在哪 / 怎么调试与上生产**。

---

## 1. 它能做什么

| 输入 | 输出 |
|---|---|
| 一句话，如「我想简单学一下《祥林嫂》这篇课文」 | 一份可直接讲课的 PPTX（含逐页旁白）+ 讲稿 Markdown + 页面说明 Markdown |
| 带周期/强度的说法：「用2周突击文言文」「今天突击光合作用」「系统学高中语文」 | 按强度自动定排期：一次课 / 1 周 / 2~15 周 |
| 指定模块、单元、页数、单元数 | 只跑指定范围，或固定页数 |

它做的事，按顺序：

1. **范围解析**：识别学段 / 学科 / 模块（比如「高中语文 → 现代文阅读」），把《作品名》/主题当成聚焦点；
2. **强度与周期**：判定「一次课 / 一周 / 系统」，定周期、周课时（模型估，不写死）；
3. **学习方案**：产出章→单元→课时→解锁条件的蓝图（YAML）；
4. **选单元**：按原话聚焦挑单元（不是无脑取第一个）；
5. **单元教学**：10 个教学模块（定位/难度调整/目标/知识讲解/例题/易错/小结/课件大纲/作业/解锁）；
6. **页数决策**：模型按「一页一个教学动作」数页数（只受 8~30 契约边界约束）；
7. **课件大纲**：逐页写出标题、要点、配图建议、旁白；
8. **产片**：调 `PPT-Agent/pipeline.py` 生成 SVG → PPTX，并导出讲稿/页面说明。

**它不做**：不录音、不剪视频、不做在线学习平台、不做学生端答题（门控只声明规则，不执行判分）。

---

## 2. 流程图

```
一句话
  │
  ▼
S0  范围解析（规则+模型补位）
     · 学段/学科：没识别出会问一次（或 --stage/--subject）
     · 模块清单：内置表 → 用户选；--no-input 时先看原话关键词，猜不准交模型读原句，再不行取第 1 个
     · 聚焦：优先取《作品名》，否则剥掉包装词后的主题词（光合作用 / 文言文 / 完形填空…）
     · 强度：突击/简单了解/今天要/考前 → 一次课；系统/深入/吃透 → 系统；说不清 → 追问
     · 周期：--weeks/--days > 句子里的周期 > 期限(今天/这周) > 单篇/一次课 → 1 周 > 模型估算
     └→ 00_spec.json（含 term / weeks_source / hours_source / intensity / pages_source）
        ↓
S0a 模块判定（仅关键词拿不准时）
     └→ 00a_module.prompt.md / .result.md
        ↓
S0b 周期/周课时估算（仅用户没说时）
     · 生成 总周期 / 每周可投入时间 / 依据，代码只夹 1~15 周、1~24 课时
     └→ 00b_estimate.prompt.md / .result.md
        ↓
S1  学习方案（teacher_planner）
     · 门控：蓝图可解析 / 单元全 pending / 课时不严重超预算 / 周期·预算与口径一致 / 覆盖聚焦主题
     · 代码把 course.mode 归一为 short_term 或 long_term
     └→ 01_plan.prompt.md / .result.md、01_blueprint.yaml
        ↓
S2  选单元（纯规则）
     · 有聚焦词先挑贴合的单元（最长公共子串覆盖 ≥60% 才算），没命中才回章序
     · 每次渲染几个单元：--units 或自动（一次课/单篇/短期 1 个，长期 2 个）
     └→ 02_targets.json
        ↓
        ├─ 对每个目标单元循环 ─────────────────────────────┐
        │  S3  单元教学（teacher_unit）                     │
        │      12 个变量 → 10 个教学模块，门控查齐全/顺序/非空 │
        │      └→ 03_unit_<uid>.prompt.md / .result.md      │
        │  S4a 页数决策（slide_plan）                        │
        │      用户指定则照办；否则模型按内容数页（8~30）      │
        │      └→ 04a_pages_<uid>.prompt.md / .result.md    │
        │  S4  课件大纲（teacher_ppt）                       │
        │      门控：4 模块 / 页序连续 / 每页四要素齐全        │
        │      └→ 04_outline_<uid>.result.md + .meta.json   │
        │  S5  产片（pipeline.py --from-plan）              │
        │      短路径构建 → 导 SVG/PPTX → 归档回 run        │
        │      └→ 交付三件套 + 05_ppt_<uid>/ 工作现场        │
        └──────────────────────────────────────────────────┘
        ↓
manifest.json（每步 ok / 校验项 / 页数与周期依据 / 交付件路径）
```

旁路：

- `--mock`：用 `fixtures/` 的假回复跑通数据流（不花 token，默认跳过 S5）
- `--render`：mock 下也真产片
- `--to map|plan|unit|outline|render`：跑到哪一步停
- `--resume <run 目录>`：复用已有产物

---

## 3. 生成什么，生成在哪

一次运行 = 一门课，目录：`TeachingFlow/runs/<YYYYMMDD>/<一句话 slug>_<HHMMSS>/`。
（同一秒撞名会加 `-2`；`--resume` 直接沿用原目录。）

### 3.1 交付件（给人用的，只有这些）

```
<run>/
└── 第1章-章名/                                  ← 章＝父目录
    ├── 第1单元-单元名.pptx                      ← 成品（每页带演讲者备注）
    ├── 第1单元-单元名-讲稿.md                    ← 逐页旁白（读 PPTX 备注，缺失时回退项目 notes/total.md）
    └── 第1单元-单元名-页面说明.md                ← 每页：讲什么 / 怎么排 / 哪里该高亮
```

命名规则：`第{章序}章-{章标题}/第{单元序}单元-{单元标题}`（章序来自蓝图 `order`，单元序是章内序号）。
只清理 Windows 非法字符（`\ / : * ? " < > |`），中文标点与书名号保留。

> 替换：一旦某单元已经产出交付件，`--resume` 会跳过该单元的 S5（不会重复烧钱）。

### 3.2 过程产物（排查 / 审计用）

| 文件 | 内容 |
|---|---|
| `00_spec.json` | 范围解析结果：学段/学科/模块/聚焦/强度/周期/来源字段 |
| `00a_module.*` | 模块判定（仅关键词拿不准时产生） |
| `00b_estimate.*` | 周期与周课时估算（仅用户没说时产生） |
| `00_scope.*` | 表外学科的模块切分（内置表没有该「学段×学科」时） |
| `01_plan.*` | teacher_planner 的提示词 + 学生视图原文 |
| `01_blueprint.yaml` | **后续所有步骤的唯一真源**（章/单元/课时/解锁） |
| `02_targets.json` | 本次渲染哪些单元、为什么（含 `focus_score`、未解锁提示） |
| `03_unit_<uid>.*` | 单元教学（10 模块） |
| `04a_pages_<uid>.*` | 页数决策（页数 + 理由 + 页面骨架） |
| `04_outline_<uid>.result.md` | 逐页大纲（排版的最终依据） |
| `04_outline_<uid>.result.meta.json` | sidecar：`role` / `inputs` / 页数（请求·实际）/ `pages_basis`，供 S5 复检 |
| `manifest.json` | 全局账本：`input`、`mode`、`spec`、每步 `ok`/产物/校验/notes |

### 3.3 `LLM返回/`（排查时最先看这里）

```
<run>/LLM返回/
├── 001_S0a-模块判定.md
├── 002_S1-学习方案.md
├── 003_S3-单元教学·ch01-u1.md
├── 004_S4a-页数决策·ch01-u1.md
├── 005_S4-课件大纲·ch01-u1.md
└── index.json        # 调用顺序 / 步骤 / 单元 / 字数 / 模型 / 温度 / mock
```

- 编号＝**实际调用顺序**；重试各占一份（头部写温度）
- 每份：元信息 → `## 返回（LLM 原文）` → `## 提示词（模板加工后实际发出）`
- 老 run 可用 `python tools/backfill_llm_returns.py "<run 目录>"` 回填

### 3.4 `05_ppt_<uid>/`（产片工作现场）

```
05_ppt_ch01-u1/<英文项目名>/
├── design_spec.md / spec_lock.md      # 每页结构化规格 + 锁定指纹
├── svg_final/*.svg                    # 真正转成 PPTX 的页面
├── svg_output/*.svg                   # 生成阶段的草稿
├── notes/total.md, notes/*.md         # 全册/逐页旁白（讲稿的兜底源）
├── images/                            # 配图 + image_sources.json（来源与许可）
├── exports/*.pptx                     # pipeline 的原始导出（交付件是它的副本）
└── validation/                        # workflow.log、质量/校准/导出复检报告
```

- 构建在 `TeachingFlow/.work/<run-hash>_<uid>/`（纯 ASCII 短路径，躲 Windows 260 字符上限），
  成功后整个项目目录**移进** `05_ppt_<uid>/`；**失败也归档**，`workflow.log` 一定在。
- 交付件是副本，工作现场保持原样（复检/修复/续跑要读旁边的 `design_spec.md`、SVG）。

---

## 4. 怎么用

### 4.1 安装与配置

```bash
pip install -r requirements.txt
cp .env.example PPT-Agent/.env      # 填 DEEPSEEK_API_KEY（以及可选的 PIXABAY_API_KEY）
```

目录要求（代码按相对位置找依赖）：

```
自建Agent/
├── TeachingFlow/       PromptTemplate/       PPT-Agent/
├── 集成路由V1.0/（只需 export_notes.py）
└── ppt-master/skills/ppt-master/            # 或设 PPT_MASTER_SKILL_DIR 指向它
```

### 4.2 一条命令

```bash
cd TeachingFlow
python flow.py "我想简单学一下《祥林嫂》这篇课文"
```

它会：判模块 → 判强度（一次课）→ 1 周 / 课时模型估 → 出 1 章 1 单元蓝图 → 写单元教学 →
数页数 → 出逐页大纲 → 产片 → 交付 PPTX + 讲稿 + 页面说明，并默认**联网配图**。

### 4.3 常用参数

| 参数 | 作用 |
|---|---|
| `--stage 高中 --subject 语文` | 免去学段/学科的确认提问 |
| `--accept-detected` | 接受它推断出的学段/学科 |
| `--module 古诗文阅读` | 直接锁定模块（跳过原话判定） |
| `--weeks N` / `--days N` | 固定周期（覆盖强度判定） |
| `--term long\|short` | 只要长期/短期；未给周期时取该类型默认 |
| `--hours-per-week N` | 固定周课时（覆盖估算） |
| `--units N` / `--unit ch01-u2` | 本次渲染几个单元 / 只做某个单元 |
| `--pages N` | 固定页数；不给则由模型按内容数 |
| `--no-images` | 关闭配图（出片更快） |
| `--image-source auto\|web\|off` | 配图来源；默认 `auto` |
| `--to map\|plan\|unit\|outline\|render` | 停在哪一步 |
| `--mock` / `--render` | 假模型验链路 / mock 下也真产片 |
| `--resume <run 目录>` | 续跑，已有产物自动跳过 |
| `--no-input` | 不提问：能自动就自动，不能自动（学段/学科没定）就报错要求指定 |

### 4.4 强度口径（决定「排多大」）

| 原话信号 | 排期 | 说明 |
|---|---|---|
| 突击、速成、速通、今天/明天、考前、一节（课）、简单、了解一下、浅尝、应付 | **一次课** | 1 周、1 个单元；课时由模型按「这一节课能讲完多少」估；页数仍按内容数 |
| 系统、全面、深入、吃透、打基础、长期 | 系统 | 周期交模型估（2~15 周） |
| 《作品名》/课文（无强度词） | 单篇 | 1 周、1 章 1 单元 |
| 都没有 | 追问 | 交互选「一节课 / 一周 / 系统」；`--no-input` 时记 note 后交估算 |

---

## 5. 调试

### 5.1 零成本三件套

```bash
python tools/smoke_cases.py       # 意图判定 16 条用例（秒级、不调模型）
python tools/prompt_lint.py       # 模板反锚定：不许出现「页数：N」这类示例数字
python flow.py "…" --mock --to plan     # 假模型跑通 S0~S1
```

### 5.2 定位问题看哪里

| 现象 | 先看 |
|---|---|
| 模块选错 / 聚焦跑偏 | `00_spec.json`（`module`/`focus`/`intensity`）→ `00a_module.result.md` |
| 周期不对 | `00b_estimate.result.md` + `manifest.steps.s0_estimate`；`weeks_source`/`hours_source` 说明是 CLI、句子还是估算 |
| 规划摊太大 / 章数太多 | `01_blueprint.yaml` + `manifest.steps.s1_plan.notes`（周期/预算/聚焦覆盖三条软检查） |
| 单元挑错 | `02_targets.json` 的 `notes` 与 `focus_score` |
| 页数异常 | `04a_pages_*.result.md`（页数+理由）；`manifest.steps["s4_pages:<uid>"].basis`（出现「兜底」即模型没给数） |
| 大纲结构不合格 | `manifest.steps["s4_outline:<uid>"].checks` |
| 产片失败 | `05_ppt_<uid>/<项目>/validation/workflow.log` 末尾 `[ERROR]`；`exports/`、`svg_output/` 是否有文件 |
| 讲稿/页面说明缺失 | 讲稿依赖 PPTX 备注（或 `notes/total.md`）；页面说明依赖 `design_spec.md` |
| 想直看模型吐了什么 | `LLM返回/NNN_*.md`（返回在前、提示词在后） |

### 5.3 分步重跑

```bash
python flow.py "…" --to plan          # 只重跑 S0~S1（换参数试排期最快）
python flow.py "…" --resume <run>     # 从断点继续；已完成的步骤/单元自动跳过
python flow.py "…" --resume <run> --unit ch01-u2   # 只补某个单元
```

> 改蓝图想重排？直接删掉 `<run>/01_blueprint.yaml` 与 `02_targets.json` 再 `--resume`。

### 5.4 常见故障

| 报错/现象 | 原因与处理 |
|---|---|
| `未找到 API Key（PPT-Agent/.env 的 DEEPSEEK_API_KEY）` | 没配 `.env` |
| `未能确定「学段/学科」…` | 用了 `--no-input` 但原话没带学段/学科：加 `--stage/--subject` 或 `--accept-detected` |
| `svg_to_pptx … No such file` / `svg_output directory does not exist` | 历史坑：中文长路径超 260 字符；现在构建在 `.work/` 短路径里，若仍出现请把 run 目录名缩短 |
| 页数偏差告警 | 模型按内容微调；要严格一致就 `--pages N` |
| 一面告警「蓝图覆盖聚焦主题」失败 | 模型没安排到你要的内容：`--resume` 重跑 S1，或 `--unit` 直接指定 |

---

## 6. 上生产

### 6.1 无人值守

```bash
python flow.py "我想系统学习高中语文" --stage 高中 --subject 语文 --no-input --to render
```

- 必须能自动决定的都给默认：模块（原话→模型→首个）、周期（估算）、单元数、页数、配图；
- 不能自动决定的（学段/学科）**不静默猜**，直接报错并给出需要的参数 —— 生产脚本里应显式传；
- 退出码：`0` 成功、`1` 某步门控终止、`2` 缺输入、`3` 模型调用失败。

### 6.2 批量与成本

- 每门课一次运行；同一课程续跑用 `--resume`，不会再花钱重跑已有步骤。
- 成本大头：S3/S4 每单元 2 次、S5 产片 ~20 次模型调用。控制手段：
  - `--units 1`（默认短期 1、长期 2）
  - `--pages` 固定页数（避免模型给到 20+）
  - `--no-images` 关配图（省时间，不省模型调用）
  - `--to outline` 只交大纲评审，通过后再 `--resume` 产片

### 6.3 产物回收（给平台/前端）

`manifest.json` 是稳定契约：

```json
{
  "input": "…", "mode": "真实调用", "spec": { "term": "短期", "weeks": 1, "focus": "祥林嫂", "…": "…" },
  "steps": {
    "s2_select": { "ok": true, "artifacts": ["02_targets.json"], "focus": "祥林嫂" },
    "s4_pages:ch01-u1": { "pages": 13, "basis": "模型按内容决策 13 页：…" },
    "s5_render:ch01-u1": { "ok": true, "deliverable": "第1章-…/第1单元-….pptx" }
  }
}
```

- 只需成品：遍历 `manifest.steps["s5_render:*"].deliverable`；
- 需要讲稿/页面说明：与 pptx 同目录、同 stem、后缀 `-讲稿.md` / `-页面说明.md`；
- 需要课表/门控：读 `01_blueprint.yaml`；需要「这次讲了哪几个单元」：读 `02_targets.json`。

### 6.4 已知边界

1. 单篇的「1 章 1 单元」、一次课的「1 周」是规则决定，不是模型自由判断；要更长用 `--weeks/--units`。
2. 「简单/突击」影响排期、课时、篇幅，**不影响 S3 的讲解深度**（仍按学生水平讲全 10 模块）。
3. 学情/进度尚未接入：`上一章测验结果`、`学情数据`、`前置薄弱点` 目前恒为「无/数据不足」。
4. `PromptTemplate/`、`PPT-Agent/` 是共用资产，本链路只做代码侧归一，不修改它们。

---

## 7. 目录速查

```
TeachingFlow/
├── flow.py                  # 唯一入口（CLI）
├── core/                    # scope（范围/强度/周期）、blueprint、gates、artifacts、llm、naming、promptlint
├── steps/                   # s0_scope / s0_module / s0_estimate / s1_plan / s2_select / s3_unit / s4_outline / s5_render
├── promptpack/templates/    # 自有模板：course_map / module_pick / scope_estimate / slide_plan
├── fixtures/                # --mock 用的假 LLM 回复
├── tools/                   # prompt_lint / smoke_cases / backfill_llm_returns / package_release
├── runs/                    # 运行产物（不入仓）
├── .work/                   # S5 短路径构建目录（成功后归档进 run）
├── CHAIN.md                 # 设计文档：契约、写死清单、口径
├── docs/
│   ├── README.md            # 本文（使用 / 调试 / 生产）
│   └── 打包说明.md          # 打包后生成的上手说明
```
