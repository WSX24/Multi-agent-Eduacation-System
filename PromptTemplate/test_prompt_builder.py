#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板库冒烟测试：可用 pytest 运行，也可直接 `python test_prompt_builder.py`。"""
import sys
from pathlib import Path

import re
import yaml

from demo_course_flow import (build_planner_prompt, build_unit_prompt, can_unlock,
                              parse_blueprint, unit_variables)
from eval_unit import split_sections, validate as validate_unit
from eval_sprint import count_duration_hours, split_sections as split_sprint_sections, validate as validate_sprint
from prompt_builder import PromptLib
from quality_gate import (QualityGate, blank_inputs, copy_hits, load_fewshot_refs,
                          require_inputs, strip_scaffolding)

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
    assert "IF {{学生水平}} == \"入门\"" in prompt


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
            "薄弱点候选列表": "浮力", "错题": "题", "学生答案": "答", "参考答案": "参"}
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
            "薄弱点候选列表": "浮力", "错题": "题", "学生答案": "答", "参考答案": "参"}
    assert require_inputs(lib, "assistant", base) is base, "齐全时应原样返回变量表"
    try:
        require_inputs(lib, "assistant", {**base, "学生答案": "  "})
    except ValueError as exc:
        assert "学生答案" in str(exc), f"错误信息应指明缺哪项，实际：{exc}"
    else:
        raise AssertionError("缺「学生答案」必须抛 ValueError")


def test_chain_calls_all_go_through_precheck():
    """链路里每个模板调用点都要过预检（此前只有助教评测接入了）。"""
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


if __name__ == "__main__":
    main()

