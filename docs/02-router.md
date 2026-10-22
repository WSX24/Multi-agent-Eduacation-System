# 集成路由器 V1.0 · 使用文档

把「提示词模板库（`PromptTemplate/`）」和「PPT-Agent」串成一条可路由的链路：
用户给一句话，路由器判断走哪条路，需要 PPT 时调 PPT-Agent 产片。

本文件是 `集成路由V1.0/` 的完整使用文档。模块内的 `README.md` 只留目录与指路。

---

## 1. 打包清单（哪些进仓、哪些是产出）

### 1.1 必须入仓

| 路径 | 作用 | 说明 |
|---|---|---|
| `集成路由V1.0/router.py` | 唯一入口 | 路由判定 + 调 pipeline |
| `集成路由V1.0/export_notes.py` | 讲稿/页面说明导出 | router 直接 import |
| `集成路由V1.0/.env.example` | 配置模板 | 实际 key 在 `PPT-Agent/.env` |
| `PromptTemplate/prompt_builder.py` | 模板渲染 | router `from prompt_builder import PromptLib` |
| `PromptTemplate/quality_gate.py` | 输入校验 | pipeline 依赖 |
| `PromptTemplate/unit_bridge.py` | 单元内容 → PPT 内容 | pipeline 依赖 |
| `PromptTemplate/eval_generic.py` | 结构校验 | pipeline 依赖 |
| `PromptTemplate/templates/*.yaml` | **模板源**（12 个） | 渲染用 |
| `PromptTemplate/fewshots/*.yaml` | **示例源**（11 个） | 渲染用 |
| `PromptTemplate/cot/snippets.yaml` | CoT 片段 | 可选（代码允许最小模板目录） |
| `PPT-Agent/pipeline.py` | 产片主流程 | router 用 subprocess 调它 |
| `PPT-Agent/staged.py` `agent.py` `prompt_template.py` | 阶段编排 / LLM 调用 | pipeline 依赖 |
| `PPT-Agent/image_pipeline.py` | **P2.6 取图** | `staged.py` 动态 import，缺了配图整段失效 |
| `PPT-Agent/plot_math.py` `bounds_fix.py` | 几何绘图 / 边界修正 | 数学类版面依赖 |
| `PPT-Agent/check_image_sources.py` | 图源体检 | 排查配图不可用 |
| `PPT-Agent/LOCKED-DEFAULTS.md` | 锁定默认值 | 改参数前必读 |
| `PPT-Agent/pyproject.toml` `.env.example` | 依赖与配置模板 | — |

### 1.2 已在 `.gitignore` 排除（不要入仓）

| 路径 | 体积 | 为什么 |
|---|---|---|
| `集成路由V1.0/ppt/` | 59M | 产出的 PPTX + 讲稿 + skill 项目目录 |
| `集成路由V1.0/templates/` | 528K | **产出**：发给 LLM 的提示词副本，按日期归类 |
| `集成路由V1.0/__pycache__/` | 96K | Python 缓存 |
| `PPT-Agent/runs/` | — | 单次运行日志 |
| `PPT-Agent/deliver/` | — | 交付物 |
| `.tmp_lotus/` | 1.4M | 临时目录 |
| `node_modules/` | — | 前端依赖 |
| `.env` / `*.pptx` / `*.docx` | — | 密钥与二进制产物 |

### 1.3 命名陷阱（读代码前务必知道）

仓库里有**两个 `templates/`**，含义完全不同：

```
PromptTemplate/templates/        ← 模板源，要入仓
集成路由V1.0/templates/          ← 产出日志，不入仓（已 gitignore）
```

---

## 2. 五条路由

| 路由 | 触发信号 | 走哪个模板 | 产出 |
|---|---|---|---|
| `ppt` | 明确要 PPT/课件 | `teacher_ppt`（默认）/ `ppt_general` | PPTX |
| `plan` | 系统学完/备考/长期 | `teacher_planner` | 课程蓝图（章节/单元） |
| `sprint` | 一周/考前/突击 | `teacher_sprint` | 一次性速成交付 |
| `lesson` | 单点讲解 | `teacher` | 单次讲解 |
| `qa` | 提问 | `qa` | 引导式答疑 |

