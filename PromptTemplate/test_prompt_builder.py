#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板库冒烟测试：可用 pytest 运行，也可直接 `python test_prompt_builder.py`。"""
import ast
import sys
import tempfile
from pathlib import Path

import re
import yaml

from demo_course_flow import (build_planner_prompt, build_unit_prompt, can_unlock,
                              parse_blueprint, unit_variables)
from eval_generic import ESSAY_SECTIONS, validate_essay, validate_qa
from eval_unit import split_sections, validate as validate_unit
from eval_sprint import count_duration_hours, split_sections as split_sprint_sections, validate as validate_sprint
from prompt_builder import PromptLib
from quality_gate import (QualityGate, blank_inputs, copy_hits, invalid_inputs,
                          load_fewshot_refs, require_inputs, strip_scaffolding)

# Windows 控制台默认 GBK，重配为 UTF-8 以正常显示中文/符号
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
lib = PromptLib(ROOT)


def test_all_roles_build():
    for role in lib.list_roles():
        prompt = lib.build(role)
        assert prompt.strip(), f"模板 {role} 生成结果为空"
        print(f"[OK] {role}: 生成 {len(prompt)} 字符")


def test_variable_fill_and_no_residue():
    prompt = lib.build(
        "teacher",
        {
            "学科": "初中数学",
            "知识点": "因式分解",
            "学生水平": "基础",
            "need_example": "true",
        },
    )
    assert "初中数学" in prompt
    assert "因式分解" in prompt
    assert "{{" not in prompt, "变量填满后不应残留占位符"


def test_custom_sections_are_rendered():
    """回归测试：sections 结构下的自定义小节（如条件分支指令）不得被静默丢弃。"""
    prompt = lib.build("teacher", {}, fewshot=False)
    assert "【条件分支指令】" in prompt
    assert "入门档：讲解降低难度" in prompt
    assert "竞赛档：增加跨知识点综合分析" in prompt


def test_no_literal_equality_branches():
    """条件分支不得写成 `IF {{变量}} == 值`。

    那种写法填入变量后会变成字面量自比（`IF true == false`、`IF 基础 == "入门"`）：
    模型收到一个无意义的比较式，还白发 token。改为“值陈述 + 命中条件”的自然语言。
    """
    for role in lib.list_roles():
        prompt = lib.build(role, {})
        hits = re.findall(r"IF\s+\{\{[^}]*\}\}", prompt)
        assert not hits, f"{role} 仍有字面量比较式分支：{hits[:2]}"


def test_branch_values_still_reach_the_model():
    """消解分支不得把开关变量也一起消掉——值必须仍然进到 Prompt 里。"""
    off = lib.build("teacher", {"学科": "初中数学", "知识点": "因式分解",
                                 "学生水平": "入门", "need_example": "false"}, fewshot=False)
    assert "本次是否需要例题：false" in off
    assert "本次学生水平：入门档" in off
    on = lib.build("teacher", {"学科": "初中数学", "知识点": "因式分解",
                                "学生水平": "竞赛", "need_example": "true"}, fewshot=False)
    assert "本次是否需要例题：true" in on
    assert "本次学生水平：竞赛档" in on


def test_new_teacher_modes_build():
    """新增的教师 Agent 三模式模板均可独立构建。"""
    assert "【核心原则】" in lib.build("teacher_planner", {}, fewshot=False)
    assert "【难度自适应规则】" in lib.build("teacher_unit", {}, fewshot=False)
    assert "【速成目标】" in lib.build("teacher_sprint", {}, fewshot=False)


def test_cot_meta_keys_filtered():
    """snippets.yaml 顶部的 name/label 元数据不应被当作 CoT 片段。"""
    assert "name" not in lib.list_cot()
    assert "label" not in lib.list_cot()


def test_fewshot_concat():
    prompt = lib.build("assistant", fewshot=True)
    assert "【示例输入】" in prompt and "【示例输出】" in prompt


def test_cot_concat():
    prompt = lib.build("qa", cot="math_steps")
    assert "第 1 步【审题】" in prompt


def test_missing_vars():
    missing = lib.missing_vars("teacher", {})
    assert {"学科", "学生水平", "知识点"} <= missing


def test_unit_template_carries_no_fewshot():
    """消融实验证明 Few-shot 对 M2 格式合规无贡献，故刻意不挂——
    既省约 40% token，也彻底消除同题撞车抄袭的风险。"""
    assert "teacher_unit" not in lib.list_fewshots(), "teacher_unit 不应再有 Few-shot 文件"
    prompt = build_unit_prompt(lib, _course(), _course()["chapters"][1]["id"], 0, {})
    assert "【示例输入】" not in prompt, "M2 不应拼接任何示例"
    assert "因式分解" not in prompt and "公因式" not in prompt


# ---------- 蓝图 / 门控：M1 与 M2 的接口契约 ----------
def _course() -> dict:
    doc = yaml.safe_load(
        (ROOT / "fewshots" / "teacher_planner.yaml").read_text(encoding="utf-8")
    )
    return parse_blueprint(doc["examples"][0]["output"])


def test_planner_fewshot_blueprint_is_parseable():
    """Few-shot 里的蓝图必须能被 yaml.safe_load 解析，否则模型会照着错的模仿。"""
    course = _course()
    # 只断言不变量，不硬编码示例的具体数值（示例会变，契约不变）
    assert len(course["chapters"]) >= 2, "长周期方案至少要有两章才能体现门控"
    assert course["course"]["student_level"] in {"入门", "基础", "中等", "进阶", "竞赛"}
    assert course["course"]["subject"].startswith("初中")


def test_planner_never_pre_generates_content():
    """规划阶段所有单元必须为 pending，不得提前生成讲解内容。"""
    course = _course()
    statuses = {u["lesson_status"] for c in course["chapters"] for u in c["units"]}
    assert statuses == {"pending"}


def test_planner_prompt_has_no_elision_syntax():
    """模板骨架不得含省略写法（units: [...] 等），防模型原样抄出非法 YAML。"""
    prompt = lib.build("teacher_planner", {}, fewshot=False)
    assert "units: [...]" not in prompt
    assert "assessment: {...}" not in prompt


def test_gate_blocks_below_min_score():
    course = _course()
    low = {"ch01": {"rate": 0.58, "weak": ["配方法"]}}
    assert can_unlock(course, "ch01", low)[0] is True, "首章总是解锁"
    assert can_unlock(course, "ch02", low)[0] is False, "58% 未达 60%，应锁住"

    high = {"ch01": {"rate": 0.72, "weak": []}}
    assert can_unlock(course, "ch02", high)[0] is True, "72% 达标，应解锁"


def test_fewshot_header_forbids_content_copy():
    """拼接示例的表头必须区分「模仿格式」与「照搬内容」——否则模型会把示例当答案模板拄。"""
    prompt = lib.build("teacher_planner", {}, fewshot=True)
    assert "禁止照搬" in prompt


