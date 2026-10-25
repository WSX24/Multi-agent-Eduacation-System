#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包教学流水线（TeachingFlow）成可上传的压缩包。

只带走「能让链路在别人机器上跑起来」的文件，绝不带：

  · runs/、state/、.work/、__pycache__      —— 运行产物
  · .env                                    —— API Key
  · ppt-master/**/projects、docs、_failed_run_backup —— 示例产物/文档

三档（`--profile`）：

  lite      我们的代码 + PromptTemplate + PPT-Agent 代码 + 集成路由的 export_notes
            不含 ppt-master（对方自己 clone，并设 PPT_MASTER_SKILL_DIR）
  standard  再加 ppt-master 的 SKILL.md / scripts / workflows / 常用模板与 references
            （不含 icons、sounds、ai-image-comparison 这些大素材）
  full      standard + templates/icons + templates/sounds（离线图标/音效齐全）

用法::

    python tools/package_release.py                    # 默认 standard
    python tools/package_release.py --profile full
    python tools/package_release.py --out D:/dist
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # TeachingFlow/
ROOT = HERE.parent                                      # 自建Agent/

#: 目录整体搬运时的排除项（相对该目录）
EXCLUDE_DIRS = {"runs", "state", ".work", "__pycache__", ".git", "projects",
                "_failed_run_backup", "recovered", "dist"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}
EXCLUDE_NAMES = {".env", "~$", ".DS_Store", "last_report.json"}

REQUIREMENTS = """\
# 必需（链路本体）
openai>=1.30          # 调模型
PyYAML>=6.0           # 模板 / 蓝图 / sidecar
python-pptx>=0.6.21   # 导讲稿、页面说明（读 PPTX 备注/结构）
click>=8.1            # PPT-Agent/pipeline.py 的 CLI
requests>=2.31        # 联网取图
Pillow>=9.0           # 配图处理
# svg → pptx（ppt-master 的转换器；缺了会退化为只出 SVG）
XlsxWriter>=3.0
skia-pathops>=0.9.2
uharfbuzz>=0.50
numpy>=1.20
"""

README = """\
# TeachingFlow 教学流水线（打包产物）

生成时间：{ts}　档位：**{profile}**

## 目录要求（重要）

保持下面的相对位置，代码里的路径是按互相位置算出来的：

```
自建Agent/
├── TeachingFlow/            # 本链路（入口 flow.py）
├── PromptTemplate/          # 模板库（teacher_planner / teacher_unit / teacher_ppt + fewshot）
├── PPT-Agent/               # 产片引擎（pipeline.py）
├── 集成路由V1.0/            # 只用它的 export_notes.py（导讲稿 / 页面说明）
└── ppt-master/skills/ppt-master/   # 产片 skill（standard/full 档自带；lite 档需自备）
```

lite 档没有 ppt-master，请自行放置或设置环境变量：

```bash
# Windows
set PPT_MASTER_SKILL_DIR=D:\\path\\to\\ppt-master\\skills\\ppt-master
# macOS / Linux
export PPT_MASTER_SKILL_DIR=/path/to/ppt-master/skills/ppt-master
```

完整使用说明（流程 / 产物路径 / 调试 / 生产）见 **TeachingFlow/docs/README.md**，设计取舍见
**TeachingFlow/CHAIN.md**。

## 安装

```bash
pip install -r requirements.txt
```

## 配置（不要提交到仓库）

复制 `.env.example` 为 `PPT-Agent/.env`，填入 Key：

```
DEEPSEEK_API_KEY=你的key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
# 联网配图（可选，缺省走 wikimedia/openverse，pixabay 需 key）
PIXABAY_API_KEY=
PEXELS_API_KEY=
```

## 跑一句话

```bash
cd TeachingFlow
python flow.py "我想简单学一下《祥林嫂》这篇课文"
python flow.py "我今天突击一下生物的光合作用" --no-input
python flow.py "我想系统学习高中语文" --no-input --mock --to plan   # 不调模型，验链路
```

产物在 `TeachingFlow/runs/<日期>/<课程>_<时分秒>/`：

```
LLM返回/                     # 每次调用的「模型原文 + 提示词」（排查首选）
第1章-章名/第M单元-单元名.pptx  # 交付件（+ -讲稿.md + -页面说明.md）
00_spec.json 01_blueprint.yaml 02_targets.json …
05_ppt_<uid>/                # 产片工作现场（design_spec / svg / 日志）
manifest.json                # 每步 ok / 校验项 / 页数与周期依据
```

## 自检（不花 token）

```bash
cd TeachingFlow
python tools/smoke_cases.py     # 意图判定用例
python tools/prompt_lint.py     # 模板里不许出现示例页数
```

## 打包里刻意没有的东西

| 没带 | 原因 |
|---|---|
| `runs/`、`state/`、`.work/` | 运行产物 |
| `.env` | 含 API Key |
| `ppt-master/**/projects`、`docs` | 示例产物与文档，与运行无关 |
| `references/ai-image-comparison`、`templates/sounds`（standard 档） | 44M/13M 的非必需素材，full 档才带 |
"""


