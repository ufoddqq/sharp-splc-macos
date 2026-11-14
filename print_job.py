#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
print_job.py —— SHARP AR-2221R 的 CUPS 后端主程序（同时可当命令行工具用）
================================================================================

两种用法
--------
1) CUPS 后端（由 /usr/libexec/cups/backend/splcprint 调用）

       print_job.py --backend <uri> <job-id> <user> <title> <copies> <options> <file>

2) 命令行

       print_job.py 文件.pdf --send 192.168.1.50
       print_job.py 文件.pdf --dry-run --out /tmp/x.spl

几何与缩放
----------
设备可用画布 4696 x 6382 px @600dpi = 198.8 x 270.2 mm，比 A4 小一圈。
`fit=auto`（默认）先看内容墨迹范围，能 1:1 放下就 1:1（只裁白边），放不下才等比缩小。
`fit=fit` 永远缩放适配；`fit=exact` 强制 1:1（超出画布的部分会被裁掉）。

灰度转黑白
----------
`tone=text`（默认）硬阈值，笔画实心，适合文字/表格/线条；
`tone=photo` Floyd-Steinberg 抖动，适合照片。
阈值由 `thresh=` 控制（1-254），调低字更细，调高字更粗。

CUPS 选项示例
-------------
    lp -d SHARP_AR2221R -o fit=exact -o tone=photo 文件.pdf
