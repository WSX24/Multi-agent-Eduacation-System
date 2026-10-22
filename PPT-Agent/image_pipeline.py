#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.6 取图阶段：把 design_spec.md §VIII 的资源行变成 images/ 里的真实文件。

设计原则（与 staged.py 一致）：**把算术和格式从模型手里拿给代码。**

模型只负责两件事：
    1. 判断某一页要不要真实影像（不要就别写行）
    2. 写「意图」——Purpose / Reference（Reference 里可带英文变体，见下）

代码负责其余全部机械活：
    · 解析 §VIII 表、逐行派发（web / ai / user）
    · 调 image_search.py / image_gen.py，带重试与 provider 回退
    · 下载完回填 Dimensions / Ratio / Status（模型填不准的列）
    · 把结果投影进 spec_lock.md 的 `## images`（source=/crop= 必须与 §VIII 逐项一致）
    · 失败降级为 Needs-Manual，绝不中断整条流水线

## Reference 里的中英双查询

Pexels / Pixabay / Wikimedia 对英文关键词命中率更高，但 §VIII 锁的是中文意图。
所以约定：Reference 末尾可跟一个括号英文短语，取图时拆成主查询 + 变体。

    宝黛初会工笔人物画（Dream of the Red Chamber, Bao-yu and Dai-yu, classical Chinese painting）

## 契约（来自 svg_quality/checker.py，改这里前先读它）

    §VIII 行     Status 必须是终态：web → Sourced / Needs-Manual
                Crop Policy ∈ {adaptive, no-crop}
                Image pattern 非空
    spec_lock    `- <key>: images/<file> | source=<via> | crop=<policy>`
                source 必须小写且等于 §VIII 的 Acquire Via
    SVG          `<image href="images/<file>">`
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: §VIII 小节标题
SECTION_RE = re.compile(r"^##\s+VIII\.\s+Image Resource List[^\n]*$", re.M)
#: 下一节标题（用来界定 §VIII 的边界）
NEXT_SECTION_RE = re.compile(r"^##\s+(?![IVX]+\.\s*Image Resource)[^\n]*$", re.M)
#: Reference 末尾的英文变体：`中文意图（English variant）`
VARIANT_RE = re.compile(r"[（(]\s*([A-Za-z][^（()）]*?)\s*[)）]\s*$")

#: 参与取图的 Acquire Via
FETCH_ACQUIRE = {"web", "ai"}

#: 默认 provider 顺序：先直连可用的国内/商业源，Wikimedia 放最后（需要代理）
DEFAULT_PROVIDERS: Tuple[str, ...] = ("shuge", "met", "pexels", "pixabay", "wikimedia", "openverse")

#: 书格（shuge.org）是 WordPress，有 REST API，国内直连 0.1s。
#: 授权：书籍本身公有领域；影像默认 CC BY-NC 4.0（教学/非商用可用，需署名），部分 CC0。
SHUGE_API = "https://www.shuge.org/wp-json/wp/v2"
SHUGE_UA = {"User-Agent": "Mozilla/5.0 (compatible; ppt-agent/1.0)"}
#: 书格图床缩略图后缀：`xxx-1000x1652.jpg` → 去掉即原图
SHUGE_THUMB_RE = re.compile(r"-\d{2,4}x\d{2,4}(?=\.(?:jpg|jpeg|png|webp)$)", re.I)
#: 书格书页前面几幅通常是装帧照/扉页/题咏（实测 红楼梦图咏 01-07 均不是人物图）。
#: 跳过它们再取，命中人物图的概率大幅提升。
SHUGE_SKIP_FRONT = 7

#: 同一本书已被取过几张 → 下一张往后错开（进程内有效，一次产出足够）
_SHUGE_USED: Dict[str, int] = {}

#: 站点自身装饰图（logo/头像/图标/横幅），不是书页扫描
SHUGE_CHROME_RE = re.compile(
    r"shugeorg|cropped-|logo|avatar|favicon|icon-|banner|footer|header|ads?-|watermark", re.I)

#: 单行最多尝试的 provider 数 / 每个 provider 的重试次数
DEFAULT_PROVIDER_TRIES = 4
DEFAULT_RETRIES = 2

#: 低于这个尺寸的图直接判废（PPT 16:9 全幅需要足够像素）
MIN_WIDTH = 1200
MIN_HEIGHT = 800

#: 代理环境变量名。只对**取图子进程**生效，不影响 DeepSeek 主链。
#: 实测（2026-10-18）：本机 127.0.0.1:7897 可稳定访达 Wikimedia；直连时段性超时。
PROXY_ENV = "PPT_IMAGE_PROXY"
#: 重试间隔（秒）。连打无用：网络是**时段性**不稳定，需要隔开时间再试。
RETRY_SLEEP = 2.5

#: 缺 Image pattern 时的兜底（该列在 current contract 下不允许为空）
DEFAULT_PATTERN = "#P1-01 单张主图，图占版面主导、文字压边或留白区"

#: 标题里出现这些词，几乎可以确定是「书的封面/扫描/展柜照」，不是可上版面的图
JUNK_TITLE_RE = re.compile(
    r"cover|binding|spine|frontispiece|book\b|scan\b|photograph of|"
    r"display|exhibit|museum|manuscript page|title page", re.I)
#: 博物馆类命名惯例 `作者 - 作品名`，命中即加分（这类通常是干净的艺术品图）
ARTIST_TITLE_RE = re.compile(r"\s[-–—]\s")


# ---------------------------------------------------------------------------
# §VIII 表：解析 / 重写
# ---------------------------------------------------------------------------
@dataclass
class ImageRow:
    """一行 §VIII 资源。保留原始列名，schema 加列也不会丢。"""

    cells: Dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str:
        return (self.cells.get(name) or "").strip()

    def set(self, name: str, value: str) -> None:
        self.cells[name] = value

    # —— 检查器读取的列 ——
    @property
    def filename(self) -> str:
        return Path(self.get("Filename")).name

    @property
    def acquire(self) -> str:
        return self.get("Acquire Via").lower()

    @property
    def status(self) -> str:
        return self.get("Status")

    @property
    def crop(self) -> str:
        return self.get("Crop Policy").lower() or "adaptive"

    @property
    def pattern(self) -> str:
        return self.get("Image pattern") or self.get("Layout pattern")

    @property
    def reference(self) -> str:
        return self.get("Reference")

    @property
    def page_role(self) -> str:
        return self.get("page_role")


def _split_row(line: str) -> List[str]:
    return [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]


def parse_image_table(spec_text: str) -> Tuple[List[str], List[ImageRow]]:
    """返回 (§VIII 列头, 行)。表不存在或为空时返回 ([], [])。"""
    m = SECTION_RE.search(spec_text)
    if not m:
        return [], []
    start = m.end()
    nxt = NEXT_SECTION_RE.search(spec_text, start)
    body = spec_text[start: nxt.start() if nxt else len(spec_text)]

    header: List[str] = []
    rows: List[ImageRow] = []
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if not header:
            if any(c.lower() == "filename" for c in cells):
                header = cells
            continue
        if set(c.replace("-", "").strip() for c in cells) == {""}:
            continue
        row = {header[i]: (cells[i] if i < len(cells) else "") for i in range(len(header))}
        if any(v.strip() for v in row.values()):
            rows.append(ImageRow(row))
    return header, rows


def render_image_table(header: Sequence[str], rows: Sequence[ImageRow]) -> str:
    """把表渲染回 Markdown（列顺序按 header，缺失值补空）。"""
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        cells = [row.cells.get(name, "").strip() for name in header]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def write_image_table(spec_path: Path, header: Sequence[str],
                      rows: Sequence[ImageRow]) -> None:
    """把更新后的表写回 design_spec.md 的 §VIII 小节。"""
    text = spec_path.read_text(encoding="utf-8")
    m = SECTION_RE.search(text)
    if not m:
        raise RuntimeError(f"{spec_path} 里找不到 §VIII 小节")
    start = m.end()
    nxt = NEXT_SECTION_RE.search(text, start)
    end = nxt.start() if nxt else len(text)

    new_body = "\n\n" + render_image_table(header, rows) + "\n\n"
    spec_path.write_text(text[:start] + new_body + text[end:], encoding="utf-8")


# ---------------------------------------------------------------------------
# 查询词
# ---------------------------------------------------------------------------
def split_query_variants(reference: str) -> Tuple[str, List[str]]:
    """把 `实体｜查询（English variant）` 拆成主查询 + 变体列表。

    ⚠️ **实体不能只当门槛、不进检索词**（旧注释就是这么写的，是个坑）：
    若模型把 Reference 写成 `李清照｜画像 古画`（实体段没在查询里重复），
    以前发出去的检索词就只剩「画像 古画」——拿纯类型词去搜图，必然漂移。
    现在：查询里没有实体时，**把实体补到最前面**。
    """
    reference = (reference or "").strip()
    subject = ""
    if "｜" in reference:
        raw_subject, reference = reference.split("｜", 1)
        # 剥掉「画像/古画/照片」这类通用后缀，避免把类型词又带回检索词
        subject = _core_subject(raw_subject)
        reference = reference.strip()
    variants: List[str] = []
    m = VARIANT_RE.search(reference)
    if m:
        variants.append(m.group(1).strip())
        reference = reference[: m.start()].strip()
    main = reference
    if subject and subject not in main:
        main = f"{subject} {main}".strip()
    return main, [v for v in variants if v]


#: Reference 里的「备选查询」分隔符：`岳飞｜岳飞 宋代 画像（…）｜备选：水墨 雪山 关隘（…）`
_FALLBACK_RE = re.compile(r"[|｜]\s*备选\s*[:：]\s*(.+)$", re.S)


def split_fallback(reference: str) -> Tuple[str, str]:
    """把 Reference 拆成 (主查询, 备选查询)。

    为什么需要它（2026-10-22 用户口径「肯定优先匹配人名」）：
    人物页先按人名搜（万一图源里真有画像/图传/博物馆藏品），但人名命中率很低——
    书格是古籍全文检索，搜「岳飞」返回《孟子》《孝经》。所以模型必须同时给一条
    **意象备选**（水墨 雪山 关隘…），主查询全部图源失败后自动改用备选，
    而不是因为「搜不到画像」就整页空着、或者去配猫。

    没有备选标记时原样返回（向后兼容）。
    """
    ref = (reference or "").strip()
    m = _FALLBACK_RE.search(ref)
    if not m:
        return ref, ""
    return ref[: m.start()].strip(), m.group(1).strip()


# ---------------------------------------------------------------------------
# 图片尺寸
# ---------------------------------------------------------------------------
def probe_size(path: Path) -> Optional[Tuple[int, int]]:
    """读真实像素尺寸；读不出返回 None。"""
    try:
        from PIL import Image
    except ModuleNotFoundError:  # pragma: no cover
        return None
    try:
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001 —— 坏图就当没有
        return None


def ratio_text(width: int, height: int) -> str:
    from math import gcd
    g = gcd(width, height) or 1
    return f"{width // g}:{height // g}"


# ---------------------------------------------------------------------------
# 取图：web
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    ok: bool
    note: str
    source: str = ""
    needs_selection: bool = False
    #: 该结果的标题/标签等元数据，用于「相关性闸门」判断是否真的对得上
    match_text: str = ""
    #: 人工索引点名（意象 → 书格书页）命中。这类候选的「对不对」由索引负责，
    #: 不再过文字相关性/实体闸门——《关山积雪图卷》与「雪山关隘」在字面上是 0 命中，
    #: 但那就是对的图（2026-10-22 岳飞那册 p09 实测）。风格闸门仍然生效。
    trusted: bool = False


def fetch_web(project: Path, row: ImageRow, skill_dir: Path,
              providers: Sequence[str] = DEFAULT_PROVIDERS,
              retries: int = DEFAULT_RETRIES,
              review: str = "score",
              max_candidates: int = 8,
              timeout: int = 60,
              query_override: str = "") -> FetchResult:
    """取一张 web 图。失败不抛异常，返回 ok=False。

    review:
        "auto"   —— 不审图，直接让脚本按元数据挑（旧行为，容易拎到废图）
        "score"  —— 先下候选集，按元数据评分挑最好的一张 promote（默认）
        "manual" —— 只下候选集 + 审图拼图，Status 置 Needs-Selection，等人选
    """
    script = skill_dir / "scripts" / "image_search.py"
    if not script.is_file():
        return FetchResult(False, f"找不到 {script}")

    if query_override:
        # 显式指定查询（如实体短名），用于人物/文物类的第二轮
        query = query_override
        variants = []
    else:
        main_query, variants = split_query_variants(row.reference)
        if _is_arts_row(row) and variants:
            # 人物/文物/版画：英文变体带作品类型词（ukiyo-e / woodblock / painting），
            # 比裸人名更容易命中「艺术品」而不是「实景照」；同时把中文实体和
            # 英文前两词作为变体一起聚合，避免长串 0 候选。
            query = variants[0]
            extra: List[str] = []
            if main_query:
                extra.append(main_query)
            words = re.findall(r"[A-Za-z][A-Za-z\-']+", query)
            if len(words) > 2:
                extra.append(" ".join(words[:2]))
            variants = extra
        elif variants:
            # 英文优先当主查询：实测 Wikimedia 对长中文描述基本乱匹配
            query = variants[0]
            variants = [main_query] if main_query else []
        else:
            query = main_query or row.get("Purpose") or row.filename
    filename = row.filename or "image.jpg"
    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    notes: List[str] = []
    if review == "auto":
        for provider in list(providers)[:DEFAULT_PROVIDER_TRIES]:
            for attempt in range(1, retries + 1):
                cmd = _base_cmd(script, query, variants, row, filename, images_dir, provider)
                try:
                    r = _run(cmd, timeout)
                except subprocess.TimeoutExpired:
                    notes.append(f"{provider}#{attempt} 超时")
                    continue
                target = images_dir / filename
                if r.returncode == 0 and _ok_file(target):
                    size = probe_size(target)
                    return FetchResult(True, f"{provider} 命中 {size[0]}x{size[1]}", provider)
                notes.append(f"{provider}#{attempt} {_tail(r)}")
        return FetchResult(False, _brief_notes(notes))

    # —— 候选集模式 ——
    for provider in list(providers)[:DEFAULT_PROVIDER_TRIES]:
        for attempt in range(1, retries + 1):
            cmd = _base_cmd(script, query, variants, row, filename, images_dir, provider)
            cmd += ["--save-candidates", "--max-candidates", str(max_candidates)]
            try:
                r = _run(cmd, timeout)
            except subprocess.TimeoutExpired:
                notes.append(f"{provider}#{attempt} 超时")
                continue

            cand_dir = images_dir / "candidates" / Path(filename).stem
            manifest = cand_dir / "candidates.json"
            if r.returncode != 0 or not manifest.is_file():
                notes.append(f"{provider}#{attempt} {_tail(r)}")
                continue

            items = _load_candidates(manifest)
            if not items:
                notes.append(f"{provider}#{attempt} 候选为空")
                continue
            rejected = _load_rejected(project)
            if rejected:
                kept = [it for it in items if _candidate_text(it) not in rejected]
                if not kept:
                    notes.append(f"{provider}#{attempt} 候选均被历史否决")
                    continue
                items = kept

            if review == "manual":
                sheet = cand_dir / "review_sheet.jpg"
                return FetchResult(False,
                                   f"已备候选 {len(items)} 张，审图拼图：{sheet}（Status=Needs-Selection 待选）",
                                   provider, needs_selection=True)

            best = max(items, key=lambda it: rank_key(it, row))
            cand_name = best.get("file") or best.get("filename") or ""
            if not cand_name:
                notes.append(f"{provider}#{attempt} 候选缺文件名")
                continue
            promote = [sys.executable, str(script), "--filename", filename,
                       "-o", str(images_dir), "--provider", provider,
                       "--promote", Path(str(cand_name)).name]
            try:
                pr = _run(promote, timeout)
            except subprocess.TimeoutExpired:
                notes.append(f"{provider}#{attempt} promote 超时")
                continue
            target = images_dir / filename
            if pr.returncode == 0 and _ok_file(target):
                # 清掉暂存产物：候选缩略图与 .review 预览图不该留在项目里
                shutil.rmtree(images_dir / "candidates", ignore_errors=True)
                shutil.rmtree(images_dir / ".review", ignore_errors=True)
                size = probe_size(target)
                match_text = " ".join(
                    str(best.get(k) or "") for k in ("title", "tags", "description"))
                best_score = score_candidate(best, row)
                _, style_why = style_preference(row, best)
                nhit, nall = len(_zh_hits(row.reference or "", match_text)[0]), \
                    len(_zh_hits(row.reference or "", match_text)[1])
                return FetchResult(
                    True,
                    f"{provider} 候选 {len(items)} 选 1（命中 {nhit}/{nall}；"
                    f"{best_score:.1f} 分；风格 {' '.join(style_why) or '无偏好命中'}）"
                    f"{size[0]}x{size[1]}｜{str(best.get('title') or '')[:40]}",
                    provider, match_text=match_text)
            notes.append(f"{provider}#{attempt} promote 失败：{_tail(pr)}")
    return FetchResult(False, _brief_notes(notes))


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    """跑取图子进程。两道防护：

    · `PPT_IMAGE_PROXY` → 只给取图子进程加 HTTPS_PROXY/HTTP_PROXY，
      避免把 DeepSeek 主链也绕进代理（那会变慢甚至断）。
    · 每次调用后 sleep(RETRY_SLEEP)：网络是时段性的，连打无效，需要隔开时间。
    """
    import time as _t
    env = dict(os.environ)
    proxy = (os.getenv(PROXY_ENV) or "").strip()
    if proxy:
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[key] = proxy
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    finally:
        _t.sleep(RETRY_SLEEP)


