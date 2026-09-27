# 交接包：书架之光 / 一本书的光 — 提示词与操作步骤

给你自己生成用。所有提示词都可以直接粘贴。

---

## 0. 先说三条已经验证过的结论（照做能少走很多弯路）

1. **必须用「首帧 + 尾帧」双锚点。** 只给首帧时，模型对终点没有任何约束。
   实测同一提示词、同一首帧：

   | | 紫色重心 x（核心在 85%） | 紫色面积 |
   | --- | --- | --- |
   | 只给首帧 | 72.6% → **56.7%**（漂离核心） | 涨到 **9.26%**（堆成紫云） |
   | 首帧 + 尾帧 | 70.8% → **75.5%**（守在核心一侧） | 始终 ≤3.46% |

2. **题材的信息密度必须低。** 我在「一排书脊」上失败了两次：
   480p + 小模型 5 秒内把书脊糊成一团色块（结构相关从 0.6 掉到 0.095）。
   换成「一本摊开的书」（几块大形状）之后才守住。**这套提示词不要再往画面里加细节。**

3. **免费档的视频带水印、上限 480p。** 图片不受影响（免费档也无水印）。
   要交付全屏 16:9 Hero，至少要 1080p 级母版 —— 所以正式出片需要付费档。

---

## 1. 素材（都在项目里，可直接上传）

| 文件 | 尺寸 | 用途 |
| --- | --- | --- |
| `source/K0-book.png` | 640×368 | **seg-01 的首帧**。免费档 640px 生成，已验收：均亮 234、亮纸 92.6%、过曝 0% |
| `qa/seg-01-book-tail.png` | 844×486 | **seg-01 的尾帧**，同时是 **seg-02 的首帧**。这是真实成片里取出的中间态 |
| `source/K3-core-lit.png` | 640×368 | **seg-02 的尾帧**。由图片编辑器在原图上只改光做出来的，已验证：紫云消失、核心亮起 |

> 三个文件都在 `C:\Users\21495\Downloads\Web-Prototype\` 下。
> 图片输出**没有水印**，所以这三张可以直接用。

**如果你要更高分辨率**：先在付费档用第 2 节的图片提示词把 K0 重新生成到 2k，
再用它替换三张图（尾帧也要用同一张新图重新编辑），否则首帧 640px
放到 1080p 视频里会发虚。

---

## 2. 图片提示词（生成首帧 K0）

**设置**：AI Image Generator · 模型 `flux-schnell`（或付费档用 `seedream-v4` / `nano-banana-2`）·
分辨率 `2k`（免费档只能 `640px`）· 比例 `16:9` · 张数 1

```
A wide, calm, very simple illustration in 16:9. Large clean shapes, minimal detail, bright and airy.

A single large open book lies flat on a plain pale surface, positioned left of centre, filling about a third of the frame. The book is seen from a low, gentle angle. Its two open pages are bright off-white, with a soft fold down the middle and a few thin grey lines of unreadable writing. Nothing else is on the surface.

To the right, further away, a small dark faceted solid floats above a low pale block: a rounded polyhedron, matte dark slate blue-grey, the size of a fist. It is unlit and quiet, with no glow, no light rays and no particles around it.

The background is a plain, near-white, softly graded surface with no room, no furniture, no shelves, no windows and no clutter. Nothing is drawn on it.

One soft even daylight from the upper left. Very bright and high-key: at least half of the picture is light, above 200 in brightness. Pale warm off-white, cream and light grey only, plus the single dark slate object. Almost no saturated colour anywhere.

Modern animated illustration, soft matte shading, generous empty space, quiet and orderly.

No text, no letters, no numbers, no watermark, no border, no UI. No bookshelf, no rows of books, no spines, no clutter, no extra objects, no second book, no hands, no people. No glow, no light rays, no beams, no particles, no sparkles, no dust in this frame. No neon, no cyberpunk, no circuitry, no screens, no sci-fi machinery. No dark shadows, no vignette, no brown, no dark wood, no moody lighting, no busy detail.
```

---

## 3. 视频提示词 · seg-01（光从书里升起）

**设置**：Image to Video · **First frame** = `source/K0-book.png` ·
**Last frame / End frame** = `qa/seg-01-book-tail.png` ·
模型 `kling-3.0`（付费档；免费档只有 `ltx-2.3`）· 分辨率 `1080p` · 时长 `5` 秒 · 音频关

```
One continuous shot, starting exactly from the supplied first frame.

A large open book lies left of centre on a bright plain surface. A small dark faceted object floats in the upper right. The whole scene is very bright, pale and simple, with a soft even light.

Over the shot, soft glowing light rises out of the open book. It starts as a faint shimmer on the pages and grows into a gentle plume of pale violet-white light that lifts off the book and drifts slowly to the right, toward the dark object. The light stays soft and diffuse, like warm mist catching sunlight — never a hard beam, never a laser, never thin sharp lines.

The book, the surface and the dark object stay exactly where they are and keep their exact shape. The dark object stays dark and unlit. The camera stays almost still with a very slow gentle push forward, holding the same framing.

