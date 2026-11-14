#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sharp 扫描接收端 —— 零依赖 FTP 服务端（仅标准库）
=================================================================
背景：Sharp AR-22xx / 24xx / 28xx / 31xx 的「网络扫描」通常只提供两种目的地：
      E-mail 和 FTP。本脚本在 Mac 上开一个专用 FTP 服务端，只实现
      「登录 + 上传」，不接受删除/改名，用于接收机器推过来的扫描件。
      只实现「登录 + 上传」必需的命令，不接受任何删除/改名操作。

用法：
    # 本地测试（免权限，换高位端口）
    /usr/bin/python3 scan_receiver.py --port 2121

    # 正式运行（占用 21 端口需管理员权限，机器侧通常只认 21）
    sudo /usr/bin/python3 scan_receiver.py --password 'your-password'

机器侧填写（机器面板 → 通讯簿/地址簿 → 新增 FTP 目的地）：
    主机名或 IP ：<本机局域网 IP>      ← 脚本启动时会打印
    用户名      ：scan                （留空则接受匿名）
    密码        ：启动时打印          （= --password 的值；未指定则随机生成）
    目录        ：/                   （= --dir 指定的文件夹）
    SSL         ：不勾选

扫描件保存到： ~/Scans/   （用 --dir 可改）

安全提示：这是明文 FTP，没有 TLS。只在可信局域网使用，务必设置一个像样的密码，
          不要把它暴露到公网。