def test_gate_blocks_without_progress():
    course = _course()
    assert can_unlock(course, "ch02", {})[0] is False


def test_unit_prompt_fills_all_variables_from_blueprint():
    """M1 蓝图 → M2 变量的映射必须填满所有占位符。"""
    course = _course()
    ch2 = course["chapters"][1]
    progress = {
        "ch01": {
            "rate": 0.72,
            "weak": ["配方法"],
            "units_done": [u["id"] for u in course["chapters"][0]["units"]],
        }
    }
    prompt = build_unit_prompt(lib, course, ch2["id"], 0, progress)
    assert "正确率 72%" in prompt, "上一章成绩应注入 M2"
    assert ch2["units"][0]["title"] in prompt
    assert "{{" not in prompt, "M2 变量应全部填满，不得残留占位符"


def test_demo_course_flow_runs():
    """链路 demo 本身要能跑通（M1 蓝图 → 门控 → M2 组装）。"""
    import subprocess

    r = subprocess.run(
        [sys.executable, str(ROOT / "demo_course_flow.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert "已解锁" in r.stdout


def test_unit_variables_is_single_source_of_truth():
    """学情三档必须映射成不同的「上一章测验结果」，数据缺失要如实标无数据。"""
    course = _course()
    ch = course["chapters"][1]
    prev = course["chapters"][0]
    done = [u["id"] for u in prev["units"]]

    def vars_for(rate):
        prog = {} if rate is None else {prev["id"]: {"rate": rate, "weak": ["薄弱点A"], "units_done": done}}
        return unit_variables(course, ch["id"], 0, prog)

    assert vars_for(0.45)["上一章测验结果"] == "正确率 45%"
    assert vars_for(0.92)["上一章测验结果"] == "正确率 92%"
    assert vars_for(None)["上一章测验结果"] == "无数据"
    assert vars_for(None)["学情数据"] == "数据不足"
    assert vars_for(0.45)["前置薄弱点"] == "薄弱点A"
    # 首章无前置
    assert unit_variables(course, prev["id"], 0, {})["上一章测验结果"] == "无（首章）"


def _synthetic_unit_output(explanation: str) -> str:
    """拼一份结构完整但内容可控的 M2 输出，用于单独测某条断言。"""
    names = ["本单元定位", "难度调整说明", "本课目标", "知识讲解", "典型例题",
             "易错提醒", "小结", "课件大纲", "单元作业", "完成后解锁"]
    bodies = {
        "本单元定位": "第1章第1单元，本章共2个单元。",
        "难度调整说明": explanation,
        "本课目标": "学完能掌握本单元内容。",
        "知识讲解": "分步讲解。",
        "典型例题": "例题与解题过程。",
        "易错提醒": "注意常见错误。",
        "小结": "三句话总结。",
        "课件大纲": "P1 标题 — 要点\nP2 标题 — 要点\nP3 标题 — 要点",
        "单元作业": "1. 题一\n2. 题二\n3. 题三",
        "完成后解锁": "完成后开启下一单元。",
    }
    return "\n".join(f"{i}. 【{n}】{bodies[n]}" for i, n in enumerate(names, 1))


def test_unit_validator_catches_missing_sections():
    """校对器必须会报错，而不是永远“全绿”。"""
    case = {"name": "low", "band": "low", "rate": 0.45,
            "weak": ["薄弱点A"], "need_example": True}
    broken = "1. 【本单元定位】第1章。\n2. 【难度调整说明】根据学情调整。\n"
    failed = {n for n, ok, _ in validate_unit(broken, case) if not ok}
    assert "H4 十个模块齐全" in failed, "缺模块必须被报出"


def test_unit_validator_catches_vague_explanation():
    """结构完整但难度说明不引数据、只说「根据学情」，必须被判失败。"""
    case = {"name": "low", "band": "low", "rate": 0.45,
            "weak": ["薄弱点A"], "need_example": True}
    checks = validate_unit(_synthetic_unit_output("根据学情，适当调整本单元难度。"), case)
    failed = {n for n, ok, _ in checks if not ok}
    assert "H2 无「根据学情」空话" in failed, "空话必须被报出"
    assert "H2 说明引用真实成绩" in failed, "未引 45% 必须被报出"


def test_unit_validator_passes_a_good_output():
    """合规输出必须全绿——否则校对器太严，会制造假阳性。"""
    case = {"name": "low", "band": "low", "rate": 0.45,
            "weak": ["薄弱点A"], "need_example": True}
    good = _synthetic_unit_output("依据 ch01 章节测验正确率 45%（<60%），执行降档，先补 薄弱点A。")
    failed = [n for n, ok, d in validate_unit(good, case) if not ok]
    assert not failed, f"合规输出不应被判失败：{failed}"


def test_section_parser_preserves_order():
    order, bodies = split_sections("1. 【A】x\n2. 【B】y\n3. 【C】z")
    assert order == ["A", "B", "C"]
    assert bodies["B"] == "y"


def _synthetic_sprint(path_rows: str) -> str:
    """拼一份结构完整的 M3 输出，用时表可注入。"""
    return f"""【速成目标】3 天内掌握核心方法。主动舍弃：高阶题型。

【学习路径】
| 序号 | 里程碑 | 用时 | 学什么 | 练什么 |
|---|---|---|---|---|
{path_rows}

【核心讲解】
M1 先认清三个量，再代公式。
M2 串联电路电流处处相等。
M3 固定四步走，最后用总压验算。

【随堂练习】
M1-1 题一。
【答案】答一。【要点】点一。
M1-2 题二。
【答案】答二。【要点】点二。
M2-1 题三。
【答案】答三。【要点】点三。
M2-2 题四。
【答案】答四。【要点】点四。
M3-1 题五。
【答案】答五。【要点】点五。
M3-2 题六。
【答案】答六。【要点】点六。

【结束测验】
1. 测一。
【答案】答。【解析】解。
2. 测二。
【答案】答。【解析】解。
3. 测三。
【答案】答。【解析】解。
4. 测四。
【答案】答。【解析】解。
5. 测五。
【答案】答。【解析】解。
【自评标准】对 4 题以上算过关。

【时间提醒】进度落后时优先保住 M1 与 M2——列不出式子，算得再快也没用。"""


SPRINT_CASE = {"limit_days": 3.0}


def test_sprint_duration_parser_uses_learning_day():
    """「1天」必须按学习日（3 小时）而非 24 小时折算，否则 8 小时内容会被放过。"""
    assert count_duration_hours("1天")[0] == 3.0
    assert count_duration_hours("1 天")[0] == 3.0, "带空格也要能解析"
    assert count_duration_hours("半天")[0] == 1.5
    assert count_duration_hours("3小时")[0] == 3.0
    assert count_duration_hours("1.5天")[0] == 4.5


def test_sprint_validator_passes_a_good_output():
    """合规输出必须全绿，否则校对器太严会造假阳性。"""
    rows = ("| M1 | 认量 | 1.5小时 | 学 | 练 |\n"
            "| M2 | 串联 | 1.5小时 | 学 | 练 |\n"
            "| M3 | 综合 | 1.5小时 | 学 | 练 |")
    failed = [n for n, ok, _ in validate_sprint(_synthetic_sprint(rows), SPRINT_CASE) if not ok]
    assert not failed, f"合规输出不应被判失败：{failed}"


def test_sprint_validator_catches_over_budget():
    """3 天 = 9 小时；排到 18 小时必须被判超时限。"""
    rows = ("| M1 | 认量 | 6小时 | 学 | 练 |\n"
            "| M2 | 串联 | 6小时 | 学 | 练 |\n"
            "| M3 | 综合 | 6小时 | 学 | 练 |")
    failed = {n for n, ok, _ in validate_sprint(_synthetic_sprint(rows), SPRINT_CASE) if not ok}
    assert "H3 里程碑总用时 ≤ 时限" in failed


def test_sprint_validator_catches_day_units():
    """口径要求用时列写小时，不写「天」。"""
    rows = ("| M1 | 认量 | 1天 | 学 | 练 |\n"
            "| M2 | 串联 | 1天 | 学 | 练 |\n"
            "| M3 | 综合 | 1天 | 学 | 练 |")
    failed = {n for n, ok, _ in validate_sprint(_synthetic_sprint(rows), SPRINT_CASE) if not ok}
    assert "时限口径：用时列不写「天」" in failed


def test_sprint_validator_catches_review_and_gate():
    """两条反向禁令必须可检出。"""
    rows = ("| M1 | 认量 | 1.5小时 | 学 | 练 |\n"
            "| M2 | 串联 | 1.5小时 | 学 | 练 |\n"
            "| M3 | 综合 | 1.5小时 | 学 | 练 |")
    with_review = _synthetic_sprint(rows).replace("【时间提醒】", "【时间提醒】安排复习环节巩固 M1。")
    failed = {n for n, ok, _ in validate_sprint(with_review, SPRINT_CASE) if not ok}
    assert "H1 无复习节点" in failed

    with_gate = _synthetic_sprint(rows).replace("【时间提醒】", "【时间提醒】学完 M1 才能解锁 M2。")
    failed = {n for n, ok, _ in validate_sprint(with_gate, SPRINT_CASE) if not ok}
    assert "H2 无解锁/门控语义" in failed


def test_sprint_fewshot_is_a_skeleton():
    """sprint 的 Few-shot 是「骨架示例」：数量具体、内容全为占位符。

    这是为消除「同题撞车抄袭」而做的设计决策（完整实例实测被搬走 18.3 处、
    最长 105 字，且重试闸门救不回来）。所以它**不是**一份合格输出，
    不能拿内容校验器去验它——只能验它「真的是骨架」。"""
    doc = yaml.safe_load((ROOT / "fewshots" / "teacher_sprint.yaml").read_text(encoding="utf-8"))
    ref = doc["examples"][0]["output"]

    ph = re.findall(r"<[^>]{1,24}>", ref)
    assert len(set(ph)) >= 15, f"占位符仅 {len(set(ph))} 种，可能混入了完整实例内容"
    assert "..." not in ref, "骨架示例不得用省略写法（会被原样抄出）"

    order, _ = split_sprint_sections(ref)
    missing = [x for x in ["速成目标", "学习路径", "核心讲解", "随堂练习", "结束测验", "时间提醒"]
               if x not in order]
    assert not missing, f"骨架示例缺模块：{missing}"

    # 数量约束仍须被示范：真列 3 个里程碑、每里程碑 2 题、5 道测验题
    assert len(re.findall(r"^\s*\|\s*M\d", ref, re.M)) == 3, "骨架应示范 3 个里程碑"
    per = {}
    for a, b in re.findall(r"^\s*M\s*(\d+)\s*[-–—]\s*(\d+)", ref, re.M):
        per.setdefault(int(a), []).append(int(b))
    assert per and all(len(v) == 2 for v in per.values()), f"骨架应示范每里程碑 2 题，实际 {per}"
    assert len(re.findall(r"^\s*\d+\s*[.、]", ref.split("【结束测验】")[1], re.M)) == 5,         "骨架应示范 5 道测验题"


def test_skeleton_fewshot_has_nothing_to_copy():
    """骨架示例里必须没有可供照抄的实质内容：与自身做照抄检测时，
    去掉结构性外壳后不应留下长片段。"""
    doc = yaml.safe_load((ROOT / "fewshots" / "teacher_sprint.yaml").read_text(encoding="utf-8"))
    ref = doc["examples"][0]["output"]
    # 阈值依据实测：骨架剔除外壳后最长残渣 42 字（纯脚手架），
    # 而具体示例为 151~1408 字。取 60 作分界，两边都有余量。
    hits = copy_hits(ref, [ref])
    assert all(len(h) < 60 for h in hits), f"骨架里存在过长可抄片段：{[h[:60] for h in hits]}"


def test_copy_detector_still_catches_concrete_fewshots():
    """防止检测器「改到失效」：具体示例必须仍被报出大段重合。"""
    for name in ("teacher", "teacher_planner"):
        refs = load_fewshot_refs(lib, name)
        hits = copy_hits(refs[0], refs)
        assert hits and max(len(h) for h in hits) > 120,             f"{name} 的具体示例未被检出，检测器可能已失效"


def test_unknown_role_raises():
    try:
        lib.build("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("未知角色应抛出 KeyError")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"[PASS] {fn.__name__}")
    print(f"\n全部 {len(tests)} 项测试通过 ✅")


# ---------- 质量闸门：照抄检测 ----------
def test_copy_detector_flags_self_and_ignores_unrelated():
    """检测器必须能报出真照抄，也不能碰瓷无关文本。"""
    refs = load_fewshot_refs(lib, "teacher_sprint")
    assert refs, "应能读到 Few-shot 参考文本"
    assert copy_hits(refs[0], refs), "自我对照必须报出重合"
    unrelated = "学生在实验中观察到，光照强度增大时气泡产生速率随之上升，说明光合作用速率与光照强度正相关。"
    assert not copy_hits(unrelated, refs), "无关文本不得误报"


def test_copy_detector_strips_template_scaffolding():
    """模板强制的表格与分节标题必须被剔除——否则闸门会把正确的格式判成抄袭，
    永远不收敛（过严的闸门比没有闸门更糟）。"""
    text = "| 序号 | 里程碑 | 用时 | 学什么 | 练什么 |" + chr(10) + "|---|---|---|---|---|" + chr(10) + "【学习路径】" + chr(10) + "实质内容"
    stripped = strip_scaffolding(text)
    assert "|" not in stripped, "表格行应被剔除"
    assert "【学习路径】" not in stripped, "分节标题应被剔除"
    assert "实质内容" in stripped, "实质内容必须保留"


def test_copy_detector_catches_number_swapped_paraphrase():
    """「换数字式抄袭」：句子骨架照搬、只改数字，也必须能检出。"""
    ref = "M1 设未知数：读完题先圈出问句里要求的那个量，直接设它为 x，单位一并写清，不要设中间量。"
    swapped = "M1 认量：读完题先圈出问句里要求的那个量，直接设它为 y，单位一并写清，不要设中间量。"
    assert copy_hits(swapped, [ref]), "换数字抄袭应被检出"


def test_blank_inputs_blocks_missing_required_variables():
    """入口预检必须拦下空值输入。

    实测：把「学生答案」留空时，模型会拿旁边的「参考答案」当学生答案，
    给出「10/10、判定对、置信度高」——学生没作答却拿满分。这个不能交给模型兜。
    """
    base = {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
            "薄弱点候选列表": "浮力", "错题": "题", "学生答案": "答",
            "参考答案": "参", "本题满分": "5"}
    assert blank_inputs(lib, "assistant", base) == []
    assert blank_inputs(lib, "assistant", {**base, "学生答案": ""}) == ["学生答案"]
    assert blank_inputs(lib, "assistant", {**base, "错题": "   "}) == ["错题"]
    # 完全不传也要能识别
    assert "参考答案" in blank_inputs(lib, "assistant", {})


def test_require_inputs_raises_before_calling_model():
    """入口预检必须是硬拒绝：缺项抛错，而不是把空白交给模型。

    助教路径已证明模型会用旁边的参考答案填空白并给满分；链路里必须用同一个
    确定性行为，而不是每个调用点各写一遍校验。
    """
    base = {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
            "薄弱点候选列表": "浮力", "错题": "题", "学生答案": "答",
            "参考答案": "参", "本题满分": "5"}
    assert require_inputs(lib, "assistant", base) is base, "齐全时应原样返回变量表"
    try:
        require_inputs(lib, "assistant", {**base, "学生答案": "  "})
    except ValueError as exc:
        assert "学生答案" in str(exc), f"错误信息应指明缺哪项，实际：{exc}"
    else:
        raise AssertionError("缺「学生答案」必须抛 ValueError")


def test_input_contract_declares_unit_for_every_numeric_field():
    """数值/时间字段必须声明类型与单位。

    链路传的是「4周」「3课时」这种带单位字符串，所以契约锁的是单位，
    而不是要求裸数字——否则会把正在工作的调用点误杀。
    """
    expect = {
        "teacher_planner": [("总周期", "周"), ("每周可投入时间", "课时")],
        "teacher_sprint": [("可用时限", "天")],
        "teacher_unit": [("本单元课时", "课时")],
    }
    for role, fields in expect.items():
        spec = lib.input_spec(role)
        for name, unit in fields:
            assert spec.get(name, {}).get("type") == "quantity", f"{role}.{name} 应声明 quantity"
            assert spec[name].get("unit") == unit, f"{role}.{name} 单位应为 {unit}"


def test_quantity_rejects_free_text_that_used_to_fail_silently():
    """「每周三小时」是实测的静默失效源：乘法拿不到数字，模型只能自己猜。

    没有任何报错，只是结果惄惄变差——所以必须在入口阻断。
    """
    variables = {"学科": "初中数学", "学习目标": "掌握一元二次方程",
                 "总周期": "6周", "每周可投入时间": "每周三小时", "学生水平": "基础"}
    problems = invalid_inputs(lib, "teacher_planner", variables)
    assert [p.name for p in problems] == ["每周可投入时间"], f"应只报该项，实际 {problems}"
    assert "课时" in problems[0].detail, "报错要指明期望单位"
    try:
        require_inputs(lib, "teacher_planner", variables)
    except ValueError as exc:
        assert "每周可投入时间" in str(exc)
    else:
        raise AssertionError("自由文本必须阻断，不得交给模型")


def test_quantity_rejects_wrong_unit():
    """「4周」填进「每周可投入时间」——数字对、单位错，同样必须拦。"""
    variables = {"学科": "初中数学", "学习目标": "x", "总周期": "6周",
                 "每周可投入时间": "4周", "学生水平": "基础"}
    problems = invalid_inputs(lib, "teacher_planner", variables)
    assert [p.name for p in problems] == ["每周可投入时间"]
    assert "单位应为" in problems[0].detail


def test_quantity_accepts_both_unit_forms():
    """带单位与裸数字都放行：单位已在模板里声明，裸数字不含歧义。"""
    for value in ("4周", "4 周", "4"):
        variables = {"学科": "初中数学", "学习目标": "x", "总周期": value,
                     "每周可投入时间": "3课时", "学生水平": "基础"}
        assert invalid_inputs(lib, "teacher_planner", variables) == [], f"{value!r} 应放行"


def test_percent_field_accepts_declared_sentinel():
    """「上一章测验结果」驱动数值分支（≥85% 升档），缺数据态必须显式声明。"""
    base = unit_variables(_course(), _course()["chapters"][0]["id"], 0, {})
    assert invalid_inputs(lib, "teacher_unit", base) == [], f"链路自产变量应合规：{base}"

    # 无前置章节 / 有章节未考：两个声明的哨兵值都必须放行
    for sentinel in ("无（首章）", "无数据"):
        assert invalid_inputs(lib, "teacher_unit", {**base, "上一章测验结果": sentinel}) == []

    problems = invalid_inputs(lib, "teacher_unit", {**base, "上一章测验结果": "还行"})
    assert [p.name for p in problems] == ["上一章测验结果"], "自由评语不得放行"


def test_invalid_inputs_does_not_duplicate_blank_inputs():
    """两层预检职责不重叠：缺项归 blank_inputs，格式不符归 invalid_inputs。"""
    base = {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
            "薄弱点候选列表": "浮力", "错题": "题", "学生答案": "",
            "参考答案": "参", "本题满分": "5"}
    assert blank_inputs(lib, "assistant", base) == ["学生答案"]
    assert invalid_inputs(lib, "assistant", base) == [], "缺项不应在类型层重复报"

    filled = {**base, "学生答案": "答", "学生水平": "大三"}
    assert blank_inputs(lib, "assistant", filled) == []
    assert [p.name for p in invalid_inputs(lib, "assistant", filled)] == ["学生水平"]


def test_assistant_score_denominator_comes_from_input():
    """满分必须由调用方传入，不能由模型自编。

    原版要求输出「得分 / 满分」却从不把满分传进来，分母是模型编的；
    评测又只校验「得分 == 自编满分」，所以这个缺陷一直没暴露——
    后果是分数不可比、不可聚合，学生成绩统计完全失效。
    """
    spec = lib.input_spec("assistant")
    assert spec.get("本题满分", {}).get("type") == "number"
    assert spec["本题满分"].get("min") == 1

    prompt = lib.build("assistant", {}, fewshot=False)
    assert "{{本题满分}}" in prompt
    # 分母的唯一来源必须写死在模板里，而不是靠模型猜「看起来像 10 分题」
    assert "分母必须等于输入的{{本题满分}}" in prompt

    base = {"学科": "初中物理", "学生姓名": "小刚", "学生水平": "基础",
            "薄弱点候选列表": "浮力", "错题": "题", "学生答案": "答", "参考答案": "参"}
    assert "本题满分" in blank_inputs(lib, "assistant", base), "漏传满分必须被拦下"
    for bad in ("0", "-3", "五分"):
        problems = invalid_inputs(lib, "assistant", {**base, "本题满分": bad})
        assert [p.name for p in problems] == ["本题满分"], f"{bad!r} 应被拦下"


def test_assistant_fewshot_input_contains_no_answer_hint():
    """示例输入里不得出现「漏掉 x = 3」这类答案提示。

    旧版把它写在 input 里（本是给评测者看的注释），等于在示例中泄题：
    模型不需判断就能把提示抄进输出，还被教会去输入里找括号提示——
    而那正是照抄行为的温床。
    """
    doc = yaml.safe_load((ROOT / "fewshots" / "assistant.yaml").read_text(encoding="utf-8"))
    assert doc["examples"], "示例不能为空"
    for ex in doc["examples"]:
        text = ex.get("input", "")
        for hint in ("漏掉", "漏了", "实际答成", "正确答案是", "错误在"):
            assert hint not in text, f"示例输入含答案提示「{hint}」：{text!r}"
        for verdict in ("部分正确", "得分", "判定"):
            assert verdict not in text, f"示例输入不得预设判定「{verdict}」：{text!r}"
        # 三要素与满分都必须在输入里
        for need in ("题目", "学生答案", "参考答案", "本题满分"):
            assert need in text, f"示例输入缺「{need}」"


def test_every_student_level_has_a_model_eval_case():
    """取值域里的每一档都必须有模型评测用例。

    上轮我只给 teacher 补了「中等」分支，却没补对应用例：结构性测试只能证明
    分支存在，证明不了它行为生效。这条断言把「漏一档」变成会报错的硬缺口。
    """
    from eval_teacher import BANDS as EVAL_BANDS, CASES as TEACHER_CASES

    levels = lib.input_spec("teacher")["学生水平"]["values"]
    assert sorted(EVAL_BANDS) == sorted(levels), \
        f"评测档位 {EVAL_BANDS} 与契约取值域 {levels} 不一致"
    covered = {c["band"] for c in TEACHER_CASES.values()}
    missing = set(levels) - covered
    assert not missing, f"这些档位没有模型评测用例：{sorted(missing)}"


def test_live_path_inputs_satisfy_contract():
    """链路自产的变量表必须天然合规——否则闸门会把正常请求拦下来。

    这是「契约不能过严」的回归线：M1/M2 是唯一真正在跑的两条路径。
    """
    planner = {"学科": "初中数学", "学习目标": "掌握一元二次方程的解法",
               "总周期": "4周", "每周可投入时间": "4课时", "学生水平": "基础"}
    assert require_inputs(lib, "teacher_planner", planner) is planner

    course = _course()
    unit = unit_variables(course, course["chapters"][0]["id"], 0, {})
    assert require_inputs(lib, "teacher_unit", unit) is unit


def test_quantity_rejects_non_positive_and_out_of_range():
    """旧实现只验「数字+单位」，-3周 / 0周 / 99999周 全部放行。"""
    base = {"学科": "初中数学", "学习目标": "x", "每周可投入时间": "3课时", "学生水平": "基础"}
    for value, keyword in (("-3周", "正数"), ("0周", "正数"), ("17周", "必须小于")):
        problems = invalid_inputs(lib, "teacher_planner", {**base, "总周期": value})
        assert [p.name for p in problems] == ["总周期"], f"{value!r} 必须被拦"
        assert keyword in problems[0].detail, f"{value!r} 的报错应含「{keyword}」"
    for value in ("4周", "3.5周", "15周"):   # 边界与常规值必须放行
        assert invalid_inputs(lib, "teacher_planner", {**base, "总周期": value}) == [], \
            f"{value!r} 不应被误杀"


def test_planner_guardrails_follow_product_spec():
    """产品口径：总周期必须「小于」16 周，每周可投入不超过 20。

    开/闭区间的差别在这里是实质的：若写成 max: 16，16 周本身会被放行，
    而口径说的是「小于」16 周。
    """
    base = {"学科": "初中数学", "学习目标": "x", "每周可投入时间": "4课时", "学生水平": "基础"}
    for value in ("1周", "6周", "15周", "15.5周"):
        assert invalid_inputs(lib, "teacher_planner", {**base, "总周期": value}) == [], \
            f"{value!r} 应放行"
    for value in ("16周", "17周"):
        problems = invalid_inputs(lib, "teacher_planner", {**base, "总周期": value})
        assert [p.name for p in problems] == ["总周期"], f"{value!r} 必须被拦"
        assert "必须小于 16周" in problems[0].detail, problems[0].detail

    base = {"学科": "初中数学", "学习目标": "x", "总周期": "6周", "学生水平": "基础"}
    # 上限 24 课时 = 20 小时（1 课时 = 50 分钟）
    assert invalid_inputs(lib, "teacher_planner", {**base, "每周可投入时间": "24课时"}) == []
    problems = invalid_inputs(lib, "teacher_planner", {**base, "每周可投入时间": "25课时"})
    assert [p.name for p in problems] == ["每周可投入时间"]
    assert "不得大于 24课时" in problems[0].detail, problems[0].detail

    # 单位本身也必须写进口径：契约里的「课时」不能是个未定义词
    prompt = lib.build("teacher_planner", {}, fewshot=False)
    assert "1 课时 = 50 分钟" in prompt, "课时计量口径必须定义「课时」是什么"


def test_percent_rejects_free_text_containing_percent_sign():
    """旧实现用 search() 捞 %，于是「下降了50%」这类自由文本能穿过预检。

    而它的下游是「正确率 ≥ 85% → 升档」的数值分支，穿过去就是静默错判。
    """
    course = _course()
    base = unit_variables(course, course["chapters"][0]["id"], 0, {})
    for value in ("下降了50%", "abc 99% def", "1000%", "-20%", "还行"):
        problems = invalid_inputs(lib, "teacher_unit", {**base, "上一章测验结果": value})
        assert [p.name for p in problems] == ["上一章测验结果"], f"{value!r} 必须被拦"
    for value in ("正确率 85%", "85%", "正确率 0%", "无（首章）", "无数据"):
        assert invalid_inputs(lib, "teacher_unit", {**base, "上一章测验结果": value}) == [], \
            f"{value!r} 应放行"


def test_unit_hours_range_matches_planner_measurement_rule():
    """本单元课时的 0.5~3 抄自 teacher_planner 的「课时计量口径」第 1 条。

    两处口径必须同源，否则蓝图能产出一个单元渲染阶段拒绝的课时。
    """
    course = _course()
    base = unit_variables(course, course["chapters"][0]["id"], 0, {})
    for value in ("0.5", "2", "3"):
        assert invalid_inputs(lib, "teacher_unit", {**base, "本单元课时": value}) == []
    for value in ("0", "0.4", "3.5", "8"):
        problems = invalid_inputs(lib, "teacher_unit", {**base, "本单元课时": value})
        assert [p.name for p in problems] == ["本单元课时"], f"{value!r} 应被拦"


def test_every_student_level_has_a_branch_in_teacher():
    """契约声明的取值域必须与模板分支一一对应。

    这正是早先修掉的缺陷：「中等」在取值域里合法、在分支里不存在，
    传入后不命中任何分支且不报错。这条测试防止二者再次漂移。
    分支形式在 2026-09-28 由 `IF {{学生水平}} == "x"` 改为
    “值陈述 + 按档位列举”（`入门档：……`）——后者填入变量后不会变成字面量自比。
    """
    spec = lib.input_spec("teacher")["学生水平"]["values"]
    prompt = lib.build("teacher", {}, fewshot=False)
    for level in spec:
        assert f"{level}档：" in prompt, f"「{level}」缺少对应档位"
    branches = re.findall(r"^([^\s：]+)档：", prompt, re.M)
    assert sorted(branches) == sorted(spec), f"档位 {branches} 与契约 {spec} 不一致"


# ---------------------------------------------------------- 生产代码闸门
# 判定「生产文件」的方式是**默认生产、按名豁免**：新增一个 xxx_chain.py 会被
# 自动当作生产代码检查，而不是静默逃过。豁免的只有：
#   eval_*.py / probe_*.py  评测器与探针——本来就要构造缺输入、坏单位这类边界用例
#   test_*.py               测试自身
#   prompt_builder.py       模板库本体；它不能 import quality_gate（会循环依赖），
#                           因此「调用前先过预检」这条规则对它不适用
_EXEMPT_PREFIXES = ("eval_", "probe_", "test_")
_EXEMPT_NAMES = {"prompt_builder.py"}
_GATE_FUNCS = {"require_inputs", "blank_inputs", "invalid_inputs", "check_inputs"}


def _is_build(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "build"


def _own_scope(node):
    """遍历 node 自身作用域，不进嵌套函数/闭包（否则内层的预检会被当成外层的）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _own_scope(child)


def _ungated_build_calls(path: Path) -> list[str]:
    """返回文件里「未经预检就调用 .build()」的位置描述（空列表 = 合规）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in _own_scope(node) if isinstance(n, ast.Call)]
        builds = sorted(c.lineno for c in calls if _is_build(c))
        if not builds:
            continue
        gates = [c.lineno for c in calls
                 if isinstance(c.func, ast.Name) and c.func.id in _GATE_FUNCS]
        # 预检必须写在第一处 .build() 之前；写在后面等于没写
        if not any(g < builds[0] for g in gates):
            found.append(f"{node.name}() 第 {builds[0]} 行")
    # 模块级的 .build() 无法可靠地「先过预检」，一律报出
    for call in (n for n in _own_scope(tree) if isinstance(n, ast.Call) and _is_build(n)):
        found.append(f"模块级第 {call.lineno} 行")
    return found


def test_production_code_never_builds_without_precheck():
    """生产代码里每一处 .build() 都必须先过入口预检。

    为什么需要这条：require_inputs 是**可选的函数**，不是框架行为。demo 记得加，
    不代表下周新增的调用点会记得加——忘了的后果是静默回到「6周 × 每周三小时」
    那种无告警的坏 prompt。判据用 AST 而非文本匹配，避免注释与字符串干扰。
    """
    offenders = {}
    checked = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name in _EXEMPT_NAMES or path.name.startswith(_EXEMPT_PREFIXES):
            continue
        checked.append(path.name)
        bad = _ungated_build_calls(path)
        if bad:
            offenders[path.name] = bad
    assert checked, "生产文件集合为空，豁免规则可能写错了"
    assert not offenders, (
        f"以下生产代码绕过了入口预检（应走 demo_course_flow 里的封装，"
        f"或在 .build() 之前调用 require_inputs）：{offenders}"
    )
    print(f"[OK] 生产文件 {checked} 的 .build() 调用点均已过预检")


def test_ungated_build_detector_actually_detects():
    """上面那条测试的检测器自测——没有它，lint 可能是永远通过的假绿。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bad = tmp / "bad.py"
        bad.write_text("def f(lib, v):\n    return lib.build('teacher', v)\n", encoding="utf-8")
        good = tmp / "good.py"
        good.write_text("def f(lib, v):\n    require_inputs(lib, 'teacher', v)\n"
                        "    return lib.build('teacher', v)\n", encoding="utf-8")
        late = tmp / "late.py"
        late.write_text("def f(lib, v):\n    p = lib.build('teacher', v)\n"
                        "    require_inputs(lib, 'teacher', v)\n    return p\n", encoding="utf-8")
        module_level = tmp / "module.py"
        module_level.write_text("PROMPT = lib.build('teacher', {})\n", encoding="utf-8")

        assert _ungated_build_calls(bad), "无预检必须报出"
        assert not _ungated_build_calls(good), "预检写在 build 之前应放行"
        assert _ungated_build_calls(late), "预检写在 build 之后不算数"
        assert _ungated_build_calls(module_level), "模块级 build 必须报出"


def test_live_chain_wrappers_reject_missing_input():
    """生产封装（build_unit_prompt / build_planner_prompt）缺输入时必须抛错。

    覆盖范围仅这两条链路；「生产代码里所有 .build() 都过了预检」由
    test_production_code_never_builds_without_precheck 负责。
    """
    course = _course()
    course["course"]["subject"] = ""          # 模拟后端漏传学科
    try:
        build_unit_prompt(lib, course, course["chapters"][0]["id"], 0)
    except ValueError as exc:
        assert "学科" in str(exc), f"应报出学科缺失，实际：{exc}"
    else:
        raise AssertionError("M2 缺学科必须抛错，不得把空值交给模型")

    try:
        build_planner_prompt(lib, {"学科": "初中数学", "学习目标": "解一元二次方程",
                                   "总周期": "4周", "每周可投入时间": "4课时",
                                   "学生水平": ""})
    except ValueError as exc:
        assert "学生水平" in str(exc), f"应报出学生水平缺失，实际：{exc}"
    else:
        raise AssertionError("M1 缺学生水平必须抛错")


def test_quality_gate_reports_failure_without_raising():
    """闸门在校验不过时必须返回结果对象，而不是抛异常或静默通过。"""
    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class M:
                        content = "【速成目标】随便写点东西，不带任何要求的结构。"
                    class C:
                        message = M()
                    class R:
                        choices = [C()]
                    return R()

    gate = QualityGate(FakeClient(), "fake-model", max_retry=1)
    result = gate.generate("x", checks=lambda r: [("必须有结构", "【速成目标】" in r and "【结束测验】" in r, "缺结构")])
    assert not result.passed, "不合规输出不得判为通过"
    assert result.attempts == 2, "max_retry=1 应共尝试 2 次"
    assert result.failures, "应能列出未通过项"


def test_humanities_cot_snippets_exist_and_concat():
    """文科 CoT：原版 3 个片段里 2 个是数学专用，文科无可用的分步指令。

    新增 4 个后，必须保证：名字出现在 list_cot 里，且能真的拼进 Prompt。
    """
    for name in ("text_reading", "source_analysis", "essay_outline", "self_check_humanities"):
        assert name in lib.list_cot(), f"缺文科 CoT 片段 {name}"
        prompt = lib.build("qa", {}, cot=name)
        assert lib.cot[name].strip().splitlines()[0][:12] in prompt, f"{name} 未拼进 Prompt"


def test_humanities_cot_has_no_math_requirements():
    """文科 CoT 不得要求「代入检验/特殊值检验」这类数学动作，否则文科用不了。"""
    for name in ("text_reading", "source_analysis", "essay_outline", "self_check_humanities"):
        body = lib.cot[name]
        for word in ("代入检验", "特殊值", "LaTeX", "乘号", "幂写"):
            assert word not in body, f"{name} 混入了理科专用要求：{word}"


def test_math_cot_still_intact():
    """回归：新增文科片段不得改动原有数学片段。"""
    assert "【数学符号规范】" in lib.cot["math_steps"]
    assert "第 1 步【审题】" in lib.cot["math_steps"]
    assert "代入检验" in lib.cot["self_check"]


def test_essay_role_builds_and_validates():
    """作文/主观题批改：新角色的变量能填满、预检能通过。"""
    v = {"学科": "初中语文", "学生姓名": "小林", "学生水平": "中等",
         "题目": "以「慢下来」为题写一篇议论文", "学生作文": "（正文）",
         "字数要求": "不少于 600 字",
         "评分维度": "立意与思想 10、结构与条理 10、语言与表达 10、素材与论证 10",
         "总分": 40}
    assert not lib.missing_vars("assistant_essay", v), "有变量未声明"
    assert not lib.check_inputs("assistant_essay", v), "合规输入不应报错"
    prompt = lib.build("assistant_essay", v, fewshot=True)
    assert "{{" not in prompt
    assert "（正文）" in prompt, "必须把学生作文传进 prompt"
    assert "立意与思想 10" in prompt, "必须把评分维度传进 prompt"
    for sec in ESSAY_SECTIONS:
        assert f"【{sec}】" in prompt, f"输出格式缺模块 {sec}"


def test_essay_template_is_subjective_not_objective():
    """作文模板不得退回客观题评分口径（对/错 + 分子分母）。

    主观题没有唯一答案，需要的是「分维度 + 等级」，把两种评分模型混在一个模板里
    会让输出格式互相打架——这正是单开 assistant_essay 的原因。
    """
    prompt = lib.build("assistant_essay", {}, fewshot=False)
    assert "【维度得分】" in prompt and "【等级】" in prompt
    assert "【答案判定】" not in prompt, "主观题模板不应出现「答案判定」"
    assert "一类文" in prompt and "四类文" in prompt, "缺少等级及其判据"


def test_essay_total_is_input_contract():
    """【总分】的分母只能来自输入，模型不得自编（与 assistant 的本题满分同一约定）。"""
    spec = lib.input_spec("assistant_essay")
    assert spec.get("总分", {}).get("type") == "number"
    bad = lib.check_inputs("assistant_essay", {"总分": "还行"})
    assert any(p.name == "总分" for p in bad), "非数值的总分应被入口预检拦下"


def test_qa_and_supervisor_use_sections():
    """qa / supervisor 已从旧式 system 重写为 sections。

    旧结构只认 7 个固定字段，多写的自定义小节会被**静默丢弃**，
    所以【输出纪律】这类小节只能存在于新式结构里。
    """
    for role in ("qa", "supervisor"):
        t = lib.templates[role]
        assert t.get("sections"), f"{role} 应使用 sections 结构"
        assert "system" not in t, f"{role} 不应再保留 system 字段"
        prompt = lib.build(role, {}, fewshot=False)
        assert "【输出纪律】" in prompt, f"{role} 缺输出纪律小节"
        assert "【输出前自检】" in prompt, f"{role} 缺输出前自检"


def test_no_template_uses_legacy_format():
    """模板库里已无角色使用旧式 system 结构（唯一一个 system_base 已归档到 _archive/）。

    归档理由：它不被任何代码/测试/评测引用（死文件），却能让人以为
    “库里 7 个字段的旧结构还在用”。但**兼容性并未取消**——见下一个测试。
    """
    legacy = [r for r in lib.list_roles() if not lib.templates[r].get("sections")]
    assert legacy == [], f"仍有角色使用旧式 system 结构：{legacy}"
    assert (ROOT / "_archive" / "system_base.yaml").exists(), "归档文件不应被删掉"


def test_legacy_system_format_still_supported():
    """旧式 system 结构仍受支持：把归档文件放进最小目录，应能正常构建。

    这条测试同时验证了“最小模板目录”可用（只需 templates/，不需要 cot/）。
    """
    legacy = (ROOT / "_archive" / "system_base.yaml").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "templates").mkdir()
        (root / "templates" / "legacy.yaml").write_text(legacy, encoding="utf-8")
        lib2 = PromptLib(root)
        assert lib2.list_roles() == ["legacy"]
        prompt = lib2.build("legacy", {"角色描述": "初中数学教师", "一句话说明核心任务": "讲清概念"})
    for sec in ("【角色设定】", "【你的目标】", "【能力边界】", "【对话对象】",
                "【语气与风格】", "【输出格式】"):
        assert sec in prompt, f"旧结构渲染丢了小节：{sec}"
    assert "初中数学教师" in prompt


def test_qa_rewrite_keeps_original_content():
    """重写不得丢内容：qa 原七个字段的内容必须逐项还在。"""
    p = lib.build("qa", {"学科": "初中数学", "学生水平": "基础",
                          "知识点链": "因式分解 → 求根"}, fewshot=False)
    for key in ("负责回答学生的提问", "教会", "告诉答案", "切换讲解深度",
                "强行讲超纲内容", "当前水平为基础档", "鼓励学生思考"):
        assert key in p, f"qa 重写后丢了：{key}"
    for sec in ("【思路引导】", "【分步讲解】", "【分层适配】", "【同类题】", "【知识点链接】"):
        assert sec in p, f"qa 重写后丢了输出模块：{sec}"
    assert "第五部分" not in p, "qa 原有一处失效引用（见第五部分）应已清除"


def test_supervisor_rewrite_keeps_original_content():
    """重写不得丢内容：supervisor 原七个字段的内容必须逐项还在。"""
    p = lib.build("supervisor", {"学生姓名": "小明", "学生水平": "基础",
                                  "学习数据": "45 道/62%", "待复习知识点": "整式乘法"},
                  fewshot=False)
    for key in ("温暖耐心的学习督学", "用对话的方式提醒", "庆祝进步", "责骂、威胁",
                "泄露学生隐私", "违背其意愿", "我们一起来"):
        assert key in p, f"supervisor 重写后丢了：{key}"
    for sec in ("【一句话问候】", "【数据回顾】", "【今日任务】", "【激励语】", "【复习提醒】"):
        assert sec in p, f"supervisor 重写后丢了输出模块：{sec}"


def test_teacher_fewshot_covers_humanities():
    """范文集必须含文科示例，且文科示例里不得出现理科符号。"""
    exs = lib.fewshots["teacher"].get("examples", [])
    assert len(exs) >= 3, "范文集仍应保留原有数学示例"
    human = [e for e in exs if "作用" in e["input"] or "环境描写" in e["input"]]
    assert human, "范文集缺文科示例（原版 5 份范文全为理科）"
    out = human[0]["output"]
    for sym in ("公式", "×", "÷", "x²", "=", "定理"):
        assert sym not in out, f"文科范文里不应出现理科记号：{sym}"


def test_planner_eval_has_humanities_case():
    """评测用例必须含文科，否则文科路径永远不被验证。"""
    from eval_planner import CASES
    assert "chinese" in CASES, "eval_planner 缺文科用例"
    for key in ("学科", "学习目标", "总周期", "每周可投入时间", "学生水平"):
        assert key in CASES["chinese"], f"文科用例缺字段 {key}"
    assert "语文" in CASES["chinese"]["学科"]


def test_essay_validator_catches_wrong_arithmetic():
    """作文校验器必须能抓出「各维得分之和 ≠ 总分」——主观题最常见的对不上账。"""
    case = {"总分": 40, "维度": {"立意与思想": 10, "结构与条理": 10,
                                   "语言与表达": 10, "素材与论证": 10}}
    good = ("1. 【总分】24/40\n2. 【维度得分】立意与思想 6/10 —— a\n结构与条理 6/10 —— b\n"
            "语言与表达 7/10 —— c\n素材与论证 5/10 —— d\n3. 【等级】三类文（60%）\n"
            "4. 【亮点】「看清风景」一句好\n5. 【逐条问题】① 「总之」重复\n"
            "6. 【改进建议】改成答一个问题。示范改写：我把晚饭站着扒完。\n7. 【置信度】高\n")
    assert all(ok for _, ok, _ in validate_essay(good, case)), "合规样本应全项通过"

    bad = good.replace("24/40", "30/40")
    failed = [n for n, ok, _ in validate_essay(bad, case) if not ok]
    assert "维度得分之和=总分" in failed, f"应抓出加法不符，实际检出 {failed}"
    assert "等级与总分占比自洽" in failed, "40 分之 30 应为二类文，写三类文应被检出"


def test_essay_validator_requires_quotes():
    """每条评价都要有原文落点：没有「」引用的评语应被检出（防无落点套话）。"""
    case = {"总分": 40, "维度": {"立意与思想": 10}}
    raw = ("1. 【总分】6/40\n2. 【维度得分】立意与思想 6/10 —— a\n3. 【等级】四类文\n"
           "4. 【亮点】语言优美\n5. 【逐条问题】逻辑不清\n"
           "6. 【改进建议】示范改写：xx\n7. 【置信度】高\n")
    failed = [n for n, ok, _ in validate_essay(raw, case) if not ok]
    assert "亮点有原文引用" in failed and "逐条问题有原文引用" in failed


def test_qa_fewshot_exists_and_covers_both_cultures():
    """答疑范文：原来 qa 是唯一零样本的角色（且答疑最需要风格锚定）。

    新范文必须同时覆盖理科与文科，且文科例里不得出现理科符号。
    """
    assert "qa" in lib.list_fewshots(), "qa 应有范文"
    exs = lib.fewshots["qa"]["examples"]
    assert len(exs) >= 2, "至少理科、文科各一例"
    for ex, must in zip(exs, ("数学", "语文")):
        assert must in ex["input"], f"范文缺{must}示例"
    zh = [e for e in exs if "语文" in e["input"]][0]["output"]
    for sym in ("×", "÷", "x²", "=", "公式"):
        assert sym not in zh, f"文科范文不应出现理科记号：{sym}"
    assert "「" in zh, "文科范文应有原文引用"


def test_qa_fewshot_keeps_guidance_not_answers():
    """范文的【思路引导】不得直接把结论写出来——这是答疑与搜题的分界线。

    断言用 validate_qa 的同一口径，避免“范文合规”与“校验器标准”两套。
    """
    exs = lib.fewshots["qa"]["examples"]
    probes = [{"答案词": ["同号得正"]}, {"答案词": ["取消句子独立性"]}]
    for ex, probe in zip(exs, probes):
        failed = [n for n, ok, _ in validate_qa(ex["output"], probe,
                                                check_contamination=False) if not ok]
        assert not failed, f"范文未通过校验：{failed}"


def test_qa_template_carries_the_question():
    """qa 必须能承载「学生提问」本身。

    原版只写「回答学生的提问」，却没有任何字段承载提问，
    只能靠调用方另发一条 user 消息，范文的【示例输入】也就无处对标。
    """
    assert "学生提问" in lib.missing_vars("qa", {}), "qa 应有 {{学生提问}} 占位符"
    p = lib.build("qa", {"学科": "初中数学", "学生水平": "基础",
                          "知识点链": "A → B", "学生提问": "为什么 1+1=2？"}, fewshot=False)
    assert "【本次提问】" in p and "为什么 1+1=2？" in p
    assert "{{学生提问}}" not in p
    # 缺提问时应被入口预检拦下（不是交给模型猜）
    blanks = blank_inputs(lib, "qa", {"学生提问": ""})
    assert "学生提问" in blanks


def test_qa_validator_catches_answer_leak():
    """校验器必须能抓出「把结论写进【思路引导】」——否则这条断言等于没装。"""
    raw = ("1. 【思路引导】直接告诉你：同号得正。\n2. 【分步讲解】x\n"
           "3. 【分层适配】基础档：讲得慢一点。\n4. 【同类题】算 (-2)×(-3)。\n"
           "5. 【知识点链接】有理数乘法法则 → 相反数 → 数轴\n")
    failed = [n for n, ok, _ in validate_qa(raw, {"答案词": ["同号得正"],
                                                "链内": ["相反数"]}) if not ok]
    assert any("思路引导未给出结论" in f for f in failed), f"漏检：{failed}"
    assert not any("模块" in f for f in failed), "该样本结构是完整的，不应报模块问题"


if __name__ == "__main__":
    main()

