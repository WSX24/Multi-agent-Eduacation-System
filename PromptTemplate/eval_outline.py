#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作文构思（teacher_outline）真实模型评测

这个模板与 teacher 的区别：teacher 的输出契约是**讲解体**（5 个教学模块），
本模板是**构思体**（审题 / 立意 / 提纲 / 素材 / 首尾）。校验重点也不同：

  O1 结构完整：5 个模块齐全、顺序未变、非空
  O2 审题到位：给出题眼/限定条件，并有一个「反例：如果只写……就跑题了」
  O3 立意站得住：有「中心句：」与「支撑：」，且不是骑墙话
  O4 提纲可执行：3-5 段、每段一行「第N段（作用）：…」、标出重点段，且没写成段落实文
  O5 素材有落点：至少 2 条「素材N：…… —— 它证明：……」，无「古今中外有很多例子」
  O6 首尾给到句：分别有「开头：」「结尾：」
  O7 交方案不是交全文、也不是交教案：全文 ≤1000 字，无教学模块名与讲解口吻
  O8 无禁止项：无 LaTeX、无 Few-shot 题目污染

用法::

    python eval_outline.py --self-check   # 离线：范文 + 8 类注入（不调 API）
    python eval_outline.py -n 1           # 真跑：记叙文 / 议论文各 1 次
    python eval_outline.py -n 2 --retry 1 # 带闸门回投重试
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import (OUTLINE_CONTAMINATION, OUTLINE_SECTIONS, split_by_names,
                          validate_outline)
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 范文【示例输入】里的字段标签（解析用；只认已知标签，别用「下一行带冒号」切段）
# 注意标签是「题目与要求」而不是「题目」——与模板里的变量名不同，得映一下。
_LABEL_RE = re.compile(r"^(学科|题目与要求)：", re.M)

# ---------------------------------------------------------------- 用例
# 两个文体各一例：提纲形态不同（起承转合 / 总分总），只测一种会让模型把提纲一律写成
# 起承转合。议论文那例还顺便验「立意不骑墙」这条——议论文最容易写成两不得罪。
CASES: dict[str, dict] = {
    "记叙文_这也是课堂": {
        "学科": "初中语文",
        "学生水平": "中等",
        "题目": "以「这也是课堂」为题，写一篇记叙文",
    },
    "议论文_小议捷径": {
        "学科": "初中语文",
        "学生水平": "中等",
        "题目": "以「小议『捷径』」为题，写一篇议论文",
    },
    # 第三个用例换**命题方式**：给材料 + 自拟题目。没有现成题眼可抓，
    # 审题难度与“抓题眼”不是一回事——专门验它会不会自拟一个骑墙或空泛的中心句。
    "材料作文_自拟题目": {
        "学科": "初中语文",
        "学生水平": "中等",
        "题目": "阅读下面材料，自选角度，自拟题目，写一篇议论文。材料：一位木匠说，"
                "他做桌子时，榫卯总要留一道很细的缝，因为木头会随季节涨缩——留了缝，桌子才用得久。",
    },
}


def outline_vars(case: dict) -> dict:
    return {k: case[k] for k in ("学科", "学生水平", "题目")}


