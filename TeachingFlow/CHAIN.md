# TeachingFlow 链路设计 v0.1

> 使用/调试/生产说明见 [`docs/README.md`](docs/README.md)；本文负责设计契约与取舍。

> 目标：终端一句话 → 意图/范围解析 → 学习方案 → 单元教学 → 课件大纲 → PPTX。
> 每一步的 LLM 输出**结构化落盘**后作为下一步的输入变量，不是靠上下文记忆传递。

---

## 1. 与现有 router.py 的差别

| | `集成路由V1.0/router.py` | `TeachingFlow` |
|---|---|---|
| 跳数 | 单跳：1 句 → 1 个模板 → 结束 | 多跳：5 步，上一步产物喂下一步 |
| 状态 | 无（进程结束即遗忘） | `runs/<日期>/<slug>_<HHMMSS>/` 全量落盘，可 `--resume` |
| 分支 | 互斥的 5 条路由 | 一条主链 + 可选分叉（要不要产片、产几个单元） |
| 中间产物 | 只有最终结果 | 每步 prompt / raw / parsed / 校验报告 |
| 失败处理 | 打印错误后退出 | 每步有 gate + 降温重试 + 降级交接 |

复用的资产**不改动**：`PromptTemplate/`（模板 + prompt_builder + quality_gate + unit_bridge）、`PPT-Agent/pipeline.py`（产片）。

---

## 2. 主链路

```
用户: "我想系统学习高中语文"
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│ S0  范围解析（纯规则，不调 LLM）                              │
│     输入: 原始一句话                                          │
│     输出: FlowSpec{学段, 学科, 模块, 目标, 周数/课时, 页数}    │
│     关键: 「高中语文」是 3 年课程，超出 teacher_planner 契约    │
│           （总周期 < 16 周）→ 必须切成可教学模块               │
│           内置切分表: 高中语文 → 现代文阅读 / 古诗文阅读 /      │
│           语言文字运用 / 写作                                  │
└─────────────────────────────────────────────────────────────┘
  │  FlowSpec（带默认值 + 可 --module/--weeks 覆盖）
  ▼
┌─────────────────────────────────────────────────────────────┐
│ S0b 周期估算（模型，按需触发）                                │
│     只在用户没给周期/周课时时跑：读模块+目标+水平+原话         │
│     输出 总周期 / 每周可投入时间 / 依据；代码夹 1~15 周、1~24  │
│     用户已指定（--weeks/--days/--hours-per-week/句子里写）则跳过 │
└─────────────────────────────────────────────────────────────┘
  │
┌─────────────────────────────────────────────────────────────┐
│ S1  学习方案  teacher_planner                                 │
│     prompt ← 学科/学习目标/总周期/每周可投入时间/学生水平       │
│     LLM  → "学生视图 + course:/chapters: YAML 蓝图"            │
│     gate → parse_blueprint(): 必须有 course:+chapters，        │
│            所有 unit.lesson_status == pending，               │
│            各章 hours 之和 ≤ net_hour_budget                   │
│     落盘 → 01_plan.prompt.md / 01_plan.result.md /            │
│            01_blueprint.yaml（后续步骤只读这个 YAML）          │
└─────────────────────────────────────────────────────────────┘
  │  blueprint.yaml
  ▼
┌─────────────────────────────────────────────────────────────┐
│ S2  选单元（纯规则，不调 LLM）                                │
│     输入: blueprint + progress（首跑为空）                     │
│     输出: Target{chapter_id, unit_index, unit_title, 课时}     │
│     规则: 按门控找「第一个已解锁且未渲染」的单元                │
│           --unit ch01-u2 可指定；--all-units 逐个跑            │
│     落盘 → 02_target.json                                     │
└─────────────────────────────────────────────────────────────┘
  │  target + blueprint
  ▼
┌─────────────────────────────────────────────────────────────┐
│ S3  单元教学  teacher_unit（不挂 fewshot）                     │
│     变量 ← unit_variables()（12 个字段，蓝图映射）             │
│            学科/当前章节/本单元名称/本单元序号/本章单元总数/     │
│            本单元课时/上一章测验结果/学情数据/前置薄弱点/       │
│            学生水平/need_example/是否安排复习                  │
│     gate → require_inputs() 预检 + 输出必须含 10 个模块名      │
│            （本单元定位/难度调整/本课目标/知识讲解/典型例题/    │
│             易错提醒/小结/课件大纲/单元作业/完成后解锁）        │
│     落盘 → 03_unit.prompt.md / 03_unit.result.md              │
└─────────────────────────────────────────────────────────────┘
  │  unit 的 10 模块文本
  ▼
┌─────────────────────────────────────────────────────────────┐
│ S4  课件大纲  teacher_ppt                                    │
│     变量 ← unit_bridge.unit_output_to_ppt_content():          │
│            5 个教学内容模块 + 【课件大纲】作页骨架参照          │
│            学科/课件主题(=单元名)/页数要求/学生水平/单元内容     │
│     gate → 页序 1..N 连续、每页有 页标题+要点+配图+旁白        │
│     落盘 → 04_ppt.prompt.md / 04_ppt.result.md（逐页大纲）     │
└─────────────────────────────────────────────────────────────┘
  │  逐页大纲
  ▼
┌─────────────────────────────────────────────────────────────┐
│ S5  产片  subprocess → PPT-Agent/pipeline.py                  │
│     复用 S4 的大纲：--unit-file（供 pipeline 自己再渲染模板）  │
│     --output-root runs/.../05_ppt_<uid>/（pipeline 工作现场）   │
│     产物 → pipeline 自己的 exports/*.pptx + 讲稿.md + 页面说明  │
│     交付件 → <课程>/第N章-章名/第M单元-单元名.pptx（见 §10）     │
└─────────────────────────────────────────────────────────────┘
  │
  ▼
  交付：PPTX + 全套中间产物 + manifest.json（每步耗时/校验/重试次数）
```

