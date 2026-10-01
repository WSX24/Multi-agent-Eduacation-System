#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""材料题示范作答（teacher_material）真实模型评测

这个模板与 teacher 的区别：teacher 的输出契约是**讲解体**（5 个教学模块），
本模板是**作答体**（审题 / 作答 / 回扣设问，每点是「结论 + 依据」）。
所以校验重点也不同：

  M1 结构完整：3 个模块齐全、顺序未变、非空
  M2 审题到位：写明设问的动词与限定范围（它决定下文要不要补背景知识）
  M3 结论有依据：每点都带「依据」，且依据**必须能指回材料原文**（用「」引用）
  M4 回扣设问：末句是对设问问句的回答（≥10 字，不是把审题复制一遍）
  M5 交答案不是交教案：不得出现教学模块名与讲解口吻（这正是缺口 #1 的形态）
  M6 无禁止项：无 LaTeX、无「生动形象」类套话、无 Few-shot 材料污染

用法::

    python eval_material.py --self-check   # 离线：范文 + 注入型坏样本（不调 API）
    python eval_material.py -n 1           # 真跑：文理两例各 1 次
    python eval_material.py -n 2 --retry 1 # 带闸门回投重试
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import (MATERIAL_CONTAMINATION, MATERIAL_SECTIONS, split_by_names,
                          split_points, validate_material)
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 范文【示例输入】里的字段标签（解析用；见 parse_example 的注释）
_LABEL_RE = re.compile(r"^(学科|材料|设问)：", re.M)

# ---------------------------------------------------------------- 用例
# 文理各一例：同一个 `source_analysis` 片段、两种形态的材料（文本 / 实验数据）。
# 理科那例是刻意留的——片段第 4 步写的是「结合所学 → 补时代背景」，
# 而它的设问是「根据材料」，正好验「不硬加背景」这一支。
CASES: dict[str, dict] = {
    "语文_爱莲说": {
        "学科": "初中语文",
        "学生水平": "基础",
        "材料": "【甲】予独爱莲之出淤泥而不染，濯清涟而不妖。（周敦颐《爱莲说》）\n"
                "【乙】予谓菊，花之隐逸者也；牡丹，花之富贵者也；莲，花之君子者也。（同上）",
        "设问": "两则材料都写莲，作者借莲寄托了什么？请结合材料分点作答。",
    },
    "物理_浮力": {
        "学科": "初中物理",
        "学生水平": "基础",
        "材料": "某同学用同一物体做实验：空气中弹簧测力计示数 6.0 N；浸没在水中示数 4.0 N；"
                "浸没在盐水中示数 3.6 N。",
        "设问": "根据材料，比较该物体在水中与盐水中受到的浮力大小，并说明理由。",
    },
}


def material_vars(case: dict) -> dict:
    return {k: case[k] for k in ("学科", "学生水平", "材料", "设问")}


