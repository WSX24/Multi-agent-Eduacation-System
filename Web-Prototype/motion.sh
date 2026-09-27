#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# 智塾 landing 机器人 — Oil Motion 可复现处理命令
#
# 用法：bash motion.sh <stage>
#
# 本项目的实际路线（与技能默认路线的每处差异都写在明处，不悄悄改）：
#   · 生成平台是 Agnes AI，走 tools/agnes_job.py。技能自带的 image_job.py /
#     video_job.py 把 base URL 写死为 ZenMux，本项目不用。
#   · background_owner = video：主体与纯白背景同图烘焙，**不要 alpha、不抠色**。
#     所以技能里的色键 / alpha-atlas 阶段在本项目不适用，脚本里没有它们。
#   · 待机姿态是**站立**，而且**站在地平线上**（`.anchor--a` 的 --stage-lift 已删除）。
#     悬停待机于 2026-09-25 由用户取消，见 source/concept-contract.yaml 决定 #7。
#   · 运行时控制器是 segment-playback，成片不需要逐帧可寻址；
#     编译后做一次仅码率层面的重编码去掉全关键帧（build/media/post-encode.json）。
#
# 阶段：
#   key-status            凭据库里有没有非空值（0=有，2=缺失，1=后端失败）
#   key-setup             启动本机凭据页面（用户亲自填写）
#   budget                执行预算 -> build/motion-budget.json
#   k0-recover            从成片第 0 帧复原站立母版（无 API 花费）
#   k0-generate           用图片模型重生成站立母版 1024x1024（需凭据）
#   verify-k0             K0 硬门：背景平涂 / 脚底接触线 / 主体面积
#   pilot                 生成 seg-01-charge（Pilot 段）
#   verify-seg <file>     单段硬门：锚点漂移 / 肢体分离 / 动作到位
#   approve               写 pilot/approval.json（**必须由用户签收**）
#   segs                  生成 seg-02-arc 与 seg-03-rise（chain 模式）
#   chain                 帧链两道硬门：生成输入接力 + 成片输出接缝
#   compile               合并 + 编译 + 由编译结果生成 build/timeline.json
#   probe                 页面几何探针（anchorA/anchorB 底边 vs #filmFloor）
#
# 为什么用 .agents 而不是 .pi\agent 路径：
#   C:\Users\21495\.pi\agent\skills\oil-motion 是指向
#   C:\Users\21495\.agents\skills\oil-motion 的 junction。
#   profile.ts 的入口守卫比较 pathToFileURL(path.resolve(process.argv[1]))
#   与 import.meta.url，junction 会让两者不一致，于是 main() 从不执行，
#   脚本静默 exit 0 且无输出（看起来像"已配置"）。必须走真实路径。
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OIL_MOTION="C:/Users/21495/.agents/skills/oil-motion"
PROFILE="node $OIL_MOTION/scripts/credential-ui/src/profile.ts"

# 解释器：技能的 compile / production_gate 需要 numpy，其它工具需要 Pillow。
# 本机的 python3 两者都不齐（只有 Pillow），python(3.10) 与 py(3.13) 都有。
pick_python() {
  for c in python python3 py; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import numpy, PIL" >/dev/null 2>&1; then echo "$c"; return 0; fi
  done
  echo "找不到同时具备 numpy 与 Pillow 的 Python（技能的 compile/production_gate 需要 numpy）" >&2
  exit 1
}
PY="$(pick_python)"

# ── 合同锁定的数值，全部来自四个事实源，不在此处另起一份 ──────────────
DISPLAY="358x358"          # 最大 CSS 尺寸，见 concept-contract.destination
DPR=2                      # 目标 DPR -> 运行时单元 716x716
EXPECT_FEET=0.899          # identity-bible §2 实测
MIN_WIDTH_PX=716           # 358 × DPR 2
SRC_WH=960x960             # 当前母版与成片的实际尺寸
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"

cd "$ROOT"
mkdir -p source pilot build qa final

need() { [ -e "$1" ] || { echo "缺少上游产物：$1（先跑前一个阶段）" >&2; exit 1; }; }

probe_frames() {  # 从编译结果或媒体读出真实帧数，不手工推算
  if [ -f build/timeline.json ]; then
    $PY -c "import json;print(int(json.load(open('build/timeline.json'))['frameCount']))"
  else
    ffprobe -v error -select_streams v:0 -count_frames \
      -show_entries stream=nb_read_frames -of csv=p=0 "$1"
  fi
}

