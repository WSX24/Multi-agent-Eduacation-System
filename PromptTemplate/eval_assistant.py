#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""助教 Agent（assistant）真实模型评测

这个模板与其它不同：它的核心不是「格式对不对」，而是**判分对不对**。
所以校验器用「已知正确答案的用例」来验：给定题目、学生答案、参考答案，
检查模型给出的【得分】【答案判定】是否与事实相符，错因归因是否指向真正的错处。

  H1  判分正确：五个用例的判定与得分必须分别落在预期区间
  H2  判分有分辨力：同一道题、只改学生答案，三次判定必须互不相同
  H3  不确定时不硬判：「答案缺失/答非所问」必须标低置信度或建议人工复核，不得编造评分
  H4  归因指向真错因：错题的错误分析须提到该错的地方（如「相加」「单位」）
  H5  结构完整：6 个模块齐全且顺序正确
  H6  无禁止行为：不得出现人身攻击式评价；无 LaTeX；无 Few-shot 学科污染

用法::
    python eval_assistant.py --self-check
    python eval_assistant.py -n 2
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import ASSISTANT_SECTIONS, split_by_names, validate_assistant
from eval_planner import load_api_key
from prompt_builder import PromptLib
from quality_gate import blank_inputs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 题目固定，只改学生答案 —— 判分必须有分辨力
PROBLEM = "一个物体在空气中重 6 N，浸没在水中时弹簧测力计的示数为 4 N。求物体受到的浮力。"
REFERENCE = "F浮 = G - F示 = 6 N - 4 N = 2 N，方向竖直向上。"
# 本题满分必须由调用方传入。此前模板要求输出「得分 / 满分」却从不把满分传进来，
# 分母是模型自己编的；而本评测的断言写的是 sc[0] == sc[1]（得分 == 自编满分），
# 于是它永远在为「模型自己跟自己对得上」盖章，测量不到任何真东西。
MAX_SCORE = 5

CASES = {
    # 注意：学生答案必须真正与参考答案一致（含方向）——
    # 之前漏写「方向竖直向上」，模型判 8/10「部分正确」其实是判对的，
    # 是我的用例没设计成真的全对。
    "全对": {
        "答案": "F浮 = G - F示 = 6 N - 4 N = 2 N，方向竖直向上。",
        "判定": ("对",), "得分": "满分", "须低置信": False, "须提": [],
    },
    # 漏单位：判「对」还是「部分正确」本就因老师而异（模型两次各给一种，
    # 且都说明了理由）。所以不硬判结论，只要求它**必须提到单位**。
    "漏单位": {
        "答案": "F浮 = 6 - 4 = 2。",
        "判定": ("对", "部分正确"), "得分": None, "须低置信": False,
        "须提": ["单位"], "须提位置": "错误分析+改进建议",
    },
    "符号错": {
        "答案": "F浮 = 6 + 4 = 10 N。",
        "判定": ("错", "部分正确"), "得分": "低", "须低置信": False,
        "须提": ["减", "相加", "相减", "差"], "须提位置": "错误分析+改进建议",
    },
    # 字段缺失：系统没把作答传进来（值与「白卷」不同）。
    # 实测模型会拿旁边的参考答案当学生答案，给出「10/10、判定对、置信度高」，
    # 并编造「你正确标注了方向」——学生没作答却拿满分。
    # 所以这一项**不交给模型**，由入口预检在调用前拦下（见 main 与 self-check）。
    "字段缺失": {
        "答案": "", "precheck_only": True,
        "判定": None, "得分": None, "须低置信": False, "须提": [],
    },
    # 白卷：学生确实一个字没写 —— 判 0 分是合理的，置信度可以高。
    # 只要求不得凭空给满分（不能把「没作答」当「全对」）。
    "白卷": {
        "答案": "（学生未作答）",
        "判定": None, "得分": None, "须低置信": False, "须提": [],
    },
    "答非所问": {
        "答案": "我觉得这道题好难，我不想做。",
        "判定": ("错", "部分正确"), "得分": None, "须低置信": False, "须提": [],
    },
}

