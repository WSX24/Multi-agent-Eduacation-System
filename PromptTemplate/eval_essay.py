#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作文/主观题批改（assistant_essay）真实模型评测

这个模板与 assistant 的区别：assistant 是**客观题**评分模型（对/错 + 分子分母），
本模板是**主观题**评分模型（分维度 + 等级 + 逐条原文依据）。所以校验重点也不同：

  H1  结构完整：7 个模块齐全且顺序未变
  H2  分数可核对：总分分母=输入总分；维度与输入完全一致；各维分母=输入满分；
      且**各维得分之和 == 总分得分**（主观题最容易在这里对不上账）
  H3  等级自洽：等级必须与总分占比一致（≥85%一类 / 70%二类 / 55%三类 / 否则四类）
  H4  评价有落点：亮点与问题都必须引用学生原文（「」）；给出示范改写；
      不得代写全文（改进建议 ≤400 字）
  H5  无禁止项：无 LaTeX；无 Few-shot 范文污染（照抄范文里的事例）
  H6  判分有分辨力：同一题目、两份水平明显不同的作文，得分必须拉开 ≥8 分（满分 40）

用法::

    python eval_essay.py --self-check      # 离线：校验范文与解析器（不调 API）
    python eval_essay.py -n 1              # 真跑：两份作文各 1 次
    python eval_essay.py -n 2 --retry 1    # 带闸门回投重试
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import ESSAY_SECTIONS, parse_dimension_scores, parse_score, validate_essay
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# ---------------------------------------------------------------- 用例
DIMENSIONS = {"内容与立意": 15, "结构与条理": 10, "语言与表达": 10, "细节与描写": 5}
TOTAL = 40
ESSAY_REQUIREMENT = "无（本次为节选，不评字数）"
TITLE = "以「这也是课堂」为题，写一篇记叙文"

WEAK = """那天放学，我看见一位老奶奶在路边摔倒了。我赶紧跑过去把她扶起来。
老奶奶说谢谢我。我说不用谢。然后我就回家了。回到家我把这件事告诉了妈妈，妈妈夸我做得对。
我觉得帮助别人很快乐。第二天我到学校，把这件事告诉了同桌。同桌说我很棒。我听了很高兴。
我想以后还要多做好事。"""

GOOD = """那天放学时下起了雨，我撑着伞往家走。路口的花坛边蹲着一位老奶奶，她正把倒下的月季
一枝枝扶起来，雨水顺着她的袖口往下淌。我犹豫了一下，还是走过去，把伞往她那边偏了偏。
她抬头笑了笑，说这些花是孙女种的，倒了可惜。我们一起把花扶好，用几根竹签撑住。
她的手上全是泥，却一直说“麻烦你了”。雨小了些，她拍拍我的手让我快回家，别让家里人担心。
回家的路上我一直在想，她为什么要把仅有的伞下空间让给我。那天我原本以为自己做了一件好事，
后来才发现，是她在教我怎么把好事做到底。这场雨里的一小课，比课堂上讲的更清楚。"""

CASES = {
    "流水账": WEAK,
    "有细节": GOOD,
}

# 离线自检时拿范文示例输出当「已知合规样本」，故维度用范文那套
FEWSHOT_CASE = {"总分": 40,
                "维度": {"立意与思想": 10, "结构与条理": 10,
                         "语言与表达": 10, "素材与论证": 10}}


def essay_vars(作文: str) -> dict:
    return {
        "学科": "初中语文",
        "学生姓名": "小林",
        "学生水平": "中等",
        "题目": TITLE,
        "学生作文": 作文,
        "字数要求": ESSAY_REQUIREMENT,
        "评分维度": "、".join(f"{k} {v}" for k, v in DIMENSIONS.items()),
        "总分": TOTAL,
    }


