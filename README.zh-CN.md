# sharp-splc-macos

**让 macOS 能打印夏普 AR-2221R / 2421R / 2821R / 3121R —— 这些只认 SPLC、官方只给
Windows 驱动的机器。**

这类机器没有 Mac 驱动，而且不是「缺个驱动」那么简单：它**没有 PDF / PCL / PostScript
解释器**，除非另购 AR-PB10 / MX-PK10 选购件。它自带且唯一支持的是 **SPLC**
（Sharp Printer Language with Compression）。

本项目在 Mac 上直接生成 SPLC：
PDF → 600 dpi 单色位图 → QM 算术编码（JBIG1 / ITU-T T.82）→ SPLC 帧 → TCP 9100。

| | |
|---|---|
| 验证环境 | SHARP AR-2221R + macOS 14，**实际出纸** |
| 语言 | Python 3（不需要 Xcode，无编译依赖） |
| 接入方式 | CUPS 后端 + 用户级常驻服务 |
| 许可 | 代码 MIT，文档 CC BY 4.0 |

> **唯一要记住的判据：机器计数器 +1 不代表打对了。**
> 这台机器会收下看不懂的作业、打一张错误页，然后照样计数。
> 验证只能去出纸口看纸。

---

## 为什么需要后台服务

macOS 的 CUPS 后端跑在沙盒里（身份 `_lp`）：读不到你的家目录、写不了 `/tmp`、
也 import 不到你的 Python 包。所以后端故意做得很薄——只把 spool 文件通过回环 TCP
转给以你的身份运行的 LaunchAgent，由后者完成渲染和发送。

```
任何 App / lp / 预览
        │  PDF
        ▼
CUPS ──► /usr/libexec/cups/backend/splcprint   （root，沙盒，只做转发）
        │  TCP 127.0.0.1:9123
        ▼
agent.py（LaunchAgent，你的用户身份）
        │  渲染 → 1bpp → QM 编码 → SPLC 帧
        ▼
打印机:9100
```

## 依赖

| 项 | 说明 |
|---|---|
| macOS 12+ | 实测 14.x |
| Python 3 | `/usr/bin/python3` 即可，只用标准库 |
| 打印机可达 TCP 9100 | `nc -z <打印机IP> 9100` 自测 |
| `band1.bin` | 7104 字节静态资源，需从夏普官方驱动里提取（见下） |

## 安装

```sh
git clone https://github.com/ufoddqq/sharp-splc-macos.git
cd sharp-splc-macos

# 1) 从你自己下载的 Windows 驱动里提取静态资源
python3 tools/extract-band1.py /path/to/sd3bnp2.dat -o band1.bin
#    期望：7104 字节，md5 b7404defe761cfd368aa7f2821ba4557

# 2) 安装（只为 CUPS 后端要一次 sudo）
./install.sh 192.168.1.50 --queue
```

`band1.bin` 是厂商数据，**不随仓库分发**。若你的驱动文件结构不同，用
`python3 tools/extract-band1.py <文件> --list` 列出全部帧，按 subtype、大小、md5 自己挑。

## 测试

```sh
python3 "$HOME/Library/Application Support/splcprint/splc.py" --pattern test --send 192.168.1.50
```

会打一张校准页，**真的走纸**，先放一张纸。

然后打正式文档：

```sh
lp -d SHARP_SPLC file.pdf
lp -d SHARP_SPLC -o fit=exact -o tone=photo photo.jpg
```

## 参数

| CUPS 参数 | 取值 | 含义 |
|---|---|---|
| `fit` | `auto`（默认）/ `fit` / `exact` | `auto`：内容放得下就 1:1，只裁白边，放不下才缩放；`fit`：总是缩放适配；`exact`：强制 1:1，超出部分裁掉 |
| `tone` | `text`（默认）/ `photo` | `text`：硬阈值，笔画实心；`photo`：Floyd–Steinberg 抖动 |
| `thresh` | 1–254，默认 200 | `text` 模式的阈值，调低字更细 |
| `copies` | 1–99 | 硬件份数 |

可用画布 4696 × 6382 px @600dpi（198.8 × 270.2 mm），比 A4 略小一圈，
所以 `auto` 通常表现为「裁白边」，而不是「缩小内容」。

## 能做到与做不到

| 项 | 状态 |
|---|---|
| 单面文字 / 表格 | 可用 |
| 照片（`tone=photo`） | 可用，1 位抖动 |
| 多份打印 | 可用（交给机器做硬件份数） |
| 双面 | 已下发 `j=1` 标志，**未逐机型验证**，先试一张 |
| 非 A4 尺寸 | 没有做成参数 |
| 扫描 | 另一套机制，见 [docs/scan-to-mac.md](docs/scan-to-mac.md) |

## 目录

```
splc.py                    SPLC 编码器：帧封装、QM 算术编码、光栅几何
print_job.py               PDF → 位图 → SPLC；也可当命令行工具
agent.py                   回环服务（127.0.0.1:9123），负责渲染与发送
backend/splcprint          CUPS 后端，把 spool 文件转给 agent
install.sh                 安装 / 卸载
tools/extract-band1.py     从夏普驱动中提取静态资源
docs/findings.md           如何证明这台机器打不了 PDF/PCL/PS
docs/scan-to-mac.md        反方向：扫描到 Mac
```

## 这不是「破解」

SPLC 是这台机器的**标配**语言；PCL6 与 PostScript3 才是需要另购 AR-PB10 / MX-PK10 的
选购件，官方 Mac 驱动同时依赖这两样。本项目没有解锁任何东西——只是用它本来就会说的语言。

QM 算术编码按公开的 JBIG1（ITU-T T.82）标准实现；帧结构与几何常数参考了下方开源项目，
并用夏普官方 Windows 驱动交叉核对。

## 致谢

| 项目 | 内容 |
|---|---|
| [zy2n9nzt5b-hue/SharpSPLCBridge](https://github.com/zy2n9nzt5b-hue/SharpSPLCBridge) | 针对 AR-5516N 的 Swift 实现；协议结构与几何参考 |

## 许可

代码 MIT，文档 CC BY 4.0。`band1.bin` 两者都不覆盖——那是夏普的数据，请从你自己的驱动里提取。