判定是**规则版**，关键词优先级：`ppt > qa > sprint > plan > lesson`。

---

## 3. 用法

```bash
cd 集成路由V1.0

# 直接要 PPT（自动路由 + 产片）
python router.py "做一份 12 页的《圆锥曲线解题技巧》PPT"

# 长期系统学习（产出课程蓝图）
python router.py "系统学完初中化学" --weeks 8 --hours-per-week 3

# 短期速成
python router.py "一周搞定浮力"

# 单点讲解 / 答疑
python router.py "讲讲因式分解"
python router.py "这道题为什么做错了"

# 只渲染提示词不调 LLM（省钱探路）
python router.py "做一份 8 页的《一次函数》PPT" --dry-run

# 强制路由 / 指定 PPT 模板角色
python router.py "帮我系统学一下初中数学" --route plan --weeks 10
python router.py "做一份 PPT" --route ppt --role ppt_general
```

### 关键参数

| 参数 | 说明 |
|---|---|
| `--route ppt\|plan\|sprint\|lesson\|qa` | 强制走某条路（默认自动判定） |
| `--dry-run` | 只渲染+保存提示词，不调 LLM、不产片 |
| `--topic / --subject / --level / --pages` | 覆盖自动抽取 |
| `--role teacher_ppt\|ppt_general` | ppt 分支用的模板角色 |
| `--reasoning-exec none\|low\|medium\|high\|max` | P3 逐页写 SVG 推理强度（**默认 none**） |
| `--reasoning-repair none\|low\|medium\|high\|max` | P4 修复推理强度（**默认 low**） |
| `--weeks / --hours-per-week` | plan 分支的总周期与每周课时 |
| `--days` | sprint 分支的可用时限 |

---

## 4. 文本导出（讲稿 + 页面说明）

产片后自动导出两份与 PPTX 同级的 `.md`（纯抽取，不额外调 LLM）。

### ① 讲稿 —— `<stem>.md`

逐页「标题 + 旁白」，可直接喂 TTS / 配音 / 提词。

- **旁白来源**：优先读 PPTX 自带备注；为空则回退到 skill 项目的 `notes/total.md`。
- **页标题来源**：优先 `design_spec.md` §IX（`#### Slide NN - 标题`）；没有则取该页字号最大的文字。

### ② 页面说明 —— `<stem>_页面说明.md`

逐页给出【核心信息】【画面构成】【层次关系】【强调线索】【页面文字】，并在顶部给一行
**视觉基线**（画布、风格、主色、强调色、字体），让下游知道高亮该用哪个色。

| 内容 | 来源 |
|---|---|
| 核心信息 / 画面构成 / 层次关系 / 页面内容 | `design_spec.md` §IX |
| 强调色 / 主色 / 字体 / 画布 | `spec_lock.md` |
| 页面文字（最终实际文字） | `svg_output/*.svg` |

### 对已有产物单独重导

```bash
python export_notes.py "ppt/20261017/圆锥曲线解题技巧_195112"            # 两份都导
python export_notes.py "ppt/20261017/圆锥曲线解题技巧_195112" --only notes
python export_notes.py "ppt/20261017/圆锥曲线解题技巧_195112" --only pages
python export_notes.py "某个文件.pptx" -o 讲稿.md
```

目录只取**顶层** pptx（skill 项目里 `exports/` 的中间产物不算）。

---

## 5. 配图（P2.6）

`ppt` 分支默认会配图（`--image-source auto`）：设计稿里声明需要真实影像的页，
代码会去图库搜图或调生图后端，取不到就自动退回 SVG 原生绘制，不会中断。

