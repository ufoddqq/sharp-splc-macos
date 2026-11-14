#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SPLC（Sharp Printer Language with Compression）生成器 —— Python 实现
======================================================================

目标机型：SHARP AR-2221R，同样适用于 AR-2421R / 2821R / 3121R（同为 SPLC 语言）。

来源与依据
----------
协议结构、QM 算术编码、光栅几何来自开源项目 `zy2n9nzt5b-hue/SharpSPLCBridge`
（Swift 实现，针对 AR-5516N）。本文件是等价的 Python 重写，便于在 Mac 上
直接调参数、改一版跑一次，不需要 Xcode 工具链。

静态资源 band1（7104 字节）不随仓库分发：它来自夏普官方驱动，请按
tools/extract-band1.py 的说明从你自己下载的驱动里提取。

用法
----
    python3 splc.py --pattern test --out test.spl        # 生成校准页（不发送）
    python3 splc.py --pattern blank --out blank.spl
    python3 splc.py --pattern test --send 192.168.1.50   # 直接发到 9100

风险提示
--------
`--send` 会真的出纸。参数若与机器不匹配，可能打出乱码或空白页。
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
from pathlib import Path

MASK = 0xFFFFFFFF
HERE = Path(__file__).resolve().parent

# 作业静态设置串（37 字节，来自夏普官方驱动 sd3bnp2.dat 的子类型 0x000A 帧）
# 后台服务目录里不一定带 setupA.txt，故内嵌一份兜底。
SETUP_A = b"7h11a0d100l100e3296s4768t1r6m16v48u1o"

# ── 设备光栅参数（AR-5516N 实测值；band1 与 AR-2221R 完全一致，几何暂沿用） ──
WIDTH = 4912          # 设备光栅宽（px）
HEIGHT = 6400         # 设备光栅高（px）
STRIDE = 614          # 每行字节数 = 4912/8
PAGE_W = 4696         # 页面区宽（px）
PAGE_H = 6382         # 页面区高（px）
PAGE_STRIDE = 587     # 页面区每行字节数 = 4696/8
ORIGIN_X = 216        # 水平原点偏移（px）
ORIGIN_Y = 18         # 垂直原点偏移（行）
DPI = 600

MAX_PAYLOAD = 32736   # 单个 0x0001 帧最大载荷

# ── QM 编码器 Qe 表（JBIG1 / ITU-T T.82，113 项） ──
QE_TABLE = [
    (23069,1,129), (9606,2,14), (4372,3,16), (2059,4,18), (984,5,20), (474,6,23), (229,7,25),
    (111,8,28), (54,9,30), (26,10,33), (13,11,35), (6,12,9), (3,13,10), (1,13,12),
    (23167,15,143), (16165,16,36), (11506,17,38), (8316,18,39), (6073,19,40), (4482,20,42),
    (3311,21,43), (2465,22,45), (1839,23,46), (1372,24,48), (1030,25,49), (771,26,51),
    (576,27,52), (433,28,54), (324,29,56), (245,30,57), (183,31,59), (138,32,60), (104,33,62),
    (78,34,63), (59,35,32), (44,9,33), (23265,37,165), (18508,38,64), (14861,39,65),
    (12017,40,67), (9759,41,68), (7987,42,69), (6568,43,70), (5400,44,72), (4471,45,73),
    (3700,46,74), (3067,47,75), (2552,48,77), (2145,49,78), (1798,50,79), (1485,51,48),
    (1246,52,50), (1039,53,50), (867,54,51), (724,55,52), (604,56,53), (504,57,54),
    (420,58,55), (352,59,56), (293,60,57), (246,61,58), (203,62,59), (171,63,61), (143,32,61),
    (23314,65,193), (19716,66,80), (16684,67,81), (14296,68,82), (12264,69,83), (10556,70,84),
    (9081,71,86), (7903,72,87), (6825,73,87), (5966,74,72), (5156,75,72), (4508,76,74),
    (3947,77,74), (3409,78,75), (2998,79,77), (2624,48,77), (22578,81,208), (19740,82,88),
    (17294,83,89), (15325,84,90), (13550,85,91), (11950,86,92), (10650,87,93), (9494,71,86),
    (21872,89,216), (19625,90,95), (17625,91,96), (15906,92,97), (14372,93,99), (12980,94,99),
    (11799,86,93), (22184,96,223), (20294,97,101), (18405,98,102), (16847,99,103),
    (15421,100,104), (14174,93,99), (21041,102,105), (19471,103,106), (17977,104,107),
    (16734,99,103), (22055,106,233), (20711,107,108), (19333,103,109), (21911,109,110),
    (20559,107,111), (23056,111,238), (21794,109,112), (23019,111,240),
]