# ── 1. 凭据 ────────────────────────────────────────────────────────────
stage_key_status() {
  echo "== 凭据状态 =="
  $PROFILE status default && echo "可读" || {
    code=$?
    [ "$code" = 2 ] && echo "尚未配置：跑 bash motion.sh key-setup" >&2
    [ "$code" = 1 ] && echo "配置或系统凭据后端失败" >&2
    exit "$code"
  }
}

stage_key_setup() {
  echo "== 启动本机凭据页面（由用户亲自填写）=="
  $PROFILE setup default
}

# ── 2. 预算 ────────────────────────────────────────────────────────────
stage_budget() {
  need source/seg-01-charge.mp4
  local frames; frames="$(probe_frames source/seg-01-charge.mp4)"
  echo "== 执行预算（frames=$frames）=="
  $PY "$OIL_MOTION/scripts/motion_budget.py" \
    --frames "$frames" --display "$DISPLAY" --dpr "$DPR" \
    --driver scroll --parameter-space linear --time-control segment-play \
    --background-owner video --source "$SRC_WH" \
    --report build/motion-budget.json --strict --json
  echo "→ build/motion-budget.json"
}

# ── 3. K0 关键帧 ───────────────────────────────────────────────────────
# 两条路都保留：复原路径零花费且与既有成片同源；生成路径能得到干净的 1024 母版。
stage_k0_recover() {
  need build/media/final/poster.png
  echo "== 从成片第 0 帧复原站立母版（该帧就是当初那张母版经视频管线落地后的样子）=="
  cp build/media/final/poster.png source/K0-standing-raw.png
  $PY tools/whiten_background.py source/K0-standing-raw.png --threshold 234 --out source/K0.png
  cp source/K0.png assets/robot-k0.png
  echo "→ source/K0.png = assets/robot-k0.png（洪泛刷白后边距必须是 255）"
}

stage_k0_generate() {
  echo "== 用图片模型重生成站立母版（锁参考图身份，1024x1024）=="
  $PROFILE run default -- $PY tools/agnes_job.py image \
    --prompt-file source/prompt-K0-ref.txt \
    --image source/reference/robot-user-ref.png \
    --size 1024x1024 \
    --output source/K0-generated.png \
    --force
  echo "→ 生成结果必须先过 verify-k0 与人工形象验收，通过后才 cp 到 source/K0.png"
}

stage_verify_k0() {
  need source/K0.png
  echo "== K0 硬门 =="
  $PY tools/verify_keyframe.py source/K0.png
  $PY - <<'PYEOF'
from PIL import Image
im = Image.open('source/K0.png').convert('RGB')
print('通道模式', im.mode, '（白底烘焙路线不需要 alpha）')
PYEOF
}

# ── 4. Pilot：seg-01-charge ────────────────────────────────────────────
stage_pilot() {
  need source/K0.png
  need source/prompt-seg-01-charge.txt
  echo "== 生成 seg-01-charge（首帧 = K0，机位锁定，97 帧 @24fps）=="
  $PROFILE run default -- $PY tools/agnes_job.py video \
    --prompt-file source/prompt-seg-01-charge.txt \
    --negative-prompt "$(cat source/prompt-seg-01-charge-negative.txt)" \
    --mode keyframe --first-frame source/K0.png \
    --seconds 4 --width 960 --height 960 --num-frames 97 --frame-rate 24 \
    --output source/seg-01-charge.mp4 \
    --metadata source/seg-01-charge.job.json \
    --force
  # 尾帧：提供给 seg-02 作为首帧，同时是帧链接力的证据
  ffmpeg -y -hide_banner -loglevel error -sseof -0.05 -i source/seg-01-charge.mp4 \
    -frames:v 1 -update 1 qa/seg-01-tail.png
  echo "→ source/seg-01-charge.mp4 + qa/seg-01-tail.png"
  echo "→ 下一步：bash motion.sh verify-seg source/seg-01-charge.mp4"
}

stage_verify_seg() {
  local f="${1:-source/seg-01-charge.mp4}"
  need "$f"
  echo "== 单段硬门：$f =="
  $PY tools/verify_segment.py "$f" \
    --expect-feet "$EXPECT_FEET" --min-width-px "$MIN_WIDTH_PX" --expect-compress \
    --report "qa/$(basename "${f%.mp4}")-verify.json"
  echo "→ 自动项通过后仍须人工看接触表（qa/*-contact.png）"
}