def _base_cmd(script: Path, query: str, variants: Sequence[str], row: ImageRow,
              filename: str, images_dir: Path, provider: str) -> List[str]:
    cmd = [sys.executable, str(script), query,
           "--filename", filename,
           "-o", str(images_dir),
           "--provider", provider,
           "--min-width", str(MIN_WIDTH),
           "--min-height", str(MIN_HEIGHT)]
    for variant in variants[:2]:
        cmd += ["--query-variant", variant]
    if row.page_role:
        cmd += ["--slide", row.page_role]
    if row.get("Purpose"):
        cmd += ["--purpose", row.get("Purpose")]
    return cmd


def _manifest_match_text(project: Path, filename: str) -> str:
    """从 image_sources.json 取该图的标题/标签，供相关性闸门使用。"""
    f = project / "images" / "image_sources.json"
    if not f.is_file():
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    for item in data.get("items", []):
        if str(item.get("filename") or "") == filename:
            # 只拼候选**自身**的元数据。绝不能带 search_query：它就是我们的查询词，
            # 拼进来会让「实体命中」门槛永远通过（实测 2026-10-19：封面查「雪山」
            # 取回蝙蝠与月亮图，仍被判相关）。
            return " ".join(str(item.get(k) or "") for k in ("title", "tags", "description"))
    return ""


def _brief_notes(notes: List[str]) -> str:
    """每个 provider 只留一条，避免把真正有用的首条（wikimedia）挤掉。"""
    seen: Dict[str, str] = {}
    for n in notes:
        provider = n.split("#", 1)[0].split(" ", 1)[0]
        seen.setdefault(provider, n)
    return "；".join(seen.values()) or "无可用链接"


def _ok_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 8192


def _tail(proc) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1][:70] if lines else "无输出"



def _candidate_text(item: Dict[str, object]) -> str:
    return " ".join(str(item.get(k) or "") for k in ("title", "tags", "description"))


def _load_rejected(project: Path) -> set:
    """人工/闸门否决过的候选元数据。同一张图不再被选中。"""
    f = project / "images" / "rejected.json"
    if not f.is_file():
        return set()
    try:
        items = json.loads(f.read_text(encoding="utf-8")).get("items", [])
    except (json.JSONDecodeError, OSError):
        return set()
    return {str(it.get("text") or "") for it in items if isinstance(it, dict) and it.get("text")}


def _record_rejected(project: Path, provider: str, text: str, reason: str,
                     filename: str = "", reference: str = "") -> None:
    """记一条被闸门拒掉的候选。

    `filename` / `reference` 是 2026-10-21 加的：旧记录只有元数据文本，
    回头做离线标定时无法把一条被拒候选对回 §VIII 的哪一行（也就无法算
    「P2 加权能多拦几张」）。新记录带文件名，老记录缺字段也照旧可读。
    """
    if not (text or "").strip():
        return
    with _STATE_LOCK:
        _record_rejected_locked(project, provider, text, reason, filename, reference)


def _record_rejected_locked(project: Path, provider: str, text: str, reason: str,
                            filename: str = "", reference: str = "") -> None:
    f = project / "images" / "rejected.json"
    data = {"items": []}
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {"items": []}
    items = data.setdefault("items", [])
    if not isinstance(items, list):
        items = data["items"] = []
    body = text[:500]
    # 去重键带上 filename：同一段元数据可能在不同页被拒（两页要同一种图），
    # 只按 text 去重会丢掉后一页的归属，离线标定时就没法把二者对齐。
    known = {(str(it.get("filename") or ""), str(it.get("text") or "")): it
             for it in items if isinstance(it, dict)}
    if (filename, body) in known:
        return
    # 老记录（无 filename）同文本：就地补文件名，不重复计量也不丢新信息
    legacy = known.get(("", body))
    if legacy is not None and filename:
        legacy["filename"] = filename
        legacy["reference"] = reference[:300]
        try:
            f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return
    items.append({"provider": provider, "text": body, "reason": reason,
                  "filename": filename, "reference": reference[:300]})
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _load_candidates(manifest: Path) -> List[Dict[str, object]]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("candidates") or data.get("items") or []
    return [it for it in items if isinstance(it, dict)]


#: 相关性阈值：查询词至少要有这么多比例出现在返回的标题/标签/书名里，
#: 否则判定「没找到对的图」——按用户要求，宁可不配图也不放错图。
MIN_RELEVANCE = 0.34


def relevance(query: str, text: str) -> float:
    """查询与元数据的相关度（0~1）。

    中文用**最长公共子串**判定，比按比例切 2-gram 稳得多：长描述句
    （「孙温绘红楼梦宝黛初会工笔册页」）按比例算只有 22%，会被误判为不相关；
    但它的核心词「红楼梦」与书名完整重合，这显然是对的。
    英文按分词命中。
    """
    q = (query or "").strip()
    t = (text or "").strip().lower()
    if not q or not t:
        return 0.0

    cjk_q = "".join(re.findall(r"[一-鿿]", q))
    best = 0
    if cjk_q:
        # 滚动取查询里的 CJK 子串，求与元数据的最长公共长度。
        # 通用词（画像/照片/中国/水墨…）**跳过不计**：2026-10-22「李清照画像」
        # 收下猫片，唯一的共同点就是「画像」（猫的标签写着「猫画像」）。
        # 注意要跳过通用词后继续找更短的子串，而不是整体放弃——
        # 否则「中国水墨山水」只因最长的公共子串是「中国」就被判 0 分。
        for n in range(len(cjk_q), 1, -1):
            for i in range(len(cjk_q) - n + 1):
                sub = cjk_q[i:i + n]
                if (sub in t and sub not in _GENERIC_ZH_TERMS
                        and sub not in _NEUTRAL_PREFIXES):
                    best = n
                    break
            if best:
                break
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", q.lower())]
    word_hit = any(w in t for w in words)

    if word_hit:
        return 1.0
    if best >= 3:
        return 1.0
    if best == 2:
        return 0.5
    return 0.0


# ---------------------------------------------------------------------------
# P3 风格偏好层：**只排序，永不否决**
# ---------------------------------------------------------------------------
# 过滤优先级（四层，本文件里的实现位置）：
#
#   P0 硬否决   命中即淘汰，不参与加权
#               JUNK_TITLE_RE（封面/扫描/展柜）、_style_mismatch（arts 行的现代城市）、
#               _ok_file（尺寸不足）、used_texts（本册重复）
#   P1 实体锚点 必须命中（_entity_hit）：Reference 的 `实体｜` 前缀
#   P2 题材覆盖 加权求和过线（relevance + _term_coverage）
#   P3 风格偏好 **只影响候选之间的先后**（style_preference）
#
# 为什么 P3 必须只排序、不能否决：风格是「偏好」不是「对错」。
# 文学课件要「荷花月色」，真实荷花照完全可用；若因为「不是水墨」就否决，
# 会打死一整批能用的照片（2026-10-18 注释里有同款教训）。
# 反过来「古画需求里出现摩天大楼」属于**一眼就知道错**，那是 P0，仍由
# _style_mismatch 负责——不要把它挪到这里。

#: P3 权重表。改这里只会改变候选之间的先后顺序，
#: 不会让任何图从「能配」变成「不能配」，也不会救回不该配的图。
STYLE_W: Dict[str, float] = {
    "medium_match": 2.0,      # 元数据说是画/版画/水墨，与 arts 需求同向
    "medium_off": -1.5,       # 元数据明说是照片/图库素材，arts 行排后面
    "modern_trace": -1.0,     # 带现代痕迹，压排序（不否决）
    "modern_trace_arts": -2.5,  # 古画/文物需求上的现代痕迹：加倍降权（仍不否决）
    "license_pd": 3.0,        # 公有领域：版面上不用压署名
    "license_attr": -0.5,     # 必须署名：多一道人工
    "artist_title": 2.0,      # 博物馆命名惯例 `作者 - 作品名`
    "portrait_square": 1.0,   # 竖幅/近方，课件里更好摆
    "resolution": 1.5,        # 分辨率（宽度上限归一）
}
#: 分辨率归一参考宽度：超过这个宽度不再加分
STYLE_RESOLUTION_REF = 4000

#: 元数据里表示「这是绘画/版画/水墨」的词
MEDIUM_ART_HINTS = (
    "水墨", "工笔", "写意", "设色", "白描", "绢本", "纸本", "版画", "木刻",
    "年画", "壁画", "绣像", "插画", "古画", "国画",
    "ink", "wash", "woodblock", "woodcut", "ukiyo", "painting", "engraving",
    "etching", "lithograph", "drawing", "artwork",
)
#: 元数据里表示「这是照片/图库素材」的词
MEDIUM_PHOTO_HINTS = (
    "照片", "摄影", "实拍", "写真", "stock", "photo", "photograph",
    "shutterstock", "getty", "alamy", "freepik",
)


def style_preference(row: Optional[ImageRow],
                     item: Dict[str, object]) -> Tuple[float, List[str]]:
    """P3 风格偏好打分。返回 (分数, 理由标签)。

    契约：**永不否决**。理由标签只用于日志/审计，不参与 pass/fail。
    row 为 None 时只做与需求无关的通用偏好（授权/构图/分辨率）。
    """
    score = 0.0
    why: List[str] = []

    def add(key: str, label: str) -> None:
        nonlocal score
        w = STYLE_W[key]
        score += w
        why.append(f"{label}{w:+.1f}")

    text = _candidate_text(item)
    tl = text.lower()
    title = str(item.get("title") or "")

    # 画种：只在明确是「画/文物/版画」的需求上才有意义
    if row is not None and _is_arts_row(row):
        art_hit = any(k in text or k in tl for k in MEDIUM_ART_HINTS)
        photo_hit = any(k in text or k in tl for k in MEDIUM_PHOTO_HINTS)
        if art_hit and not photo_hit:
            add("medium_match", "画种")
        elif photo_hit and not art_hit:
            add("medium_off", "照片")

    # 现代痕迹：软惩罚。硬否决只在 `_style_mismatch`（P0，建筑级意象），
    # 这里管排序。古画/文物类与「古诗古文」类都加倍——这类主题上
    # 「城市/现代/写真」虽不否决，但要靠后；真实荷花照不会被扣分（见 `_CLASSICAL_HINTS` 注释）。
    if any(k in text or k in tl for k in _MODERN_SOFT_HINTS):
        stronger = (_is_arts_row(row) or _is_classical_row(row)) if row else False
        add("modern_trace_arts" if stronger else "modern_trace", "现代痕迹")

    if ARTIST_TITLE_RE.search(title):
        add("artist_title", "署名")

    tier = str(item.get("license_tier") or "").lower()
    if tier == "no-attribution":
        add("license_pd", "PD")
    elif tier:
        add("license_attr", "需署名")

    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    if width:
        w = min(width, STYLE_RESOLUTION_REF) / STYLE_RESOLUTION_REF * STYLE_W["resolution"]
        score += w
        why.append(f"分辨率+{w:.1f}")
    if height:
        ar = width / height
        if 0.55 <= ar <= 1.25:            # 竖幅/近方：课件里更好摆
            add("portrait_square", "竖幅")
    return score, why


def score_candidate(item: Dict[str, object],
                    row: Optional[ImageRow] = None) -> float:
    """候选排序分 = P0 废图罚分 + P3 风格偏好。**不参与 pass/fail**。

    只基于元数据，不靠视觉，所以只是「排序」不是「审图」。
    实测教训（2026-10-18）：不审图时元数据排序会把「书的封面照」排在第一位，
    所以废图特征（cover/scan/展柜）在这里重罚——这是 P0 语义（一眼就知道错），
    只是它作用在「挑哪一张」而不是「要不要这一张」。
    其余偏好全部来自 style_preference，只调先后。
    """
    title = str(item.get("title") or "")
    score = 0.0
    if JUNK_TITLE_RE.search(title):
        score -= 10.0                     # P0：封面/扫描/展柜，不是可上版面的图
    bonus, _ = style_preference(row, item)
    return score + bonus


def rank_key(item: Dict[str, object],
             row: Optional[ImageRow] = None) -> Tuple[int, float]:
    """候选排序键 = (命中词数, P3 风格分)。**命中数优先**。

    用户口径（2026-10-21）：
      · 一个词都不中 → 根本进不到这一步（`_term_coverage` 已先排除）；
      · 全中最好；没全中时，中的词多者优先级高；
      · 风格（画种/竖幅/PD/分辨率）只在命中数相同时当次级排序。

    注意：命中数**只用于排序，不做否决**。即使命中 0 个，本函数也照常返回分数，
    否决是 `_term_coverage` 的事——两层职责不混。
    """
    style = score_candidate(item, row)
    if row is None:
        return 0, style
    hits, _ = _zh_hits(row.reference or "", _candidate_text(item))
    return len(hits), style


# ---------------------------------------------------------------------------
# 取图：from-url（人工/外部搜到的直链入库）
# ---------------------------------------------------------------------------
def _append_source_record(project: Path, filename: str, **fields) -> bool:
    """把一个图的溯源写进 images/image_sources.json（合并，不覆盖其他条目）。

    为什么不复用 image_search.py --from-url：它会**再下载一次**图片，而
    Pixabay 的 largeImageURL 是临时链接，二次下载经常失败；失败被我吞掉后，
    成品里就有图无溯源，导出时被 checker 判 `image_sources_missing` 挡住
    （2026-10-18 光合作用那次就是这么挂的）。
    """
    f = project / "images" / "image_sources.json"
    with _STATE_LOCK:
        return _append_source_record_locked(f, filename, fields)


def _append_source_record_locked(f: Path, filename: str, fields: Dict[str, object]) -> bool:
    data: Dict[str, object] = {"items": []}
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {"items": []}
    items = data.get("items")
    if not isinstance(items, list):
        items = data["items"] = []
    record = {"filename": filename, **{k: v for k, v in fields.items() if v not in (None, "")}}
    items[:] = [it for it in items if not (isinstance(it, dict) and it.get("filename") == filename)]
    items.append(record)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def _remove_source_record(project: Path, filename: str) -> None:
    """删掉某个图的溯源条目（相关性闸门拒绝后调用，避免留下假记录）。"""
    with _STATE_LOCK:
        _remove_source_record_locked(project / "images" / "image_sources.json", filename)


def _remove_source_record_locked(f: Path, filename: str) -> None:
    if not f.is_file():
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    items = data.get("items")
    if not isinstance(items, list):
        return
    data["items"] = [it for it in items
                     if not (isinstance(it, dict) and it.get("filename") == filename)]
    try:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def fetch_from_url(project: Path, row: ImageRow, url: str,
                   skill_dir: Path, timeout: int = 180) -> FetchResult:
    """拿一个图片直链入库（--from-url）。

    用途：搜图这件事由「能看见图的人/工具」做（浏览器、搜索框、或模型侧的 web search），
    本函数只负责把选定的那张图落到 images/ 并写好溯源。
    许可档位是 `manual`（权利与署名由使用者自负），因此页面不强制署名。
    """
    script = skill_dir / "scripts" / "image_search.py"
    if not script.is_file():
        return FetchResult(False, f"找不到 {script}")
    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(script), "--from-url", url,
           "--filename", row.filename, "-o", str(images_dir)]
    try:
        r = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return FetchResult(False, "下载超时")
    target = images_dir / row.filename
    if r.returncode == 0 and _ok_file(target):
        size = probe_size(target)
        return FetchResult(True, f"from-url {size[0]}x{size[1]}", "url")
    return FetchResult(False, _tail(r))


def ensure_row(spec_path: Path, filename: str, page_role: str = "",
               purpose: str = "", acquire: str = "web",
               crop: str = "adaptive") -> ImageRow:
    """确保 §VIII 里有这一行（没有就建），返回该行。"""
    header, rows = parse_image_table(spec_path.read_text(encoding="utf-8"))
    for row in rows:
        if row.filename == filename:
            return row
    row = ImageRow({
        "Filename": filename,
        "Dimensions": "—",
        "Ratio": "—",
        "Purpose": purpose or "配图",
        "Type": "Photography",
        "Image pattern": DEFAULT_PATTERN,
        "Crop Policy": crop,
        "Acquire Via": acquire,
        "Status": "Pending",
        "Reference": purpose or "外部选定图片",
        "text_policy": "—",
        "page_role": page_role or "—",
    })
    if not header:
        header = list(row.cells.keys())
    write_image_table(spec_path, header, rows + [row])
    return row


# ---------------------------------------------------------------------------
# 取图：书格（shuge.org）——国内直连，不需代理，有中国古画/古籍
# ---------------------------------------------------------------------------

