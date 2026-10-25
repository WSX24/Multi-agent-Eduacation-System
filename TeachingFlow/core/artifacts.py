# -*- coding: utf-8 -*-
"""每次运行的产物目录 + manifest。

落盘规则（与 CHAIN.md 一致）：每一步都有 prompt / result / parsed 三件，
外加一份全局 manifest.json。这样「上一步 LLM 返回了什么、怎么变成下一步输入」
永远可追溯，不依赖控制台滚动历史。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import RUNS_DIR


def safe_name(s: str, limit: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", s or "").strip("_")
    return s[:limit] or "未命名"


class Run:
    def __init__(self, slug: str, root: Path | None = None, resume: Path | None = None):
        if resume:
            self.dir = Path(resume).resolve()
            if not self.dir.is_dir():
                raise FileNotFoundError(f"续跑目录不存在：{self.dir}")
        else:
            now = datetime.now()
            base = (root or RUNS_DIR) / f"{now:%Y%m%d}"
            stem = f"{safe_name(slug)}_{now:%H%M%S}"
            self.dir = base / stem
            # 同一秒内跑两次会撞名：补 -2/-3…，绝不追加到别人已完成的 run 里
            n = 2
            while self.dir.exists():
                self.dir = base / f"{stem}-{n}"
                n += 1
            self.dir.mkdir(parents=True, exist_ok=True)

        self.manifest_path = self.dir / "manifest.json"
        self.manifest: dict = {"dir": str(self.dir), "steps": {}, "notes": []}
        if self.manifest_path.is_file():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- 基础
    def path(self, name: str) -> Path:
        return self.dir / name

    def write(self, name: str, text: str) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def write_json(self, name: str, data) -> Path:
        return self.write(name, json.dumps(data, ensure_ascii=False, indent=2))

    # ---------------------------------------------------------------- 步骤产物
    def step_done(self, step: str, *, ok: bool, artifacts: list[Path],
                  notes: list[str] | None = None, extra: dict | None = None) -> None:
        self.manifest["steps"][step] = {
            "ok": ok,
            "at": datetime.now().isoformat(timespec="seconds"),
            "artifacts": [str(p.relative_to(self.dir)) for p in artifacts],
            "notes": notes or [],
            **(extra or {}),
        }
        self.save()

    def note(self, msg: str) -> None:
        self.manifest["notes"].append(msg)

    def save(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- LLM 返回集
    LLM_DIR = "LLM返回"

    def record_llm(self, number: int, label: str, step: str, prompt: str,
                   raw: str, meta: dict | None = None) -> Path:
        """把一次真实调用（提示词 + 原始返回）收进 ``<run>/LLM返回/``。

        为什么单独建一个文件夹：排查时最先要看的是「模型到底吐了什么」，
        而它原先散在 ``01_plan.result.md`` / ``03_unit_<uid>.result.md`` /
        ``04a_pages_<uid>.result.md`` 各处，还得自己对上提示词。
        这里按调用顺序编号，返回在前、提示词在后，一份文件看完全貌。
        """
        meta = meta or {}
        d = self.dir / self.LLM_DIR
        d.mkdir(parents=True, exist_ok=True)
        name = f"{number:03d}_{safe_name(label, 28)}.md"
        path = d / name
        head = [
            f"# {number:03d} · {label}",
            "",
            f"> 步骤：`{step}`　|　模型：{meta.get('model', '—')}"
            f"　|　温度：{meta.get('temperature', '—')}"
            f"　|　返回字数：{len(raw)}"
            f"　|　mode：{meta.get('mode') or ('mock' if meta.get('mock') else '真实调用')}",
            f"> 时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            "",
            "## 返回（LLM 原文）",
            "",
            raw.rstrip(),
            "",
            "---",
            "",
            "## 提示词（模板加工后实际发出）",
            "",
            "```text",
            prompt.rstrip(),
            "```",
            "",
        ]
        path.write_text(chr(10).join(head), encoding="utf-8")

        idx_path = d / "index.json"
        try:
            items = json.loads(idx_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            items = []
        items.append({"n": number, "label": label, "step": step,
                      "file": name, "chars": len(raw),
                      "model": meta.get("model"), "mock": bool(meta.get("mock")),
                      "at": datetime.now().isoformat(timespec="seconds")})
        idx_path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return path

    # ---------------------------------------------------------------- 展示
    def rel(self, p: Path) -> str:
        try:
            return str(Path(p).relative_to(self.dir))
        except ValueError:
            return str(p)