"""
from __future__ import annotations

import argparse
import io
import os
import socket
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

import splc

DPI = 600
A4_W_MM, A4_H_MM = 210.0, 297.0
A4_W_PX = round(A4_W_MM / 25.4 * DPI)      # 4961
A4_H_PX = round(A4_H_MM / 25.4 * DPI)      # 7016

# 判定"有内容"的灰度阈值：低于它的像素算墨迹（纸白是 255）
INK_CUT = 245


def log(msg: str) -> None:
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    sys.stderr.write(line)
    sys.stderr.flush()
    for p in ("/var/log/splcprint.log", "/tmp/splcprint.log"):
        try:
            with open(p, "a") as f:
                f.write(line)
            return
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# 载入与摆放
# --------------------------------------------------------------------------- #
def load_pages(src) -> list:
    """PDF（路径或字节）→ 每页 600dpi 灰度图；图片 → 单页。"""
    import pymupdf
    data = src if isinstance(src, (bytes, bytearray)) else None
    if data is not None and data[:5] == b"%PDF-":
        doc = pymupdf.open(stream=bytes(data), filetype="pdf")
    elif data is not None:
        img = Image.open(io.BytesIO(bytes(data)))
        img = ImageOps.exif_transpose(img).convert("L")
        return [img]
    else:
        path = Path(src)
        if path.suffix.lower() == ".pdf":
            doc = pymupdf.open(str(path))
        else:
            img = Image.open(str(path))
            img = ImageOps.exif_transpose(img).convert("L")
            return [img]
    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY, alpha=False)
        out.append(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    doc.close()
    return out


def _ink_bbox(img: Image.Image):
    """返回内容墨迹的 (x0, y0, x1, y1)；整页空白返回 None。"""
    # getbbox() 找的是"非零"区域，故把墨迹置 255、纸白置 0
    mask = img.point([255 if v < INK_CUT else 0 for v in range(256)])
    return mask.getbbox()


def place(img: Image.Image, fit: str = "auto") -> tuple:
    """把灰度页摆进设备画布，返回 (canvas, 说明dict)。"""
    canvas = Image.new("L", (splc.PAGE_W, splc.PAGE_H), 255)
    w, h = img.size

    # 1:1 居中时，整页左上角在画布坐标系里的位置（负值表示被裁）
    cx = (splc.PAGE_W - w) // 2
    cy = (splc.PAGE_H - h) // 2

    scale, note = 1.0, "1:1"
    if fit == "fit":
        if w > splc.PAGE_W or h > splc.PAGE_H:
            scale = min(splc.PAGE_W / w, splc.PAGE_H / h)
    elif fit == "auto":
        if w > splc.PAGE_W or h > splc.PAGE_H:
            bb = _ink_bbox(img)
            if bb is None:
                note = "1:1（空白页）"
            else:
                x0, y0, x1, y1 = bb
                # 内容是否完整落在画布窗口内
                if x0 >= -cx and y0 >= -cy and x1 <= -cx + splc.PAGE_W and y1 <= -cy + splc.PAGE_H:
                    note = "1:1（内容未越界，只裁白边）"
                else:
                    scale = min(splc.PAGE_W / w, splc.PAGE_H / h)
                    note = "缩放 %.1f%%（内容超出画布）" % (scale * 100)
    # fit == "exact" 时 scale 保持 1.0

    if scale != 1.0:
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
        img = img.resize((w, h), Image.LANCZOS)
        cx = (splc.PAGE_W - w) // 2
        cy = (splc.PAGE_H - h) // 2

    canvas.paste(img, (cx, cy))      # 负坐标由 PIL 自动裁切
    return canvas, {"scale": scale, "note": note, "src": img.size}


def binarize(img: Image.Image, tone: str = "text", thresh: int = 200) -> Image.Image:
    """灰度 → 1 位。text=硬阈值（笔画实心），photo=抖动（保留层次）。"""
    if tone == "photo":
        return img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    thresh = max(1, min(254, int(thresh)))
    lut = [255 if v >= thresh else 0 for v in range(256)]
    return img.point(lut).convert("1", dither=Image.Dither.NONE)


_INVERT = bytes(255 ^ i for i in range(256))


def to_rows(bw: Image.Image) -> list:
    """1 位图 → 每行字节（MSB 最左，1 = 打点）。

    逐字节用 bytes.translate 取反（C 速度），比 Python 里 `255 ^ b for b in ...` 快一个量级。
    """
    assert bw.mode == "1"
    raw = bw.tobytes()
    stride = (bw.width + 7) // 8
    return [raw[y * stride:(y + 1) * stride].translate(_INVERT)
            for y in range(bw.height)]


# --------------------------------------------------------------------------- #
# 组装与发送
# --------------------------------------------------------------------------- #
def build(src, fit="auto", tone="text", thresh=200, copies=1, duplex=False,
          quiet=False) -> bytes:
    pages = load_pages(src)
    if not pages:
        raise SystemExit("没有可打印的页")
    rows_all = []
    for i, im in enumerate(pages, 1):
        canvas, info = place(im, fit)
        bw = binarize(canvas, tone, thresh)
        # 墨覆盖率：1 位图里 0 = 黑，直方图取 0 号桶（比逐字节 bin().count 快百倍）
        ink = bw.histogram()[0] / float(bw.width * bw.height)
        rows_all.append(to_rows(bw))
        if not quiet:
            log("  第 %d 页  %s  墨覆盖 %.2f%%" % (i, info["note"], ink * 100))
    return splc.make_job(rows_all, copies=copies, duplex=duplex)


def send(data: bytes, host: str, port: int = 9100, timeout: int = 60) -> float:
    t0 = time.time()
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        s.settimeout(10)
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass
    return time.time() - t0


def _parse_opts(opts: str) -> dict:
    out = {}
    for tok in (opts or "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.lower()] = v
    return out


def _device_host(uri: str) -> tuple:
    """splcprint://192.168.1.50:9100 -> ('192.168.1.50', 9100)

    Falls back to $SPLC_HOST. Returning an empty host is intentional: the caller
    reports a readable error instead of silently talking to somebody else's printer.
    """
    s = uri.split("://", 1)[-1].strip("/")
    host, _, port = s.partition(":")
    return host or os.environ.get("SPLC_HOST", ""), int(port or 9100)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run_backend(job_id, user, title, copies, opts, filename) -> int:
    o = _parse_opts(opts)
    fit = o.get("fit", os.environ.get("SPLC_FIT", "auto"))
    tone = o.get("tone", os.environ.get("SPLC_TONE", "text"))
    thresh = int(o.get("thresh", os.environ.get("SPLC_THRESH", 200)))
    try:
        copies = max(1, min(99, int(o.get("copies", copies or 1))))
    except ValueError:
        copies = 1

    data = b""
    if filename and os.path.isfile(filename):
        with open(filename, "rb") as f:
            data = f.read()
    if not data:
        data = sys.stdin.buffer.read()
    if not data:
        log("[失败] 作业 #%s 数据为空" % job_id)
        return 1

    log("[开始] 作业 #%s 用户=%s 标题=%r 份数=%d 大小=%d 字节 fit=%s tone=%s thresh=%d"
        % (job_id, user, title, copies, len(data), fit, tone, thresh))

    try:
        t0 = time.time()
        spl = build(data, fit=fit, tone=tone, thresh=thresh, copies=copies)
    except Exception as e:                                  # noqa: BLE001
        log("[失败] 渲染出错：%s: %s" % (type(e).__name__, e))
        return 1
    log("[编码] %d 字节（%.1f KB），耗时 %.1f 秒"
        % (len(spl), len(spl) / 1024, time.time() - t0))

    host, port = _device_host(os.environ.get("DEVICE_URI", ""))
    if not host:
        log("[失败] 没有打印机地址：设置 SPLC_HOST 或使用 splcprint://<ip> 设备 URI")
        return 1
    for attempt in range(1, 3):
        try:
            t1 = time.time()
            send(spl, host, port)
            log("[成功] 作业 #%s 已送达 %s:%d（%.2f 秒）" % (job_id, host, port, time.time() - t1))
            return 0
        except Exception as e:                              # noqa: BLE001
            log("[重试 %d/2] 发送失败：%s: %s" % (attempt, type(e).__name__, e))
            time.sleep(2)
    log("[失败] 作业 #%s 无法连接 %s:%d —— 打印机是否开机？" % (job_id, host, port))
    return 1


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--backend":
        a = sys.argv[2:]
        if not a:                                           # 设备发现
            print('direct splcprint:/ "SHARP AR-2221R (SPLC)" "Sharp AR-2221R direct"')
            return 0
        uri = a[0] if len(a) > 0 else ""
        os.environ.setdefault("DEVICE_URI", uri)
        job_id = a[1] if len(a) > 1 else "0"
        user = a[2] if len(a) > 2 else "unknown"
        title = a[3] if len(a) > 3 else "job"
        copies = a[4] if len(a) > 4 else "1"
        opts = a[5] if len(a) > 5 else ""
        fname = a[6] if len(a) > 6 else ""
        return run_backend(job_id, user, title, copies, opts, fname)

    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--fit", choices=["auto", "fit", "exact"], default="auto",
                    help="auto=能 1:1 就 1:1（默认）；fit=缩放适配；exact=强制 1:1 裁切")
    ap.add_argument("--tone", choices=["text", "photo"], default="text")
    ap.add_argument("--thresh", type=int, default=200)
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--duplex", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--send", metavar="IP")
    ap.add_argument("--port", type=int, default=9100)
    a = ap.parse_args()

    p = Path(a.file).expanduser()
    if not p.exists():
        sys.exit("找不到文件：%s" % p)
    log("文档 %s  fit=%s tone=%s" % (p.name, a.fit, a.tone))
    data = build(p, a.fit, a.tone, a.thresh, a.copies, a.duplex)
    log("SPLC 作业 %d 字节（%.1f KB）" % (len(data), len(data) / 1024))
    if a.out:
        Path(a.out).write_bytes(data)
        log("已写出 %s" % a.out)
    if a.send:
        log("已发送，耗时 %.2f 秒" % send(data, a.send, a.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