**默认只跑一个单元**（一次终端命令产一份 PPT）。`--all-units` 才在 S2→S5 之间循环。

---

## 3. 每步统一契约

每个 step 都是同一个形状，所以新增/替换步骤不用改编排器：

```python
class Step:
    name: str                  # "s1_plan"
    def build_prompt(self, ctx) -> str          # 渲染提示词（纯函数）
    def run(self, ctx) -> StepResult            # 调 LLM + 校验 + 落盘
```

```python
@dataclass
class StepResult:
    prompt: str                # 实际发给 LLM 的提示词
    raw: str                   # LLM 原始返回
    parsed: dict               # 结构化结果（下一步唯一输入）
    ok: bool
    attempts: int              # 含降温重试
    artifacts: list[Path]
    notes: list[str]           # gate 警告（不阻断）
```

落盘规则（`Artifacts` 统一负责）：

| 文件 | 内容 |
|---|---|
| `NN_<step>.prompt.md` | 提示词 + 头部元信息（时间 / 模板角色 / 变量表） |
| `NN_<step>.result.md` | LLM 原始返回 |
| `NN_<step>.parsed.json` | 解析后的结构化结果（含 gate 报告） |
| `manifest.json` | 全局：输入、FlowSpec、每步状态/耗时/token/产物路径 |

---

## 4. 数据契约（步骤间唯一真源）

```
FlowSpec            ← S0
  stage: 高中
  subject: 语文
  module: 现代文阅读          # 从内置切分表取，可 --module 覆盖
  level: 基础
  goal: 现代文阅读的方法体系与题型应对
  weeks: 10                  # 必须 < 16
  hours_per_week: 3
  pages: 12                  # 8~30
  produce_ppt: true

blueprint           ← S1（YAML，直接从 LLM 输出里切 course: 段）
  course: {subject, goal, student_level, total_weeks, weekly_hours,
           mode, buffer_ratio, net_hour_budget}
  chapters: [{id, order, title, estimated_hours, unlock, units[], assessment}]
  units[]:   {id, title, type, estimated_hours, homework, lesson_status}

Target              ← S2
  {chapter_id, unit_index, unit_id, unit_title, hours, unlock_reason}

unit_text           ← S3（10 模块 Markdown 文本，原样存）
ppt_outline         ← S4（逐页大纲文本，原样存）
pptx_path           ← S5（<课程>/第N章-章名/第M单元-单元名.pptx，见 §10）
```

**不做的事**：不把 S1 的自由文本整篇塞进 S3 的提示词。S3 只接收 `unit_variables()` 算出来的 12 个字段——模板契约里写死了它们的类型与单位，多塞就降级。

---

## 5. 门控与失败策略

| 步骤 | 硬 gate（不通过不往下走） | 失败处理 |
|---|---|---|
| S0 | 学科能识别 / 周期 < 16 周且 > 0 | 缺项用默认值 + 打印「已采用默认值」 |
| S1 | 有 `course:` + `chapters:`；`lesson_status` 全 `pending`；课时不超预算 | 降温重试 2 次 → 仍败则终止（蓝图是后续所有步骤的输入，不能降级） |
| S2 | 存在可解锁且未渲染的单元 | 无 → 打印关卡地图后正常退出 |
| S3 | `require_inputs` 通过；输出含 10 个模块名 | 重试 1 次 → 缺模块只警告（`unit_bridge` 会原样透传），继续 |
| S4 | 页序连续、每页四要素齐全 | 重试 1 次 → 仍败则用 S3 的【课件大纲】兜底 |
| S5 | 产出 `.pptx`（且必须在本次构建目录里） | 失败也把工作现场归档回 `05_ppt_<uid>/`，打印 workflow.log 路径与重跑命令 |

---

## 6. 目录与入口（已实现）