#: 人工索引：书格某本书里「哪一张是什么」——自动挑图在扫描古籍上不可靠
#: （一页图版、一页题咏交替，且无元数据；墨迹密度也分不开，实测 15/17 比
#: 主题咏页还密），所以对高频书做一次人工标注，按需点名取图。
SHUGE_INDEX: Dict[str, Dict[str, List[str]]] = {
    # 《红楼梦图咏》（改琦）：编号 → 可用图版；键是意图关键词
    "hong_lou_meng_tu_yong": {
        "宝黛|初会|同框|相见": ["08", "12", "15", "20"],
        "通灵|绛珠|前世|缘起": ["05"],
        "警幻": ["07"],
        "女子|闺秀|裙钗|宝钗|湘云|凤姐": ["10", "14", "17", "20"],
        "书案|书房|读书": ["19"],
    },
}
#: 该书里已知是「题咏文字页」的编号，永远跳过
SHUGE_TEXT_PAGES: Dict[str, set] = {
    "hong_lou_meng_tu_yong": {"01", "02", "03", "04", "06", "09", "13", "16", "18", "21"},
}

#: 人工索引（第二张）：**意象 → 书格书页**。
#:
#: 为什么需要它：书格是**按书名检索**的站，而古画的书名用的是文言命名。
#: 搜「雪山关隘古道」只会返回《王孟津赠汤若望诗册》（书法，完全不对），
#: 真正的雪景山水叫《关山积雪图卷》—— 字面上一个词都不重合，
#: `relevance()` 必然给 0 分，再好的图也会被闸门丢掉（2026-10-22 岳飞那册 p09）。
#: 所以「哪个意象该去翻哪本书」交给人工点单，命中后这张图**不再过文字闸门**
#: （`FetchResult.trusted`），但仍要过风格闸门（现代痕迹/建筑否决）。
#: 键是正则（匹配 Reference + Purpose 全串），值是 (书页, 标签)。
SHUGE_INTENT_BOOKS: List[tuple] = [
    # ⚠️ 这里的正则**必须是两字以上的词**（2026-10-22 事故）：
    # 原来写单字 `水`，结果「水墨 昆虫 草地」因为含「水」命中「江舟」组，
    # 把《龙池竞渡图卷》的引首题字页配到了「蜂儿、蚂蚁、瓢虫」这一页上——货不对板。
    (r"雪山|雪景|积雪|寒林|关山|关隘|边塞|塞外|征战|沙场",
     [("https://www.shuge.org/view/guan_shan_ji_xue_tu_juan/", "关山积雪图卷"),
      ("https://www.shuge.org/view/xi_shan_xue_yi_tu_juan/", "溪山雪意图卷")]),
    (r"山水|远山|群峰|溪山|画卷|长卷|手卷|烟雨|楼阁|亭台|舟楫",
     [("https://www.shuge.org/view/xia_shan_yu_yu_tu_juan/", "夏山欲雨图卷"),
      ("https://www.shuge.org/view/tian_xia_ming_shan_tu/", "天下名山图册")]),
    (r"江舟|扁舟|小舟|渡口|竞渡|烟波|渔舟|汀洲",
     [("https://www.shuge.org/view/long_chi_jing_du_tu_juan/", "龙池竞渡图卷")]),
    (r"园林|山庄|庭院|回廊|古寺|寺庙|亭台",
     [("https://www.shuge.org/view/yu_zhi_bi_shu_shan_zhuang_shi_tu_ce/", "御制避暑山庄诗图册")]),
    (r"折枝|花卉|落红|花鸟|桃夭|杨柳|兰竹|梅花|荷花|牡丹",
     [("https://www.shuge.org/view/song_yuan_ming_ren_hua_niao_he_bi/", "宋元明人花鸟合璧"),
      ("https://www.shuge.org/view/shi_er_yue_hua_shen_ce/", "十二月花神册")]),
    (r"仕女|闺阁|梳妆|对镜|秋千|簪花",
     [("https://www.shuge.org/view/shi_nv_tu_ce/", "诗女图册")]),
    (r"四时|暮色|黄昏|晨雾|月色|幽赏",
     [("https://www.shuge.org/view/si_shi_you_shang/", "四时幽赏")]),
]


def _shuge_index_books(query: str) -> List[tuple]:
    """按意象挑书页：命中哪条就用哪条（多条全用，按声明顺序）。"""
    text = query or ""
    picked: List[tuple] = []
    for pattern, books in SHUGE_INTENT_BOOKS:
        if re.search(pattern, text):
            for url, title in books:
                if (url, title) not in picked:
                    picked.append((url, title))
    return picked


def _shuge_preferred(book: str, hint: str) -> List[str]:
    """按意图关键词从人工索引里取候选编号；没有则返回空。"""
    table = SHUGE_INDEX.get(book) or {}
    for pattern, nums in table.items():
        if re.search(pattern, hint or ""):
            return list(nums)
    return []


def _shuge_probes(query: str) -> List[str]:
    """生成书格检索词。

    书格是**按书名检索**的站，而模型给的 Reference 是一整句描述
    （如「孙温绘《红楼梦》宝黛初会工笔绢本册页」）。实测直接把整句丢进去、
    或者硬切前 4 个字（切出「孙温绘《」），都会 0 命中，于是流程静默回落到
    Pixabay，结果拿到银河、水晶这类完全无关的照片。
    正确做法：优先书名号里的书名，其次整句去标点，最后才退到短前缀。
    """
    probes: List[str] = []
    for m in re.finditer(r"《([^》]{2,20})》", query or ""):
        t = m.group(1).strip()
        if t and t not in probes:
            probes.append(t)
    flat = re.sub(r"[《》〈〉（）()\[\]「」『』]" + "|" + r"[\s，,、；;：:·—\-]", "", query or "")
    if flat and flat not in probes:
        probes.append(flat)
    for n in (6, 4, 3, 2):
        if len(flat) > n:
            head = flat[:n]
            if head not in probes:
                probes.append(head)
    return [p for p in probes if p]


def search_shuge(query: str, limit: int = 3, use_index: bool = True) -> List[Dict[str, str]]:
    """搜书格：先找书（文章），再从书页里抽原图直链。返回候选列表。

    `use_index=False`：不查「意象 → 书页」人工索引。
    **非古画行必须传 False**（2026-10-22 事故：现代散文《我与地坛》P12 要「暮色园林」的
    实景照，却因「暮色」命中索引，被配了一页《四时幽赏》古籍图）。
    """
    import requests

    out: List[Dict[str, str]] = []
    posts: List[dict] = []
    # ① 人工索引：意象 → 书页（最准，优先）——只给古画行用
    if use_index:
        for url, title in _shuge_index_books(query):
            posts.append({"url": url, "title": title, "_indexed": True})
    # ② 常规检索（词命中差，逐级回退）。索引已经给书了就不再多此一举。
    if not posts:
        probes = _shuge_probes(query)
        for probe in probes[:5]:
            try:
                r = requests.get(f"{SHUGE_API}/search",
                                 params={"search": probe, "per_page": limit},
                                 headers=SHUGE_UA, timeout=20)
                if r.ok and r.json():
                    posts = [p for p in r.json() if p.get("url")]
                    if posts:
                        break
            except Exception:  # noqa: BLE001
                continue
    if not posts:
        return out

    cjk_query = "".join(re.findall(r"[一-鿿]", query or "")[:8])
    for post in posts[:max(limit, 4)]:
        indexed = bool(post.get("_indexed"))
        page_url = str(post.get("url"))
        title_cn = str(post.get("title") or "")
        slug = page_url.rstrip("/").rsplit("/", 1)[-1]
        try:
            page = requests.get(page_url, headers=SHUGE_UA, timeout=25)
            if not page.ok:
                continue
        except Exception:  # noqa: BLE001
            continue

        # 相关性：**书名或正文**命中所查的词就算。
        # 只看书名会漏掉大量正确匹配——实测查「林冲」，书格返回《水浒叶子》
        # 《新刻全像水浒传》等（正文都提到林冲），但书名里没有「林冲」二字，
        # 被旧的书名交集规则全部挡掉，结论就成了「林冲找不到」。
        # 反过来，查「紫藤萝花瀑」命中《十二月花神册》时，正文里并不会有
        # 「紫藤」，这样又能正确拒掉。
        # 人工索引点名的书**不跑这句**：《关山积雪图卷》与「雪山关隘」字面零重合。
        if cjk_query and not indexed:
            hay = title_cn + " " + page.text
            if not any(cjk_query[i:i + 2] in hay for i in range(len(cjk_query) - 1)):
                continue

        urls: List[str] = []
        for raw in re.findall(
                r'https://www\.shuge\.org/wp-content/uploads/[^"\'\s>]+\.(?:jpg|jpeg|png|webp)',
                page.text, re.I):
            name = raw.rsplit("/", 1)[-1]
            if SHUGE_CHROME_RE.search(name):        # 站点图标/头像/广告图，不是书页
                continue
            url = SHUGE_THUMB_RE.sub("", raw)       # 缩略图 → 原图
            if url not in urls:
                urls.append(url)
        # 与本书 slug 同名的优先，同组内按文件名里的序号升序（书页顺序）
        def _key(u: str):
            name = u.rsplit("/", 1)[-1]
            num = re.search(r"(\d{1,3})(?=\.[A-Za-z]+$)", name)
            return (0 if slug[:8] in name else 1, int(num.group(1)) if num else 9999)
        urls.sort(key=_key)
        for url in urls:
            out.append({"url": url, "page": page_url,
                        "title": str((post.get("title") or ""))[:60],
                        "trusted": indexed})
        if len(out) >= 40:
            break
    return out


def fetch_shuge(project: Path, row: ImageRow, skill_dir: Path,
                timeout: int = 120) -> FetchResult:
    """从书格取一张图：搜书 → 抽原图 → 取第一张能用的。"""
    import requests

    query = (row.reference or "").strip()
    query = VARIANT_RE.sub("", query).strip() or row.get("Purpose") or row.filename
    candidates = search_shuge(query, use_index=_wants_painting(row))
    if not candidates:
        return FetchResult(False, "书格：无搜索结果")

    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / row.filename

    # 取图顺序：
    #   ① 人工索引点名（最准）→ ② 跳过前置页 + 书内递增偏移（兜底）
    book_key = candidates[0]["page"] if candidates else row.filename
    book_slug = book_key.rstrip("/").rsplit("/", 1)[-1]
    hint = f"{row.get('Purpose')} {row.page_role} {row.reference}"
    prefer = _shuge_preferred(book_slug, hint)
    text_pages = SHUGE_TEXT_PAGES.get(book_slug, set())

    ordered: List[Dict[str, str]] = []
    if any(c.get("trusted") for c in candidates):
        # 人工索引点名的书：**手卷/册页的前后都是引首与题跋（满纸书法）**，画心在中间。
        # 实测《夏山欲雨图卷》9 页：01 带题签、08-09 是跋文。旧的 SHUGE_SKIP_FRONT=7
        # 按「大书」调的，对 9 页小卷直接跳到跋文上——封面就变成了一整屏书法。
        # 所以索引书按比例去头去尾，而不是用固定偏移。
        def _pageno(cand: Dict[str, str]) -> Optional[int]:
            m = re.search(r"(\d{1,3})(?=\.[A-Za-z]+$)", cand["url"].rsplit("/", 1)[-1])
            return int(m.group(1)) if m else None

        nums = sorted({n for c in candidates if (n := _pageno(c)) is not None})
        if len(nums) > 6:
            keep = set(nums[3:-2])
            trimmed = [c for c in candidates if _pageno(c) in keep]
            if trimmed:
                candidates = trimmed
                    # 保留顺序，仅剔除首尾
        # 索引书直接按（去头去尾后的）书页顺序取，不走 SHUGE_SKIP_FRONT 的固定偏移；
        # 同一册里多页命中同一本书时按 nth 轮转，避免两页拿同一张。
        nth = _SHUGE_USED.get(book_key, 0)
        _SHUGE_USED[book_key] = nth + 1
        if candidates:
            shift = nth % len(candidates)
            ordered = candidates[shift:] + candidates[:shift]
            ordered = ordered[:8]
    if prefer:
        # 索引里多张候选时按书内序号轮转，避免三页拿到同一张
        shift = _SHUGE_USED.get(book_key, 0)
        prefer = prefer[shift % len(prefer):] + prefer[:shift % len(prefer)]
        _SHUGE_USED[book_key] = shift + 1
        for num in prefer:
            for cand in candidates:
                if re.search(rf"{num}(?=\.|[a-z-])", cand["url"].rsplit("/", 1)[-1]):
                    if cand not in ordered:
                        ordered.append(cand)
        if ordered:
            print(f"   [书格] 按索引取图：{', '.join(prefer)}", flush=True)
    if not ordered:
        nth = _SHUGE_USED.get(book_key, 0)
        _SHUGE_USED[book_key] = nth + 1
        start = SHUGE_SKIP_FRONT + (nth * 2) % 14
        ordered = [c for c in candidates[start:start + 8]
                   if not any(t in c["url"].rsplit("/", 1)[-1][-8:] for t in text_pages)]
        if not ordered:
            ordered = candidates[start:start + 8]

    for cand in ordered[:8]:
        try:
            r = requests.get(cand["url"], headers=SHUGE_UA, timeout=timeout)
        except Exception:  # noqa: BLE001
            continue
        if not r.ok or len(r.content) < 20000:
            continue
        tmp = images_dir / f"._shuge_{row.filename}"
        tmp.write_bytes(r.content)
        size = probe_size(tmp)
        if not size or size[0] < 800 or size[1] < 600:
            tmp.unlink(missing_ok=True)
            continue
        tmp.replace(target)
        # 溯源：自己写。书格影像默认 CC BY-NC 4.0（教学/非商用可用，需署名），
        # 档位记 manual（页面不强制渲染署名，但日志会提示补一行出处）
        _append_source_record(
            project, row.filename,
            provider="shuge",
            title=str(cand.get("title") or "")[:120],
            license_name="CC BY-NC 4.0",
            license_tier="manual",
            source_page_url=str(cand.get("page") or ""),
            download_url=str(cand.get("url") or ""),
            width=size[0], height=size[1],
            search_query=query,
            attribution_text=f"选自《{cand.get('title') or ''}》，书格 shuge.org，CC BY-NC 4.0",
            status="sourced")
        return FetchResult(True, f"书格 {size[0]}x{size[1]}｜{cand['title'][:24]}", "shuge",
                           match_text=f"{cand['title']} {cand['url'].rsplit('/', 1)[-1]}",
                           trusted=bool(cand.get("trusted")))
    return FetchResult(False, f"书格：{len(candidates)} 张候选均不可用")


# ---------------------------------------------------------------------------
# 取图：The Met 开放数据（纽约大都会博物馆，零 key，CC0）
# ---------------------------------------------------------------------------
# 为什么加它：Pixabay/书格 都没有「仕女 / 花鸟 / 山水 / 楼阁」这类中国古画的
# 系统收藏，导致「李清照画像」这种需求只能配到猫。Met 有约 49 万件公有领域高清图，
# 每件带 isPublicDomain + primaryImage + 年代/材质/文化，授权干净、竖幅多。
#
# 两个必须知道的坑：
#   1. 全馆搜索会把「Chinese lady」配成欧洲铜版画、日本浮世绘
#      → 必须带 departmentId=6（亚洲艺术部）
#   2. 具体中国名人（李清照 / 岳飞）它**一件也没有** → 这类需求仍会降级
MET_API = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_UA = {"User-Agent": "Mozilla/5.0 (compatible; ppt-agent/1.0)"}
#: 亚洲艺术部
MET_ASIAN_DEPT = 6
#: 每次最多看多少个详情（详情接口要一个个拿；实测 PD+有图 的命中率 20%~100%，
#: 所以池子要开大一点，否则「Ming dynasty painting」10 条里只有 2 条可用）
MET_MAX_DETAILS = 20
#: 已经用过的 Met 图（按 URL 记住），避免同册重复
_MET_USED: Dict[str, int] = {}


def _met_search(query: str, limit: int, timeout: int = 25) -> List[int]:
    """调 Met 搜索，返回 objectID 列表。

    ⚠️ `v1/search` 已于 2026-10-01 退役（官方报错里点名要用 v1.1 + offset/limit）。
    """
    import requests

    if not query.strip():
        return []
    params = {"q": query, "offset": 0, "limit": max(1, limit),
              "departmentId": MET_ASIAN_DEPT}
    try:
        r = requests.get(f"{MET_API}.1/search", params=params,
                         headers=MET_UA, timeout=timeout)
        if not r.ok:
            return []
        ids = r.json().get("objectIDs") or []
        return [int(i) for i in ids if str(i).isdigit()]
    except Exception:          # noqa: BLE001 —— 网络问题不抛，让上游换图源
        return []


def _met_object(obj_id: int, timeout: int = 25) -> Optional[Dict[str, object]]:
    """取一件藏品详情；不可用（非公有领域 / 无图）返回 None。"""
    import requests

    try:
        r = requests.get(f"{MET_API}/objects/{obj_id}", headers=MET_UA, timeout=timeout)
        if not r.ok:
            return None
        d = r.json()
    except Exception:          # noqa: BLE001
        return None
    if not d.get("isPublicDomain"):
        return None            # 版权图一律不碰
    # **必须是中国的藏品**。departmentId=6 是「亚洲艺术」，实测中国画只占少数：
    # 搜 "Chinese landscape" 得到的 10 条里 7 条是日本浮世绘/屏风（只是画了
    # 中国题材）。不卡 culture 就会给古诗词课件配一张日本浮世绘 —— 正是要修的病。
    culture = str(d.get("culture") or "")
    country = str(d.get("country") or "")
    if not ("China" in culture or "Chinese" in culture or "China" in country):
        return None
    img = str(d.get("primaryImage") or "").strip()
    if not img:
        return None            # 没有高清图的不用（primaryImageSmall 只作兜底）
        img = str(d.get("primaryImageSmall") or "").strip()
    tags = " ".join(str(d.get(k) or "") for k in
                    ("objectDate", "culture", "medium", "classification", "department"))
    return {
        "id": obj_id,
        "title": str(d.get("title") or ""),
        "tags": tags.strip(),
        "description": str(d.get("objectName") or ""),
        "url": img,
        "page": str(d.get("objectURL") or ""),
        "artist": str(d.get("artistDisplayName") or ""),
        "license_tier": "no-attribution",   # CC0 / Public Domain
        "width": 0, "height": 0,
    }


