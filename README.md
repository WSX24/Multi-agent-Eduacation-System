PromptTemplate/
├── README.md                 # 本文件：索引 + 变量表 + 使用说明
├── pyproject.toml            # 打包配置（pip install -e . 用）
├── requirements.txt          # 依赖声明
├── prompt_builder.py         # 核心加载器（build_prompt 的实现）
├── quality_gate.py           # 生产级质量闸门 + 算法化照抄检测 + 入口预检
├── demo_course_flow.py       # 课程链路 demo + 门控参考实现（无需 API Key）
├── eval_planner.py           # 规划模板真实模型评测器（18 项断言 + 重试闸门）
├── eval_unit.py              # 单元渲染模板评测器（含学情差分测试）
├── eval_sprint.py            # 短周期速成模板评测器（含时限压力测试）
├── probe_same_topic.py       # 同题撞车探针（检测「换数字抄袭」）
├── eval_ablation.py          # Few-shot 消融实验
├── eval_runs/                # 评测原始输出存档（生成的产物）
├── test_prompt_builder.py    # 冒烟测试（可 pytest 也可直接运行）
├── templates/                # 角色模板本体（每角色一个 YAML）
│   ├── system_base.yaml      # 通用骨架 + 设计四原则（旧 system 结构示例）
│   ├── teacher.yaml          # 教师 Agent · 讲授者（单次讲解，带水平分支）
│   ├── teacher_planner.yaml  # 教师 Agent · 学习方案规划者（长周期大纲 + 门控）
│   ├── teacher_unit.yaml     # 教师 Agent · 单元渲染者（按需生成单个单元）
│   ├── teacher_sprint.yaml   # 教师 Agent · 短周期速成（一次性交付）
│   ├── assistant.yaml        # 助教 Agent · 批改者
│   ├── supervisor.yaml       # 督学 Agent · 督促者
│   └── qa.yaml               # 答疑 Agent · 解答者（扩展）
├── fewshots/                 # Few-shot 示例（与模板分离，按需拼接）
│   ├── teacher.yaml
│   ├── teacher_planner.yaml
│   ├── teacher_sprint.yaml
│   ├── assistant.yaml
│   └── supervisor.yaml
└── cot/
    └── snippets.yaml         # 可复用 CoT 指令片段
