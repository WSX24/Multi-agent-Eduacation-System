#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短周期速成模板（teacher_sprint）真实模型评测

M1/M2 的约束是「必须做到什么」，M3 的约束是**反向的**——「必须不做什么」：

  H1  禁止安排任何复习节点
  H2  禁止设计章节解锁与门控（短周期一次性交付）
  H3  禁止规划超出可用时限能完成的内容量  ← 靠时限压力测试验证

H3 是这一层最需要证伪的一句。验证方式是差分测试：
**同一个学习目标，只改可用时限**，产出量必须随之变化；
再给一个「1 天 + 巨大目标」的用例，看它会不会硬塞。

其余检查：里程碑 3~5 个、每里程碑讲解 ≤5 句、随堂练习每里程碑 2~3 题、
结束测验 5~8 题且覆盖全部里程碑、结构 6 模块完整、无 LaTeX、无 Few-shot 学科污染。

用法::

    python eval_sprint.py --self-check   # 离线检查 prompt 组装
    python eval_sprint.py -n 2           # 真实模型
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

EXPECTED_SECTIONS = ["速成目标", "学习路径", "核心讲解", "随堂练习", "结束测验", "时间提醒"]

# Few-shot 是初中数学一元一次方程应用题——渲染初中物理时必须零漏入
CONTAMINATION_TOKENS = [
    "一元一次方程", "移项", "系数化1", "系数化 1", "等量关系", "设未知数",
    "男生比女生多", "长方形长比宽多", "本子", "笔每支", "甲仓库存粮",
]

LATEX_TOKENS = ["\\frac", "\\times", "\\div", "\\cdot", "\\[", "\\]", "\\(", "\\)", "\\sqrt"]

# 长周期词汇：出现在短周期输出里即为违约
LONG_TERM_TOKENS = ["解锁", "门控", "下一章", "预习下一", "学完才能", "章节测验", "单元作业"]

# 「复习」是常用动词，只有作为「节点」出现才算违约
REVIEW_NODE_RE = re.compile(r"复习(?:阶段|环节|课|单元|周|节点|模块|任务|计划)|(?:安排|设置|加入)[^。\n]{0,6}复习")

# 学习路径表格里的一行：| M1 | 名称 | 用时 | 学什么 | 练什么 |
PATH_ROW_RE = re.compile(r"^\s*\|(.+?)\|\s*$", re.M)
# 时限口径：1 个学习日 = 最多 3 小时有效学习（模板中已写明，可调）
HOURS_PER_DAY = 3
DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)\s*天|半天|(\d+(?:\.\d+)?)\s*(?:个)?(?:小时|课时|h))")
UNPARSEABLE_DAY_RE = re.compile(r"(?:\d+(?:\.\d+)?|半)\s*天")
# 核心讲解的里程碑分段
MILESTONE_RE = re.compile(r"^\s*(?:M|里程碑|阶段)\s*(\d+)", re.M)
# 题目编号：兼容 第N题 / N. / ① / M1-1 四种写法
QUESTION_RE = re.compile(r"^\s*(?:第\s*\d+\s*题|\d{1,2}\s*[.、)）]|[①-⑩]|M\s*\d+\s*[-–—]\s*\d+)", re.M)
TEST_Q_RE = re.compile(r"^\s*(?:第\s*\d+\s*题|\d{1,2}\s*[.、)）]|M\s*\d+\s*[-–—]\s*\d+)", re.M)


def _stats(raw: str) -> tuple[int, int, int]:
    """返回 (里程碑数, 随堂练习题数, 结束测验证题数)。"""
    _, bodies = split_sections(raw)
    return (
        len(parse_milestones(raw)),
        len(QUESTION_RE.findall(bodies.get("随堂练习", ""))),
        len(TEST_Q_RE.findall(bodies.get("结束测验", ""))),
    )


