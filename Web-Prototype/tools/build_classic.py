#!/usr/bin/env python3
"""把 ESM 运行时打包成「经典脚本」，让页面双击打开（file://）也能跑。

为什么需要这个
--------------
`file://` 下有两件事会被浏览器直接拦掉，而首屏两个都踩中：

  1. `<script type="module">` —— 模块要求 CORS 同源，本地文件算不透明源，
     浏览器**根本不会执行**这个脚本。所以 film-runtime.js 从未运行，
     `data-film` 没被设置，画面就停在静态。
  2. `fetch('build/timeline.json')` —— 同样是 CORS，`file://` 下必失败。

结果就是：走服务器一切正常，双击 HTML 完全不动。这个坑本项目实际踩过一次。

产物（都是生成物，不要手改）
----------------------------
  js/film-runtime.classic.js   内联了 vendor 的经典脚本版本，暴露 window.OilMotionFilm
  build/timeline.js            时间轴的经典脚本版本，写 window.OIL_TIMELINE

什么时候要重跑
--------------
改了下面任何一项之后：
  js/vendor/interactive-motion.js
  js/film-runtime.js
  build/timeline.json
只想验证产物是否最新，用 --check。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "js" / "vendor" / "interactive-motion.js"
RUNTIME = ROOT / "js" / "film-runtime.js"
TIMELINE_JSON = ROOT / "build" / "timeline.json"

OUT_BUNDLE = ROOT / "js" / "film-runtime.classic.js"
OUT_TIMELINE = ROOT / "build" / "timeline.js"

BANNER = "/* 生成物 —— 由 tools/build_classic.py 生成，不要手改。源头：{sources} */\n"

# vendor 文件的具名导出；必须与 interactive-motion.js 的 export 一致。
VENDOR_EXPORTS = ("createFrameAnimator", "createCssSpriteRenderer", "createSegmentPlayer")


def strip_esm_exports(src: str) -> str:
    """去掉行首的 `export `，把 ESM 变成经典脚本可解析的代码。"""
    return re.sub(r"^export\s+", "", src, flags=re.MULTILINE)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def digest(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def build_bundle() -> str:
    vendor = strip_esm_exports(read(VENDOR))
    runtime = read(RUNTIME)

    # film-runtime.js 里的 import 行在经典脚本里非法，换成从全局取。
    runtime, n = re.subn(
        r"^import\s*\{[^}]*\}\s*from\s*['\"][^'\"]*['\"];\s*$",
        "const { " + ", ".join(VENDOR_EXPORTS) + " } = window.OilMotionVendor;",
        runtime,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise SystemExit(f"预期 film-runtime.js 里恰好 1 条 import，实际 {n} 条 —— 请检查源文件")

    return (
        BANNER.format(sources="js/vendor/interactive-motion.js + js/film-runtime.js")
        + f"/* 输入摘要 {digest(VENDOR, RUNTIME)} */\n"
        + "(function () {\n'use strict';\n\n"
        # vendor 与 runtime 各占一个作用域：两边都有 clamp/wrap 这类短名顶层声明，
        # 放进同一个作用域会重名（实际撞过一次 createFrameAnimator）。
        + "/* ── 以下来自 js/vendor/interactive-motion.js（ESM 的 export 已去掉）── */\n"
        + "(function () {\n"
        + vendor
        + "\nwindow.OilMotionVendor = { " + ", ".join(VENDOR_EXPORTS) + " };\n"
        + "})();\n\n"
        + "/* ── 以下来自 js/film-runtime.js ── */\n"
        + "(function () {\n"
        + runtime
        + "\n})();\n\n"
        + "})();\n"
    )


def build_timeline() -> str:
    data = json.loads(read(TIMELINE_JSON))
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        BANNER.format(sources="build/timeline.json")
        + "/* file:// 下 fetch 会被 CORS 拦掉，所以时间轴以经典脚本注入。\n"
        + "   走服务器时 film-runtime 也优先用这一份，避免两份数据打架。 */\n"
        + f"window.OIL_TIMELINE = {body};\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只检查产物是否最新，不写入")
    args = ap.parse_args()

    for p in (VENDOR, RUNTIME, TIMELINE_JSON):
        if not p.exists():
            raise SystemExit(f"缺输入文件：{p.relative_to(ROOT)}")

    want = {OUT_BUNDLE: build_bundle(), OUT_TIMELINE: build_timeline()}

    if args.check:
        stale = [p for p, text in want.items() if not p.exists() or read(p) != text]
        if stale:
            for p in stale:
                print(f"  过期：{p.relative_to(ROOT)}", file=sys.stderr)
            print("  → 跑 python tools/build_classic.py 重新生成", file=sys.stderr)
            return 1
        print("  经典脚本产物是最新的 ✓")
        return 0

    for p, text in want.items():
        p.write_text(text, encoding="utf-8")
        print(f"  写入 {str(p.relative_to(ROOT)):<32} {len(text.encode('utf-8'))/1024:6.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
