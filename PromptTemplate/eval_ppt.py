#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PPT 课件（teacher_ppt）真实模型评测

这个模板与 teacher 的区别：teacher 的输出契约是**讲解体**（5 个教学模块），
本模板是**逐页大纲体**（课件主题 / 整体结构 / 逐页大纲 / 视觉建议）。校验重点也不同：

  S1 模块完整：4 个模块齐全、顺序未变、非空
  S2 主题到位：声明了受众与页数
  S3 结构有逻辑线：导入 / 展开 / 小结（或「→」）
  S4 逐页可执行：页号从 1 连续、每页有配图与旁白、要点是短语不是段落
  S5 视觉建议非空
  S6 交课件大纲不是交教案：无教学模块名与讲解口吻
  S7 无禁止项：无 LaTeX、无 Few-shot 主题污染

用法::

    python eval_ppt.py --self-check   # 离线：范文 + 注入缺陷（不调 API）
    python eval_ppt.py -n 1           # 真跑：文理各 1 次
    python eval_ppt.py -n 2 --retry 1 # 带闸门回投重试
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import PPT_CONTAMINATION, PPT_SECTIONS, split_by_names, validate_ppt
from eval_planner import load_api_key
from prompt_builder import PromptLib
from quality_gate import require_inputs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 范文【示例输入】里的字段标签（解析用；只认已知标签，别用「下一行带冒号」切段）。
#
# 必须把输入契约里的**每个**字段都列上：清单不全时，最后一个命中标签会把后面
# 未列出的行一并吞掉。真实事故：漏了「单元内容」，于是「学生水平」解析成
# "基础\n单元内容：无"——单看没报错，但拿它当 case 就永远匹配不上档位，
# 渲染进提示词更会把「单元内容：无」凭空插到句子中间。
_LABEL_RE = re.compile(r"^(学科|课件主题|页数要求|学生水平|单元内容)：", re.M)

# ---------------------------------------------------------------- 用例
# 文理各一例，刻意避开范文主题（化学·质量守恒 / 历史·丝绸之路）与其他角色的评测用例
# （物理浮力、欧姆定律、秦统一、爱莲说等），免得污染检测把正常输出判成抄袭。
CASES: dict[str, dict] = {
    "数学_勾股定理": {
        "学科": "初中数学",
        "课件主题": "勾股定理",
        "页数要求": 8,
        "学生水平": "基础",
    },
    "地理_中国的气候": {
        "学科": "初中地理",
        "课件主题": "中国的气候",
        "页数要求": 9,
        "学生水平": "中等",
    },
}


def ppt_vars(case: dict, lib: PromptLib | None = None) -> dict:
    v = {k: str(case[k]) for k in ("学科", "课件主题", "页数要求", "学生水平")}
    v["单元内容"] = str(case.get("单元内容", "无"))   # 默认「从零制作」模式
    if lib is not None:
        # 入口预检：枚举越域（如脏解析带出的换行）必须在这里确定性报错，
        # 而不是把坏值默默渲染进提示词——上面那个 _LABEL_RE 事故就是这种漏网。
        require_inputs(lib, "teacher_ppt", v)
    return v