# ══════════════════════════════════════════════════════════════════
#  QM 算术编码器
# ══════════════════════════════════════════════════════════════════
class SPLCEncoder:
    def __init__(self) -> None:
        self.a = 0x10000
        self.c = 0
        self.ct = 11
        self.cx = [0] * 1024
        self.buffer = 0x80000000
        self.sc = 0
        self.out = bytearray()

    def _byte_out(self) -> None:
        t = self.c >> 19
        if t < 0xFF:
            if self.buffer != 0x80000000:
                self.out.append(self.buffer & 0xFF)
            for _ in range(self.sc):
                self.out.append(0xFF)
                self.out.append(0x00)
            self.sc = 0
            self.buffer = t
        elif (t & 0xFFFFFF00) != 0:
            if self.buffer != 0x80000000:
                self.buffer = (self.buffer + 1) & 0xFF
                self.out.append(self.buffer)
                if self.buffer == 0xFF:
                    self.out.append(0x00)
            for _ in range(self.sc):
                self.out.append(0x00)
            self.sc = 0
            self.buffer = t & 0xFF
        else:
            self.sc += 1
        self.c &= 0x7FFFF
        self.ct = 8

    def _renorm(self) -> None:
        while True:
            self.a = (self.a << 1) & MASK
            self.c = (self.c << 1) & MASK
            self.ct -= 1
            if self.ct == 0:
                self._byte_out()
            if (self.a & 0xFFFF8000) != 0:
                break

    def bit(self, context: int, pixel: int) -> None:
        st = self.cx[context]
        mps = st >> 7
        qe, nmps, nlps = QE_TABLE[st & 0x7F]
        self.a = (self.a - qe) & MASK
        if pixel != mps:
            if self.a >= qe:
                self.c = (self.c + self.a) & MASK
                self.a = qe
            self.cx[context] = (st & 0x80) ^ nlps
            self._renorm()
        else:
            if (self.a & 0xFFFF8000) != 0:
                return
            if self.a < qe:
                self.c = (self.c + self.a) & MASK
                self.a = qe
            self.cx[context] = (st & 0x80) | nmps
            self._renorm()

    def finish(self) -> bytes:
        for _ in range(32):
            self.c = (self.c << 1) & MASK
            self.ct -= 1
            if self.ct == 0:
                self._byte_out()
        if self.buffer != 0x80000000:
            self.out.append(self.buffer & 0xFF)
        return bytes(self.out)


def _little_word(row: bytes, p: int) -> int:
    if p < 0:
        return row[0] << 8 | row[1] << 16 | row[2] << 24
    return row[p] | row[p + 1] << 8 | row[p + 2] << 16 | row[p + 3] << 24


# ── C 加速版（libsplcqm.dylib）：与下面纯 Python 版逐位等价 ──────────────
# 纯 Python 一页 A4 要 31–34 秒（3140 万次 bit() 调用），C 版约 0.13 秒。
# 找不到库或加载失败时自动退回纯 Python，不影响正确性。
_LIB = None
_LIB_TRIED = False