```bash
python router.py "做一份 8 页的《宝黛初会》PPT"                  # 默认 auto
python router.py "..." --image-source off                        # 关掉配图
python router.py "..." --image-source web --image-review manual  # 只备候选等人挑
python router.py "..." --image-providers shuge,pixabay           # 指定图库顺序
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--image-source` | `auto` | `off` / `web` / `ai` / `auto` |
| `--image-review` | `score` | `auto` / `score` / `manual` |
| `--image-providers` | 见下 | 图库优先级，逗号分隔 |
| `--image-max-candidates` | `8` | 每张图候选数 |
| `--no-image-strict` | 关 | 关掉相关性闸门（**不建议**） |

`--image-providers` 不指定时用 `image_pipeline.DEFAULT_PROVIDERS`：

```
shuge → met → pexels → pixabay → wikimedia → openverse
```

> 人物/文物/版画/古诗文类需求会临时重排为
> `met → wikimedia → openverse → shuge → pixabay → pexels`（博物馆有分类与藏品元数据）；
> 实景/风景类重排为 `pixabay → pexels → met → ...`。
> 这是代码里的 `_provider_order_for()`，不看文档也会自动生效。

### 图源一览

| 图源 | key | 授权 | 擅长 |
|---|---|---|---|
| `met` | 不需要 | **CC0 / 公有领域** | 中国古画（山水/花鸟/竹石/立轴）、文物。实测「竹石图」拿到 2404×4000 立轴 |
| `shuge` 书格 | 不需要 | CC BY-NC 4.0（教学可用，需署名） | 古籍整书扫描（书法、版画、册页） |
| `pixabay` | `PIXABAY_API_KEY` | Pixabay License | 实景照片、花卉、风景 |
| `pexels` | `PEXELS_API_KEY` | Pexels License | 实景照片 |
| `wikimedia` | 不需要 | PD/CC | 综合；**国内直连常不通** |
| `openverse` | 不需要 | CC 系 | 综合；**国内直连常不通** |
| `local` | — | 本地库（PD） | 首批命中，不联网，同样要过闸门 |

**Met 的两个坑**（2026-10-22 实测）：

1. 它的 `departmentId=6` 是「亚洲艺术」，**日本藏品占大头**。搜 `Chinese landscape`
   的 10 条里 7 条是日本浮世绘/屏风（只是画了中国题材）。所以代码里强制
   `culture` 含 `China`，否则会给古诗词课件配日本画。
2. **具体中国名人它一件也没有**：`Li Qingzhao`、`Yue Fei` 搜索结果都是 0。
   李清照画像、岳飞画像这类需求仍会降级为「不配图」。

### 配图不工作时按这个顺序查

```bash
python ../PPT-Agent/check_image_sources.py        # ① 图源体检（不打 LLM、不花钱）
```

1. **`网络=不通(代理ProxyError)`** —— 最常见。`PPT-Agent/.env` 里的
   `PPT_IMAGE_PROXY` 指向了一个连不上的代理。清空该行（或改成可用的
   `http://127.0.0.1:7897`）再试。pixabay / shuge 通常可直连，
   wikimedia / openverse 在国内直连常不通。
2. **`凭据=未配置`** —— `PIXABAY_API_KEY` / `PEXELS_API_KEY` 没填。
3. 都通但配不到图 —— 看 `ppt/.../images/rejected.json` 的 `reason`
   字段，以及运行日志里的 `[p2-diag]` 行（见 §7）。

---

## 6. 速度与推理强度（实测 2026-10-19）

P3 逐页写 SVG 是唯一的大头，`--reasoning-exec` 直接决定它的耗时：

| `--reasoning-exec` | 秒/页 | 12 页 P3 耗时 | 说明 |
|---|---|---|---|
| `none`（默认） | 6~9 | ~90s | 坐标/字数由代码算好，关思考够用 |
| `low` | 23~41 | 240~500s | 版面复杂、想更稳时用 |
| 未设（继承全局 low） | 44~87 | 500~870s | 别用 |

