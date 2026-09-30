#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""replay_eval.py（L2 回放）的测试。

回放脚本本身也是断言系统的一部分：它一旦解析错文件名、或漏登记某个用例，
就会**静默地少验东西**——那种失败最贵，因为报告上显示的是一片绿。
所以这里专门把「少验」的路径钉死。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import replay_eval as R  # noqa: E402

RUNS = ROOT / "eval_runs"


# ------------------------------------------------------------------ 文件名解析
def test_parse_both_filename_shapes():
    """两种落盘形状都要认：首轮 `_1`，重试后修正版 `_1_r1`。"""
    recs, skipped = R.discover(ROOT / "tests_nonexistent_dir")
    assert recs == [] and skipped == []

    m1 = R.NAME_RE.match("assistant_全对_20260927_171022_1.md")
    assert m1 and m1.group("stem") == "assistant_全对"
    assert m1.group("idx") == "1" and m1.group("retry") is None

    m2 = R.NAME_RE.match("planner_chinese_20260928_201055_1_r1.md")
    assert m2 and m2.group("stem") == "planner_chinese"
    assert m2.group("idx") == "1" and m2.group("retry") == "1"

    # 用例名自带下划线（qa 的用例名就是 physics_导热），必须整段留着
    m3 = R.NAME_RE.match("qa_physics_导热_20260928_220137_1.md")
    assert m3 and m3.group("stem") == "qa_physics_导热"


def test_discover_splits_role_and_case_on_first_underscore():
    rec = R.Recording(Path("qa_physics_导热_20260928_220137_1.md"), "qa", "physics_导热", 1, False)
    assert rec.key == ("qa", "physics_导热")
    assert rec.label == "qa/physics_导热"


def test_discover_strips_experiment_prefix_instead_of_silently_skipping(tmp_path):
    """实验脚本的录制名把「运行种类」写在角色前，必须先剥掉一层。

    不剥的后果不是报错，而是**静默少验**：报告里只多一句“涉及前缀 rollout”，
    看上去一片绿。supervisor 的 6 份录制就是这样躲过 L2 的（V2.0-说明.md 缺口 #2）。
    """
    name = "rollout_supervisor_完整实例_同题_20260926_221243_1.md"
    (tmp_path / name).write_text("1. 【一句话问候】小明\n", encoding="utf-8")
    recs, skipped = R.discover(tmp_path)
    assert not skipped
    assert len(recs) == 1
    rec = recs[0]
    assert rec.kind == "rollout"
    assert rec.key == ("supervisor", "完整实例_同题"), "前缀没剥干净，永远匹配不上已登记用例"
    assert rec.label == "rollout_supervisor/完整实例_同题", "报告里仍要能认出它来自哪类实验"

    # 角色名本身以实验前缀开头时不得误剥（不存在这种角色，但契约要明确）
    (tmp_path / "teacher_基础_20260927_171022_1.md").write_text("x", encoding="utf-8")
    recs2, _ = R.discover(tmp_path)
    assert ("teacher", "基础") in {r.key for r in recs2}


# ------------------------------------------------------------------ 取代关系
def test_superseded_drops_first_attempt_when_retry_exists():
    """重试机制会同时留下首轮（失败）与 `_r1`（修正后）两份。

    首轮那两份按设计就是不合格的。若不剔除，守门模式会天天红，
    然后人就会开始无视红灯——闸门就此失效。
    """
    recs = [
        R.Recording(Path("unit_noex_20260928_212200_1.md"), "unit", "noex", 1, False),
        R.Recording(Path("unit_noex_20260928_212200_1_r1.md"), "unit", "noex", 1, True),
        # 另一轮、没有重试版：不应被判为被取代
        R.Recording(Path("unit_noex_20260928_212323_1.md"), "unit", "noex", 1, False),
    ]
    gone = R.superseded(recs)
    assert Path("unit_noex_20260928_212200_1.md") in gone
    assert Path("unit_noex_20260928_212200_1_r1.md") not in gone
    assert Path("unit_noex_20260928_212323_1.md") not in gone


# ------------------------------------------------------------------ 登记完整性
def test_registry_covers_every_case_of_every_eval_script():
    """每个 eval 脚本的每个用例，都必须有回放入口。

    这条是防「静默少验」的关键：若某天在 eval_qa 里加了新用例却忘了登记，
    回放不会报错，只会安安静静地不验它。
    """
    import eval_assistant
    import eval_essay
    import eval_planner
    import eval_qa
    import eval_sprint
    import eval_supervisor
    import eval_teacher
    import eval_unit

    targets = R.build_targets()
    expected = {
        "assistant": [n for n, s in eval_assistant.CASES.items() if not s.get("precheck_only")],
        "essay": list(eval_essay.CASES),
        "planner": list(eval_planner.CASES),
        "qa": list(eval_qa.CASES),
        "sprint": list(eval_sprint.CASES),
        "supervisor": list(eval_supervisor.CASES),
        "teacher": list(eval_teacher.CASES),
        "unit": list(eval_unit.CASES),
    }
    for role, cases in expected.items():
        for case in cases:
            assert (role, case) in targets, f"{role}/{case} 未登记，回放会静默漏掉它"

    # 消融实验留下的录制用的是它自己的用例名（把 Few-shot 条件编进了名字），
    # 别名也必须登记——否则那 6 份 supervisor 录制照样落进“未登记”堆里。
    for alias, name in eval_supervisor.REPLAY_ALIASES.items():
        assert ("supervisor", alias) in targets, f"别名 {alias} 未登记"
        assert name in eval_supervisor.CASES, f"别名 {alias} 指向不存在的用例 {name}"

    # 「字段缺失」由入口预检拦下，永远不产生录制，故刻意不登记
    assert ("assistant", "字段缺失") not in targets


