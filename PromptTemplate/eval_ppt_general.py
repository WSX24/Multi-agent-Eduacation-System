#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 PPT（ppt_general）真实模型评测

这个模板与 teacher_ppt 的区别：teacher_ppt 是**课件体**（受众=学生、场景=授课、
旁白=配音讲稿），本模板是**教学逐页大纲体**（受众固定=学生、场景固定=教学、旁白=演讲提示）。
校验重点也不同：

  S1 模块完整：主题与受众 / 整体结构 / 逐页大纲 / 视觉建议 四模块齐全、顺序未变、非空
  S2 主题与受众到位：声明了受众 / 场景 / 页数，主题沿用输入不自拟
  S3 场景骨架落地：整体结构里命中教学骨架词（概念→原理步骤→示例→练习→小结）
  S4 逐页可执行：页号从 1 连续、每页有配图与旁白、要点是短语不是段落
  S5 旁白是演讲提示：80-200 字，太短 = 内容单薄，太长 = 写成讲稿全文
  S6 视觉建议非空
  S7 交大纲不是交教案：无教学模块名
  S8 无禁止项：无 LaTeX、无 Few-shot 主题污染

用法::

    python eval_ppt_general.py --self-check   # 离线：范文 + 注入缺陷（不调 API）
    python eval_ppt_general.py -n 1           # 真跑：文理各 1 次
    python eval_ppt_general.py -n 2 --retry 1 # 带闸门回投重试
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import (PPT_GENERAL_CONTAMINATION, PPT_GENERAL_SECTIONS,
                          validate_ppt_general)
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 范文【示例输入】里的字段标签（解析用；只认已知标签，别用「下一行带冒号」切段）
_LABEL_RE = re.compile(r"^(主题|场景|受众|页数要求|核心重点|边界|风格偏好)：", re.M)

# ---------------------------------------------------------------- 用例
# 文理各一例、都是教学场景，刻意避开范文主题（全等三角形 / 议论文）与其它角色的
# 评测用例（勾股定理、中国的气候、质量守恒、丝绸之路等），免得污染检测误报。
CASES: dict[str, dict] = {
    "理科_一次函数": {
        "主题": "一次函数的图像与性质",
        "场景": "教学",
        "受众": "学生",
        "页数要求": 9,
        "核心重点": "k 与 b 对图像的影响",
        "边界": "不展开二次函数",
        "风格偏好": "无",
    },
    "文科_古诗词意象": {
        "主题": "古诗词的意象分析",
        "场景": "教学",
        "受众": "学生",
        "页数要求": 10,
        "核心重点": "常见意象与情感对应",
        "边界": "不展开格律平仄",
        "风格偏好": "无",
    },
}


def ppt_general_vars(case: dict) -> dict:
    return {k: str(case[k]) for k in ("主题", "场景", "受众", "页数要求",
                                      "核心重点", "边界", "风格偏好")}