慢的根源是**隐藏推理 token**：开 `low` 时单页输出里 ~85% 是看不见的思考，
一单 12 页能烧到 14 万输出 token。

`--reasoning-repair` 默认仍是 `low`——P4 只修失败页，调用次数少，保留判断力更划算。

⚠️ 只有走 `router.py` 才是快的。直接跑 `pipeline.py` / `agent.py` 会继承全局 `low`，
P3 慢 4 倍。详见 `PPT-Agent/LOCKED-DEFAULTS.md`。

---

## 7. 已知局限（V1.0，如实记录）

### 7.1 配图闸门与排序：现在的口径

**筛选（闸门，决定"要不要这张"）**

| 规则 | 行为 |
|---|---|
| 一个关键词都不中 | **100% 排除**（中文 0 命中 + 英文兜底也不中） |
| 只命中时间/氛围词（月夜/夕阳/远景…） | 判不命中（氛围词不算"画对了对象"） |
| 命中建筑级现代意象（摩天大楼/天际线/高楼大厦/玻璃幕墙） | **硬否决**（`_MODERN_HINTS` 硬档） |
| Reference 缺 `实体｜` 前缀 | 仍过题材覆盖，但运行日志会点名警告 |
| Reference 为空 | 放行（无从校验；仍受 relevance 约束） |

**排序（命中词数优先，决定"选哪张"）**

- 主键：**命中的关键词个数**——命中越多越靠前，全中最好。
- 次键：风格偏好（画种 / 竖幅 / PD / 分辨率）。
- 现代气息**只降权不否决**：`城市 / 现代 / 写真 / 街景 / city / urban` 这类词，
  一般 −1.0，古诗古文/古画类 **−2.5**。
  所以「现代的古风摄影」可用，只是排在古画后面；真实荷花照对《荷塘月色》不扣分。

**两层职责不混**：否决只在闸门，排序永否决不了任何东西。

**实跑量级**

| 册子 | 改前 | 改后 |
|---|---|---|
| 赤壁赋（6 行） | 4 张，含 1 张纯月亮照 | 3 张，0 张错图 |
| 李清照·卖花担上（6 行） | 5 张，含猫/向日葵/红玫瑰情侣照共 3 张错图 | 2 张，0 张错图（4 页退回原生 SVG） |

> 李清照那册是真事故：`李清照画像` 配了一张**猫**——猫的标签写着「猫画像」，
> 靠「画像」二字骗过了三道闸门。已修（通用类型词不算命中、实体剥通用后缀、
> relevance 跳过通用词）。用户口径：**没有就别放**。
> 本地图库同一张图每册只用 1 次（封面除外），避免相邻页放同一张图。

排查时看运行日志：

```
· [p2-diag] p09_scholars_drinking.jpg: 现判=收｜加权 0.29<0.35 → 拒｜命中 饮酒+0.7  ⚠分歧
```

`现判` = 当前闸门结论，`加权` = P2 加权覆盖会怎么判。**它只打印，不改变决定。**

**以后怎么防止复发（2026-10-22 起）**

| 防线 | 做法 |
|---|---|
| ① 内置回归 | `python ../PPT-Agent/image_pipeline.py --selftest` —— 11 条验收用例 + 3 条检索词用例，**每一条都对应一次真实事故**（猫/向日葵/月亮/缅甸古庙/鸢尾花/河岸图）。改验收规则前先跑，退化成 0 秒可发现 |
| ② 通用词不算命中 | 画像/肖像/照片/特写/局部… 只说明"这是哪类图"，命中它 ≠ 命中需求 |
| ③ 实体剥通用后缀 | `李清照画像` → `李清照`，否则 5 个字会跳过 2~4 字的实体严格校验 |
| ④ 实体必须进检索词 | `实体｜` 只当门槛不进检索词是旧坑：若查询段没重复实体，检索词就只剩「画像 古画」 |
| ⑤ 提示词契约 | 实体段**只写专名**，不写「画像/古画/照片」；查询段再写一遍实体 |
| ⑥ 失败要看得见 | 取不到就 `Needs-Manual` 退回原生 SVG，日志逐页打 `✗`；本地图库同一张图每册只用 1 次 |
| ⑦ 本地库也过闸门 | 以前 local 命中会把 `providers_try` 清空，**绕过全部校验**；现在 local 作为「虚拟图源」进同一循环，不过就换图库 |
| ⑧ 索引词要够 | 本地库 `subjects` 太薄会导致「有图但匹配不到」——花鸟画原来只登记了 落花/花鸟/梨花，需求写「折枝花卉」就零交集 |

