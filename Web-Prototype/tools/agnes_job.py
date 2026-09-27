#!/usr/bin/env python3
"""Agnes AI 适配器 —— Oil Motion 的图片与视频生成入口。

技能自带的 image_job.py / video_job.py 把 base URL 写死为 ZenMux，本项目实际使用
Agnes AI（https://api.agnes-ai.cn/v1），因此在这里提供等价且更完整的适配器。
按 api-key-setup.md 的要求：换适配器 = 换配置名 + 换业务参数，不能只换 Key。

与 ZenMux 版本的差异（都是本项目的实际收益）：
  * images/generations 支持 `image[]` 参考图 —— 关键帧之间可以真正锁身份，
    不必像 ZenMux 版那样只靠文字描述，也不必"每张关键帧互相漂移"。
  * videos 支持 mode=keyframe + first_frame/last_frame —— 首尾帧约束可用。
  * 视频按秒计费且当前为 $0/秒。

用法：
  ## 图片
  python3 tools/agnes_job.py image \
      --prompt-file source/prompt-K0.txt --size 1024x1024 \
      --output source/K0-alpha.png

  ## 图片（带参考图，用于锁身份）
  python3 tools/agnes_job.py image \
      --prompt-file source/prompt-K1.txt --image source/K0-alpha.png \
      --size 1024x1024 --output source/K1-alpha.png

  ## 视频（首尾帧）
  python3 tools/agnes_job.py video \
      --prompt-file source/prompt-seg-01-charge.txt \
      --mode keyframe --first-frame source/K0-alpha.png \
      --seconds 4 --size 720P --aspect-ratio 1:1 \
      --output source/seg-01-charge.mp4 --metadata source/seg-01-charge.job.json

不写入任何日志或元数据到凭据；AI 生成的素材一律落到 source/。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://api.agnes-ai.cn/v1"
DEFAULT_IMAGE_MODEL = "agnes-image-2.5-flash"
DEFAULT_VIDEO_MODEL = "agnes-video-2.5"

# 用户实测确认：只有 .cn 的 api 域接受 .cn 平台签发的令牌。
# apihub.agnes-ai.com 会返回 401 Invalid token，不要改回文档里的 .com。


class ApiError(RuntimeError):
    pass


def api_key() -> str:
    import os

    for name in ("AGNES_API_KEY", "ZENMUX_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ApiError(
        "未读到 Agnes API Key。请经凭据入口运行：\n"
        "  node <SKILL>/scripts/credential-ui/src/profile.ts run default -- python3 tools/agnes_job.py ..."
    )


def request_json(url: str, payload: dict | None, key: str, timeout: int = 180):
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {error.code} {url}\n{body[:900]}") from error
    except urllib.error.URLError as error:
        raise ApiError(f"网络失败 {url}：{error.reason}") from error


def download(url: str, target: Path, key: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            target.write_bytes(resp.read())
    except urllib.error.HTTPError:
        # 有些返回的 URL 是预签名直链，不接受 Authorization 头
        with urllib.request.urlopen(url, timeout=600) as resp:
            target.write_bytes(resp.read())
    return target


def as_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def media_ref(value: str) -> str:
    """把本地路径或 URL 统一成 API 能接受的引用。

    Agnes 的媒体参数文档写的是「公共媒体 URL」。本地文件先尝试 data URI；
    data URI 不是所有字段都接受，所以调用方要把失败原样报出来，不静默降级。
    """
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value).expanduser()
    if not path.exists():
        raise ApiError(f"找不到媒体文件：{path}")
    return as_data_uri(path)


# ── 图片 ───────────────────────────────────────────────────────────────
def cmd_image(args) -> int:
    key = api_key()
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    out = Path(args.output)
    if out.exists() and not args.force:
        raise ApiError(f"输出已存在：{out}；确认后加 --force")

    payload: dict = {"model": args.model, "prompt": prompt, "size": args.size}
    if args.image:
        payload["image"] = [media_ref(p) for p in args.image]
    # 顶级 response_format 会被忽略，必须塞进 extra_body
    payload["extra_body"] = {"response_format": args.response_format}

    print(f"[agnes] 提交图片生成 model={args.model} size={args.size} "
          f"参考图={len(args.image or [])} 张", flush=True)
    t0 = time.time()
    data = request_json(f"{args.base}/images/generations", payload, key, timeout=args.timeout)
    print(f"[agnes] 返回耗时 {time.time() - t0:.1f}s", flush=True)

    items = data.get("data") or []
    if not items:
        raise ApiError(f"响应里没有 data：{json.dumps(data, ensure_ascii=False)[:500]}")
    item = items[0]

    if item.get("b64_json"):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        download(item["url"], out, key)
    else:
        raise ApiError(f"未知的图片返回结构：{list(item.keys())}")

    print(f"[agnes] 已保存 {out}（{out.stat().st_size} 字节）", flush=True)
    if item.get("revised_prompt"):
        print("[agnes] revised_prompt:", item["revised_prompt"][:300], flush=True)
    if args.metadata:
        meta = {"endpoint": "/images/generations", "model": args.model,
                "size": args.size, "output": str(out),
                "reference_images": args.image or [],
                "usage": data.get("usage"), "created": data.get("created")}
        Path(args.metadata).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ── 视频 ───────────────────────────────────────────────────────────────
# 两套代际的参数形状不同，必须分开构造，不能混用：
#   v2.5（OpenAI Videos 兼容）：seconds / size / aspect_ratio / first_frame / last_frame
#   v2.0（原生）：height / width / num_frames(<=441, 遵循 8n+1) / frame_rate / image / extra_body
# 官方文档里 v2.0 标注当前价格 $0/秒，v2.5 是 $0.025/秒，所以 v2.0 值得先试。
def is_v20(model: str) -> bool:
    return model.strip().lower() in ("agnes-video-v2.0", "agnes-video-2.0")


def build_v20_payload(args, prompt: str) -> dict:
    payload: dict = {
        "model": args.model,
        "prompt": prompt,
        "mode": args.v20_mode,
        "height": args.height,
        "width": args.width,
        "num_frames": args.num_frames,
        "frame_rate": args.frame_rate,
    }
    if args.steps:
        payload["num_inference_steps"] = args.steps
    if args.seed is not None:
        payload["seed"] = args.seed
    if args.negative_prompt:
        payload["negative_prompt"] = args.negative_prompt
    if args.v20_mode == "ti2vid":
        if args.first_frame:
            payload["image"] = media_ref(args.first_frame)
    else:
        frames = ([media_ref(args.first_frame)] if args.first_frame else []) + \
                 ([media_ref(args.last_frame)] if args.last_frame else [])
        if not frames:
            raise ApiError("mode=keyframes 需要 --first-frame 和/或 --last-frame")
        payload["extra_body"] = {"mode": "keyframes", "image": frames}
    return payload


def cmd_video(args) -> int:
    key = api_key()
    prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    out = Path(args.output)
    if out.exists() and not args.force:
        raise ApiError(f"输出已存在：{out}；确认后加 --force")

    if is_v20(args.model):
        payload: dict = build_v20_payload(args, prompt)
        shape = "v2.0"
    else:
        payload = {
            "model": args.model,
            "prompt": prompt,
            "mode": args.mode,
            "seconds": str(args.seconds),
            "size": args.size,
            "aspect_ratio": args.aspect_ratio,
            "n": 1,
        }
        shape = "v2.5"
        if args.seed is not None:
            payload["seed"] = args.seed
        if args.mode == "keyframe":
            if not args.first_frame and not args.last_frame:
                raise ApiError("mode=keyframe 至少要给 --first-frame 或 --last-frame 之一")
            if args.first_frame:
                payload["first_frame"] = media_ref(args.first_frame)
            if args.last_frame:
                payload["last_frame"] = media_ref(args.last_frame)
        elif args.mode == "reference":
            if not args.image:
                raise ApiError("mode=reference 需要至少一张 --image")
            payload["images"] = [media_ref(p) for p in args.image]

    detail = (f"mode={args.v20_mode} num_frames={args.num_frames} "
              f"{args.width}x{args.height} fps={args.frame_rate}"
              if shape == "v2.0" else
              f"mode={args.mode} seconds={args.seconds} size={args.size} ratio={args.aspect_ratio}")
    print(f"[agnes] 提交视频任务 model={args.model} ({shape}) {detail}", flush=True)
    created = request_json(f"{args.base}/videos", payload, key, timeout=args.timeout)
    video_id = created.get("video_id") or created.get("id") or created.get("task_id")
    if not video_id:
        raise ApiError(f"没有拿到 video_id：{json.dumps(created, ensure_ascii=False)[:500]}")
    print(f"[agnes] video_id={video_id}", flush=True)

    deadline = time.time() + args.poll_timeout
    state = created
    while time.time() < deadline:
        time.sleep(args.poll_interval)
        state = request_json(
            f"{args.poll_base}/agnesapi?video_id={video_id}&model_name={args.model}",
            None, key, timeout=120)
        status = str(state.get("status", "?")).lower()
        print(f"[agnes] status={status} progress={state.get('progress')}", flush=True)
        if status in ("completed", "succeeded", "success"):
            break
        if status in ("failed", "error", "cancelled"):
            raise ApiError(f"任务失败：{json.dumps(state, ensure_ascii=False)[:700]}")
    else:
        raise ApiError(f"轮询超时（{args.poll_timeout}s），video_id={video_id}")

    url = state.get("url") or (state.get("metadata") or {}).get("url")
    if not url:
        raise ApiError(f"完成但没有 url：{json.dumps(state, ensure_ascii=False)[:700]}")
    download(url, out, key)
    print(f"[agnes] 已保存 {out}（{out.stat().st_size} 字节）", flush=True)

    if args.metadata:
        meta = {k: state.get(k) for k in
                ("id", "video_id", "task_id", "model", "status", "progress",
                 "seconds", "size", "created_at", "completed_at")}
        meta["request"] = {k: v for k, v in payload.items()
                           if k not in ("first_frame", "last_frame", "images", "prompt")}
        meta["output"] = str(out)
        Path(args.metadata).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"API base（默认 {DEFAULT_BASE}；不要改成 .com 的 apihub）")
    parser.add_argument("--timeout", type=int, default=300, help="单次请求超时秒数")
    sub = parser.add_subparsers(dest="command", required=True)

    img = sub.add_parser("image", help="生成/编辑图片")
    img.add_argument("--prompt-file", required=True)
    img.add_argument("--output", required=True)
    img.add_argument("--model", default=DEFAULT_IMAGE_MODEL)
    img.add_argument("--size", default="1024x1024",
                     help="1024x1024 / 1024x768 / 768x1024")
    img.add_argument("--image", action="append",
                     help="参考图路径或 URL，可重复；给了就是图生图")
    img.add_argument("--response-format", default="b64_json",
                     choices=("b64_json", "url"))
    img.add_argument("--metadata")
    img.add_argument("--force", action="store_true")
    img.set_defaults(func=cmd_image)

    vid = sub.add_parser("video", help="提交并轮询视频任务")
    vid.add_argument("--prompt-file", required=True)
    vid.add_argument("--output", required=True)
    vid.add_argument("--model", default=DEFAULT_VIDEO_MODEL)
    vid.add_argument("--mode", default="keyframe",
                     choices=("text", "keyframe", "reference"))
    vid.add_argument("--first-frame")
    vid.add_argument("--last-frame")
    vid.add_argument("--image", action="append", help="mode=reference 时的参考图")
    vid.add_argument("--seconds", default="4")
    vid.add_argument("--size", default="720P",
                     choices=("720P", "1080P", "1K", "2K"))
    vid.add_argument("--aspect-ratio", default="1:1",
                     help="1:1 / 16:9 / 9:16 / 4:3 / 3:4")
    vid.add_argument("--seed", type=int)
    vid.add_argument("--poll-base", default="https://api.agnes-ai.cn",
                     help="轮询用的根（/agnesapi 挂在根上，不在 /v1 下）")
    vid.add_argument("--poll-interval", type=float, default=8.0)
    vid.add_argument("--poll-timeout", type=float, default=900.0)
    # v2.0（原生参数形状）
    vid.add_argument("--v20-mode", default="ti2vid", choices=("ti2vid", "keyframes"))
    vid.add_argument("--num-frames", type=int, default=97,
                     help="v2.0：<=441 且遵循 8n+1；97 帧 @24fps 约 4 秒")
    vid.add_argument("--frame-rate", type=float, default=24.0, help="v2.0：1-60")
    vid.add_argument("--width", type=int, default=768)
    vid.add_argument("--height", type=int, default=768)
    vid.add_argument("--steps", type=int, default=0,
                     help="v2.0：推理步数；0 表示不传，交给服务端默认")
    vid.add_argument("--negative-prompt", default=None)
    vid.add_argument("--metadata")
    vid.add_argument("--force", action="store_true")
    vid.set_defaults(func=cmd_video)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except ApiError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