No cuts, no camera shake, no reframing, no zoom pumping. No text, no letters, no numbers, no watermark. No shelf, no extra books, no extra objects, no hands, no people. No hard beams, no lasers, no thin bright lines, no lens flare, no particles, no sparkles, no dust, no smoke. No neon, no cyberpunk, no circuitry. No darkening, no colour shift, no vignette, no brown, no warm colour cast. No blur, no ghosting, no flicker.
```

**做完之后**：取成片的**最后一帧**，它就是 seg-02 的首帧。
（如果你们的界面不提供取尾帧，就直接用我给的 `qa/seg-01-book-tail.png` —— 我这一版
seg-01 的尾帧和它基本一致。）

---

## 4. 视频提示词 · seg-02（光汇入核心、核心亮起）

**设置**：Image to Video · **First frame** = 上一段 seg-01 的尾帧 ·
**Last frame / End frame** = `source/K3-core-lit.png` ·
模型 `kling-3.0` · 分辨率 `1080p` · 时长 `5` 秒 · 音频关

```
One continuous shot, starting exactly from the supplied first frame.

The scene is a bright pale surface with a soft open book and, further right, a small dark faceted object floating in the air. Overcast soft light, very bright and simple.

Over the shot, the soft pale light that is drifting across the scene gathers itself together and flows into the dark faceted object. The light stream narrows and streams into the object's left side, and as it arrives the dark object wakes up: its flat facets light up from within with a soft violet-white glow, its middle seam becomes a thin bright line, and a modest halo sits at its surface. The glow stays contained — it does not flood the room and does not brighten the walls.

The book and the surface stay exactly where they are and keep their shape. The camera stays almost still with a very slow gentle push forward, holding the same framing.

No cuts, no camera shake, no reframing, no zoom pumping. No text, no letters, no numbers, no watermark. No shelf, no extra books, no extra objects, no hands, no people. No hard beams, no lasers, no lens flare, no particles, no sparkles, no dust, no smoke. No neon, no cyberpunk. No darkening of the room, no colour shift, no vignette. No blur, no ghosting, no flicker.
```

---

## 5. 尾帧提示词（如果你要自己重做 `K3-core-lit.png`）

**设置**：AI Image Editor · 模型 `qwen-edit`（免费档可用）· 输入图 = seg-01 的尾帧 ·
分辨率 `2k` · 比例 `16:9`

```
Edit this image. Keep everything exactly the same: the same bright pale surface, the same open book in the same place, the same floating dark faceted object in the same position and size, the same soft even lighting and the same overall brightness.

Change only the light. The diffuse violet-white cloud of light that is spread around the middle of the image is gone — remove it completely, leaving the clean bright pale surface behind it. Instead, all of that light has now been absorbed into the floating dark faceted object: the object glows softly from within with pale violet-white light, its flat facets are lit, and its middle seam is a thin bright line. The glow hugs the object closely and fades out within a short distance, so the surrounding surface stays clean and bright.

The book keeps its exact shape and stays unlit. The surface stays plain and bright.

No diffuse light cloud, no fog, no haze, no particles, no sparkles, no hard beams, no lasers, no lens flare. No text, no letters, no numbers, no watermark. No extra objects, no hands, no people. No darkening, no vignette, no colour cast.
```

---

## 6. 成片后怎么自己验收（三条，都是可量的）

1. **接缝**：seg-01 的尾帧与 seg-02 的首帧应该是同一张图 ——
   平均差 <3、差超过 40 的像素占比 0%。我这边实测 2.5 / 0.00%。
2. **光的方向**：逐秒量紫色像素的水平重心。核心在约 85% 宽，
   重心应当**朝 85% 移动或停在 70–80%**，**不能一路向左漂**（那就是失败版）。
3. **紫色面积**：任何一帧不超过画面的 8%。超过就是堆成紫云了。

工具都在项目里，可以直接跑：

```bash
python tools/verify_scene.py <文件> --fps 2 --no-axis     # 色调与面积门限
python tools/magichour_job.py status --kind video --id <project_id>
```

---

## 7. 两点交付前必须处理的

| 项 | 说明 |
| --- | --- |
| **水印** | 免费档视频自带。付费订阅或买 credit pack 才移除；官方支持**无需重新生成**直接给已有视频去水印 |
| **分辨率** | 全屏 16:9 Hero 需要母版 ≥ 显示尺寸的 2 倍（1920 宽要 1080p 级）。480p 铺满必然发虚 |

## 8. 提示词不要在哪些地方加料（我踩过的坑）

- **不要加回书架**。一排书脊是细密高频结构，低分辨率模型守不住，5 秒内就糊。
- **不要写"一道道细如发丝的光"**。那是同一个问题：细结构会消失或被糊成一团。
  要写"一团柔和的光 / 一缕光带"。
- **不要在负面里堆几十个词**。实测负面词越长，小模型越容易整体漂移；
  上面这几条已经够用。
- **不要用文生图生成中间关键帧**。实测四张独立生成的"关键帧"之间结构相关只有
  0.05（等于无关）—— 它们不是一条链。中间态只能来自**视频的实际尾帧**。