**仍未覆盖**

| 缺口 | 例子 |
|---|---|
| 对象词本身是泛行为词时，现代照仍可能混入 | 「文人夜宴古画」收下宿醉派对照（命中「饮酒」即过；关键词法分不清"画里的饮酒"和"现代酒局"） |
| 无 `｜` 的行拿不到实体锚点 | 只能靠提示词修正格式 |

不影响稳定性（失败一律降级为原生 SVG），只影响配图准确率。

### 7.2 其它

- 路由是规则版，未接 LLM 兜底；复合需求（如「系统学完并每章出 PPT」）暂取单条路。
- `--image-source ai` 需要配 `IMAGE_BACKEND` + 对应生图 key，未配则自动回落 web。
- `集成路由V1.0/ppt/` 与 `templates/` 是产出，已 gitignore；**换机后没有历史产物**是正常的。

---

## 8. 依赖与配置

### 8.1 本仓内的依赖

- 依赖同级的 `PromptTemplate/`（模板库）与 `PPT-Agent/`（产片）。
- API Key 从 `PPT-Agent/.env` 读取（`DEEPSEEK_API_KEY` 等），模板见 `.env.example`。
- 非 ppt 分支调 LLM 需要 `openai` 包；模板渲染需要 `pyyaml`。
- 配图另需：`requests`；可选 `PIXABAY_API_KEY` / `PEXELS_API_KEY`。

### 8.2 必须单独安装的外部依赖：`ppt-master` skill（不入本仓）

**只有这一块不在仓里，但它是产出 PPTX 的最后一块拼图。**

`ppt-master` 是**独立仓库**（体量大，SKILL.md + 60~ 份参考文档 + scripts + 模板库），
本仓只负责驱动它，不 vendor 它。所以刚 clone 完是**跑不出 PPT 的**，需先装：

```bash
# 1) 把它 clone 到任意位置（下面以本仓同级的 ppt-master/ 为例）
git clone <ppt-master-skill-repo> ppt-master

# 2) 让 PPT-Agent 能找到它，二选一：
#    a) 环境变量（推荐，最明确）
export PPT_MASTER_SKILL_DIR=/绝对路径/ppt-master/skills/ppt-master

#    b) 放到约定位置（下面两个路径会被自动探测）
#       <本仓>/ppt-master/skills/ppt-master
#       <本仓>/.agents/skills/ppt-master   （本机是一个 symlink，指向上面那个）
```

探测函数是 `PPT-Agent/image_pipeline.py` 的 `resolve_skill_dir()`，
候选顺序：`PPT_MASTER_SKILL_DIR` → 从项目目录向上 → `<起点>/.agents/skills/ppt-master`
→ `<起点>/ppt-master/skills/ppt-master`。校验标志：`SKILL.md` + `scripts/attribution_guard.py`
（或 `scripts/image_search.py`）同时存在才算命中。

排查：

```bash
python ../PPT-Agent/check_image_sources.py     # 会一并报 skill 是否找到
```

---

## 9. 设计边界（V1.0）

- 提示词落盘保证「保存的 = 实际发给 LLM 的」：ppt 分支直接读 pipeline `--explain`
  的渲染结果，其余分支用 `PromptLib.build` 的返回值。
- 路由判定与产片解耦：`--dry-run` 只跑前一半，可零成本看提示词。