def fetch_met(project: Path, row: ImageRow, skill_dir: Path,
              max_candidates: int = 8, timeout: int = 60) -> FetchResult:
    """从 The Met 取一张公有领域高清图（仕女/花鸟/山水/楼阁这类题材最对路）。"""
    import requests
    from concurrent.futures import ThreadPoolExecutor

    main_query, variants = split_query_variants(row.reference)
    # Met 是英文库：优先用英文变体；中文查询基本 0 命中，但作为兜底仍试一次
    queries: List[str] = []
    for q in list(variants) + [main_query, row.get("Purpose") or ""]:
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)
    if not queries:
        return FetchResult(False, "Met：没有可用的检索词")

    ids: List[int] = []
    used_query = ""
    for q in queries[:3]:
        got = [i for i in _met_search(q, min(max_candidates * 4, 30), timeout=25) if i not in ids]
        if got:
            ids.extend(got)
            used_query = used_query or q
        if len(ids) >= MET_MAX_DETAILS:
            break
    if not ids:
        return FetchResult(False, f"Met：无搜索结果（试过 {'/'.join(queries[:3])[:40]}）")

    ids = ids[:MET_MAX_DETAILS]
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = [it for it in pool.map(_met_object, ids) if it]
    if not items:
        return FetchResult(False, f"Met：{len(ids)} 条结果里没有公有领域高清图")

    # 与其它图源同一排序口径：先比命中词数，再比风格偏好
    items.sort(key=lambda it: rank_key(it, row), reverse=True)   # type: ignore[arg-type]

    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / row.filename
    for it in items[:6]:
        url = str(it.get("url") or "")
        if _MET_USED.get(url):
            continue                       # 同册不重复用同一张
        try:
            r = requests.get(url, headers=MET_UA, timeout=timeout)
        except Exception:                  # noqa: BLE001
            continue
        if not r.ok or len(r.content) < 20000:
            continue
        tmp = images_dir / f"._met_{row.filename}"
        tmp.write_bytes(r.content)
        size = probe_size(tmp)
        if not size or size[0] < MIN_WIDTH or size[1] < MIN_HEIGHT:
            tmp.unlink(missing_ok=True)
            continue
        tmp.replace(target)
        _MET_USED[url] = 1
        title = str(it.get("title") or "")
        _append_source_record(
            project, row.filename,
            provider="met",
            title=title[:120],
            author=str(it.get("artist") or ""),
            license_name="CC0 / Public Domain (The Met Open Access)",
            license_tier="no-attribution",
            source_page_url=str(it.get("page") or ""),
            download_url=url,
            width=size[0], height=size[1],
            search_query=used_query,
            attribution_text="",
            status="sourced")
        return FetchResult(True, f"Met {size[0]}x{size[1]}｜{title[:26]}", "met",
                           match_text=f"{title} {it.get('tags') or ''}")
    return FetchResult(False, f"Met：{len(items)} 张公有领域候选均不满足尺寸")


# ---------------------------------------------------------------------------
# 取图：Pixabay（免费 key，直连；中文查询 + lang=zh 才能命中中文标签）
# ---------------------------------------------------------------------------
def _shorten_query(q: str, limit: int = 4) -> str:
    """把长句压成 1~4 个关键词。图库按整句匹配会漂。"""
    tokens = [t for t in re.split(r"[\s,，、;；]+", q or "") if t]
    return " ".join(tokens[:limit]) or (q or "").strip()


def _tag_score(query: str, tags: str) -> float:
    """查询词与返回标签的重合度。用来筛「明明搜显微镜却给望远镜」这种漂移。"""
    q_tokens = {t.lower() for t in re.split(r"[\s,，、;；]+", query or "") if len(t) > 1}
    if not q_tokens:
        return 0.0
    tag_tokens = {t.lower() for t in re.split(r"[\s,，、;；]+", tags or "") if t}
    hits = sum(1 for t in q_tokens if any(t in tag for tag in tag_tokens))
    return hits / len(q_tokens)


def fetch_pixabay(project: Path, row: ImageRow, skill_dir: Path,
                  timeout: int = 60,
                  max_candidates: int = 20) -> FetchResult:
    """直接调 Pixabay API：中文优先 + lang=zh + 标签重合度排序。"""
    key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not key:
        return FetchResult(False, "PIXABAY_API_KEY 未配置")
    import requests

    zh_query, variants = split_query_variants(row.reference)
    # 中文查询用 lang=zh，返回中文标签，才能与查询词对上
    use_zh = bool(re.search(r"[\u4e00-\u9fff]", zh_query))
    query = _shorten_query(zh_query if use_zh else (variants[0] if variants else zh_query), 4)
    if not query:
        query = row.get("Purpose") or row.filename

    params = {
        "key": key, "q": query, "image_type": "photo",
        "orientation": "horizontal", "safesearch": "true",
        "min_width": MIN_WIDTH, "min_height": MIN_HEIGHT,
        "per_page": max_candidates, "order": "popular",
    }
    if use_zh:
        params["lang"] = "zh"

    hits: List[dict] = []
    tried: List[str] = []
    query_used = query
    # 依次试：中文原词 → 英文变体 → 中文短词。**每家都要过标签交集检查**，
    # 没交集就换下一个查询（而不是按尺寸兜底选一张跑题的图）。
    attempts: List[tuple] = [(query, "zh" if use_zh else "en")]
    if variants:
        attempts.append((_shorten_query(variants[0], 4), "en"))
    if use_zh:
        attempts.append((_shorten_query(zh_query, 2), "zh"))

    for q_try, lang_try in attempts:
        if not q_try or q_try in tried:
            continue
        tried.append(q_try)
        params["q"] = q_try
        params["lang"] = lang_try
        try:
            r = requests.get("https://pixabay.com/api/", params=params, timeout=30,
                               headers={"User-Agent": "Mozilla/5.0"})
            if not r.ok:
                continue
            got = r.json().get("hits") or []
        except Exception:  # noqa: BLE001
            continue
        if not got:
            continue
        got.sort(key=lambda h: _tag_score(q_try, str(h.get("tags") or "")), reverse=True)
        if _tag_score(q_try, str(got[0].get("tags") or "")) <= 0:
            continue                      # 这一批标签全不匹配，换下一个查询
        hits = got
        query_used = q_try
        break

    if not hits:
        return FetchResult(False, f"Pixabay：无匹配标签的结果（试过 {'/'.join(tried[:3])}）")
    query = query_used

    def rank(h: Dict[str, object]) -> Tuple[int, float]:
        """与 `rank_key` 同一口径：**先比命中词数，再比标签重合度与分辨率**。

        Pixabay 拿的是中文标签，所以中文命中数能算准；Reference 为空时
        （旧行/手写行）退回 `_tag_score`，行为与以前一致。
        """
        text = str(h.get("tags") or "")
        zh_hits, _ = _zh_hits(row.reference or "", text)
        return (len(zh_hits),
                _tag_score(query, text) * 10
                + min(int(h.get("imageWidth") or 0), 4000) / 4000.0)

    hits.sort(key=rank, reverse=True)
    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / row.filename

    for h in hits[:6]:
        url = str(h.get("largeImageURL") or "")   # 免费账号最大 1280px
        if not url:
            continue
        try:
            img = requests.get(url, timeout=timeout,
                               headers={"User-Agent": "Mozilla/5.0"})
        except Exception:  # noqa: BLE001
            continue
        if not img.ok or len(img.content) < 20000:
            continue
        target.write_bytes(img.content)
        size = probe_size(target)
        if not size or size[0] < MIN_WIDTH // 2:
            target.unlink(missing_ok=True)
            continue
        # 溯源：直接自己写，不再走「二次下载」那条易失败的路
        _append_source_record(
            project, row.filename,
            provider="pixabay",
            title=str(h.get("tags") or "")[:120],
            license_name="Pixabay Content License",
            license_tier="no-attribution",
            source_page_url=str(h.get("pageURL") or ""),
            download_url=url,
            width=size[0], height=size[1],
            search_query=query,
            # checker 对非 manual 档位硬要求 attribution_text 非空，写空串会让
            # final 报告 failed、svg_to_pptx release gate 直接拒导（2026-10-19
            # 紫藤萝：三张 Pixabay 图空 attribution_text → 导出失败）。
            attribution_text=(
                f'{row.filename} via Pixabay, license: Pixabay Content License'
                + (f' ({h.get("pageURL")})' if h.get("pageURL") else '')
            ),
            status="sourced")
        return FetchResult(
            True,
            f"pixabay {size[0]}x{size[1]}｜标签={str(h.get('tags') or '')[:26]}",
            "pixabay", match_text=str(h.get("tags") or ""))
    return FetchResult(False, f"pixabay：{len(hits)} 条候选均不可用")


# ---------------------------------------------------------------------------
# 取图：ai（写提示词清单；有后端就跑，没有就留给人工）
# ---------------------------------------------------------------------------
def fetch_ai(project: Path, row: ImageRow, skill_dir: Path,
             timeout: int = 600) -> FetchResult:
    """AI 生图。没有可用后端时只写提示词并标 Needs-Manual，不报错。"""
    backend = (os.getenv("IMAGE_BACKEND") or "").strip()
    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    prompt = row.reference or row.get("Purpose") or row.filename
    manifest = images_dir / "image_prompts.json"

    payload: Dict[str, object] = {"items": []}
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"items": []}
    items = payload.setdefault("items", [])
    if not isinstance(items, list):
        items = payload["items"] = []
    items.append({"filename": row.filename, "prompt": prompt,
                  "page": row.page_role, "purpose": row.get("Purpose", "")})
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if not backend:
        return FetchResult(False, "未配置 IMAGE_BACKEND，已写 image_prompts.json")

    script = skill_dir / "scripts" / "image_gen.py"
    if not script.is_file():
        return FetchResult(False, f"找不到 {script}")
    cmd = [sys.executable, str(script), "--manifest", str(manifest),
           "-o", str(images_dir)]
    try:
        r = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return FetchResult(False, "生图超时")
    target = images_dir / row.filename
    if r.returncode == 0 and target.is_file():
        size = probe_size(target)
        return FetchResult(True, f"{backend} 生成 {size}", backend)
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    return FetchResult(False, (tail[-1][:80] if tail else "生图失败"))


# ---------------------------------------------------------------------------
# 机械列回填
# ---------------------------------------------------------------------------
def apply_mechanics(project: Path, row: ImageRow, ok: bool,
                    needs_selection: bool = False) -> None:
    """按真实文件回填 Dimensions / Ratio / Status，并规范化其余列。"""
    if not row.get("Crop Policy") or row.crop not in ("adaptive", "no-crop"):
        row.set("Crop Policy", "adaptive")
    if not row.pattern:
        row.set("Image pattern", DEFAULT_PATTERN)

    target = project / "images" / row.filename
    if ok and target.is_file():
        size = probe_size(target)
        if size:
            w, h = size
            row.set("Dimensions", f"{w}x{h}")
            row.set("Ratio", ratio_text(w, h))
    if needs_selection:
        row.set("Status", "Needs-Selection")
    else:
        row.set("Status", "Sourced" if ok and row.acquire == "web"
                else "Generated" if ok
                else "Needs-Manual")


# ---------------------------------------------------------------------------
# spec_lock 投影
# ---------------------------------------------------------------------------
LOCK_ROW_RE = re.compile(r"^-\s*([^:]+):\s*(.+)$", re.M)


def _lock_key(row: ImageRow, index: int) -> str:
    m = re.search(r"(\d{1,3})", row.page_role or "")
    return f"p{int(m.group(1)):02d}" if m else f"img{index:02d}"


def sync_lock_images(lock_path: Path, rows: Sequence[ImageRow]) -> int:
    """把 §VIII 里**真的有文件**的行写进 spec_lock.md 的 `## images`。返回写入条数。

    ⚠️ “有文件”是硬条件，不是看 Acquire Via：2026-10-22 事故——岳飞那册
    3 张配图降级为 Needs-Manual（库里没真人古画），但以前只按 acquire=web 就
    全部投影进了 `## images`，于是 P3 看到 `- p02: images/p02_yuefei_portrait.jpg`
    就当真地在 SVG 里写了 `<image href="../images/p02_yuefei_portrait.jpg">`。
    文件不存在 → 校验器报 blocking → **整册拒绝导出**（12 页 SVG 写完了也不给导出）。
    """
    if not lock_path.is_file():
        return 0
    img_dir = lock_path.parent / "images"
    usable = [
        r for r in rows
        if (r.acquire in FETCH_ACQUIRE or r.acquire in {"user", "slice"})
        and r.filename and _ok_file(img_dir / r.filename)
    ]
    entries = [
        f"- {_lock_key(r, i)}: images/{r.filename} | source={r.acquire} | crop={r.crop}"
        for i, r in enumerate(usable, 1) if r.filename
    ]

    text = lock_path.read_text(encoding="utf-8")
    block = "## images\n" + "\n".join(entries) + "\n" if entries else ""

    # 已有 ## images 段：整段替换
    existing = re.search(r"^##\s+images\s*$.*?(?=^##\s+|\Z)", text, re.M | re.S)
    if existing:
        text = text[: existing.start()] + (block + "\n" if block else "") + text[existing.end():]
    elif block:
        anchor = None
        for pattern in (r"^##\s+page_rhythm\s*$", r"^##\s+forbidden\s*$"):
            m = re.search(pattern, text, re.M)
            if m:
                anchor = m.start()
                break
        if anchor is None:
            text = text.rstrip() + "\n\n" + block
        else:
            text = text[:anchor] + block + "\n" + text[anchor:]

    # 清掉连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    lock_path.write_text(text, encoding="utf-8")
    return len(entries)


# ---------------------------------------------------------------------------
# skill 目录探测
# ---------------------------------------------------------------------------
def looks_like_skill(path: Path) -> bool:
    return (path / "SKILL.md").is_file() and (path / "scripts" / "image_search.py").is_file()


def resolve_skill_dir(start: Optional[Path] = None) -> Optional[Path]:
    """从项目目录向上、或从 PPT-Agent 附近找 skill 根目录。"""
    env = os.getenv("PPT_MASTER_SKILL_DIR")
    if env and looks_like_skill(Path(env).expanduser()):
        return Path(env).expanduser().resolve()

    seeds: List[Path] = []
    if start:
        seeds.extend([start, *start.parents])
    here = Path(__file__).resolve().parent
    seeds.extend([here, *here.parents])
    for base in seeds:
        for cand in (base, base / ".agents" / "skills" / "ppt-master",
                     base / "ppt-master" / "skills" / "ppt-master"):
            try:
                if looks_like_skill(cand):
                    return cand.resolve()
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
@dataclass
class ImagePhaseReport:
    total: int = 0
    fetched: int = 0
    failed: int = 0
    skipped: int = 0
    lines: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"§VIII 共 {self.total} 行，取到 {self.fetched}，"
                f"失败降级 {self.failed}，跳过 {self.skipped}")



# ---------------------------------------------------------------------------
# 源路由 + 实体门槛（人物/文物类走博物馆与 Commons，实景类走照片库）
# ---------------------------------------------------------------------------
_ARTS_HINTS = (
    "画像", "肖像", "绣像", "刻本", "版画", "古画", "书画", "文物", "插画", "人物",
    "portrait", "painting", "woodcut", "woodblock", "ukiyo", "artwork", "print",
)
_SCENE_HINTS = (
    "实景", "照片", "摄影", "风景", "地貌", "雪景", "山", "街", "天空",
    "photograph", "landscape", "scenery", "sky", "mountain", "street",
)

#: 英文题材通用词：不参与「实体命中」判定（否则 Water/Margin 会命中 river 图）
_GENERIC_EN = {
    "water", "margin", "woodblock", "woodcut", "ukiyo", "ukiyo-e", "painting",
    "portrait", "print", "art", "artwork", "snow", "mountain", "landscape",
    "river", "photo", "photograph", "illustration", "drawing", "ink", "chinese",
}

#: 别名匹配时剔除的虚词（不剔除短实词：拼音音节常常只有 2~3 字母）
_ALIAS_STOPWORDS = {"of", "the", "and", "a", "an", "in", "on", "at", "to", "for",
                    "de", "la", "le", "el", "al"}

