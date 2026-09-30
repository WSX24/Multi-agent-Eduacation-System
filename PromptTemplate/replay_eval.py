#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 回放校验：拿 eval_runs/ 里的真实录制重跑现成断言，不调模型、不要 API Key。

为什么需要它
------------
现有 10 个 eval_*.py 把「拿到模型输出」和「校验输出」焊在同一个进程里：
没有 Key 就 `sys.exit(2)`，走不到断言（见 EVAL-REPORT 第四节缺口 #3）。
后果是校验器自身的错误只能靠真跑 + 人眼发现——而本项目已经出现过 5 次，
**全部是误报**（qa 三条污染词表误报、一条关键词过于字面、essay 一条范文自引用）。

本脚本把两层拆开：

    L1 组装   eval_*.py --self-check   验 prompt 拼没拼对      离线、确定
    L2 判定   replay_eval.py           验校验器判得对不对      ← 本文件
    L3 行为   eval_*.py -n             验模型有没有真照做      要 Key、不可去人工

L2 抓什么、抓不到什么（先看这里，否则会拿到假安全感）
----------------------------------------------------
抓得到：校验器变**严**造成的误报——一份当初被判合格的录制，现在被判红。
        这正是本项目历史上 5 次校验器缺陷的全部形态。
抓不到：校验器变**松**造成的漏检——录制里的合格样本在更宽松的断言下照样合格。
        要抓这一类，必须另备「已知坏样本」语料，本脚本无法从现有录制里推出来。
        `--selftest` 的 M2 会把这件事现场演示一遍。

用法::

    python replay_eval.py                      # 探索：全部录制跑一遍，看失败分布
    python replay_eval.py -r qa -r essay       # 只看某几个角色
    python replay_eval.py -v                   # 打印所有失败项，不只前几条
    python replay_eval.py --selftest           # 注入已知缺陷，验证本脚本抓得住
    python replay_eval.py --write-baseline eval_baseline.txt   # 冻结当前合格集
    python replay_eval.py --expect eval_baseline.txt --strict  # 守门：清单内任何一份红了就退 1
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

Check = tuple[str, bool, str]
Validator = Callable[[str], list[Check]]

# 录制文件名形如 ``assistant_符号错_20260927_171022_1.md``、
# ``planner_chinese_20260928_201055_1_r1.md``（先用 _rN 表示「重试后修正的版本」）。
NAME_RE = re.compile(
    r"^(?P<stem>.+?)_(?P<stamp>\d{8})_(?P<time>\d{6})"
    r"(?:_(?P<idx>\d+))?(?:_r(?P<retry>\d+))?\.md$"
)

# 实验脚本会在角色名最前面再挂一层「运行种类」前缀：
#   rollout_supervisor_完整实例_同题_20260926_221243_1.md
#   ablation_teacher_基础_...
# 不剥掉这层，「角色」会被切出 "rollout"、用例被切出 "supervisor_完整实例_同题"，
# 于是永远匹配不上已登记用例 → **静默跳过**。supervisor 的 6 份录制就是这样漏掉的
# （见 V2.0-说明.md 缺口 #2）：报告里只写了句“涉及前缀 rollout”，一片绿。
RUN_KINDS = ("rollout", "ablation", "variant", "probe")


@dataclass(frozen=True)
class Recording:
    path: Path
    role: str
    case: str
    idx: int | None  # 同一次评测里的第 i 次采样
    retried: bool  # 重试后才通过的那一版
    kind: str = ""  # 实验脚本挂在最前面的“运行种类”前缀（rollout / ablation / …）

    @property
    def key(self) -> tuple[str, str]:
        return (self.role, self.case)

    @property
    def run(self) -> str:
        """本轮评测的标识 ``YYYYMMDD_HHMMSS``。

        必须带上时刻：同一用例一天可能跑好几轮，只按日期分组会把
        **后一轮**的录制误当成前一轮重试版的“首轮兄弟”而剔除掉。
        """
        m = NAME_RE.match(self.path.name)
        return "" if not m else f"{m.group('stamp')}_{m.group('time')}"

    @property
    def label(self) -> str:
        return f"{self.kind + '_' if self.kind else ''}{self.role}/{self.case}"


