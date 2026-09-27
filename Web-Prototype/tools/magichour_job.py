#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# magichour_job.py — Magic Hour AI（https://api.magichour.ai）的图片 / 视频作业工具
#
# 为什么需要它：用户 2026-09-26 把生成服务换成了 Magic Hour
# （magichour.ai/developer）。原来的 tools/agnes_job.py 打的是
# api.agnes-ai.cn，拿 Magic Hour 的 key 必然 401 —— 这是换服务，不是换 key。
#
# 凭据：经 credential-ui 的 run 入口注入到子进程环境变量里。
#   槽位名 oil-motion/zenmux/default 与变量名 ZENMUX_API_KEY 是历史遗留的
#   程序兼容名，**实际服务是 Magic Hour**。优先读 MAGIC_HOUR_API_KEY，
#   没有则回落到 ZENMUX_API_KEY。Key 只存在于进程环境，不写日志、不进参数、
#   不进元数据。
#
# 用法：
#   # 上传本地文件拿 file_path（Magic Hour 的图片/视频输入都用这个引用）
#   python tools/magichour_job.py upload --file source/K0-shelf-graded.png
#
#   # 图生视频（首帧；可选 --last-frame 给尾帧约束）
#   python tools/magichour_job.py video \
#     --prompt-file source/prompt-seg-01-mh.txt \
#     --first-frame source/K0-shelf-graded.png \
#     --seconds 5 --model kling-3.0 --resolution 720p \
#     --output source/seg-01-knowledge.mp4
#
#   # 查状态
#   python tools/magichour_job.py status --kind video --id <project_id>
#
#   # 文生图（关键帧）
#   python tools/magichour_job.py image \
#     --prompt-file source/prompt-K0-shelf.txt \
#     --aspect-ratio 16:9 --resolution 1k --model flux-schnell \
#     --output source/K0-shelf-16x9.png
# ═══════════════════════════════════════════════════════════════════════
import argparse
import json
import os
import mimetypes
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.magichour.ai"
# 代理：本机实测直连 magichour 会偶发被重置（curl 56 / RemoteDisconnected），
# 而 127.0.0.1:7897 上的本地代理稳定可用。用 --proxy 或 MAGIC_HOUR_PROXY 指定，
# 不写死端口 —— 那是环境相关的，写进代码会让别人复现不了。
PROXY = os.environ.get("MAGIC_HOUR_PROXY", "")
RETRIES = 6
DEFAULT_POLL = 8.0
DEFAULT_TIMEOUT = 3600.0


def key() -> str:
    k = os.environ.get("MAGIC_HOUR_API_KEY") or os.environ.get("ZENMUX_API_KEY")
    if not k:
        raise SystemExit(
            "缺少凭据：环境里没有 MAGIC_HOUR_API_KEY / ZENMUX_API_KEY。\n"
            "必须经 run 入口执行，例如：\n"
            '  node <skill>/scripts/credential-ui/src/profile.ts run default -- \\\n'
            "    python tools/magichour_job.py ..."
        )
    return k


def call(method: str, path: str, body=None, raw: bytes = None,
         content_type: str = "application/json", expect_json: bool = True):
    """所有 JSON 接口。带重试：实测网络会偶发重置，一次失败不代表接口错。
    4xx 是确定性的（字段错 / key 错 / 额度不足 / 套餐不含），不重试；
    5xx 与网络异常才重试。"""
    url = BASE + path
    data = None
    headers = {"Authorization": "Bearer " + key(), "accept": "application/json"}
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})] if PROXY else []
    opener = urllib.request.build_opener(*handlers)
    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=120) as r:
                payload = r.read()
                if not expect_json:
                    return r.status, payload
                return r.status, json.loads(payload or b"{}")
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", "replace")
            if e.code < 500:
                raise SystemExit(
                    f"HTTP {e.code} {url}\n{body_text[:600]}\n\n"
                    "错误码含义：400 字段错 / 401 key 错 / 402 额度不足 / "
                    "403 该模型或分辨率不在你的套餐内")
            last = f"HTTP {e.code} {body_text[:200]}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        wait = min(20.0, 1.6 ** attempt)
        print(f"  [重试 {attempt + 1}/{RETRIES}] {last}  → {wait:.1f}s 后重试", flush=True)
        time.sleep(wait)
    raise SystemExit(f"{RETRIES} 次仍失败：{last}")


