# sharp-splc-macos

**Print from macOS to Sharp AR-2221R / 2421R / 2821R / 3121R — the printers that only
speak SPLC, for which Sharp ships a Windows-only driver.**

There is no Mac driver for these machines, and there cannot be one made of standard parts:
the printer has no PDF, PCL or PostScript interpreter unless you buy the AR-PB10 / MX-PK10
options. What it *does* have, built in, is **SPLC** (Sharp Printer Language with Compression).

This project generates SPLC on the Mac: PDF → 600 dpi 1-bit raster → QM arithmetic coding
(JBIG1 / ITU-T T.82) → SPLC frames → TCP port 9100.

| | |
|---|---|
| Verified on | SHARP AR-2221R, macOS 14, real paper out of the tray |
| Language | Python 3 (no Xcode, no compiled dependencies) |
| Integration | CUPS backend + a small user-level agent |
| License | MIT (code), CC BY 4.0 (docs) |

> **The one rule that matters:** a page counter incrementing does *not* mean the page
> printed. This printer happily accepts a job it cannot interpret, prints an error page,
> and still counts it. Always verify at the output tray.

---

## Why a background agent

macOS runs CUPS backends in a sandbox as `_lp`: the backend cannot read your home
directory, cannot write to `/tmp`, and cannot import your Python packages. So the backend is
deliberately thin — it forwards the spool file over loopback TCP to a LaunchAgent running as
you, which does the rendering and talks to the printer.

```
lp / Preview / any app
        │  PDF
        ▼
CUPS  ──►  /usr/libexec/cups/backend/splcprint   (root, sandboxed, does nothing but forward)
        │  TCP 127.0.0.1:9123
        ▼
agent.py (LaunchAgent, your user)
        │  render → 1bpp → QM encode → SPLC frames
        ▼
printer:9100
```

## Requirements

| Item | Notes |
|---|---|
| macOS 12+ | tested on 14.x |
| Python 3 | `/usr/bin/python3` is enough; only the standard library is used |
| Printer reachable over TCP 9100 | check with `nc -z <printer-ip> 9100` |
| `band1.bin` | 7104-byte static resource, extracted from Sharp's own driver — see below |

## Install

```sh
git clone https://github.com/ufoddqq/sharp-splc-macos.git
cd sharp-splc-macos

# 1. get the static resource out of the Windows driver you downloaded from Sharp
python3 tools/extract-band1.py /path/to/sd3bnp2.dat -o band1.bin
#    expected: 7104 bytes, md5 b7404defe761cfd368aa7f2821ba4557

# 2. install (asks for sudo once, for the CUPS backend)
./install.sh 192.168.1.50 --queue
```

`band1.bin` is vendor data and is **not** redistributed here. If your driver file has a
different layout, `python3 tools/extract-band1.py <file> --list` prints every SPLC frame with
its subtype, size and md5.

## Test

```sh
python3 "$HOME/Library/Application Support/splcprint/splc.py" --pattern test --send 192.168.1.50
```

This prints a calibration page. It really uses paper — put a sheet in the tray first.

Then a real document:

```sh
lp -d SHARP_SPLC file.pdf
lp -d SHARP_SPLC -o fit=exact -o tone=photo photo.jpg
```

## Options

| CUPS option | Values | Meaning |
|---|---|---|
| `fit` | `auto` (default), `fit`, `exact` | `auto`: 1:1 if the ink fits the printable canvas, otherwise scale down. `fit`: always scale. `exact`: force 1:1, crop the overflow |
| `tone` | `text` (default), `photo` | `text`: hard threshold, solid strokes. `photo`: Floyd–Steinberg dithering |
| `thresh` | 1–254, default 200 | threshold for `tone=text`; lower = thinner strokes |
| `copies` | 1–99 | hardware copies |

Printable canvas is 4696 × 6382 px at 600 dpi (198.8 × 270.2 mm), slightly smaller than A4, so
`auto` usually trims the white margin instead of shrinking the content.

## What works, what does not

| | Status |
|---|---|
| Single-sided text/table printing | works |
| Photos (`tone=photo`) | works, dithered 1-bit |
| Multiple copies | works (passed to the printer as a hardware copy count) |
| Duplex | the flag is sent (`j=1` in the setup string); not validated on every model — try one sheet |
| Non-A4 paper sizes | not exposed as an option |
| Scanning | included: `scan/` runs an FTP receiver on the Mac (see below) |

## Scanning (printer → Mac)

Scanning is a completely different mechanism: the printer pushes the image, so the Mac only
has to offer an FTP destination. No custom encoder involved.

```sh
sudo ./scan/install-scan.sh                        # ~/Scans, random password, printed for you
sudo ./scan/install-scan.sh --dir /Volumes/Data/Scans --password 'something-long'
sudo ./scan/install-scan.sh --uninstall
```

It installs `/Library/LaunchDaemons/com.sharp.scanreceiver.plist` (port 21 needs root), starts
the receiver, and prints the exact values to type on the printer panel. Received PDFs are
normalised to A4 in place by rewriting only the `MediaBox` and wrapping the content in a
scale matrix — images are never re-encoded, and a second pass leaves an A4 file untouched.

Panel side, in short: press **CA** to leave `nEt`, then **Scan** and a number key `1`–`5`.
Key `6` is Sharpdesk, which waits for a Windows client that will never arrive — that is the
`nEt` people get stuck on. Details in [docs/scan-to-mac.md](docs/scan-to-mac.md).

> Plain FTP, no TLS. Keep it on a trusted LAN, and set a real password.

## Repository layout

```
splc.py                    SPLC encoder: frames, QM arithmetic coding, raster geometry
print_job.py               PDF → raster → SPLC; also usable as a CLI
agent.py                   loopback service (127.0.0.1:9123) that renders and sends
backend/splcprint          CUPS backend, forwards the spool file to the agent
install.sh                 installer / uninstaller
tools/extract-band1.py     pulls the vendor static resource out of a Sharp driver
scan/scan_receiver.py      FTP receiver (stdlib only); login + upload, no delete/rename
scan/a4_normalize.py       A4 normalisation via PDF incremental update
scan/install-scan.sh       installs the receiver as a LaunchDaemon on port 21
docs/findings.md           how we know the printer cannot print PDF/PCL/PS
docs/scan-to-mac.md        the scan direction in detail
```

## Why this is not a "crack"

SPLC is the printer's **standard** language. PCL6 and PostScript3 are the paid options
(AR-PB10, MX-PK10); the Mac driver Sharp offers requires both. This project does not unlock
anything — it generates the language the machine already speaks, so no optional kit is
needed.

The QM arithmetic coder is implemented from the public JBIG1 (ITU-T T.82) specification. The
frame structure and geometry constants were derived from the open-source Swift project below
and cross-checked against Sharp's Windows driver.

## Credits

| Project | What |
|---|---|
| [zy2n9nzt5b-hue/SharpSPLCBridge](https://github.com/zy2n9nzt5b-hue/SharpSPLCBridge) | Swift SPLC implementation for AR-5516N; protocol structure and geometry reference |

## License

Code: MIT. Documentation: CC BY 4.0. `band1.bin` is not covered by either — it is Sharp's
data, obtained from your own driver copy.