@dataclass
class Outcome:
    rec: Recording
    checks: list[Check]

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if ok)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c[1]]

    @property
    def ok(self) -> bool:
        return not self.failed


# --------------------------------------------------------------- 校验器登记
def build_targets() -> dict[tuple[str, str], Validator]:
    """``(角色, 用例)`` → 校验器。

    刻意**复用各 eval 脚本自己的 CASES 与 validate**，不在这里重写一份：
    复制出来的用例一旦与真评测漂移，回放就在验另一套东西了。
    （为此把 eval_unit 内联的用例提到了模块级 CASES。）
    """
    import eval_assistant
    import eval_essay
    import eval_generic
    import eval_planner
    import eval_qa
    import eval_sprint
    import eval_supervisor
    import eval_teacher
    import eval_unit

    targets: dict[tuple[str, str], Validator] = {}

    for name, spec in eval_assistant.CASES.items():
        if spec.get("precheck_only"):
            # 「字段缺失」由入口预检在调用前拦下，永远不产生录制文件。
            continue
        targets[("assistant", name)] = lambda raw, n=name: eval_assistant.validate(raw, n)

    for name, case in eval_qa.CASES.items():
        targets[("qa", name)] = lambda raw, c=case: eval_generic.validate_qa(raw, c)

    essay_case = {"总分": eval_essay.TOTAL, "维度": eval_essay.DIMENSIONS}
    for name in eval_essay.CASES:
        targets[("essay", name)] = lambda raw, c=essay_case: eval_generic.validate_essay(raw, c)

    for name, case in eval_teacher.CASES.items():
        targets[("teacher", name)] = lambda raw, c=case: eval_teacher.validate(raw, c)

    for name, case in eval_unit.CASES.items():
        targets[("unit", name)] = lambda raw, c=case: eval_unit.validate(raw, c)

    for name, case in eval_sprint.CASES.items():
        targets[("sprint", name)] = lambda raw, c=case: eval_sprint.validate(raw, c)

    # planner 的 validate 只吃 raw：它的用例上下文（学科/目标）已烘进 prompt 与断言。
    for name in eval_planner.CASES:
        targets[("planner", name)] = lambda raw: eval_planner.validate(raw)

    # supervisor 此前**完全没有登记**：它的用例既不在上面任何一张表里，
    # 录制文件名又带着 rollout_ 前缀，于是被静默跳过。两处都已补上。
    for name, case in eval_supervisor.CASES.items():
        targets[("supervisor", name)] = lambda raw, c=case: eval_generic.validate_supervisor(raw, c)
    # 同一份输入，消融实验把它叫「完整实例_同题」/「骨架示例_同题」
    for alias, name in eval_supervisor.REPLAY_ALIASES.items():
        case = eval_supervisor.CASES[name]
        targets[("supervisor", alias)] = lambda raw, c=case: eval_generic.validate_supervisor(raw, c)

    return targets


# ------------------------------------------------------------------- 发现
def discover(runs_dir: Path) -> tuple[list[Recording], list[tuple[Path, str]]]:
    found: list[Recording] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(runs_dir.glob("*.md")):
        m = NAME_RE.match(path.name)
        if not m:
            skipped.append((path, "文件名不含 _YYYYMMDD_HHMMSS 时间戳"))
            continue
        stem = m.group("stem")
        kind = ""
        head, _, rest = stem.partition("_")
        if head in RUN_KINDS and rest:
            kind, stem = head, rest
        role, _, case = stem.partition("_")
        if not case:
            skipped.append((path, f"文件名拆不出「角色_用例」：{stem}"))
            continue
        found.append(Recording(
            path, role, case,
            idx=int(m.group("idx")) if m.group("idx") else None,
            retried=m.group("retry") is not None,
            kind=kind,
        ))
    return found, skipped


def superseded(recordings: list[Recording]) -> set[Path]:
    """找出被 ``_rN`` 修正版取代的首轮录制。

    评测脚本在重试成功后才落盘，会同时留下首轮（失败）与 ``_r1``（修正后）两份。
    首轮那两份**按设计就是不合格的**，不该进基线清单，否则守门模式天天红。
    """
    by_run: dict[tuple[str, str, str, int | None], list[Recording]] = defaultdict(list)
    for rec in recordings:
        by_run[(rec.role, rec.case, rec.run, rec.idx)].append(rec)
    gone: set[Path] = set()
    for group in by_run.values():
        if any(r.retried for r in group):
            gone.update(r.path for r in group if not r.retried)
    return gone


