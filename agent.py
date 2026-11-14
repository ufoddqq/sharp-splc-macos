#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent.py —— splcprint 后台渲染服务（用户级，监听 127.0.0.1:9123）
================================================================================

为什么要有它
------------
macOS 的 CUPS 后端进程跑在沙盒里（身份 `_lp`），只能读 CUPS 自己的目录，
连 /tmp 都不能写。所以后端只做一件事：把 CUPS 递过来的 spool 文件通过
回环 TCP 转给这里；真正的 PDF 渲染、SPLC 编码、发给打印机都在这个用户级
服务里完成。

协议（极简）
------------
    客户端 → "SPLC1 <job> <copies> <fit> <tone> <thresh> <nbytes>\\n" + nbytes 字节
    服务端 → "OK <作业字节数>\\n"   或   "ERR <原因>\\n"

运行
----
通常由 ~/Library/LaunchAgents/com.splcprint.agent.plist 常驻启动。
手动调试：  python agent.py --foreground
单文件测试：python agent.py --print 文件.pdf [--fit exact]
"""
from __future__ import annotations

import argparse
import os
import socket
import socketserver
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import print_job                      # noqa: E402

HOST = os.environ.get("SPLC_LISTEN", "127.0.0.1")
PORT = int(os.environ.get("SPLC_PORT", "9123"))
PRINTER = os.environ.get("SPLC_PRINTER", "")
PRINTER_PORT = int(os.environ.get("SPLC_PRINTER_PORT", "9100"))
LOGFILE = Path(os.environ.get(
    "SPLC_LOG", str(Path.home() / "Library/Logs/splcprint.log")))

LOCK = threading.Lock()               # 同一时刻只渲染一个作业，避免交错


def log(msg: str) -> None:
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)
    sys.stderr.flush()


def _parse_header(line: bytes):
    tok = line.decode("utf-8", "replace").strip().split()
    if not tok or tok[0] != "SPLC1":
        raise ValueError("协议头不对：%r" % line[:60])
    job = tok[1] if len(tok) > 1 else "0"
    copies = int(tok[2]) if len(tok) > 2 and tok[2].isdigit() else 1
    fit = tok[3] if len(tok) > 3 else "auto"
    tone = tok[4] if len(tok) > 4 else "text"
    thresh = int(tok[5]) if len(tok) > 5 and tok[5].lstrip("-").isdigit() else 200
    size = int(tok[6]) if len(tok) > 6 and tok[6].isdigit() else -1
    return job, copies, fit, tone, thresh, size


def _read_stream(f, first: bytes) -> bytes:
    """把连接余下的字节全部读进来（socket 后端写完就关连接，读到 EOF 为止）。"""
    buf = bytearray(first)
    while True:
        try:
            chunk = f.read(65536)
        except Exception:                                   # 读超时/连接复位
            break
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _split_docs(data: bytes):
    """CUPS 对多份作业是「同一份文件连发 N 遍」（*cupsManualCopies: False），
    所以按 %PDF- 头切开，一份一个文档。图片等其它格式不切。"""
    if data.startswith(b"%PDF-"):
        parts = data.split(b"%PDF-")
        docs = [b"%PDF-" + p for p in parts[1:] if p]
        if len(docs) > 1:
            return docs
    return [data]


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        peer = "%s:%s" % self.client_address
        self.request.settimeout(600)
        f = self.request.makefile("rb")
        try:
            line = f.readline(1024)
            if not line:
                return

            if line.startswith(b"SPLC1"):
                # —— 老协议：splcprint 后端转发的，带作业号与选项 ——
                job, copies, fit, tone, thresh, size = _parse_header(line)
                if size < 0:
                    raise ValueError("缺少数据长度")
                data = f.read(size)
                if len(data) != size:
                    raise ValueError("数据不完整：%d/%d" % (len(data), size))
                docs = [data]
                log("收到作业 #%s  份数=%d fit=%s tone=%s thresh=%d  数据=%d 字节  来自 %s"
                    % (job, copies, fit, tone, thresh, size, peer))
            else:
                # —— 裸流：CUPS 的 socket 后端直连本端口，没有任何协议头 ——
                data = _read_stream(f, line)
                docs = _split_docs(data)
                job = "裸流"
                copies, fit, tone, thresh = 1, "auto", "text", 200
                log("收到裸流作业 文档=%d 份（多份拼包已切分）  数据=%d 字节  来自 %s"
                    % (len(docs), len(data), peer))

            with LOCK:
                spl = b""
                for idx, doc in enumerate(docs, 1):
                    tag = job if len(docs) == 1 else "%s-%d/%d" % (job, idx, len(docs))
                    t0 = time.time()
                    spl = print_job.build(doc, fit=fit, tone=tone, thresh=thresh,
                                          copies=copies, quiet=False)
                    log("[编码] #%s -> %d 字节（%.1f KB），耗时 %.1f 秒"
                        % (tag, len(spl), len(spl) / 1024, time.time() - t0))
                    t1 = time.time()
                    print_job.send(spl, PRINTER, PRINTER_PORT)
                    log("[完成] #%s 已送达 %s:%d（%.2f 秒）"
                        % (tag, PRINTER, PRINTER_PORT, time.time() - t1))

            try:
                self.request.sendall(b"OK %d\n" % len(spl))
            except Exception:                               # socket 后端早已关连接
                pass
        except Exception as e:                              # noqa: BLE001
            log("[失败] %s: %s\n%s" % (type(e).__name__, e,
                                       traceback.format_exc(limit=3)))
            try:
                self.request.sendall(("ERR %s\n" % str(e)[:200]).encode())
            except Exception:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve() -> None:
    if not PRINTER:
        raise SystemExit("SPLC_PRINTER is not set (for example: SPLC_PRINTER=192.168.1.50)")
    log("=== splcprint 服务启动 %s:%d → 打印机 %s:%d ==="
        % (HOST, PORT, PRINTER, PRINTER_PORT))
    with Server((HOST, PORT), Handler) as srv:
        srv.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--foreground", action="store_true", help="前台运行（调试用）")
    ap.add_argument("--print", metavar="文件", help="直接打印一个文件后退出")
    ap.add_argument("--fit", default="auto", choices=["auto", "fit", "exact"])
    ap.add_argument("--tone", default="text", choices=["text", "photo"])
    ap.add_argument("--thresh", type=int, default=200)
    ap.add_argument("--copies", type=int, default=1)
    a = ap.parse_args()

    if a.print:
        data = Path(a.print).read_bytes()
        log("手动打印 %s（%d 字节）" % (a.print, len(data)))
        spl = print_job.build(data, fit=a.fit, tone=a.tone,
                              thresh=a.thresh, copies=a.copies)
        log("SPLC %d 字节，发送中…" % len(spl))
        print_job.send(spl, PRINTER, PRINTER_PORT)
        log("已送达")
        return 0

    try:
        serve()
    except KeyboardInterrupt:
        log("=== 收到中断，退出 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
