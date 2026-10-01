# Multi-agent-Education-System

> LangChain开发对应的文件是调用模板库python代码示例

本分支（`PromptTemplateV3.0`）是提示词模板库的交付分支，内容分三块：

| 路径 | 是什么 |
|---|---|
| `PromptTemplate/` | **库本体**：10 个角色模板 + 9 份范文 + CoT 片段 + 校验器 + 评测器（技术索引见 `PromptTemplate/README.md`） |
| `LangChain开发/PromotTemplate.py` | **「怎么调用模板库」的端到端示例**：10 个阶段、闸门回投重做、token 记账、请求留痕（只随本分支发布，不合并回 `main`） |
| `文档索引.md` · `提示词模板库使用指南.md` · `提示词模板库实现原理与使用方法.md` | **中文导航与手册**：不知道看哪份时先看《文档索引》；《使用指南》是实操手册；《实现原理》讲内部怎么运作 |

## 三步跑起来

```bash
cd PromptTemplate
pip install -e .                      # 只装 PyYAML + openai
python -m pytest -q                   # 108 通过 / 4 跳过（跳过的是需要本地录像的 L2 测试）
python eval_outline.py --self-check   # 离线自检一个角色，不花钱
```

**不需要 API Key 就能跑**：`pytest`、各 `eval_*.py --self-check`、`demo_course_flow.py`；
示例脚本还能 `python PromotTemplate.py --dry-run`（或 `--cot-table`）导出「将发给模型的原文」。

真跑评测要配 `DEEPSEEK_API_KEY`（或 `OPENAI_API_KEY`）——没配时会明确报错并退码 2，而不是静默失败。

## 本仓库里没有的东西（别当成缺文件）

| 不在仓库里 | 为什么 |
|---|---|
| `eval_runs/`（真实模型的原始输出录像） | 原始样本不外传；L2 回放定位为**本机**回归网。因此新克隆上跑 `replay_eval.py` 会**退码 2**，而不是把「零份录制」当成「全部通过」 |
| `Web-Prototype/`（156MB 网页原型） | 2026-10-01 判定不纳入（历史也已清理）；决策与理由见 `PromptTemplate/V2.0-说明.md` 第七节 |
| `LangChain开发/` 的其余脚本（`Memory.py`、`RAG_Demo.py`、`Chained_Call.py` 等） | 个人练习脚本，与模板库无关 |
| `项目分析文档.md`、`WORKLOG-20260927.md` | 项目背景与历史工作记录，尚未纳入仓库 |

完整的实测数字与历史决策见 `PromptTemplate/EVAL-REPORT.md`（评测基线）与 `PromptTemplate/V2.0-说明.md`（每版改了什么、验到什么程度、边界在哪）。
