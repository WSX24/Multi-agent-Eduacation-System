#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集成路由器 V1.0

把「提示词模板库（PromptTemplate/）」和「PPT-Agent」串成一条可路由的链路：

    用户一句话
      │  意图路由（规则优先，可 --route 强制）
      ├─ ppt     直接要课件/PPT        → teacher_ppt / ppt_general 渲染 → 调 pipeline 产片
      ├─ plan    长期系统学习          → teacher_planner 渲染 → 调 LLM 出课程蓝图（章节/单元）
      ├─ sprint  短期速成（≤2 周）     → teacher_sprint 渲染 → 调 LLM 一次性交付
      ├─ lesson  单点讲解              → teacher 渲染 → 调 LLM 单次讲解
      └─ qa      答疑                  → qa 渲染 → 调 LLM（引导不喂答案）

产物归类（全部落本目录下，按日期分文件夹）：

    templates/YYYYMMDD/          经由提示词模板加工、发给 LLM 的提示词（以及非 ppt 分支的结果）
    ppt/YYYYMMDD/               按日期分文件夹
      └─ <主题>_<HHMMSS>/       每次产出一个目录（同一主题不同时间互不覆盖）：
           · <主题>_<HHMMSS>.pptx
           · <主题>_<HHMMSS>.md             结构化讲稿（逐页标题 + 旁白）
           · <主题>_<HHMMSS>_页面说明.md     页面内容 / 视觉构成 / 强调线索
           · 产出它的 ppt-master skill 项目文件夹（原样保留）

用法::

    python router.py "做一份 8 页的《一次函数》PPT"
    python router.py "系统学完初中化学" --weeks 8 --hours-per-week 3
    python router.py "一周搞定浮力"
    python router.py "讲讲因式分解"
    python router.py "这道题为什么做错了"
    python router.py "..." --dry-run          # 只渲染+保存提示词，不调 LLM / 不产片
    python router.py "..." --route ppt        # 强制走某条路
    python router.py "..." --route ppt --role ppt_general
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent            # 集成路由V1.0/
ROOT = HERE.parent                                # 自建Agent/
PPT_AGENT_DIR = ROOT / "PPT-Agent"
PROMPT_LIB_DIR = ROOT / "PromptTemplate"
TEMPLATES_OUT = HERE / "templates"                # 提示词产出
PPT_OUT = HERE / "ppt"                            # PPT 产出