def curl_put(url: str, path: Path, content_type: str) -> None:
    """用 curl 上传，不用 urllib。
    实测环境：Python 3.10.11 / OpenSSL 1.1.1t 与 videos.magichour.ai 协商 TLS 失败
    （TLSV1_ALERT_PROTOCOL_VERSION，随后 RemoteDisconnected），而同一个 URL 用 curl
    能正常 PUT（HTTP 200）。这是本机 TLS 栈的问题，不是 API 的问题，
    所以把「传字节」这件事交给 curl，JSON 接口仍走 urllib。
    预签名 URL 不能带 Authorization 头。"""
    if not shutil.which("curl"):
        raise SystemExit("需要 curl（本机 Python 的 TLS 栈连不上 videos.magichour.ai）")
    base_cmd = ["curl", "-sS", "-o", os.devnull, "-w", "%{http_code}", "-X", "PUT"]
    if PROXY:
        base_cmd += ["--proxy", PROXY]
    base_cmd += ["--retry", "5", "--retry-all-errors", "--retry-delay", "2"]
    r = subprocess.run(
        base_cmd + ["--data-binary", "@" + str(path),
                    "-H", "Content-Type: " + content_type, url],
        capture_output=True, text=True)
    if r.returncode != 0 or r.stdout.strip() not in ("200", "201", "204"):
        raise SystemExit("上传失败：curl 退出码 %s HTTP %s\n%s"
                         % (r.returncode, r.stdout.strip(), r.stderr[:400]))


