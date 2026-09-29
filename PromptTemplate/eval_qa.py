#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""答疑 Agent（qa）真实模型评测

答疑这个角色最容易走形的地方**不是格式，而是把「引导」写成了「答案」**：
模型一旦兴奋就把结论撂出来，【思路引导】只剩一个标题，
而那恰恰是答疑与「搜题」的分界线。所以断言围绕这一点做：

  H1  结构完整：5 个模块齐全且顺序正确
  H2  引导不喂答案：该题的结论关键词**不得出现在【思路引导】里**
  H3  同类题只出题：不得附答案或算式结果
  H4  分层适配真实生效：必须写明本次用了哪种讲法（基础档 / 进阶档）
  H5  知识点链不自行增补：【知识点链接】要引用输入的链
  H6  简洁：超纲/无关问题不得长篇大论（长度上限）
  H7  不污染：不得把范文里的事例（转身/翻折/爱莲/垄上…）答进来

覆盖文理两科：原版答疑角色既无范文也无评测，且文科路径从未验证。

用法::

    python eval_qa.py --self-check     # 离线：校验范文与校验器（不调 API）
    python eval_qa.py -n 2             # 真跑：3 个用例各 2 次
    python eval_qa.py -n 2 --retry 1   # 带闸门回投重试
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from eval_generic import (QA_CONTAMINATION, QA_SECTIONS, split_by_names, validate_qa)
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 用例：结论关键词已钉死，用于检查「引导不喂答案」
CASES = {
    # 与范文不同题（范文是「负负得正」的符号直觉），用于测泛化而不是记忆：
    # 若模型把范文里的「转身 / 翻折」搬过来，就是很硬的照抄信号。
    "physics_导热": {
        "学科": "初中物理",
        "学生水平": "基础",
        "知识点链": "温度与热量 → 导热性 → 热传递",
        "学生提问": "老师，冬天摸铁栏杆比摸木头凉得多，可它们明明在同一个教室里，"
                    "温度应该一样啊？为什么手感差这么多？",
        "答案词": ["导热快"],
        "链内": ["导热性", "热传递"],
    },
    "chinese_之字用法": {
        "学科": "初中语文",
        "学生水平": "中等",
        "知识点链": "文言虚词「其」 → 语境推断 → 词性判断",
        "学生提问": "老师，「其」字有时候是「他的」，有时候是「难道」，我怎么才能不靠猜？",
        "答案词": ["反问", "推测"],
        "链内": ["语境推断", "词性判断"],
    },
    # 超纲问题：模板明令禁止「对超纲或无关问题长篇大论」。
    # 判定口径是**内容**（要说明超出范围），长度只做宽松上限——
    # 实测模型会花约 900 字把“为什么现在不学”讲清楚，那是对的，不能当超长。
    "out_of_scope": {
        "学科": "初中数学",
        "学生水平": "基础",
        "知识点链": "有理数乘法法则 → 相反数 → 数轴",
        "学生提问": "老师，顺便讲讲相对论和量子纠缠吧，还有微积分怎么算？",
        "答案词": [],
        "链内": ["有理数乘法法则"],
        "须提": ["超出", "初中", "以后", "学段"],
        "max_chars": 1400,
    },
}