def parse_example(text: str) -> dict:
    """从 Few-shot 的【示例输入】里解析出 ``学科 / 材料 / 设问``。

    刻意用解析而不是另抄一份：范文改了，自检跟着改；抄一份迟早漂移。

    ⚠ 只能按**已知标签**切段，不能写成「下一行带冒号的就是下一个字段」：
    材料正文里本就会出现以「依次为：」开头的行（踩过），那样会把材料截断。
    """
    marks = list(_LABEL_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[m.end():stop].strip()
    return out


def fewshot_cases() -> list[tuple[str, dict]]:
    import yaml

    data = yaml.safe_load((ROOT / "fewshots" / "teacher_material.yaml").read_text(encoding="utf-8"))
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
        assert case["材料"] and case["设问"], f"{name} 的示例输入解析不出材料/设问：{case}"
        checks = validate_material(case["_output"], case, check_contamination=False)
        failed = [n for n, ok, _ in checks if not ok]
        assert not failed, f"{name} 未通过校验：{failed}"
        total = len(checks)

    # ② 注入型坏样本必须被检出（否则校验器等于没装）
    good = cases[0][1]
    base = good["_output"]
    _, bodies0 = split_by_names(base, MATERIAL_SECTIONS)
    ans = bodies0["作答"]
    q = re.search(r"「([^」]+)」", ans)
    assert q, "范文的作答点里竟找不到「」引用，注入用例无从构造"
    # 破坏引文：给**每一个片段**都插一个字。
    # 只改一处是不够的——校验器允许把合并引文按标点拆开逐段比对（避免误报），
    # 因此只要能拆出一个仍然对得上的片段，整体就算合规。
    qq = "".join(("某" + piece) if piece.strip() and not re.fullmatch(r"[，。；、：,;]", piece)
                   else piece for piece in re.split(r"([，。；、：,;])", q.group(1)))
    broken = base.replace(ans, ans[:q.start(1)] + qq + ans[q.end(1):], 1)

    injections = [
        ("每点都有「依据」", base.replace("依据：", "说明：")),
        ("依据引用了材料原文", broken),
        ("未输出教学模块", "1. 【本课目标】学会答材料题\n2. 【知识讲解】\n" + base),
        ("审题点明动词与限定范围",
         base.replace("设问动词是「分析」，限定范围是「结合材料与所学」", "先看清题目问什么")),
        ("无讲解口吻", base + "\n下面我们来总结一下这道题。"),
    ]
    for expect, sample in injections:
        # 拿范文自己当样本时不能开污染检测（它本来就带自己那份材料的词）
        failed = [n for n, ok, _ in validate_material(sample, good,
                                                      check_contamination=False) if not ok]
        assert any(expect in f for f in failed), f"未检出：{expect}（实得 {failed}）"

    # 污染检测单独验一次：把范文的输出拿去对**另一个用例**，材料锚点必然对不上
    leak_failed = [n for n, ok, _ in validate_material(base, CASES["语文_爱莲说"]) if not ok]
    assert any("材料污染" in f for f in leak_failed), \
        f"未检出：无 Few-shot 材料污染（实得 {leak_failed}）"
    injections.append(("无 Few-shot 材料污染", base))

    # ③ 污染词表双向审计：在范文里（命得中）、又不在用例输入里（不误报）
    blob = "".join(str(ex["input"]) + str(ex["output"]) for ex in lib.fewshots["teacher_material"]["examples"])
    for tok in MATERIAL_CONTAMINATION:
        assert tok in blob, f"污染词「{tok}」不在范文里，永远命不中"
    for name, case in CASES.items():
        text = "".join(str(v) for v in case.values())
        hit = [t for t in MATERIAL_CONTAMINATION if t in text]
        assert not hit, f"用例 {name} 输入自带污染词 {hit}，必然误报"

    # ④ prompt 组装：材料与设问分别进 prompt、无残留占位符、片段真挂上
    for name, case in CASES.items():
        v = material_vars(case)
        p = lib.build("teacher_material", v, cot="source_analysis")
        assert "{{" not in p, f"{name} 有未填充占位符"
        assert f"材料：{case['材料']}" in p, f"{name} 未把材料作为独立变量传入"
        assert f"设问：{case['设问']}" in p, f"{name} 未把设问作为独立变量传入"
        assert "第 5 步【分点作答】" in p, f"{name} 未接上 source_analysis 片段"

    print("离线自检通过：")
    print(f"  · {len(cases)} 份范文通过全部 {total} 项检查")
    print(f"  · {len(injections)} 类注入缺陷全部被检出（含「写成教案」「依据指不回材料」）")
    print(f"  · 污染词表 {len(MATERIAL_CONTAMINATION)} 个词双向审计通过")
    print(f"  · {len(CASES)} 个用例的材料/设问各自独立进入 prompt，CoT 片段已挂上")


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

    print(f"CoT 片段：source_analysis（文理共用）")
    print(f"模型: {args.model}  每例 {args.n} 次\n")

    per: dict[str, list[list[tuple[str, bool, str]]]] = {}
    first_fail: dict[str, list[str]] = {}      # 用例 → 首轮失败项（闸门拦下了什么）
    first_fail_runs = 0                        # 首轮就被拦下的次数（相对总跑数）
    n_runs = 0
    for name, case in CASES.items():
        v = material_vars(case)
        prompt = lib.build("teacher_material", v, fewshot=True, cot="source_analysis")
        print(f"  用例 {name}：{case['设问']}")
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
                checks = validate_material(raw, case)
                failed = [x for x in checks if not x[1]]
                if attempt == 0:
                    # 首轮失败项单独记：只看最终结果会把「闸门拦下了什么」这件事吞掉
                    first_fail.setdefault(name, []).extend(x[0] for x in failed)
                    if failed:
                        first_fail_runs += 1
                # 每次尝试都落盘：首轮不带后缀，重试带 _r1/_r2。
                # 为什么必须存首轮：只看最终结果会把「闸门拦下了什么」吞掉。
                # 首轮那份按设计就是不合格的，L2 的 superseded() 会把它排除在基线之外。
                suffix = f"_r{attempt}" if attempt else ""
                (RUNS_DIR / f"material_{name}_{stamp}_{i}{suffix}.md").write_text(
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
            _, bodies = split_by_names(raw, MATERIAL_SECTIONS)
            pts = len(split_points(bodies.get("作答", "")))
            failed = [x for x in checks if not x[1]]
            print(f"    {len(checks) - len(failed)}/{len(checks)}  {pts} 个作答点"
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
        print(f"  {name:14s} 全项通过 {ok}/{len(runs)}")

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