```
TeachingFlow/
├── flow.py                    # 唯一入口（CLI）
├── core/
│   ├── config.py              # 路径 / .env / api 配置
│   ├── llm.py                 # LLM 出口 + mock + run_with_gate（降温重试）
│   ├── templates.py           # PromptLib 封装：预检 → 渲染（主库 + 自有库）
│   ├── blueprint.py           # 复用 demo_course_flow 的蓝图/门控/单元变量
│   ├── scope.py               # S0：学段/学科/周期抽取 + 模块表 + FlowSpec
│   ├── gates.py               # S1/S3/S4 的出关校验
│   ├── naming.py              # 交付件命名：章目录 / 单元文件
│   └── artifacts.py           # run 目录 / 落盘 / manifest
├── steps/
│   ├── s0_scope.py  s1_plan.py  s2_select.py
│   └── s3_unit.py   s4_outline.py  s5_render.py
├── promptpack/templates/course_map.yaml   # 模块表兜底用的自有模板
├── fixtures/                  # --mock 用的假 LLM 回复（4 个）
└── runs/YYYYMMDD/<slug>_HHMMSS[-N]/
```

CLI：

```bash
cd TeachingFlow
python flow.py "我想用2周突击高中语文文言文"          # 全链路：一句话即可（自动选模块/估周期/联网配图/产片）
python flow.py "我想系统学习高中语文"                 # 同上，模块与周期也由模型判断
python flow.py "我想系统学习高中语文" --mock          # 不调 API，验证 5 跳数据流
python flow.py "..." --mock --to plan                 # 停在 S1，只交蓝图
python flow.py "..." --module 古诗文阅读 --weeks 10
python flow.py "..." --term short                      # 强制短期（未给周期时默认 2 周）
python flow.py "..." --term long --weeks 12            # 长期但只排 12 周
python flow.py "..." --days 10                         # 10 天 → 向上取整 2 周
python flow.py "..." --unit ch01-u2 --units 1         # 指定单元 / 改单元数
python flow.py "..." --no-input --strict-gate         # 不交互 + 门控硬卡
python flow.py "..." --resume runs/20261023/xxx       # 续跑（已有产物自动跳过）
```

---

## 7. 实现状态（骨架已通过验证）

| 步骤 | 状态 | 验证结果 |
|---|---|---|
| S0 范围解析 + 模块选择 | ✅ | 命中内置表；表外学科必须用户确认（4 条路径已验）；表外学段×学科走模型切分 |
| S1 学习方案 + 蓝图 gate | ✅ | 3 章 / 6 单元 / 15 课时预算，6 项 gate 全过 |
| S2 选单元（默认前 2 个） | ✅ | ch01-u1 + ch01-u2；未解锁只告警不阻断 |
| S3 单元教学 + 10 模块 gate | ✅ | 模块齐全 / 顺序正确 / 非空 |
| S4 课件大纲 + 4 模块 gate | ✅ | 10 页、页序连续、每页四要素齐全 |
| S5 产片（`--from-plan`） | ⏳ | mock 下默认跳过；sidecar 已被 pipeline 复检器接受 |

**S4 → S5 的衔接已验证**：用 `PPT-Agent` 自己的 `load_plan` + `teacher_ppt` 校验器
复检 S4 落盘的大纲，17/17 项通过、不会触发降级交接。

**刻意保留的取舍**：
- mock 下两个单元共用同一份 fixture（内容相同），只证明数据流，不代表真实产出。
- S4 的 gate 只查结构（页序 / 四要素），不查「要点是否 ≤25 字」这类文风项——
  那类问题由 pipeline 交接时复核并降级，不在链路中间反复重试烧 token。

---

## 8. 已定口径（用户确认）

1. **范围切分**：先给模块清单，**由用户选择**。内置表优先（确定性、零成本）；
   表外学段×学科用 `promptpack/templates/course_map.yaml` 让模型切分。
   非交互终端取第一个，并把该决定写进 manifest 的 notes。
   **学段 / 学科未确定时必须由用户确认或修正**（学段给编号菜单；学科为「回车采用推断值 /
   输入正确学科名」）；非交互环境不静默采用推断值，一次把两项都报出来，
   要求 `--stage` / `--subject` 或 `--accept-detected`。
2. **每次产几个单元**：默认**前两个单元**（`--units N` 可改），长期学习按章序推进。
   门控默认只报告不阻断——门控管「学生能不能学下一章」，不是「老师能不能提前备课」；
   要学生视角的硬门控时用 `--strict-gate`。
3. **不跳过 S3**：任何情况都先渲染单元教学，再由单元内容产课件大纲，
   以保住例题、易错提醒与学情自适应这些教学细节。

---

## 9. 页数怎么定（口径）

页数**不写死、不按时长硬算**，由模型读完本单元的教学内容后决定（S4a）。

