# Scanning: printer → Mac

The other direction is much easier than printing, and needs no custom encoder at all — it is
pure configuration on the printer plus an FTP receiver on the Mac.

> This page is a cookbook, not code. The receiver used on the reference machine is a small
> Python FTP service with A4 normalisation; it is not part of this repository (yet). Open an
> issue if you want it published.

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

Any FTP server that accepts a plain upload works. Two practical notes:

1. **Bind it to the LAN address, not `0.0.0.0`**, and put it behind macOS firewall rules that
   only allow the printer's IP.
2. **Normalise page size after arrival** if you want uniform A4. Rewriting just the PDF
   `MediaBox` plus a scale matrix is enough — do not re-encode the images, or quality drops and
   a 300 dpi page takes seconds instead of milliseconds. Make the step idempotent so a second
   pass does not shrink an already-normalised file.

A simple layout that works well:

```
~/Scans/incoming/    ← FTP writes here
~/Scans/             ← normalised, date-named PDFs land here
```

## Verifying

Scan one page with a distinctive word on it, then check both:

- the file exists and opens,
- the page is the size and orientation you expect.

Do not trust the panel's "sending complete" message alone — like the print direction, it only
reports that bytes left the device.
