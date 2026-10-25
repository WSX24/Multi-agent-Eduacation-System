# -*- coding: utf-8 -*-
"""路径常量与环境变量加载。

只做一件事：把「仓库里那几个目录」的相对位置固定下来，
其余模块一律从这里 import，避免到处写 ``../../``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent      # TeachingFlow/
ROOT = HERE.parent                                 # 自建Agent/
PROMPT_LIB_DIR = ROOT / "PromptTemplate"           # 模板源（不改动）
PPT_AGENT_DIR = ROOT / "PPT-Agent"                 # 产片引擎（不改动）
PACK_DIR = HERE / "promptpack"                     # 本链路自有模板（course_map）
FIXTURES_DIR = HERE / "fixtures"                   # --mock 用的假 LLM 回复
RUNS_DIR = HERE / "runs"                           # 每次运行的产物
STATE_DIR = HERE / "state"                         # 跨运行状态（progress.json）

for _p in (str(PROMPT_LIB_DIR), str(PPT_AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def load_env_file(path: Path | None = None) -> None:
    """读 .env 到环境变量（幂等，已有的不覆盖）。

    默认读 ``PPT-Agent/.env``（与 集成路由V1.0 同源），再看 ``TeachingFlow/.env``
    以便本链路单独覆盖模型/温度。
    """
    for p in (path or (PPT_AGENT_DIR / ".env"), HERE / ".env"):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def api_config() -> tuple[str, str, str]:
    """返回 (api_key, base_url, model)。"""
    load_env_file()
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
    model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    return key, base_url, model