def curl_download(url: str, out: Path) -> int:
    """下载同样交给 curl —— downloads[0].url 也在 magichour.ai 的存储主机上。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("curl"):
        cmd = ["curl", "-sSL", "-o", str(out)]
        if PROXY:
            cmd += ["--proxy", PROXY]
        cmd += ["--retry", "5", "--retry-all-errors", "--retry-delay", "2", url]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("下载失败：curl 退出码 %s\n%s"
                             % (r.returncode, r.stderr[:400]))
        return out.stat().st_size
    return download(url, out)


def upload_file(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    _, res = call("POST", "/v1/files/upload-urls",
                  body={"items": [{"type": "image", "extension": ext}]})
    item = res["items"][0]
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    curl_put(item["upload_url"], path, ctype)
    return item["file_path"]


def asset_ref(spec: str) -> str:
    """既接受本地路径（自动上传），也接受已经是 file_path / http URL 的字符串。"""
    if spec.startswith("http://") or spec.startswith("https://") or spec.startswith("api-assets/"):
        return spec
    p = Path(spec)
    if not p.exists():
        raise SystemExit(f"找不到输入文件 {p}")
    return upload_file(p)


def poll(kind: str, project_id: str, interval: float, timeout: float) -> dict:
    path = f"/v1/{kind}-projects/{project_id}"
    t0 = time.time()
    while True:
        _, res = call("GET", path)
        st = res.get("status")
        el = time.time() - t0
        print(f"  [{el:5.1f}s] status={st}"
              + (f"  credits={res.get('credits_charged')}" if res.get("credits_charged") else ""),
              flush=True)
        if st == "complete":
            return res
        if st in ("error", "canceled"):
            raise SystemExit(f"作业 {st}：{json.dumps(res.get('error'), ensure_ascii=False)}")
        if el > timeout:
            raise SystemExit(f"超时 {timeout:.0f}s 仍未完成；project_id={project_id} 可稍后用 status 查")
        time.sleep(interval)


def download(url: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=600) as r:
        data = r.read()
    out.write_bytes(data)
    return len(data)


def cmd_upload(a) -> int:
    fp = upload_file(Path(a.file))
    print(f"file_path: {fp}")
    if a.report:
        Path(a.report).write_text(json.dumps({"file": a.file, "file_path": fp},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_status(a) -> int:
    _, res = call("GET", f"/v1/{a.kind}-projects/{a.id}")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
    return 0


def cmd_edit(a) -> int:
    """图片编辑：拿一张现成画面改一处，用于生成「尾帧」。
    为什么需要它：只给首帧时，视频模型对终点没有任何锚点 —— 实测 seg-02
    把"光汇入核心"做成了"核心左边堆出一团紫云"（紫色重心从 81.7% 漂到
    56.8%，面积涨到 9.36%）。而 Magic Hour 的图生视频支持 end_image_file_path，
    所以先用编辑器把一个符合要求的尾帧做出来，再把两端都钉住。
    用编辑器而不是文生图：文生图会重画整个场景（实测四张关键帧之间的边缘
    相关只有 0.05），而编辑是在原图上改。"""
    prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    body = {
        "name": a.name or ("oil-motion edit " + time.strftime("%Y%m%d-%H%M%S")),
        "image_count": 1,
        "model": a.model,
        "aspect_ratio": a.aspect_ratio,
        "resolution": a.resolution,
        "style": {"prompt": prompt},
        "assets": {"image_file_path": asset_ref(a.image)},
    }
    print(f"[magichour] 提交图片编辑：model={a.model} res={a.resolution} aspect={a.aspect_ratio}")
    _, res = call("POST", "/v1/ai-image-editor", body=body)
    pid = res["id"]
    print(f"[magichour] project_id={pid} credits≈{res.get('credits_charged')}")
    final = poll("image", pid, a.poll_interval, a.poll_timeout)
    n = curl_download(final["downloads"][0]["url"], Path(a.output))
    print(f"[magichour] 已保存 {a.output}（{n} 字节，实扣 {final.get('credits_charged')} credits）")
    if a.metadata:
        Path(a.metadata).write_text(json.dumps(
            {"provider": "magic-hour", "kind": "ai-image-editor", "project_id": pid,
             "model": a.model, "resolution": a.resolution,
             "credits_charged": final.get("credits_charged"),
             "assets": body["assets"]},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_image(a) -> int:
    prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    body = {
        "name": a.name or ("oil-motion keyframe " + time.strftime("%Y%m%d-%H%M%S")),
        "image_count": 1,
        "aspect_ratio": a.aspect_ratio,
        "resolution": a.resolution,
        "model": a.model,
        "style": {"prompt": prompt},
    }
    if a.tool:
        body["style"]["tool"] = a.tool
    print(f"[magichour] 提交图片：model={a.model} aspect={a.aspect_ratio} res={a.resolution}")
    _, res = call("POST", "/v1/ai-image-generator", body=body)
    pid = res["id"]
    print(f"[magichour] project_id={pid} credits_charged≈{res.get('credits_charged')}")
    final = poll("image", pid, a.poll_interval, a.poll_timeout)
    url = final["downloads"][0]["url"]
    n = curl_download(url, Path(a.output))
    print(f"[magichour] 已保存 {a.output}（{n} 字节，实扣 {final.get('credits_charged')} credits）")
    if a.metadata:
        Path(a.metadata).write_text(json.dumps(
            {"provider": "magic-hour", "kind": "image", "project_id": pid,
             "request": {k: v for k, v in body.items() if k != "style"},
             "model": a.model, "resolution": a.resolution, "aspect_ratio": a.aspect_ratio,
             "credits_charged": final.get("credits_charged")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_video(a) -> int:
    prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    if a.negative_prompt:
        neg = Path(a.negative_prompt).read_text(encoding="utf-8").strip()
        # Magic Hour 的 image-to-video 只有一个 style.prompt 字段，没有独立的负面提示词，
        # 所以把负面约束并进提示词末尾。这不是等价替代，是接口限制下的折中。
        prompt = prompt + "\n\nStrictly avoid all of the following: " + neg + "."
    assets = {"image_file_path": asset_ref(a.first_frame)}
    if a.last_frame:
        assets["end_image_file_path"] = asset_ref(a.last_frame)
    body = {
        "name": a.name or ("oil-motion segment " + time.strftime("%Y%m%d-%H%M%S")),
        "end_seconds": a.seconds,
        "model": a.model,
        "resolution": a.resolution,
        "audio": False,          # 人声/配乐另算 credits，本片不需要
        "style": {"prompt": prompt},
        "assets": assets,
    }
    mode = "首帧+尾帧" if a.last_frame else "仅首帧"
    print(f"[magichour] 提交视频：模型 {a.model}  分辨率 {a.resolution}  "
          f"{a.seconds}s  {mode}  输出比例随输入图")
    _, res = call("POST", "/v1/image-to-video", body=body)
    pid = res["id"]
    print(f"[magichour] project_id={pid} 预估 credits={res.get('credits_charged')}")
    final = poll("video", pid, a.poll_interval, a.poll_timeout)
    url = final["downloads"][0]["url"]
    n = curl_download(url, Path(a.output))
    print(f"[magichour] 已保存 {a.output}（{n} 字节）"
          f"  实扣 {final.get('credits_charged')} credits"
          + (f"  实际 fps={final.get('fps')}" if final.get("fps") else ""))
    if a.metadata:
        Path(a.metadata).write_text(json.dumps(
            {"provider": "magic-hour", "kind": "image-to-video", "project_id": pid,
             "model": a.model, "resolution": a.resolution, "end_seconds": a.seconds,
             "mode": "first+last" if a.last_frame else "first-only",
             "credits_charged": final.get("credits_charged"), "fps": final.get("fps"),
             "assets": assets},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    global PROXY
    ap = argparse.ArgumentParser(description="Magic Hour AI 作业工具（图片 / 图生视频）")
    ap.add_argument("--proxy", default=None,
                    help="HTTP 代理，例如 http://127.0.0.1:7897。本机实测直连 magichour "
                         "会偶发被重置，走本地代理稳定。也可用 MAGIC_HOUR_PROXY 环境变量。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("upload", help="上传本地文件，打印 file_path")
    u.add_argument("--file", required=True)
    u.add_argument("--report")
    u.set_defaults(fn=cmd_upload)

    s = sub.add_parser("status", help="查作业状态")
    s.add_argument("--kind", choices=["video", "image"], required=True)
    s.add_argument("--id", required=True)
    s.set_defaults(fn=cmd_status)

    i = sub.add_parser("image", help="文生图")
    i.add_argument("--prompt-file", required=True)
    i.add_argument("--output", required=True)
    i.add_argument("--aspect-ratio", choices=["16:9", "9:16", "1:1"], default="16:9")
    i.add_argument("--resolution", choices=["640px", "1k", "2k", "4k"], default="1k")
    i.add_argument("--model", default="flux-schnell")
    i.add_argument("--tool")
    i.add_argument("--name")
    i.add_argument("--metadata")
    i.set_defaults(fn=cmd_image)

    e = sub.add_parser("edit", help="图片编辑（在现成画面上改一处，用于生成尾帧）")
    e.add_argument("--prompt-file", required=True)
    e.add_argument("--image", required=True, help="输入图（本地路径会自动上传）")
    e.add_argument("--output", required=True)
    e.add_argument("--aspect-ratio",
                   choices=["auto", "16:9", "9:16", "4:3", "3:2", "1:1", "4:5", "2:3"],
                   default="16:9")
    e.add_argument("--resolution", choices=["auto", "640px", "1k", "2k", "4k"], default="640px")
    e.add_argument("--model", default="qwen-edit")
    e.add_argument("--name")
    e.add_argument("--metadata")
    e.set_defaults(fn=cmd_edit)

    v = sub.add_parser("video", help="图生视频（首帧，可选尾帧）")
    v.add_argument("--prompt-file", required=True)
    v.add_argument("--negative-prompt")
    v.add_argument("--first-frame", required=True)
    v.add_argument("--last-frame")
    v.add_argument("--output", required=True)
    v.add_argument("--seconds", type=float, default=5)
    v.add_argument("--model", default="kling-3.0")
    v.add_argument("--resolution", choices=["480p", "720p", "1080p", "4k"], default="720p")
    v.add_argument("--name")
    v.add_argument("--metadata")
    v.set_defaults(fn=cmd_video)

    for p in (i, v, e):
        p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL)
        p.add_argument("--poll-timeout", type=float, default=DEFAULT_TIMEOUT)

    a = ap.parse_args()
    if a.proxy is not None:
        PROXY = a.proxy
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
