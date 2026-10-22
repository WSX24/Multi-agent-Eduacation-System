#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检：这台机器能用哪些「取图」通道。

分两类，互相独立：

  A. 网络图库（image_search.py）：openverse / wikimedia / pexels / pixabay
  B. AI 生图（image_gen.py）：openai / gemini / qwen / zhipu / volcengine / siliconflow ...

用法::

    python check_image_sources.py          # 只体检，不花钱
    python check_image_sources.py --probe  # 额外真跑一次图库搜索（仅已配 key 的图库）

设计取舍：**不打 LLM / 不生成图片**。只做 TLS 可达性 + 凭据存在性检查，
因为生图一次就是真金白银，体检不该花钱。
"""
from __future__ import annotations

import argparse
import os
import socket
import ssl
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
SKILL_CANDIDATES = (
    Path(os.getenv("PPT_MASTER_SKILL_DIR", "")),
    HERE.parent / ".agents" / "skills" / "ppt-master",
    HERE.parent / "ppt-master" / "skills" / "ppt-master",
)

#: 图库：名字 → (探测主机, 所需环境变量, 是否零配置)
WEB_SOURCES = {
    "openverse": ("api.openverse.org", (), True),
    "wikimedia": ("commons.wikimedia.org", (), True),
    "met": ("collectionapi.metmuseum.org", (), True),
    "pexels": ("api.pexels.com", ("PEXELS_API_KEY",), False),
    "pixabay": ("pixabay.com", ("PIXABAY_API_KEY",), False),
}

#: 生图后端：名字 → (探测主机, 首选环境变量)
AI_BACKENDS = {
    "openai": ("api.openai.com", ("OPENAI_API_KEY",)),
    "gemini": ("generativelanguage.googleapis.com", ("GEMINI_API_KEY",)),
    "qwen": ("dashscope.aliyuncs.com", ("QWEN_API_KEY", "DASHSCOPE_API_KEY")),
    "zhipu": ("open.bigmodel.cn", ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY")),
    "volcengine": ("ark.cn-beijing.volces.com", ("LAS_API_KEY",)),
    "siliconflow": ("api.siliconflow.cn", ("SILICONFLOW_API_KEY",)),
    "minimax": ("api.minimaxi.com", ("MINIMAX_API_KEY",)),
}


def find_skill_dir() -> Path | None:
    for cand in SKILL_CANDIDATES:
        if not str(cand):
            continue
        if (cand / "SKILL.md").is_file() and (cand / "scripts" / "image_search.py").is_file():
            return cand
    return None


def load_env() -> None:
    """读 PPT-Agent/.env（KEY=VALUE），不覆盖已有环境变量。

    为什么要读：图库 key 和 PPT_IMAGE_PROXY 都写在这里，而体检脚本是
    直接跑的，不经过 agent.py 的 load_env_files()。
    """
    for base in (HERE, *HERE.parents):
        env_file = base / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


def proxy_url() -> str:
    return (os.getenv("PPT_IMAGE_PROXY") or "").strip()


def reachable(host: str) -> tuple[bool, str]:
    """探可达性。配了 PPT_IMAGE_PROXY 就走代理（否则直连，会误报不通）。"""
    px = proxy_url()
    if px:
        try:
            import requests
            r = requests.get(f"https://{host}/", timeout=20,
                             proxies={"http": px, "https": px},
                             headers={"User-Agent": "ppt-agent/1.0"})
            return True, f"代理 HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"代理{type(exc).__name__}"
    return tls_ok(host)


def tls_ok(host: str, timeout: float = 6.0) -> tuple[bool, str]:
    try:
        sock = socket.create_connection((host, 443), timeout=timeout)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            return True, ss.version() or "tls"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"


def env_state(names: tuple[str, ...]) -> tuple[bool, str]:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return True, f"{name}(len={len(value)})"
    return False, "未配置"


def probe_web(skill_dir: Path, provider: str) -> str:
    """真跑一次图库搜索（会联网，但不花钱）。"""
    out = Path(os.getenv("TEMP") or "/tmp") / f"_imgprobe_{provider}"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(skill_dir / "scripts" / "image_search.py"),
           "office desk", "--filename", "probe.jpg", "-o", str(out),
           "--provider", provider]
    env = dict(os.environ)
    px = proxy_url()
    if px:
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[key] = px
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return "超时"
    if (out / "probe.jpg").is_file():
        size = (out / "probe.jpg").stat().st_size
        return f"成功（{size // 1024} KB）"
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    return "失败：" + (tail[-1][:70] if tail else "无输出")


def main() -> int:
    ap = argparse.ArgumentParser(description="取图通道体检（不花钱）")
    ap.add_argument("--probe", action="store_true", help="对已配 key 的图库真跑一次搜索")
    args = ap.parse_args()

    load_env()
    skill_dir = find_skill_dir()
    px = proxy_url()
    print("=" * 68)
    print("取图通道体检" + (f"    skill = {skill_dir}" if skill_dir else "    ⚠️ 未找到 ppt-master skill"))
    print(f"代理：{px if px else '未配置（直连；国内访问 Wikimedia/Openverse 会时段性超时）'}")
    print("=" * 68)

    print("\n【A】网络图库（image_search.py）")
    usable_web = []
    for name, (host, keys, zero_config) in WEB_SOURCES.items():
        ok_net, net_note = reachable(host)
        has_key, key_note = env_state(keys)
        if zero_config:
            ready = ok_net
            cred = "零配置"
        else:
            ready = ok_net and has_key
            cred = key_note
        mark = "✅" if ready else "❌"
        print(f"  {mark} {name:10s} 网络={'通' if ok_net else '不通(' + net_note + ')'}"
              f"  凭据={cred}")
        if ready:
            usable_web.append(name)

    if args.probe and skill_dir:
        print("\n  --probe 实跑（仅已就绪通道）：")
        for name in usable_web:
            print(f"    {name}: {probe_web(skill_dir, name)}")

    print("\n【B】AI 生图（image_gen.py）")
    usable_ai = []
    current = (os.getenv("IMAGE_BACKEND") or "").strip().lower()
    for name, (host, keys) in AI_BACKENDS.items():
        ok_net, net_note = reachable(host)
        has_key, key_note = env_state(keys)
        ready = ok_net and has_key
        mark = "✅" if ready else "❌"
        active = "  ← 当前 IMAGE_BACKEND" if name == current else ""
        print(f"  {mark} {name:12s} 网络={'通' if ok_net else '不通(' + net_note + ')'}"
              f"  凭据={key_note}{active}")
        if ready:
            usable_ai.append(name)

    print("\n" + "-" * 68)
    print(f"结论：图库可用 {len(usable_web)} 家 {usable_web or '（无）'}")
    print(f"      生图可用 {len(usable_ai)} 家 {usable_ai or '（无）'}")
    if not usable_web and not usable_ai:
        print("\n下一步：在 PPT-Agent/.env 里加一把 key 即可（两家都免费注册）：")
        print("  网络图库（适合实景/产品/办公场景）：")
        print("    PEXELS_API_KEY=...     https://www.pexels.com/api/")
        print("    PIXABAY_API_KEY=...    https://pixabay.com/api/docs/")
        print("  国内 AI 生图（适合人物/国风/概念插画，网络已验证可达）：")
        print("    IMAGE_BACKEND=qwen")
        print("    QWEN_API_KEY=...       阿里云百炼 DashScope")
        print("    # 或 IMAGE_BACKEND=zhipu   ZHIPU_API_KEY=...")
        print("    # 或 IMAGE_BACKEND=volcengine  LAS_API_KEY=...")
    print("-" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
