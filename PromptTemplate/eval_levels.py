#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两个新角色的**档位差分**评测：同题只改 {{学生水平}}，看分支是否真生效

为什么单独写一个：`eval_material.py` / `eval_outline.py` 只跑各自用例默认的那一档
（材料题＝基础、构思＝中等），其余四档**只有组装层断言**（测试只证明模板里有那段分支文本，
证明不了模型收到后行为真的变了）。`teacher` 当初正是栽在这里：取值域里有「中等」，
分支里却没有，传入后不命中任何档位也**不报错**。

  L1  五档各自的档位特征：模板里写死的起头词必须出现（如中等档的「抗反例：」）
  L2  档位不越界：低档不得出现高档特征（如入门档的提纲里不得混进「抗反例」）
  L3  差分：五档两两相似度 < 0.8（只改一个变量，输出却要彼此不同）
  L4  结构：每档仍要过该角色自己的结构校验器（跑分档不能以破坏契约为代价）

为什么断言优先用「模板写死的起头词」而不是泛泛的关键词：本项目踩过「关键词判据误判」
的坑——模型是**示范**某个档位，不是**标注**它。所以只锚定模板里逐字规定要写的标记
（`抗反例：` `备选` 等），其余交给差分断言。

用法::

    python eval_levels.py --self-check            # 离线：组装层五档全查（不调 API）
    python eval_levels.py --role outline -n 1     # 真跑：只跑构思，每档 1 次
    python eval_levels.py -n 1 --retry 1          # 两个角色都跑，带闸门回投
