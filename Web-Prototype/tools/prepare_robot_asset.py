#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# prepare_robot_asset.py — 把一张截图里的机器人抠成透明 PNG，供 auth.html 用
#
# 源图（用户 2026-09-27 指定，在项目外，所以路径写死在这里留档）：
#   C:\Users\21495\Pictures\Screenshots\屏幕截图 2026-09-27 141253.png
#   436×605 / RGBA / alpha 全 255（截图，不是透明图）/ 底色近白
#
# 产出：assets/robot-auth.png — 四周等距裁齐的透明 PNG，
#       于是页面只要用 <img> 摆位就行，不必再靠 background-position 凑。
#
# 为什么不用 mix-blend-mode:
#   .auth-side 的底色是 --surface #f7f7f8（不是纯白），乘上去会在
#   #f7f7f8 上留下一块约 2% 的灰矩形；而且图像里凡是比底色亮的像素也会被压暗。
#   直接抠掉背景更干净，也换得动位置。
#
# 抠法（先量再切，不拍脑袋定阈值）：
#   边框 6px 带实测 min=250 max=255 均值 252.6 标准差 0.61 —— 底色极平；
#   而机器人内部最亮的像素只到 249。250 这条线几乎不碰主体（只剩 23px 同亮度），
#   所以阈值取 250，并且**只删与边界连通的那些**，主体内部的浅色一律保留。
#
#   python tools/prepare_robot_asset.py            # 生成 + 打印验收数据
#   python tools/prepare_robot_asset.py --check    # 只看数据，不写文件
# ═══════════════════════════════════════════════════════════════════════
import argparse
import colorsys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

SRC = Path(r'C:\Users\21495\Pictures\Screenshots\屏幕截图 2026-09-27 141253.png')
OUT = Path('assets/robot-auth.png')
TH = 250.0        # 底色阈值：>= 它且与边界连通 → 背景
RAMP = 7.0        # 边缘过渡带宽度（亮度）：250→243 之间按比例给 alpha
BAND = 2          # 内侧边界带宽度（像素）：只在这条带里按亮度做半透明
FEATHER = 0.7     # 最后整体羽化半径


def background_mask(lum: np.ndarray, th: float) -> np.ndarray:
    """从四条边洪泛：只有与边界连通的亮像素才算背景。
    这样机器人身上的浅色（visor 那块 246–249）不会被误删。"""
    H, W = lum.shape
    cand = lum >= th
    seen = np.zeros_like(cand)
    dq = deque()
    for x in range(W):
        for y in (0, H - 1):
            if cand[y, x] and not seen[y, x]:
                seen[y, x] = True
                dq.append((y, x))
    for y in range(H):
        for x in (0, W - 1):
            if cand[y, x] and not seen[y, x]:
                seen[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and cand[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                dq.append((ny, nx))
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()

    src = Image.open(SRC).convert('RGB')
    rgb = np.asarray(src, dtype=np.float32)
    lum = rgb.mean(axis=2)

    bg = background_mask(lum, TH)
    # 内侧边界带：把背景膨胀 BAND 像素再减掉背景本身
    dil = np.asarray(Image.fromarray((bg * 255).astype(np.uint8))
                     .filter(ImageFilter.MaxFilter(2 * BAND + 1)), dtype=np.uint8) > 127
    band = dil & ~bg

    alpha = np.where(bg, 0.0, 255.0)
    ramp = np.clip((TH - lum) / RAMP, 0, 1) * 255.0     # 越亮越透明，只在边界带里用
    alpha = np.where(band, ramp, alpha)
    alpha = np.asarray(Image.fromarray(alpha.astype(np.uint8))
                       .filter(ImageFilter.GaussianBlur(FEATHER)), dtype=np.float32)

    h, w = lum.shape
    # 四边各留同样多的余量。**不要**去凑正方形：原图 42/49 与 15/32 的余量本就不等，
    # 凑方会把宽的那边撑到贴边、机器人反而偏了。直接按内容外接框 + 等距内边距裁，
    # 于是 img 的比例就等于机器人本身的比例，摆位时可预期。
    ys, xs = np.nonzero(alpha > 8)
    pad = 6
    x0, x1 = max(0, int(xs.min()) - pad), min(w, int(xs.max()) + 1 + pad)
    y0, y1 = max(0, int(ys.min()) - pad), min(h, int(ys.max()) + 1 + pad)

    out = np.dstack([rgb, alpha]).astype(np.uint8)
    img = Image.fromarray(out, 'RGBA').crop((x0, y0, x1, y1))

    # ── 验收数据 ──────────────────────────────────────────────────────
    A = np.asarray(img)[:, :, 3]
    print(f'源图    {w}×{h}')
    print(f'裁切    x {x0}–{x1}  y {y0}–{y1}   →  {img.size[0]}×{img.size[1]}  '
          f'(比例 {img.size[0]/img.size[1]:.3f})')
    print(f'alpha   全透明 {int((A == 0).sum()):6d}px ({A.size and (A==0).mean()*100:4.1f}%)   '
          f'全不透明 {int((A == 255).sum()):6d}px ({ (A==255).mean()*100:4.1f}%)   '
          f'半透明(过渡边) {int(((A > 0) & (A < 255)).sum()):5d}px')
    print(f'        四边贴边像素 alpha: 上{int(A[0].max())} 下{int(A[-1].max())} '
          f'左{int(A[:,0].max())} 右{int(A[:,-1].max())}   （都该是 0，否则还有残留底）')
    # 合成到底色上，量一下还有没有残留的亮矩形
    base = np.zeros((img.size[1], img.size[0], 3), dtype=np.float32)
    base[:, :] = (247, 247, 248)                        # --surface
    al = (A.astype(np.float32) / 255)[:, :, None]
    comp = np.asarray(img.convert('RGB'), dtype=np.float32) * al + base * (1 - al)
    diff = np.abs(comp - base).max(axis=2)
    print(f'合到 #f7f7f8 上：与底色差 ≤2 的像素占 {float((diff <= 2).mean() * 100):.1f}%  '
          f'（原图此处应为 100%，即背景完全消失）')
    row = comp[2].mean(axis=0)
    print(f'  合成图第 2 行均值 {row.round(1)}  （应等于 247,247,248）')

    if not a.check:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        img.save(OUT, optimize=True)
        print(f'\n已写出 {OUT}  {OUT.stat().st_size / 1024:.0f} KB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
