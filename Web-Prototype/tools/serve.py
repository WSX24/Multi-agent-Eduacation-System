#!/usr/bin/env python
# ═══════════════════════════════════════════════════════════════════════
# serve.py — 带 HTTP Range 支持的静态服务器（QA 验收用）
#
# 为什么不能用 python -m http.server：
#   SimpleHTTPRequestHandler 忽略 Range 请求头，总是返回 200 + 整个文件。
#   浏览器要 seek 视频必须先确认服务器支持按字节范围取数据；拿不到 Range
#   就认为媒体不可寻址。实测后果：
#
#       <video src="...">  loadedmetadata 后
#       duration = 9.083   readyState = 4   seekable = 1 [0.00..0.00]
#       video.currentTime = 4.0   →   seeked 事件触发，currentTime 仍是 0
#
#   于是「scrub 不生效」这个现象看起来像运行时的 bug，其实是测试服务器
#   缺了 Range。生产静态托管（Nginx / S3 / Vercel / GitHub Pages）都支持，
#   所以这是验收工具的问题，不是交付物的问题。
#
# 用法：
#   python tools/serve.py 8807            # 在项目根目录起服务
#   http://localhost:8807/landing.html?p=0.62
# ═══════════════════════════════════════════════════════════════════════
import argparse
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)')


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        rng = self.headers.get('Range')
        if not rng:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, 'File not found')
            return None

        size = os.fstat(f.fileno()).st_size
        m = RANGE_RE.match(rng.strip())
        if not m:
            f.close()
            self.send_error(400, 'Bad Range')
            return None
        start_s, end_s = m.group(1), m.group(2)
        if start_s == '':                       # bytes=-N  最后 N 字节
            length = int(end_s or 0)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        if start >= size or start > end:
            f.close()
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.end_headers()
            return None
        end = min(end, size - 1)
        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Last-Modified', self.date_time_string(os.path.getmtime(path)))
        self.end_headers()
        f.seek(start)
        self._range_remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        # 只写请求的那一段
        remaining = getattr(self, '_range_remaining', None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def end_headers(self):
        # 让浏览器知道这个服务器支持范围请求
        if self.command in ('GET', 'HEAD') and not self._headers_buffer_has('Accept-Ranges'):
            self.send_header('Accept-Ranges', 'bytes')
        # 开发/验收服务器不该让浏览器缓存：改了 CSS 却看到旧样式，
        # 会让人以为是代码没生效（本项目就踩过一次）。
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def _headers_buffer_has(self, name: str) -> bool:
        for raw in getattr(self, '_headers_buffer', []):
            if raw.lower().startswith(name.lower().encode() + b':'):
                return True
        return False

    def log_message(self, fmt, *a):
        pass                                 # 静默，避免刷屏


def main() -> int:
    ap = argparse.ArgumentParser(description='带 Range 的静态服务器')
    ap.add_argument('port', nargs='?', type=int, default=8807)
    ap.add_argument('--root', default='.')
    a = ap.parse_args()
    root = Path(a.root).resolve()
    handler = partial(RangeHandler, directory=str(root))
    with ThreadingHTTPServer(('127.0.0.1', a.port), handler) as httpd:
        print(f'服务 {root}  →  http://localhost:{a.port}/  （支持 Range，视频可 seek）')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
