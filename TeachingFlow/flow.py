#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TeachingFlow —— 教学链路编排器

    一句话
      → S0 范围解析（学段/学科/模块，用户选模块）
      → S1 学习方案（teacher_planner → 蓝图 YAML）
      → S2 选单元（默认前两个）
      → S3 单元教学（teacher_unit → 10 模块）
      → S4 课件大纲（teacher_ppt → 逐页大纲）
      → S5 产片（pipeline.py --from-plan → PPTX）

用法（默认就够，一句话即可）::

    python flow.py "我想用2周突击高中语文文言文"            # 全链路：选模块+估周期+产片+联网配图
    python flow.py "我想系统学习高中语文"                   # 同上，模块/周期由模型判断
    python flow.py "我想系统学习高中语文" --mock            # 不调 API，验证 5 跳数据流
    python flow.py "我想系统学习高中语文" --mock --to plan  # 只跑 S0-S1
    python flow.py "我想系统学习高中语文" --module 古诗文阅读 --weeks 10
    python flow.py "..." --unit ch01-u2 --units 1          # 只做一个单元
    python flow.py "..." --resume runs/20261023/xxx       # 续跑
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml

from core import naming, promptlint, scope
from core.artifacts import Run, safe_name
from core.config import PACK_DIR
from core.llm import LLM, LLMError
from steps import s0_scope, s1_plan, s2_select, s3_unit, s4_outline, s5_render

STOPS = ("map", "plan", "unit", "outline", "render")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="TeachingFlow：一句话 → 学习方案 → 单元教学 → 课件 → PPTX")
    ap.add_argument("text", help="一句话需求，如「我想系统学习高中语文」")
    # —— S0 覆盖 ——
    ap.add_argument("--stage", choices=scope.STAGES, default=None,
                    help="学段（指定即视为确认，不再询问）")
    ap.add_argument("--subject", default=None,
                    help="学科（指定即视为确认，不再询问）")
    ap.add_argument("--accept-detected", action="store_true",
                    help="学段/学科未能确定时，确认采用当前推断值（非交互环境必选）")
    ap.add_argument("--level", default=None,
                    choices=["入门", "基础", "中等", "进阶", "竞赛"], help="学生水平")
    ap.add_argument("--module", default=None, help="指定学习模块（子串匹配）")
    ap.add_argument("--term", choices=["long", "short"], default=None,
                    help="任务类型：long 长期（默认 10 周）/ short 短期（默认 2 周）"
                         "；不指定时按句子里的周期或关键词判断")
    ap.add_argument("--weeks", type=int, default=None, help="本阶段总周期（周，须 < 16）")
    ap.add_argument("--days", type=int, default=None, help="本阶段总周期（天，换算成周）")
    ap.add_argument("--hours-per-week", type=int, default=None, help="每周可投入课时")
    ap.add_argument("--pages", type=int, default=None,
                    help="每份 PPT 页数（8~30）。留空＝不写死：由 S4a 读完单元内容后"
                         "按「一页一个教学动作」决策（接口契约边界 8~30）")
    ap.add_argument("--units", type=int, default=None,
                    help="本次渲染几个单元。留空＝自动（短期任务 1 个，长期任务 2 个）")
    ap.add_argument("--unit", default=None, help="只渲染指定单元 id，如 ch01-u2")
    ap.add_argument("--strict-gate", action="store_true",
                    help="按学生门控硬卡（默认只报告未解锁，仍生成备课内容）")
    ap.add_argument("--no-input", action="store_true", help="不交互，模块直接取第一个")
    # —— 执行控制 ——
    ap.add_argument("--mock", action="store_true",
                    help="用 fixtures/ 里的假回复代替 LLM，验证链路（不花 token）")
    ap.add_argument("--to", choices=STOPS, default="render",
                    help="跑到哪一步为止（默认 render：全链路）")
    ap.add_argument("--render", action="store_true", help="mock 模式下也真的调 pipeline 产片")
    ap.add_argument("--resume", default=None, help="续跑指定 run 目录")
    ap.add_argument("--temperature", type=float, default=0.7)
    # —— S5 透传给 pipeline ——
    ap.add_argument("--reasoning-exec", default="none",
                    choices=["none", "low", "medium", "high", "max"])
    ap.add_argument("--reasoning-repair", default="low",
                    choices=["none", "low", "medium", "high", "max"])
    ap.add_argument("--image-source", default="auto",
                    choices=["off", "web", "ai", "auto"],
                    help="配图来源（默认 auto＝按大纲声明联网取图；--no-images 关闭）")
    ap.add_argument("--no-images", action="store_true",
                    help="关闭配图（等价 --image-source off），出片更快")
    ap.add_argument("--image-review", default="score", choices=["auto", "score", "manual"])
    ap.add_argument("--image-providers", default="pixabay,wikimedia",
                    help="图库优先级，逗号分隔（默认 pixabay,wikimedia——国内 pixabay 最稳）")
    ap.add_argument("--image-max-candidates", type=int, default=8,
                    help="每张 web 图最多拉多少候选（默认 8）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_images", False):
        args.image_source = "off"
    text = (args.text or "").strip()
    if not text:
        print("❌ 缺用户输入")
        return 2

    run = Run(safe_name(text, 24), resume=args.resume)
    llm = LLM(mock=args.mock, temperature=args.temperature, run=run)
    mode = "MOCK（不调模型）" if args.mock else "真实调用"
    print(f"\n▶ TeachingFlow · {mode} · --to {args.to}")
    print(f"  产物目录：{run.dir}\n")
    run.manifest.update({"input": text, "mode": mode, "args": vars(args)})
    _lint_prompts()

    spec = course = None
    targets: list[dict] | None = None
    if args.resume:
        spec, course, targets = _load_resume(run)

    try:
        # ---------------- S0 ----------------
        if spec is None:
            spec = s0_scope.run(text, args, run, llm)
        else:
            print(f"【S0】复用 00_spec.json：{spec.subject}·{spec.module}，{spec.weeks} 周")
        if args.to == "map":
            return _finish(run, spec)

        # ---------------- S1 ----------------
        if course is None:
            course = s1_plan.run(spec, run, llm)
        else:
            print(f"【S1】复用 01_blueprint.yaml：{len(course.get('chapters') or [])} 章")
        if args.units is None:                       # 未指定 → 用 S0 自动决定的个数
            args.units = spec.units_per_run or 2
        if args.to == "plan":
            return _finish(run, spec)

        # ---------------- S2 ----------------
        if targets is not None and (args.unit or len(targets) != args.units):
            print(f"【S2】已存 {len(targets)} 个目标与本次要求（--unit={args.unit} "
                  f"--units={args.units}）不一致，重新选单元")
            targets = None
        if targets is None:
            targets = s2_select.run(course, args, run, focus=spec.focus)
        else:
            print(f"【S2】复用 02_targets.json：{len(targets)} 个单元")

        produced: list[str] = []
        for target in targets:
            uid = target["unit_id"]
            outline_path = run.path(f"04_outline_{uid}.result.md")
            skip_unit = outline_path.is_file()          # --resume：大纲已在，不重跑 S3/S4
            if skip_unit:
                print(f"【S3/S4】{uid} 已有大纲，跳过（--resume）")
                unit_text = run.path(f"03_unit_{uid}.result.md").read_text(
                    encoding="utf-8") if run.path(f"03_unit_{uid}.result.md").is_file() else ""
            else:
                # ---------------- S3 ----------------
                _, unit_text = s3_unit.run(course, target, run, llm)

            if args.to == "unit":
                continue

            # ---------------- S4 ----------------
            if not skip_unit:
                s4_outline.run(course, target, unit_text, spec.pages, run, llm,
                               pages_source=spec.pages_source, intensity=spec.intensity)
            if args.to == "outline":
                continue

            # ---------------- S5 ----------------
            deliverable = naming.deliverable_path(run.dir, target)
            if deliverable.is_file():
                print(f"【S5】{uid} 已有交付件 {deliverable.name}，跳过（--resume）")
                produced.append(str(deliverable))
                continue
            old = naming.old_output_roots(run.dir, target)
            if old:                                  # 旧布局：已产过片，只补中文名交付件
                print(f"【S5】{uid} 复用旧产物 {run.rel(old[0])}，按课程命名补交付件")
                produced.append(str(s5_render.publish(old[0], target, run)))
                continue
            if llm.mock and not args.render:
                print(f"【S5】{uid} 跳过产片（--mock 默认不产片；加 --render 可强制）")
                run.note(f"S5 跳过：{uid}（mock）")
                continue
            pptx = s5_render.run(target, outline_path, run, args)
            if pptx:
                produced.append(str(pptx))

        run.manifest["produced"] = produced
        return _finish(run, spec)

    except LLMError as exc:
        print(f"\n❌ {exc}")
        run.note(f"LLMError：{exc}")
        run.save()
        return 3
    except SystemExit as exc:                     # 各步主动终止
        if exc.code not in (0, None):
            print(f"\n⛔ {exc.code}")
            run.save()
            return 1
        return 0