def self_check(lib: PromptLib) -> None:
    """离线自检：范文自身是否合规 + 解析器能否抓出典型错误 + prompt 组装是否正确。"""
    import yaml

    # ① 范文示例输出应当通过全部检查（拿它当「已知合规样本」）
    fs = yaml.safe_load((ROOT / "fewshots" / "assistant_essay.yaml").read_text(encoding="utf-8"))
    sample = fs["examples"][0]["output"]
    checks = validate_essay(sample, FEWSHOT_CASE, check_contamination=False)
    failed = [n for n, ok, _ in checks if not ok]
    assert not failed, f"范文示例自身未通过校验：{failed}"

    # ② 故意构造的残缺样本必须被检出（否则校验器等于没装）
    bad = ("1. 【总分】30/40\n2. 【维度得分】立意与思想 6/10 —— x\n结构与条理 6/10 —— x\n"
           "语言与表达 7/10 —— x\n素材与论证 5/10 —— x\n3. 【等级】一类文\n"
           "4. 【亮点】写得不错\n5. 【逐条问题】逻辑混乱\n6. 【改进建议】多读书\n7. 【置信度】高\n")
    bad_failed = [n for n, ok, _ in validate_essay(bad, FEWSHOT_CASE) if not ok]
    must = ["维度得分之和=总分", "等级与总分占比自洽", "亮点有原文引用",
            "逐条问题有原文引用", "给出示范改写"]
    miss = [m for m in must if m not in bad_failed]
    assert not miss, f"校验器漏检：{miss}（实际检出 {bad_failed}）"

    # ③ prompt 组装：每个用例都要把作文原文与维度表传进去，且无残留占位符
    for name, essay in CASES.items():
        v = essay_vars(essay)
        p = lib.build("assistant_essay", v, fewshot=True)
        assert "{{" not in p, f"{name} 有未填充占位符"
        assert essay.strip().splitlines()[0] in p, f"{name} 未把作文原文传进 prompt"
        assert "内容与立意 15" in p, f"{name} 未把评分维度传进 prompt"

    print("离线自检通过：")
    print(f"  · 范文示例通过全部 {len(checks)} 项检查")
    print(f"  · 残缺样本被抓出 {len(bad_failed)} 项问题（含 {len(must)} 项关键项）")
    print(f"  · {len(CASES)} 个用例的 prompt 均正确携带作文原文与评分维度")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="每个用例跑几次")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--retry", type=int, default=0, help="校验失败时回投重试次数")
    ap.add_argument("--self-check", action="store_true", help="离线自检，不调 API")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    if args.self_check:
        self_check(lib)
        return

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    case_spec = {"总分": TOTAL, "维度": DIMENSIONS}

    print(f"题目：{TITLE}")
    print(f"评分维度：{'、'.join(f'{k} {v}' for k, v in DIMENSIONS.items())}  总分：{TOTAL}")
    print(f"模型: {args.model}  每例 {args.n} 次\n")

    per: dict[str, list[list[tuple[str, bool, str]]]] = {}
    scores: dict[str, list[float]] = {}
    for name, essay in CASES.items():
        v = essay_vars(essay)
        prompt = lib.build("assistant_essay", v, fewshot=True)
        per[name], scores[name] = [], []
        for i in range(1, args.n + 1):
            raw, messages = "", [{"role": "user", "content": prompt}]
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=0.7)
                    raw = resp.choices[0].message.content or ""
                except Exception as e:                    # noqa: BLE001
                    print(f"  {name}: API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate_essay(raw, case_spec)
                failed = [x for x in checks if not x[1]]
                if not failed or attempt >= args.retry:
                    break
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "你的输出存在以下问题，请修正后重新输出完整结果：\n"
                     + "\n".join(f"- {x[0]}：{x[2]}" for x in failed)},
                ]
            if not raw:
                continue
            (RUNS_DIR / f"essay_{name}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per[name].append(checks)
            _, bodies = __import__("eval_generic").split_by_names(raw, ESSAY_SECTIONS)
            sc = parse_score(bodies.get("总分", ""))
            dims = parse_dimension_scores(bodies.get("维度得分", ""))
            if sc:
                scores[name].append(sc[0])
            failed = [x for x in checks if not x[1]]
            print(f"  {name:6s} {len(checks) - len(failed)}/{len(checks)}  总分 {sc[0] if sc else '?'}"
                  f"/{sc[1] if sc else '?'}  维度 {[d[1] for d in dims]}"
                  + (f"（重试 {attempt} 次）" if attempt else ""))
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    # ---- 汇总 ----
    print("\n" + "=" * 64)
    for name, runs in per.items():
        if not runs:
            continue
        ok = sum(1 for r in runs if all(x[1] for x in r))
        print(f"  {name:6s} 全项通过 {ok}/{len(runs)}"
              + (f"｜得分 {scores[name]}" if scores.get(name) else ""))

    weak = scores.get("流水账") or []
    good = scores.get("有细节") or []
    if weak and good:
        gap = min(good) - max(weak)
        print(f"\n  H6 判分分辨力：有细节 {min(good)} 分 − 流水账 {max(weak)} 分 = {gap:+.1f}"
              f"（要求 ≥8）→ {'✓ 通过' if gap >= 8 else '✗ 未通过'}")
    print(f"\n  原始输出已存档：{RUNS_DIR}")


if __name__ == "__main__":
    main()
