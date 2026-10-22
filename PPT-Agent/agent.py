"""PPT-Agent —— 把通用 ReAct Agent 特化为 ppt-master skill 的专用执行体。

改造要点（相对原始 ReActAgent）
-------------------------------------------------
1. 提示词从"通用工具助手"换成"skill 执行体"：预载 SKILL.md / routing.md /
   当前 profile 权威文档，并把 skill 的 Global Execution Discipline 写进纪律。
2. 工具从 3 个通用文件操作，扩展为"skill 感知"工具集：
   多了 read_skill_doc / grep_skill_doc / list_directory / append_file / ask_user，
   并统一做 ${SKILL_DIR} 展开与输出截断。
3. 第一个动作强制跑 attribution_guard.py 完整性闸门（skill 硬规则）。
4. 命令执行改为"白名单自动批准 + 其余人工确认"，支持 --yes 全自动。
5. 加了循环上限、观察截断、上下文压缩——skill 流程动辄几十步，裸循环必爆上下文。
6. 去掉 string.Template，改 {{}} 占位（原因见 prompt_template.py 顶部说明）。

用法：
    python agent.py --profile quick "做一份 8 页的《向量数据库入门》PPT"
    python agent.py --profile quick --task-file brief.md --yes
    python agent.py --profile default "把 report.pptx 美化一下"
"""

from __future__ import annotations

import ast
import inspect
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import click
from openai import (APIConnectionError, APITimeoutError, InternalServerError,
                    OpenAI, RateLimitError)

try:  # python-dotenv 是可选依赖，缺失时用内置解析器兜底
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except ModuleNotFoundError:  # pragma: no cover
    _HAS_DOTENV = False

from prompt_template import PROFILE_DISCIPLINE, system_prompt_template

# Windows 控制台默认 GBK，遇到 emoji / 中文会抛 UnicodeEncodeError。
# 必须在任何输出之前改好编码，包括 click 渲染 --help 的时候。
#
# line_buffering=True 同样关键：当 stdout 被管道接走（`| tee`、IDE 终端、重定向）时，
# Python 默认改用**块缓冲**，输出会攒到 8KB 才一次性吐出——于是长时间“一点过程都没有”，
# 然后突然涌出一大段。行缓冲让每条换行立即刷出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

# ===========================================================================
# 常量
# ===========================================================================

#: profile -> 该 profile 的运行时权威文档（来自 SKILL.md 的路由表）
PROFILE_AUTHORITY: Dict[str, str] = {
    "quick": "workflows/profiles/quick-generate.md",
    "default": "workflows/generate-pptx.md",
}

#: 每次运行都预载的文档
ALWAYS_PRELOAD: Tuple[str, ...] = ("SKILL.md", "workflows/routing.md")

#: skill 的完整性闸门
GUARD_SCRIPT = "scripts/attribution_guard.py"

#: 只读命令前缀 —— 命中即无需人工确认
READ_ONLY_PREFIXES: Tuple[str, ...] = (
    "dir ", "ls ", "type ", "cat ", "findstr ", "where ", "echo ", "pwd", "git status",
)

#: 批量工具的观察上限。批量读取不该被单文档上限截断，
#: 否则“一次读 8 份文档”会把大部分内容丢掉，反而更慢。
MAX_BATCH_OBSERVATION_CHARS = 60_000

#: **文档类**读取的上限，与命令输出分开。
#: skill 的参考文档是 5~53 KB，复用命令输出的 6,000 上限会丢掉 90%
#: ——实测 `executor-base.md`（52,977 B，skill 要求“完整读完”的执行权威）
#: 只读到 5,447 字符，模型拿着残缺规则写 SVG，随后被校验门报出 6 个错。
#: 读取份数由调用方的 max_lines 控制，这里只做防爆保护。
MAX_DOC_OBSERVATION_CHARS = 80_000

#: 单条工具结果的默认上限。
#: 早期用 6000，实测会把文档/报告的主体（位于中间）整段挖空，
#: 模型只能拿着半截信息创作。现在放宽到 30,000，并由 clip() 保留更多头部。
#: 注册画布的 viewBox（来自 references/canvas-formats.md）
CANVAS_VIEWBOX: Dict[str, str] = {
    "ppt169": "0 0 1280 720",
    "ppt43": "0 0 1024 768",
    "xiaohongshu": "0 0 1242 1660",
    "moments": "0 0 1080 1080",
    "story": "0 0 1080 1920",
    "wechat": "0 0 900 383",
    "banner": "0 0 1920 1080",
    "a4": "0 0 1240 1754",
}

DEFAULT_MAX_OBSERVATION_CHARS = 30_000

#: 步数上限。**0 = 不限**：步数与实际工作量不成比例，拿它截断正常任务会砍断
#: 未完成的 deck。真正的保护交给下面的循环检测。
DEFAULT_MAX_STEPS = 0

#: 停滞检测：同一工具 + 同一参数重复到该次数时在观察里植入提醒
STALL_WARN_REPEATS = 4
#: 重复到该次数时判定为原地打转，终止运行
STALL_ABORT_REPEATS = 7
#: 连续输出格式不合规（既无 action 也无 final_answer）到该次数时终止
FORMAT_FAILURE_ABORT = 5

#: —— 循环检测（滑动窗口）——
#: 只看"同一动作重复多少次"抓不住 A,B,A,B 这种双动作打转，所以改看窗口内的动作多样性。
#: 最近 LOOP_WINDOW 个动作里不同动作种类 ≤ LOOP_DISTINCT_ABORT 即判定为死循环。
LOOP_WINDOW = 10
LOOP_DISTINCT_WARN = 3
LOOP_DISTINCT_ABORT = 2
#: 每多少步打一条"仍在推进"的心跳，让无人值守运行可观测
PROGRESS_HEARTBEAT_STEPS = 50

#: —— 模型调用瞬时错误重试（与 pipeline 规划阶段的 BACKOFF_SECONDS 对齐）——
#: 规划阶段对网络异常有 backoff 重试，执行阶段（staged P3/P4 逐页生成）此前没有：
#: 一次 APIConnectionError 直接冒泡到 agent.py 退出码 1，整条无人值守链路被打断。
#: 这里补上：只对瞬时错误（网络中断/超时/限流/5xx）重试，认证(401)/参数(400)不重试。
MODEL_RETRIES = 3                        # 额外重试次数（含首次共 4 次）
MODEL_BACKOFF_SECONDS = (0.0, 3.0, 8.0, 15.0)
#: 值得重试的瞬时异常类型（openai SDK：APIConnectionError 含 APITimeoutError 子类 /
#: RateLimitError(429) / InternalServerError(5xx)。认证、权限、参数错误不在其内。）
MODEL_TRANSIENT_EXC = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)


# ===========================================================================
# 提供商自适应
# ===========================================================================
# 原实现把 base_url 硬编码成 OpenRouter。实际使用中换任何别家（DeepSeek /
# Moonshot / 通义 / 智谱 …）都会 401，所以这里做成注册表 + 自动探测。

@dataclass(frozen=True)
class ProviderConfig:
    """一次运行实际生效的提供商配置。"""

    name: str
    base_url: str
    api_key: str
    key_env: str          # 实际取到 key 的来源（环境变量名或 --api-key），用于故障诊断
    default_model: str
    context_tokens: int   # 上下文窗口（token），用于推导预载策略与字符预算
    # 单次回复的输出上限。不显式指定就受制于提供商默认值（DeepSeek 默认较小），
    # 模型想写长内容时会被自己截断，表现就是“内容不够丰富”。
    max_output_tokens: Optional[int] = None


#: 内置提供商。key_env 按顺序探测；context_tokens 决定预载模式与上下文预算。
PROVIDERS: Dict[str, Dict[str, object]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": ("OPENROUTER_API_KEY",),
        "default_model": "openai/gpt-4o",
        "context_tokens": 128_000,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "key_env": ("DEEPSEEK_API_KEY",),
        "default_model": "deepseek-v4-flash",
        # 实测：1,000,087 prompt tokens 被接受 → V4 系列为 1M 窗口；
        # 官方 /models 只列 deepseek-flash / deepseek-v4-pro，但 v4-flash 别名同样可用。
        "context_tokens": 1_024_000,
        # 工作默认值，不是模型上限（模型上限 384K）。
        # 实测：开启思考模式时，单次输出里 ~85% 是隐藏推理（一次真实调用：
        # 20,093 输出 tokens 中约 17,105 是推理，可见 SVG 只占 ~3,000）。
        # 所以 16384/32768 都会被截断而直接产生半截页面。65,536 留足余量。
        "max_output_tokens": 65_536,
    },
     "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": ("OPENAI_API_KEY",),
        "default_model": "gpt-4o",
        "context_tokens": 128_000,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": ("MOONSHOT_API_KEY",),
        "default_model": "kimi-k2-0905-preview",
        "context_tokens": 128_000,
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": ("DASHSCOPE_API_KEY",),
        "default_model": "qwen-max",
        "context_tokens": 128_000,
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY"),
        "default_model": "glm-4.6",
        "context_tokens": 128_000,
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "key_env": ("SILICONFLOW_API_KEY",),
        "default_model": "deepseek-ai/DeepSeek-V3",
        "context_tokens": 64_000,
    },
}

#: OpenRouter key 的特征前缀，用于识别「把别家 key 填到 OPENROUTER_API_KEY」这类错误
OPENROUTER_KEY_PREFIX = "sk-or-"

#: 超过此窗口的模型才默认全量预载（小窗口必须精简，否则提示词一填就满）
LARGE_CONTEXT_TOKENS = 128_000

#: 预载策略
PRELOAD_MODES = ("auto", "full", "lean", "minimal")


