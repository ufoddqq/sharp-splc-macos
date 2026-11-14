# How we know this printer cannot print PDF, PCL or PostScript

Short version: the machine has exactly one page-description language, and it is SPLC.
This is not a driver problem — adding a driver cannot help, because there is nothing on the
device that understands the bytes a normal driver would send.

Below are the four independent checks, in the order they were run. All can be reproduced in
a few minutes; replace `192.168.1.50` with your printer.

## 1. Ask the printer which interpreters it has (SNMP)

```sh
snmpwalk -v1 -c public 192.168.1.50 1.3.6.1.2.1.43.15.1.1
```

Result:

```
43.15.1.1.5.1.1 = "SPLC"
43.15.1.1.6.1.1 = "45.00.00"
```

One interpreter, named SPLC. No PDF, no PCL, no PostScript. This is the authoritative answer —
it comes from the device itself, not from a datasheet.

## 2. Check installed options on the web interface

```sh
curl --http1.0 http://192.168.1.50/machine_config.html
```

(`--http1.0` matters; the embedded server mishandles HTTP/1.1.)

```
Hardware Installed:        Not Installed
Software Options installed: Not Installed
```

No AR-PB10 (PCL/PS expansion board), no MX-PK10 (PostScript kit). Those are the paid options
that would add other languages.

## 3. Check what Sharp actually sells

From Sharp's own product and driver pages for this family:

| Capability | Requires |
|---|---|
| SPLC | built in |
| PCL6 | AR-PB10 |
| PostScript 3 | AR-PB10 + MX-PK10 |
| macOS driver | AR-PB10 + MX-PK10 (the driver page lists it under optional drivers and states it is unusable without them) |
| Windows SPLC driver | free download — this is where `band1` comes from |

So "official Mac support" exists on paper, and costs two optional kits.

## 4. Look at the Bonjour record

```sh
dns-sd -L "SHARP AR-2221R (00000000)" _ipp._tcp local.
```

This printer does advertise `_ipp._tcp`, which is why people assume AirPrint should work. What
it advertises is an empty shell:

```
txtvers=1 note= product=(SHARP AR-2221R) ty=... usb_MFG=SHARP usb_MDL=AR-2221R
```

No `pdl=`, no `URF=`, no `rp=`. Compare with a printer that really does AirPrint:

```
HP LaserJet MFP M427dw : pdl=image/urf,application/pdf,... rp=ipp/print URF=V1.4,...
Brother DCP-7180DN    : pdl=application/octet-stream,image/urf,image/pwg-raster rp=ipp/print
SHARP AR-2221R        : (identity fields only, no capability fields)
```

That is why macOS never lists it as a network printer while happily auto-detecting the Brother
next to it.

## The experiment that settled it

The first attempt sent a PDF straight to port 9100. Everything looked successful:

- `lpstat` showed the job as completed,
- the FTP/raw transfer returned `226 Transfer complete`,
- the printer's page counter incremented.

The paper in the tray said:

```
the job was canceled.
(Non-printable file type)
```

**The counter increments for error pages too.** Every layer in the chain reports success
because every layer is only responsible for delivering bytes, not for understanding them.
This is the one verification rule worth keeping: print a page with a distinctive mark and go
read it at the output tray.

## Consequence

To print from macOS, the Mac must produce SPLC itself. That is what this project does:
rasterise to 600 dpi 1-bit, QM-encode (JBIG1 / T.82), wrap in SPLC frames, send to port 9100.