#: 中文短专名 → 英文别名（**人工策展，绝不自动音译**）。
#:
#: 为什么不用拼音自动生成：拼音会把「林」变成 `lin`，而 `lin` 在拉丁文标题里
#: 到处都是——2026-10-20 实测 "Lin" 命中了 Pixabay 的 `fleur-de-lin`（鸢尾花），
#: 一张花图被当成林冲雪战图。所以这张表只收**能当专名用**的英文写法：
#: 西文通行的作品译名（Outlaws of the Marsh）或名姓顺序明确的人名（Lin Chong）。
#:
#: 只对 2~4 字短专名生效（见 `_entity_hit`），且只在中文实体没命中时启用，
#: 因此它的存在只会把闸门**放宽**到「中文意图 + 英文元数据」这一种先前必败的情形。
ENTITY_ALIASES: Dict[str, Tuple[str, ...]] = {
    # 人物
    "林冲": ("lin chong", "outlaws of the marsh", "water margin heroes"),
    "武松": ("wu song", "outlaws of the marsh", "water margin heroes"),
    "宝黛": ("bao-yu", "bao yu", "dai-yu", "dai yu", "dream of the red chamber",
             "hongloumeng", "hong lou meng"),
    "宝黛初会": ("bao-yu", "bao yu", "dai-yu", "dai yu", "dream of the red chamber",
                 "hongloumeng", "hong lou meng"),
    "宝钗": ("bao-chai", "bao chai", "dream of the red chamber", "hongloumeng"),
    "黛玉": ("dai-yu", "dai yu", "dream of the red chamber", "hongloumeng"),
    "林黛玉": ("dai-yu", "dai yu", "dream of the red chamber", "hongloumeng"),
    "李清照": ("li qingzhao", "li qing-zhao", "qingzhao"),
    "晏殊": ("yan shu"),
    "苏轼": ("su shi", "su dongpo", "su dong-po"),
    # 作品 / 篇目
    "红楼梦": ("dream of the red chamber", "hong lou meng", "hongloumeng"),
    "赤壁赋": ("red cliff", "chibi fu", "ode to the red cliff"),
    "水浒": ("outlaws of the marsh", "water margin heroes"),
    "祝福": ("new year sacrifice", "zhufu"),
    "琵琶行": ("pipa xing", "song of the pipa"),
    # 地标 / 意象（只用作品里的专名；残留风险已在下方注明）
    "西湖": ("west lake", "xihu"),
    "湖心亭": ("huxinting", "mid-lake pavilion"),
    "紫藤萝": ("wisteria", "purple wisteria"),
    "荷塘": ("lotus pond", "lotus pool"),
    "荷塘月色": ("lotus pond", "moonlight lotus", "lotus moonlight"),
    "月色": ("moonlight", "moonlit"),
}
#: 已知残留风险（不做事，只记录）："red cliff"（澳洲红崖海滩）、"west lake"
#: （杭州西湖区写字楼）、"wisteria"（Wisteria Lane 路牌）都能让实体门槛误过。
#: 但它们仍须再过 relevance / _term_coverage / P0，比「中文意图永远配不上英文图」好。


def _alias_hit(subject: str, tl: str) -> bool:
    """中文短专名的**英文人工别名**命中（按词匹配，绝不做子串 / 自动拼音）。

    四道防线（每一条都对应一次真实事故或一个具体坑）：
      1. **按词匹配**，不做子串——`lin` 不得命中 `fleur-de-lin`；
      2. **全通用词的别名作废**——`water margin` 两个词都在 `_GENERIC_EN` 里，
         它会命中河岸图（2026-10-20 事故），所以这种别名直接不生效；
      3. **多词别名必须逐词全中**——`wu song` 不能因为只命中 `song` 就放行
         （否则任何带 “song” 的音乐图都能冒充武松）；
      4. **单词别名 ≥ 5 字母**——三四个字母的拉丁词在标题里没有区分度。
    只剔除虚词（of/the/and…），**不剔除短实词**：拼音音节常常只有 2~3 字母（wu/su/yu）。
    """
    if not subject or not tl:
        return False
    text_words = set(re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", tl))
    if not text_words:
        return False
    # 只认**精确键**。不做「键与实体互为子串」的模糊回落：那会让
    # 「荷塘月色」靠「月色」的 moonlight 单独过关，等于把实体门槛降成题材门槛。
    # 要覆盖复合专名就在表里显式加一行（宝黛初会 / 荷塘月色 已经加了）。
    keys = [subject] if subject in ENTITY_ALIASES else []
    for key in keys:
        for alias in ENTITY_ALIASES[key]:
            parts = [p for p in re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", alias.lower())
                     if p not in _ALIAS_STOPWORDS]
            if not parts:
                continue
            if all(p in _GENERIC_EN for p in parts):
                continue               # 防线 2：全通用词，命中也不认
            if len(parts) == 1:
                if len(parts[0]) >= 5 and parts[0] in text_words:
                    return True
            elif all(p in text_words for p in parts):
                return True            # 防线 3：逐词全中
    return False


def _row_text(row: ImageRow) -> str:
    return f"{row.reference or ''} {row.get('Purpose') or ''} {row.filename}".lower()


def _is_arts_row(row: ImageRow) -> bool:
    """人物画像 / 文物 / 版画类需求。"""
    return any(k in _row_text(row) for k in _ARTS_HINTS)


#: 「古诗/古文/古画」类需求：用户口径（2026-10-21）—— 这类主题要**尽量避免现代气息**
#: （高楼/城市/街景），但**现代的古风摄影也完全可用**，所以只用于**降权**，
#: 不用来否决，也不用它去扣「照片 vs 画种」的分（真实荷花照对《荷塘月色》仍然可用）。
#: 故意不放裸「词」：会命中「关键词/词语」。
_CLASSICAL_HINTS = (
    "诗", "词牌", "宋词", "唐诗", "元曲", "楚辞", "乐府", "古文", "文言",
    "古籍", "古画", "国画", "水墨", "丹青", "山水", "画卷", "工笔", "写意",
    "绢本", "拓本", "线装", "诗意图",
    "classical chinese", "ink painting", "scroll painting", "poetry", "poem",
)


def _is_classical_row(row: Optional[ImageRow]) -> bool:
    """古诗/古文/古画类主题（只用于「现代气息」降权的范围）。"""
    return row is not None and any(k in _row_text(row) for k in _CLASSICAL_HINTS)


#: 古画行不跑「只出实景照片」的图库
_STOCK_PHOTO_PROVIDERS = ("pexels", "pixabay")

#: 古画/博物馆类图源：元数据是「作者 + 画名 + 朝代」，与「山河朝阳」这类中文意象词
#: 几乎不可能字符串命中。对它们用实体硬门槛，实测会把**正确**的中国水墨画全拒掉：
#: 2026-10-22 岳飞那册 p09/p10/p06 三行全灭（Met 相关度 1.00 的水墨山水被「实体未命中」拒），
#: 整册退回 0 图。所以这三家的实体门槛降级——画种风险它们自己卡了
#: （Met 卡 culture=China + 公有领域，书格整站古籍古画），闸门交给 relevance + _style_mismatch。
_ART_SOURCES = ("shuge", "met", "local")

#: 明确要「画」而不是「照片」的措辞（只看 Reference/Purpose/Type，不猜）
_PAINTING_HINTS = (
    "水墨", "古画", "国画", "绢本", "纸本", "设色", "工笔", "写意", "青绿",
    "白描", "长卷", "手卷", "册页", "扇面", "界画", "丹青", "拓本", "画册",
    "ink painting", "ink wash", "scroll painting", "painting", "brushwork",
)


def _wants_painting(row: ImageRow) -> bool:
    """这一行要的是「画」还是「照片」。

    只在**有明确证据**时返回 True：§VIII 的 `Type` 写了 Painting/Illustration，
    或 Reference/Purpose 里写了水墨/古画/长卷……否则一律 False——
   “荷塘实景照”“紫藤花瀑”这类正常照片需求不能被卡死。

    为什么需要它（2026-10-22 事故）：岳飞《满江红》P09「踏破贺兰山缺」要的是水墨关隘，
    结果落了 Pixabay 的雪山彩照，贴在一整册宣纸淡彩版面上像一个 PPT 里夹了张风景壁纸。
    判据写在数据里（Type/Reference），不靠模型自报风格。
    """
    typ = str(row.get("Type") or "").strip().lower()
    if typ.startswith("paint") or typ.startswith("illustration"):
        return True
    return any(k in _row_text(row) for k in _PAINTING_HINTS)


def _provider_order_for(row: ImageRow, default: Sequence[str]) -> List[str]:
    """按需求类型重排图源；只在给定 providers 内重排，不新增能力。"""
    base = list(default)
    text = _row_text(row)
    if _is_arts_row(row) or _is_classical_row(row):
        # 古画/文物/版画/古诗文：**书格排第一**。
        # 2026-10-22 实测：met/wikimedia/openverse 三家在这台机器上一次都没成——
        # met 要 culture=China + PD（命中率低），wikimedia/openverse 走代理，
        # 代理没开就直接连错三次，等到书格时尝试额度已用完，最后落到 pixabay 的
        # 实景彩照（岳飞那册 P09 的雪山照就是这么来的）。古画需求先把能出古画的那家试完。
        order = ["shuge", "met", "wikimedia", "openverse", "pixabay", "pexels"]
    elif any(k in text for k in _SCENE_HINTS):
        order = ["pixabay", "pexels", "met", "wikimedia", "openverse", "shuge"]
    else:
        order = base
    picked = [p for p in order if p in base]
    # 硬编码顺序里没列到的图源不能就此丢掉 ——
    # 2026-10-22 实测：新加的图源若没登记进上面的顺序表，会被这里整个过滤掉，压根不跑。
    picked += [p for p in base if p not in picked]
    return picked or base


#: 常见繁体/异体 → 简体（只覆盖人名/书名里高频的字，够用且无依赖）
_TRAD2SIMP = str.maketrans(
    "沖衝頭廟傳滸畫像誌鐘劉關張趙錢孫李楊歐陽",
    "冲冲头庙传浒画象志钟刘关张赵钱孙李杨欧阳",
)


def _norm_cjk(text: str) -> str:
    return (text or "").translate(_TRAD2SIMP)



#: 诗词意象的近义组：查询说「荷塘」，图库标签写「荷花/荷叶」，应视为命中
_SYNONYMS = {
    "荷塘": ("荷花", "荷叶", "莲花", "莲", "lotus"),
    "荷花": ("荷塘", "荷叶", "莲花", "lotus"),
    "荷叶": ("荷花", "荷塘", "莲花"),
    "月色": ("月夜", "月光", "月亮", "明月", "moon"),
    "月夜": ("月色", "月光", "明月", "moon"),
    "月影": ("月色", "月夜", "月光", "倒影"),
    "归燕": ("燕子", "燕", "swallow"),
    "燕子": ("归燕", "燕", "swallow"),
    "落花": ("花", "花瓣", "flowers"),
    "秋树": ("秋山", "秋林", "autumn"),
    "高楼": ("楼阁", "塔楼", "tower"),
    "书斋": ("书房", "studio", "study"),
    "园林": ("园", "花园", "garden"),
    "小径": ("小路", "path"),
}


def _reference_terms(reference: str) -> Tuple[List[str], List[str], List[str]]:
    """拆 Reference 的 `实体｜查询（English variant）`。

    返回 (中文主题词, 英文全词, 英文专有词)。
    由现役阀门 `_term_coverage`、实体别名（`_alias_hit` 的外部上下文）与诊断
    `p2_weighted_coverage` **共用**：两边必须看同一批词，否则诊断出的分歧全是假分歧。

    没有 `｜` 时（旧格式 / 模型漏写）拿**整条 Reference** 当查询——以前这种行
    直接放行，等于零校验；现在至少还过一遍题材覆盖（见 `_term_coverage`）。
    """
    body = reference.split("｜", 1)[1] if "｜" in reference else reference
    zh_part = re.sub(r"（.*?）", "", body)
    zh_terms = [
        x for x in re.split(r"[\s,，、;；]+", zh_part)
        # 通用前缀/通用类型词不参与覆盖判定：否则「水墨」会命中任何水墨风标签，
        # 黄山云海就能冒充「落花与飞燕」；「画像」会让猫片冒充李清照
        # （2026-10-22 事故：猫的标签写着「猫画像」）。
        if len(x) >= 2 and x not in _NEUTRAL_PREFIXES and x not in _GENERIC_ZH_TERMS
    ]
    m = re.search(r"（(.*?)）", reference)
    en_all = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-']+", m.group(1) if m else "")
              if len(w) >= 3]
    en_terms = [w for w in en_all if w not in _GENERIC_EN]
    return zh_terms, en_all, en_terms


#: 「只说明什么时候 / 什么气氛」的词：
#: 单独命中它们不足以证明画对了需求里的**对象**——月夜图里没江面小舟、
#: 黄昏图里没楼阁。只有「命中的中文词全落在这个集合里」时才判不命中；
#: 一旦命中任何一个对象词（荷塘/西湖/林冲/小舟…）就照常放行。
#: 故意**不收录**「雪景 / 夜景 / 景色」这类看得见的主体场景词。
_MOOD_ONLY_TERMS = {
    "月夜", "夜色", "夜晚", "夜里", "月下", "月色", "月光", "月亮", "明月",
    "黄昏", "夕阳", "落日", "傍晚", "清晨", "黎明", "早晨", "深夜", "午夜",
    "春", "夏", "秋", "冬", "初春", "深秋", "寒冬", "初夏",
    "倒影", "氛围", "气氛", "全景", "远景", "近景", "特写", "俯瞰", "远眺",
}


def _mood_term(term: str) -> bool:
    """该中文主题词是不是「只说明时间/气氛」。"""
    nt = _norm_cjk(term or "")
    if nt in _MOOD_ONLY_TERMS:
        return True
    return len(nt) >= 3 and nt[:2] in _MOOD_ONLY_TERMS


def _zh_hits(reference: str, probe_text: str) -> Tuple[List[str], List[str]]:
    """逐词判定中文主题词命中，返回 (命中词, 全部主题词)。

    阀门（`_term_coverage`）与排序（`rank_key`）**共用一个函数**：
    两边判定不一致的话，会出现「闸门说中了、排序说没中」这种鬼故事。
    """
    zh_terms, _, _ = _reference_terms(reference or "")
    t = _norm_cjk(probe_text or "")
    hits: List[str] = []
    for term in zh_terms:
        nt = _norm_cjk(term)
        if nt in t or (len(nt) >= 3 and nt[:2] in t):
            hits.append(term)
            continue
        # 近义词也算命中：荷塘→荷花、月色→月夜…
        if any(_norm_cjk(s) in t for s in _SYNONYMS.get(term, ())):
            hits.append(term)
    return hits, zh_terms


def _term_coverage(reference: str, probe_text: str) -> bool:
    """关键词覆盖率：中文至少命中 1 个词；中文全不中时英文至少命中 2 个。

    比单一实体门槛更严，专门拦「泛词漂移」：
    2026-10-20 赤壁赋事故——P07「洞箫」收下危地马拉排箫老头（只命中 panpipe），
    P12「月夜江舟」收下伦敦塔桥（只命中 river/night）。

    三条补丁（均为「该拦的没拦」）：
      1. **无 `｜` 不再直接放行**：以前这种行零校验（赤壁赋 6 行里占 2 行）。
         现在拿整条 Reference 当查询走一遍下面的判定。
      2. **只命中氛围词不算命中**：2026-10-21 实测，「月夜江面小舟」收下一张
         纯月亮照（标签只有 月出/月亮/满月/夜晚/月夜/气氛）——命中「月夜」即过，
         而图里根本没有水面与舟。现在若真正命中的中文词**全部**是时间/氛围词，
         则判不命中。
      3. **一个词都不中 = 直接排除**（用户口径 2026-10-21）：「图片没中里面的
         任何一个词就百分百排除」。中得多的排序靠前，由 `rank_key` 负责。

    已知弱点（故意保留、由 P2 加权诊断去量）：中文命中 1 个**对象**词即可过，
    词与词之间没有轻重。
    """
    # Reference 为空 = 无从校验（旧产物里这种行很多）。保持放行，不自作主张：
    # 该行能不能用仍由 relevance（会回退到 Purpose）去判。
    if not (reference or "").strip():
        return True
    zh_terms, en_all, en_terms = _reference_terms(reference)
    t = _norm_cjk(probe_text or "")
    tl = t.lower()
    hit_terms, _ = _zh_hits(reference, probe_text)
    if hit_terms:
        # 只命中时间/氛围词不算（2026-10-21：月夜江面小舟 → 纯月亮照）
        if all(_mood_term(x) for x in hit_terms):
            return False
        return True
    en_hits = sum(1 for w in en_terms if w[:4] in tl)
    if en_hits >= 2:
        return True
    if en_terms and en_hits / len(en_terms) >= 0.5:
        return True
    # 纯题材需求（如「雪山」，没有专名）：允许通用词命中 ≥2，
    # 但绝不用于人物/作品名（那些有专名，走上面的严格分支）
    if not en_terms:
        generic_hits = sum(1 for w in en_all if w in _GENERIC_EN and w[:4] in tl)
        if generic_hits >= 2:
            return True
    return False


# ---------------------------------------------------------------------------
# P2 加权覆盖：**只读诊断版**（不接管阀门）
# ---------------------------------------------------------------------------
# 现役 `_term_coverage` 是「中文命中任意 1 个词即过」：能拦泛词漂移，
# 但它把「雪夜古庙」里的「古庙」和「雪夜」当同权——命中一个就算过。
#
# 加权版只改一处：按**词序**加权求和，不过线就拒。
# 现在只打印不接管：先跑几册回归拿到分歧数据，再定 `P2_PASS`。
# 开关：`run_image_phase(..., p2_diag=True/False)`，默认开（纯字符串运算，不联网）。