```
用户指定（--pages N / 句中「12 页」）
   └─ 直接用（只夹到 8~30）

未指定
   └─ S4a 页数决策（promptpack/templates/slide_plan.yaml）
        · 输入：本单元教学内容（unit_bridge 后的 5 模块）+ 课时 + 学生水平
        · 口径：一页一个教学动作
            页数 = 封面1 + 目标1 + 知识讲解页(每要点1页)
                 + 例题页(读题1+解析1) + 易错页(每点1页,≤2)
                 + 练习1 + 小结1
        · 输出：页数 + 理由 + 页面骨架
        · 结果夹到 8~30（模板契约），越界记警告
```

**为什么保留 8~30**：这是 `teacher_ppt` 的输入契约（number 8~30），也是常识边界——
3 页讲不完一节课，60 页没人看。模型在边界内自由，越界由代码夹回并记录。

**反锚定（重要）**：`slide_plan.yaml` 里**不允许出现任何示例页数**。早先「输出格式」
写的是 `页数：12` 配一段凑巧等于 12 的理由，模型会把示例当默认值照抄——示例即隐式锚点。
现在输出格式用 `<阿拉伯数字>` 占位，并写明「不存在标准页数，上面没有给任何示例数字」。
改模板时别把人数字写回去（文件头有 ⚠️ 维护守则）。

**可见性**：S4a 打印 `页数决策：10 页 —— 模型按内容决策 10 页：知识讲解 5 页 + 例题 2 页 + …`；
决策原文落 `04a_pages_<uid>.prompt.md` / `.result.md`；依据写进 `manifest.json` 与
`04_outline_*.result.meta.json` 的 `pages_basis`。

**兜底顺序**：用户指定 → 模型决策 → 模型没给出「页数：N」时才回落 `课时 × 4`（并明确标注为兜底）。

**三道防回归**（2026-10-23 补）：

1. **模板自检**：`flow.py` 启动先跑 `core/promptlint.py`，扫 `promptpack/templates/*.yaml`
   的 `sections[].body`，命中 `页数：N` / `共 N 页` 就报警（不阻断，但说得很响）。
   单独查：`python tools/prompt_lint.py`（也可 `--prompt <渲染后的提示词>`）。
2. **S5 显式锁页数**：`s5_render.build_cmd()` 按**大纲实际页数**传 `--pages N`。
   原来依赖 sidecar 覆盖 pipeline 的 `defaults["页数要求"]=10`，sidecar 一缺就静默退回 10，
   复检凭空多出「页数=声明」缺陷、白跑一轮降级交接；现在这条依赖被切断。
3. **占位符不会静默通过**：`<阿拉伯数字>` 不匹配 `_PAGES_RE`，模型真照抄占位符会走
   「模型未给出页数」分支，manifest 记 `ok=false` + 兜底值，不会伪装成正常结果。

**实际页数仍由 S4 逐页写出**，偏差由 S4 gate（`≥ 决策-2`）与 pipeline 的
`页数=声明` 缺陷检查兼管；sidecar 按实际页数写，避免 S5 复检多出假缺陷。

---

## 10. 交付件命名与目录（章 / 单元是父子）

**一次 run 就是一门课**（`runs/<日期>/<用户原话>_<时分秒>/`），章与单元是父子关系，
于是目录也按父子排：**章建目录，单元是章目录里的文件**——不把两级压成
`第1章-第1单元-中文名.pptx` 一条扁平字符串。

```
runs/20261022/我想系统学习高中语文_220640/              # ← 这门课
├── 第1章-散文阅读入门/                                 # ← 父：章
│   ├── 第1单元-散文的文本细读与圈点勾画.pptx            # ← 子：单元交付件
│   ├── 第1单元-散文的文本细读与圈点勾画-讲稿.md
│   ├── 第1单元-散文的文本细读与圈点勾画-页面说明.md
│   └── 第2单元-散文的情感线索与主旨概括.pptx
├── 第2章-小说阅读/                                     # 同一门课，下一章接着往里放
│   └── 第1单元-人物形象的分析与概括.pptx
├── 00_spec.json / 01_blueprint.yaml / 02_targets.json
├── 03_unit_<uid>.* / 04_outline_<uid>.* / 04a_pages_<uid>.*
├── 05_ppt_<uid>/                                       # ← pipeline 工作现场（构建完从 .work 归档回来）
│   └── <english_slug>_ppt169_<date>/{exports,design_spec,svg,validation,…}
└── manifest.json                                       # steps[].deliverable 指到交付件
```

**命名规则**（`core/naming.py`）：

| 层级 | 形态 | 来源 | 兜底 |
|---|---|---|---|
| 章目录 | `第{章序}章-{章标题}` | 蓝图 `chapter.order` + `title` | 章序从 `chapter_id`（`ch01`）取数字 → 1；无标题时只留 `第1章` |
| 单元文件 | `第{单元序}单元-{单元标题}.pptx` | `target.unit_index + 1` + `unit_title` | 单元序从 `unit_id`（`ch01-u2`）取数字 → 1；无标题 `未命名单元` |