stage_approve() {
  need source/seg-01-charge.mp4
  need qa/seg-01-tail.png
  need qa/pilot-page.md
  echo "== Pilot 硬门（记录工件 SHA-256）——决策必须来自用户 =="
  $PY "$OIL_MOTION/scripts/production_gate.py" approve-pilot \
    --contract source/concept-contract.yaml \
    --identity-bible source/identity-bible.md \
    --first-frame source/K0.png \
    --last-frame qa/seg-01-tail.png \
    --video source/seg-01-charge.mp4 \
    --page-evidence qa/pilot-page.md \
    --reviewer "${REVIEWER:-user}" \
    --decision "${DECISION:-pass}" \
    --output pilot/approval.json \
    --force
  echo "→ pilot/approval.json（未签收前不许跑 segs）"
}

# ── 5. 量产：seg-02-arc / seg-03-rise ──────────────────────────────────
gen_segment() {  # $1=段号 2|3  $2=id  $3=首帧
  local idx="$1" id="$2" first="$3"
  local prompt="source/prompt-${id}.txt"
  [ -f "$prompt" ] || { echo "缺少提示词 $prompt：先按照 source/prompt-seg-01-charge.txt 的结构写它（含逐帧的双臂留白约束）" >&2; exit 1; }
  $PROFILE run default -- $PY tools/agnes_job.py video \
    --prompt-file "$prompt" \
    --negative-prompt "$(cat source/prompt-seg-01-charge-negative.txt)" \
    --mode keyframe --first-frame "$first" \
    --seconds 4 --width 960 --height 960 --num-frames 97 --frame-rate 24 \
    --output "source/${id}.mp4" \
    --metadata "source/${id}.job.json" \
    --force
  ffmpeg -y -hide_banner -loglevel error -sseof -0.05 -i "source/${id}.mp4" \
    -frames:v 1 -update 1 "qa/${id}-tail.png"
  echo "→ source/${id}.mp4 + qa/${id}-tail.png"
}

stage_segs() {
  need pilot/approval.json
  need qa/seg-01-tail.png
  [ -f qa/frame-chain.json ] || echo '{"schemaVersion":1,"segments":[]}' > qa/frame-chain.json
  echo "== seg-02-arc =="
  gen_segment 2 seg-02-arc qa/seg-01-tail.png
  stage_verify_seg source/seg-02-arc.mp4
  echo "== seg-03-rise =="
  gen_segment 3 seg-03-rise qa/seg-02-arc-tail.png
  stage_verify_seg source/seg-03-rise.mp4
  echo "→ 跑 bash motion.sh chain"
}

stage_chain() {
  need source/seg-02-arc.mp4
  echo "== 硬门 1：生成输入接力（seg-N 的尾帧 == seg-N+1 的首帧）=="
  $PY "$OIL_MOTION/scripts/production_gate.py" verify-chain \
    --previous-tail qa/seg-01-tail.png --next-first qa/seg-01-tail.png \
    --segment-index 2 --manifest qa/frame-chain.json
  echo "== 硬门 2：成片输出接缝（解码后的真实边界）=="
  $PY "$OIL_MOTION/scripts/production_gate.py" verify-output-chain \
    --previous-video source/seg-01-charge.mp4 --next-video source/seg-02-arc.mp4 \
    --segment-index 2 --manifest qa/frame-chain.json \
    --evidence-dir qa/frame-chain-evidence
  if [ -f source/seg-03-rise.mp4 ]; then
    $PY "$OIL_MOTION/scripts/production_gate.py" verify-chain \
      --previous-tail qa/seg-02-arc-tail.png --next-first qa/seg-02-arc-tail.png \
      --segment-index 3 --manifest qa/frame-chain.json
    $PY "$OIL_MOTION/scripts/production_gate.py" verify-output-chain \
      --previous-video source/seg-02-arc.mp4 --next-video source/seg-03-rise.mp4 \
      --segment-index 3 --manifest qa/frame-chain.json \
      --evidence-dir qa/frame-chain-evidence
  fi
  echo "→ 自动相似度通过后仍须人工看 qa/frame-chain-evidence/ 的证据帧"
}

