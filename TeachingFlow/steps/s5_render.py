# -*- coding: utf-8 -*-
"""S5：产片（subprocess → PPT-Agent/pipeline.py）。

**走 ``--from-plan`` 而不是重新规划**：
S4 已经用 teacher_ppt 渲染出了逐页大纲，若这里再让 pipeline 从一句话重新规划，
等于同一件事调两次模型——token 翻倍，而且两次产出的页序可能不一致，
审计时也无法判断「交付的 PPT 到底来自哪份大纲」。

**实时转发关键行**：一次完整产片要几分钟到十几分钟，全程无声会让人以为卡死。
只转发阶段标记与逐页进度，其余（token/chunk/预填充）丢弃；超过 45 秒没有任何
关键行时补一行「仍在运行」，避免误判。

**交付件按章/单元父子落盘**：pipeline 产出的 pptx 在英文 slug 项目里，名字既英文又没章序。
这里复制一份到 ``<课程>/第N章-章名/第M单元-单元名.pptx``（讲稿/页面说明同 stem），
命名口径见 ``core/naming.py``。

**两件容易踩的事（都已处理）**：

1. **构建在短路径里做**：课程目录名是中文长句（如「我想用2周集中突破高中语文古诗文阅读
   的断句翻译」），加上 ``05_ppt_<uid>/<英文slug>/exports/.pptx-build-xxx/pptx_content/…``
   会把 Windows 的 260 字符 MAX_PATH 撑破，skill 里的 ``svg_to_pptx`` 就报
   ``No such file or directory`` 或 ``svg_output directory does not exist``。
   所以 pipeline 在 ``TeachingFlow/.work/<run-hash>_<uid>/``（纯 ASCII）里构建，
   导出成功后再把整个项目目录**移进** ``<课程>/05_ppt_<uid>/`` 归档。
2. **只认 run 内的产物**：pipeline 的 ``find_pptx`` 在 output-root 里找不到时会回退扫
   PPT-Agent/ppt-master 的共享 projects，于是报告里可能写出一份**别人的旧 PPTX**。
   这里绝不读那份报告，只在本次工作目录里找，并且硬性断言产物在 run 内。

⚠️ mock 模式默认不产片，需要真出片时加 ``--render``。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import gates, naming
from core.artifacts import Run
from core.config import PPT_AGENT_DIR

ROUTER_DIR = Path(__file__).resolve().parent.parent.parent / "集成路由V1.0"

#: 短路径构建根：放仓库内但纯 ASCII，避开 Windows MAX_PATH（见模块 docstring）
WORK_ROOT = Path(__file__).resolve().parent.parent / ".work"

#: 值得转发的行（与 集成路由V1.0/router.py 同口径）
_KEY_LINE_RES = [
    re.compile(r"【S\d"),                     # pipeline 阶段 S0~S4
    re.compile(r"▌\s*P\d"),                  # agent 阶段 P0~P5
    re.compile(r"模型调用："),                # 逐页「写/修复 第N页」
    re.compile(r"^\s*(✅|❌|⚠️)"),            # 结果标记行
    re.compile(r"报告："),
    re.compile(r"阶段化执行完成|总耗时|交付文件|页数|项目目录"),
    re.compile(r"最终校验|导出前复检|svg_to_pptx|blocking|失败"),
]


def _is_key(line: str) -> bool:
    s = line.strip()
    return bool(s) and any(r.search(s) for r in _KEY_LINE_RES)


def _mentions_foreign_pptx(line: str) -> bool:
    """pipeline 自己的 ``find_pptx`` 会回退扫共享 projects，报出一条**不相关**的旧 pptx。

    那行（``交付文件：…ppt-master…``）必须挡下来：否则用户以为我们把 PPT 放到
    ppt-master 去了。真正的产物位置由本模块自己确定，不读 pipeline 的报告。
    """
    low = line.lower()
    return "pptx" in low and ("ppt-master" in low or "ppt_agent" in low)


def _run_pipeline_stream(cmd: list[str]) -> tuple[int, str]:
    """实时转发关键行，返回 (退出码, 完整输出尾部)。"""
    proc = subprocess.Popen([sys.executable, "pipeline.py"] + cmd,
                            cwd=str(PPT_AGENT_DIR),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    assert proc.stdout is not None
    tail: list[str] = []
    last_beat = time.monotonic()
    for line in proc.stdout:
        tail.append(line)
        if len(tail) > 400:
            tail = tail[-400:]
        if _is_key(line):
            if _mentions_foreign_pptx(line):
                print("   ⚠️ （已忽略 pipeline 报的共享 projects 旧文件，不是本次产物）")
            else:
                print("   " + line.rstrip())
            last_beat = time.monotonic()
        elif time.monotonic() - last_beat > 45:
            print(f"   … 仍在运行 {int(time.monotonic() - last_beat)}s：{line.strip()[:70]}")
            last_beat = time.monotonic()
    proc.wait()
    return proc.returncode, "".join(tail)


def run(target: dict, outline_path: Path, run: Run, args) -> Path | None:
    uid = target["unit_id"]
    work_root = _work_root(run, uid)                 # 短路径构建（避开 MAX_PATH）
    archive = run.dir / f"05_ppt_{uid}"               # 构建完归档回课程目录

    cmd = build_cmd(target, outline_path, work_root, args)
    pages = _pages_in(outline_path)
    print(f"【S5】{uid} 产片（--from-plan）…")
    print(f"   构建目录：{work_root}（短路径；完成后归档到 {run.rel(archive)}）")
    print(f"   页数：大纲实际 {pages} 页（已用 --pages 显式锁定，不依赖 sidecar 默认值）")
    print(f"   配图：{args.image_source}｜推理：exec={args.reasoning_exec} "
          f"repair={args.reasoning_repair}")

    started = time.time()
    code, output = _run_pipeline_stream(cmd)
    if code != 0:
        _archive(work_root, archive)                 # 失败也归档：日志/中间的诊断材料要留
        run.step_done(f"s5_render:{uid}", ok=False, artifacts=[],
                      notes=[output[-800:]],
                      extra={"returncode": code, "output_root": run.rel(archive)})
        print(f"   ✗ pipeline 退出码 {code}")
        print(f"     看：{run.rel(archive)}/validation/workflow.log 末尾的 [ERROR]")
        return None

    pptx = _newest_pptx(work_root)
    if not pptx:
        _archive(work_root, archive)
        run.step_done(f"s5_render:{uid}", ok=False, artifacts=[],
                      notes=["未找到 .pptx", output[-600:]],
                      extra={"returncode": code, "output_root": run.rel(archive)})
        print(f"   ✗ 未导出 .pptx（pipeline 已退出但没产物）")
        print(f"     看：{run.rel(archive)}/validation/workflow.log")
        return None

    deliverable = publish(pptx, target, run)         # 先取交付件（design_spec 还在旁边）
    if pptx.stat().st_mtime < started - 60:          # 本次没新写，拿的是上次残留
        print(f"   ⚠️ 产物疑似上一次残留（mtime {pptx.stat().st_mtime:.0f} < 开工时间）："
              f"{pptx.name}")
        run.note(f"S5 {uid} 交付件可能是上次残留：{pptx.name}")
    _archive(work_root, archive)                     # 再把工作现场归档回课程目录
    run.step_done(f"s5_render:{uid}", ok=True,
                  artifacts=[deliverable, *_sidecars(run, deliverable)],
                  extra={"output_root": run.rel(archive),
                         "deliverable": run.rel(deliverable)})
    return deliverable


def _work_root(run: Run, uid: str) -> Path:
    """本次构建的短 ASCII 目录：``TeachingFlow/.work/<run-hash>_<uid>``。

    不能用 ``<课程>/05_ppt_<uid>``：课程名是中文长句，再加项目 slug、``exports``、
    ``.pptx-build-<hex>/pptx_content/ppt/slideMasters/_rels/…`` 就超过 Windows 260 字符，
    svg_to_pptx 会在建目录/写 rels 时抛 FileNotFoundError。构建完再整个移进课程目录，
    挪动本身是顶层 rename，不受内部深度影响。
    """
    key = hashlib.sha1(str(run.dir).encode("utf-8")).hexdigest()[:8]
    p = WORK_ROOT / f"{key}_{uid}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _newest_pptx(work_root: Path) -> Path | None:
    """只在**本次构建目录**里找 pptx，并硬性断言它没跑到 run 外面去。"""
    hits = [p for p in work_root.rglob("*.pptx") if not p.name.startswith("~$")]
    if not hits:
        return None
    newest = max(hits, key=lambda p: p.stat().st_mtime)
    resolved, root = newest.resolve(), work_root.resolve()
    if root not in resolved.parents:
        raise RuntimeError(f"拒绝交付 run 目录之外的产物：{resolved}")
    if "ppt-master" in str(resolved).lower():        # 共享 projects 里的别人旧文件
        raise RuntimeError(f"拒绝交付 ppt-master 里的旧产物：{resolved}")
    return newest


def _archive(work_root: Path, archive: Path) -> Path | None:
    """把构建目录里的项目子目录移进课程目录；失败则保留原位并告警。"""
    archive.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for child in sorted(p for p in work_root.iterdir() if p.is_dir()):
        _drop_transient(child)                       # 清掉 .pptx-build-* 等临时目录
        dst = archive / child.name
        n = 2
        while dst.exists():
            dst = archive / f"{child.name}-{n}"
            n += 1
        try:
            shutil.move(str(child), str(dst))
            moved.append(dst)
        except OSError as exc:
            print(f"   ⚠️ 归档失败（{type(exc).__name__}）：{child} → {dst}")
    try:
        work_root.rmdir()                            # 空了就清掉
    except OSError:
        pass
    if moved:
        print(f"   ✓ 工作现场已归档：{moved[-1].name}")
    return moved[-1] if moved else None


def _drop_transient(project_dir: Path) -> None:
    """建包临时目录（``exports/.pptx-build-*``）不必归档：

    它们嵌套最深（``pptx_content/ppt/slideMasters/_rels/…``），留着会让归档后的路径
    又撞上 260 字符上限，而且里面只有导出中间态。删不掉就忽略（失败不影响交付）。
    """
    try:
        for p in list(project_dir.rglob(".pptx-build-*")):
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def publish(src: Path, target: dict, run: Run) -> Path:
    """把 pipeline 产出的 pptx 按课程命名交到课程目录，并导出讲稿/页面说明。

    ``src`` 所在目录拷后不删：``design_spec.md`` / ``notes/`` 就在它旁边，
    页面说明导出要从那里读；归档由调用方随后完成。交付件是副本。
    """
    dst = naming.deliverable_path(run.dir, target)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if Path(src).resolve() != dst.resolve():
        shutil.copy2(src, dst)

    for fn, out in ((_export_narration, naming.narration_path(dst)),
                    (_export_pages, naming.pages_path(dst))):
        try:
            fn(src, out, target.get("unit_title") or "")
        except Exception as exc:                     # noqa: BLE001
            print(f"   ⚠️ 文本导出失败（{type(exc).__name__}）：{exc}")

    print(f"   ✓ 交付件：{run.rel(dst)}")
    return dst


def _sidecars(_run: Run, deliverable: Path) -> list[Path]:
    return [p for p in (naming.narration_path(deliverable),
                        naming.pages_path(deliverable)) if p.is_file()]


# ---------------------------------------------------------------- 文本导出
def _pages_in(outline_path: Path) -> int:
    """大纲里实际写了多少页（S5 以它作为唯一的页数来源）。"""
    try:
        return gates.count_pages(Path(outline_path).read_text(encoding="utf-8"))
    except OSError:
        return 0


def build_cmd(target: dict, outline_path: Path, out_root: Path, args) -> list[str]:
    """拼 pipeline 命令。**页数总是显式传**：

    ``pipeline.py`` 的 ``teacher_ppt`` 档案自带 ``defaults[\"页数要求\"] = 10``；
    ``--from-plan`` 时它用 ``{**defaults, **sidecar.inputs}`` 构造校验 case。
    Sidecar 一旦缺失/损坏，页数会静默退回 10，而大纲里明明是别的页数，
    于是复检多出「页数=声明」缺陷、白跑一轮降级交接。
    所以这里按**大纲实际页数**传 ``--pages``，把这条依赖彻底切断。
    """
    cmd = [str(outline_path), "--from-plan", str(outline_path),
           "--role", "teacher_ppt",
           "--output-root", str(out_root)]
    pages = _pages_in(outline_path)
    if pages:
        cmd += ["--pages", str(pages)]
    cmd += ["--reasoning-exec", args.reasoning_exec,
            "--reasoning-repair", args.reasoning_repair,
            "--image-source", args.image_source,
            "--image-review", args.image_review,
            "--image-max-candidates", str(getattr(args, "image_max_candidates", 8))]
    providers = getattr(args, "image_providers", None)
    if providers:
        cmd += ["--image-providers", str(providers)]
    return cmd


def _export_narration(pptx: Path, out: Path, topic: str):
    _ensure_path()                                    # 必须先插 path，再 import
    from export_notes import export_narration_md      # 复用 集成路由V1.0 的实现
    return export_narration_md(pptx, out_path=out, topic=topic)


def _export_pages(pptx: Path, out: Path, topic: str):
    _ensure_path()
    from export_notes import export_pages_md
    return export_pages_md(pptx, out_path=out, topic=topic)


def _ensure_path() -> None:
    p = str(ROUTER_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