文件名/目录名只清 Windows 非法字符（`\ / : * ? " < > |`），**中文标点和空格保留**——
标题里本来就有《背影》之类的书名号。单元文件里不再重复章号：父目录已经说明了它在第几章，
同一个文件被单独拷走时靠章目录名回溯。

**为什么保留 `05_ppt_<uid>/` 不搬走**：pipeline 的复检、修复、`design_spec`、svg、
`validation/workflow.log` 全在那份目录旁边；把 pptx 挪走会断链，后续修复就找不到素材。
交付件是**副本**（`shutil.copy2`），工作现场不动；导出讲稿/页面说明也从工作现场的
pptx 读（`find_in_project` 才能找到隔壁的 `design_spec.md`）。

**`--resume` 判定**：先看章目录里的交付件在不在；不在再扫旧布局
`05_ppt_<uid>/**/*.pptx`——旧 run 直接按新命名补一份交付件（含讲稿导出），不重跑产片。
这样也顺手修掉 RECOVERY.md 里那个坑：旧布局顶层没有 pptx，曾导致「已产过片但仍重跑」。

### 10.0 LLM 返回集（`<run>/LLM返回/`）

每次「模板加工后的提示词 → LLM 原始返回」都会自动归档一份到这个文件夹，
不用再去 `01_plan.result.md` / `03_unit_*.result.md` / `04a_pages_*` 各处拼。

```
<run>/LLM返回/
├── 001_S0b-周期估算.md
├── 002_S1-学习方案.md
├── 003_S3-单元教学·ch02-u2.md
├── 004_S4a-页数决策·ch02-u2.md
├── 005_S4-课件大纲·ch02-u2.md
└── index.json            # 调用顺序 / 步骤 / 单元 / 字数 / 模型 / 温度 / mock
```

- 文件编号 = **实际调用顺序**（重试也算一次，各自一份，温度写在头部）
- 每份文件：头部元信息 → `## 返回（LLM 原文）` → `## 提示词（模板加工后实际发出）`
- `--mock` 也归档（`mode：mock`、模型记 `fixture`），空跑与真跑的差异可直接对照
- 旧的 `NN_<step>.result.md` / `.prompt.md` **保留**：`--resume`、sidecar（`04_outline_*.meta.json`）
  与 pipeline `--from-plan` 复检都依赖它们；`LLM返回/` 是给人看的聚合视图
- **历史 run 回填**：`python tools/backfill_llm_returns.py "<run 目录>"`（或 `--latest`）。
  按 `00a→00→00b→01→03→04a→04` 的顺序配对 `.prompt.md` / `.result.md`，头部标 `mode：回填`，
  `model/temperature` 记「回填（未知）」（历史产物没存这两个字段）。

### 10.1 构建在短路径里做，产物只认 run 内

两个坑，2026-10-23 修：

| 坑 | 症状 | 处理 |
|---|---|---|
| Windows 260 字符 MAX_PATH | 中文长课程名 + `<英文slug>/exports/.pptx-build-<hex>/pptx_content/ppt/slideMasters/_rels/…` 超长，`svg_to_pptx` 抛 `FileNotFoundError`；或后续 `total_md_split` 报 `svg_output directory does not exist` | pipeline 改在 `TeachingFlow/.work/<run-hash>_<uid>/`（纯 ASCII、短）里构建，导出成功后把整个项目目录**移进** `<课程>/05_ppt_<uid>/` 归档。移动是顶层 rename，不受内部深度影响 |
| pipeline 的 `find_pptx` 回退 | output-root 里没 pptx 时，它会去扫 PPT-Agent / ppt-master 的共享 `projects`，于是报告里写出一份**别人的旧 PPTX**（如 `microscope_usage…`），看起来像"PPT 放到 ppt-master 了" | ① `_newest_pptx()` 只在本次 `.work/<hash>_<uid>` 里找，并断言产物在 run 内、路径不含 `ppt-master`；② 控制台把 pipeline 报的共享 projects 路径替换成「已忽略」；③ 根本不读 PPT-Agent 的报告 JSON |

失败也归档：pipeline 退出码非 0 时同样执行 `_archive()`，把 `validation/workflow.log`、`design_spec`、半成品 SVG 留在 `05_ppt_<uid>/` 里可查。`.work/` 为空时自动 rmdir，残留目录可直接删（已 gitignore）。

---

## 11. 长期 / 短期、周期与页数都不靠用户明说

用户只说一句「我想系统学高中语文」就够了。**周期、每周课时由模型估，页数由模型按内容定，
本次渲染几个单元按周期类型自动定**；这些都可以用 CLI 覆盖，但不是必填项。