def test_validator_returns_triples():
    """校验器返回契约：[(检查项, 是否通过, 说明)]，回放依赖这个形状。"""
    targets = R.build_targets()
    checks = targets[("qa", "out_of_scope")]("1.【思路引导】空\n")
    assert checks and all(isinstance(c, tuple) and len(c) == 3 for c in checks)
    assert all(isinstance(c[1], bool) for c in checks)


# ------------------------------------------------------------------ CLI 契约
requires_runs = pytest.mark.skipif(
    not RUNS.exists() or not list(RUNS.glob("*.md")),
    reason="需要 eval_runs/ 里的真实录制（该目录被 gitignore）",
)


@requires_runs
def test_supervisor_recordings_are_replayed_not_skipped():
    """消融实验留下的 6 份 supervisor 录制必须真进回放（此前被静默跳过）。"""
    recs, _ = R.discover(RUNS)
    sup = [r for r in recs if r.role == "supervisor"]
    assert len(sup) >= 6, f"应当找到消融实验的 supervisor 录制，实得 {len(sup)}"
    targets = R.build_targets()
    outcomes, unregistered = R.run_all(sup, targets)
    assert not unregistered, "supervisor 录制仍未被登记"
    assert all(o.checks for o in outcomes), "登记了却没真跑断言（空检查项＝假绿）"


@requires_runs
def test_gate_returns_1_on_known_bad_recording():
    """守门模式必须真能拦住回归——否则它只是打印了一堆好看的字。"""
    bad = RUNS / "unit_high_20260928_212200_1.md"      # 首轮「单元作业 0 题」，已知不合格
    if not bad.exists():
        pytest.skip(f"缺少样本 {bad.name}")
    manifest = ROOT / "_tmp_bad_manifest.txt"
    manifest.write_text(f"# 故意放一份已知不合格的录制\n{bad.name}\n", encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "replay_eval.py"),
             "--expect", str(manifest), "--strict"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        manifest.unlink(missing_ok=True)
    assert r.returncode == 1, f"守门模式没拦住已知不合格的录制\n{r.stdout[-800:]}"


@requires_runs
def test_gate_refuses_to_pass_when_corpus_missing(tmp_path):
    """语料缺失时守门模式必须**报错退出**，不得静默通过。

    这是本脚本最危险的一种失败：清单里的录制一份都没跑，却因为「没有失败的」
    而返回成功。空了就绿，比红了没人看更糟。
    """
    manifest = tmp_path / "baseline.txt"
    manifest.write_text("assistant_全对_20260927_171022_1.md\n", encoding="utf-8")
    missing_dir = tmp_path / "empty_runs"
    missing_dir.mkdir()
    r = subprocess.run(
        [sys.executable, str(ROOT / "replay_eval.py"),
         "--dir", str(missing_dir), "--expect", str(manifest), "--strict"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 2, f"语料为空竟然没报错（退码 {r.returncode}）\n{r.stdout[-500:]}"
    assert "没有可回放的录制" in r.stdout

    # 目录整个不存在时同样不得静默通过
    r2 = subprocess.run(
        [sys.executable, str(ROOT / "replay_eval.py"),
         "--dir", str(tmp_path / "nope"), "--expect", str(manifest), "--strict"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r2.returncode == 2

    # 语料在、但清单里的文件对不上：也必须报错
    r3 = subprocess.run(
        [sys.executable, str(ROOT / "replay_eval.py"),
         "--dir", str(RUNS), "--expect", str(manifest), "--strict"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r3.returncode == 0, f"清单内合格录制应当放行\n{r3.stdout[-500:]}"

    bad_manifest = tmp_path / "bad.txt"
    bad_manifest.write_text("根本没这个文件_20260101_000000_1.md\n", encoding="utf-8")
    r4 = subprocess.run(
        [sys.executable, str(ROOT / "replay_eval.py"),
         "--dir", str(RUNS), "--expect", str(bad_manifest), "--strict"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r4.returncode == 2, "清单里的文件不存在时守门模式竟然放行了"


@requires_runs
def test_selftest_passes_and_states_its_boundary():
    """自检应当：M1（校验器变严）抓到、M2（校验器变松）抓不到。

    M2 抓不到是**预期**而非缺陷——它正是 L2 适用边界的证据。
    若哪天 M2 也抓到了，说明语料里多了「已知坏样本」，那这条断言要一起改。
    """
    r = subprocess.run(
        [sys.executable, str(ROOT / "replay_eval.py"), "--selftest"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"自检未通过\n{r.stdout[-1200:]}"
    assert "✅ 抓到" in r.stdout
    assert "抓不到（符合预期）" in r.stdout
