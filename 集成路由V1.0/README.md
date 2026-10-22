# 集成路由器 V1.0

把「提示词模板库（`PromptTemplate/`）」和「PPT-Agent」串成一条可路由的链路：
用户给一句话，路由器判断走哪条路，需要 PPT 时调 PPT-Agent 产片。

> **完整使用文档见 [`docs/02-router.md`](../docs/02-router.md)**
> （用法、参数、文本导出、配图、打包清单、已知局限）

## 目录

```
集成路由V1.0/
├── router.py              # 集成路由器（唯一入口）
├── export_notes.py        # 讲稿 + 页面说明导出（router 直接 import）
├── README.md              # 本文件（指路）
├── .env.example           # API Key 模板（实际 key 在 PPT-Agent/.env）
├── templates/             # 提示词产出（按日期归类，自动创建，gitignore）
│   └── YYYYMMDD/
│       ├── ppt_圆锥曲线解题技巧_HHMMSS_prompt.md   # 发给 LLM 的提示词
│       └── plan_初中化学_HHMMSS_result.md          # 非 ppt 分支的 LLM 结果
└── ppt/                   # PPT 产出（自动创建，gitignore）
    └── YYYYMMDD/
        └── 圆锥曲线解题技巧_193503/
            ├── 圆锥曲线解题技巧_193503.pptx        # 成品
            ├── 圆锥曲线解题技巧_193503.md          # 讲稿（逐页标题 + 旁白）
            ├── 圆锥曲线解题技巧_193503_页面说明.md # 内容/视觉/强调线索
            └── conic_sections_skills_.../          # 产出它的 skill 项目文件夹
```

⚠️ 注意 `templates/` 有**两个**：本目录下的是**产出日志**（gitignore），
模板源在 `PromptTemplate/templates/`。

## 五条路由

| 路由 | 触发信号 | 走哪个模板 | 产出 |
|---|---|---|---|
| `ppt` | 明确要 PPT/课件 | `teacher_ppt`（默认）/ `ppt_general` | PPTX |
| `plan` | 系统学完/备考/长期 | `teacher_planner` | 课程蓝图（章节/单元） |
| `sprint` | 一周/考前/突击 | `teacher_sprint` | 一次性速成交付 |
| `lesson` | 单点讲解 | `teacher` | 单次讲解 |
| `qa` | 提问 | `qa` | 引导式答疑 |

## 最快上手

```bash
cd 集成路由V1.0

python router.py "做一份 12 页的《圆锥曲线解题技巧》PPT"   # 自动路由 + 产片
python router.py "一周搞定浮力"                          # 短期速成
python router.py "这道题为什么做错了"                     # 答疑

python router.py "做一份 8 页的《一次函数》PPT" --dry-run  # 只渲染提示词，不调 LLM
python ../PPT-Agent/check_image_sources.py               # 配图前先体检图源
```

其余参数（`--route` / `--role` / `--reasoning-exec` / `--image-*` / `--weeks` 等）
见 [`docs/02-router.md`](../docs/02-router.md)。