def _skip(p: Path) -> bool:
    name = p.name
    if name in EXCLUDE_DIRS or name in EXCLUDE_NAMES:
        return True
    if name.startswith("~$") or name.startswith(".") and name not in (".env.example",):
        return True
    return p.suffix.lower() in EXCLUDE_SUFFIXES


def copy_tree(src: Path, dst: Path, *, skip_names: set[str] | None = None) -> int:
    """把目录按白名单规则拷进 dst（跳过 runs/env/缓存等）。返回文件数。"""
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for item in sorted(src.iterdir()):
        if _skip(item):
            continue
        if skip_names and item.name in skip_names:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            n += copy_tree(item, target, skip_names=skip_names)
        elif item.is_file():
            shutil.copy2(item, target)
            n += 1
    return n


def build(profile: str, out_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d")
    name = f"TeachingFlow-{profile}-{ts}"
    stage = out_dir / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    total = 0
    # 1) 本链路（排除 runs/.work/state/__pycache__/recovered）
    total += copy_tree(HERE, stage / "TeachingFlow")

    # 2) 模板库（排除 eval_runs / experiments / _archive / __pycache__）
    pt = stage / "PromptTemplate"
    total += copy_tree(ROOT / "PromptTemplate", pt,
                       skip_names={"eval_runs", "experiments", "_archive"})

    # 3) 产片引擎（排除 runs / deliver / assets 里的示例；assets/local 是本地图库要带）
    pa = stage / "PPT-Agent"
    total += copy_tree(ROOT / "PPT-Agent", pa, skip_names={"deliver"})

    # 4) 集成路由：只要 export_notes.py（导讲稿/页面说明）
    router = stage / "集成路由V1.0"
    router.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "集成路由V1.0" / "export_notes.py", router / "export_notes.py")
    total += 1

    # 5) ppt-master（按档位）
    if profile in ("standard", "full"):
        skill_src = ROOT / "ppt-master" / "skills" / "ppt-master"
        skill_dst = stage / "ppt-master" / "skills" / "ppt-master"
        skill_dst.mkdir(parents=True, exist_ok=True)
        for f in ("SKILL.md", "requirements.txt", "LICENSE"):
            s = skill_src / f
            if s.is_file():
                shutil.copy2(s, skill_dst / f)
                total += 1
        for d in ("scripts", "workflows", "references", "templates"):
            if (skill_src / d).is_dir():
                total += copy_tree(skill_src / d, skill_dst / d,
                                   skip_names=_skill_skip(profile, d))

    # 6) 说明与依赖
    (stage / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (stage / ".env.example").write_text(
        "DEEPSEEK_API_KEY=\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        "DEEPSEEK_MODEL=deepseek-chat\nPIXABAY_API_KEY=\nPEXELS_API_KEY=\n",
        encoding="utf-8")
    (stage / "docs").mkdir(parents=True, exist_ok=True)
    (stage / "docs" / "打包说明.md").write_text(
        README.replace("{ts}", datetime.now().strftime("%Y-%m-%d %H:%M"))
              .replace("{profile}", profile), encoding="utf-8")
    (stage / "README.md").write_text(
        chr(10).join([
            "# TeachingFlow 教学流水线",
            "",
            f"- 打包/安装/运行说明：**[docs/打包说明.md](docs/打包说明.md)**（档位：{profile}）",
            "- 完整使用文档：**[TeachingFlow/docs/README.md](TeachingFlow/docs/README.md)**",
            "- 设计契约与取舍：**[TeachingFlow/CHAIN.md](TeachingFlow/CHAIN.md)**",
            "",
        ]),
        encoding="utf-8")
    total += 4

    zip_path = out_dir / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                # 带一层顶层目录，解压后就是 TeachingFlow-<profile>-<date>/
                z.write(p, (name + "/" + p.relative_to(stage).as_posix()))

    size = zip_path.stat().st_size / 1048576
    shutil.rmtree(stage, ignore_errors=True)     # 只留 zip，不留中间目录
    print(f"✓ {zip_path}  （{size:.1f} MB，{total} 个文件）")
    return zip_path


def _skill_skip(profile: str, subdir: str) -> set[str]:
    """skill 里的大素材按档位裁剪。"""
    skip: set[str] = set()
    if subdir == "references":
        skip.add("ai-image-comparison")            # 44M 的对比图，只有生图对比要用
    if subdir == "templates":
        if profile != "full":
            skip |= {"icons", "sounds"}            # 34M + 13M，standard 档不带
    return skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 TeachingFlow")
    ap.add_argument("--profile", choices=["lite", "standard", "full"], default="standard")
    ap.add_argument("--out", default=str(HERE / "dist"), help="输出目录（默认 TeachingFlow/dist）")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    build(a.profile, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