def _lint_prompts() -> None:
    """启动时扫一遍本地模板：防止「示例页数」这类隐式锚点被改回去。

    不阻断（可能正在做实验），但必须说得响：一旦有示例数字，模型很可能直接照抄，
    而页数错了会一路传到 SVG、导出与复检，事后很难看出是哪一步带进去的。
    """
    hits = promptlint.scan_templates(PACK_DIR / "templates")
    if not hits:
        return
    print(f"⚠️ promptpack 里有 {len(hits)} 处示例页数，模型可能直接照抄：")
    for h in hits[:6]:
        print("   " + h)
    print("   → 改成 <阿拉伯数字> 占位；详见 tools/prompt_lint.py\n")


def _load_resume(run: Run):
    """从已落盘的中间产物里恢复 (FlowSpec, 蓝图, 单元清单)。"""
    spec = course = None
    targets: list[dict] | None = None
    p = run.path("00_spec.json")
    if p.is_file():
        spec = scope.FlowSpec(**json.loads(p.read_text(encoding="utf-8")))
    p = run.path("01_blueprint.yaml")
    if p.is_file():
        course = yaml.safe_load(p.read_text(encoding="utf-8"))
    p = run.path("02_targets.json")
    if p.is_file():
        targets = json.loads(p.read_text(encoding="utf-8")).get("targets") or []
    return spec, course, targets


def _finish(run: Run, spec) -> int:
    run.manifest["spec"] = spec.to_dict()
    run.save()
    print("\n" + "─" * 60)
    print(f"✓ 运行结束：{run.dir}")
    print(f"  清单：manifest.json（每步 ok / 校验项 / 产物路径）")
    for name, st in run.manifest["steps"].items():
        print(f"  {'✓' if st['ok'] else '✗'} {name}")
        for n in st.get("notes") or []:
            print(f"      ⚠️ {n}")
    print("─" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
