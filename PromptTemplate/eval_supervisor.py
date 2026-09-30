#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""督学 Agent（supervisor）真实模型评测

为什么补这一个：`supervisor` 是 8 个角色里唯一「端到端流程里真跑、却没人针对性
验过」的一个。它此前只有 `eval_generic.make_section_validator` 生成的**结构**
校验（5 个模块齐不齐、顺序、有无 LaTeX），而模板最要紧的四条约束全在「输出纪律」
那一段，全是内容——一条都没被验过：

  S1  结构完整：5 个模块齐全且顺序正确（沿用结构校验）
  S2  问候叫出学生姓名
  S3  数据只用输入的：【数据回顾】里的数字必须能在输入的{{学习数据}}里找到
  S4  不与同学比较：不得出现排名 / 全班 / 别的同学这类横向比较
  S5  先讲亮点：说了负面（如「正确率偏低」）时，正向表述必须出现在它前面
  S6  今日任务 1-3 个（模板硬约束：不超过 3 个）
  S7  任务量随水平下调：低水平档不得因为进度落后而加量（用例给「任务上限」）
  S8  每条任务写明「做什么 + 做多少」——只写「把错题标一下」不算
  S9  不制造焦虑：禁止倒计时 / 落后 / 惩罚 / 「再不学就来不及了」
  S10 复习提醒要有依据，且引用输入的{{待复习知识点}}

为什么是这几条：它们全部可程序化判定，所以能进闸门；而「语气是否真的温暖」这
类判不了的，就不假装能判——只留在人看的评价里。

用法::

    python eval_supervisor.py --self-check     # 离线：范文 + 校验器 + 注入实验（不调 API）
    python eval_supervisor.py -n 2             # 真跑：4 个用例各 2 次
    python eval_supervisor.py -n 2 --retry 1   # 带闸门回投重试

回放（L2，不花钱）：这 4 个用例已登记进 `replay_eval.py`；其中 `同题` 的输入与
`eval_skeleton_rollout.py` 的「同题」用例逐字相同，因此那次实验留下的
`rollout_supervisor_*_同题_*.md` 录制可以直接被回放（此前它们被静默跳过）。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from eval_generic import SUPERVISOR_CONTAMINATION, validate_supervisor
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

# 用例：``任务上限`` 是给校验器的元键，不进 prompt。
CASES = {
    # 与 eval_skeleton_rollout.py 的 supervisor「同题」用例**逐字相同**（它多传
    # 一个 supervisor 模板并不使用的 {{学科}}）。刻意保持一致：那批录制
    # （rollout_supervisor_完整实例_同题_*）才能拿来当 L2 回放的语料。
    "同题": {
        "学生姓名": "小明",
        "学生水平": "基础",
        "学习数据": "本周做题 20 道，正确率 92%；连续打卡 7 天",
        "待复习知识点": "因式分解",
    },
    "physics_浮力": {
        "学生姓名": "小刚",
        "学生水平": "基础",
        "学习数据": "本周做题 30 道，正确率 70%；已 2 天未登录",
        "待复习知识点": "浮力",
    },
    # 文科轨道：数据是阅读量 / 实词正确率，不是做题量——顺带验「数据只用输入的」
    # 不会因为换了数据形态而失效。
    "chinese_文言虚词": {
        "学生姓名": "小红",
        "学生水平": "进阶",
        "学习数据": "本周完成文言文阅读 6 篇，实词正确率 58%；已 1 天未登录",
        "待复习知识点": "词类活用、之字用法",
    },
    # 最该走形的一档：数据难看 + 久未登录 + 入门水平。
    # 这里同时验「不制造焦虑」与「不得因落后加量」（任务上限 2）。
    "low_久未登录": {
        "学生姓名": "小刚",
        "学生水平": "入门",
        "学习数据": "本周做题 4 道，正确率 30%；已 9 天未登录",
        "待复习知识点": "有理数乘法法则、数轴",
        "任务上限": 2,
    },
}

# 录制用例名 → CASES 键。消融实验（eval_skeleton_rollout）把「Few-shot 条件」
# 编进了用例名，所以同一份输入在录制里叫 `完整实例_同题` / `骨架示例_同题`。
# 不登记这两个别名，那 6 份录制在 L2 里匹配不上用例，会被**静默跳过**。
REPLAY_ALIASES = {
    "完整实例_同题": "同题",
    "骨架示例_同题": "同题",
}

# 范文自检时用的合成用例：镜像 fewshots/supervisor.yaml 那条例子的输入行。
FEWSHOT_CASE = {
    "学生姓名": "小明",
    "学生水平": "基础",
    "学习数据": "小明本周做题 45 道，正确率 62%；已 3 天未登录；薄弱点：因式分解",
    "待复习知识点": "整式乘法",
    "任务上限": 3,
}


def _values(case: dict) -> dict:
    """用例 → prompt 变量（``任务上限`` 只是给校验器的元键）。"""
    return {k: v for k, v in case.items() if k != "任务上限"}


def _failed(checks) -> list[str]:
    return [n for n, ok, _ in checks if not ok]


def _with_section(text: str, section: str, body: str) -> str:
    """把【section】的正文整段换成 ``body``（注入实验用）。"""
    head = f"【{section}】"
    i = text.index(head) + len(head)
    nxt = re.search(r"\n\s*\d{1,2}\s*[.、]\s*【", text[i:])
    end = i + nxt.start() if nxt else len(text)
    return text[:i] + body + text[end:]


