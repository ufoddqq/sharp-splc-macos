# Scanning: printer → Mac

The other direction is much easier than printing, and needs no custom encoder at all — it is
pure configuration on the printer plus an FTP receiver on the Mac.

The receiving side is included in `scan/`:

| File | What |
|---|---|
| `scan/scan_receiver.py` | FTP server, standard library only. Implements login + upload, refuses delete/rename |
| `scan/a4_normalize.py` | incremental PDF update that turns any page into A4 without re-encoding images |
| `scan/install-scan.sh` | installs the receiver as a LaunchDaemon on port 21, generates a password |

## Why it "doesn't work" out of the box

The panel's scan destinations are configurable, but the factory default key is **Sharpdesk**.
Pressing it makes the printer wait for a Windows client to come and pull the image — the panel
shows `nEt`. A Mac never comes, so the scan appears to vanish.

The fix is to stop pressing the Sharpdesk key and use one of the numbered scan keys instead.

## Panel procedure

| Step | Key | Screen |
|---|---|---|
| 1 | **CA** (clear all, top right) | leaves `nEt`, back to `0` |
| 2 | **Scan**, then a number key `1`–`5` | shows `1`…`5` |
| 3 | place the original, press **Start** | paper feeds, then the file is pushed to the Mac |

Keys `1`–`5` are separate destinations/profiles (resolution, colour, format). Key `6` is
Sharpdesk — do not use it on a Mac.

If pressing Scan shows `nEt` again, step 1 was skipped: the panel kept the Sharpdesk mode.

## Printer side (web interface)

Under **Scan setup** (exact wording varies by firmware):

| Setting | Value |
|---|---|
| Destination | FTP |
| Host | the Mac's LAN IP |
| Port | 21 |
| Directory | where the receiver writes files |
| Format | PDF |
| Resolution | 300 dpi is a good default |
| Colour mode | depends on key 1–5 |
| File naming | Date & Time (avoids overwrites) |

The printer's own **original size** setting is auto: put A3 in and you get A3 out. The web
interface has no target-size option for scanning, so page size has to be normalised on the
receiving side if you always want A4.

## Mac side

```sh
sudo ./scan/install-scan.sh                       # ~/Scans, random password, prints it
sudo ./scan/install-scan.sh --dir /Volumes/Data/Scans --password 'something-long'
sudo ./scan/install-scan.sh --uninstall
```

It writes `/Library/LaunchDaemons/com.sharp.scanreceiver.plist`, starts the receiver, and
prints exactly what to type on the printer panel. Two practical notes:

1. **Restrict who can reach it.** The daemon listens on all interfaces, because the printer may
   reach the Mac through a different interface than the default route does. Add a macOS
   firewall rule that only allows the printer's IP; if you know the exact address, pass
   `--bind` to `scan_receiver.py` to limit it further.
2. **Normalise page size after arrival** if you want uniform A4. `a4_normalize.py` only rewrites
   the PDF `MediaBox` plus a scale matrix — images are never re-encoded, so a 300 dpi page takes
   milliseconds and loses nothing. The step is idempotent: an already-normalised file is left
   alone.

The password is generated with `secrets.token_urlsafe(9)` when you do not pass `--password`,
and is stored in the LaunchDaemon plist. Plain FTP means no TLS — treat the password as
LAN-only material.

Files land directly in the target directory (`~/Scans` by default) and are normalised in place;
no separate incoming folder is needed, because the receiver writes to a temporary name and
renames only after the upload completes.

## Verifying

Scan one page with a distinctive word on it, then check both:

- the file exists and opens,
- the page is the size and orientation you expect.

Do not trust the panel's "sending complete" message alone — like the print direction, it only
reports that bytes left the device.
