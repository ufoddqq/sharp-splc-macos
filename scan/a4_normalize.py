#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a4_normalize.py —— 把扫描件 PDF 的页面统一成 A4（等比缩放 + 居中，不裁切内容）

用途：Sharp AR-22xx / 24xx / 28xx / 31xx 的原稿尺寸是「自动」，放 A3 就输出 A3。
      本脚本在上游不改任何设备设置的前提下，把收到的 PDF 归一化成 A4：
        · 比 A4 大（A3 等）→ 等比缩小到能放下为止，居中，内容一点不丢
        · 比 A4 小         → 不放大，居中补白，仍是 A4 页面
        · 本来就是 A4/A4 横向 → 原样不动（不旋转，不重采样）

实现：PDF 增量更新（incremental update）。不重写原文件、不重新编码图像，
      只在尾部追加新的 Page 对象（换 MediaBox）和新的 Contents（外面套一层变换矩阵）。
      图像数据一个字节都不动，所以速度和画质都不受影响。

依赖：仅标准库。

用法：
    python3 a4_normalize.py 输入.pdf               # 原地修改
    python3 a4_normalize.py 输入.pdf -o 输出.pdf    # 另存
    python3 a4_normalize.py --dry-run 输入.pdf      # 只报告会怎么改
退出码：0 = 已处理或无需处理；1 = 结构不认识，未改动；2 = 参数错误
"""

import argparse
import os
import re
import sys
import tempfile

# A4 尺寸（pt，1pt = 1/72 inch）
A4_SHORT = 595.276
A4_LONG = 841.890

TOL = 3.0          # 与 A4 相差 3pt（约 1mm）以内视为已是 A4
MIN_GAIN = 1.0     # 缩放比接近 1 且尺寸已合规时不折腾


# --------------------------------------------------------------------------
# 结构探测：只认 Sharp 扫描件那种「单页 + 一张整幅图像」的结构
# --------------------------------------------------------------------------
def _find_page_dict(data):
    """返回 (obj_num, dict_text_start, dict_text_end)，找不到返回 None。

    注意：本脚本用的是增量更新，处理过的文件里会同时存在「旧 Page」和「新 Page」。
    按 PDF 规则，后出现的版本覆盖先出现的，所以这里**取最后一个** /Type /Page，
    否则二次处理会拿着旧尺寸再缩一遍（内容会越缩越小）。
    """
    chosen = None
    i = data.find(b'/Type /Page')
    while i != -1:
        if data[i + 11:i + 12] != b's':          # 排除 /Type /Pages
            chosen = i
        i = data.find(b'/Type /Page', i + 1)
    if chosen is None:
        return None

    start = data.rfind(b'obj', 0, chosen)
    if start == -1:
        return None
    hdrm = re.search(rb'(\d+)\s+0\s+obj\s*$', data[0:start + 3])
    if not hdrm:
        return None
    dstart = start + 3
    dend = data.find(b'endobj', dstart)
    if dend == -1:
        return None
    return int(hdrm.group(1)), dstart, dend


def _parse_mediabox(page_dict):
    m = re.search(rb'/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]',
                  page_dict)
    if not m:
        return None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    return x0, y0, x1, y1


def _find_contents_ref(page_dict):
    m = re.search(rb'/Contents\s+(\d+)\s+0\s+R', page_dict)
    return int(m.group(1)) if m else None


def _read_stream(data, obj_num):
    """读取编号 obj_num 的流对象，返回 (字典字节, 流字节) 或 None。

    同样取**最后一个**同名对象 —— 增量更新后旧版本仍在文件里。
    """
    pat = re.compile(rb'(?<![0-9])' + str(obj_num).encode() + rb'\s+0\s+obj')
    m = None
    for m in pat.finditer(data):
        pass
    if m is None:
        return None
    start = m.end()
    s_i = data.find(b'stream', start)
    if s_i == -1:
        return None
    dict_bytes = data[start:s_i]
    # 跳过 stream 后的换行
    body = s_i + 6
    if data[body:body + 2] == b'\r\n':
        body += 2
    elif data[body:body + 1] in (b'\n', b'\r'):
        body += 1
    lm = re.search(rb'/Length\s+(\d+)', dict_bytes)
    if lm:
        length = int(lm.group(1))
        raw = data[body:body + length]
    else:
        e = data.find(b'endstream', body)
        if e == -1:
            return None
        raw = data[body:e].rstrip(b'\r\n')
    return dict_bytes, raw


# --------------------------------------------------------------------------
# 增量更新写出
# --------------------------------------------------------------------------
def _num(v):
    """紧凑的数字格式，避免 0.30000000000000004 这种尾巴。"""
    s = ('%.4f' % v).rstrip('0').rstrip('.')
    return s if s else '0'


def build_normalized(data):
    """返回 (新文件字节, 说明文字)。无法处理时抛 ValueError。"""
    found = _find_page_dict(data)
    if not found:
        raise ValueError('找不到 /Type /Page 对象')
    page_num, dstart, dend = found
    page_dict = data[dstart:dend]

    box = _parse_mediabox(page_dict)
    if not box:
        raise ValueError('页面没有 /MediaBox')
    x0, y0, x1, y1 = box
    W, H = x1 - x0, y1 - y0
    if W <= 0 or H <= 0:
        raise ValueError('MediaBox 尺寸异常: %r' % (box,))

    cont_num = _find_contents_ref(page_dict)
    if cont_num is None:
        raise ValueError('页面没有 /Contents 引用')
    stream = _read_stream(data, cont_num)
    if not stream:
        raise ValueError('/Contents %d 读取失败' % cont_num)
    _, content = stream
    if len(content) == 0:
        raise ValueError('/Contents 为空')

    # ---- 目标页面 ----
    portrait = H >= W
    tw = A4_SHORT if portrait else A4_LONG
    th = A4_LONG if portrait else A4_SHORT

    if abs(W - tw) <= TOL and abs(H - th) <= TOL:
        raise ValueError('已经是 A4（%s x %s pt），无需处理'
                         % (_num(W), _num(H)))

    # 只有比 A4 大才缩小；比 A4 小则保持原始比例、居中补白（不放大，避免糊）
    s = min(tw / W, th / H)
    if s > MIN_GAIN:
        s = MIN_GAIN
    tx = (tw - W * s) / 2.0
    ty = (th - H * s) / 2.0

    # 外层套一层「缩放 + 居中」。原内容本身把整页映射到 (x0,y0)-(x1,y1)，
    # 外面乘 [s 0 0 s tx ty] 后，内容正好落在居中的 A4 区域内。
    wrap = ('q\n%s 0 0 %s %s %s cm\n' % (_num(s), _num(s), _num(tx), _num(ty))
            ).encode('ascii')
    new_content = wrap + content + b'\nQ\n'

    # ---- 新的 Page 字典：只换 MediaBox ----
    new_box = ('[0 0 %s %s]' % (_num(tw), _num(th))).encode('ascii')
    new_page = re.sub(rb'/MediaBox\s*\[[^\]]*\]',
                      b'/MediaBox ' + new_box, page_dict, count=1)
    if new_page == page_dict:
        raise ValueError('替换 MediaBox 失败')

    # ---- 追加写入 ----
    out = bytearray(data)
    if not out.endswith(b'\n'):
        out += b'\n'

    offs = {}

    def add_obj(num, body):
        offs[num] = len(out)
        out.extend(('%d 0 obj\n' % num).encode('ascii'))
        out.extend(body)
        out.extend(b'\nendobj\n')

    add_obj(page_num, new_page.strip())
    add_obj(cont_num,
            ('<< /Length %d >>\nstream\n' % len(new_content)).encode('ascii')
            + new_content + b'endstream')

    # 原有 /Size
    size = 0
    for m in re.finditer(rb'/Size\s+(\d+)', data):
        size = max(size, int(m.group(1)))
    size = max(size, max(offs) + 1)

    prev = 0
    for m in re.finditer(rb'startxref\s+(\d+)', data):
        prev = int(m.group(1))

    xref_pos = len(out)
    out.extend(b'xref\n')
    for num in sorted(offs):
        out.extend(('%d 1\n' % num).encode('ascii'))
        out.extend(('%010d 00000 n\r\n' % offs[num]).encode('ascii'))
    out.extend(('trailer\n<< /Size %d /Root 2 0 R /Prev %d >>\n'
                'startxref\n%d\n%%%%EOF\n' % (size, prev, xref_pos)
                ).encode('ascii'))

    desc = ('%s x %s pt（%d x %d mm）→ A4 %s，缩放 %.1f%%'
            % (_num(W), _num(H),
               round(W / 72 * 25.4), round(H / 72 * 25.4),
               '纵向' if portrait else '横向', s * 100))
    return bytes(out), desc


def atomic_write(path, blob, mode_from=None):
    d = os.path.dirname(os.path.abspath(path)) or '.'
    # mkstemp 建出来是 0600，必须把原文件的权限位带过来，否则收件人读不了
    src_mode = None
    for cand in (mode_from, path):
        if cand and os.path.exists(cand):
            try:
                src_mode = os.stat(cand).st_mode & 0o7777
                break
            except OSError:
                pass
    fd, tmp = tempfile.mkstemp(prefix='.a4-', dir=d)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        if src_mode is not None:
            try:
                os.chmod(tmp, src_mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description='把扫描 PDF 归一化为 A4')
    ap.add_argument('input')
    ap.add_argument('-o', '--output', help='输出路径（默认原地覆盖）')
    ap.add_argument('--dry-run', action='store_true', help='只报告，不写文件')
    ap.add_argument('-q', '--quiet', action='store_true')
    a = ap.parse_args(argv)

    try:
        data = open(a.input, 'rb').read()
    except OSError as e:
        print('读取失败：%s' % e, file=sys.stderr)
        return 2

    try:
        blob, desc = build_normalized(data)
    except ValueError as e:
        if not a.quiet:
            print('跳过：%s' % e)
        return 0

    if a.dry_run:
        print('将处理：%s' % desc)
        return 0

    out = a.output or a.input
    try:
        atomic_write(out, blob, mode_from=a.input)
    except OSError as e:
        print('写入失败：%s' % e, file=sys.stderr)
        return 2

    if not a.quiet:
        print('已处理：%s' % desc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