### 11.0 先看原话：聚焦主题 + 期限（2026-10-24 修）

用户说「我想在今天突击生物的光合作用」，旧链路会交出「种子萌发」的课件，三个原因与对应改法：

| 原因 | 改法 |
|---|---|
| S2 按章序取第一个单元，不看原话 | 新增 `spec.focus`（剥掉包装词/学段/学科/期限/虚词后的名词短语，如「光合作用」）；`pick_targets(focus=…)` 按 `focus_score` 排序：标题命中给 `100+len`，否则按 2 字窗口累计；没命中才回章序 |
| 「今天」是**期限**不是周期，被当成普通需求估成 3 周 | `extract_deadline()` 命中今天/今晚/明天/这周/尽快… → 强制 **1 周**最小闭环（`weeks_source=近期期限「今天」（按 1 周排）`），不再交给估算器拉长 |
| 学习目标直接用模块 goal（三块内容全包） | 选定模块后把 `spec.goal` 收窄为「本次聚焦「光合作用」……」，S0b 估算与 S1 规划都吃这句话；S1 提示词再补一行「只排与它直接相关的章/单元」 |

**强度优先：一次课解决 vs 系统学（2026-10-25 定）**

先判「这次要投多大」——命中即**一次课解决**，不排成几周：

| 信号 | 强度 | 排期 | 页数 |
|---|---|---|---|
| 突击 / 速成 / 速通 / 临考 / 明天要 / 今天要 / 一节（课）/ 简单 / 简略 / 了解一下 / 过一下 / 应付 / 入门 | `一次课` | 1 周、1 个单元；**周课时由 S0b 按「这一节课能讲完多少」估**（代码只在 >4 时做上限保护） | **无默认值**：S4a 按内容数页；提示词只加“内容要在这节课讲完、可比常规课件长”，不给数字 |
| 系统 / 全面 / 深入 / 吃透 / 打基础 / 长期 | `系统` | 交 S0b 估算（2~15 周） | S4a 按内容定 |
| 《作品名》/课文（且无强度词） | `单篇` | 1 周、1 章 1 单元；课时同样由 S0b 估 | S4a 按内容定 |
| 都没有（「我想学高中语文」） | 模糊 | **交互追问**：一节课 / 一周 / 系统；`--no-input` 时记 note 后交估算 | — |

**课时没有默认值**：只有 `--hours-per-week` 才固定；否则 S0b 按内容估（一次课按“这一节课能讲完多少”，超过 4 课时才做上限保护并记 note）。
显式给了周期（`--weeks`/句子里写「两周/2个月」）时以周期为准，强度只影响单元数与篇幅；
`--hours-per-week`、`--pages`、`--units` 始终可覆盖。页数只有两种来源：用户指定，或 S4a 按内容数出；
依据写进 `04a_pages_*.result.md` 与 manifest 的 `pages_basis`。

**单篇课文 / 轻量意图（2026-10-25 修）**

「我想简单学一下《祥林嫂》这篇课文」以前被排成 **3 周 / 7.5 课时 / 2 章 4 单元**，四个原因：

| 原因 | 现象 | 改法 |
|---|---|---|
| `focus` 抽歪 | 抽成「简单学祥林嫂这篇课文」，还被塞进学习目标 | 噪声词补「简单/学一下/这篇/课文/一篇…」；**有《作品名》时 focus 直接取作品名**（`extract_work`） |
| 没有「投入强度」概念 | 「简单」被当成普通需求 | `extract_effort()`：简单/简略/了解一下/速览… → 轻；系统/深入/吃透… → 重。轻 → **1 周 / 2 课时（封顶）/ 1 单元** |
| 没有「单篇课文」概念 | 被当成整模块方法体系 | `is_single_text()`：有《…》或课文/这一课/一篇 → **1 周 / 1 章 / 1 单元**，学习目标改成「围绕《X》做一次精读」 |
| 学科猜不出 | 「祥林嫂」不含学科名 → 猜成「通用」，要用户手动改成语文 | `guess_subject()`：出现《…》/课文/小说/散文/诗歌/文言文 → 候选「语文」（仍需确认，但不再默认通用） |

单篇/轻量时**不再调 S0b 估算器**（周数与课时都已确定：1 周 × 2 课时），避免出现「依据说 3 周、实际按 1 周执行」的自相矛盾记录。

同一句话改后：

```
【S0】周期=1周（来源：单篇课文（按 1 周最小闭环））课时/周=2（单篇/轻量默认 2）预算=1.5课时 每次单元数=1
蓝图：1 章 / 3 单元 → 再收紧为 1 章 1 单元
第1章 《祥林嫂》精读（1.5）
   ch01-u1 《祥林嫂》内容结构梳理、写法作用分析与主旨把握  1.5课时
```

配套的两道可见性：