#: P2 加权权重。**词序即优先级**：Reference `实体｜` 之后的第一个词是锚。
P2_W: Dict[str, float] = {
    "head": 1.0,          # 首个主题词
    "term": 0.7,          # 其余主题词
    "en_specific": 0.5,   # 英文专有/题材词
    "en_generic": 0.2,    # 英文通用词（water/landscape…）
}
#: 弱命中折减：近义词 ×0.7；长词的 2 字前缀 ×0.5；**氛围词 ×0.5**
#: （只说「什么时候 / 什么气氛」的词是弱证据：月夜图里未必有江面小舟）
P2_W_SYNONYM = 0.7
P2_W_PREFIX = 0.5
P2_W_MOOD = 0.5
#: 加权覆盖阈值（得分/满分）。改它之前先看 `[p2-diag]` 的分歧清单。
#:
#: 初标定（2026-10-21，`--audit-p2` 扫 7 个项目 / 15 张真实自动取图）：
#:     0.45 → 一致 11、误杀 4   0.40 → 一致 14、误杀 1
#:     0.35 → 一致 14、误杀 1   0.30 → 一致 13、误杀 1、放松 1
#: 0.45 是拍脑袋值，实测太严；0.35~0.40 平台稳定，取 0.35。
#: 样本只有 15 张，扩到 30+ 张后需重标（`python image_pipeline.py --audit-p2 <ROOT>`）。
P2_PASS = 0.35


def p2_weighted_coverage(reference: str,
                         probe_text: str) -> Tuple[float, float, List[str], str]:
    """P2 加权覆盖（诊断）。返回 (比例, 胜出语言的满分, 命中标签, 备注)。

    **只读**：只被 `_p2_diagnose` / `audit_p2` 调，不参与任何 pass/fail。
    比例 = -1.0 表示无词可判（Reference 为空 / 词被近义组与通用前缀剥光），
    调用方应把它排除出统计，**不要当成「拒」**。

    分母按**语言分开**算，不合并：Reference 一般是「中文意图（English variant）」，
    而图库元数据只会是其中一种语言。合并分母等于让另一种语言永远拿 0 分，
    把比例系统性腰斩——2026-10-21 用 7 个项目 15 张真实取图实测，合并分母时
    加权通过率只有 7%（现役阀门 73%），全是假阴性。
    正确模型：哪种语言对上了，就用哪种语言算，取各语言比例的**较大者**。
    """
    if not (reference or "").strip():
        return -1.0, 0.0, [], "Reference 为空（现阀门也放行）"
    zh_terms, en_all, en_terms = _reference_terms(reference)
    t = _norm_cjk(probe_text or "")
    tl = t.lower()
    hits: List[str] = []

    zh_got = zh_full = 0.0
    for i, term in enumerate(zh_terms):
        w = P2_W["head"] if i == 0 else P2_W["term"]
        zh_full += w
        nt = _norm_cjk(term)
        weak = P2_W_MOOD if _mood_term(term) else 1.0
        if nt in t:
            zh_got += w * weak
            hits.append(f"{term}+{w * weak:.1f}")
        elif len(nt) >= 3 and nt[:2] in t:
            zh_got += w * P2_W_PREFIX * weak
            hits.append(f"{term}~{w * P2_W_PREFIX * weak:.1f}")
        elif any(_norm_cjk(s) in t for s in _SYNONYMS.get(term, ())):
            zh_got += w * P2_W_SYNONYM * weak
            hits.append(f"{term}≈{w * P2_W_SYNONYM * weak:.1f}")

    en_got = en_full = 0.0
    for w in en_terms:
        en_full += P2_W["en_specific"]
        if w[:4] in tl:
            en_got += P2_W["en_specific"]
            hits.append(f"{w}+{P2_W['en_specific']:.1f}")
    for w in en_all:
        if w in _GENERIC_EN:               # 通用词只补一点分，不当主判据
            en_full += P2_W["en_generic"]
            if w[:4] in tl:
                en_got += P2_W["en_generic"]
                hits.append(f"{w}·{P2_W['en_generic']:.1f}")

    cands: List[Tuple[float, float]] = []
    if zh_full > 0:
        cands.append((zh_got / zh_full, zh_full))
    if en_full > 0:
        cands.append((en_got / en_full, en_full))
    if not cands:
        return -1.0, 0.0, hits, "无可判词（近义组/通用前缀已全部剥掉）"
    best = max(cands, key=lambda x: x[0])
    return best[0], best[1], hits, ""


@dataclass
class P2Diag:
    """P2 加权诊断的累计统计。只读：不参与任何 pass/fail。

    这些计数器在一次取图阶段内累加，用来回答唯一一个问题：
    **如果换成 P2 加权闸门，会多收哪几张、多拒哪几张。**
    """

    total: int = 0
    #: 无词可判，排除出统计
    skipped: int = 0
    #: 两边都收 / 两边都拒
    agree_accept: int = 0
    agree_reject: int = 0
    #: 现判拒、加权判收 → 加权会放松
    widen: int = 0
    #: 现判收、加权判拒 → 加权会收紧
    tighten: int = 0
    disagreements: List[str] = field(default_factory=list)

    def note(self, filename: str, current_accept: bool,
             weighted_accept: Optional[bool], ratio: float, why: str) -> None:
        if weighted_accept is None:
            self.skipped += 1
            return
        self.total += 1
        if current_accept == weighted_accept:
            if current_accept:
                self.agree_accept += 1
            else:
                self.agree_reject += 1
            return
        if weighted_accept:
            self.widen += 1
            tag = "加权更松"
        else:
            self.tighten += 1
            tag = "加权更严"
        self.disagreements.append(
            f"{filename}｜现判={'收' if current_accept else '拒'} "
            f"加权={ratio:.2f} 拒/收={('收' if weighted_accept else '拒')}｜{tag}｜{why}")

    def summary(self, say: Callable[[str], None]) -> None:
        if not self.total and not self.skipped:
            return
        say(f"   [p2-diag] 共判 {self.total} 次（无词可判 {self.skipped} 次已排除）"
            f"｜一致 {self.agree_accept + self.agree_reject}"
            f"（收 {self.agree_accept}/拒 {self.agree_reject}）"
            f"｜分歧 {self.widen + self.tighten}"
            f"（更松 {self.widen}/更严 {self.tighten}）")
        for line in self.disagreements[:12]:
            say(f"   [p2-diag] ⚠ 分歧 {line}")
        if len(self.disagreements) > 12:
            say(f"   [p2-diag] …其余 {len(self.disagreements) - 12} 条分歧未列出")
        if not self.disagreements:
            say(f"   [p2-diag] 本批次无分歧（阈值 {P2_PASS}）→ 可考虑让 P2 接管，"
                f"但请先用 --audit-p2 扩到 30+ 张真实取图再定")


def _p2_diagnose(row: "ImageRow", probe_text: str, current_accept: bool,
                 say: Callable[[str], None], diag: P2Diag) -> None:
    """并列打印「现判」与「P2 加权会怎么判」。

    红线：**不能改任何决定、不能抛异常**。诊断代码自己出 bug 不应搞挂取图，
    所以整个函数体包在 try/except 里，出错只打一行提示。
    """
    try:
        reference = row.reference or ""
        ratio, full, hits, why = p2_weighted_coverage(reference, probe_text)
        if ratio < 0:
            weighted: Optional[bool] = None
        else:
            weighted = ratio >= P2_PASS
        with _STATE_LOCK:
            diag.note(row.filename, current_accept, weighted, ratio, why or "")
        if weighted is None:
            verdict = f"不适用（{why}）"
        else:
            verdict = (f"{ratio:.2f}{'≥' if weighted else '<'}{P2_PASS}"
                       f" → {'收' if weighted else '拒'}")
        mark = "" if weighted in (None, current_accept) else "  ⚠分歧"
        say(f"     · [p2-diag] {row.filename}: 现判={'收' if current_accept else '拒'}"
            f"｜加权 {verdict}"
            f"｜命中 {' '.join(hits) or '-'}{mark}")
    except Exception as exc:               # 诊断绝不能影响取图
        say(f"     · [p2-diag] 诊断异常已忽略：{exc}")


#: subject 里的通用前缀：剥掉后再做实体命中，避免「中国…」「水墨…」命中一切中文结果
_NEUTRAL_PREFIXES = (
    "中国画", "中国水墨", "中国", "水墨", "古画", "国画", "传统", "古代",
    "宋代", "唐代", "明代", "清代", "宋", "唐", "明", "清",
)

#: 现代/城市类特征词，分两档（2026-10-21 用户定的口径）：
#: “尽量避免现代气息，但现代的古风摄影也未尝不可”—— 所以只有「看一眼就知道
#: 不可能是古画」的建筑意象才硬否决，其余只降权，留出古风摄影/汉服写真这类活路。
#: 硬否决：
_MODERN_HARD_HINTS = (
    "摩天大楼", "天际线", "高楼大厦", "写字楼", "玻璃幕墙", "航拍城市",
    "skyscraper", "skyline", "high-rise", "highrise",
)
#: 软降权（不否决）：
_MODERN_SOFT_HINTS = (
    "摩天", "城市", "现代", "写真", "街景", "楼群", "都市",
    "city", "urban", "modern", "downtown",
)
#: 兼用（旧名字保留，两档合并）：
_MODERN_HINTS = _MODERN_HARD_HINTS + _MODERN_SOFT_HINTS


#: 通用**类型词**（中）：只说明「这是哪类图」，不说明「画的是谁/什么」。
#: 不计入命中：2026-10-22 事故——「李清照画像」收下一张猫片，猫的标签里写着
#: 「猫画像」，就靠「画像」二字过了三道阀门。
_GENERIC_ZH_TERMS = {
    "画像", "肖像", "绣像", "古画", "国画", "水墨", "照片", "摄影", "图片",
    "画面", "构图", "特写", "局部", "全身", "半身", "示意", "插图", "插画",
    "绘画", "手绘",
}

#: 通用**后缀**：从 subject 尾部剥掉才能拿到真实体（李清照画像 → 李清照）。
#: 按长度降序匹配，避免「图」先于「画像」被剥。
_GENERIC_SUFFIXES = (
    "画像", "肖像", "绣像", "照片", "绘画", "插图", "插画", "绘图",
    "特写", "局部", "全身", "半身", "示意", "图", "画",
)
# 注：不要剥裸「照」——会把人名「李清照」剥成「李清」。