def parse_example(text: str) -> dict:
    """从 Few-shot 的【示例输入】里解析出 7 个字段。

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

    data = yaml.safe_load((ROOT / "fewshots" / "ppt_general.yaml").read_text(encoding="utf-8"))
    out = []
    for i, ex in enumerate(data["examples"], 1):
        case = parse_example(ex["input"])
        case["_output"] = ex["output"]
        out.append((f"范文{i}", case))
    return out


def _case_for(case: dict) -> dict:
    return {"主题": case["主题"], "受众": case["受众"],
            "场景": case["场景"], "页数要求": case["页数要求"]}


def self_check(lib: PromptLib) -> None:
    """离线自检：范文自身合规 + 坏样本必被检出 + 污染词表双向审计 + prompt 组装。"""
    cases = fewshot_cases()

    # ① 范文示例输出应当通过全部检查（拿它当「已知合规样本」）
    total = 0
    for name, case in cases:
        assert case["主题"], f"{name} 的示例输入解析不出主题：{case}"
        checks = validate_ppt_general(case["_output"], _case_for(case),
                                      check_contamination=False)
        failed = [n for n, ok, _ in checks if not ok]
        assert not failed, f"{name} 未通过校验：{failed}"
        total = len(checks)

    # ② 注入型坏样本必须被检出（否则校验器等于没装）
    good = cases[0][1]
    base = good["_output"]
    # 先确认范文结构没变，注入才能钉得住
    for frag in ("第1页（封面）：全等三角形的判定", "4. 【视觉建议】",
                 "· 形状相同、大小相等", "· SSS 看三边"):
        assert frag in base, f"范文结构已变，注入用例需同步：{frag[:20]}"

    injections = [
        ("模块齐全", base.replace("【视觉建议】", "【排版建议】")),
        ("页号从 1 连续", base.replace(
            "第6页（练习）：动手试一试", "第7页（练习）：动手试一试")),
        ("每页都有配图与旁白", base.replace("配图：两个完全重合的三角形示意图", "")),
        ("旁白是演讲提示", re.sub(r"旁白：[^\n]*", "旁白：谢谢。", base, count=1)),
        ("要点是短语", base.replace(
            "· 形状相同、大小相等",
            "· 全等三角形就是形状完全相同大小也完全相等并且能够完全重合在一起的两个三角形")),
        ("未输出教学模块", "1. 【本课目标】学会判定\n" + base),
        ("场景骨架落地", re.sub(
            r"2\. 【整体结构】[^\n]*",
            "2. 【整体结构】三部分：背景 → 内容 → 结论。逻辑线：先背景、再内容、最后结论。",
            base)),
    ]
    for expect, sample in injections:
        failed = [n for n, ok, _ in validate_ppt_general(
            sample, _case_for(good), check_contamination=False) if not ok]
        assert any(expect in f for f in failed), f"未检出：{expect}（实得 {failed}）"

    # ③ 污染词表双向审计：在范文里（命得中）、又不在用例输入里（不误报）
    fe = lib.fewshots["ppt_general"]["examples"]
    blob = "".join(str(e["input"]) + str(e["output"]) for e in fe)
    for tok in PPT_GENERAL_CONTAMINATION:
        assert tok in blob, f"污染词「{tok}」不在范文里，永远命不中"
    for name, case in CASES.items():
        text = "".join(str(v) for v in case.values())
        hit = [t for t in PPT_GENERAL_CONTAMINATION if t in text]
        assert not hit, f"用例 {name} 输入自带污染词 {hit}，必然误报"

    # ④ prompt 组装：主题/场景作为独立变量进 prompt、无残留占位符
    for name, case in CASES.items():
        v = ppt_general_vars(case)
        p = lib.build("ppt_general", v)
        assert "{{" not in p, f"{name} 有未填充占位符"
        assert f"主题：{case['主题']}" in p, f"{name} 未把主题作为独立变量传入"
        assert f"场景：{case['场景']}" in p, f"{name} 未把场景传入"

    print("离线自检通过：")
    print(f"  · {len(cases)} 份范文通过全部 {total} 项检查")
    print(f"  · {len(injections)} 类注入缺陷全部被检出"
          "（含「场景骨架丢失」「写成教案」「页号断」「空页」「旁白太短」「要点写成段落」）")
    print(f"  · 污染词表 {len(PPT_GENERAL_CONTAMINATION)} 个词双向审计通过")
    print(f"  · {len(CASES)} 个用例的主题/场景独立进入 prompt")


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
        v = ppt_general_vars(case)
        prompt = lib.build("ppt_general", v, fewshot=True)
        print(f"  用例 {name}：{case['主题']}（{case['页数要求']} 页 · {case['场景']} · {case['受众']}）")
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
                checks = validate_ppt_general(raw, _case_for(case))
                failed = [x for x in checks if not x[1]]
                if attempt == 0:
                    first_fail.setdefault(name, []).extend(x[0] for x in failed)
                    if failed:
                        first_fail_runs += 1
                suffix = f"_r{attempt}" if attempt else ""
                (RUNS_DIR / f"ppt_general_{name}_{stamp}_{i}{suffix}.md").write_text(
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