- S2 若在蓝图里找不到贴合单元 → note 明说「蓝图里没有与「X」贴合的章/单元（规划跑偏）」，不静默退章序；
- S1 gate 新增软检查「蓝图覆盖聚焦主题」（`focus_score>0`），跑偏会打 ⚠ 并建议 `--resume` 重跑 S1 或用 `--unit` 指定。

### 11.1 周期与每周课时

```
--weeks N / --days N / --hours-per-week N   → 用户指定，直接采用（来源记 CLI）
句子里有周期（3周/三周/2个月/半年/十天）     → 用句子的周数；缺的那项交给 S0b
都没给                                      → S0b 模型估算（scope_estimate）
--term long|short                           → 定任务类型；未给周数时取该类型默认（长期 10 / 短期 2）
```

S0b 的输出与兜底：

| 项 | 规则 |
|---|---|
| 模型输入 | 学段 / 学科 / 模块 / 学习目标 / 学生水平 / **用户原话** |
| 模型输出 | `总周期：N`、`每周可投入时间：M`、`依据：一句话`（模板里**没有任何示例数字**） |
| 代码边界 | 周数夹 `1~15`（模板契约 `< 16`）；周课时夹 `1~24`（=20h÷50min） |
| 解析失败 | 保留临时默认（长期 10 周 / 3 课时），`s0_estimate` 记 `ok=false`，`--weeks` 可接管 |
| 落盘 | `00b_estimate.prompt.md` / `.result.md`；`00_spec.json` 带 `weeks_source` / `hours_source` / `term_source` |

**类型判定**：句子里出现「突击/冲刺/速成/考前/集中突破」→ 短期，出现「系统/学完/打基础/从零/半年」
→ 长期；否则按估算周数 `≤4 短期 / >4 长期`。句子里明说的类型**只定类型、不锁周数**，
冲突时以用户明说为准（`term_source` 记 `句子关键词`）。

### 11.2 蓝图 mode

模板骨架写死 `mode: long_term`（`PromptTemplate` 是共用资产，不改），S1 解析完蓝图后按
`spec.term` 改写 `course.mode`，并写入 `course.term` / `course.weeks_source`；
差异记进 manifest。短期任务另在提示词末尾追加三行「本次口径」（类型/预算/跳过插复习/别照抄范文规模）。

### 11.3 页数（早已自动）

`--pages`/句子里写「12 页」才照办；否则 S4a（`slide_plan`）读单元内容按「一页一个教学动作」
数出页数并给理由，代码只夹 `8~30`。S5 用 `--pages` 锁大纲实际页数，防 sidecar 缺失时退回默认 10。

### 11.4 本次渲染几个单元（本次改成自动）

`--units` 不填时：**短期 1 个、长期 2 个**（`s0_scope` 按 term 决定，写入 `units_per_run`）；
S2 打印合计课时，超过本阶段预算会告警并建议用 `--units/--unit` 收敛。

### 11.5 可见性

```
【S0b】周期估算：读完「现代文阅读」与目标后判断学几周、每周几课时 …
   ✓ 采用：6 周 × 3 课时/周（长期） —— 模块含散文、小说、论述类三类…
【S0】学段=高中 学科=语文 周期=6周（长期，来源：模型估算） 课时/周=3（来源：模型估算） 预算=15课时 每次单元数=2
   页数：未指定 → 由模型读完本单元教学内容后决定（S4a，夹 8~30）
```

CLI 覆盖：`--weeks N`、`--days N`、`--hours-per-week N`、`--term long|short`、`--pages N`、`--units N`。

---

## 12. 写死清单（2026-10-24 排查）

口径：**「写死」= 用户没指定时代码替用户做了决定，或模板把常量塞进提示词影响模型。**
分四类：① 已改成动态；② 产品规则（有意固定，可改可不改）；③ 隐蔽写死（学情/进度）；
④ 共用资产里的示例锚点。

### ① 已改成动态

| 项 | 原来 | 现在 |
|---|---|---|
| 学习周期 | 没说就静默取 10 周 | `--weeks/--days` > 句子（汉字/月/年/半年） > **S0b 模型估算**（输入模块/目标/水平/原话，输出周数+依据，夹 1~15）；解析失败才回落默认并记 `ok=false` |
| 长期/短期 | 无此概念，蓝图恒 `long_term` | `term` 进 FlowSpec；`--term long|short`；S1 按实际周期改写 `course.mode` 并写 `term/weeks_source` |
| 每周课时 | 没说就静默 3 课时 | 同一跳 S0b 估算（夹 1~24）+ `hours_source`；预算按 `周×课时×0.85` 核算并进 gate |
| 「总周期与请求一致」 | 只查「<16 周」，2 周请求配 6 周方案也勾 ✓ | 真比较（±0.5 周），不符记 ⚠ |
| 预算口径 | 不核对 | gate 比对 `蓝图 net_hour_budget` vs `周×课时×(1-0.15)`，差 >10% 记 ⚠ |
| 本次渲染的单元 vs 预算 | 不核对 | S2 打印合计课时，超预算记 ⚠ 并建议 `--units/--unit` |
| PPT 页数 | 曾经模板示例写 `页数：12` | S4a 模型决策；S5 用 `--pages` 锁大纲实际页数 |
| 本次渲染几个单元 | `--units` 默认 2 | 未指定时自动：短期 1 个、长期 2 个（S0 按 term 决定） |