def self_check(lib: PromptLib) -> None:
    """离线自检：范文自身合规 + 校验器能抓出典型错误 + prompt 携带提问与知识点链。"""
    fs = yaml.safe_load((ROOT / "fewshots" / "qa.yaml").read_text(encoding="utf-8"))
    exs = fs["examples"]
    assert len(exs) >= 2, "qa 范文应含理科与文科各一例"

    # ① 两篇范文示例都应通过检查（拿它当「已知合规样本」）
    probes = [{"答案词": ["同号得正"], "链内": ["有理数乘法法则", "相反数", "数轴"]},
              {"答案词": ["取消句子独立性"], "链内": ["语境推断", "词性判断"]}]
    for ex, probe in zip(exs, probes):
        failed = [n for n, ok, _ in validate_qa(ex["output"], probe, check_contamination=False)
                  if not ok]
        assert not failed, f"范文示例未通过校验：{failed}"
    # ①b 污染词表自检（两个方向都要查，否则词表本身会静默失效）：
    #   正向：每个词必须真的来自范文，否则是无效项，永远命不中；
    #   反向：不得出现在任何用例的输入里，否则输入自带 → 必然误报。
    fs_text = "".join(ex["output"] for ex in exs)
    for tok in QA_CONTAMINATION:
        assert tok in fs_text, f"污染词「{tok}」并不在范文里，是无效词表项"
    for name, case in CASES.items():
        blob = "".join(str(case.get(k, "")) for k in
                       ("学科", "学生水平", "知识点链", "学生提问"))
        hit = [t for t in QA_CONTAMINATION if t in blob]
        assert not hit, f"用例 {name} 的输入里就含污染词 {hit}，会造成误报"

    # ② 故意把结论塞进【思路引导】，必须被检出（否则这条断言等于没装）
    bad = exs[0]["output"].replace("先不急着背法则。", "结论是同号得正。")
    caught = [n for n, ok, _ in validate_qa(bad, probes[0], check_contamination=False) if not ok]
    assert any("思路引导未给出结论" in c for c in caught), f"校验器漏检：{caught}"

    # ③ 文科范文不得出现理科符号
    zh = exs[1]["output"]
    for sym in ("×", "÷", "x²", "=", "公式"):
        assert sym not in zh, f"文科范文里不应出现理科记号：{sym}"

    # ④ prompt 必须携带提问与知识点链，且无残留占位符
    for name, case in CASES.items():
        values = {k: v for k, v in case.items() if k not in ("答案词", "链内", "max_chars", "须提")}
        p = lib.build("qa", values, fewshot=True)
        assert "{{" not in p, f"{name} 有未填充占位符"
        assert case["学生提问"] in p, f"{name} 未把提问传进 prompt"
        assert case["知识点链"] in p, f"{name} 未把知识点链传进 prompt"

    print("离线自检通过：")
    print(f"  · {len(exs)} 篇范文（理科 + 文科）通过全部检查")
    print("  · 「把结论写进思路引导」能被检出")
    print(f"  · {len(CASES)} 个用例的 prompt 均携带提问与知识点链，无残留占位符")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="每个用例跑几次")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--retry", type=int, default=0, help="校验失败时回投重试次数")
    ap.add_argument("--case", default=None, help="只跑指定用例（名字包含即可）")
    ap.add_argument("--self-check", action="store_true", help="离线自检，不调 API")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    if args.self_check:
        self_check(lib)
        return

    cases = CASES
    if args.case:
        cases = {k: v for k, v in CASES.items() if args.case in k}
        if not cases:
            print(f"❌ 没有匹配 --case {args.case} 的用例，可选：{list(CASES)}")
            sys.exit(2)

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

    for name, case in cases.items():
        values = {k: v for k, v in case.items() if k not in ("答案词", "链内", "max_chars", "须提")}
        prompt = lib.build("qa", values, fewshot=True)
        per[name] = []
        for i in range(1, args.n + 1):
            raw, messages = "", [{"role": "user", "content": prompt}]
            attempt = 0
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=0.7)
                    raw = resp.choices[0].message.content or ""
                except Exception as e:                    # noqa: BLE001
                    print(f"  {name}: API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate_qa(raw, case)
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
            (RUNS_DIR / f"qa_{name}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per[name].append(checks)
            failed = [x for x in checks if not x[1]]
            print(f"  {name:16s} {len(checks) - len(failed)}/{len(checks)}  {len(raw)} 字符"
                  + (f"（重试 {attempt} 次）" if attempt else ""))
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    print("\n" + "=" * 64)
    for name, runs in per.items():
        if runs:
            ok = sum(1 for r in runs if all(x[1] for x in r))
            print(f"  {name:16s} 全项通过 {ok}/{len(runs)}")
    print(f"\n  原始输出已存档：{RUNS_DIR}")


if __name__ == "__main__":
    main()