def mask_secret(value: str) -> str:
    """脱敏展示 key，便于用户自己核对是不是填了错的那把。"""
    if not value:
        return "(空)"
    if len(value) <= 10:
        return value[:2] + "***"
    return f"{value[:6]}...{value[-4:]}（长度 {len(value)}）"


def resolve_provider(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> ProviderConfig:
    """解析并校验提供商配置。

    优先级：
      1. 显式 --api-key / --base-url
      2. 显式 --provider 或环境变量 PPT_AGENT_PROVIDER
      3. 按 PROVIDERS 声明顺序自动探测哪个 key 环境变量存在
    """
    load_env_files()
    env_base = os.getenv("PPT_AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    custom_base = base_url or env_base

    # 同时显式给出端点与密钥、且未指定 provider 名 —— 视为完全自定义网关。
    # 必须放在 env_provider 前面：命令行显式参数优先级高于 .env。
    if base_url and api_key and not provider:
        return ProviderConfig("custom", base_url, api_key, "--api-key", "gpt-4o", 128_000)

    name = (provider or os.getenv("PPT_AGENT_PROVIDER") or "").strip().lower()
    if name:
        if name not in PROVIDERS:
            raise ValueError(
                f"未知提供商「{name}」。可选：{', '.join(PROVIDERS)}；"
                "自定义端点请跳过 --provider，改用 --base-url + --api-key。"
            )
        spec = PROVIDERS[name]
        env_names = list(spec["key_env"])
        resolved_key, used_env = "", "--api-key"
        if api_key:
            resolved_key = api_key
        else:
            for env_name in env_names:
                if os.getenv(env_name):
                    resolved_key, used_env = os.getenv(env_name), env_name
                    break
        if not resolved_key:
            raise ValueError(
                f"提供商「{name}」缺少 API Key。请设置 {' 或 '.join(env_names)}，"
                "或用 --api-key 传入。"
            )
        return ProviderConfig(
            name=name,
            base_url=custom_base or str(spec["base_url"]),
            api_key=resolved_key,
            key_env=used_env,
            default_model=str(spec["default_model"]),
            context_tokens=int(spec["context_tokens"]),
            max_output_tokens=spec.get("max_output_tokens"),
        )

    # 完全自定义端点
    if api_key and custom_base:
        return ProviderConfig("custom", custom_base, api_key, "--api-key", "gpt-4o", 128_000)

    # 自动探测
    for cand_name, spec in PROVIDERS.items():
        for env_name in spec["key_env"]:
            value = os.getenv(env_name)
            if not value:
                continue
            if env_name == "OPENROUTER_API_KEY" and not value.startswith(OPENROUTER_KEY_PREFIX):
                print(
                    f"\n⚠️  警告：{env_name} 的值不像 OpenRouter key"
                    f"（应以 {OPENROUTER_KEY_PREFIX} 开头）。\n"
                    "    如果你用的是别家（如 DeepSeek）的 key，很可能就是 401 的原因。\n"
                    "    改用 --provider deepseek，或把变量名改成 DEEPSEEK_API_KEY。"
                )
            print(f"ℹ️  自动探测到提供商：{cand_name}（来自 {env_name}）")
            return ProviderConfig(
                name=cand_name,
                base_url=custom_base or str(spec["base_url"]),
                api_key=value,
                key_env=env_name,
                default_model=str(spec["default_model"]),
                context_tokens=int(spec["context_tokens"]),
                max_output_tokens=spec.get("max_output_tokens"),
            )

    all_envs = [e for spec in PROVIDERS.values() for e in spec["key_env"]]
    raise ValueError(
        "未找到任何可用的 API Key。请设置以下任一环境变量：\n  "
        + "\n  ".join(all_envs)
        + "\n或显式传入 --api-key / --provider / --base-url。"
    )


#: 每 token 约多少字符。skill 文档以英文技术文为主，约 3.5；
#: 早期版本用 2.0（按 CJK 估算），导致预算被腰斩、过早触发折叠。
CHARS_PER_TOKEN = 3.5

#: 必须预留的输出 token（模型回复 + 余量）
RESERVED_OUTPUT_TOKENS = 12_000

#: skill 每轮都绕不开的阅读清单，用于开工前的容量预检。
#: 这些不是可以偷工减料的东西——skill 的硬性要求就是开工前读完。
CAPACITY_CHECKLIST: Tuple[str, ...] = (
    "SKILL.md",
    "workflows/routing.md",
    "workflows/profiles/quick-generate.md",
    "references/plan-core.md",
    "references/canvas-formats.md",
    "references/modes/_index.md",
    "references/visual-styles/_index.md",
    "references/image-renderings/_index.md",
    "templates/icons/README.md",
    "templates/charts/chart-vocabulary.md",
    "templates/tables/table-vocabulary.md",
    "references/shared-standards-core.md",
    "references/executor-base.md",
    "references/semantic-svg.md",
    "references/preset-shape-vocabulary.md",
)


def resolve_preload_mode(mode: str, provider: ProviderConfig) -> str:
    """auto 模式下按上下文窗口决定预载深度。

    quick profile 的权威文档就占 62,526 字符；64K 窗口的模型若全量预载，
    提示词一填就满，跑不了几页就溢出。所以小窗口自动降为 lean。
    """
    if mode == "auto":
        return "full" if provider.context_tokens >= LARGE_CONTEXT_TOKENS else "lean"
    return mode


def estimate_tokens(chars: int) -> int:
    """粗略把字符数换成 token，**仅用于开工前的容量预警**。

    运行时计量一律用提供商上报的真实 usage，不用这个换算。
    """
    return int(chars / CHARS_PER_TOKEN)


# ===========================================================================
# 环境探测
# ===========================================================================

def get_python_command() -> str:
    """本机可用的 Python 命令。skill 文档写 python3，Windows 上必须改写。"""
    return "python" if platform.system() == "Windows" else "python3"


def get_operating_system_name() -> str:
    return {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(
        platform.system(), "Unknown"
    )


def resolve_skill_dir(explicit: Optional[str] = None) -> Path:
    """定位 ppt-master skill 根目录。

    优先顺序：显式参数 > 环境变量 PPT_MASTER_SKILL_DIR > 从本文件向上逐级探测
    `.agents/skills/ppt-master` 与 `ppt-master/skills/ppt-master`。

    校验标准：必须同时存在 SKILL.md 和 scripts/attribution_guard.py。
    """
    candidates: List[Path] = []

    if explicit:
        candidates.append(Path(explicit))
    env = os.getenv("PPT_MASTER_SKILL_DIR")
    if env:
        candidates.append(Path(env))

    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        candidates.append(base / ".agents" / "skills" / "ppt-master")
        candidates.append(base / "ppt-master" / "skills" / "ppt-master")

    for cand in candidates:
        try:
            resolved = cand.expanduser().resolve()
        except OSError:
            continue
        if (resolved / "SKILL.md").is_file() and (resolved / GUARD_SCRIPT).is_file():
            return resolved

    raise FileNotFoundError(
        "未找到 ppt-master skill 根目录。请用 --skill-dir 指定，"
        "或设置环境变量 PPT_MASTER_SKILL_DIR。\n"
        "校验条件：目录下必须同时存在 SKILL.md 与 scripts/attribution_guard.py。"
    )


def load_env_files() -> None:
    """加载 .env。优先用 python-dotenv；缺装时退回内置解析器。

    查找位置：当前工作目录，以及本文件所在目录及其各级父目录。
    已存在的环境变量不会被覆盖。
    """
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for base in (start, *start.parents):
            env_file = base / ".env"
            if not env_file.is_file():
                continue
            if _HAS_DOTENV:
                load_dotenv(env_file, override=False)
            else:
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            return


def clip(text: str, limit: int, label: str = "输出") -> str:
    """超长时保留头部 75% + 尾部 25%，中间省略。

    尾部保留是为了接住报错（错误信息总在最后）；头部保留是因为它通常是
    最相关的内容。但**省略中间是破坏性的**——所以默认上限设得很宽，
    并在提示里告知模型如何取回其余部分。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    head_len = int(limit * 0.75)
    tail_len = int(limit * 0.25)
    head, tail = text[:head_len], text[-tail_len:]
    omitted = len(text) - head_len - tail_len
    return (
        f"{head}\n\n"
        f"……【{label}过长，中间省略 {omitted:,} 字符（原文 {len(text):,}）】……\n"
        f"【如需被省略的部分，请用更小的 max_lines 或 start_line 分段重读，"
        f"不要凭推测填空】\n\n"
        f"{tail}"
    )


# ===========================================================================
# 工具集
# ===========================================================================

class SkillTools:
    """暴露给模型的工具。

    每个公开方法的「签名 + docstring」会被自动渲染进 system prompt，
    所以 **docstring 就是模型看到的工具说明书，必须写清楚**。
    """

    def __init__(
        self,
        skill_dir: Path,
        output_root: Path,
        auto_approve: bool = True,
        command_timeout: int = 900,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
    ) -> None:
        self.skill_dir = skill_dir
        self.output_root = output_root
        self.auto_approve = auto_approve
        self.command_timeout = command_timeout
        self.max_observation_chars = max_observation_chars
        #: 本次运行中用户批准过的命令前缀，避免同一类命令反复追问
        self._approved_prefixes: set[str] = set()

    # ---------------------------------------------------------------- 路径

    def _resolve(self, path: str) -> Path:
        """把模型给的路径规范化为绝对路径，并展开 ${SKILL_DIR} 占位符。

        相对路径按输出根目录解析——这样模型写 `demo/svg_output/01.svg`
        会落到 `<OUTPUT_ROOT>/demo/svg_output/01.svg`。
        """
        raw = (path or "").strip().strip('"').strip("'")
        for token in ("${SKILL_DIR}", "$SKILL_DIR", "{SKILL_DIR}"):
            raw = raw.replace(token, str(self.skill_dir))
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.output_root / p
        return Path(os.path.normpath(str(p)))

    def _guard_skill_path(self, relative_path: str) -> Path:
        """只允许访问 SKILL_DIR 内的文件。

        注：先对**原始路径**做 resolve 再判包含性，不要把 `..` 当成字符裁掉——
        否则越界会被误报成 "文件不存在"，掩盖真实原因。
        """
        raw = (relative_path or "").strip().strip('"').strip("'").replace("\\", "/")
        skill_root = self.skill_dir.resolve()
        target = (self.skill_dir / raw).resolve()
        if target != skill_root and skill_root not in target.parents:
            raise PermissionError(
                f"拒绝访问 skill 目录之外的路径：{relative_path}"
            )
        if not target.is_file():
            raise FileNotFoundError(f"skill 文档不存在：{relative_path}")
        return target

    def _resolve_readonly(self, path: str) -> Path:
        """只读类工具的路径解析：先按输出根目录解析，不存在则回退到 skill 目录。

        这样模型既可以写 `list_directory("demo/svg_output")`（产物），
        也可以写 `list_directory("workflows")`（skill 文档），不会互相干扰。
        """
        primary = self._resolve(path)
        if primary.exists():
            return primary
        raw = (path or "").strip().strip('"').strip("'")
        if raw and not Path(raw).is_absolute() and "${SKILL_DIR}" not in raw:
            fallback = Path(os.path.normpath(str(self.skill_dir / raw)))
            if fallback.exists():
                return fallback
        return primary

    # ---------------------------------------------------------------- 工具

    def list_directory(self, path: str, max_entries: int = 300) -> str:
        """列出目录内容（名称、类型、大小）。用于确认工程结构、检查产物是否生成。

        path 可写输出根目录下的相对路径，也可写 skill 内的相对路径。
        只给相对路径时会先找输出根目录，找不到再回退到 skill 目录。
        例：list_directory("demo_20261003/svg_output")   list_directory("workflows")
        """
        target = self._resolve_readonly(path)
        if not target.exists():
            return f"目录不存在：{target}"
        if not target.is_dir():
            return f"不是目录：{target}"
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"{target} （共 {len(entries)} 项，显示前 {min(len(entries), max_entries)} 项）"]
        for item in entries[:max_entries]:
            try:
                if item.is_dir():
                    lines.append(f"  [DIR ] {item.name}/")
                else:
                    lines.append(f"  [FILE] {item.name}  ({item.stat().st_size:,} B)")
            except OSError as exc:
                lines.append(f"  [????] {item.name}  ({exc})")
        if len(entries) > max_entries:
            lines.append(f"  …… 其余 {len(entries) - max_entries} 项已省略")
        return clip("\n".join(lines), self.max_observation_chars, "目录列表")

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 500) -> str:
        """按行读取文件。适合读源码、JSON、已生成的 SVG、日志。

        大文件请用 start_line / max_lines 分段读，不要一次读几十万字符。
        只给相对路径时先找输出根目录，找不到再回退到 skill 目录。
        例：read_file("demo/svg_output/01_cover.svg")
            read_file("references/plan-core.md", 1, 200)
        """
        target = self._resolve_readonly(path)
        if not target.is_file():
            return f"文件不存在：{target}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"读取失败：{exc}"
        lines = text.splitlines()
        start = max(1, int(start_line))
        chunk = lines[start - 1: start - 1 + int(max_lines)]
        numbered = "\n".join(f"{start + i:>5}| {ln}" for i, ln in enumerate(chunk))
        header = f"{target}\n共 {len(lines)} 行，显示 {start}-{start + len(chunk) - 1} 行\n"
        return clip(header + numbered, MAX_DOC_OBSERVATION_CHARS, "文件内容")

    def write_file(self, path: str, content: str) -> str:
        """写入文件（覆盖）。父目录会自动创建。

        content 中的 \\n 会被还原为真实换行。写 SVG、Markdown、JSON 都用它。
        例：write_file("demo/svg_output/01_cover.svg", "<svg ...>\\n</svg>")
        """
        target = self._resolve(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.replace("\\n", "\n")
            target.write_text(data, encoding="utf-8")
        except OSError as exc:
            return f"写入失败：{exc}"
        return f"写入成功：{target}（{len(data):,} 字符）"

    def append_file(self, path: str, content: str) -> str:
        """在文件末尾追加内容。适合分块写长文件，避免单次生成过长而中断。

        例：append_file("demo/svg_output/01_cover.svg", "  <rect .../>\\n")
        """
        target = self._resolve(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.replace("\\n", "\n")
            with target.open("a", encoding="utf-8") as handle:
                handle.write(data)
        except OSError as exc:
            return f"追加失败：{exc}"
        return f"追加成功：{target}（+{len(data):,} 字符）"

    def read_skill_doc(
        self, relative_path: str, start_line: int = 1, max_lines: int = 1500
    ) -> str:
        """读取 ppt-master skill 内部的文档（**只能读 skill 内文件**）。

        relative_path 是相对 SKILL_DIR 的路径，例如：
          workflows/stages/topic-research.md
          references/plan-core.md
          references/executor-base.md
          scripts/docs/svg-contract.md
        每次读的文档必须真实存在；不存在会报错，请先用 grep_skill_doc 定位。
        """
        target = self._guard_skill_path(relative_path)
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start_line))
        chunk = lines[start - 1: start - 1 + int(max_lines)]
        numbered = "\n".join(f"{start + i:>5}| {ln}" for i, ln in enumerate(chunk))
        header = (
            f"【skill 文档】{relative_path}\n共 {len(lines)} 行，"
            f"显示 {start}-{start + len(chunk) - 1} 行\n"
        )
        return clip(header + numbered, MAX_DOC_OBSERVATION_CHARS, "skill 文档")

    def read_skill_docs(self, relative_paths: str, max_lines: int = 1500) -> str:
        """**【优先用它】**一次读取多份 skill 文档，减少往返次数。

        相对路径用逗号分隔。模型每轮只能发有限次调用，把互不依赖的阅读
        合并成一次，能省下大量网络往返（实测每轮往返约 1.5~4 秒）。

        例：read_skill_docs("references/plan-core.md, references/canvas-formats.md")
            read_skill_docs("references/executor-base.md", max_lines=1200)
        """
        parts = [p.strip() for p in str(relative_paths).split(",") if p.strip()]
        if not parts:
            return "未提供任何路径。"
        chunks: List[str] = []
        for rel in parts:
            try:
                chunks.append(self.read_skill_doc(rel, 1, max_lines))
            except Exception as exc:  # noqa: BLE001 —— 单份失败不影响其余
                chunks.append(f"【{rel}】读取失败：{exc}")
        return clip(
            f"批量读取 {len(parts)} 份文档：\n\n" + "\n\n".join(chunks),
            MAX_BATCH_OBSERVATION_CHARS,
            "批量文档",
        )

    def grep_skill_doc(self, keyword: str, path_glob: str = "**/*.md", max_hits: int = 40) -> str:
        """在 skill 文档中检索关键词，返回「文件:行号: 内容」。

        用途：当你不确定该读哪份文档、或要找某个脚本/参数/规则的权威定义时，
        先用它定位，再用 read_skill_doc 精读。
        例：grep_skill_doc("verify-charts")
            grep_skill_doc("--quick-generate", path_glob="scripts/docs/*.md")
        """
        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        hits: List[str] = []
        searched = 0
        for file in sorted(self.skill_dir.glob(path_glob)):
            if not file.is_file() or file.suffix.lower() not in {".md", ".py", ".json"}:
                continue
            searched += 1
            try:
                for lineno, line in enumerate(
                    file.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if pattern.search(line):
                        rel = file.relative_to(self.skill_dir).as_posix()
                        hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                        if len(hits) >= max_hits:
                            break
            except OSError:
                continue
            if len(hits) >= max_hits:
                break
        if not hits:
            return f"未找到关键词「{keyword}」（已扫描 {searched} 个文件，glob={path_glob}）"
        return clip(
            f"关键词「{keyword}」命中 {len(hits)} 处（已扫描 {searched} 个文件）：\n"
            + "\n".join(hits),
            self.max_observation_chars,
            "检索结果",
        )

    def run_command(self, command: str, cwd: str = "", timeout_seconds: int = 0) -> str:
        """执行 shell 命令，返回合并后的 stdout/stderr 与退出码。

        - skill 自带脚本必须用**展开后的绝对路径**调用，例如：
          run_command('python "<skill_dir>/scripts/project_manager.py" init demo --quick-generate --dir "<output_root>"')
          把上面两处占位换成你实际环境里 system prompt 给出的真实路径。
        - 用 cwd 指定工作目录，**不要用 cd**。
        - 参数含空格时必须加引号，例如字体名：`--role "body:Microsoft YaHei:24"`。
          不加引号会被 shell 拆成两段而报 usage 错。
        - 本机已授予全局执行权限：命令直接运行，不会向你或用户索要确认。
          因此不要写“需要用户批准”之类的表述，也不要为规避确认而绕路。
        """
        code, body = self.run_command_checked(command, cwd, timeout_seconds)
        workdir = self._resolve(cwd) if cwd else self.output_root
        return clip(
            f"cwd={workdir}\nexit_code={code}\n{body}",
            self.max_observation_chars,
            "命令输出",
        )

    def run_command_checked(
        self, command: str, cwd: str = "", timeout_seconds: int = 0
    ) -> Tuple[int, str]:
        """执行 shell 命令，返回 (退出码, 合并后的 stdout/stderr 文本)。

        供内部驱动器（如 staged 引擎）在需要**结构化退出码**时调用，避免用
        `"exit_code=0" in output` 这类脆弱的字符串匹配。返回的 body **不 clip、
        不包装**，退出码永远是精确值；观察长度截断由调用方负责。此方法
        **不进模型工具列表**（public_tools 未收录）。
        """
        cmd = (command or "").strip()
        if not cmd:
            return 0, "（空命令，已忽略）"

        if not self._is_auto_approved(cmd):
            print(f"\n⚠️  待执行命令（非白名单，需确认）：\n    {cmd}")
            answer = input("    执行？(Y/N) ").strip().lower()
            if answer != "y":
                return 1, "用户拒绝执行该命令。请改用其他方式，或向用户说明原因。"

        workdir = self._resolve(cwd) if cwd else self.output_root
        workdir.mkdir(parents=True, exist_ok=True)
        effective_timeout = int(timeout_seconds) or self.command_timeout

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"命令超时（>{effective_timeout}s）：{cmd}"
        except OSError as exc:
            return 127, f"命令启动失败：{exc}"

        body = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        body = body.strip() or "(无输出)"
        return proc.returncode, body

    def ask_user(self, question: str) -> str:
        """向用户提问并等待回答。**仅在 Default 路由的 ⛔ BLOCKING 确认门使用。**

        必须一次只问一个门，把 skill 文档要求的选项完整列出，不得替用户预设答案。
        """
        print(f"\n⛔ [BLOCKING 确认门]\n{question}\n")
        return input("用户回答：").strip() or "(用户未作答)"

    # ---------------------------------------------------------------- 内部

    def _is_auto_approved(self, command: str) -> bool:
        """默认全局放行：只要 auto_approve 为真就直接执行。

        只有显式开启 --confirm-commands 时，才退回“只读命令 + skill 自带脚本”白名单，
        其余命令交给用户确认。
        """
        if self.auto_approve:
            return True
        lowered = command.strip().lower()
        for prefix in READ_ONLY_PREFIXES:
            if lowered.startswith(prefix):
                return True
        first = lowered.split()[0] if lowered.split() else ""
        if first in {"python", "python3", "py", "python.exe"}:
            normalized = command.replace("\\", "/")
            # 只自动放行"跑 skill 自带脚本"这一类；其他 python 调用仍要确认
            if "scripts/" in normalized and str(self.skill_dir).replace("\\", "/") in normalized:
                return True
        for prefix in self._approved_prefixes:
            if lowered.startswith(prefix):
                return True
        return False

    def public_tools(self, include_ask_user: bool) -> List[Callable]:
        names = [
            "list_directory",
            "read_file",
            "write_file",
            "append_file",
            "read_skill_doc",
            "read_skill_docs",
            "grep_skill_doc",
            "run_command",
        ]
        if include_ask_user:
            names.append("ask_user")
        return [getattr(self, name) for name in names]


# ===========================================================================
# Agent
# ===========================================================================

class PPTAgent:
    """ReAct 循环 + ppt-master skill 上下文。"""

    def __init__(
        self,
        skill_dir: Path,
        output_root: Path,
        model: Optional[str] = None,
        profile: str = "quick",
        auto_approve: bool = True,
        max_steps: int = DEFAULT_MAX_STEPS,
        command_timeout: int = 900,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
        context_budget: int = 0,
        preload: str = "auto",
        stall_abort: int = STALL_ABORT_REPEATS,
        stall_detect: bool = True,
        capacity_check: bool = True,
        compact_threshold: float = 0.85,
        context_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking: str = "default",
        reasoning_effort: Optional[str] = "low",
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        verbose: bool = True,
    ) -> None:
        self.skill_dir = skill_dir
        self.output_root = output_root
        self.profile = profile
        self.max_steps = max_steps
        self.stall_abort = stall_abort
        self.stall_detect = stall_detect
        self.capacity_check = capacity_check
        self.compact_threshold = compact_threshold
        # —— 上下文计量：全部来自提供商上报的真实 usage，不做字符估算 ——
        self.last_prompt_tokens = 0
        self.peak_prompt_tokens = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.truncations = 0
        # —— 耗时埋点：把“慢”变成可测量的数字 ——
        self.last_model_seconds = 0.0
        self.time_model = 0.0
        self.time_tools = 0.0
        self.calls = 0
        self.verbose = verbose
        self.python_cmd = get_python_command()

        # —— 提供商：解析 base_url / key / 模型 / 上下文窗口 ——
        self.provider: ProviderConfig = resolve_provider(provider, base_url, api_key)
        if context_tokens:
            # 用户显式声明的窗口优先：同一模型名在不同渠道的窗口可能不同
            self.provider = replace(self.provider, context_tokens=int(context_tokens))
        self.model = model or self.provider.default_model
        # 显式设定输出上限与采样温度：不显式给就会走提供商默认值，
        # 而默认值往往偏小，会让模型想写长内容时被自己截断。
        self.max_tokens = max_tokens if max_tokens is not None else self.provider.max_output_tokens
        self.temperature = temperature
        # DeepSeek V4 默认开启思考模式：实测单次输出 20,093 tokens 中有 ~17,105（8
        # 5%）是隐藏推理，可见内容只占 15%。这不是模型慢，是在生成 7 倍的量。
        # 官方开关：extra_body={"thinking": {"type": "disabled"}}
        self.thinking = (thinking or "default").lower()
        self.reasoning_effort = reasoning_effort
        #: 统计累加锁：并行生成多页时多个线程会同时调 call_model，
        #: calls / total_*_tokens 等累加不是原子的，必须串行化。
        self._stats_lock = threading.Lock()
        self.preload = resolve_preload_mode(preload, self.provider)
        self.context_budget = int(context_budget)  # 0 = 用提供商上报的真实 token 计量

        self.tools = SkillTools(
            skill_dir=skill_dir,
            output_root=output_root,
            auto_approve=auto_approve,
            command_timeout=command_timeout,
            max_observation_chars=max_observation_chars,
        )
        self.tool_map: Dict[str, Callable] = {
            func.__name__: func for func in self.tools.public_tools(include_ask_user=(profile == "default"))
        }
        self.client = OpenAI(
            base_url=self.provider.base_url,
            api_key=self.provider.api_key,
        )

    # ------------------------------------------------------------ 连通性

    def describe_connection(self) -> str:
        cfg = self.provider
        return (
            f"   provider   = {cfg.name}\n"
            f"   base_url   = {cfg.base_url}\n"
            f"   model      = {self.model}\n"
            f"   key 来源   = {cfg.key_env}\n"
            f"   key 值     = {mask_secret(cfg.api_key)}\n"
            f"   上下文窗口 = {cfg.context_tokens:,} tokens\n"
            f"   输出上限   = {self.max_tokens or '提供商默认'}\n"
            f"   思考模式   = {self.thinking}"
            + (f"（reasoning_effort={self.reasoning_effort}）" if self.reasoning_effort else "")
            + "\n"
            f"   温度       = {self.temperature if self.temperature is not None else '提供商默认'}\n"
            f"   预载策略   = {self.preload}\n"
            f"   计量方式   = "
            + (
                f"手动字符预算 {self.context_budget:,}"
                if self.context_budget > 0
                else f"提供商上报 token，{self.compact_threshold:.0%} 时折叠"
            )
        )

    def _diagnose_failure(self, exc: Exception) -> str:
        """把底层异常翻译成可操作的修复建议。"""
        cfg = self.provider
        message = str(exc)
        lines = ["❌ 模型连通性预检失败。", self.describe_connection(),
                 f"   异常       = {type(exc).__name__}: {message[:300]}", ""]
        lowered = message.lower()
        if "401" in message or "auth" in lowered or "AuthenticationError" in type(exc).__name__:
            lines.append("诊断：认证失败。最常见的原因是 **base_url 与 key 不是同一家**。")
            if "openrouter.ai" in cfg.base_url:
                lines.append("  → 请求正发往 OpenRouter，请确认 key 是 OpenRouter 的（sk-or- 开头）。")
            lines.append("  → 用 DeepSeek key 时应指定：--provider deepseek")
            lines.append("  → 或在 .env 中写入：PPT_AGENT_PROVIDER=deepseek")
        elif "404" in message or "model" in lowered and "not" in lowered:
            lines.append(f"诊断：模型名可能不被该提供商支持。{cfg.name} 的默认模型是 {cfg.default_model}。")
        elif "connect" in lowered or "timeout" in lowered or "ssl" in lowered:
            lines.append("诊断：网络不可达。检查代理 / 防火墙，或用 PPT_AGENT_BASE_URL 指向可用网关。")
        else:
            lines.append("诊断：未识别的错误。上面的 base_url / model / key 来源可用于人工核对。")
        return "\n".join(lines)

    def preflight(self) -> None:
        """发起一次最小请求，把认证/端点/模型名错误提前到运行开始前暴露。

        否则这些错误会在跑完几十分钟的准备阶段后才爆，白白浪费。
        """
        print("\n🔌 预检模型连通性...")
        try:
            self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 —— 预检就是要接住一切
            raise RuntimeError(self._diagnose_failure(exc)) from exc
        print("✅ 连通性正常")

    # ------------------------------------------------------------ 容量预检

    def capacity_report(self) -> Dict[str, int]:
        """统计 skill 强制阅读清单的体积，用于开工前的容量判断。"""
        mandatory = 0
        missing = 0
        for rel in CAPACITY_CHECKLIST:
            file = self.skill_dir / rel
            if file.is_file():
                mandatory += file.stat().st_size
            else:
                missing += 1
        return {
            "mandatory_chars": mandatory,
            "system_chars": len(self.build_system_prompt()),
            "window_tokens": self.provider.context_tokens,
            "missing": missing,
        }

    def check_capacity(self) -> str:
        """开工前预警：skill 的强制阅读量能否装进模型窗口。

        这是花 20 分钟跑空一个任务换来的教训：ppt-master 要求开工前读完约
        235 KB 文档；装不下时会发生「读完 → 折叠 → 忘掉 → 重读」的抖动，
        永远进不到创作阶段，而表面上看只是“很慢”。

        注：这里的 token 数是**估算**（字符 ÷ 3.5），只用于预警，
        余量放得比较宽；运行时计量一律用提供商上报的真实 usage。
        """
        info = self.capacity_report()
        need_chars = info["mandatory_chars"] + info["system_chars"]
        need_tokens = estimate_tokens(need_chars)
        usable = max(1, info["window_tokens"] - RESERVED_OUTPUT_TOKENS)
        ratio = need_tokens / usable

        lines = [
            f"   强制阅读清单 = {info['mandatory_chars']:,} 字符"
            f"（{len(CAPACITY_CHECKLIST)} 份文档）",
            f"   system prompt = {info['system_chars']:,} 字符",
            f"   开工前已占   ≈ {need_tokens:,} tokens（按 {CHARS_PER_TOKEN} 字符/token 估算）",
            f"   可用窗口     = {usable:,} tokens（{info['window_tokens']:,} 减去输出预留 {RESERVED_OUTPUT_TOKENS:,}）",
            f"   占用率       ≈ {ratio:.0%}",
        ]
        head = "\n".join(lines)

        if ratio >= 0.95:
            sys.stdout.flush()
            raise RuntimeError(
                "❌ 容量不足，已终止（避免白跑）：\n" + head + "\n\n"
                "skill 的强制阅读量已经占满模型窗口，无论怎么跑都会陷入\n"
                "“读完→折叠→忘掉→重读”的抖动，产出不了任何一页。\n\n"
                "可选修复：\n"
                "  1. 换更大窗口的模型，例如：--provider dashscope --model qwen-max\n"
                "  2. 如果这个模型实际窗口更大，显式告知：--context-tokens 131072\n"
                "  3. 用 --no-capacity-check 跳过本检查（不推荐，很可能白跑）"
            )
        if ratio >= 0.7:
            return (
                "⚠️  容量偏紧：重任务是可行的，但中途可能触发历史折叠。\n"
                + head
                + "\n   这是估算值，实际用量会打印在每一步的 📊 行；若实际远超预期，"
                "用 --context-tokens 修正窗口。"
            )
        return "✅ 容量充足：\n" + head

    # ------------------------------------------------------------ 提示词

    def get_tool_list(self) -> str:
        """由签名 + docstring 自动生成工具说明。

        同时把 docstring 里的 `${SKILL_DIR}` 展开成真实绝对路径，
        让模型在提示词里看到的全是可用路径，不需要自己做替换。
        """
        blocks: List[str] = []
        for func in self.tool_map.values():
            doc = (inspect.getdoc(func) or "").strip()
            param = str(inspect.signature(func))
            doc = doc.replace("${SKILL_DIR}", str(self.skill_dir))
            param = param.replace("{SKILL_DIR}", str(self.skill_dir))
            blocks.append(f"### {func.__name__}{param}\n{doc}")
        return "\n\n".join(blocks)

    def _preload_paths(self) -> List[str]:
        authority = PROFILE_AUTHORITY[self.profile]
        if self.preload == "full":
            return list(ALWAYS_PRELOAD) + [authority]
        if self.preload == "lean":
            return list(ALWAYS_PRELOAD)
        return [ALWAYS_PRELOAD[0]]

    def _preload_note(self) -> str:
        """告诉模型"哪些已预载、哪些必须自己去读"。

        这是 lean / minimal 模式能成立的前提：预载变少，但强制读取的要求不能变，
        否则模型会直接开始创作而跳过权威文档。
        """
        authority = PROFILE_AUTHORITY[self.profile]
        if self.preload == "full":
            return (
                f"✅ 已预载：SKILL.md、workflows/routing.md、{authority}\n"
                "   先通读，然后进入第 2 步。"
            )
        if self.preload == "lean":
            return (
                "✅ 已预载：SKILL.md、workflows/routing.md\n"
                f"⚠️ **未预载**当前 profile 的运行时权威文档（{authority}）。\n"
                f'   你必须先调用 read_skill_doc("{authority}") 完整读取它，'
                "然后才能开始任何创作。不要跳过这一步。"
            )
        return (
            "✅ 已预载：SKILL.md（仅此一份）\n"
            "⚠️ **未预载** workflows/routing.md 与 profile 权威文档。你必须依次读完：\n"
            '   read_skill_doc("workflows/routing.md")\n'
            f'   read_skill_doc("{authority}")\n'
            "   读完后才能开始任何创作。"
        )

    def _load_skill_preload(self) -> str:
        """预载核心权威文档，并把 ${SKILL_DIR} 展开成真实绝对路径。"""
        chunks: List[str] = []
        for rel in self._preload_paths():
            file = self.skill_dir / rel
            if not file.is_file():
                chunks.append(f"（警告：预载文档缺失 {rel}）")
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            text = text.replace("${SKILL_DIR}", str(self.skill_dir))
            text = text.replace("$SKILL_DIR", str(self.skill_dir))
            chunks.append(
                f"\n════════ 文档开始：{rel} ════════\n{text}\n════════ 文档结束：{rel} ════════\n"
            )
        return "\n".join(chunks)

    def _output_listing(self) -> str:
        if not self.output_root.exists():
            return f"  （目录尚不存在，将在首次写入时创建）"
        items = sorted(self.output_root.iterdir())[:30]
        if not items:
            return "  （空目录）"
        return "\n".join(f"  - {i.name}{'/' if i.is_dir() else ''}" for i in items)

    def build_system_prompt(self) -> str:
        return (
            system_prompt_template
            .replace("{{SKILL_DIR}}", str(self.skill_dir))
            .replace("{{OUTPUT_ROOT}}", str(self.output_root))
            .replace("{{OUTPUT_ROOT_LISTING}}", self._output_listing())
            .replace("{{OPERATING_SYSTEM}}", get_operating_system_name())
            .replace("{{PYTHON_CMD}}", self.python_cmd)
            .replace("{{PROFILE_NAME}}", self.profile)
            .replace("{{PROFILE_DISCIPLINE}}", PROFILE_DISCIPLINE[self.profile])
            .replace("{{PRELOAD_NOTE}}", self._preload_note())
            .replace("{{TOOL_LIST}}", self.get_tool_list())
            .replace("{{SKILL_PRELOAD}}", self._load_skill_preload())
        )

    # ------------------------------------------------------------ 模型

    def _is_transient_model_error(self, exc: Exception) -> bool:
        """瞬时错误（网络中断/超时/限流/5xx）才值得重试；其余确定性错误重试无意义。"""
        return isinstance(exc, MODEL_TRANSIENT_EXC)

    def _call_with_retry(
        self, request: Dict[str, object], started: float
    ) -> Tuple[str, Optional[str], Optional[object]]:
        """发起一次模型请求，瞬时错误自动 backoff 重试。

        与 pipeline 规划阶段对齐：只对瞬时异常重试，backoff 0/3/8/15s；
        认证(401)/权限(403)/参数(400) 等确定性错误直接抛，不空转。
        返回 (content, finish_reason, usage)，流式与非流式同形。
        """
        stream = bool(request.get("stream"))
        for attempt in range(MODEL_RETRIES + 1):
            try:
                if stream:
                    return self._stream_call(request, started)
                response = self.client.chat.completions.create(**request)  # type: ignore[arg-type]
                choice = response.choices[0]
                return (choice.message.content or "",
                        getattr(choice, "finish_reason", None),
                        getattr(response, "usage", None))
            except Exception as exc:  # noqa: BLE001 —— 瞬时错误重试，其余直接抛
                if not self._is_transient_model_error(exc) or attempt >= MODEL_RETRIES:
                    raise
                delay = MODEL_BACKOFF_SECONDS[min(attempt, len(MODEL_BACKOFF_SECONDS) - 1)]
                print(
                    f"\n   ⚠️ {'流式' if stream else '非流式'}调用瞬时错误"
                    f"（{type(exc).__name__}），{delay:g}s 后重试"
                    f"（{attempt + 1}/{MODEL_RETRIES}）",
                    flush=True,
                )
                if delay:
                    time.sleep(delay)
        # 理论不可达：循环内要么 return 要么 raise
        raise RuntimeError("_call_with_retry 不可达分支")

    def call_model(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        reasoning_effort: Optional[str] = None,
        thinking: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """请求模型。默认流式。

        为什么不非流式：思考模式下单次调用可达 60~160 秒（其中 ~85% 是不可见的
        推理 token），非流式会让终端像卡死几分钟。流式能实时显示推理进度，
        且总花费与结果与非流式完全一致。

        reasoning_effort / thinking 用于**单次覆盖**（None = 沿用 self 上的全局
        默认值），让 staged 引擎能按阶段切换推理强度而不改全局状态。
        """
        request: Dict[str, object] = {"model": model or self.model, "messages": messages}
        if self.max_tokens:
            request["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            request["temperature"] = self.temperature
        eff = reasoning_effort if reasoning_effort is not None else self.reasoning_effort
        think = thinking if thinking is not None else self.thinking
        extra: Dict[str, object] = {}
        if think in ("enabled", "disabled"):
            extra["thinking"] = {"type": think}
        if eff:
            extra["reasoning_effort"] = eff
        if extra:
            request["extra_body"] = extra

        started = time.time()
        usage = None
        finish_reason = None
        if stream:
            request["stream"] = True
            request["stream_options"] = {"include_usage": True}
            try:
                content, finish_reason, usage = self._call_with_retry(request, started)
            except Exception as exc:  # noqa: BLE001 —— 流式重试仍失败/不支持流式时退回
                print(f"\n   ⚠️ 流式失败（{type(exc).__name__}），退回非流式重试", flush=True)
                request.pop("stream", None)
                request.pop("stream_options", None)
                stream = False
        if not stream:
            content, finish_reason, usage = self._call_with_retry(request, started)

        # 统计累加加锁：并行生成多页时多个线程同时调 call_model，这些累加
        # 不是原子的，串行化后账本数字才不丢更新。
        with self._stats_lock:
            self.calls += 1
            self.last_model_seconds = time.time() - started
            self.time_model += self.last_model_seconds

            # —— 真实用量：唯一的 ground truth，不做字符估算 ——
            prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
            completion = getattr(usage, "completion_tokens", None) if usage else None
            if prompt_tokens:
                self.last_prompt_tokens = int(prompt_tokens)
                self.peak_prompt_tokens = max(self.peak_prompt_tokens, self.last_prompt_tokens)
                self.total_prompt_tokens += self.last_prompt_tokens
            if completion:
                self.total_completion_tokens += int(completion)

        # 截断必须被看见：静默截断会直接产生半截 SVG / 半截文档
        if finish_reason == "length":
            with self._stats_lock:
                self.truncations += 1
                trunc_count = self.truncations
            print(
                f"\n⚠️  输出达到 max_tokens={self.max_tokens} 被截断！"
                f"（第 {trunc_count} 次）内容可能不完整。"
            )
            if completion:
                print(
                    f"     本次输出 {completion:,} tokens；开启思考模式时隐藏推理也占该额度"
                    f"（实测可占 85%，可 --thinking disabled 关闭或调高 --max-tokens）"
                )
            content += (
                "\n\n【注意：你的上一条输出被 max_tokens 上限截断了，内容不完整。"
                "请拆成多次调用续写，或先写骨架再用 append_file 逐段补充。】"
            )

        messages.append({"role": "assistant", "content": content})
        return content

    def _stream_call(self, request: Dict[str, object], started: float):
        """流式收流：实时显示进度，返回 (content, finish_reason, usage)。"""
        parts: List[str] = []
        reasoning_chunks = 0
        visible_chars = 0
        finish_reason = None
        usage = None
        last_report = started
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        tick = 0

        stream = self.client.chat.completions.create(**request)  # type: ignore[arg-type]
        for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            # DeepSeek 思考模式把隐藏推理放在 reasoning_content
            if getattr(delta, "reasoning_content", None):
                reasoning_chunks += 1
            text = getattr(delta, "content", None)
            if text:
                parts.append(text)
                visible_chars += len(text)

            now = time.time()
            if self.verbose and now - last_report >= 3.0:
                last_report = now
                print(
                    f"\r   {spinner[tick % len(spinner)]} 生成中 {now - started:>5.1f}s"
                    f" | 推理 {reasoning_chunks:>7,} chunk | 正文 {visible_chars:>7,} 字符   ",
                    end="", flush=True,
                )
                tick += 1

        if self.verbose:
            print(
                f"\r   ✓ 用时 {time.time() - started:>5.1f}s"
                f" | 推理 {reasoning_chunks:>7,} chunk | 正文 {visible_chars:>7,} 字符"
                + " " * 20,
                flush=True,
            )
        return "".join(parts), finish_reason, usage

    # ------------------------------------------------------------ 解析

    @staticmethod
    def _parse_single_arg(arg_str: str):
        arg_str = arg_str.strip()
        if (
            (arg_str.startswith('"') and arg_str.endswith('"') and len(arg_str) >= 2)
            or (arg_str.startswith("'") and arg_str.endswith("'") and len(arg_str) >= 2)
        ):
            inner = arg_str[1:-1]
            inner = inner.replace('\\"', '"').replace("\\'", "'")
            inner = inner.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
            inner = inner.replace("\\\\", "\\")
            return inner
        try:
            return ast.literal_eval(arg_str)
        except (SyntaxError, ValueError):
            return arg_str

    @classmethod
    def parse_action(cls, code_str: str) -> Tuple[str, List, Dict[str, object]]:
        """解析 `tool_name(arg1, arg2, kw=value)` -> (函数名, 位置参, 关键字参)。

        **必须支持关键字参数**。模型很自然会写
        `run_command(cmd, timeout_seconds=180)`；早期版本用的是「手写逗号分割」，
        会把 `timeout_seconds=180` 当成第二个位置参数，于是 cwd 变成字面量
        字符串，凭空建出 `projects/timeout_seconds=180/` 这种垃圾目录。

        现在改用 ast 解析真实的调用表达式：嵌套括号、逗号、引号、转义、
        关键字参数全部由 Python 自己的语法保证正确。解析失败时退回旧实现。
        """
        source = (code_str or "").strip().rstrip("`").strip()
        try:
            node = ast.parse(source, mode="eval").body
            if not isinstance(node, ast.Call):
                raise SyntaxError("不是函数调用")
            func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if not func_name:
                raise SyntaxError("无法识别函数名")

            def literal(value_node: ast.AST):
                """字面量直接求值；裸标识符 / 未加引号的路径退回文本处理。"""
                try:
                    return ast.literal_eval(value_node)
                except (SyntaxError, ValueError):
                    segment = ast.get_source_segment(source, value_node) or ""
                    return cls._parse_single_arg(segment)

            args = [literal(a) for a in node.args]
            kwargs: Dict[str, object] = {}
            for kw in node.keywords:
                if kw.arg is None:
                    raise SyntaxError("不支持 ** 展开")
                kwargs[kw.arg] = literal(kw.value)
            return func_name, args, kwargs
        except SyntaxError:
            pass

        # —— 退回旧实现：兼容模型输出的非标准 Python 写法 ——
        func_name, args = cls._legacy_parse_action(source)
        return func_name, args, {}

    @classmethod
    def _legacy_parse_action(cls, code_str: str) -> Tuple[str, List]:
        """早期版本的解析器，仅在 ast 解析失败时兜底。"""
        match = re.match(r"\s*(\w+)\s*\((.*)\)\s*$", code_str, re.DOTALL)
        if not match:
            raise ValueError(f"无效的工具调用语法：{code_str[:200]}")

        func_name, args_str = match.group(1), match.group(2).strip()
        if not args_str:
            return func_name, []

        # 快路径：整串当元组解析
        try:
            parsed = ast.literal_eval(f"({args_str},)")
            if isinstance(parsed, tuple):
                return func_name, list(parsed)
        except (SyntaxError, ValueError):
            pass

        # 慢路径：手写状态机，处理含多行字符串 / 裸标识符的情况
        args: List = []
        current, in_string, quote, depth = "", False, None, 0
        for i, char in enumerate(args_str):
            if not in_string:
                if char in "\"'":
                    in_string, quote = True, char
                    current += char
                elif char == "(":
                    depth += 1
                    current += char
                elif char == ")":
                    depth -= 1
                    current += char
                elif char == "," and depth == 0:
                    args.append(cls._parse_single_arg(current))
                    current = ""
                else:
                    current += char
            else:
                current += char
                if char == quote and (i == 0 or args_str[i - 1] != "\\"):
                    in_string, quote = False, None
        if current.strip():
            args.append(cls._parse_single_arg(current))
        return func_name, args

    # ------------------------------------------------------------ 上下文

    @staticmethod
    def _total_chars(messages: Sequence[Dict[str, str]]) -> int:
        return sum(len(m.get("content") or "") for m in messages)

    def _compact_if_needed(self, messages: List[Dict[str, str]]) -> None:
        """上下文压缩。

        触发依据是**提供商返回的真实 prompt_tokens**，不是字符数估算。
        字符→token 的换算在不同语言与内容上能差 3~6 倍（实测同一批次内容，
        按 4 字符/token 估 144K，实际只有 112K），用它来触发折叠会要么过早
        丢失上下文，要么根本拦不住真溢出。

        只有在模型还没上报过 usage 时，才退回手动的 `--context-budget` 字符模式。
        """
        window = self.provider.context_tokens
        if self.context_budget > 0:
            # 手动模式：用于不上报 usage 的自定义端点
            if self._total_chars(messages) <= self.context_budget:
                return
            reason = f"字符预算 {self.context_budget:,}（手动指定）"
        else:
            if not window or not self.last_prompt_tokens:
                return  # 还没有真实计量，不猜
            ratio = self.last_prompt_tokens / window
            if ratio < self.compact_threshold:
                return
            reason = (
                f"真实用量 {self.last_prompt_tokens:,}/{window:,} tokens "
                f"({ratio:.0%} ≥ 阈值 {self.compact_threshold:.0%})"
            )

        keep_tail = 12
        if len(messages) <= keep_tail + 3:
            return
        head = messages[:2]
        tail = messages[-keep_tail:]
        dropped = messages[2:-keep_tail]
        dropped_actions = sum(1 for m in dropped if "<observation>" in (m.get("content") or ""))
        stub = {
            "role": "user",
            "content": (
                "<observation>【上下文压缩】为控制长度，中间 "
                f"{len(dropped)} 条消息（其中约 {dropped_actions} 条工具观察）已被折叠。"
                "已落盘的文件不会丢失；如需回顾，请用 read_file / list_directory 重新读取，"
                "不要凭记忆假设其内容。</observation>"
            ),
        }
        messages[:] = head + [stub] + tail
        print(f"\n🗜️  已折叠中段历史（{reason}）")
        # 折叠后真实用量要等下次响应才知道，先清零避免重复触发
        self.last_prompt_tokens = 0

    def timing_report(self) -> str:
        """把“慢”拆成可测量的分子：模型往返 vs 本地工具。"""
        total = self.time_model + self.time_tools
        model_share = (self.time_model / total) if total else 0.0
        return (
            f"  模型调用     = {self.calls} 次\n"
            f"  模型耗时     = {self.time_model:8.1f}s  ({model_share:.0%})\n"
            f"  工具耗时     = {self.time_tools:8.1f}s  ({1 - model_share:.0%})\n"
            f"  平均往返     = {self.time_model / max(1, self.calls):8.2f}s\n"
            f"  输入 tokens  = {self.total_prompt_tokens:,}（峰值 {self.peak_prompt_tokens:,}）\n"
            f"  输出 tokens  = {self.total_completion_tokens:,}"
            + (f"\n  输出被截断   = {self.truncations} 次（建议提高 --max-tokens）"
               if self.truncations else "")
        )

    # ------------------------------------------------------------ 主循环

    def run(self, user_input: str) -> str:
        system_prompt = self.build_system_prompt()
        print(
            f"\n📦 skill 目录：{self.skill_dir}"
            f"\n🎯 profile={self.profile}  预载={self.preload}"
            f"\n📂 输出根目录：{self.output_root}"
            f"\n🔧 模型接入：\n{self.describe_connection()}"
            f"\n🧮 system prompt 约 {len(system_prompt):,} 字符"
        )

        # 容量预检：不足就当场报错，不要跑 20 分钟才发现进不到创作阶段
        if self.capacity_check:
            print("\n📐 容量预检", flush=True)
            print(self.check_capacity(), flush=True)
        else:
            print("\n📐 容量预检已跳过（--no-capacity-check）", flush=True)

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"<question>{user_input}</question>"},
        ]

        step = 0
        seen_actions: Counter = Counter()
        recent_signatures: deque = deque(maxlen=LOOP_WINDOW)
        format_failures = 0

        while True:
            step += 1
            # 步数上限默认关闭（0）；仅在用户显式设限时才截断
            if self.max_steps and step > self.max_steps:
                return (
                    f"已达最大步数上限 {self.max_steps}，任务未完成。"
                    "建议改为 --max-steps 0（不限），让循环检测而不是步数来防护。"
                )
            label = f"{step}/{self.max_steps}" if self.max_steps else f"{step}/不限"
            print(f"\n{'=' * 60}\nStep {label}\n{'=' * 60}")
            if step % PROGRESS_HEARTBEAT_STEPS == 0:
                print(f"\n⏱️  已执行 {step} 步，仍在推进中（无步数上限，循环检测生效中）")

            content = self.call_model(messages)
            tools_started = time.time()

            # 真实用量可观测：这是判断“会不会溢”的唯一可靠依据
            if self.last_prompt_tokens and self.provider.context_tokens:
                pct = self.last_prompt_tokens / self.provider.context_tokens
                print(
                    f"📊 上下文 {self.last_prompt_tokens:,}"
                    f" / {self.provider.context_tokens:,} tokens ({pct:.1%})"
                    f"   ⏱️ 模型 {self.last_model_seconds:.1f}s"
                )

            thought = re.search(r"<thought>(.*?)</thought>", content, re.DOTALL)
            if thought:
                print(f"\n💭 Thought: {thought.group(1).strip()}")

            final = re.search(r"<final_answer>(.*?)</final_answer>", content, re.DOTALL)
            if final:
                return final.group(1).strip()

            action_matches = re.findall(r"<action>(.*?)</action>", content, re.DOTALL)
            if not action_matches:
                format_failures += 1
                if format_failures >= FORMAT_FAILURE_ABORT:
                    return (
                        f"模型连续 {format_failures} 次输出不合规（既无 <action> 也无 "
                        "<final_answer>），已终止。最后一段输出：\n"
                        + content[-800:]
                    )
                messages.append({
                    "role": "user",
                    "content": (
                        f"<observation>格式错误（第 {format_failures} 次）：你的回复既没有 "
                        "<final_answer>，也没有 <action>。请严格按格式重发："
                        "第一个标签 <thought>，第二个标签是一个或多个 <action>，"
                        "或 <final_answer>。不要包裹代码块。</observation>"
                    ),
                })
                continue
            format_failures = 0

            # —— 逐个执行本轮所有动作（支持一轮多个互不依赖的调用）——
            observations: List[str] = []
            abort_reason: Optional[str] = None

            for raw_action in (m.strip() for m in action_matches if m.strip()):
                try:
                    tool_name, args, kwargs = self.parse_action(raw_action)
                except ValueError as exc:
                    observations.append(f"工具调用解析失败：{exc}")
                    continue

                # —— 循环检测 ——
                # 第一层：同一动作完整重复（最直接的打转信号）
                signature = f"{tool_name}({raw_action})"
                seen_actions[signature] += 1
                repeats = seen_actions[signature]
                if self.stall_detect and self.stall_abort and repeats >= self.stall_abort:
                    abort_reason = (
                        f"检测到原地打转，提前终止：同一动作已完整重复 {repeats} 次。\n"
                        f"  重复的动作：{signature[:400]}\n"
                        f"  总执行步数：{step}\n"
                        "请到工程目录确认卡在哪一步（校验报告通常在 validation/ 下）。"
                    )
                    break

                # 第二层：窗口内动作多样性过低（抓 A,B,A,B 这类双动作打转）
                recent_signatures.append(signature)
                distinct = len(set(recent_signatures))
                if (self.stall_detect and len(recent_signatures) == LOOP_WINDOW
                        and distinct <= LOOP_DISTINCT_ABORT):
                    abort_reason = (
                        f"检测到死循环，提前终止：最近 {LOOP_WINDOW} 个动作里只有 {distinct} 种。\n"
                        f"  最近的动作：{list(dict.fromkeys(recent_signatures))[:4]}\n"
                        f"  总执行步数：{step}\n"
                        "这说明模型在几种动作间反复横跳而没推进，需要换模型或换提示策略。"
                    )
                    break

                printable = ", ".join(
                    [a[:60] if isinstance(a, str) else repr(a) for a in args]
                    + [f"{k}={v[:40]!r}" if isinstance(v, str) else f"{k}={v!r}"
                       for k, v in kwargs.items()]
                )
                print(f"\n🔧 Action: {tool_name}({printable})")

                if tool_name not in self.tool_map:
                    result = f"未知工具「{tool_name}」。可用工具：{', '.join(self.tool_map)}"
                else:
                    try:
                        result = str(self.tool_map[tool_name](*args, **kwargs))
                    except TypeError as exc:
                        result = (
                            f"参数不匹配：{exc}。签名："
                            f"{tool_name}{inspect.signature(self.tool_map[tool_name])}"
                        )
                    except Exception as exc:  # noqa: BLE001 —— 工具异常必须回灌给模型
                        result = f"工具执行错误（{type(exc).__name__}）：{exc}"

                if self.stall_detect and repeats >= STALL_WARN_REPEATS:
                    result = (
                        f"⚠️ 停滞提醒：这个完全相同的动作你已经调用第 {repeats} 次了。\n"
                        "如果是在返工，请改变策略（换工具、换参数、先读相关文档）而不是重复同一次调用；\n"
                        "如果是有意重试（例如刚改完文件、重跑校验），请在 <thought> 里说明理由；\n"
                        f"重复到 {self.stall_abort} 次会被强制终止。\n\n" + result
                    )
                elif (self.stall_detect and len(recent_signatures) == LOOP_WINDOW
                        and distinct <= LOOP_DISTINCT_WARN):
                    result = (
                        f"⚠️ 停滞提醒：最近 {LOOP_WINDOW} 个动作里只有 {distinct} 种。\n"
                        "请检查自己是不是在几种动作间反复横跳而没有实际推进；"
                        "如果确实在推进，请在 <thought> 里说明当前进展和下一步计划。\n\n" + result
                    )

                observations.append(result)

            if abort_reason:
                return abort_reason

            # 一轮多个动作合并成一条观察，只消耗一次往返
            if len(observations) == 1:
                payload = f"<observation>{observations[0]}</observation>"
            else:
                joined = "\n\n".join(
                    f"── 结果 {i}/{len(observations)} ──\n{text}"
                    for i, text in enumerate(observations, 1)
                )
                payload = (
                    f"<observation>本轮同时执行了 {len(observations)} 个动作，结果如下：\n\n"
                    f"{joined}</observation>"
                )

            preview = observations[0] if observations else "(无)"
            elapsed_tools = time.time() - tools_started
            self.time_tools += elapsed_tools
            print(
                f"\n🔍 Observation（{len(observations)} 项，耗时 {elapsed_tools:.1f}s）"
                f"：{clip(preview, 900, '预览')}"
            )
            messages.append({"role": "user", "content": payload})
            self._compact_if_needed(messages)


# ===========================================================================
# CLI
# ===========================================================================

def apply_staged_reasoning_defaults(mode: str) -> None:
    """staged 模式的锁定默认：P3 执行=none，P4 修复=low。

    见 LOCKED-DEFAULTS.md。用户 2026-10-19 明确要求锁定这两个值。

    为什么不直接改 `--reasoning-effort` 的默认：那是**全局**旋钮，P1/P2 规划
    也吃它；降到 none 会影响规划质量。这里只给 staged 的两个阶段旋钮设默认，
    而且用 `setdefault` —— 外部（pipeline.py 的 stage_env）已经设了就不覆盖。
    """
    if mode != "staged":
        return
    try:
        from staged import STAGE_REASONING_ENV
    except Exception:  # noqa: BLE001 —— 取不到就不设，不影响主流程
        return
    os.environ.setdefault(STAGE_REASONING_ENV["exec"], "none")
    os.environ.setdefault(STAGE_REASONING_ENV["repair"], "low")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("task", required=False)
@click.option("--task-file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="从文件读取任务描述（适合长 brief）。")
@click.option("--skill-dir", default=None, help="ppt-master skill 根目录；默认自动探测。")
@click.option("--output-root", default=None,
              help="生成项目的落盘根目录；默认 <SKILL_DIR>/projects。")
@click.option("--mode", type=click.Choice(["staged", "react"]), default="staged", show_default=True,
              help="staged=阶段化（Plan-and-Execute，代码驱动流程）；react=原自由循环（便于对照）。")
@click.option("--profile", type=click.Choice(["quick", "default"]), default="quick", show_default=True,
              help="仅 react 模式生效。staged 模式固定走 Default 路由的阶段化骨架。")
@click.option("--provider", type=click.Choice(sorted(PROVIDERS)), default=None,
              help="模型提供商；默认按 .env 里存在的 key 自动探测。")
@click.option("--base-url", default=None,
              help="自定义 OpenAI 兼容端点；可与 --provider 配合覆盖默认地址。")
@click.option("--api-key", default=None, help="直接传入 API Key；默认从 .env 读取。")
@click.option("--model", default=None,
              help="模型 ID；默认用所选提供商的默认模型。")
@click.option("--plan-model", default=None,
              help="staged 模式规划/填值阶段（P1/P2）使用的模型；默认 = --model。")
@click.option("--execute-model", default=None,
              help="staged 模式执行/修复阶段（P3/P4）使用的模型；默认 = --model。")
@click.option("--max-tokens", default=None, type=int,
              help="单次回复输出上限。不填则用提供商默认值（DeepSeek 为 65536）。")
@click.option("--temperature", default=None, type=float,
              help="采样温度。不填则用提供商默认值。")
@click.option("--thinking", type=click.Choice(["default", "enabled", "disabled"]), default="default",
              show_default=True,
              help="思考模式。default=跟随提供商（DeepSeek 默认开）。默认配合 low 推理强度：稳定、0 错误、快 15%%。")
@click.option("--reasoning-effort", type=click.Choice(["none", "low", "high", "max"]), default="low",
              show_default=True,
              help="推理强度。默认 low：README 实测 0 错误、快 15%%、质量持平。none 关思考（快 6~8 倍但排版可能崩，勿用于正式任务）。")
@click.option("--preload", type=click.Choice(PRELOAD_MODES), default="auto", show_default=True,
              help="提示词预载深度。auto=大窗口全量预载、小窗口自动精简为 lean。")
@click.option("--max-steps", default=DEFAULT_MAX_STEPS, show_default=True,
              help="ReAct 循环步数上限。默认 0 = 不限，任务跑完为止；防护交给循环检测。")
@click.option("--stall-abort", default=STALL_ABORT_REPEATS, show_default=True,
              help="同一动作完整重复到多少次就判定卡死并终止。0 = 关闭该项检测。")
@click.option("--loop-detect/--no-loop-detect", "stall_detect", default=True, show_default=True,
              help="窗口内动作多样性过低（如 A,B,A,B 反复横跳）时判定死循环并终止。")
@click.option("--command-timeout", default=900, show_default=True, help="单条命令超时（秒）。")
@click.option("--max-observation-chars", default=DEFAULT_MAX_OBSERVATION_CHARS, show_default=True,
              help="单条工具结果回灌给模型的最大字符数。")
@click.option("--parallel-pages", default=1, show_default=True, type=int,
              help="P3 逐页生成的并发度。默认 1 = 串行（思考型模型实测并行更慢，见 README §7）；>1 时多页并行。")
@click.option("--context-budget", default=0, show_default=True,
              help="字符预算模式（仅用于不上报 usage 的端点）。0=默认，改用提供商上报的真实 token 计量。")
@click.option("--context-tokens", default=None, type=int,
              help="声明模型实际上下文窗口（token）。同一模型名在不同渠道窗口可能不同。")
@click.option("--compact-threshold", default=0.85, show_default=True,
              help="真实用量达到窗口的该比例时才折叠中段历史（基于提供商上报的 usage）。")
@click.option("--capacity-check/--no-capacity-check", "capacity_check", default=True, show_default=True,
              help="开工前验证 skill 强制阅读量能否装进上下文；不足则直接报错，避免白跑。")
@click.option("--image-source", type=click.Choice(["off", "web", "ai", "auto"]), default="off", show_default=True,
              help="配图来源。off=不配图（默认，行为完全不变）/ web=联网图库 / ai=生图 / auto=按 design_spec §VIII 声明逐行走")
@click.option("--image-review", type=click.Choice(["auto", "score", "manual"]), default="score", show_default=True,
              help="web 图审图方式。auto=元数据直选（易夹废图）/ score=候选集评分后定稿 / manual=只备候选等人选")
@click.option("--image-providers", default=None,
              help="图库优先级，逗号分隔（默认 wikimedia,openverse,pexels,pixabay）")
@click.option("--image-max-candidates", default=8, show_default=True, type=int,
              help="每张 web 图最多拉多少候选")
@click.option("--image-strict/--no-image-strict", "image_strict", default=True, show_default=True,
              help="相关性闸门：取到的图与查询对不上就丢弃（宁可不配图，也不放错图）")
@click.option("--confirm-commands", is_flag=True, default=False,
              help="执行命令前询问（默认关闭）。默认行为：agent 拥有全局执行权限，直接运行。")
@click.option("--preflight/--no-preflight", default=True, show_default=True,
              help="开工前先发一次最小请求，提前暴露认证/端点/模型名错误。")
@click.option("--check", is_flag=True, default=False,
              help="只做环境与凭据体检，不执行任务。")
@click.option("--quiet", is_flag=True, default=False, help="减少终端输出。")
def main(task, task_file, skill_dir, output_root, profile, provider, base_url, api_key,
         model, plan_model, execute_model, max_tokens, temperature, thinking, reasoning_effort, preload, max_steps,
         stall_abort, stall_detect,
         command_timeout, mode, parallel_pages,
         max_observation_chars, context_budget, context_tokens, capacity_check,
         compact_threshold, confirm_commands, preflight, check, quiet,
         image_source, image_review, image_providers, image_max_candidates,
         image_strict):
    """基于 ppt-master skill 的 PPT 生成 Agent。

    示例：

      # 阶段化（默认）：代码驱动流程，模型只做判断与创作
      python agent.py "做一份 4 页的《什么是向量数据库》PPT，中文"

      # 自由循环（便于对照）
      python agent.py --mode react --profile quick "做一份 4 页的 PPT"

      # 先体检再开工
      python agent.py --check
    """
    # Windows 控制台编码已在模块导入时统一设为 UTF-8（见文件顶部）。

    resolved_skill = resolve_skill_dir(skill_dir)
    resolved_output = Path(
        output_root or os.getenv("PPT_AGENT_OUTPUT_ROOT") or (resolved_skill / "projects")
    ).expanduser().resolve()

    try:
        agent = PPTAgent(
            skill_dir=resolved_skill,
            output_root=resolved_output,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            profile=profile,
            auto_approve=not confirm_commands,
            max_steps=max_steps,
            stall_abort=stall_abort,
            stall_detect=stall_detect,
            command_timeout=command_timeout,
            max_observation_chars=max_observation_chars,
            context_budget=context_budget,
            context_tokens=context_tokens,
            capacity_check=capacity_check,
            compact_threshold=compact_threshold,
            preload=preload,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            verbose=not quiet,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc

    # —— 体检模式：只报告环境与凭据，不跑任务 ——
    if check:
        print("\n🔎 环境体检")
        print(f"   skill 目录 = {resolved_skill}")
        guard = subprocess.run(
            [get_python_command(), str(resolved_skill / GUARD_SCRIPT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(f"   完整性闸门 = exit {guard.returncode}")
        print(f"   profile    = {profile}\n")
        print("🔎 模型接入")
        print(agent.describe_connection())
        print(f"\n   system prompt = {len(agent.build_system_prompt()):,} 字符\n")
        if capacity_check:
            try:
                print("📐 " + agent.check_capacity(), flush=True)
            except RuntimeError as exc:
                print("📐 " + str(exc), flush=True)
                raise click.ClickException("容量预检未通过（原因见上方）") from exc
        print()
        try:
            agent.preflight()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        return

    if task_file:
        task = Path(task_file).read_text(encoding="utf-8")
    if not task:
        task = input("请输入 PPT 任务：")

    if preflight:
        try:
            agent.preflight()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

    # 锁定的分阶段推理默认（P3=none / P4=low），见 LOCKED-DEFAULTS.md
    apply_staged_reasoning_defaults(mode)

    if mode == "staged":
        from staged import StagedRunner
        runner = StagedRunner(
            agent,
            parallel_pages=parallel_pages,
            plan_model=plan_model,
            execute_model=execute_model,
            image_source=image_source,
            image_review=image_review,
            image_providers=tuple(
                p.strip() for p in (image_providers or "").split(",") if p.strip()
            ) or None,
            image_max_candidates=image_max_candidates,
            image_strict=image_strict,
        )
        try:
            final_answer = runner.run(task)
        except RuntimeError as exc:
            print(f"\n⏱️  耗时账本\n{agent.timing_report()}")
            raise click.ClickException(str(exc)) from exc
        print(f"\n\n✅ Final Answer：\n{final_answer}")
        print(f"\n⏱️  耗时账本\n{agent.timing_report()}")
        return

    final_answer = agent.run(task)
    print(f"\n\n✅ Final Answer：\n{final_answer}")
    print(f"\n⏱️  耗时账本\n{agent.timing_report()}")


if __name__ == "__main__":
    main()