def read_manifest(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


# --------------------------------------------------------------------- 跑
def run_all(
    recordings: list[Recording],
    targets: dict[tuple[str, str], Validator],
) -> tuple[list[Outcome], list[Recording]]:
    outcomes: list[Outcome] = []
    unregistered: list[Recording] = []
    for rec in recordings:
        validate = targets.get(rec.key)
        if validate is None:
            unregistered.append(rec)
            continue
        raw = rec.path.read_text(encoding="utf-8")
        outcomes.append(Outcome(rec, validate(raw)))
    return outcomes, unregistered


def report(
    outcomes: list[Outcome],
    unregistered: list[Recording],
    skipped: list[tuple[Path, str]],
    *,
    verbose: bool,
    max_fail: int,
    title: str,
) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")

    by_key: dict[tuple[str, str], list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_key[o.rec.key].append(o)

    print(f"\n【逐用例】共 {len(outcomes)} 份录制、{len(by_key)} 个用例")
    print(f"  {'角色/用例':<26}{'文件':>4}{'通过/总项':>12}   判定")
    for key in sorted(by_key):
        group = by_key[key]
        tot = sum(len(o.checks) for o in group)
        ok = sum(o.passed for o in group)
        bad = [o for o in group if not o.ok]
        mark = "✅" if not bad else f"❌ {len(bad)}/{len(group)} 份有失败项"
        print(f"  {key[0] + '/' + key[1]:<26}{len(group):>4}{f'{ok}/{tot}':>12}   {mark}")

    # 失败项的**系统性**信号：同一个检查项在多份录制上一起红，通常是校验器问题，
    # 而不是某一份输出恰好走形。这是回放最该给出来的东西。
    counter: Counter[str] = Counter()
    for o in outcomes:
        for name, _, _ in o.failed:
            counter[name] += 1

    if counter:
        print(f"\n【失败项分布】{len(counter)} 类，按命中份数排序")
        for name, n in counter.most_common(12):
            print(f"  {n:>3} 份  {name}")
        if len(counter) > 12:
            print(f"  … 另有 {len(counter) - 12} 类")

        print("\n【失败明细】")
        shown = 0
        for o in outcomes:
            if o.ok:
                continue
            rec = o.rec
            tag = "（重试后修正版）" if rec.retried else ""
            print(f"  · {rec.path.name}{tag}")
            for name, _, detail in o.failed:
                if not verbose and shown >= max_fail:
                    print("      …（--max-fail 截断，用 -v 看全部）")
                    shown = 10 ** 9
                    break
                print(f"       ✗ {name}" + (f"：{detail}" if detail else ""))
                shown += 1
            if shown >= 10 ** 9:
                break
    else:
        print("\n✅ 所有已登记录制在当前断言下全项通过")

    if unregistered:
        print(f"\n【未登记：{len(unregistered)} 份没有对应用例，未参与回放】")
        agg: Counter[str] = Counter()
        for rec in unregistered:
            agg[rec.label] += 1
        roles = sorted({rec.label.split("/")[0] for rec in unregistered})
        for label, n in sorted(agg.items()):
            print(f"  {n:>3} 份  {label}")
        print(f"  涉及前缀：{', '.join(roles)}")
        print("  说明：这些是消融/变体/骨架实验的产物，不属于基线用例；"
              "若想纳入回放，先给它们登记校验器。")

    if skipped:
        print(f"\n【文件级跳过：{len(skipped)} 份】")
        agg2: Counter[str] = Counter(reason for _, reason in skipped)
        for reason, n in sorted(agg2.items()):
            print(f"  {n:>3} 份  {reason}")
        for path, _ in skipped[:5]:
            print(f"       例：{path.name}")


# ---------------------------------------------------------------- 自检演示
def _red(recordings: list[Recording], targets: dict[tuple[str, str], Validator],
         role: str) -> dict[str, list[Check]]:
    """跑某个角色的录制，返回 {文件名: 失败项}（只含有失败的）。"""
    recs = [r for r in recordings if r.role == role]
    outcomes, _ = run_all(recs, targets)
    return {o.rec.path.name: o.failed for o in outcomes if not o.ok}


def selftest() -> int:
    """注入两个已知形态的校验器缺陷，看回放能不能抓到。

    关键：只看**注入前后的差集**。直接数“有多少份判红”是错的——语料里本来
    就有历史遗留的失败文件（如 20260926 那批 assistant 录制），它们会把
    注入造成的增量淹没掉，得出“M2 也抓到了”这种假结论。

    M1 校验器变**严**（误报）——复刻 EVAL-REPORT 记的 qa 误报 #2。
    M2 校验器变**松**（漏检）——预期抓不到，用来标出 L2 的边界。
    """
    import eval_assistant
    import eval_generic

    recordings, _ = discover(RUNS_DIR)
    base_targets = build_targets()
    print("回放自检：向校验器注入缺陷，看 L2 抓不抓得住（只计注入造成的增量）")
    print(f"语料：{RUNS_DIR}  共 {len(recordings)} 份\n")

    # ---- M1：污染词表收得过宽（真误报）----
    print("-" * 68)
    print("M1 校验器变严：把正当概念词「翻折」当成 Few-shot 污染信号")
    print("   （复刻 EVAL-REPORT 第七节记的 qa 误报 #2：概念相邻词不能当污染信号）")
    before = _red(recordings, base_targets, "qa")
    original = eval_generic.QA_CONTAMINATION
    eval_generic.QA_CONTAMINATION = list(original) + ["翻折"]
    try:
        after = _red(recordings, build_targets(), "qa")
    finally:
        eval_generic.QA_CONTAMINATION = original
    new_red = sorted(set(after) - set(before))
    print(f"   注入前已有 {len(before)} 份红；注入后新增 {len(new_red)} 份红")
    for name in new_red[:3]:
        check, _, detail = after[name][0]
        print(f"     ✗ {name} → {check}：{detail}")
    m1 = bool(new_red)
    print(f"   结论：{'✅ 抓到' if m1 else '❌ 没抓到'}"
          "——回放能拦住「校验器比输出更严」这类回归")

    # ---- M2：期望模块清单被削短（漏检）----
    print("\n" + "-" * 68)
    print("M2 校验器变松：把 assistant 的「置信度」从期望模块里删掉")
    print("   （少查一项：录制里的合格样本照样合格——这是 L2 抓不到的方向）")
    before2 = _red(recordings, base_targets, "assistant")
    original_va = eval_assistant.validate_assistant
    eval_assistant.validate_assistant = eval_generic.make_section_validator(  # type: ignore[assignment]
        [s for s in eval_generic.ASSISTANT_SECTIONS if s != "置信度"]
    )
    try:
        after2 = _red(recordings, build_targets(), "assistant")
    finally:
        eval_assistant.validate_assistant = original_va  # type: ignore[assignment]
    new_red2 = sorted(set(after2) - set(before2))
    print(f"   注入前已有 {len(before2)} 份红（历史遗留，与本次注入无关）"
          f"；注入后新增 {len(new_red2)} 份红")
    m2 = bool(new_red2)
    print(f"   结论：{'⚠️ 竟然抓到了' if m2 else '❌ 抓不到（符合预期）'}"
          "——要抓漏检必须另备「已知坏样本」语料，现有录制里给不出信号")

    print("\n" + "-" * 68)
    print("L2 的适用边界：")
    print("  抓得到 → 校验器变严（误报）。本项目历史 5 次校验器缺陷全属此类。")
    print("  抓不到 → 校验器变松（漏检）。需要「已知坏样本」语料，属下一步工作。")
    print("  两种都抓不到 → 模型行为退化（模板改了，模型不照做了）。这是 L3 的地盘。")
    return 0 if (m1 and not m2) else 1


# ------------------------------------------------------------------- 入口
def main() -> None:
    ap = argparse.ArgumentParser(description="L2 回放校验：录制重跑断言，不调模型")
    ap.add_argument("--dir", type=Path, default=RUNS_DIR, help="录制目录")
    ap.add_argument("-r", "--role", action="append", help="只看这些角色（可重复）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印所有失败项")
    ap.add_argument("--max-fail", type=int, default=6, help="每个用例最多打印几条失败明细")
    ap.add_argument("--expect", type=Path, help="只对清单里的录制计成败（守门模式）")
    ap.add_argument("--write-baseline", type=Path, metavar="FILE",
                    help="把当前全项通过的录制写成清单文件（守门模式的语料）")
    ap.add_argument("--strict", action="store_true", help="有失败即以退出码 1 结束")
    ap.add_argument("--selftest", action="store_true", help="注入已知缺陷，验证本脚本抓得住")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    targets = build_targets()
    if not args.dir.exists():
        print(f"❌ 语料目录不存在：{args.dir}")
        print("   eval_runs/ 被 .gitignore 排除，换一台机器就没有它。")
        print("   没有录制就什么都验不了——因此这里直接报错，而不是“零份录制 ⇒ 全部通过”。")
        sys.exit(2)
    recordings, skipped = discover(args.dir)
    if not recordings:
        print(f"❌ {args.dir} 里没有可回放的录制（文件名需形如 name_YYYYMMDD_HHMMSS_1.md）")
        sys.exit(2)
    if args.role:
        wanted = set(args.role)
        recordings = [r for r in recordings if r.role in wanted]
        print(f"角色过滤：{', '.join(sorted(wanted))}")

    outcomes, unregistered = run_all(recordings, targets)

    if args.write_baseline:
        gone = superseded(recordings)
        keep = [o for o in outcomes if o.ok and o.rec.path not in gone]
        lines = [
            "# L2 回放基线清单（由 replay_eval.py --write-baseline 生成）",
            "#",
            "# 收录规则：当前断言下**全项通过**、且未被 _rN 修正版取代的录制。",
            "# 注意这条规则是自指的（先把“现在能过”的挑出来，再拿它当基线）——",
            "# 它不是“正确性证明”，而是“当下快照”：冻结今天的行为，让**今后**任何",
            "# 校验器改动都以 diff 的形式暴露出来。若某天改严了校验器，清单里必然",
            "# 有文件变红，那一刻要人工判断“是校验器错了还是判定该收紧”，",
            "# 而不是默默把清单改小——那就把守门当成橡皮图章了。",
            "#",
            f"# 生成于现有 {len(outcomes)} 份录制，收录 {len(keep)} 份。",
        ]
        lines += [o.rec.path.name for o in sorted(keep, key=lambda o: o.rec.path.name)]
        if not keep:
            print("❌ 没有任何全项通过的录制，拒绝写出空清单——空清单会让守门模式看起来在守、实则无物可守")
            sys.exit(2)
        args.write_baseline.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"已写入基线清单：{args.write_baseline}（{len(keep)} 份）")

    if args.expect:
        names = set(read_manifest(args.expect))
        present = {o.rec.path.name for o in outcomes}
        missing = sorted(names - present)
        outcomes = [o for o in outcomes if o.rec.path.name in names]
        title = f"守门模式：清单 {args.expect.name}（{len(names)} 项）"
        # 静默通过是最危险的假绿：清单里的文件没跑，就等于没验。硬失败。
        if missing:
            print(f"❌ 守门模式无法执行：清单里 {len(missing)} 项在语料中不存在")
            for n in missing[:10]:
                print(f"    {n}")
            if len(missing) > 10:
                print(f"    … 另有 {len(missing) - 10} 项")
            print("   请确认录制目录是否完整（eval_runs/ 不入库）。")
            sys.exit(2)
    else:
        title = f"探索模式：{args.dir}"

    report(outcomes, unregistered, skipped, verbose=args.verbose,
           max_fail=args.max_fail, title=title)

    bad = [o for o in outcomes if not o.ok]
    print(f"\n合计：{len(outcomes)} 份录制，{len(outcomes) - len(bad)} 通过，{len(bad)} 有失败项")
    if args.expect:
        print("守门模式：清单内任何一份红了就是回归信号。"
              "若确认是断言该收紧而非校验器出错，请重新生成基线并说明原因。")
    else:
        print("提醒：探索模式下的失败不一定是缺陷——模板与断言此后改过多轮，"
              "早先的录制本来就可能过不了现在的断言。要当回归门用，请用 --expect 固定清单。")

    if (args.strict or args.expect) and bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
