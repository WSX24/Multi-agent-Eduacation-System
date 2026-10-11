#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元 → PPT 一体化入口：把「提示词模板库」串成一条链路。

    teacher_unit 渲染 → 桥接(unit_bridge) → teacher_ppt 规划 → agent 产出 PPTX

用法::

    python run_unit_ppt.py --unit "导数的几何意义" --chapter "第2章 导数及其应用" \\
        --level 竞赛 --hours 1.5 --pages 10
    python run_unit_ppt.py --unit "..." --chapter "..." --dry-run   # 只渲染 3 段提示词，不调 LLM

三步各有落盘，便于单独复查：

    runs/unit_<stamp>.md            teacher_unit 的 10 模块输出（单元教学材料）
    runs/unit_ppt_input_<stamp>.md  桥接结果（5 内容模块 + 页骨架参照）—— 喂给 pipeline.py
    runs/...                        规划 plan_*.md / task_*.md，最终 PPTX 路径由 pipeline 打印
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT_LIB_DIR = HERE.parent / "PromptTemplate"        # 提示词模板库
RUNS_DIR = HERE / "runs"
for _p in (str(PROMPT_LIB_DIR), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from prompt_builder import PromptLib                     # noqa: E402
from unit_bridge import unit_output_to_ppt_content       # noqa: E402

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 合法档位（与 teacher_unit / teacher_ppt 模板一致）
LEVELS = ("入门", "基础", "中等", "进阶", "竞赛")
#: 常见说法 → 5 档（大小写/后缀都容错）
_LEVEL_SYNONYMS = {
    "零基础": "入门", "无基础": "入门", "小白": "入门", "初学": "入门",
    "差生": "入门", "后进": "入门", "后进生": "入门", "薄弱": "入门",
    "初级": "基础", "一般": "基础", "普通": "基础", "基础水平": "基础", "正常": "基础",
    "中等生": "中等", "中级": "中等", "中等水平": "中等", "普通生": "中等", "平均水平": "中等",
    "中上": "进阶", "较好": "进阶", "提高": "进阶", "尖子": "进阶", "中等偏上": "进阶",
    "竞赛生": "竞赛", "尖子生": "竞赛", "优秀": "竞赛", "拔尖": "竞赛",
    "奥赛": "竞赛", "学霸": "竞赛", "优秀生": "竞赛", "顶尖": "竞赛",
}


def normalize_level(raw: str) -> str:
    """把常见说法归一到 5 档（入门/基础/中等/进阶/竞赛）；无法识别时回退「基础」。

    为什么要归一：`--level` 原来原样透传，传「中等生」会得到「中等生档」这种
    模板不认的档位，档位条件分支就落了空。
    """
    s = (raw or "").strip().replace("档", "").replace("水平", "")
    if s in LEVELS:
        return s
    if s in _LEVEL_SYNONYMS:
        return _LEVEL_SYNONYMS[s]
    for lv in LEVELS:                      # 包含关系：如「中等生」含「中等」
        if lv in s:
            return lv
    for key, val in _LEVEL_SYNONYMS.items():
        if key in s:
            return val
    return "基础"


def load_env() -> None:
    """读 PPT-Agent/.env（KEY=VALUE），不覆盖已有环境变量。"""
    env = HERE / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def build_unit_prompt(args) -> str:
    """组装 teacher_unit 的渲染提示词。"""
    lib = PromptLib(PROMPT_LIB_DIR)
    variables = {
        "学科": args.subject or "数学",
        "学生水平": args.level,
        "need_example": args.need_example,
        "是否安排复习": args.review,
        "本单元课时": str(args.hours),
        "本单元序号": str(args.seq),
        "本章单元总数": str(args.total),
        "上一章测验结果": args.prev,
        "当前章节": args.chapter,
        "本单元名称": args.unit,
        "学情数据": args.data,
        "前置薄弱点": args.weak,
    }
    return lib.build("teacher_unit", variables, fewshot=not args.no_fewshot)


def call_llm(prompt: str, args) -> str:
    """调 LLM（OpenAI 兼容；复用 pipeline 的 .env 约定）。"""
    from openai import OpenAI
    key = args.api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise SystemExit("未找到 API Key（PPT-Agent/.env 的 DEEPSEEK_API_KEY）")
    client = OpenAI(api_key=key, base_url=args.base_url, timeout=600)
    resp = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=args.temperature)
    return (resp.choices[0].message.content or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="单元 → PPT 一体化（teacher_unit → teacher_ppt → PPTX）")
    ap.add_argument("--unit", required=True, help="单元名称（= 课件主题）")
    ap.add_argument("--chapter", required=True, help="所属章节，如「第2章 导数及其应用」")
    ap.add_argument("--subject", default="", help="学科（透传给 PPT；可留空）")
    ap.add_argument("--level", default="基础", help="学生水平：入门/基础/中等/进阶/竞赛")
    ap.add_argument("--hours", default="1.5", help="本单元课时")
    ap.add_argument("--seq", type=int, default=1, help="本单元序号（本章第几个单元）")
    ap.add_argument("--total", type=int, default=1, help="本章单元总数")
    ap.add_argument("--prev", default="无（首章）", help="上一章测验结果，如「正确率 82%%」或「无（首章）」")
    ap.add_argument("--data", default="无数据", help="近期学情数据")
    ap.add_argument("--weak", default="无", help="前置薄弱点")
    ap.add_argument("--need-example", default="true", choices=["true", "false"])
    ap.add_argument("--review", default="false", choices=["true", "false"], help="是否安排复习")
    ap.add_argument("--pages", type=int, default=10, help="PPT 页数")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--temperature", type=float, default=0.4)
    ap.add_argument("--no-fewshot", action="store_true", help="渲染 teacher_unit 提示词时不拼 fewshot")
    ap.add_argument("--dry-run", action="store_true", help="只渲染提示词，不调 LLM、不产片")
    ap.add_argument("--skip-unit", action="store_true",
                    help="跳过 teacher_unit 渲染，直接用 --unit-file 里已有的单元输出")
    ap.add_argument("--unit-file", default=None, help="已有的 teacher_unit 输出（配合 --skip-unit）")
    ap.add_argument("--agent-args", default="", help="透传给 pipeline.py 的其它参数，如 --agent-args '--parallel-pages 3'")
    # 推理旋钮：透传给 pipeline.py（控制 PPT 阶段 P1~P4 的推理强度，影响速度/质量）
    ap.add_argument("--reasoning-plan", default=None, choices=["none", "low", "medium", "high", "max"],
                    help="P1/P2 规划阶段推理强度（默认继承全局 low）")
    ap.add_argument("--reasoning-exec", default=None, choices=["none", "low", "medium", "high", "max"],
                    help="P3 逐页写 SVG 的推理强度；none=关思考（快，坐标系已由代码算好）")
    ap.add_argument("--reasoning-repair", default=None, choices=["none", "low", "medium", "high", "max"],
                    help="P4 修复的推理强度；常用组合：--reasoning-exec none --reasoning-repair low")
    ap.add_argument("--max-page-repairs", type=int, default=None, help="P4 逐页修复轮数上限（默认 2）")
    args = ap.parse_args()

    raw_level = args.level
    args.level = normalize_level(raw_level)
    if args.level != raw_level:
        print(f"ℹ️ 学生水平「{raw_level}」已归一为「{args.level}」")

    load_env()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    # ── ① 单元渲染 ────────────────────────────────────────────────
    if args.skip_unit:
        if not args.unit_file:
            raise SystemExit("--skip-unit 需要同时给 --unit-file")
        unit_out = Path(args.unit_file).read_text(encoding="utf-8", errors="replace")
        print(f"① 跳过单元渲染，复用 {args.unit_file}（{len(unit_out)} 字符）")
    else:
        prompt = build_unit_prompt(args)
        if args.dry_run:
            print("① teacher_unit 提示词（dry-run，不调 LLM）：\n" + "─" * 60)
            print(prompt)
            print("─" * 60)
            unit_out = ""
        else:
            print(f"① 单元渲染：调 {args.model}（{len(prompt):,} 字符提示词）…")
            unit_out = call_llm(prompt, args)
            unit_md = RUNS_DIR / f"unit_{stamp}.md"
            unit_md.write_text(unit_out, encoding="utf-8")
            print(f"   单元教学内容已落盘：{unit_md}（{len(unit_out):,} 字符）")

    if args.dry_run:
        # dry-run 时把 teacher_ppt 的渲染也打出来看看（用占位单元内容）
        lib = PromptLib(PROMPT_LIB_DIR)
        demo = unit_output_to_ppt_content("【本课目标】…\n【知识讲解】…\n【课件大纲】P1 … — …")
        print("\n②↓③ teacher_ppt 提示词将由 pipeline.py 渲染，其「单元内容」形如：\n" + "─" * 60)
        print(demo)
        print("─" * 60)
        return 0

    # ── ② 桥接：单元输出 → PPT 的「单元内容」──────────────────────
    ppt_input = unit_output_to_ppt_content(unit_out)
    bridge_md = RUNS_DIR / f"unit_ppt_input_{stamp}.md"
    bridge_md.write_text(ppt_input, encoding="utf-8")
    print(f"② 桥接完成：{bridge_md}（{len(ppt_input):,} 字符；"
          f"{'含页骨架参照' if '上游页级大纲' in ppt_input else '无页骨架参照'}）")

    # ── ③ pipeline.py：规划 → 产出 PPTX ───────────────────────────
    sentence = f"做一份 {args.pages} 页的《{args.unit}》课件"
    cmd = [sys.executable, str(HERE / "pipeline.py"), sentence,
           "--unit-file", str(bridge_md),
           "--topic", args.unit,
           "--pages", str(args.pages),
           "--level", args.level]
    if args.subject:
        cmd += ["--subject", args.subject]
    if args.reasoning_plan:
        cmd += ["--reasoning-plan", args.reasoning_plan]
    if args.reasoning_exec:
        cmd += ["--reasoning-exec", args.reasoning_exec]
    if args.reasoning_repair:
        cmd += ["--reasoning-repair", args.reasoning_repair]
    if args.max_page_repairs is not None:
        cmd += ["--max-page-repairs", str(args.max_page_repairs)]
    if args.agent_args.strip():
        cmd += ["--agent-args", args.agent_args]
    print(f"③ 规划 + 产片：{' '.join(cmd)}\n" + "─" * 72)
    code = subprocess.run(cmd, cwd=str(HERE)).returncode
    if code != 0:
        print(f"\n❌ pipeline.py 返回 {code}（单元内容与桥接结果已落盘，可单独复查）")
        return code
    print("\n✅ 全链路完成（单元渲染 → 桥接 → 规划 → PPTX）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