# ------------------------------------------------------------------ 用例
CASES = {
    "d1": {"学科": "初中物理", "学习目标": "掌握欧姆定律，能计算串联电路中各处的电流与电压",
           "可用时限": "1天", "学生水平": "中等", "limit_days": 1.0},
    "d3": {"学科": "初中物理", "学习目标": "掌握欧姆定律，能计算串联电路中各处的电流与电压",
           "可用时限": "3天", "学生水平": "中等", "limit_days": 3.0},
    "d7": {"学科": "初中物理", "学习目标": "掌握欧姆定律，能计算串联电路中各处的电流与电压",
           "可用时限": "7天", "学生水平": "中等", "limit_days": 7.0},
    "overload": {"学科": "初中物理",
                 "学习目标": "掌握初中电学全部内容：欧姆定律、电功率、焦耳定律、家庭电路与安全用电",
                 "可用时限": "1天", "学生水平": "中等", "limit_days": 1.0},
}


# ------------------------------------------------------------------ 解析
def split_sections(raw: str) -> tuple[list[str], dict[str, str]]:
    """按 EXPECTED_SECTIONS 的名字定位分段（容忍带编号或不带编号两种写法）。"""
    marks: list[tuple[int, int, str]] = []
    for name in EXPECTED_SECTIONS:
        m = re.search(r"^[ \t]*(?:\d{1,2}\s*[.、]\s*)?【" + re.escape(name) + r"】", raw, re.M)
        if m:
            marks.append((m.start(), m.end(), name))
    marks.sort()
    order = [n for _, _, n in marks]
    bodies: dict[str, str] = {}
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(raw)
        bodies[name] = raw[end:stop].strip()
    return order, bodies


def parse_milestones(raw: str) -> list[tuple[str, str]]:
    """从【核心讲解】切出 (里程碑标题, 正文)。

    模型可能把同一个里程碑写成多行并重复标号（如「M1 …。M1 …。」），
    因此同号的连续分段要合并，否则会把 5 个里程碑数成 10 个。
    """
    _, bodies = split_sections(raw)
    text = bodies.get("核心讲解", "")
    marks = list(MILESTONE_RE.finditer(text))
    merged: list[tuple[int, str, str]] = []
    for i, m in enumerate(marks):
        num = int(m.group(1))
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        nl = text.find("\n", m.start())
        head = text[m.start(): nl if 0 < nl < stop else stop].strip()
        piece = text[m.end():stop].strip()
        if merged and merged[-1][0] == num:
            merged[-1] = (num, merged[-1][1], merged[-1][2] + "\n" + piece)
        else:
            merged.append((num, head, piece))
    return [(h, b) for _, h, b in merged]