def self_check(lib: PromptLib) -> None:
    """离线自检：范文合规 + 校验器能抓出典型错误 + 污染词表双向审计 + prompt 组装。"""
    fs = yaml.safe_load((ROOT / "fewshots" / "supervisor.yaml").read_text(encoding="utf-8"))
    exs = fs["examples"]
    assert len(exs) >= 1, "supervisor 范文至少要有 1 例"
    few = exs[0]["output"]

    # ① 范文必须自身合规（拿它当「已知合规样本」）。check_contamination=False
    #    是因为范文本来就是污染词表的来源，拿它对照自己必然命中。
    bad = _failed(validate_supervisor(few, FEWSHOT_CASE, check_contamination=False))
    assert not bad, f"范文未通过校验：{bad}"

    # ②a 污染词表**正向**审计：每个词都得真的来自范文用例（input 或 output），
    #     否则是死项，永远命不中。
    few_all = str(exs[0]["input"]) + str(exs[0]["output"])
    for tok in SUPERVISOR_CONTAMINATION:
        assert tok in few_all, f"污染词「{tok}」不在范文用例里，是无效词表项"
    # ②b **反向**审计：词表不得与任何用例的输入相交，否则正常作答必然误报。
    #     旧词表里的「小明」就是这样：它既是范文里的学生名，也是「同题」用例传进去的
    #     {{学生姓名}} → 每份正常输出都被判「照抄范文」。这是 eval_qa 踩过的同一类坑，
    #     这里用断言把它钉死。
    for name, case in CASES.items():
        blob = "".join(str(v) for v in _values(case).values())
        hit = [t for t in SUPERVISOR_CONTAMINATION if t in blob]
        assert not hit, f"用例 {name} 的输入里就含污染词 {hit}，会造成误报"
    assert "小明" not in SUPERVISOR_CONTAMINATION, \
        "学生姓名不能进污染词表（它来自用例输入，必然误报）"

    # ③ 注入已知缺陷，每一类断言都要真能抓到（抓不到就等于没装）
    injections = [
        ("编造学习数据（45 道 → 50 道）", few.replace("45 道", "50 道"),
         "数据回顾只用输入数据"),
        ("把负面写在前头",
         _with_section(few, "数据回顾",
                       "你这周正确率偏低，只有 62%，不过积累还是不错的（做了 45 道题）。"),
         "数据回顾先讲亮点"),
        ("任务超 3 条",
         _with_section(few, "今日任务",
                       "① 复习笔记 5 分钟；② 做 5 道题；③ 背 10 个单词；④ 再做 3 道题。"),
         "今日任务 1-3 个"),
        ("任务没写做多少", few.replace("① 用 5 分钟复习", "① 复习"), "每条任务写明做多少"),
        ("制造焦虑", few + "\n再不学就来不及了，你已经落后很多，必须马上补上。",
         "不制造焦虑"),
        ("与同学比较", few + "\n（你的排名已经掉到全班第 20 了）", "不与同学比较"),
        ("复习提醒只罗列内容", _with_section(few, "复习提醒", "复习“整式乘法”。"),
         "复习提醒说明依据"),
        ("丢了模块", "\n".join(l for l in few.splitlines() if "激励语" not in l), "模块齐全(5)"),
    ]
    for label, sample, expect in injections:
        got = _failed(validate_supervisor(sample, FEWSHOT_CASE, check_contamination=False))
        assert any(expect in g for g in got), f"注入「{label}」未被检出（应命中「{expect}」，实得 {got}）"

    # ④ prompt 必须携带全部变量，且无残留占位符
    for name, case in CASES.items():
        p = lib.build("supervisor", _values(case), fewshot=True)
        assert "{{" not in p, f"{name} 有未填充占位符"
        for k, val in _values(case).items():
            assert str(val) in p, f"{name} 未把 {k} 传进 prompt"

    print("离线自检通过：")
    print(f"  · 范文通过全部 {len(validate_supervisor(few, FEWSHOT_CASE, check_contamination=False))} 项检查")
    print(f"  · 污染词表双向审计通过（{len(SUPERVISOR_CONTAMINATION)} 词：既在范文里，又不在用例输入里）")
    print(f"  · {len(injections)} 类注入缺陷全部被检出（编造数据/焦虑/比较/任务超量/无依据…）")
    print(f"  · {len(CASES)} 个用例的 prompt 均携带全部变量，无残留占位符")


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
        prompt = lib.build("supervisor", _values(case), fewshot=True)
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
                checks = validate_supervisor(raw, case)
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
            (RUNS_DIR / f"supervisor_{name}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per[name].append(checks)
            failed = [x for x in checks if not x[1]]
            print(f"  {name:18s} {len(checks) - len(failed)}/{len(checks)}  {len(raw)} 字符"
                  + (f"（重试 {attempt} 次）" if attempt else ""))
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    print("\n" + "=" * 64)
    for name, runs in per.items():
        if runs:
            ok = sum(1 for r in runs if all(x[1] for x in r))
            print(f"  {name:18s} 全项通过 {ok}/{len(runs)}")
    print(f"\n  原始输出已存档：{RUNS_DIR}")
    print("  提示：新录制要进 L2 守门清单，跑 "
          f"`python replay_eval.py --write-baseline eval_baseline.txt`")


if __name__ == "__main__":
    main()
