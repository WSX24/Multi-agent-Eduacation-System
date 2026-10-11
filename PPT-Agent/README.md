# PPT-Agent

把通用 ReAct Agent 特化为 **ppt-master skill 专用执行体**：给它一句话任务，它按 skill 的
路由协议自动完成「读文档 → 跑脚本 → 手写 SVG → 过校验门 → 导出 PPTX」全流程。

```bash
python agent.py --profile quick "做一份 8 页的《向量数据库入门》PPT"
```

---

## 1. 它和原始 agent 的本质区别

原 agent 是一个**通用工具助手**：给模型 3 个函数，让模型自由发挥。

PPT-Agent 是一个**协议执行体**：ppt-master 不是函数库，而是一套「路由 + 强制加载顺序 +
分阶段权威文档 + 阻塞确认门」的工作流协议。执行体必须能：

| skill 的要求 | PPT-Agent 的对应机制 |
|---|---|
| 先跑 `attribution_guard.py` 完整性闸门，非 0 即终止 | system prompt 把这一步定为**第一个必须执行的 action** |
| 路径必须从 `SKILL_DIR` 展开，禁止 `cd`、禁止猜路径 | 提示词预先把 `${SKILL_DIR}` 全部替换成真实绝对路径；`read_skill_doc` 只收相对 SKILL_DIR 的路径并做越界防护 |
| 文档里链接的 references/*.md 必须真实读取，不能凭记忆 | 预载 3 份核心权威文档，其余用 `read_skill_doc` / `grep_skill_doc` **按需懒加载** |
| `⛔ BLOCKING` 门必须停下等用户 | `--profile default` 才注册 `ask_user` 工具；`quick` 模式在提示词层禁止提问 |
| 「不做投机执行」「在所属层修复」等 7 条纪律 | 原样写入 system prompt 的「运行纪律」段 |
| 步骤动辄几十上百轮 | 加了步数上限、观察截断、上下文折叠 |

**核心设计判断**：skill 的文本量太大（60+ 参考文档 / 100 万字符），不可能全塞进上下文。
所以采用「3 份预载 + 工具懒加载」的两级策略，并用 `grep_skill_doc` 解决"不知道该读哪份"的定位问题。

---

## 2. 架构

```
agent.py
├── resolve_skill_dir()      探测 skill 根目录（校验 SKILL.md + attribution_guard.py 双标志）
├── SkillTools               8 个工具，签名 + docstring 自动渲染进 system prompt
│   ├── list_directory       看工程结构 / 检查产物
│   ├── read_file            分段读文件（产物、源码、JSON、日志）
│   ├── write_file           写文件（父目录自动建，\n 自动还原）
│   ├── append_file          分块追加（避免单次生成过长而截断）
│   ├── read_skill_doc       读 skill 内文档（带越界防护）
│   ├── grep_skill_doc       在 skill 文档里检索关键词定位
│   ├── run_command          执行命令（默认全局放行 + 超时 + 输出截断）
│   └── ask_user             仅 default profile 注册，对应 BLOCKING 门
└── PPTAgent                 ReAct 循环
    ├── build_system_prompt  预载 SKILL.md + routing.md + profile 权威文档
    ├── parse_action         ast.literal_eval 快路径 + 手写状态机兜底
    ├── call_model           OpenRouter
    └── run                  Thought → Action → Observation 循环
```

### 提示词的两级加载

```
预载（每次运行都注入，约 2.2 万 token）
  ├── SKILL.md                     入口 + 全局纪律 + 路由表
  ├── workflows/routing.md         路由权威
  └── workflows/profiles/quick-generate.md   或 workflows/generate-pptx.md

懒加载（模型自己按需 read_skill_doc）
  ├── references/*.md              40+ 份设计/执行规范
  ├── workflows/stages/*.md        11 个阶段剧本
  └── scripts/docs/*.md            17 份脚本说明书
```

---

## 2.1 两种执行模式

| | `--mode staged`（默认） | `--mode react` |
|---|---|---|
| 架构 | Plan-and-Execute | 自由 ReAct |
| 流程由谁定 | **代码** | 模型 |
| 模型职责 | 只做判断点：规划、填规格、逐页创作、定向修复 | 每一步自己决定做什么 |
| 文档如何进上下文 | **代码直接注入**（0 次往返） | 模型自己 read/grep（十几次往返） |
| 规格合法性 | 注入权威 JSON schema，validate 第 1 轮就过 | 模型自己拉规则，容易缺字段 |
| 失败归集 | 只把**有 blocking 错误的页**交回模型 | 整轮重来 |

### 阶段划分

```
P0 PREPARE  纯代码  完整性闸门 → 选路 → 注入 263KB 权威文档
P1 PLAN     模型×1  只做真正要判断的：沟通契约 / 页清单 / 配色 / 字体 / 风格
P2 FREEZE   代码+模型×1  init → scaffold-spec/lock → 注入 schema 填值 → validate（失败回灌重试）
                     → text_measure 字体标定 → 注入执行核心
P3 EXECUTE  模型×N  每页一次，只写 SVG（可 --parallel-pages N 并行，默认串行，见 §7）
P4 VERIFY   代码+模型×k  读权威 JSON 报告 → 按文件归集 blocking 错误 → 只修失败页
P5 EXPORT   纯代码  svg_to_pptx
```

### 实测对比（DeepSeek · 同一 skill）

| 指标 | react | staged |
|---|---|---|
| 模型调用次数 | ~30–65 | **4**（2 页任务） |
| 文档阅读往返 | 15 次 | **0 次**（代码注入） |
| validate / 校验器 | 模型自行决定何时跑 | 每阶段强制，且**重试到过** |
| 校验结果 | 6 个 blocking 错误 → 多轮返工 | **0 错误，0 轮修复** |
| SVG 单页体量 | 2.6–3.7 KB | **7.4–8.2 KB** |
| 总耗时 | 269s / 4 页 | 274s / 2 页 |

**关键结论：往返次数降了一个数量级，但总耗时没明显下降。**
因为瓶颈从“往返次数”换成了“单次预填充体积”——每次调用都带 ~75k token 的注入上下文，
平均每次 66 秒。这是阶段化的真实代价，不是调优问题。

**阶段化真正买到的是**：确定性流程、可观测的阶段边界、失败局部化、规格合法性有保证，
以及明显更高的内容密度（模型有 32K 输出预算 + 不再被截断）。

### 阶段化的硬约束

- **`project_manager validate` 是严格 schema 校验**：字段名、枚举值必须精确
  （如 `canvas.format` 必须等于 `PPT 16:9`）。所以代码**必须**注入
  `templates/schemas/*.schema.json`，不能指望模型猜对。
- **同名项目用唯一名避开**：`init` 会追加 `_<YYYYMMDD>`，同天重跑必撞名。
  不删也不重命名旧目录（Windows 上重命名会被占用而 WinError 5），而是给项目名加时间戳。
- **参数含空格必须加引号**：`--role "body:Microsoft YaHei:24"`。
  不加引号会被 shell 拆成两段——这个 bug 在 react 模式里由模型犯，在 staged 里由代码避免。

---

## 3. 安装与配置

```bash
pip install -r "<SKILL_DIR>/requirements.txt"   # skill 自身依赖（导出 PPTX 必需）
```

在 `PPT-Agent/.env` 写入你的模型凭据。agent **支持多家提供商**，按 `.env` 里存在的
key 自动探测；同时填了多家时请用 `PPT_AGENT_PROVIDER` 明确指定。

```bash
# DeepSeek（key 形如 sk- + 32 位十六进制，长度 35）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PPT_AGENT_PROVIDER=deepseek
```

内置提供商：

| `--provider` | base_url | 默认模型 | 上下文 |
|---|---|---|---|
| `deepseek` | api.deepseek.com/v1 | `deepseek-v4-flash` | 1M |
| `openrouter` | openrouter.ai/api/v1 | `openai/gpt-4o` | 128K |
| `openai` | api.openai.com/v1 | `gpt-4o` | 128K |
| `moonshot` | api.moonshot.cn/v1 | `kimi-k2-0905-preview` | 128K |
| `dashscope` | dashscope.aliyuncs.com/compatible-mode/v1 | `qwen-max` | 128K |
| `zhipu` | open.bigmodel.cn/api/paas/v4 | `glm-4.6` | 128K |
| `siliconflow` | api.siliconflow.cn/v1 | `deepseek-ai/DeepSeek-V3` | 64K |

> 其他 OpenAI 兼容端点用 `--base-url` + `--api-key` 即可。
> 代码内置轻量 .env 解析器，**没装 python-dotenv 也能跑**。

skill 目录默认自动探测：
`--skill-dir` → `PPT_MASTER_SKILL_DIR` 环境变量 → 从本文件向上找
`.agents/skills/ppt-master` 或 `ppt-master/skills/ppt-master`。

**开工前先体检**（不会消耗多少 token，但能把认证/端点/模型名错误提前暴露）：

```bash
python agent.py --check
```

输出示例：

```
🔎 模型接入
   provider   = deepseek
   base_url   = https://api.deepseek.com/v1
   model      = deepseek-v4-flash
   key 来源   = DEEPSEEK_API_KEY
   key 值     = sk-xxxx...xxxx（长度 35）
   上下文窗口 = 1,024,000 tokens
   预载策略   = full
   计量方式   = 提供商上报 token，85% 时折叠

🔌 预检模型连通性...
✅ 连通性正常
```

---

## 3.1 提示词预载策略（小上下文模型必读）

quick profile 的权威文档本身就占 **62,526 字符**。全部预载会让小窗口模型
（如硅基流动 64K）提示词一填就满，跑不了几页就溢出。所以按窗口自动分级：

| `--preload` | 预载内容 | prompt 大小 | 适用 |
|---|---|---|---|
| `full` | SKILL.md + routing.md + profile 权威文档 | ≈ 68,000 字符 | ≥128K 窗口 |
| `lean` | SKILL.md + routing.md | ≈ 30,000 字符 | 64K 窗口 |
| `minimal` | 仅 SKILL.md | ≈ 15,000 字符 | 极端受限 |
| `auto`（默认） | 按提供商窗口自动选 full / lean | — | — |

精简模式下权威文档改为**工具懒加载**，但提示词会强制要求模型
先 `read_skill_doc(...)` 读完才能开始创作——预载变少，强制读取的要求不变。

### 容量预检（开工前必看）

ppt-master 要求开工前读完的文档总量约 **235,000 字符**（15 份；实际值会随 skill
更新而变，以 `python agent.py --check` 打印的「强制阅读清单」为准）。
如果这个量超过模型窗口，会发生「读完 → 折叠 → 忘掉 → 重读」的抖动——
**表面上看只是“很慢”，实际上永远进不到创作阶段，一页也产出不了**。

所以启动时会先算一笔账：

```
📐 ⚠️  容量偏紧：重任务是可行的，但中途可能触发历史折叠。
   强制阅读清单 = 235,617 字符（15 份文档）
   system prompt = 67,673 字符
   开工前已占   ≈ 86,654 tokens（按 3.5 字符/token 估算）
   可用窗口     = 116,000 tokens（128,000 减去输出预留 12,000）
   占用率       ≈ 75%
```

占用率 ≥95% 直接报错终止；≥70% 给警告；否则绿。
`--context-tokens` 用于声明模型真实窗口（同一模型名在不同渠道可能不同），
`--no-capacity-check` 可跳过（不推荐）。

---

## 4. 用法

```bash
# 无人值守快速生成（推荐）
python agent.py --profile quick "做一份 8 页的《向量数据库入门》PPT"

# 显式指定提供商
python agent.py --provider deepseek --profile quick "做一份 8 页的 PPT"

# 长 brief 走文件
python agent.py --profile quick --task-file brief.md

# 标准路由，遇到 BLOCKING 门会停下来问你
python agent.py --profile default "把 report.pptx 美化一下"

# 指定 skill / 输出目录 / 自定义端点 / 换模型
python agent.py --skill-dir "D:/ppt-master/skills/ppt-master" \
                --output-root "D:/ppt-out" \
                --base-url "https://my-gateway.internal/v1" --api-key "sk-xxx" \
                --model "deepseek-v4-flash" \
                --profile quick "做一份季度复盘"
```

### 两个 profile 怎么选

| | `quick`（默认） | `default` |
|---|---|---|
| 交互 | 全程无人值守，所有设计决策由模型直接定 | 遇 ⛔ BLOCKING 门停下问你 |
| 产物 | 只产出 SVG + PPTX | 额外产出 `design_spec.md` / `spec_lock.md` |
| 适用 | 批量生成、自动化、脚本调用 | 需要精细控制、品牌对齐、要复现的正式交付 |
| 代价 | 无设计记录，改稿只能直接改 SVG | 慢、轮次多，必须有终端交互 |

---

## 5. 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--max-steps` | **0** | **默认不限步数**。步数与工作量不成比例，拿它截断会砍断未完成的 deck |
| `--stall-abort` | 7 | 同一动作完整重复到多少次判定卡死。0 = 关闭 |
| `--loop-detect` | 开 | 窗口内动作多样性过低时判定死循环 |
| `--preload` | auto | 预载深度，见 §3.1 |
| `--context-tokens` | 自动 | 声明模型真实上下文窗口（token）；影响预载深度与容量预检 |
| `--capacity-check` | 开 | 开工前校验强制阅读量能否装进窗口 |
| `--compact-threshold` | 0.85 | 真实用量达到窗口的该比例时才折叠中段历史 |
| `--context-budget` | 0 | 手动字符预算模式，**仅用于不上报 usage 的端点**；0=用真实 token 计量 |
| `--max-observation-chars` | 6000 | 单条工具结果回灌上限。保留头 60% + 尾 30%，尾部用于接住报错 |
| `--command-timeout` | 900 | 单条命令超时（秒）。导出 PPTX 较慢，别调太小 |
| `--parallel-pages` | 1 | P3 逐页生成并发度。默认 1 = 串行（思考型模型实测并行更慢）；>1 时多页并行。见 §7 |
| `--plan-model` | = `--model` | staged 规划/填值阶段（P1/P2）用的模型；可配更快的 |
| `--execute-model` | = `--model` | staged 执行/修复阶段（P3/P4）用的模型；可配更强的 |
| `--confirm-commands` | 关 | 开启后才会在执行命令前询问。**默认关闭 = agent 有全局执行权限** |

---

## 6. 循环保护

原版是 `while True`，唯一出口是模型输出 `<final_answer>`。这里**不靠步数截断**，
而是检测“到底是不是真的在打转”。

### 为什么不用步数上限

步数与实际工作量不成比例：20 页的 deck 很容易跑过 200 步，而 3 页的可能 30 步就完。
拿一个拍脑袋的阈值去卡，结果就是把正常任务砍断。**所以默认 `--max-steps 0`（不限）**，
真正交付才停。

### 三道循环检测

| 检测 | 触发条件 | 行为 |
|---|---|---|
| 重复动作 | 同一工具 **+ 同一参数** 完整重复 | 第 4 次植入提醒；第 7 次终止（`--stall-abort`） |
| 死循环横跳 | 最近 10 个动作里只有 ≤ 2 种 | 终止。专门抓 `A,B,A,B` 这类单看“重复次数”抓不住的打转 |
| 格式兜底 | 连续输出既无 `<action>` 也无 `<final_answer>` | 第 5 次终止，附最后一段输出 |

多样性 ≤ 3 种时只在观察里提醒；≤ 2 种才终止。心跳每 50 步打一条，便于无人职守
时判断进程是否还活着。

### 提醒会区分两种“重复”

> ⚠️ 停滞提醒：这个完全相同的动作你已经调用第 4 次了。
> 如果是在返工，请改变策略（换工具、换参数、先读相关文档）而不是重复同一次调用；
> 如果是有意重试（例如刚改完文件、重跑校验），请在 `<thought>` 里说明理由；
> 重复到 7 次会被强制终止。

所以合法重试（改了文件再跑一遍校验）不会被误杀。

### 实测

```
测试1  200 步各不相同（正常长任务） -> 实际 201 步跑完，未被截断   ✅
测试2  A,B,A,B 横跳                -> 终止：最近 10 个动作里只有 2 种  ✅
测试3  同一动作反复                 -> 终止：同一动作已完整重复 7 次    ✅
```

真正限制长任务的是**上下文窗口**，不是步数。每一步都会打印真实用量：

```
📊 上下文 52,431 / 128,000 tokens (41.0%)
```

这个数字来自提供商上报的 `usage.prompt_tokens`，**不是字符数估算**。达到
`--compact-threshold`（默认 85%）时才折叠中段历史，并在日志里写明触发原因。
如果模型从不上报 usage，agent 不会去猜，而是一律不折叠。

---

## 7. 性能：慢在哪里，以及怎么改

### 时间账（一次真实的 4 页任务，总 269.1s）

```
总时长 269.1s
├─ 本地脚本  7.4s   (2.8%)   8 次调用，最快 34ms，最慢 2.5s
└─ 模型往返 261.7s (97.2%)
   ├─ 107.1s  准备（研究 + 读 15 份强制文档）
   ├─ 112.8s  逐页创作（4 页）
   └─  43.1s  校验返工 + 导出
```

**模型本身不慢。** 流式实测 DeepSeek：首 token 延迟 1.55s（含 16,105 token 预填充，
前缀缓存命中时预填充几乎免费），生成速率 273 tokens/s。一页 3 KB 的 SVG ≈ 2,000 token，
生成只要 7 秒。

**慢在往返次数：** 261s ÷ 4s ≈ **65 次往返**。

### 三个已修复的原因

| 原因 | 后果 | 修复 |
|---|---|---|
| 一轮只能发一个 `<action>`（原 ReAct 写法） | 光读 15 份强制文档就要 15 次往返 | 支持**一轮多个 `<action>`**，一次执行、合并回灌 |
| 没有批量读文档的工具 | 每份文档一次往返 | 新增 `read_skill_docs("a.md, b.md, c.md")` |
| 文档读取复用了命令输出的 6,000 字符上限 | `executor-base.md`（52,977 B）**只读到 10%**，模型拿着残缺规则写作，被校验门报出 6 个错→额外返工 | 文档类读取单独用 80,000 上限 |

第三条最隐蔽：它同时损害质量和速度——读不全 → 写错 → 校验失败 → 返工往返。

### 现在的可观测性

每一步都会打印真实用量与耗时，不再需要事后推断：

```
📊 上下文 41,208 / 128,000 tokens (32.2%)   ⏱️ 模型 3.4s
🔧 Action: read_skill_docs(references/plan-core.md, references/executor-base.md)
🔍 Observation（1 项，耗时 0.1s）：批量读取 2 份文档：
```

任务结束时给出账本：

```
⏱️  耗时账本
  模型调用     = 38 次
  模型耗时     =    142.6s  (97%)
  工具耗时     =      4.1s  (3%)
  平均往返     =     3.75s
  输入 tokens  = 1,284,903（峰值 52,110）
  输出 tokens  = 27,455
```

### 真正的瓶颈：隐藏推理 token

分阶段注入把预填充降了 63~82%，**但墙钟时间几乎没变**。因为预填充本来就不是瓶颈：

```
实测一次 P3 页页创作调用：
  预填充   23,632 tokens      ← 很少
  输出     20,093 tokens
     ├─ 可见  ≈ 2,988 tokens   （5,976 字符的 SVG）
     └─ 隐藏  ≈ 17,105 tokens  ← 85% 的输出是看不见的推理！
  用时 82.1s（≈245 tokens/s，模型跑在满速，只是在生成 7 倍的量）
```

**DeepSeek V4 默认开启思考模式**（官方 `deepseek-v4-flash` / `v4-pro` 默认 `enabled`）。
这不是模型慢，是在生成 7 倍的 token。

### DeepSeek 不是“只有思考模式”——默认开，但可以关

实测（同一提示词，`deepseek-v4-flash`）：

| 开关 | 输出 tokens | 隐藏推理 | 耗时 |
|---|---|---|---|
| 无参数（= 默认） | 4,521 | 4,033（**89%**） | 19.7s |
| `--thinking enabled`（显式） | 5,178 | 4,572 | 21.8s |
| `--reasoning-effort low` | 3,487 | 2,856 | 15.3s |
| `--reasoning-effort high` | 3,425 | 2,944 | 14.9s |
| `--thinking disabled` | **528** | 58 | **2.8s** |
| `--reasoning-effort none` | 583 | 93 | 3.0s |

关闭手段有三种（官方均支持）：`thinking.type=disabled`、`reasoning_effort=none`，
或在 OpenAI SDK 里用 `extra_body` 传前者。代码已封装为 `--thinking` 与 `--reasoning-effort`。

**但快不等于能用。** 在同一个 PPT 任务上：

| 配置 | 模型调用 | 校验 | 导出 |
|---|---|---|---|
| 默认（思考开） | **4** | **0 错误** | ✅ |
| `--reasoning-effort low` | 5 | **0 错误** | ✅ |
| `--thinking disabled` | 9 | 1 残留 | ❌ 被拒 |

关掉思考省下的是**排版算术能力**：文字宽度、边界重叠、容器溢出这些约束靠推理算，
不算就只能试错，而试错比推理更贵。

### 三种思考模式实测对比（同一个 2 页任务）

| 配置 | 模型调用 | P1 | P2 | P3（2页） | 校验 | 导出 | 总耗时 |
|---|---|---|---|---|---|---|---|
| 默认（思考开） | **4** | 11.6s | 48.7s | 241.4s | **0 错误** | ✅ | 305.7s |
| `--reasoning-effort low` | 5 | 6.6s | 28.7s | 209.1s | **0 错误** | ✅ | ≈260s |
| `--thinking disabled` | 9 | 4.1s | 18.8s | **22.5s** | 1 残留 | ❌ 被拒 | — |

**反直觉但重要：`--thinking disabled` 单次快 6~8 倍，但交付失败。**
它省下了推理，也丢掉了排版算术能力——校验器报出 9+3 个 blocking 错误，
修复 3 轮仍不收敛，导出被拒。而思考开时 **4 次调用就 0 错误**。

> **思考模式在“赚回”自己**：它省掉的返工比它多花的时间更值。
> 本任务是 SVG 坐标与字体度量的算术活，恰好是推理最能发挥的地方。

### 可用的旋钮

| 旋钮 | 效果 | 建议 |
|---|---|---|
| `--thinking disabled` | 单次快 6~8× | 仅在简单 deck（无密集文字排版）时考虑 |
| `--reasoning-effort low` | 快 ~15%，质量持平 | 想稳步提速时用它 |
| `--max-tokens` | 思考模式下需 ≥65,536 | 太小会截断成半截页面；模型上限 384K |
| `--mode react` | 每步模型自己决定 | 仅用于对照，调用次数多一个数量级 |
| `--parallel-pages N` | 视模型而定 | 仅「输出少、并发友好」的模型划算，详见下 |
| `--plan-model`/`--execute-model` | 各阶段换模型 | flash 做规划填值、pro 做执行，详见下 |

### P3 逐页并行（实测：对思考型模型不划算，默认关闭）

P3 支持 `--parallel-pages N` 多页并行生成，但**实测 DeepSeek 思考型模型下 4 路并行
总耗时反而比串行更长**。原因：

1. **前缀缓存失效**：并发请求同时发出都不命中缓存，input token ×N 且每路都完整预填充；
2. **算力切分而非叠加**：思考型模型单请求已吃满服务端算力（85% 输出是隐藏推理），
   并发只是把吞吐切成 N 份，生成速率骤降，还可能触发限流排队。

**结论：对「输出 token 多、思考开启」的模型，串行 + 前缀缓存更快（默认即串行）。**
并行仅适合「输出 token 少、并发友好」的模型/端点；要启用请先小 deck 实测
（`--parallel-pages 2` 起步），确认墙钟确实下降再放大。

### 按阶段选模型（不依赖并发，直接缩短单次耗时）

staged 各阶段对模型能力的需求差异很大，可用两个参数分级：

| 阶段 | 工作 | 建议模型 |
|---|---|---|
| P1 规划 / P2 填值 | 判断 + 填 schema | 快模型（如 `deepseek-v4-flash`） |
| P3 写 SVG / P4 修复 | 排版算术 + 精确坐标 | 强模型（如 `deepseek-v4-pro`） |

```bash
python agent.py --plan-model deepseek-v4-flash --execute-model deepseek-v4-pro \
    --profile quick "做一份 8 页的《向量数据库入门》PPT"
```

收益：规划/填值阶段用快模型，省下的时间立竿见影；而真正吃排版算术的 P3/P4 仍用
强模型，不牺牲返工率。这比「全局换强模型」省钱，比「全局换快模型」更稳。

### 分阶段注入到底买到了什么

| 阶段 | 注入量 | 预填充 |
|---|---|---|
| P1 PLAN | 规划权威 + 能力批次 171 KB | 40,021 tokens |
| P2 FREEZE | 计划 + schema + 脚手架 22 KB | **14,165**（↓82%） |
| P3 EXECUTE | 锁 + 标定表 + 执行核心 105 KB | **26,268**（↓66%） |
| P4 VERIFY | 锁 + 标定表 + 执行核心 + 待修页 | **30,018**（↓62%） |

另外 P3 每交付一页就把整页 SVG **替换为紧凑签名**，避免 N 页 ×8KB 被反复预填充：

```
🗜️ 上下文已压缩为签名：cover | 左右非对称分栏：左 680px 文字主轴（眉标→48px 标题→…）
```

签名还顺便服务于 skill 要求的「几何签名比对」——跨页一致性靠它守住。

**结论：分阶段注入是正确的基础设施（预填充↓66~82%、上下文不再无限增长），
但它不是速度问题的答案。速度在推理模式，而推理模式是质量与速度的真实取舍。**

---

### 实时进度（流式）

思考模式下单次调用可达 60~160 秒，其中 ~85% 是不可见的推理 token。
非流式会让终端像卡死几分钟，所以 `call_model` 默认流式，实时显示推理进度：

```
⠙ 生成中   6.0s | 推理   1,054 chunk | 正文       0 字符   ← 6 秒全在推理，一字未出
⠹ 生成中   9.0s | 推理   1,505 chunk | 正文     454 字符
✓ 用时   9.2s | 推理   1,505 chunk | 正文 501 字符
```

流式不改变 token 消耗，也不改变结果，只是把过程变得可见。

### 交叉对比需要多把 key

代码会校验 key 形态。若 `OPENAI_API_KEY` 与 `DEEPSEEK_API_KEY` 的 **sha256 相同**
（即同一个 DeepSeek key 被填到了两个变量名），`--provider openai` 实际会把
DeepSeek key 发往 api.openai.com 而吃 401——这正是 §9 里 401 的典型来源。

- 想跟非思考型模型做交叉对比，需要**新增一把**对应的 key，而不是复用同一把。
- 同一把 key 下也能做对比：`deepseek-v4-flash` vs `deepseek-v4-pro`。

---

## 8. 安全边界

- **只读工具**（`read_file` / `list_directory` / `read_skill_doc` / `grep_skill_doc`）
  可以读 skill 目录；`read_skill_doc` 额外做 resolve 后包含性校验，`../` 越界直接
  `PermissionError`。
- **`run_command` 默认全局放行**：本机已授予全局执行权限，不弹确认、不卡 `input()`。
  默认情况下什么都能跑（`pip install`、`rm`、任意脚本）。需要护栏时加
  `--confirm-commands`，此时退回“只读命令 + skill 自带脚本”白名单，其余命令交给用户确认。
- **命令超时**仍生效（默认 900s），防止单条命令挂死。
- 所有命令都会完整打印，有日志可查。
- **写操作**按输出根目录解析，相对路径不会误写到 skill 目录里。
- **完整性闸门不可绕过**：`attribution_guard.py` 非 0 时代理被要求立即终止，而不是"修复"。

---

## 9. 故障排查

### `401 Missing Authentication header` / `AuthenticationError`

**几乎总是 base_url 与 key 不是同一家。** 典型场景：把 DeepSeek 的 key 填到了
`OPENROUTER_API_KEY`，请求就发去了 openrouter.ai。

怎么区分：

| 提供商 | key 特征 |
|---|---|
| OpenRouter | 以 `sk-or-` 开头，约 73 字符 |
| DeepSeek | `sk-` + 32 位十六进制，共 35 字符 |

代码会自动告警并打印实际的 `provider / base_url / key 来源 / key 脱敏值`。
修复：

```bash
python agent.py --provider deepseek "..."      # 临时指定
# 或在 .env 中：
#   DEEPSEEK_API_KEY=sk-...
#   PPT_AGENT_PROVIDER=deepseek
```

先跑 `python agent.py --check` 体检，能直接定位。

### `model not found` / 404

该提供商没有这个模型名。用 `--model` 指定它支持的（如 `deepseek-v4-flash` /
`deepseek-v4-pro`），或 `--provider` 换一家。

### 跑到一半提示「上下文已折叠」或溢出

64K 窗口的模型建议：`--preload lean`、`--max-observation-chars 4000`，
并把页数控制在 8 页以内。想跑大 deck 请换 ≥128K 窗口的模型（`--preload full`）。

---

## 10. 已知限制

1. **首轮提示词约 2.2 万 token**。这是预载权威文档的代价，换来的是模型不会凭记忆编规则。
2. **手写 SVG 是主要成本**。页数越多，轮次和 token 线性增长。建议先用 5–8 页验证链路。
3. **`quick` 模式丢上下文即重来**。这是 skill 的明文设计（无 spec/lock 记录），不是本实现的缺陷。
4. **模型能力是硬约束**。SVG 排版涉及精确坐标与字体度量，弱模型会反复过不了
   `svg_quality_checker.py`。默认用 `openai/gpt-4o`，可换更强的模型。
5. **图片生成/搜索需要额外凭据**。见 `<SKILL_DIR>/.env.example`；未配置时按 skill 的
   「no-AI replan」规则降级为原生图形，不会中断流程。