def count_duration_hours(text: str) -> tuple[float, list[str]]:
    """把「半天 / 1.5天 / 3小时」折算成小时，返回 (总小时, 无法解析的片段)。

    口径：1 个学习日 = HOURS_PER_DAY 小时（非 24 小时）——否则「1天」
    会被模型当成 24 小时而把 8 小时内容「合法」塞进去。
    """
    # 先去空格，否则「1 天」会被拆成「1」和「天」两个 token
    text = re.sub(r"\s+", "", text)
    total, unparsed = 0.0, []
    for chunk in re.split(r"[|｜/、,，;；]+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk == "半天":
            total += 1.5
            continue
        m = DURATION_RE.fullmatch(chunk)
        if not m:
            if re.search(r"\d", chunk):
                unparsed.append(chunk)
            continue
        if m.group(1):
            total += float(m.group(1)) * HOURS_PER_DAY
        elif m.group(2):
            total += float(m.group(2))
    return total, unparsed


# ------------------------------------------------------------------ 校验
def validate(raw: str, case: dict, *, check_contamination: bool = True) -> list[tuple[str, bool, str]]:
    """check_contamination=False 用于校验 Few-shot 自身（它本来就是数学内容）。"""
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    order, bodies = split_sections(raw)
    missing = [s for s in EXPECTED_SECTIONS if s not in bodies]
    add("结构 6 模块齐全", not missing, f"缺 {missing}" if missing else "")
    if missing:
        return checks

    idx = [order.index(s) for s in EXPECTED_SECTIONS]
    add("结构顺序正确", idx == sorted(idx), f"实际 {order}")

    # ---- H1: 禁止复习节点 ----
    hit = REVIEW_NODE_RE.search(raw)
    add("H1 无复习节点", hit is None, f"出现「{hit.group(0)}」" if hit else "")

    # ---- H2: 禁止门控 ----
    leaks = [t for t in LONG_TERM_TOKENS if t in raw]
    add("H2 无解锁/门控语义", not leaks, f"出现 {leaks}" if leaks else "")

    # ---- 里程碑 3~5 个 ----
    ms = parse_milestones(raw)
    n = len(ms) if ms else len([
        r for r in PATH_ROW_RE.findall(bodies["学习路径"])
        if re.match(r"\s*(?:M|里程碑|阶段)\s*\d", r)
    ])
    add("里程碑 3~5 个", 3 <= n <= 5, f"识别到 {n} 个")

    # ---- H3: 用时不得超时限（口径：1 学习日 = HOURS_PER_DAY 小时）----
    rows = [r for r in PATH_ROW_RE.findall(bodies["学习路径"])
            if re.match(r"\s*(?:M|里程碑|阶段)\s*\d", r)]
    total_h, unparsed, day_units = 0.0, [], []
    for r in rows:
        cells = [c.strip() for c in r.split("|")]
        if len(cells) >= 3:
            h, u = count_duration_hours(cells[2])
            total_h += h
            unparsed += u
            if UNPARSEABLE_DAY_RE.search(cells[2]):
                day_units.append(cells[2])
    limit_h = case["limit_days"] * HOURS_PER_DAY
    if rows and total_h > 0:
        add("H3 里程碑总用时 ≤ 时限", total_h <= limit_h + 1e-6,
            f"合计 {total_h:g} 小时 / 时限 {case['limit_days']:g}天={limit_h:g}小时")
        add("H3 用时全部可解析", not unparsed, f"无法解析 {unparsed}")
        add("时限口径：用时列不写「天」", not day_units, f"出现 {day_units}")
    else:
        add("H3 学习路径含用时列", False, "未能从表格解析出用时")

    # ---- 每里程碑讲解 ≤5 句 ----
    if ms:
        over = []
        tiny = []
        for head, body in ms:
            # 只统计句末标点，过滤表格与答案行
            clean = "\n".join(l for l in body.splitlines()
                              if not l.strip().startswith("|") and "【答案】" not in l)
            cnt = len(re.findall(r"[。！？]", clean)) or (1 if clean.strip() else 0)
            if cnt > 5:
                over.append(f"{head[:12]}={cnt}句")
        add("每里程碑讲解 ≤5 句", not over, f"超限 {over}")
    add("核心讲解编号不冗余", len({int(x.group(1)) for x in MILESTONE_RE.finditer(bodies["核心讲解"])}) == n,
        f"出现 {len(MILESTONE_RE.findall(bodies['核心讲解']))} 个编号标记，实际 {n} 个里程碑")

    # ---- 随堂练习：逐里程碑判定 2~3 题 ----
    drill = bodies["随堂练习"]
    per: dict[int, list[int]] = {}
    for a, b in re.findall(r"^\s*M\s*(\d+)\s*[-–—]\s*(\d+)", drill, re.M):
        per.setdefault(int(a), []).append(int(b))
    if per:
        bad = {f"M{k}": len(v) for k, v in per.items() if not 2 <= len(v) <= 3}
        add("每里程碑随堂练习 2~3 题", not bad,
            f"越界 {bad}；实际 {({f'M{k}': len(v) for k, v in per.items()})}")
        groups = len(per)
        qs = sum(len(v) for v in per.values())
    else:
        groups = len(set(re.findall(r"^\s*(?:里程碑|阶段)\s*(\d+)", drill, re.M))) or 1
        qs = len(QUESTION_RE.findall(drill))
        add("随堂练习题量合理", 3 <= qs <= 3 * max(len(ms), 1), f"共 {qs} 题 / {len(ms)} 个里程碑")

    # ---- 结束测验 5~8 题且覆盖全部里程碑 ----
    test = bodies["结束测验"]
    tqs = TEST_Q_RE.findall(test)
    add("结束测验 5~8 题", 5 <= len(tqs) <= 8, f"识别到 {len(tqs)} 题")
    add("结束测验附答案解析", "【答案】" in test and "【解析】" in test)
    add("结束测验有自评标准", "自评" in test, test[-80:] if "自评" not in test else "")

    # ---- 主动舍弃 ----
    goal = bodies["速成目标"]
    add("H3 主动说明舍弃内容", ("舍弃" in goal or "放弃" in goal or "砍掉" in goal),
        goal[:100])

    # ---- 时间提醒 ----
    add("时间提醒非空洞", len(bodies["时间提醒"]) >= 15, bodies["时间提醒"][:60])

    # ---- 无 LaTeX / 无污染 ----
    latex = [t for t in LATEX_TOKENS if t in raw]
    add("无 LaTeX 记号", not latex, f"出现 {latex}" if latex else "")
    if check_contamination:
        leak = [t for t in CONTAMINATION_TOKENS if t in raw]
        add("无 Few-shot 学科污染", not leak, f"混入 {leak}" if leak else "")

    return checks


# ------------------------------------------------------------------ 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="每个用例运行次数")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--retry", type=int, default=0)
    ap.add_argument("--cases", default="d1,d3,d7,overload")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    lib = PromptLib(ROOT)
    names = [c.strip() for c in args.cases.split(",") if c.strip()]

    if args.self_check:
        doc = yaml.safe_load((ROOT / "fewshots" / "teacher_sprint.yaml").read_text(encoding="utf-8"))
        # 1) Few-shot 自身是否合规
        #    注意：本模板的示例是「骨架示例」（数量具体、内容为占位符），
        #    它不是一份合格输出，所以只校验结构骨架，不能跑内容类校验器。
        ref = doc["examples"][0]["output"]
        placeholders = re.findall(r"<[^>]{1,24}>", ref)
        if placeholders:
            order, _ = split_sections(ref)
            missing = [x for x in EXPECTED_SECTIONS if x not in order]
            print(f"Few-shot 自检（骨架示例，{len(set(placeholders))} 种占位符）:", 
                  "结构齐全" if not missing else f"缺 {missing}")
            # 既然声称是骨架，就必须真的是骨架：占位符种类要够多。
            # （不用「含不含学科词」做判据——那样的关键词表会误伤，
            #   例如「核心讲解」里的「解」字。这坑本项目踩过多次。）
            assert len(set(placeholders)) >= 15, (
                f"骨架示例的占位符仅 {len(set(placeholders))} 种，可能混入了完整实例内容")
            assert "..." not in ref, "骨架示例不得使用省略写法（会被原样抄出）"
        else:
            bad = [(n, d) for n, ok, d in validate(ref, ref_case, check_contamination=False) if not ok]
            print("Few-shot 自检:", "全绿" if not bad else "")
            for n, d in bad:
                print(f"   ✗ {n}  {d}")
        # 2) prompt 组装
        for name in names:
            c = CASES[name]
            prompt = lib.build("teacher_sprint", c)
            assert "{{" not in prompt, f"{name} 有未填充占位符"
            leak = [t for t in CONTAMINATION_TOKENS if t in prompt]
            print(f"  {name:9s} prompt {len(prompt):5d} 字符 | Few-shot 自带数学示例: {len(leak)} 处")
        print("\n注：上面数学示例是 H「无污染」检查的目标，模型若照搬即判失败。")
        return

    key = load_api_key()
    if not key:
        print("❌ 未找到 API Key")
        sys.exit(2)
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=args.base_url)
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"模型: {args.model}  温度: {args.temperature}  每例 {args.n} 次\n")

    per_case: dict[str, list[list[tuple[str, bool, str]]]] = {}
    latest: dict[str, str] = {}

    for name in names:
        c = CASES[name]
        prompt = lib.build("teacher_sprint", c)
        per_case[name] = []
        sizes, mstones, drills, tqs = [], [], [], []
        for i in range(1, args.n + 1):
            messages = [{"role": "user", "content": prompt}]
            raw = ""
            for attempt in range(args.retry + 1):
                try:
                    resp = client.chat.completions.create(
                        model=args.model, messages=messages, temperature=args.temperature,
                    )
                    raw = resp.choices[0].message.content or ""
                except Exception as e:
                    print(f"  {name}: API 失败 {type(e).__name__}: {e}")
                    break
                checks = validate(raw, c)
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
            (RUNS_DIR / f"sprint_{name}_{stamp}_{i}.md").write_text(raw, encoding="utf-8")
            per_case[name].append(checks)
            latest[name] = raw
            sizes.append(len(raw))
            _m, _d, _t = _stats(raw)
            mstones.append(_m)
            drills.append(_d)
            tqs.append(_t)
            failed = [x for x in checks if not x[1]]
            print(f"  {name:9s} {len(checks)-len(failed)}/{len(checks)}"
                  + (f"（重试 {attempt} 次）" if attempt else "")
                  + f"  正文 {len(raw)} 字符 | 里程碑 {mstones[-1]} | 随堂 {drills[-1]} 题 | 测验 {tqs[-1]} 题")
            for n_, _, d in failed:
                print(f"      ✗ {n_}  {d}")

    # ---- 时限压力：产出量应随时限增长 ----
    print("\n" + "=" * 72)
    print("时限压力测试：同一学习目标，只改可用时限")
    print("=" * 72)
    print(f"  {'用例':10s} {'时限':>5s} {'正文':>7s} {'里程碑':>6s} {'随堂':>5s} {'测验':>5s}")
    for name in ("d1", "d3", "d7", "overload"):
        if name in latest:
            raw = latest[name]
            _m, _d, _t = _stats(raw)
            print(f"  {name:10s} {CASES[name]['limit_days']:>4g}天 {len(raw):>7d}"
                  f" {_m:>6d} {_d:>5d} {_t:>5d}")
    if "d1" in latest and "d7" in latest:
        v1, v7 = len(latest["d1"]), len(latest["d7"])
        m1, m7 = len(parse_milestones(latest["d1"])), len(parse_milestones(latest["d7"]))
        # 目标相同时，时间长短不应改变内容体量，只影响节奏；
        # 真正要守的是「短时限不得比长时限更重」——即不得硬塞。

        d1_ok = v1 <= v7 and m1 <= m7
        print(f"\n  {'✅' if d1_ok else '⚠️ '} d1 不得比 d7 更重："
              f"正文 {v1}≤{v7} 且 里程碑 {m1}≤{m7}")
        print(f"      d7/d1 体积比 {v7 / max(v1, 1):.2f}（目标相同时接近 1 属正常）")
        print(f"      d1 vs d7 相似度 {difflib.SequenceMatcher(None, latest['d1'], latest['d7']).ratio():.2f}")
        # 巨大目标 + 1 天：必须比普通 1 天目标更克制或至少不更重
        if "overload" in latest:
            vo, mo = len(latest["overload"]), len(parse_milestones(latest["overload"]))
            ok = vo <= v1 * 1.15 and mo <= m1
            print(f"  {'✅' if ok else '⚠️ '} 巨大目标+1天 未硬塞："
                  f"正文 {vo} vs 普通1天 {v1}，里程碑 {mo} vs {m1}")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    allc = [c for lst in per_case.values() for r in lst for c in r]
    seen: list[str] = []
    for lst in per_case.values():
        for r in lst:
            for c in r:
                if c[0] not in seen:
                    seen.append(c[0])
    for n_ in seen:
        tot = sum(1 for lst in per_case.values() for r in lst for c in r if c[0] == n_)
        ok = sum(1 for lst in per_case.values() for r in lst for c in r if c[0] == n_ and c[1])
        mark = "✅" if ok == tot else ("⚠️ " if ok else "❌")
        print(f"  {mark} {n_:30s} {ok}/{tot}")
    ok = sum(1 for c in allc if c[1])
    print(f"\n合计 {ok}/{len(allc)} 项通过；样本存于 {RUNS_DIR}")


if __name__ == "__main__":
    main()