"""

import argparse
import os
import socket
import sys
import threading
import time
from datetime import datetime

CRLF = "\r\n"

# 以 root 常驻（LaunchDaemon）时，收到的文件属主会是 root；
# 用 --chown UID:GID 可以在落盘后改回你的账号，保证 Finder 里能直接改/删。
CHOWN = None


def ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


# --------------------------------------------------------------------------
# A4 归一化
#   机器的「原稿尺寸」是自动的，放 A3 就输出 A3。这里在收件后把页面统一成 A4：
#   大页等比缩小（内容不裁），小页居中补白，本来就是 A4 的原样不动。
#   失败一律保留原件 —— 宁可尺寸不统一，也不能把扫描件搞坏。
# --------------------------------------------------------------------------
A4_NORMALIZE = True


def a4_inplace(path):
    """就地归一化为 A4，返回说明文字；无需处理返回 None。永不抛异常。"""
    if not A4_NORMALIZE or not path.lower().endswith(".pdf"):
        return None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import a4_normalize
        with open(path, "rb") as f:
            data = f.read()
        blob, desc = a4_normalize.build_normalized(data)
        a4_normalize.atomic_write(path, blob)
        return desc
    except ValueError as e:
        if not str(e).startswith("已经是 A4"):
            log(f"     ·  未做 A4 归一化：{e}")
        return None
    except Exception as e:                                  # noqa: BLE001
        log(f"     ⚠️  A4 归一化失败，已保留原件：{e}")
        return None


def local_ips():
    ips = []
    try:
        out = os.popen("/sbin/ifconfig 2>/dev/null").read()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "127.0.0.1" not in line:
                ips.append(line.split()[1])
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            ips.append("<本机 IP>")
    return ips


class Session(threading.Thread):
    """一条 FTP 控制连接。"""

    def __init__(self, conn, addr, cfg):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.cfg = cfg
        self.rf = conn.makefile("rb")
        self.pasv_sock = None
        self.active_addr = None
        self.root = cfg["dir"]
        self.cwd = "/"
        self.logged = False
        self.user = None
        self.got_files = []

    # ---------- 基础收发 ----------
    def send(self, line):
        try:
            self.conn.sendall((line + CRLF).encode("utf-8", "replace"))
        except OSError:
            pass

    def close_data(self):
        if self.pasv_sock:
            try:
                self.pasv_sock.close()
            except OSError:
                pass
            self.pasv_sock = None
        self.active_addr = None

    # ---------- 主循环 ----------
    def run(self):
        try:
            self.send(f"220 {self.cfg['banner']}")
            while True:
                raw = self.rf.readline()
                if not raw:
                    break
                try:
                    line = raw.decode("utf-8", "ignore").rstrip("\r\n")
                except Exception:
                    break
                if not line.strip():
                    continue
                parts = line.split(" ", 1)
                cmd = parts[0].upper()
                arg = parts[1].strip() if len(parts) > 1 else ""
                try:
                    if not self.handle(cmd, arg):
                        break
                except Exception as e:
                    log(f"⚠️  命令 {cmd} 处理异常: {e}")
                    self.send("451 Requested action aborted: local error")
        except Exception as e:
            log(f"⚠️  会话异常 {self.addr[0]}: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        self.close_data()
        try:
            self.rf.close()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        if self.got_files:
            log(f"—— 会话结束：{self.addr[0]} 共上传 {len(self.got_files)} 个文件")

    # ---------- 命令分发 ----------
    def handle(self, cmd, arg):
        # 无需登录即可用的
        if cmd == "USER":
            self.user = arg
            self.send("331 Password required.")
            return True
        if cmd == "PASS":
            if self.check_login(self.user, arg):
                self.logged = True
                self.send("230 User logged in. Ready to receive scans.")
                log(f"✅ 机器已登录: 用户={self.user or '(匿名)'} 来自 {self.addr[0]}")
            else:
                self.send("530 Login incorrect.")
                log(f"❌ 登录失败: 用户={self.user or '(空)'} 来自 {self.addr[0]}")
            return True
        if cmd == "QUIT":
            self.send("221 Goodbye.")
            return False
        if cmd == "SYST":
            self.send("215 UNIX Type: L8")
            return True
        if cmd == "FEAT":
            self.send("211-Extensions supported:")
            self.send(" SIZE")
            self.send(" UTF8")
            self.send("211 End")
            return True
        if cmd == "NOOP":
            self.send("200 OK.")
            return True

        if not self.logged and cmd not in ("AUTH", "OPTS"):
            self.send("530 Please login with USER and PASS.")
            return True

        if cmd in ("TYPE", "MODE", "STRU"):
            self.send(f"200 Command okay ({cmd} {arg}).")
            return True
        if cmd == "OPTS":
            self.send("200 OPTS okay.")
            return True
        if cmd in ("PWD", "XPWD"):
            self.send(f'257 "{self.cwd}" is current directory.')
            return True
        if cmd in ("CWD", "XCWD"):
            self.cwd = "/" + arg.strip("/")
            self.send(f'250 CWD command successful. "{self.cwd}"')
            return True
        if cmd == "CDUP":
            self.cwd = "/"
            self.send("250 CDUP command successful.")
            return True
        if cmd == "PASV":
            return self.do_pasv()
        if cmd == "EPSV":
            return self.do_epsv()
        if cmd == "PORT":
            return self.do_port(arg)
        if cmd in ("LIST", "NLST"):
            return self.do_list(cmd)
        if cmd == "STOR":
            return self.do_stor(arg)
        if cmd in ("DELE", "RNFR", "RNTO", "MKD", "RMD", "XMKD", "XRMD", "APPE", "STOU"):
            self.send("550 Permission denied (receiver is write-only for new scans).")
            return True
        if cmd in ("RETR", "SIZE", "MDTM"):
            self.send("550 Not available on scan receiver.")
            return True
        if cmd in ("ALLO", "REST", "ABOR"):
            self.send("200 Command okay.")
            return True
        if cmd == "AUTH":
            self.send("500 Not supported.")
            return True
        self.send(f"500 Command '{cmd}' not understood.")
        return True

    def check_login(self, user, pw):
        cfg_user = self.cfg.get("user")
        if not cfg_user:
            return True                       # 允许匿名
        return (user == cfg_user and pw == self.cfg.get("password"))

    # ---------- 数据连接 ----------
    def do_pasv(self):
        self.close_data()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lo, hi = self.cfg["pasv_ports"]
        bound = None
        for p in range(lo, hi + 1):
            try:
                s.bind(("0.0.0.0", p))
                bound = p
                break
            except OSError:
                continue
        if not bound:
            s.close()
            self.send("425 Can't open passive connection (no free port).")
            return True
        s.listen(1)
        s.settimeout(60)
        self.pasv_sock = s
        ip = self.cfg["advertise_ip"]
        h = ip.split(".")
        self.send(f"227 Entering Passive Mode ({h[0]},{h[1]},{h[2]},{h[3]},"
                  f"{bound // 256},{bound % 256})")
        return True

    def do_epsv(self):
        self.close_data()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lo, hi = self.cfg["pasv_ports"]
        bound = None
        for p in range(lo, hi + 1):
            try:
                s.bind(("0.0.0.0", p))
                bound = p
                break
            except OSError:
                continue
        if not bound:
            s.close()
            self.send("425 Can't open passive connection.")
            return True
        s.listen(1)
        s.settimeout(60)
        self.pasv_sock = s
        self.send(f"229 Entering Extended Passive Mode (|||{bound}|)")
        return True

    def do_port(self, arg):
        try:
            nums = [int(x) for x in arg.split(",")]
            ip = ".".join(str(n) for n in nums[:4])
            port = nums[4] * 256 + nums[5]
            self.active_addr = (ip, port)
            self.send("200 PORT command successful.")
        except Exception:
            self.send("501 Bad PORT syntax.")
        return True

    def open_data(self):
        if self.pasv_sock:
            try:
                conn, _ = self.pasv_sock.accept()
                conn.settimeout(120)
                return conn
            except (socket.timeout, OSError) as e:
                log(f"⚠️  数据连接失败: {e}")
                return None
            finally:
                try:
                    self.pasv_sock.close()
                except OSError:
                    pass
                self.pasv_sock = None
        if self.active_addr:
            try:
                c = socket.create_connection(self.active_addr, timeout=20)
                c.settimeout(120)
                return c
            except OSError as e:
                log(f"⚠️  主动模式连接失败: {e}")
                return None
            finally:
                self.active_addr = None
        return None

    # ---------- 上传 ----------
    def safe_name(self, name):
        name = os.path.basename(name.replace("\\", "/")).strip()
        if not name or name in (".", ".."):
            name = "scan_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(self.root, name)
        if not os.path.abspath(out).startswith(os.path.abspath(self.root)):
            out = os.path.join(self.root, "scan_unnamed")
        # 重名自动加序号
        base, ext = os.path.splitext(out)
        n = 1
        while os.path.exists(out):
            out = f"{base}_{n}{ext}"
            n += 1
        return out

    def do_stor(self, arg):
        target = self.safe_name(arg)
        self.send(f"150 Opening BINARY mode data connection for {os.path.basename(target)}.")
        conn = self.open_data()
        if not conn:
            self.send("425 Can't open data connection.")
            return True
        total = 0
        part = target + ".part"
        try:
            with open(part, "wb") as out:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
            os.replace(part, target)
        except OSError as e:
            log(f"⚠️  写入失败: {e}")
            try:
                if os.path.exists(part):
                    os.remove(part)    # 自己的半成品才删
            except OSError:
                pass
            self.send("451 Transfer aborted.")
            conn.close()
            return True
        finally:
            try:
                conn.close()
            except OSError:
                pass
        self.send("226 Transfer complete.")
        self.got_files.append(target)
        note = a4_inplace(target)          # 先归一化，再改属主（会换 inode）
        if note:
            log(f"     📐 {note}")
        if CHOWN:
            try:
                os.chown(target, CHOWN[0], CHOWN[1])
            except OSError as e:
                log(f"⚠️  改属主失败（文件已收到，不影响内容）: {e}")
        log(f"📄 收到扫描件: {os.path.basename(target)}  ({total:,} 字节) 来自 {self.addr[0]}")
        log(f"     → {target}")
        return True

    def do_list(self, cmd):
        self.send("150 Opening data connection for directory listing.")
        conn = self.open_data()
        if not conn:
            self.send("425 Can't open data connection.")
            return True
        lines = []
        try:
            for nm in sorted(os.listdir(self.root)):
                p = os.path.join(self.root, nm)
                st = os.stat(p)
                d = datetime.fromtimestamp(st.st_mtime).strftime("%b %e %H:%M")
                lines.append(f"-rw-r--r--   1 owner group {st.st_size:>10} {d} {nm}")
        except OSError:
            pass
        payload = CRLF.join(lines) + CRLF if lines else ""
        try:
            conn.sendall(payload.encode("utf-8", "replace"))
        except OSError:
            pass
        finally:
            conn.close()
        self.send("226 Transfer complete.")
        return True


def main():
    ap = argparse.ArgumentParser(description="Sharp 扫描接收端（零依赖 FTP）")
    ap.add_argument("--port", type=int, default=21,
                    help="监听端口（夏普机器侧只认 21；测试可用 2121）")
    ap.add_argument("--dir", default=os.path.expanduser("~/Scans"),
                    help="扫描件保存目录")
    ap.add_argument("--user", default="scan", help="登录用户名（传空字符串=允许匿名）")
    ap.add_argument("--password", default=None,
                    help="登录密码（不填则随机生成并在启动时打印）")
    ap.add_argument("--bind", default="0.0.0.0", help="监听地址")
    ap.add_argument("--advertise-ip", default=None,
                    help="PASV 模式下告诉机器的本机 IP（默认自动探测）")
    ap.add_argument("--chown", default=None, metavar="UID:GID",
                    help="收到文件后改属主（以 root 常驻时用，例如 501:20）")
    ap.add_argument("--no-a4-normalize", action="store_true",
                    help="关闭 A4 归一化（默认开启：A3 等大页等比缩到 A4）")
    args = ap.parse_args()

    if args.user and not args.password:
        import secrets
        args.password = secrets.token_urlsafe(9)

    global CHOWN, A4_NORMALIZE
    A4_NORMALIZE = not args.no_a4_normalize
    if args.chown:
        try:
            u, g = args.chown.split(":")
            CHOWN = (int(u), int(g))
        except ValueError:
            sys.exit("--chown 格式应为 UID:GID，例如 501:20")

    root = os.path.abspath(os.path.expanduser(args.dir))
    os.makedirs(root, exist_ok=True)
    # 以 root 常驻时，目录本身也要交给登录用户，
    # 否则访达里「删除扫描件」会因目录不可写而失败（文件属主对了也没用）。
    if CHOWN:
        try:
            os.chown(root, CHOWN[0], CHOWN[1])
        except OSError as e:
            log(f"⚠️  目录改属主失败: {e}")

    ips = local_ips()
    adv = args.advertise_ip or (ips[0] if ips else "127.0.0.1")

    cfg = {
        "dir": root,
        "user": args.user,
        "password": args.password,
        "banner": "Sharp scan receiver (Mac) ready.",
        "pasv_ports": (50000, 50049),
        "advertise_ip": adv,
    }

    if not (1 <= args.port <= 65535):
        sys.exit("端口非法。")

    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((args.bind, args.port))
        srv.listen(16)
    except PermissionError:
        sys.exit(f"\n❌ 端口 {args.port} 需要管理员权限。\n"
                 f"   正式运行： sudo {sys.executable} {os.path.abspath(sys.argv[0])}\n"
                 f"   本地测试： {sys.executable} {os.path.abspath(sys.argv[0])} --port 2121\n")
    except OSError as e:
        sys.exit(f"\n❌ 无法绑定 {args.bind}:{args.port} → {e}\n"
                 f"   端口可能被占用；试试 --port 2121。\n")

    print("=" * 66)
    print("  Sharp 扫描接收端  已启动")
    print("=" * 66)
    print(f"  保存目录  : {root}")
    print(f"  监听      : {args.bind}:{args.port}")
    print(f"  PASV 端口 : 50000-50049（按需在防火墙放行）")
    print(f"  登录账号  : {args.user or '(匿名)'}"
          f"{' / ' + args.password if args.user else ' / 任意密码'}")
    print("  ── 机器面板「通讯簿 → FTP 目的地」请填 ──")
    shown = [adv] + [ip for ip in ips if ip != adv]
    for i, ip in enumerate(shown):
        print(f"    {'主机名/IP ' if i == 0 else '     备用 '}: {ip}")
    print(f"    用户名    : {args.user or '(留空)'}")
    print(f"    密码      : {args.password if args.user else '(留空)'}")
    print(f"    目录      : /")
    print(f"    SSL       : 不勾选")
    print("=" * 66)
    print("  等待机器连接…（Ctrl-C 停止）\n")

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            conn.settimeout(300)
            Session(conn, addr, cfg).start()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            srv.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