def _load_lib():
    global _LIB, _LIB_TRIED
    if _LIB_TRIED:
        return _LIB
    _LIB_TRIED = True
    import ctypes
    for p in (HERE / "libsplcqm.dylib",
              Path.home() / "Library/Application Support/splcprint/libsplcqm.dylib"):
        if not p.exists():
            continue
        try:
            lib = ctypes.CDLL(str(p))
            lib.splc_encode_page.restype = ctypes.c_long
            lib.splc_encode_page.argtypes = [
                ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
            lib._free = ctypes.CDLL("/usr/lib/libSystem.B.dylib").free
            lib._free.argtypes = [ctypes.c_void_p]
            _LIB = lib
            return lib
        except Exception:                                  # noqa: BLE001
            continue
    return None


def _splc_encode_page_c(rows: list, stride: int) -> bytes | None:
    lib = _load_lib()
    if lib is None:
        return None
    import ctypes
    flat = b"".join(bytes(r[:stride]).ljust(stride, b"\0") for r in rows)
    out = ctypes.POINTER(ctypes.c_ubyte)()
    n = lib.splc_encode_page(flat, len(rows), stride, ctypes.byref(out))
    if n < 0:
        return None
    try:
        return ctypes.string_at(out, n)
    finally:
        lib._free(out)


def splc_encode_page(rows: list, stride: int) -> bytes:
    """把按行倒序、字节倒序的设备光栅编码成 SPLC 位流。"""
    fast = _splc_encode_page_c(rows, stride)
    if fast:
        return fast
    return splc_encode_page_py(rows, stride)


def splc_encode_page_py(rows: list, stride: int) -> bytes:
    """纯 Python 参考实现（C 库不可用时的兜底，也是等价性校验的基准）。"""
    enc = SPLCEncoder()
    ref = bytes(stride)
    for row_data in rows:
        cur = bytearray(row_data) + bytearray(8)
        ref_padded = bytearray(ref) + bytearray(8)
        p = stride - 3
        while p > -2:
            cw = _little_word(cur, p)
            rw = _little_word(ref_padded, p)
            for k in range(16):
                ref_part = ((rw >> (21 - k)) & 0x3F) << 4
                cur_part = (cw >> (24 - k)) & 0x0F
                enc.bit(ref_part | cur_part, (cw >> (23 - k)) & 1)
            p -= 2
        ref = bytes(cur[:stride])
    return enc.finish()


# ══════════════════════════════════════════════════════════════════
#  位图页 → 设备光栅
# ══════════════════════════════════════════════════════════════════
def to_device_rows(natural: list[bytes]) -> list[bytes]:
    """自然序（第 0 行 = 顶部）→ 补边 → 行倒序 → 每行前置水平偏移。"""
    blank = bytes(PAGE_STRIDE)
    rows = list(natural)
    if len(rows) < PAGE_H:
        rows.extend([blank] * (PAGE_H - len(rows)))
    rows = [blank] * ORIGIN_Y + rows[:PAGE_H]
    rows.reverse()
    pad = bytes(ORIGIN_X // 8)          # 27 字节 = 216 px
    out = []
    for r in rows:
        r = bytes(r[:PAGE_STRIDE])
        if len(r) < PAGE_STRIDE:
            r += bytes(PAGE_STRIDE - len(r))
        out.append(pad + r)
    return out


def to_native_order(device_rows: list[bytes]) -> list[bytes]:
    """再整体倒序 + 每行字节倒序（位不反转）。"""
    return [bytes(reversed(r)) for r in reversed(device_rows)]


# ══════════════════════════════════════════════════════════════════
#  SPLC 帧封装
# ══════════════════════════════════════════════════════════════════
def record(subtype: int, payload: bytes = b"") -> bytes:
    return b"\x81\x81\x00\x00" + struct.pack(">HH", len(payload), subtype) + payload


def load_band1() -> bytes:
    """读取 SPLC 静态资源 band1.bin（7104 字节，来自夏普官方驱动 sd3bnp2.dat）。

    依次在：脚本同目录 → 用户服务目录 → 当前目录 里找，找不到抛异常
    （不能用 sys.exit，否则会在服务线程里静默退出、只表现为连接被重置）。
    """
    cands = [
        HERE / "band1.bin",
        Path.home() / "Library/Application Support/splcprint/band1.bin",
        Path.cwd() / "band1.bin",
    ]
    for p in cands:
        if p.exists():
            return p.read_bytes()
    raise RuntimeError("缺少资源文件 band1.bin，找过：%s" % [str(c) for c in cands])


def job_setup(copies: int = 1, duplex: bool = False, tumble: bool = False) -> str:
    """作业设置串。x 字段 = 硬件份数，j = 双面，e = 装订方向。"""
    j = 1 if duplex else 0
    e = 1 if tumble else 0
    x = max(1, min(copies, 99))
    return f"300z1p600r{j}j{x}x600d1c{e}e"


def page_setup(side: int = 0, source_h: int = 7) -> str:
    """每页设置串。side: 单面=0 / 双面正面=2 / 双面反面=1；source_h: Tray1=7, 手送=261。"""
    return f"{source_h}h1a{side}d6400t4912s100e100l6m16v48u0o0r"


def make_job(pages: list[list[bytes]], copies: int = 1, duplex: bool = False,
             tumble: bool = False, source_h: int = 7) -> bytes:
    """pages: 每页是 natural 序的位图行列表。"""
    if not pages:
        return b""
    band1 = load_band1()
    out = bytearray()
    out += record(0x0008)
    out += record(0x0002, job_setup(copies, duplex, tumble).encode())
    _setup_a = HERE / "setupA.txt"
    out += record(0x000A, _setup_a.read_bytes() if _setup_a.exists() else SETUP_A)
    out += record(0x0001, band1)
    out += record(0x000B)

    for idx, rows in enumerate(pages):
        side = (2 if idx % 2 == 0 else 1) if duplex else 0
        band = splc_encode_page(to_native_order(to_device_rows(rows)), STRIDE)
        # 收尾标记 + 32 字节对齐
        while band and band[-1] == 0:
            band = band[:-1]
        band += b"\x02" if (band and band[-1] == 0xFF) else b"\xFF\x02"
        while len(band) % 32:
            band += b"\x00"
        out += record(0x0005, page_setup(side, source_h).encode())
        off = 0
        while off < len(band):
            out += record(0x0001, band[off:off + MAX_PAYLOAD])
            off += MAX_PAYLOAD
        out += record(0x0006)

    out += record(0x0003)
    out += record(0x0009)
    return bytes(out)


# ══════════════════════════════════════════════════════════════════
#  位图绘制（不依赖 PIL）
# ══════════════════════════════════════════════════════════════════
class Canvas:
    def __init__(self, w: int = PAGE_W, h: int = PAGE_H) -> None:
        self.w = w
        self.h = h
        self.rows = [bytearray((w + 7) // 8) for _ in range(h)]

    def px(self, x: int, y: int, on: bool = True) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            byte, bit = x >> 3, 7 - (x & 7)
            if on:
                self.rows[y][byte] |= 1 << bit
            else:
                self.rows[y][byte] &= ~(1 << bit) & 0xFF

    def hline(self, x0: int, x1: int, y: int, t: int = 1) -> None:
        for dy in range(t):
            for x in range(max(0, x0), min(self.w, x1)):
                self.px(x, y + dy)

    def vline(self, x: int, y0: int, y1: int, t: int = 1) -> None:
        for dx in range(t):
            for y in range(max(0, y0), min(self.h, y1)):
                self.px(x + dx, y)

    def rect(self, x0: int, y0: int, x1: int, y1: int, t: int = 2) -> None:
        self.hline(x0, x1, y0, t)
        self.hline(x0, x1, y1 - t, t)
        self.vline(x0, y0, y1, t)
        self.vline(x1 - t, y0, y1, t)

    def fill(self, x0: int, y0: int, x1: int, y1: int) -> None:
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self.px(x, y)

    def out(self) -> list[bytes]:
        return [bytes(r) for r in self.rows]


def pattern_test() -> list[bytes]:
    """校准页：外框 + 中心十字 + 四角定位块 + 刻度 + 渐变块。"""
    c = Canvas()
    m = 120
    c.rect(m, m, PAGE_W - m, PAGE_H - m, 3)          # 外框
    cx, cy = PAGE_W // 2, PAGE_H // 2
    c.vline(cx, m, PAGE_H - m, 3)                     # 中心竖线
    c.hline(m, PAGE_W - m, cy, 3)                     # 中心横线
    # 四角定位块（实心 120x120）
    s = 120
    for (bx, by) in ((m + 40, m + 40), (PAGE_W - m - 40 - s, m + 40),
                     (m + 40, PAGE_H - m - 40 - s), (PAGE_W - m - 40 - s, PAGE_H - m - 40 - s)):
        c.fill(bx, by, bx + s, by + s)
    # 顶部刻度：每 200px 一条，每 5 条加长
    for i, x in enumerate(range(m + 200, PAGE_W - m, 200)):
        ln = 120 if i % 5 == 4 else 60
        c.vline(x, m + 4, m + 4 + ln, 2)
    # 左侧刻度
    for i, y in enumerate(range(m + 200, PAGE_H - m, 200)):
        ln = 120 if i % 5 == 4 else 60
        c.hline(m + 4, m + 4 + ln, y, 2)
    # 灰度阶梯（用抖动近似 5 档，检验半色调输出）
    bw, gap = 260, 40
    x0 = m + 100
    y0 = PAGE_H - m - 500
    for k in range(5):
        dens = (k + 1) / 5
        for yy in range(y0, y0 + 300):
            for xx in range(x0, x0 + bw):
                if ((xx + yy) % 5) < dens * 5:
                    c.px(xx, yy)
        x0 += bw + gap
    # 细线组（检验 1px 线能否出来）
    for k in range(6):
        c.hline(m + 100, m + 1100, PAGE_H - m - 140 + k * 10, 1 + k)
    return c.out()


def pattern_blank() -> list[bytes]:
    return Canvas().out()


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", choices=["test", "blank"], default="test")
    ap.add_argument("--out")
    ap.add_argument("--send", metavar="IP", help="发送到打印机 9100（会真的出纸）")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--duplex", action="store_true")
    ap.add_argument("--port", type=int, default=9100)
    a = ap.parse_args()

    rows = pattern_test() if a.pattern == "test" else pattern_blank()
    print(f"位图 {PAGE_W}x{PAGE_H} px，行数 {len(rows)}")
    data = make_job([rows], copies=a.copies, duplex=a.duplex)
    print(f"SPLC 作业 {len(data)} 字节（{len(data)/1024:.1f} KB）")

    if a.out:
        Path(a.out).write_bytes(data)
        print(f"已写出 {a.out}")

    if a.send:
        print(f"→ 连接 {a.send}:{a.port} …")
        with socket.create_connection((a.send, a.port), timeout=30) as s:
            s.sendall(data)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        print("已发送完毕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