for _p in (str(HERE), str(PROMPT_LIB_DIR), str(PPT_AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from prompt_builder import PromptLib  # noqa: E402
from export_notes import export_narration_md, export_pages_md  # noqa: E402

# ---------------------------------------------------------------------------
# 常量：学科 / 学生水平 / 路由关键词
# ---------------------------------------------------------------------------
SUBJECTS = ("数学", "语文", "英语", "物理", "化学", "生物", "历史", "地理",
            "思想政治", "道德与法治", "政治", "信息技术", "计算机", "编程",
            "科学", "音乐", "美术", "体育")

LEVEL_SYNONYMS = {
    "零基础": "入门", "小白": "入门", "初学": "入门", "入门": "入门",
    "基础": "基础", "初级": "基础", "普通": "基础", "一般": "基础",
    "中等": "中等", "中级": "中等", "中等生": "中等", "普通生": "中等",
    "进阶": "进阶", "提高": "进阶", "拔高": "进阶", "中上": "进阶",
    "竞赛": "竞赛", "奥赛": "竞赛", "尖子": "竞赛", "学霸": "竞赛", "拔尖": "竞赛",
}

# 路由判定：顺序即优先级（ppt 最明确 > qa 提问 > sprint 短时限 > plan 长期 > lesson 兜底）
_PPT_RE = re.compile(r"ppt|课件|幻灯片|演示文稿|slides|slideshow", re.I)
_QA_RE = re.compile(r"为什么|怎么(?:办|做|回事|解|写|理解)|这道题|错在|哪(?:里|儿)错|"
                    r"不会|不懂|帮我看看|求(?:解|答案)|如何(?:解|做)|讲讲这道")
_SPRINT_RE = re.compile(r"一[两周]|三天|五天|考前|突击|速成|几天|尽快|短期|冲刺|抱佛脚|"
                        r"临时|快[速点]|急救")
_PLAN_RE = re.compile(r"系统|学完|备考|学期|暑假|寒假|几个月|从头|零基础|全程|长期|"
                      r"课程|规划|计划|定制|体系|框架|从.*到.*")

# 页数 / 时限抽取
#: 与 `PPT-Agent/pipeline.py` 的 `_PAGES_RE` **必须一致**：
#: 那里支持汉字数字与三位数，这里以前只认 1~2 位阿拉伯数字，导致「十二页」
#: 在 --dry-run 渲染的提示词副本里被记成 10 页、而实际产片又是 12 页。
_PAGES_RE = re.compile(r"(?:共\s*)?(\d{1,3}|[一二两三四五六七八九十]{1,3})\s*页")
_WEEKS_RE = re.compile(r"(\d{1,2})\s*[周个星期]")
_DAYS_RE = re.compile(r"(\d{1,2})\s*天")

#: 汉字数字（与 pipeline.py 的 `_CN_DIGITS` 同口径，支持到 99）
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """阿拉伯数字原样；汉字数字支持到 99（八 / 十 / 十二 / 二十 / 二十五）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        if head and head not in _CN_DIGITS:
            return None
        if tail and tail not in _CN_DIGITS:
            return None
        return ((_CN_DIGITS.get(head, 1) if head else 1) * 10
                + (_CN_DIGITS.get(tail, 0) if tail else 0))
    return _CN_DIGITS.get(s)

ROUTES = ("ppt", "plan", "sprint", "lesson", "qa")

# 路由 → 模板角色名（route 是产品概念，模板名是 PromptTemplate/ 里的实际角色）
ROUTE_TO_TEMPLATE = {
    "plan": "teacher_planner",
    "sprint": "teacher_sprint",
    "lesson": "teacher",
    "qa": "qa",
}


# ---------------------------------------------------------------------------
# 意图路由
# ---------------------------------------------------------------------------
def route_intent(text: str) -> str:
    """规则版意图路由。返回 route ∈ ROUTES。拿不准时默认 lesson（单点讲解）。"""
    if _PPT_RE.search(text):
        return "ppt"
    if _QA_RE.search(text):
        return "qa"
    if _SPRINT_RE.search(text):
        return "sprint"
    if _PLAN_RE.search(text):
        return "plan"
    return "lesson"


# ---------------------------------------------------------------------------
# 字段提取（供各分支组装模板变量 + 命名）
# ---------------------------------------------------------------------------
def extract_subject(text: str) -> str:
    for s in sorted(SUBJECTS, key=len, reverse=True):   # 长词优先（"信息技术"先于"信息"）
        if s in text:
            return s
    return "通用"


def extract_level(text: str) -> str:
    for kw, lv in sorted(LEVEL_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        if kw in text:
            return lv
    return "基础"


def extract_topic(text: str) -> str:
    """主题/知识点：优先《》，否则去掉引导/包装词后的残句，兜底整句。"""
    m = re.search(r"《([^》]+)》", text)
    if m:
        return m.group(1).strip()
    t = _PAGES_RE.sub("", text)
    # 去掉动作引导词、包装词、路由词（按长度降序，避免「学习」先于「学习目标」被误删）
    t = re.sub(r"做|帮我|生成|制作|写|一份|一个|的|，|,|。|！|！|？|？|"
               r"讲讲|讲解|介绍一下|什么是|是什么|给我|我想|想学|学习|了解|关于", "", t)
    t = _PPT_RE.sub("", t)
    t = re.sub(r"^(讲讲|讲解|介绍|关于|学习|了解|给我|帮我|我想|想学)+", "", t)
    return t.strip(" ，,。 ")[:40] or text[:40]


def extract_pages(text: str, default: int = 10) -> int:
    """从用户话里抽页数；抽不到给默认值（10）。"""
    m = _PAGES_RE.search(text)
    if not m:
        return default
    n = _cn_to_int(m.group(1))
    return n if n else default


def extract_duration(text: str) -> tuple[int, int]:
    """返回 (周数, 天数)，供 plan / sprint 用。"""
    weeks = int(_WEEKS_RE.search(text).group(1)) if _WEEKS_RE.search(text) else 0
    days = int(_DAYS_RE.search(text).group(1)) if _DAYS_RE.search(text) else 0
    return weeks, days


def safe_name(s: str, limit: int = 40) -> str:
    """把主题/课程名清洗成可作文件名的片段。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", s or "").strip("_")
    return s[:limit] or "未命名"


# ---------------------------------------------------------------------------
# 产物落盘
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now()


def _dated_dir(base: Path) -> Path:
    d = base / _now().strftime("%Y%m%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return _now().strftime("%H%M%S")


def _run_dir(topic: str) -> Path:
    """一次产出的归集目录：ppt/<YYYYMMDD>/<主题中文名>_<HHMMSS>/

    三层结构：日期 → 本次运行（主题_时分秒）→ PPTX + 产出它的 skill 项目。
    同一主题不同时间跑就是不同文件夹，互不覆盖。
    """
    now = _now()
    d = PPT_OUT / f"{now:%Y%m%d}" / f"{safe_name(topic)}_{now:%H%M%S}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_prompt(route: str, topic: str, prompt: str, original: str) -> Path:
    """把「发给 LLM 的提示词」落到 templates/日期/。"""
    d = _dated_dir(TEMPLATES_OUT)
    path = d / f"{route}_{safe_name(topic)}_{_stamp()}_prompt.md"
    path.write_text(
        f"# 路由：{route}  时间：{_now():%Y-%m-%d %H:%M:%S}\n"
        f"# 用户输入：{original}\n\n"
        f"{prompt}\n",
        encoding="utf-8")
    return path


def save_result(route: str, topic: str, result: str) -> Path:
    """非 ppt 分支：把 LLM 返回结果落到 templates/日期/（与提示词同目录）。"""
    d = _dated_dir(TEMPLATES_OUT)
    path = d / f"{route}_{safe_name(topic)}_{_stamp()}_result.md"
    path.write_text(result, encoding="utf-8")
    return path


def save_pptx(src: Path, topic: str, run_dir: Path | None = None) -> Path:
    """把产出的 PPTX 复制进本次运行目录 <主题>_<HHMMSS>/ 里。

    run_dir 由调用方传入（产片前已建好），避免跨秒时又算出另一个目录。
    """
    d = run_dir or _run_dir(topic)
    dst = d / f"{safe_name(topic)}_{_stamp()}.pptx"
    shutil.copy2(src, dst)
    return dst


# ---------------------------------------------------------------------------
# LLM 调用（非 ppt 分支）
# ---------------------------------------------------------------------------
def load_env_file() -> None:
    """从 PPT-Agent/.env 读 DEEPSEEK_* 到环境变量（幂等）。"""
    env_file = PPT_AGENT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def api_config() -> tuple[str, str, str]:
    load_env_file()
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
    model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    return key, base_url, model


def call_llm(prompt: str) -> str:
    key, base_url, model = api_config()
    if not key:
        raise RuntimeError("未找到 API Key（PPT-Agent/.env 的 DEEPSEEK_API_KEY）")
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url, timeout=600)
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], temperature=0.7)
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# 各分支：组装模板变量
# ---------------------------------------------------------------------------
def build_variables(route: str, text: str, args) -> dict:
    subject = args.subject or extract_subject(text)
    level = args.level or extract_level(text)
    topic = args.topic or extract_topic(text)
    weeks, days = extract_duration(text)

    if route == "ppt":
        # ppt 分支的变量由 pipeline.py 的 parse_task 自己抽，这里只留命名与角色
        return {"主题": topic, "学科": subject, "学生水平": level,
                "页数要求": args.pages or extract_pages(text)}
    if route == "plan":
        return {"学习目标": topic, "学科": subject, "学生水平": level,
                "总周期": f"{args.weeks or weeks or 8}周",
                "每周可投入时间": f"{args.hours_per_week or 3}课时"}
    if route == "sprint":
        return {"学习目标": topic, "学科": subject, "学生水平": level,
                "可用时限": f"{args.days or days or 7}天"}
    if route == "lesson":
        return {"学科": subject, "知识点": topic, "学生水平": level,
                "need_example": "true"}
    if route == "qa":
        return {"学生提问": text, "学科": subject, "学生水平": level,
                "知识点链": topic}
    raise ValueError(f"未知路由 {route}")