# ── 6. 合并与编译 ──────────────────────────────────────────────────────
stage_compile() {
  local files=() ids=(K1 K2 K3) names=(seg-01-charge seg-02-arc seg-03-rise)
  for n in "${names[@]}"; do [ -f "source/$n.mp4" ] && files+=("source/$n.mp4"); done
  [ ${#files[@]} -ge 1 ] || { echo "source/ 里没有可编译的片段" >&2; exit 1; }

  echo "== 合并（运行时只用一个持续存在的媒体实例）=="
  : > source/concat.txt
  for f in "${files[@]}"; do echo "file '$ROOT/$f'" >> source/concat.txt; done
  ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i source/concat.txt \
    -c copy source/master.mp4

  # 每段的真实帧数由 ffprobe 数出来，不手工推算；timeline 由编译结果生成
  $PY - "${files[@]}" <<'PYEOF' > source/segments.json
import json, subprocess, sys
files = sys.argv[1:]
frames = []
for f in files:
    out = subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-count_frames',
                          '-show_entries','stream=nb_read_frames,r_frame_rate',
                          '-of','json', f], capture_output=True, text=True, check=True)
    s = json.loads(out.stdout)['streams'][0]
    frames.append(int(s['nb_read_frames']))
    fps = s['r_frame_rate']
print(json.dumps({'fps': fps, 'files': files, 'frames': frames}, ensure_ascii=False, indent=2))
PYEOF
  cat source/segments.json

  local segargs
  segargs="$($PY - "${ids[@]}" <<'PYEOF'
import json, sys
d = json.load(open('source/segments.json'))
ids = sys.argv[1:]
start = 0
for i, n in enumerate(d['frames']):
    print(f"--segment {ids[i]}={start}:{start+n-1}:{start+n}")
    start += n
PYEOF
)"
  echo "$segargs"

  echo "== 编译 + 生成 build/timeline.json =="
  # shellcheck disable=SC2086
  $PY "$OIL_MOTION/scripts/compile_scroll_video.py" \
    source/master.mp4 build/media \
    --background-owner video \
    --budget-report build/motion-budget.json \
    --frame-policy native \
    --fps 24 \
    --timeline-output build/timeline.json \
    --initial-state-id K0 \
    --poster-source-frame 0 \
    --playback-curve edge-mid-edge --edge-rate 1.2 --mid-rate 0.85 \
    --force $segargs
  $PY -c "import json;d=json.load(open('build/timeline.json'));print('states',[s['id'] for s in d['states']]);print('segments',[(s['id'],s['start'],s['hold']) for s in d['segments']])"
  echo "→ segment-playback 不需要全关键帧，跑重编码："
  echo "  ffmpeg -y -i build/media/final/motion-baked-desktop.mp4 -c:v libx264 -g 48 -keyint_min 24 \\"
  echo "    -sc_threshold 0 -crf 21 -preset slow -pix_fmt yuv420p -movflags +faststart \\"
  echo "    -map_metadata -1 build/media/final/motion-baked.mp4"
}

# ── 7. 页面几何探针 ────────────────────────────────────────────────────
stage_probe() {
  need qa/layout-probe.html
  echo "== 页面几何：anchorA/anchorB 底边是否落在地平线上 =="
  # 静态服务器：探针页要用 fetch 读 build/timeline.json，file:// 下会被挡。
  # 用 trap 保证无论后面哪一步失败都不会留下野进程（上一次就留过一个 12:11 的）。
  $PY -m http.server 8765 >/dev/null 2>&1 &
  local srv=$!
  trap 'kill '"$srv"' 2>/dev/null || true' RETURN
  sleep 1.5
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --virtual-time-budget=6000 \
    --window-size=1600,900 --dump-dom http://localhost:8765/qa/layout-probe.html 2>/dev/null \
    | $PY -c "
import sys,re,html
m=re.search(r'<pre id=\"out\">(.*?)</pre>', sys.stdin.read(), re.S)
print(html.unescape(m.group(1)) if m else '未取到输出（探针页没跑起来？）')"
}

case "${1:-}" in
  key-status)  stage_key_status ;;
  key-setup)   stage_key_setup ;;
  budget)      stage_budget ;;
  k0-recover)  stage_k0_recover ;;
  k0-generate) stage_k0_generate ;;
  verify-k0)   stage_verify_k0 ;;
  pilot)       stage_pilot ;;
  verify-seg)  shift; stage_verify_seg "${1:-source/seg-01-charge.mp4}" ;;
  approve)     stage_approve ;;
  segs)        stage_segs ;;
  chain)       stage_chain ;;
  compile)     stage_compile ;;
  probe)       stage_probe ;;
  *) sed -n '2,32p' "$0"; exit 1 ;;
esac