### ② 产品规则（有意固定；要改就是加参数，不是 bug）

| 常量 | 值 | 位置 |
|---|---|---|
| 阶段周期上限 | `< 16 周` | `scope.WEEKS_MAX_EXCLUSIVE` / `teacher_planner.inputs` |
| 页数边界 | `8~30` | `scope.PAGES_MIN/MAX` / `teacher_ppt.inputs` / `slide_plan` |
| 缓冲比例 | `0.15` | `scope.BUFFER_RATIO` / 模板 `buffer_ratio` |
| 单元课时 | `0.5~3` | `teacher_planner` 口径 + `teacher_unit.inputs` |
| 作业题数 | `3-5 题`（骨架示例 `count: 4`） | `teacher_planner` 输出骨架 |
| 章节解锁分 | `min_score: 60` | `teacher_planner` 输出骨架（`can_unlock` 读它） |
| 每周课时上限 | `24`（=20h÷50min） | `teacher_planner.inputs` |
| 模块切分 | `3~6 个`、每模块 `8~15 周` | `course_map` |
| 页数算法 | 封面1/目标1/练习1/小结1、易错≤2、例题+2；gate 侧：旁白≥60 字、页数容差 `-2`、每页要点/配图/旁白必须齐 | `slide_plan` + `gates.gate_outline` |
| 温度 | `0.7`；重试 (0.7,0.4,0.0)；S3/S4 (0.7,0.3) | `flow.py --temperature` / `run_with_gate` / 各 step |
| 每次渲染单元数 | 短期 1 / 长期 2（`--units` 可覆盖） | `steps/s0_scope.py`、`blueprint.pick_targets` |
| 产片默认 | `image=auto`（联网取图，provider `pixabay,wikimedia`）、`reasoning exec=none/repair=low`、`image-review=score`、P4 修复 2 轮 | `flow.py`（`--no-images` 可关） |

### ③ 隐蔽写死（学情/进度没接）

| 项 | 现状 | 影响 |
|---|---|---|
| `progress={}` | S2/S3 都传空 | `上一章测验结果=无（首章）`、`学情数据=数据不足`、`前置薄弱点=无` **恒成立**，teacher_unit 的「难度调整说明必须引用数据」永远只能引用空数据 |
| `STATE_DIR` | `core/config.py` 定义了 `state/progress.json`，**没有任何读写** | 跨次运行的学情无从累积 |
| `need_example=True` | S3 恒传 true | 例题永远要；没有按水平分支 |
| S2 选单元 | 按章序取前 N 个，不看预算 | 已加超预算告警（不自动减） |

### ④ 共用资产里的示例锚点（`PromptTemplate/` 只读，不改）

| 位置 | 常量/示例 | 风险 |
|---|---|---|
| `templates/teacher_planner.yaml` 算例 | 6 周 / 3 课时 / 0.15 → 15.0 | 短期任务可能照抄 6 周规模 |
| `fewshots/teacher_planner.yaml` | 5 周 / 3 章 / 6 单元 / 12.5 预算 | 同上（短期已加三行口径注缓解） |
| `templates/teacher_planner.yaml` 骨架 | `mode: long_term`、`buffer_ratio: 0.15`、`homework.count: 4`、`min_score: 60`、`difficulty: 基础` | 模型可能整份照抄这些字段值 |
| `fewshots/teacher_ppt.yaml` | 两篇范文 8 页 / 9 页 | 偏低锚定（输入里已明确 `页数要求：N 页`） |
| `templates/teacher_ppt.yaml` | `页数要求` number 8~30；每页 2-4 条要点、每条 ≤25 字 | 与 ② 同一口径 |
| `pipeline.py` ROLE_PROFILES | `页数要求: 10`、`学生水平: 基础`、`学科: 通用` | 仅非 `--from-plan` 路径生效；S5 已用 `--pages` + sidecar 覆盖 |

### 怎么复核

```bash
cd TeachingFlow
python tools/prompt_lint.py                                   # 模板里的页数示例
grep -rn "DEFAULT_\|_FALLBACK\|= 0\.[0-9]\|= [0-9]\+$" core steps flow.py
# 每次真跑后：
#   manifest.steps.s0_scope → term / weeks / weeks_source / hours_source / budget
#   manifest.steps.s1_plan  → 总周期、预算两条软检查的 detail
#   manifest.steps.s4_pages:* → pages / basis / 是否兜底
```