# ---------------------------------------------------------------------------
# 分支处理
# ---------------------------------------------------------------------------
def _run_pipeline(args: list[str]) -> subprocess.CompletedProcess:
    """subprocess 调 pipeline.py（静默，拿完整输出）。"""
    cmd = [sys.executable, str(PPT_AGENT_DIR / "pipeline.py")] + args
    return subprocess.run(cmd, cwd=str(PPT_AGENT_DIR),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# 产片阶段只打印这些「关键步骤」行，其余（token/chunk/预填充等）一律丢弃
_KEY_LINE_RES = [
    re.compile(r"【S\d"),                     # pipeline 阶段 S0~S4
    re.compile(r"▌\s*P\d"),                  # agent 阶段 P0~P5
    re.compile(r"模型调用："),                # 逐页「写/修复 第N页」
    re.compile(r"^\s*(✅|❌|⚠️)"),            # 结果标记行
    re.compile(r"报告："),                    # 校验汇总「12 页 / 通过 1 / …」
    re.compile(r"阶段化执行完成|总耗时|交付文件|页数|项目目录|核心信息"),
    re.compile(r"最终校验|导出前复检|svg_to_pptx|blocking|失败"),
]


def _is_key_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and any(r.search(s) for r in _KEY_LINE_RES)


def _run_pipeline_stream(args: list[str]) -> int:
    """实时调 pipeline.py，只转发关键步骤行（轻量反馈）。"""
    cmd = [sys.executable, str(PPT_AGENT_DIR / "pipeline.py")] + args
    proc = subprocess.Popen(cmd, cwd=str(PPT_AGENT_DIR),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        if _is_key_line(line):
            print("   " + line.rstrip())
    proc.wait()
    return proc.returncode


def handle_ppt(text: str, args, lib: PromptLib) -> None:
    role = args.role or "teacher_ppt"
    topic = safe_name(args.topic or extract_topic(text))

    # ① 拿「实际发给 LLM 的提示词」：pipeline --explain 只渲染不调 LLM
    print(f"【ppt】渲染提示词（{role}）…")
    r = _run_pipeline([text, "--role", role, "--explain"])
    prompt_path = None
    m = re.search(r"提示词已落盘[：:]\s*(.+\.txt)", r.stdout)
    if m:
        prompt_path = Path(m.group(1).strip())
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path and prompt_path.exists() else r.stdout
    saved = save_prompt("ppt", topic, prompt_text, text)
    print(f"   ✓ 提示词已保存：{saved}")
    if r.returncode != 0:
        print(f"   ✗ pipeline --explain 返回 {r.returncode}：\n{r.stderr[-800:]}")
        return

    if args.dry_run:
        print("   （--dry-run：到此为止，不产片）")
        return

    # ② 产片：output-root 指向 ppt/<YYYYMMDD>/<主题>_<HHMMSS>/
    #    这样 skill 项目文件夹会和 PPTX 落在同一个「本次运行」目录里
    out_root = _run_dir(topic)
    print(f"【ppt】产片（output-root={out_root}）…")
    cmd = [text, "--role", role, "--output-root", str(out_root),
           "--reasoning-exec", args.reasoning_exec,
           "--reasoning-repair", args.reasoning_repair]
    if args.pages:
        cmd += ["--pages", str(args.pages)]
    if args.level:
        cmd += ["--level", args.level]
    if args.image_source != "off":
        cmd += ["--image-source", args.image_source,
                "--image-review", args.image_review,
                "--image-max-candidates", str(args.image_max_candidates)]
        if args.image_providers:
            cmd += ["--image-providers", args.image_providers]
        if args.no_image_strict:
            cmd += ["--no-image-strict"]
        if args.image_websearch:
            # 兜底图源：必应图片。产出的图授权未核实，只会备候选（Needs-Selection），
            # 不会自动上版面 —— 但仍是**显式开启**才启用的。
            cmd += ["--allow-websearch"]
    code = _run_pipeline_stream(cmd)

    # ③ 找最新 pptx 并命名归集
    pptx = _latest(out_root, "*.pptx")
    if pptx:
        dst = save_pptx(pptx, topic, run_dir=out_root)
        print(f"   ✓ PPT 已保存：{dst}")
        # ④ 文本产物导出（与 PPTX 同级）：
        #    <stem>.md         讲稿（逐页标题 + 旁白）
        #    <stem>_页面说明.md 页面内容 / 视觉构成 / 强调线索
        try:
            export_narration_md(dst, topic=topic)
            export_pages_md(dst, topic=topic)
        except Exception as exc:  # noqa: BLE001 —— 文本导出失败不该拖垮产片
            print(f"   ⚠️ 文本导出失败（{type(exc).__name__}）：{exc}")
    else:
        # 失败必须**可行动**：2026-10-22 岳飞那册 12 页 SVG 全写完却没 pptx，
        # 用户只能看到一句“未找到 .pptx”，无从下手。这里把项目路径、
        # 日志位置和“只差导出时”的重跑命令一并打出来。
        proj = next((d for d in out_root.iterdir() if (d / "design_spec.md").is_file()),
                    out_root) if out_root.is_dir() else out_root
        print(f"   ✗ 未导出 PPTX（pipeline 退出码 {code}）")
        print(f"     skill 项目：{proj}")
        print(f"     先看：{proj}/validation/workflow.log 末尾的 [ERROR]")
        scripts = _skill_scripts()
        print("     若 SVG 已齐全、只差导出，可直接重跑（不重新生成内容）：")
        if scripts:
            print(f'       python "{scripts}/svg_quality_checker.py" "{proj}" '
                  f'--quick-generate --canonical-authoring --stage final --json')
            print(f'       python "{scripts}/svg_to_pptx.py" "{proj}" --quick-generate')
        else:
            print("       （未找到 ppt-master skill，请设 PPT_MASTER_SKILL_DIR 后重跑）")


def _skill_scripts() -> Path | None:
    """找到 ppt-master skill 的 scripts/ 目录（供失败提示给出可复制的命令）。"""
    env = (os.getenv("PPT_MASTER_SKILL_DIR") or "").strip()
    seeds = [Path(env)] if env else []
    seeds += [ROOT, ROOT / ".agents" / "skills", ROOT / "ppt-master" / "skills"]
    for base in seeds:
        for cand in (base / "scripts", base / "ppt-master" / "scripts"):
            if (cand / "svg_to_pptx.py").is_file():
                return cand
    return None


def _latest(root: Path, pattern: str) -> Path | None:
    files = sorted(root.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def handle_text_role(route: str, text: str, args, lib: PromptLib) -> None:
    """plan / sprint / lesson / qa：渲染提示词 → 保存 → 调 LLM → 保存结果。"""
    variables = build_variables(route, text, args)
    try:
        prompt = lib.build(ROUTE_TO_TEMPLATE[route],
                           variables, fewshot=(route in ("plan", "sprint")))
    except ValueError as e:
        print(f"【{route}】输入不合契约，已阻断：{e}")
        return
    saved = save_prompt(route, variables.get("学习目标") or variables.get("知识点")
                        or variables.get("学生提问", ""), prompt, text)
    print(f"【{route}】提示词已保存：{saved}")
    if args.dry_run:
        print("   （--dry-run：到此为止，不调 LLM）")
        return
    print(f"【{route}】调 LLM …")
    try:
        result = call_llm(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"   ✗ LLM 调用失败：{e}")
        return
    res_path = save_result(route, variables.get("学习目标") or variables.get("知识点")
                           or variables.get("学生提问", ""), result)
    print(f"   ✓ 结果已保存：{res_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="集成路由器 V1.0：一句话 → 路由 → 提示词/PPT")
    ap.add_argument("text", help="用户的一句话需求")
    ap.add_argument("--route", choices=ROUTES, default=None,
                    help="强制路由（默认自动判定）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只渲染+保存提示词，不调 LLM、不产片")
    # 通用覆盖
    ap.add_argument("--topic", default=None, help="主题/知识点/学习目标（覆盖抽取）")
    ap.add_argument("--subject", default=None, help="学科（覆盖抽取）")
    ap.add_argument("--level", default=None, help="学生水平：入门/基础/中等/进阶/竞赛")
    ap.add_argument("--pages", type=int, default=None, help="页数（ppt 分支）")
    # ppt 分支
    ap.add_argument("--role", default=None, choices=["teacher_ppt", "ppt_general"],
                    help="ppt 分支用的模板角色（默认 teacher_ppt）")
    # ⚠️ 锁定：exec=none / repair=low。未经用户同意不要改（见 staged.py 同名注释）
    ap.add_argument("--reasoning-exec", default="none",
                    choices=["none", "low", "medium", "high", "max"],
                    help="P3 逐页写 SVG 的推理强度。none=~7s/页（默认，坐标已由代码算好）；"
                         "low=~30s/页（版面复杂、想更稳时用）")
    ap.add_argument("--reasoning-repair", default="low",
                    choices=["none", "low", "medium", "high", "max"],
                    help="P4 修复的推理强度（默认 low：P3 快 + P4 会判断）")
    ap.add_argument("--image-source", default="auto", choices=["off", "web", "ai", "auto"],
                    help="配图来源（默认 auto：按 §VIII 声明逐行走；off=不配图）")
    ap.add_argument("--image-review", default="score", choices=["auto", "score", "manual"],
                    help="web 图审图方式（默认 score：候选集评分后定稿）")
    ap.add_argument("--image-providers", default=None,
                    help="图库优先级，逗号分隔（默认 wikimedia,openverse,pexels,pixabay）")
    ap.add_argument("--image-max-candidates", type=int, default=8,
                    help="每张 web 图候选数上限（默认 8）")
    ap.add_argument("--image-websearch", action="store_true",
                    help="授权图源全部失败时，用必应图片搜一批**候选**（授权未核实，"
                         "只落 images/candidates/ 等人工挑选，不会自动上版面）")
    ap.add_argument("--no-image-strict", action="store_true",
                    help="关闭相关性闸门（默认开启：图与查询对不上就丢弃）")
    # plan 分支
    ap.add_argument("--weeks", type=int, default=None, help="总周期（周，plan 分支）")
    ap.add_argument("--hours-per-week", type=int, default=None, help="每周可投入时间（课时）")
    # sprint 分支
    ap.add_argument("--days", type=int, default=None, help="可用时限（天，sprint 分支）")
    args = ap.parse_args()

    text = args.text.strip()
    if not text:
        print("❌ 缺用户输入")
        return 2

    route = args.route or route_intent(text)
    print(f"\n🔀 路由：{route}  ← 输入：{text!r}")

    lib = PromptLib(PROMPT_LIB_DIR)
    if route == "ppt":
        handle_ppt(text, args, lib)
    else:
        handle_text_role(route, text, args, lib)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