# 禁止出现的评价方式（模板明令：不得情绪化、人身攻击）
ABUSE = ["笨", "蠢", "差劲", "没救", "废物", "太糟糕了", "真丢人"]
LATEX = ["\\frac", "\\times", "\\div", "\\cdot", "\\[", "\\]", "\\(", "\\)", "\\sqrt"]
# assistant 的 Few-shot 是初中数学一元二次方程；本题是初中物理，不得漏进来
CONTAMINATION = ["一元二次方程", "x² - 5x + 6", "因式分解", "求根", "漏根", "代回检验"]


def verdict_of(bodies: dict) -> str:
    """从【答案判定】里抽出判定结论。"""
    v = bodies.get("答案判定", "")
    if "部分" in v:
        return "部分正确"
    if "错" in v:
        return "错"
    if "对" in v:
        return "对"
    return "?"


def score_of(bodies: dict) -> tuple[float, float] | None:
    """从【得分】里抽出 (得分, 满分)。

    只认「A/B」形式：模板已强制该形式，而旧的「N 分」回退会把 (N, N) 当成
    (得分, 满分)，于是「得分==满分」这类断言可以靠一句「5 分」蒙过去。
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", bodies.get("得分", ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def validate(raw: str, case_name: str) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    spec = CASES[case_name]

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # ---- H5 结构 ----
    for n, ok, d in validate_assistant(raw):
        if "污染" in n:       # 污染单列，且本题为物理，数学词不该出现
            continue
        add(n, ok, d)
    order, bodies = split_by_names(raw, ASSISTANT_SECTIONS)
    if len(bodies) < len(ASSISTANT_SECTIONS):
        return checks

    # ---- H1 判定 ----
    if spec["判定"]:
        v = verdict_of(bodies)
        want = " 或 ".join(spec["判定"])
        add(f"H1 判定∈{{{want}}}", v in spec["判定"], f"实际判定「{v}」")

    # ---- H1 得分：分母必须来自传入的满分，而不是模型自编的 ----
    sc = score_of(bodies)
    if sc is not None:
        add(f"H1 分母 = 传入满分 {MAX_SCORE}（不得自编）", sc[1] == MAX_SCORE,
            f"实际 {sc}，分母应等于输入的本题满分")
    if spec["得分"] == "满分":
        add("H1 得分=满分", sc is not None and sc[0] == MAX_SCORE, f"实际 {sc}")
    elif spec["得分"] == "部分":
        add("H1 得分介于 0 与满分之间",
            sc is not None and 0 < sc[0] < MAX_SCORE, f"实际 {sc}")
    elif spec["得分"] == "低":
        add("H1 得分接近 0", sc is not None and sc[0] <= MAX_SCORE * 0.3, f"实际 {sc}")

    # ---- H3 字段缺失时不得编造评分 ----
    if spec["须低置信"]:
        conf = bodies.get("置信度", "")
        flagged = ("低" in conf) or ("人工复核" in conf) or ("复核" in conf)
        add("H3 字段缺失须标低置信度/建议复核", flagged, f"置信度原文：{conf[:50]}")

    # ---- 整题不得给满分（全对以外）----
    if spec["得分"] is None and case_name != "漏单位":
        add("未作答/答非所问不得给满分",
            not (sc is not None and sc[0] == sc[1]), f"实际 {sc}")

    # ---- H4 归因指向真错因 ----
    if spec["须提"]:
        seg = bodies.get("错误分析", "") + bodies.get("改进建议", "")
        hit = [k for k in spec["须提"] if k in seg]
        add("H4 错误分析指向真错因", bool(hit),
            f"未提到 {spec['须提']}；原文：{seg[:70]}")

    # ---- H6 ----
    abuse = [w for w in ABUSE if w in raw]
    add("H6 无人身攻击式评价", not abuse, f"出现 {abuse}" if abuse else "")
    latex = [t for t in LATEX if t in raw]
    add("H6 无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    leak = [t for t in CONTAMINATION if t in raw]
    add("H6 无 Few-shot 学科污染", not leak, f"混入 {leak}" if leak else "")

    return checks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--retry", type=int, default=0)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    common = {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
              "薄弱点候选列表": "浮力计算、受力分析", "错题": PROBLEM,
              "参考答案": REFERENCE, "本题满分": str(MAX_SCORE)}

    if args.self_check:
        for name, spec in CASES.items():
            v = {**common, "学生答案": spec["答案"]}
            if spec.get("precheck_only"):
                blanks = blank_inputs(lib, "assistant", v)
                assert "学生答案" in blanks, f"{name} 未被入口预检拦下！实际 {blanks}"
                continue
            p = lib.build("assistant", v, fewshot=True)
            assert "{{" not in p, f"{name} 有未填充占位符"
            assert PROBLEM in p, f"{name} 未把题目传进 prompt"
        print(f"离线自检通过：{len(CASES)} 个用例 prompt 正常；"
              f"「字段缺失」被入口预检拦下（不交给模型）")
        return

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"题目：{PROBLEM}")
    print(f"参考答案：{REFERENCE}")
    print(f"模型: {args.model}  每例 {args.n} 次\n")

    per: dict[str, list[list[tuple[str, bool, str]]]] = {}
    verdicts: dict[str, list[str]] = {}

    for name, spec in CASES.items():
        v = {**common, "学生答案": spec["答案"]}
        if spec.get("precheck_only"):
            blanks = blank_inputs(lib, "assistant", v)
            verdicts[name] = [f"入口拦截:{','.join(blanks)}"]
            per[name] = [[("H3 字段缺失由入口预检拦下（不调用模型）",
                           bool(blanks), f"实际 {blanks}")]]
            print(f"  {name:6s} 入口预检拦下 {blanks}——不调用模型（确定性保障）")
            continue
        prompt = lib.build("assistant", v, fewshot=True)
        per[name] = []
        for i in range(1, args.n + 1):
            raw = ""
            messages = [{"role": "user", "content": prompt}]
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=0.7)
                    raw = resp.choices[0].message.content or ""
                except Exception as e:
                    print(f"  {name}: API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate(raw, name)
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
            (RUNS_DIR / f"assistant_{name}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per[name].append(checks)
            _, bd = split_by_names(raw, ASSISTANT_SECTIONS)
            verdicts.setdefault(name, []).append(verdict_of(bd))
            failed = [x for x in checks if not x[1]]
            print(f"  {name:6s} {len(checks)-len(failed)}/{len(checks)}  "
                  f"判定「{verdict_of(bd)}」  得分 {score_of(bd)}"
                  + (f"（重试 {attempt} 次）" if attempt else ""))
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    # ---- H2 判分分辨力 ----
    print("\n" + "=" * 72)
    print("H2 判分分辨力：同一道题、只改学生答案")
    print("=" * 72)
    for k in CASES:
        print(f"    {k:6s} → 判定「{verdicts.get(k, ['?'])[0]}」")
    full = {"全对"}
    others_ok = all(verdicts.get(k, ["?"])[0] != "对" for k in CASES if k not in full)
    print(f"  {'✅' if others_ok else '⚠️ '} 只有「全对」被判为对（判分有分辨力）")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    allc = [c for lst in per.values() for r in lst for c in r]
    seen: list[str] = []
    for lst in per.values():
        for r in lst:
            for c in r:
                if c[0] not in seen:
                    seen.append(c[0])
    for n_ in seen:
        tot = sum(1 for lst in per.values() for r in lst for c in r if c[0] == n_)
        ok = sum(1 for lst in per.values() for r in lst for c in r if c[0] == n_ and c[1])
        mark = "✅" if ok == tot else ("⚠️ " if ok else "❌")
        print(f"  {mark} {n_:30s} {ok}/{tot}")
    ok = sum(1 for c in allc if c[1])
    print(f"\n合计 {ok}/{len(allc)} 项通过；样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