def parse_example(text: str) -> dict:
    """从 Few-shot 的【示例输入】里解析出 ``学科 / 题目``。

    刻意用解析而不是另抄一份：范文改了，自检跟着改；抄一份迟早漂移。
    """
    marks = list(_LABEL_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = "题目" if m.group(1) == "题目与要求" else m.group(1)
        out[key] = text[m.end():stop].strip()
    return out


def fewshot_cases() -> list[tuple[str, dict]]:
    import yaml

    data = yaml.safe_load((ROOT / "fewshots" / "teacher_outline.yaml").read_text(encoding="utf-8"))
    out = []
    for i, ex in enumerate(data["examples"], 1):
        case = parse_example(ex["input"])
        case["_output"] = ex["output"]
        out.append((f"范文{i}", case))
    return out


def self_check(lib: PromptLib) -> None:
    """离线自检：范文自身合规 + 坏样本必被检出 + 污染词表双向审计 + prompt 组装。"""
    cases = fewshot_cases()

    # ① 范文示例输出应当通过全部检查（拿它当「已知合规样本」）
    total = 0
    for name, case in cases:
        assert case["题目"], f"{name} 的示例输入解析不出题目：{case}"
        checks = validate_outline(case["_output"], case, check_contamination=False)
        failed = [n for n, ok, _ in checks if not ok]
        assert not failed, f"{name} 未通过校验：{failed}"
        total = len(checks)

    # ② 注入型坏样本必须被检出（否则校验器等于没装）
    good = cases[0][1]
    base = good["_output"]
    p2 = "第2段（承）：我想上前又不好意思，假装低头看手机，余光盯着她。"
    central_line = "中心句：长大不是学会做更多的事，而是第一次发现母亲也会累、也会需要人扶。"
    key_para = "第3段（转）：她弯腰时晃了一下，我一把接过米袋，她愣了一下，说我提不动。← 重点"
    mat1 = ("素材1：母亲把米袋从一只手换到另一只手、弯腰时晃了一下 —— "
            "它证明：她确实累了，只是从不说。（用在第1段）")
    for frag in (p2, central_line, key_para, mat1):
        assert frag in base, f"范文结构已变，注入用例需同步：{frag[:20]}"

    injections = [
        ("审题给出反例", base.replace("反例：", "")),
        ("立意不骑墙", base.replace(central_line,
                                  "中心句：快有快的好，慢有慢的好，各有各的道理。")),
        ("提纲为 3-5 段", base.replace(p2, p2 + "\n   第5段（补）：补一段。\n   第6段（补）：再补一段。")),
        # 提纲写成段落实文——两个判据各注入一次（句数 / 字数）
        ("提纲每段是一句话说明", base.replace(p2, "第2段（承）：我想上前又不好意思，假装低头看手机，"
                                          "余光盯着她。我看见她手背上有几道口子，忽然想起小时候"
                                          "她也是这样替我系鞋带、替我背书包。我站在原地没有动，"
                                          "心里有点说不出的难受，只好把眼睛移开。")),
        ("提纲每段是一句话说明", base.replace(p2, "第2段（承）：" + "我想上前又不好意思，" * 20 + "。")),
        ("素材无空话", base.replace(mat1, mat1 + "\n   素材3：古今中外有很多例子 —— "
                                                   "它证明：坚持就能成功。（用在第4段）")),
        ("未写成全文(≤1000字)", base + "\n" + "那天放学以后，天还没有完全黑下来。" * 60),
        ("未输出教学模块", "1. 【本课目标】学会写记叙文\n2. 【知识讲解】\n" + base),
        ("无讲解口吻", base + "\n通俗例子：就像很多同学都会遇到的那样，妈妈总是很辛苦。"),
        # 素材与提纲的咬合：丢标注 / 段号不存在 / 重点段没被覆盖
        ("每条素材标注用在哪一段", base.replace(mat1, mat1.replace("（用在第1段）", ""))),
        ("素材指向的段号存在", base.replace("（用在第3段）", "（用在第7段）")),
        ("重点段有素材支撑", base.replace("（用在第3段）", "（用在第2段）")),
        # 「它证明」不能是敷衍的几个字，也不能把中心句抄一遍
        ("每条素材说明它证明什么", base.replace(mat1, mat1.replace(
            "它证明：她确实累了，只是从不说。", "它证明：很好。"))),
        ("素材证明不是照抄中心句", base.replace(mat1, mat1.replace(
            "它证明：她确实累了，只是从不说。",
            "它证明：长大不是学会做更多的事，而是第一次发现母亲也会累、也会需要人扶。"))),
    ]
    for expect, sample in injections:
        # 拿范文自己当样本时不能开污染检测（它本来就带自己那份题目的词）
        failed = [n for n, ok, _ in validate_outline(sample, good,
                                                     check_contamination=False) if not ok]
        assert any(expect in f for f in failed), f"未检出：{expect}（实得 {failed}）"

    # 污染检测单独验一次：把范文的输出拿去对**另一个题目**，素材锚点必然对不上
    leak_failed = [n for n, ok, _ in validate_outline(base, CASES["议论文_小议捷径"]) if not ok]
    assert any("题目污染" in f for f in leak_failed), \
        f"未检出：无 Few-shot 题目污染（实得 {leak_failed}）"

    # ③ 污染词表双向审计：在范文里（命得中）、又不在用例输入里（不误报）
    fe = lib.fewshots["teacher_outline"]["examples"]
    blob = "".join(str(e["input"]) + str(e["output"]) for e in fe)
    for tok in OUTLINE_CONTAMINATION:
        assert tok in blob, f"污染词「{tok}」不在范文里，永远命不中"
    for name, case in CASES.items():
        text = "".join(str(v) for v in case.values())
        hit = [t for t in OUTLINE_CONTAMINATION if t in text]
        assert not hit, f"用例 {name} 输入自带污染词 {hit}，必然误报"

    # ④ prompt 组装：题目作为独立变量进 prompt、无残留占位符、片段真挂上
    for name, case in CASES.items():
        v = outline_vars(case)
        p = lib.build("teacher_outline", v, cot="essay_outline")
        assert "{{" not in p, f"{name} 有未填充占位符"
        assert f"题目与要求：{case['题目']}" in p, f"{name} 未把题目作为独立变量传入"
        assert "第 5 步【写首尾】" in p, f"{name} 未接上 essay_outline 片段"

    print("离线自检通过：")
    print(f"  · {len(cases)} 份范文通过全部 {total} 项检查")
    print(f"  · {len(injections)} 类注入缺陷全部被检出"
          "（含「写成教案」「立意骑墙」「提纲写成段落实文」「素材与提纲脱节」「证明只是照抄中心句」）")
    print(f"  · 污染词表 {len(OUTLINE_CONTAMINATION)} 个词双向审计通过")
    print(f"  · {len(CASES)} 个用例的题目独立进入 prompt，CoT 片段已挂上")


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

    print("CoT 片段：essay_outline（审题→立意→提纲→素材→首尾）")
    print(f"模型: {args.model}  每例 {args.n} 次\n")

    per: dict[str, list[list[tuple[str, bool, str]]]] = {}
    first_fail: dict[str, list[str]] = {}      # 用例 → 首轮失败项（闸门拦下了什么）
    first_fail_runs = 0                        # 首轮就被拦下的次数（相对总跑数）
    n_runs = 0
    for name, case in CASES.items():
        v = outline_vars(case)
        prompt = lib.build("teacher_outline", v, fewshot=True, cot="essay_outline")
        print(f"  用例 {name}：{case['题目']}")
        per[name] = []
        for i in range(1, args.n + 1):
            n_runs += 1
            raw, messages = "", [{"role": "user", "content": prompt}]
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=0.7)
                    raw = resp.choices[0].message.content or ""
                except Exception as e:                    # noqa: BLE001
                    print(f"    API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate_outline(raw, case)
                failed = [x for x in checks if not x[1]]
                if attempt == 0:
                    # 首轮失败项要单独记：只看最终结果会把「闸门拦下了什么」这件事吞掉，
                    # 而它正是评估模板稳定性的关键数字（首轮就能过，才算模板写得清楚）。
                    first_fail.setdefault(name, []).extend(x[0] for x in failed)
                    if failed:
                        first_fail_runs += 1
                # 每次尝试都落盘：首轮不带后缀，重试带 _r1/_r2。
                # 为什么必须存首轮：只看最终结果会把「闸门拦下了什么」吞掉。
                # 首轮那份按设计就是不合格的，L2 的 superseded() 会把它排除在基线之外。
                suffix = f"_r{attempt}" if attempt else ""
                (RUNS_DIR / f"outline_{name}_{stamp}_{i}{suffix}.md").write_text(
                    raw, encoding="utf-8")
                if not failed or attempt >= args.retry:
                    break
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "你的输出存在以下问题，请修正后重新输出完整结果：\n"
                     + "\n".join(f"- {x[0]}：{x[2]}" for x in failed)},
                ]
            if not raw:
                continue
            per[name].append(checks)
            _, bodies = split_by_names(raw, OUTLINE_SECTIONS)
            failed = [x for x in checks if not x[1]]
            print(f"    {len(checks) - len(failed)}/{len(checks)}  {len(raw)} 字"
                  + (f"（重试 {attempt} 次）" if attempt else ""))
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    # ---- 汇总 ----
    print("\n" + "=" * 64)
    allok = True
    for name, runs in per.items():
        if not runs:
            continue
        ok = sum(1 for r in runs if all(x[1] for x in r))
        allok = allok and ok == len(runs)
        print(f"  {name:16s} 全项通过 {ok}/{len(runs)}")

    # 首轮通过率：不要只看最终通过——闸门回投后过会让「模板写得清不清楚」看起来比实际好
    first_clean = n_runs - first_fail_runs
    print(f"\n  首轮即通过 {first_clean}/{n_runs} 次（其余由闸门回投后修正）")
    if any(first_fail.values()):
        from collections import Counter
        cnt: Counter = Counter()
        for items in first_fail.values():
            cnt.update(items)
        print("  首轮失败项分布：")
        for k, v in cnt.most_common():
            print(f"    {v} 次  {k}")
    print(f"\n  原始输出已存档：{RUNS_DIR}")
    if not allok:
        sys.exit(1)


if __name__ == "__main__":
    main()