"""
from __future__ import annotations

import argparse
import difflib
import sys
from datetime import datetime
from pathlib import Path

from eval_generic import validate_material, validate_outline
from eval_planner import load_api_key
from prompt_builder import PromptLib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
RUNS_DIR = ROOT / "eval_runs"

BANDS = ("入门", "基础", "中等", "进阶", "竞赛")

# 每个角色的差分用例：**固定不变**，只改学生水平（这是差分的意义所在）。
# 用例与 eval_material / eval_outline 的首例保持一致，便于互相对照。
ROLES: dict[str, dict] = {
    "material": {
        "template": "teacher_material",
        "cot": "source_analysis",
        "case": {
            "学科": "初中语文",
            "材料": "【甲】予独爱莲之出淤泥而不染，濯清涟而不妖。（周敦颐《爱莲说》）\n"
                    "【乙】予谓菊，花之隐逸者也；牡丹，花之富贵者也；莲，花之君子者也。（同上）",
            "设问": "两则材料都写莲，作者借莲寄托了什么？请结合材料分点作答。",
        },
        # 档位特征：模板「条件分支指令」里**逐字规定**该档要多写什么。
        # None = 该档不锚定固定标记（基础档是标准输出，本来就没有额外标记）。
        "band_check": {
            "入门": None,
            "基础": None,
            "中等": {"must": ["动词"], "where": "回扣设问",
                     "why": "中等档要求在【回扣设问】里点明「设问的动词是……」"},
            "进阶": None,          # 「补一句为什么能支撑」无固定起头词，交给差分断言
            "竞赛": {"must_any": ["主要", "之一", "在该条件下", "条件下", "通常"], "where": "作答",
                     "why": "竞赛档要求结论表述加上必要的限定词"},
        },
        "must_not": {},            # 该角色低档没有需要排除的高档固定标记
    },
    "outline": {
        "template": "teacher_outline",
        "cot": "essay_outline",
        "case": {
            "学科": "初中语文",
            "题目": "以「这也是课堂」为题，写一篇记叙文",
        },
        "band_check": {
            "入门": {"para_count": 3, "material_count": 1,
                     "why": "入门档要求提纲只排 3 段、素材只给 1 个"},
            "基础": None,
            "中等": {"must": ["抗反例"], "where": "立意",
                     "why": "中等档要求在中心句与支撑之间补一行「抗反例：」"},
            "进阶": {"must_any": ["细节"], "where": "素材",
                     "why": "进阶档要求每个素材后补「写进那一段时用什么细节」"},
            "竞赛": {"must_any": ["备选"], "where": "立意",
                     "why": "竞赛档要求给出主选与一个备选中心句"},
        },
        "must_not": {"入门": ["抗反例"]},   # 入门档不得混进中等档的标记
    },
}


def band_pairs() -> list[tuple[str, str]]:
    """相邻档位对 + 首尾对。从 BANDS 派生，不手写——手写会漏档（teacher 踩过）。"""
    return list(zip(BANDS, BANDS[1:])) + [(BANDS[0], BANDS[-1])]


def level_case(role: str, band: str) -> dict:
    return {**ROLES[role]["case"], "学生水平": band}


def outline_para_count(raw: str) -> int:
    from eval_generic import outline_paragraphs, split_by_names
    _, bodies = split_by_names(raw, ["审题", "立意", "提纲", "素材", "首尾"])
    return len(outline_paragraphs(bodies.get("提纲", "")))


def section_of(raw: str, role: str, name: str) -> str:
    from eval_generic import MATERIAL_SECTIONS, OUTLINE_SECTIONS, split_by_names
    names = MATERIAL_SECTIONS if role == "material" else OUTLINE_SECTIONS
    _, bodies = split_by_names(raw, names)
    return bodies.get(name, "")


def structural(raw: str, role: str, band: str | None = None) -> list[tuple[str, bool, str]]:
    """跑分档不能以破坏契约为代价：结构仍要过该角色自己的校验器。

    必须带上档位（用 level_case）：有些契约是**档位相关**的——
    例如入门档的素材只要求 1 条，而不是 2 条。
    """
    case = level_case(role, band) if band else ROLES[role]["case"]
    if role == "material":
        return validate_material(raw, case)
    return validate_outline(raw, case)


def band_checks(raw: str, role: str, band: str) -> list[tuple[str, bool, str]]:
    """档位特征断言（模板逐字规定的标记 + 可数的结构）。"""
    checks: list[tuple[str, bool, str]] = []
    spec = ROLES[role]["band_check"].get(band)

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    if spec:
        if spec.get("para_count"):
            n = outline_para_count(raw)
            add(f"{band}档·提纲为 {spec['para_count']} 段", n == spec["para_count"],
                f"实际 {n} 段（{spec['why']}）")
        if spec.get("material_count"):
            from eval_generic import outline_material_items, split_by_names
            _, bodies = split_by_names(raw, ["审题", "立意", "提纲", "素材", "首尾"])
            n = len(outline_material_items(bodies.get("素材", "")))
            add(f"{band}档·素材为 {spec['material_count']} 条", n == spec["material_count"],
                f"实际 {n} 条（{spec['why']}）")
        if spec.get("must") or spec.get("must_any"):
            seg = section_of(raw, role, spec["where"])
            keys = spec.get("must") or spec["must_any"]
            hit = [k for k in keys if k in seg]
            add(f"{band}档·{spec['where']}有档位标记", bool(hit),
                f"{spec['why']}；未命中 {keys}")

    for tok in ROLES[role]["must_not"].get(band, []):
        add(f"{band}档·不越界({tok})", tok not in raw, f"{band}档出现了高档标记：{tok}")
    return checks


def self_check(lib: PromptLib) -> None:
    """离线：五档的 prompt 组装 + 分支文本 + 无残留占位符。"""
    total = 0
    for role, spec in ROLES.items():
        for band in BANDS:
            values = level_case(role, band)
            p = lib.build(spec["template"], values, fewshot=True, cot=spec["cot"])
            assert "{{" not in p, f"{role}/{band} 有未填充占位符"
            assert f"本次学生水平：{band}档" in p, f"{role}/{band} 没把档位值传进 prompt"
            assert f"{band}档：" in p, f"{role}/{band} 的档位分支文本缺失"
            total += 1
    # 差分对的完备性：5 档必须都在、且派生出的对里没有漏档
    for role in ROLES:
        assert set(ROLES[role]["band_check"]) == set(BANDS), f"{role} 的档位断言不全"
    pairs = band_pairs()
    assert len(pairs) == 5, f"差分对应为 5 对（4 相邻 + 1 首尾），实际 {len(pairs)}"
    covered = {b for pair in pairs for b in pair}
    assert covered == set(BANDS), f"差分对漏了档位：{set(BANDS) - covered}"

    print("离线自检通过：")
    print(f"  · {len(ROLES)} 个角色 × {len(BANDS)} 档 = {total} 份 prompt 组装正常、无残留占位符")
    print(f"  · 档位断言表覆盖全部 {len(BANDS)} 档；差分对 {len(pairs)} 对（含首尾对，不漏档）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=sorted(ROLES), default=None, help="只跑某个角色")
    ap.add_argument("-n", type=int, default=1, help="每档跑几次")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--retry", type=int, default=0)
    ap.add_argument("--self-check", action="store_true")
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
    roles = [args.role] if args.role else list(ROLES)
    print(f"模型: {args.model}  每档 {args.n} 次  角色: {'、'.join(roles)}\n")

    all_fail: list[str] = []
    for role in roles:
        spec = ROLES[role]
        print(f"【{role}】{spec['template']}   用例固定，只改 {{学生水平}}")
        bodies: dict[str, str] = {}
        struct_fail: list[str] = []
        band_fail: list[str] = []
        for band in BANDS:
            values = level_case(role, band)
            prompt = lib.build(spec["template"], values, fewshot=True, cot=spec["cot"])
            for i in range(1, args.n + 1):
                raw, messages = "", [{"role": "user", "content": prompt}]
                checks: list[tuple[str, bool, str]] = []
                for attempt in range(args.retry + 1):
                    try:
                        resp = client.chat.completions.create(
                            model=args.model, messages=messages, temperature=0.7)
                        raw = resp.choices[0].message.content or ""
                    except Exception as e:                    # noqa: BLE001
                        print(f"  {band}: API 失败 {type(e).__name__}: {e}")
                        break
                    checks = structural(raw, role, band) + band_checks(raw, role, band)
                    failed = [c for c in checks if not c[1]]
                    # 每次尝试都落盘：首轮不带后缀、重试带 _rN（同 eval_material 的约定）
                    suffix = f"_r{attempt}" if attempt else ""
                    (RUNS_DIR / f"levels_{role}_{band}_{stamp}_{i}{suffix}.md").write_text(
                        raw, encoding="utf-8")
                    if not failed or attempt >= args.retry:
                        break
                    messages += [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": "你的输出存在以下问题，请修正后重新输出完整结果：\n"
                         + "\n".join(f"- {c[0]}：{c[2]}" for c in failed)},
                    ]
                if not raw:
                    continue
                bodies[band] = raw
                failed = [c for c in checks if not c[1]]
                s_fail = [c[0] for c in failed if "档·" not in c[0]]
                struct_fail += s_fail
                band_fail += [c[0] for c in failed if "档·" in c[0]]
                print(f"  {band:4s} {len(checks) - len(failed)}/{len(checks)}"
                      + (f"（重试 {attempt} 次）" if attempt else "")
                      + f"  正文 {len(raw):>4d} 字符")
                for n_, _, d in failed:
                    print(f"      ✗ {n_}  {d}")

        # ---- 差分：只改了一个变量，输出却要彼此不同 ----
        print(f"\n  差分检查（同一题、只改学生水平）：")
        sim_fail: list[str] = []
        for a, b in band_pairs():
            if a in bodies and b in bodies:
                r = difflib.SequenceMatcher(None, bodies[a], bodies[b]).ratio()
                ok = r < 0.8
                if not ok:
                    sim_fail.append(f"{a}/{b}={r:.2f}")
                print(f"    {'✅' if ok else '⚠️ '} {a:4s} vs {b:4s} 相似度 {r:.2f}")
        print(f"  {'OK ' if not sim_fail else 'WARN '}L3 五档互不相似(<0.8) {sim_fail}")
        print(f"  {'OK ' if not struct_fail else 'WARN '}L4 结构仍合规 {sorted(set(struct_fail))}")
        print(f"  {'OK ' if not band_fail else 'WARN '}L1/L2 档位特征 {sorted(set(band_fail))}")
        all_fail += [f"{role}:{x}" for x in sim_fail + struct_fail + band_fail]
        print()

    print("=" * 72)
    print(f"原始输出已存档：{RUNS_DIR}")
    if all_fail:
        print(f"存在未通过项：{all_fail}")
        sys.exit(1)
    print("两个角色的五档差分全部通过。")


if __name__ == "__main__":
    main()