def _core_subject(subject: str) -> str:
    """剥掉通用前缀/括号/通用后缀，留下真正的实体。

    例：
      「中国水墨山水」    → 山水
      「李清照画像」      → 李清照（**剩下 3 字，实体门槛才会真生效**）
      「折枝春花（中国古画）」 → 折枝春花（括号里是风格注释，不是实体的一部分）

    以前不剥后缀，「李清照画像」是 5 字，直接跳过 2~4 字的严格实体校验——
    这就是 2026-10-22「李清照配猫」事故的直接原因之一。
    """
    s = (subject or "").strip()
    # 括号里的东西多是风格/类型注释，不是实体
    s = re.sub(r"[（(][^）)]*[）)]", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for prefix in _NEUTRAL_PREFIXES:
            if s.startswith(prefix) and len(s) - len(prefix) >= 2:
                s = s[len(prefix):].strip()
                changed = True
                break
        if changed:
            continue
        for suffix in _GENERIC_SUFFIXES:
            if s.endswith(suffix) and len(s) - len(suffix) >= 2:
                s = s[: -len(suffix)].strip()
                changed = True
                break
    return s


def _style_mismatch(probe_text: str) -> bool:
    """P0 硬否决：命中**建筑级**现代意象才拒（摩天大楼/天际线/玻璃幕墙）。

    “城市 / 现代 / 写真 / street” 这类只交给 `style_preference` 降权，
    因为「现代的古风摄影」完全可用（用户 2026-10-21 定的口径）。
    """
    text = probe_text or ""
    tl = text.lower()
    return any(k in text or k in tl for k in _MODERN_HARD_HINTS)


def _entity_hit(reference: str, probe_text: str) -> bool:
    """实体硬门槛：Reference 的 `实体｜查询` 前缀必须命中结果元数据。

    没有 `｜`（旧格式）时不做限制，保持向后兼容。
    中文实体允许 2 字片段命中（如「林冲」）；英文按 query_en 的词命中（≥1 个）。
    2026-10-19 事故：书格《水浒传》整书第 9 页（武松打虎）被当成林冲画像，
    因为闸门只看「水浒」二字命中。实体门槛就是拦这一类。

    2026-10-21 补充：中文专名对**英文元数据**必然不命中（两套语言），这类行以前
    只有 arts 行的 `query_override` 能救。现在优先查 `ENTITY_ALIASES` 的人工英文
    别名（见 `_alias_hit`）——中文没命中时才会走到那里，所以只放宽、不收紧。
    """
    if not reference or "｜" not in reference:
        return True
    subject, rest = reference.split("｜", 1)
    subject = _core_subject(subject)
    if not subject:
        return True
    # 只有「短专名」（2~4 字、无连接词）才做严格实体校验；
    # 「落花与飞燕」「高楼远望秋树」这类题材描述交给 _term_coverage 按题材词判，
    # 否则会把 Commons 上正确的花鸟/山水古画全部拒掉（实测 8 张只剩 1 张）。
    if not (2 <= len(subject) <= 4) or any(ch in subject for ch in "与和及的、，"):
        return True
    t = _norm_cjk(probe_text or "")
    tl = t.lower()
    cjk = "".join(re.findall(r"[一-鿿]", _norm_cjk(subject)))
    if cjk:
        if cjk in t:
            return True
        # 长实体不能靠任意 2 字片段放行：2026-10-20 实测「雪夜古庙」只因命中
        # 「古庙」就收下了缅甸儿童古庙照。多字实体只认**首个 2 字限定词**
        # （通常就是核心限定：雪夜/雪中/林冲）。
        if len(cjk) >= 3 and (cjk[:2] in t or cjk[-2:] in t):
            return True
        # 中文实体没在元数据里命中 → 可能是英文标题，试人工英文别名
        if _alias_hit(cjk, tl):
            return True
        return False
    # 英文只认**专名**（首字母大写的人名/作品名），且排除题材通用词。
    # 不能拿 query_en 里的水/边缘/版画这类词做实体命中：实测 2026-10-20，
    # 林冲的查询里 "Water Margin" 的 water 命中了 Pixabay 的 river 标签，
    # 一张河岸图被当成林冲画像放进了 P03。
    en_words = re.findall(r"[A-Za-z][A-Za-z\-']+", rest)
    proper = [w.lower() for w in en_words
              if w[:1].isupper() and w.lower() not in _GENERIC_EN]
    if proper:
        # 有人名/作品名专名时，专名必须**按词**命中（Water/Margin 这类通用词不算）。
        # 不能用子串：2026-10-20 实测 "Lin" 命中了 Pixabay 的 fleur-de-lin（鸢尾花），
        # 一张花图被当成林冲雪战图。
        text_words = set(re.findall(r"[a-z][a-z\-']+", tl))
        return any(w in text_words for w in proper)
    # 没有专名（如「雪山」这种题材需求）：允许题材词命中，
    # 但只认 query_en 里确实出现的题材词，不做泛化
    generic = [w.lower() for w in en_words if w.lower() in _GENERIC_EN]
    return any(w in tl for w in generic)



# ---------------------------------------------------------------------------
# 取图：本地免费库（assets/local/index.json）——不联网，优先命中
# ---------------------------------------------------------------------------
_LOCAL_LIB = Path(__file__).resolve().parent / "assets" / "local"
_LOCAL_USED: Dict[str, int] = {}
#: 每册已用过的本地图（按项目路径），避免同一张图被多页重复命中
_LOCAL_DECK_USED: Dict[str, Dict[str, int]] = {}


#: 图源域名（TCP 443 探测用）
_PROVIDER_HOSTS: Dict[str, tuple] = {
    "wikimedia": ("commons.wikimedia.org", 443),
    "openverse": ("api.openverse.org", 443),
    "pexels": ("api.pexels.com", 443),
    "pixabay": ("pixabay.com", 443),
    "shuge": ("www.shuge.org", 443),
    "met": ("collectionapi.metmuseum.org", 443),
}
_REACH_CACHE: Dict[str, bool] = {}
_REACH_LOCK = threading.Lock()
#: 并发取图时保护 manifest / rejected / 轮转计数
_STATE_LOCK = threading.RLock()


def _provider_reachable(provider: str, timeout: float = 3.0) -> bool:
    """TCP 探测图源可达性（3s 超时）。结果进程内缓存。"""
    with _REACH_LOCK:
        if provider in _REACH_CACHE:
            return _REACH_CACHE[provider]
    host_port = _PROVIDER_HOSTS.get(provider)
    ok = True
    if host_port:
        try:
            with socket.create_connection(host_port, timeout=timeout):
                ok = True
        except OSError:
            ok = False
    with _REACH_LOCK:
        _REACH_CACHE[provider] = ok
    return ok


def _reachable_providers(providers: Sequence[str]) -> List[str]:
    """并发探测，返回可达的图源。避免每张图都等一轮 20s 超时。"""
    providers = list(providers)
    if not providers:
        return []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        results = list(pool.map(_provider_reachable, providers))
    return [p for p, ok in zip(providers, results) if ok]


#: 本地库：实景照片源 / 认作古画的源（只看来源域名，不看文件名）
_LOCAL_PHOTO_HOSTS = ("pixabay.com", "pexels.com", "unsplash.com")
_LOCAL_ART_HOSTS = ("shuge.org", "commons.wikimedia.org", "metmuseum.org")
_LOCAL_ART_TITLE_RE = re.compile(
    r"画|绘|卷|册|轴|扇面|绢本|纸本|设色|版画|浮世绘|图咏|"
    r"painting|print|ukiyo|scroll|album|woodblock", re.I)


def _local_is_art(item: Dict[str, object]) -> bool:
    """本地库里这张是「古画/版画」还是「实景照片」。

    不看文件名：库里的 `art_lotus_pond.jpg` 其实是 Pixabay 照片（前缀是后人加的）。
    只看来源域名；域名认不出时，才看标题里的画种字眼。
    """
    src = str(item.get("source") or "").lower()
    if any(h in src for h in _LOCAL_PHOTO_HOSTS):
        return False
    if any(h in src for h in _LOCAL_ART_HOSTS):
        return True
    text = f"{item.get('title') or ''} {item.get('attribution') or ''}"
    return bool(_LOCAL_ART_TITLE_RE.search(text))


def fetch_local(project: Path, row: ImageRow, paint_only: bool = False,
                allow_art: bool = True) -> FetchResult:
    """先查本地库：命中就直接复制，不走网络。

    本地库存的是 PD 古画/版画和已下载的 Pixabay 图——「林冲」「雪山」这类
    反复出现的实体不依赖网络，也不会被图库检索漂移带偏。
    """
    index_path = _LOCAL_LIB / "index.json"
    if not index_path.is_file():
        return FetchResult(False, "无本地库")
    try:
        items = json.loads(index_path.read_text(encoding="utf-8")).get("items", [])
    except (json.JSONDecodeError, OSError):
        return FetchResult(False, "本地库索引损坏")

    reference = row.reference or ""
    subject = reference.split("｜", 1)[0] if "｜" in reference else ""
    probe = f"{subject} {reference} {row.get('Purpose') or ''}"
    matches = []
    for it in items:
        if not isinstance(it, dict):
            continue
        for s in it.get("subjects", []):
            if s and (s in probe or (subject and s in subject)):
                matches.append(it)
                break
    if not matches:
        return FetchResult(False, "本地库未命中")
    if paint_only:
        kept = [m for m in matches if _local_is_art(m)]
        if not kept:
            return FetchResult(
                False, f"本地库命中 {len(matches)} 条但全是实景照片（本行要古画/水墨）")
        matches = kept
    elif not allow_art:
        # 非古画行（现代散文/纪实/实景需求）：本地库的古画不能上版面。
        # 2026-10-22 事故：《我与地坛》P08「母亲在园中小径张望」被配上
        # 《御制避暑山庄诗图册》（空园林全景），subject 只命中了「园林」二字。
        kept = [m for m in matches if not _local_is_art(m)]
        if not kept:
            return FetchResult(
                False, f"本地库命中 {len(matches)} 条古画，本行要实景，留给联网图库")
        matches = kept

    deck_key = str(project.resolve())
    with _STATE_LOCK:
        used = _LOCAL_DECK_USED.setdefault(deck_key, {})
        # 同一张本地图在一册里默认**只用 1 次**（用户口径 2026-10-22：
        # 「没有就别放」）。以前允许用 2 次，结果同一册只匹配到一张图时，
        # 相邻两页（P06/P07）放的是完全同一张仕女图（md5 相同）。
        # 唯一例外：封面（P01）允许复用一次，供首尾呼应。
        max_uses = 2 if (row.page_role or "").startswith("P01") else 1
        fresh = [m for m in matches if used.get(m.get("file"), 0) < max_uses]
        if not fresh:
            return FetchResult(False, "本地匹配图已在本册用完（不重复用同一张）")
        it = min(fresh, key=lambda m: used.get(m.get("file"), 0))
        used[it.get("file")] = used.get(it.get("file"), 0) + 1
    src = _LOCAL_LIB / str(it.get("file") or "")
    if not src.is_file():
        return FetchResult(False, f"本地文件缺失 {it.get('file')}")
    dst = project / "images" / row.filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _append_source_record(
        project, row.filename,
        provider="local", title=it.get("title", ""),
        license_name=it.get("license", ""),
        license_tier=it.get("license_tier", "no-attribution"),
        source_page_url=it.get("source", ""),
        width=it.get("width", 0), height=it.get("height", 0),
        search_query="local-library",
        attribution_text=it.get("attribution", ""),
        status="sourced")
    return FetchResult(True, f"本地库命中 {it.get('file')}", "local",
                       match_text=f"{it.get('title', '')} {' '.join(it.get('subjects', []))}")


def run_image_phase(project: Path, skill_dir: Path,
                    source: str = "auto",
                    providers: Sequence[str] = DEFAULT_PROVIDERS,
                    retries: int = DEFAULT_RETRIES,
                    review: str = "score",
                    max_candidates: int = 8,
                    strict: bool = True,
                    p2_diag: bool = True,
                    log: Optional[Callable[[str], None]] = None) -> ImagePhaseReport:
    """执行 P2.6。任何单行失败都只降级，不抛异常。

    p2_diag: 是否打印 P2 加权覆盖的**只读诊断**（默认开）。
        它只多打几行日志，**不参与任何 pass/fail**；要关就 `--no-p2-diag`。

    source: off / web / ai / auto
        off  不做任何事
        web  只取 web 行
        ai   只取 ai 行
        auto 按行声明的 Acquire Via 走（web 失败且配了生图后端才回落 ai）
    review: auto / score / manual（仅 web 行生效，见 fetch_web）
    strict: 相关性闸门。True 时，取到的图与查询对不上就丢弃，
            宁可该页不配图（按用户要求：没有总比错和乱好）。
    """
    report = ImagePhaseReport()
    say = log or (lambda _m: None)

    if source == "off":
        say("   取图：已关闭（--image-source off）")
        return report

    spec_path = project / "design_spec.md"
    lock_path = project / "spec_lock.md"
    if not spec_path.is_file():
        say("   取图：没有 design_spec.md，跳过")
        return report

    header, rows = parse_image_table(spec_path.read_text(encoding="utf-8"))
    targets = [r for r in rows if r.acquire in FETCH_ACQUIRE and r.filename]
    report.total = len(rows)
    report.skipped = len(rows) - len(targets)
    if not targets:
        if rows:
            say("   取图：§VIII 有行但都不是 web/ai，跳过")
        else:
            say("   ⚠️ 取图：§VIII 是空表（模型未声明任何图片需求）→ 本次不会配图。"
                "若本册确实需要照片/古画，请检查 P2 提示词里 §VIII 指令是否被“逐字一致”那条规则抵消。")
        return report

    (project / "images").mkdir(parents=True, exist_ok=True)
    say(f"   取图：§VIII {len(rows)} 行，其中待取 {len(targets)} 行")

    # 无 `｜` 的行拿不到实体锚点 → `_entity_hit` 直接放行。这不是间门 bug，
    # 而是上游提示词没按 `实体｜主题词（英文）` 三段式写。不报出来就永远没人发现。
    anchorless = [r.filename for r in targets if "｜" not in (r.reference or "")]
    if anchorless:
        head = "、".join(anchorless[:4]) + ("…" if len(anchorless) > 4 else "")
        say(f"   ⚠️ 取图：{len(anchorless)}/{len(targets)} 行 Reference 缺 `实体｜` 前缀"
            f"（{head}）→ 这些行实体校验关闭，只靠题材覆盖")

    # 一次性探测图源可达性：不可达的直接跳过，别让每张图都等一轮超时
    available: set = set(providers)
    if source in ("web", "auto"):
        available = set(_reachable_providers(list(providers)))
        dead = [p for p in providers if p not in available]
        if dead:
            say(f"   取图：跳过不可达图源 {', '.join(dead)}（可达：{', '.join(sorted(available)) or '无'}）")

    used_texts: set = set()
    diag = P2Diag()
    stats_lock = threading.Lock()

    def handle_row(row: ImageRow) -> None:
        if row.acquire == "web" and source in ("web", "auto"):
            arts_row = _is_arts_row(row)
            want_paint = (arts_row or _is_classical_row(row)) and _wants_painting(row)
            # 逐家失败原因要留下来：否则最后只显示「最后一家」的报错，
            # 用户看不到「Met 有 9 条但都不是公有领域」「wikimedia 超时」这类关键信息
            notes: List[str] = []

            def _attempt(alt: ImageRow) -> FetchResult:
                """把一行（或它的备选查询）对所有图源跑一遍，返回值不抛异常。"""
                local_result = fetch_local(project, alt, paint_only=want_paint,
                                           allow_art=want_paint)
                result = FetchResult(False, "未尝试")
                providers_try = _provider_order_for(
                    alt,
                    DEFAULT_PROVIDERS if providers is DEFAULT_PROVIDERS else providers)
                providers_try = [p for p in providers_try if p in available]
                # 古画行不跑「只出实景照片」的图库：跑了也只能拿回彩照，
                # 而彩照落到宣纸版面上就是风格事故（岳飞那册 P09 雪山照）。
                # 宁可不配（该页退化成排版页，版面不破），也不上错风格。
                if want_paint:
                    dropped = [p for p in providers_try if p in _STOCK_PHOTO_PROVIDERS]
                    providers_try = [p for p in providers_try
                                     if p not in _STOCK_PHOTO_PROVIDERS]
                    if dropped:
                        say(f"     · 本行要古画/水墨：跳过实景图库 {','.join(dropped)}")
                # 本地库也用「虚拟图源 local」进同一个循环：
                # 以前 local 命中就把 providers_try 清空，等于**绕过全部闸门**
                # （只要索引词命中就直接上版面）。现在本地图同样要过
                # relevance / 实体 / 题材覆盖，不过就换图库。
                if local_result.ok:
                    providers_try = ["local"] + providers_try
                    say(f"     · 本地库候选：{local_result.note}")
                for provider in providers_try:
                    if provider == "local":
                        result = local_result
                    elif provider == "shuge":
                        result = fetch_shuge(project, alt, skill_dir)
                    elif provider == "pixabay":
                        result = fetch_pixabay(project, alt, skill_dir,
                                               max_candidates=max_candidates)
                    elif provider == "met":
                        result = fetch_met(project, alt, skill_dir,
                                           max_candidates=max_candidates)
                    else:
                        override = ""
                        if (arts_row and provider in ("wikimedia", "openverse")
                                and "｜" in (alt.reference or "")):
                            override = (alt.reference or "").split("｜", 1)[0].strip()
                        result = fetch_web(project, alt, skill_dir, providers=[provider],
                                           retries=retries, review=review,
                                           max_candidates=max_candidates,
                                           query_override=override)
                    if result.needs_selection:
                        break
                    if not result.ok:
                        notes.append(f"{provider}:{result.note}")
                        continue
                    # ── 相关性闸门放在循环**内**：被拒就换下一家 ──
                    # 实测事故（2026-10-19 紫藤萝）：书格先「成功」拿到不相干的花册，
                    # 闸门在循环之后才判，拒了也不会回头试 Pixabay，结果整册没配图，
                    # 而 Pixabay 上其实有 255+ 张紫藤。
                    if not strict:
                        break
                    probe_text = (result.match_text
                                  or _manifest_match_text(project, alt.filename))
                    score = relevance(alt.reference or alt.get("Purpose"), probe_text)
                    # 实体门槛对**所有**带 `实体｜` 前缀的行生效（不只 arts）：
                    # 2026-10-20 赤壁赋事故——P07「洞箫」收下危地马拉排箫老头、
                    # P12「月夜江舟」收下伦敦塔桥，都是非 arts 行只走 relevance 的结果。
                    entity_ok = (_entity_hit(alt.reference or "", probe_text)
                                 and _term_coverage(alt.reference or "", probe_text))
                    if not entity_ok and provider in _ART_SOURCES:
                        # 古画源：中文意象词 ↔ 英文藏品名的错配不算「实体未命中」
                        entity_ok = True
                    if result.trusted:
                        # 人工索引点名的书（意象 → 书页）：字面必然 0 命中，但那是对的图。
                        # 例：《关山积雪图卷》对应「雪山关隘古道」，字上一个词不重合。
                        # **风格闸门仍在下面生效**：索引只免除「对不对得上」，不免「像不像」。
                        entity_ok = True
                    if arts_row and _style_mismatch(probe_text):
                        entity_ok = False
                        why_style = "风格不符（水墨/古画需求命中现代/照片特征）"
                    else:
                        why_style = ""
                    with stats_lock:
                        duplicate = probe_text in used_texts
                        accepted = ((score >= MIN_RELEVANCE or result.trusted)
                                    and entity_ok and not duplicate)
                        if accepted:
                            used_texts.add(probe_text)
                    # —— P2 加权诊断：只读，不改变上面算好的 accepted ——
                    # 一行一打：一条取图日志里就能看出「现判」与「加权判」差在哪。
                    if p2_diag:
                        _p2_diagnose(alt, probe_text, accepted, say, diag)
                    if accepted:
                        break
                    (project / "images" / alt.filename).unlink(missing_ok=True)
                    _remove_source_record(project, alt.filename)
                    why = (why_style if why_style
                           else f"相关度 {score:.2f} < {MIN_RELEVANCE}"
                           if score < MIN_RELEVANCE
                           else "实体未命中（结果里没有该人物/器物）")
                    if score >= MIN_RELEVANCE and entity_ok and duplicate:
                        why = "与本册已用图片重复"
                    say(f"     · {provider}: {why}，换下一家")
                    if why != "与本册已用图片重复":
                        _record_rejected(project, provider, probe_text, why,
                                         filename=alt.filename,
                                         reference=alt.reference or "")
                    result = FetchResult(False, f"{provider} 结果不合格（{why}）")
                if (not result.ok and not result.needs_selection and notes):
                    result = FetchResult(False, "｜".join(notes[:4]))
                return result

            # ── 先按主查询（人名/实体）取，取不到再用备选（意象）取一次 ──
            # 用户口径 2026-10-22：「人物画像搜不到就算，肯定优先匹配人名」。
            main_ref, fallback_ref = split_fallback(row.reference or "")
            first_row = row
            if main_ref and main_ref != (row.reference or ""):
                first_row = ImageRow(dict(row.cells))
                first_row.set("Reference", main_ref)
            result = _attempt(first_row)
            if fallback_ref and not result.ok and not result.needs_selection:
                say(f"     · {row.filename}: 主查询未命中 → 改用备选「{fallback_ref[:30]}」")
                alt_row = ImageRow(dict(row.cells))
                alt_row.set("Reference", fallback_ref)
                notes.clear()
                result = _attempt(alt_row)
            if (not result.ok and not result.needs_selection
                    and source == "auto" and (os.getenv("IMAGE_BACKEND") or "").strip()):
                say(f"     · {row.filename}: web 失败（{result.note}）→ 尝试 AI 生图")
                result = fetch_ai(project, row, skill_dir)
        elif row.acquire == "ai" and source in ("ai", "auto"):
            result = fetch_ai(project, row, skill_dir)
        else:
            result = FetchResult(False, "本次未启用该来源")

        apply_mechanics(project, row, result.ok, result.needs_selection)
        if result.ok:
            with stats_lock:
                report.fetched += 1
            say(f"     ✓ {row.filename}: {result.note}")
        elif result.needs_selection:
            with stats_lock:
                report.skipped += 1
            say(f"     ⏸ {row.filename}: {result.note}")
        else:
            with stats_lock:
                report.failed += 1
            say(f"     ✗ {row.filename}: {result.note} → Status=Needs-Manual（该页退回原生 SVG）")

    # 多页并发取图：每行互相独立，manifest/去重已加锁
    workers = max(1, min(4, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(handle_row, targets))

    # 回写 §VIII（先落盘，后面投影 lock 才有据可依）
    write_image_table(spec_path, header, rows)
    written = sync_lock_images(lock_path, rows)
    say(f"   取图：§VIII 已回写；spec_lock.md ## images 写入 {written} 条")
    say("   " + report.summary())
    if p2_diag:
        diag.summary(say)
    return report


#: 配图验收的内置回归用例：(说明, Reference, 图元数据文本, 期望是否收)
#:
#: 为什么放代码里而不是测试目录：这层规则的事故都长得一模一样（“跟需求只差一个
#: 通用词”），必须能在**任何机器上一条命令复跑**。改 `_term_coverage` / `_entity_hit` /
#: `relevance` / 词表之前，先跑 `python image_pipeline.py --selftest`。
SELFTEST_CASES: Tuple[Tuple[str, str, str, bool], ...] = (
    # —— 必须拒（每一行都对应一次真实事故）——
    ("猫片冒充李清照画像（2026-10-22）",
     "李清照画像｜李清照 画像 古画（Li Qingzhao portrait）",
     "猫, 宠物, 猫科动物, 蓝眼睛, 国内的猫, 猫画像, 动物世界", False),
    ("秋天向日葵冒充折枝春花（2026-10-22）",
     "折枝春花（中国古画）｜中国 古画 折枝 花卉",
     "秋天, 八月, 植物, 花, 向日葵, 落叶", False),
    ("纯月亮照冒充月夜江面小舟（2026-10-21）",
     "月夜江面小舟｜水墨 月夜 江面 小舟（Chinese ink painting）",
     "月出, 月亮, 满月, 夜晚, 月夜, 气氛", False),
    ("缅甸古庙照冒充雪夜古庙（2026-10-20）",
     "雪夜古庙｜雪夜 古庙（snow night temple）",
     "children in front of an old temple Myanmar", False),
    ("鸢尾花冒充林冲（fleur-de-lin，2026-10-20）",
     "林冲｜林冲（Lin Chong）", "fleur-de-lin iris flower closeup", False),
    ("河岸图冒充林冲（Water Margin 的 water）",
     "林冲｜林冲（Lin Chong, Water Margin）", "river bank at dusk water", False),
    # —— 必须收（防过度收紧）——
    ("中国水墨山水：公共子串最长只能到「山水」（防误伤）",
     "中国水墨山水｜中国 水墨 山水 淡墨（Chinese ink painting）",
     "中国山水, 中国, 山水, 中国风, 自然, 湖", True),
    ("李清照：英文元数据的正确古画（靠别名命中）",
     "李清照画像｜李清照 画像 古画（Li Qingzhao portrait）",
     "Li Qingzhao portrait, Song dynasty ink on silk", True),
    ("荷塘白荷：散文课用真实荷花照也得能用",
     "荷塘月色｜荷塘 月色（lotus pond）", "荷塘白荷（Pixabay）", True),
    ("紫藤萝：稀疏中文标签也要能用",
     "紫藤萝花串｜紫藤萝 花串 垂落（wisteria）",
     "紫藤萝, 公园, 春季, 紫色, 花", True),
    ("无实体前缀的旧格式：正确结果仍要放行",
     "赤壁赋书画卷 舟中宾客", "前后赤壁赋书画卷", True),
)

#: 检索词回归：(说明, Reference, 检索词里必须出现的片段)
SELFTEST_QUERY_CASES: Tuple[Tuple[str, str, str], ...] = (
    ("实体没在查询里重复时，要补进检索词",
     "李清照｜画像 古画（Li Qingzhao portrait）", "李清照"),
    ("实体带类型后缀时，不要带「画像」回去",
     "李清照画像｜画像 古画", "李清照"),
    ("正常三段式不被破坏",
     "宝黛初会｜宝黛初会 工笔人物画（portrait）", "宝黛初会"),
)


def selftest(log: Callable[[str], None] = print) -> int:
    """跑配图验收的内置回归。返回失败数（0 = 全绿）。"""
    bad = 0
    log("【配图验收】Reference × 图元数据 → 应不应该收")
    for label, ref, text, want in SELFTEST_CASES:
        try:
            got = (relevance(ref, text) >= MIN_RELEVANCE
                   and _entity_hit(ref, text)
                   and _term_coverage(ref, text))
        except Exception as exc:            # noqa: BLE001 自测自己崩了也是失败
            log(f"  ✗ {label} → 抛异常 {type(exc).__name__}: {exc}")
            bad += 1
            continue
        ok = got == want
        bad += 0 if ok else 1
        log(f"  {'✓' if ok else '✗'} [{'收' if got else '拒'}] {label}")

    log("")
    log("【检索词】实体必须进检索词")
    for label, ref, must in SELFTEST_QUERY_CASES:
        main, _ = split_query_variants(ref)
        ok = must in main
        bad += 0 if ok else 1
        log(f"  {'✓' if ok else '✗'} {label} → 查询={main!r}")

    log("")
    log("【导出兜底】指向缺失文件的 <image> 必须被剥掉（否则整册导不出）")
    try:
        # 懒导入：staged.py 在模块级 from image_pipeline import ...，
        # 顶层反向导入会成环。跑 selftest 时 image_pipeline 已加载完毕，安全。
        from staged import strip_unresolved_images  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "svg_output"
            (d / "images").mkdir(parents=True)
            (d / "images" / "ok.jpg").write_bytes(b"x" * 16)
            (d / "a.svg").write_text(
                '<svg><image href="images/ok.jpg" width="1" height="1"/></svg>', encoding="utf-8")
            (d / "b.svg").write_text(
                '<svg><image href="../images/missing.jpg" width="9" height="9"/><text>x</text></svg>',
                encoding="utf-8")
            (d / "c.svg").write_text(
                '<svg><image href="https://example.com/y.jpg" width="1" height="1"/></svg>',
                encoding="utf-8")
            n = strip_unresolved_images(d)
            kept_a = "<image" in (d / "a.svg").read_text(encoding="utf-8")
            gone_b = "<image" not in (d / "b.svg").read_text(encoding="utf-8")
            kept_c = "<image" in (d / "c.svg").read_text(encoding="utf-8")
            ok = (n == 1 and kept_a and gone_b and kept_c)
            bad += 0 if ok else 1
            log(f"  {'✓' if ok else '✗'} 剥掉 {n} 个｜有图保留={kept_a}｜缺图剥掉={gone_b}"
                f"｜外链保留={kept_c}")
    except Exception as exc:                   # noqa: BLE001
        bad += 1
        log(f"  ✗ 兜底用例抛异常 {type(exc).__name__}: {exc}")

    log("")
    log(f"{'✅ 全绿' if bad == 0 else f'❌ {bad} 条失败'}"
        f"（验收 {len(SELFTEST_CASES)} + 检索词 {len(SELFTEST_QUERY_CASES)} + 导出兜底 1）")
    return bad


# ---------------------------------------------------------------------------
# 离线审计：P3 排序明细（不联网、不改项目）
# ---------------------------------------------------------------------------
def explain_style(manifest: Path, purpose: str = "", reference: str = "") -> int:
    """打印一份 candidates.json 的 P0 罚分 + P3 偏好明细，用于验证排序改动。

    不联网、不写项目：用来回答「这批评分改动到底把哪一张顶上来了」。
    """
    row: Optional[ImageRow] = None
    if purpose or reference:
        row = ImageRow()
        if purpose:
            row.set("Purpose", purpose)
        if reference:
            row.set("Reference", reference)
    items = _load_candidates(manifest)
    if not items:
        print(f"✗ 候选为空或不是 candidates.json：{manifest}")
        return 2
    ranked = sorted(items, key=lambda it: rank_key(it, row), reverse=True)
    print(f"   {manifest}（{len(ranked)} 张候选）")
    if row is not None:
        print(f"   行上下文：{row.get('Reference') or row.get('Purpose')}")
    for i, it in enumerate(ranked, 1):
        title = str(it.get("title") or it.get("file") or it.get("filename") or "")[:52]
        bonus, why = style_preference(row, it)
        hits, all_terms = _zh_hits((row.reference if row else "") or "", _candidate_text(it))
        junk = "  ⚠P0废图标题" if JUNK_TITLE_RE.search(str(it.get("title") or "")) else ""
        print(f"   {i:>2}. 命中{len(hits)}/{len(all_terms)}  {score_candidate(it, row):>6.2f}  "
              f"风格{bonus:>+5.2f}  {' '.join(why) or '-'}{junk}  {title}")
    return 0


# ---------------------------------------------------------------------------
# 离线回归：P2 加权 vs 现役阀门（不联网、不写文件）
# ---------------------------------------------------------------------------
def audit_p2(root: Path, log: Callable[[str], None] = print) -> int:
    """量一个真正要担心的事：**现在收下的图，P2 加权会误杀几张**。

    扫 root 下所有已产出的 PPT 项目（有 design_spec.md + images/image_sources.json），
    把图重新过一遍「现役阀门」与「P2 加权」，只读、不联网、不写文件。

    局限：`rejected.json` 只存元数据不存文件名，所以只能对账**已收下**的图，
    「加权能多拦几张错图」这一侧只能靠 `[p2-diag]` 在线日志攒。
    """
    specs = sorted(root.rglob("design_spec.md"))
    if not specs:
        log(f"✗ {root} 下没有 design_spec.md")
        return 2

    n = skipped = 0
    agree_accept = agree_reject = tighten = widen = 0
    tighten_lines: List[str] = []
    projects = 0
    #: 被拒侧：只有本条改动之后记录的 rejected.json 才带 filename 与 reference
    rej_n = rej_kept = rej_skipped = 0
    rej_kept_lines: List[str] = []

    for spec in specs:
        proj = spec.parent
        src = proj / "images" / "image_sources.json"
        if not src.is_file():
            continue
        try:
            _, rows = parse_image_table(spec.read_text(encoding="utf-8"))
            items = json.loads(src.read_text(encoding="utf-8")).get("items", [])
        except (OSError, json.JSONDecodeError):
            continue
        by_name = {r.filename: r for r in rows if r.filename}
        used: List[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            row = by_name.get(str(it.get("filename") or ""))
            if row is None:
                continue
            text = " ".join(str(it.get(k) or "") for k in ("title", "tags", "description"))
            if not text.strip():
                skipped += 1        # 手放入库图没有元数据，两边都无话可说
                continue
            ref = row.reference or ""
            cur = (relevance(ref or row.get("Purpose"), text) >= MIN_RELEVANCE
                   and _entity_hit(ref, text) and _term_coverage(ref, text))
            if cur and _is_arts_row(row) and _style_mismatch(text):
                cur = False         # P0 风格否决也在现役阀门里
            ratio, full, _, _ = p2_weighted_coverage(ref, text)
            if ratio < 0:
                skipped += 1
                continue
            weight = ratio >= P2_PASS
            n += 1
            if cur and weight:
                agree_accept += 1
            elif not cur and not weight:
                agree_reject += 1
            elif weight:
                widen += 1         # 现判拒、加权收：加权会放松
            else:
                tighten += 1       # 现判收、加权拒：加权会误杀
                if len(tighten_lines) < 15:
                    tighten_lines.append(
                        f"{proj.name}/{row.filename}｜加权 {ratio:.2f}｜{ref[:46]}")
            used.append(f"{1 if weight else 0}{1 if cur else 0}")

        # —— 被拒侧：量「P2 加权会漏掉多少现有拦截」——
        # 旧 rejected.json 没有 filename/reference，只能跳过，不能拿它冒充结论。
        rej_f = proj / "images" / "rejected.json"
        if rej_f.is_file():
            try:
                rej_items = json.loads(rej_f.read_text(encoding="utf-8")).get("items", [])
            except (OSError, json.JSONDecodeError):
                rej_items = []
            for it in rej_items:
                if not isinstance(it, dict):
                    continue
                fn = str(it.get("filename") or "")
                ref = str(it.get("reference") or "")
                text = str(it.get("text") or "")
                row = by_name.get(fn)
                if row is not None and not ref:
                    ref = row.reference or ""
                if not fn or not ref or not text.strip():
                    rej_skipped += 1
                    continue
                r2, _, _, _ = p2_weighted_coverage(ref, text)
                if r2 < 0:
                    rej_skipped += 1
                    continue
                rej_n += 1
                if r2 >= P2_PASS:
                    rej_kept += 1
                    if len(rej_kept_lines) < 10:
                        rej_kept_lines.append(
                            f"{proj.name}/{fn}｜加权 {r2:.2f}｜"
                            f"原拒因 {str(it.get('reason') or '')[:20]}"
                            f"｜文本 {text[:32]}")
        if used:
            projects += 1
            got_w = sum(1 for u in used if u[0] == "1")
            log(f"   {proj.name}: {len(used)} 张元数据可用，加权通过 {got_w}")

    if not n and not rej_n:
        log("✗ 没有可对账的自动取图（全是手放入库图 / 空元数据 / 旧 rejected 记录）")
        return 1
    log("")
    log(f"   [p2-audit] {projects} 个项目 / {n} 张自动取图（无元数据跳过 {skipped}）")
    log(f"   [p2-audit] 一致 {agree_accept + agree_reject}"
        f"（收 {agree_accept}/拒 {agree_reject}）"
        f"｜分歧 {tighten + widen}（加权误杀 {tighten}/加权放松 {widen}）")
    log(f"   [p2-audit] 加权通过率 {agree_accept + widen}/{n}"
        f" = {(agree_accept + widen) / n:.0%}（现役为 {(agree_accept + tighten) / n:.0%}）")
    if tighten_lines:
        log("   [p2-audit] 加权会误杀的清单（前 15）：")
        for line in tighten_lines:
            log(f"     · {line}")
    else:
        log("   [p2-audit] 本批无误杀（样本小不等于安全）——凑满 30+ 张再谈接管")
    if rej_n:
        log(f"   [p2-audit] 被拒侧 {rej_n} 条可对账（旧记录缺 filename 跳过 {rej_skipped}）："
            f"加权会收下其中 {rej_kept} 条 = **会漏掉现有拦截**")
        for line in rej_kept_lines:
            log(f"     · {line}")
    elif rej_skipped:
        log(f"   [p2-audit] 被拒侧 {rej_skipped} 条全部缺 filename/reference（旧记录）"
            f"——跑一册新任务后才能量「多拦」这一侧")
    return 0


# ---------------------------------------------------------------------------
# CLI（独立调试用，正常由 staged.py 调用）
# ---------------------------------------------------------------------------
def main() -> int:
    global P2_PASS
    import argparse
    ap = argparse.ArgumentParser(description="对已有项目跑一次 P2.6 取图")
    ap.add_argument("project", nargs="?", default=".",
                    help="项目目录（含 design_spec.md / spec_lock.md）")
    ap.add_argument("--skill-dir", default=None, help="ppt-master skill 根目录")
    ap.add_argument("--image-source", choices=("off", "web", "ai", "auto"), default="auto")
    ap.add_argument("--image-review", choices=("auto", "score", "manual"), default="score",
                    help="web 行审图方式：auto=元数据直揃（易夹废图）/score=候选集评分（默认）/manual=只备候选")
    ap.add_argument("--max-candidates", type=int, default=8)
    ap.add_argument("--no-strict", action="store_true",
                    help="关闭相关性闸门（默认开启：图与查询对不上就丢弃，宁可不配图）")
    ap.add_argument("--proxy", default=None,
                    help="仅取图子进程用的代理，如 http://127.0.0.1:7897（也可用环境变量 PPT_IMAGE_PROXY）")
    ap.add_argument("--add", action="append", default=[], metavar="FILE=URL",
                    help="把一个图片直链入库（可重复）：--add 宝黛初会.jpg=https://...")
    ap.add_argument("--page", default="", help="配合 --add：页码标记，如 'P03 宝黛初会'")
    ap.add_argument("--purpose", default="", help="配合 --add：用途说明")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS),
                    help="逗号分隔的图库顺序")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    ap.add_argument("--explain-style", default=None, metavar="CANDIDATES_JSON",
                    help="离线审计：打印候选的 P0/P3 排序明细（不联网、不改项目）；"
                         "可配合 --purpose / --reference 提供行上下文")
    ap.add_argument("--reference", default="",
                    help="配合 --explain-style：模拟 §VIII 的 Reference 列")
    ap.add_argument("--audit-p2", default=None, metavar="ROOT",
                    help="离线回归：扫 ROOT 下所有已产出项目，量 P2 加权对现役阀门的误杀率"
                         "（不联网、不写文件）")
    ap.add_argument("--p2-pass", type=float, default=None, metavar="FLOAT",
                    help=f"临时覆盖 P2 加权阈值（默认 {P2_PASS}），配合 --audit-p2 标定用")
    ap.add_argument("--no-p2-diag", action="store_true",
                    help="关闭取图时的 P2 加权只读诊断日志（默认开）")
    ap.add_argument("--selftest", action="store_true",
                    help="跑配图验收的内置回归用例（不联网、不改项目）；改验收规则前先跑")
    args = ap.parse_args()

    if args.selftest:
        return 1 if selftest() else 0

    if args.p2_pass is not None:
        P2_PASS = args.p2_pass

    if args.audit_p2:
        return audit_p2(Path(args.audit_p2).expanduser().resolve())

    if args.explain_style:
        return explain_style(Path(args.explain_style).expanduser().resolve(),
                             purpose=args.purpose, reference=args.reference)

    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(f"✗ 目录不存在：{project}")
        return 2
    if args.proxy:
        os.environ[PROXY_ENV] = args.proxy

    skill_dir = Path(args.skill_dir).expanduser().resolve() if args.skill_dir else resolve_skill_dir(project)
    if skill_dir is None:
        print("✗ 找不到 ppt-master skill，请用 --skill-dir 指定")
        return 2

    # —— 模式 A：把外部选定的直链入库 ——
    if args.add:
        spec = project / "design_spec.md"
        lock = project / "spec_lock.md"
        if not spec.is_file():
            print(f"✗ 没有 design_spec.md：{spec}")
            return 2
        all_rows: List[ImageRow] = []
        for item in args.add:
            if "=" not in item:
                print(f"✗ 格式应为 FILE=URL：{item}")
                return 2
            filename, _, url = item.partition("=")
            filename, url = filename.strip(), url.strip()
            row = ensure_row(spec, filename, page_role=args.page, purpose=args.purpose)
            result = fetch_from_url(project, row, url, skill_dir)
            apply_mechanics(project, row, result.ok)
            mark = "✓" if result.ok else "✗"
            print(f"   {mark} {filename}: {result.note}")
            all_rows.append(row)
        header, rows = parse_image_table(spec.read_text(encoding="utf-8"))
        write_image_table(spec, header, rows)
        written = sync_lock_images(lock, rows)
        print(f"   spec_lock.md ## images 已写入 {written} 条")
        return 0

    provs = tuple(p.strip() for p in args.providers.split(",") if p.strip())
    report = run_image_phase(
        project, skill_dir, source=args.image_source,
        providers=provs,
        retries=args.retries, review=args.image_review,
        max_candidates=args.max_candidates, strict=not args.no_strict,
        p2_diag=not args.no_p2_diag,
        log=lambda m: print(m))
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