def parse_example(text: str) -> dict:
    """从 Few-shot 的【示例输入】里解析出 ``学科 / 课件主题 / 页数要求 / 学生水平``。

    刻意用解析而不是另抄一份：范文改了，自检跟着改；抄一份迟早漂移。
    """
    marks = list(_LABEL_RE.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[m.end():stop].strip()
    return out


def fewshot_cases() -> list[tuple[str, dict]]:
    import yaml

    data = yaml.safe_load((ROOT / "fewshots" / "teacher_ppt.yaml").read_text(encoding="utf-8"))
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
        assert case["课件主题"], f"{name} 的示例输入解析不出课件主题：{case}"
        checks = validate_ppt(case["_output"], {"页数要求": case["页数要求"],
                                "课件主题": case["课件主题"],
                                "学生水平": case["学生水平"]}, check_contamination=False)
        failed = [n for n, ok, _ in checks if not ok]
        assert not failed, f"{name} 未通过校验：{failed}"
        total = len(checks)

    # ② 注入型坏样本必须被检出（否则校验器等于没装）
    good = cases[0][1]
    base = good["_output"]
    # 先确认范文结构没变，注入才能钉得住
    for frag in ("第7页（内容）：微观解释", "配图：三个要点汇总图",
                 "· 参加反应的物质质量总和", "4. 【视觉建议】"):
        assert frag in base, f"范文结构已变，注入用例需同步：{frag[:20]}"

    injections = [
        ("模块齐全", base.replace("【视觉建议】", "【排版建议】")),
        ("页号从 1 连续", base.replace("第7页（内容）：微观解释", "第8页（内容）：微观解释")),
        ("每页都有配图与旁白", base.replace("配图：三个要点汇总图", "")),
        ("要点是短语", base.replace(
            "· 参加反应的物质质量总和",
            "· 参加反应的各物质质量总和等于反应后生成物的质量总和，这是一个写得很长的要点，"
            "明显超过了二十五字的限制")),
        ("未输出教学模块", "1. 【本课目标】学会质量守恒\n" + base),
        ("无讲解口吻", base + "\n下面我们来看这道题怎么做。"),
    ]
    for expect, sample in injections:
        failed = [n for n, ok, _ in validate_ppt(sample, {"页数要求": "8",
                                                    "课件主题": good["课件主题"],
                                                    "学生水平": good["学生水平"]},
                                                    check_contamination=False) if not ok]
        assert any(expect in f for f in failed), f"未检出：{expect}（实得 {failed}）"

    # ②b 档位落地：中等档的「可能的提问」被抽掉时必须检出。
    #     这条规则原本只写在提示词里，而范文2（中等档）自己都没照做——
    #     因为校验器与测试都不传 学生水平，谁都没发现。
    mid = next(c for _, c in cases if c["学生水平"] == "中等")
    mid_case = {"页数要求": mid["页数要求"], "课件主题": mid["课件主题"],
                "学生水平": mid["学生水平"]}
    failed = [n for n, ok, _ in validate_ppt(
        mid["_output"].replace("可能的提问", "课后想一想"), mid_case,
        check_contamination=False) if not ok]
    assert any("中等档" in f for f in failed), f"档位落地未被检出：{failed}"

    # ③ 污染词表双向审计：在范文里（命得中）、又不在用例输入里（不误报）
    fe = lib.fewshots["teacher_ppt"]["examples"]
    blob = "".join(str(e["input"]) + str(e["output"]) for e in fe)
    for tok in PPT_CONTAMINATION:
        assert tok in blob, f"污染词「{tok}」不在范文里，永远命不中"
    for name, case in CASES.items():
        text = "".join(str(v) for v in case.values())
        hit = [t for t in PPT_CONTAMINATION if t in text]
        assert not hit, f"用例 {name} 输入自带污染词 {hit}，必然误报"

    # ④ prompt 组装：主题作为独立变量进 prompt、无残留占位符
    for name, case in CASES.items():
        v = ppt_vars(case, lib)
        p = lib.build("teacher_ppt", v)
        assert "{{" not in p, f"{name} 有未填充占位符"
        assert f"课件主题：{case['课件主题']}" in p, f"{name} 未把课件主题作为独立变量传入"
        assert f"页数要求：{case['页数要求']}" in p, f"{name} 未把页数要求传入"

    # ⑤ 单元内容模式：传了单元内容时，它必须进 prompt，且条件说明在
    unit_content = ("【本课目标】掌握勾股定理\n【知识讲解】勾股定理：直角三角形两直角边的"
                    "平方和等于斜边的平方。\n【典型例题】3-4-5 三角形\n【易错提醒】别忘开方\n"
                    "【小结】勾股定理描述三边关系")
    p2 = lib.build("teacher_ppt", {**ppt_vars(CASES["数学_勾股定理"]), "单元内容": unit_content})
    assert unit_content in p2, "单元内容未进入 prompt"
    assert "关于「单元内容」" in p2, "缺少单元内容的条件说明"

    print("离线自检通过：")
    print(f"  · {len(cases)} 份范文通过全部 {total} 项检查")
    print(f"  · {len(injections)} 类注入缺陷全部被检出"
          "（含「写成教案」「页号断」「空页」「要点写成段落」）")
    print(f"  · 污染词表 {len(PPT_CONTAMINATION)} 个词双向审计通过")
    print(f"  · {len(CASES)} 个用例的主题独立进入 prompt")


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

    print(f"模型: {args.model}  每例 {args.n} 次\n")

    per: dict[str, list[list[tuple[str, bool, str]]]] = {}
    first_fail: dict[str, list[str]] = {}
    first_fail_runs = 0
    n_runs = 0
    for name, case in CASES.items():
        v = ppt_vars(case, lib)
        prompt = lib.build("teacher_ppt", v, fewshot=True)
        print(f"  用例 {name}：{case['课件主题']}（{case['页数要求']} 页）")
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
                checks = validate_ppt(raw, {"页数要求": case["页数要求"],
                                            "课件主题": case["课件主题"],
                                            "学生水平": case["学生水平"]})
                failed = [x for x in checks if not x[1]]
                if attempt == 0:
                    first_fail.setdefault(name, []).extend(x[0] for x in failed)
                    if failed:
                        first_fail_runs += 1
                suffix = f"_r{attempt}" if attempt else ""
                (RUNS_DIR / f"ppt_{name}_{stamp}_{i}{suffix}.md").write_text(
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
