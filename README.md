# chirp-opengd77-aes

A loadable [CHIRP](https://chirpmyradio.com) module for radios running the
**OpenGD77-AES256** firmware (a private OpenGD77 fork for the TYT MD-UV380 /
UV390 "10W Plus" that adds stock-interoperable DMRA **AES-256** voice
encryption).

The headline feature is **AES-256 key management** — reading, editing and
writing the radio's 16-slot AES key store and the active TX key — which no other
cross-platform GUI provides. Full codeplug editing (channels, zones, contacts,
…) is being added incrementally; see [STATUS.md](STATUS.md).

This is **not a fork of CHIRP**. It is a single self-contained driver file you
load into stock, official CHIRP via *File → Load Module…*. You keep your normal
CHIRP install and updates.

## Requirements

* Official CHIRP (a recent build), installed normally.
* A TYT MD-UV380/UV390 running the OpenGD77-AES256 firmware. It enumerates as a
  USB CDC serial port (VID:PID `1FC9:0094`, shown as "OpenGD77 (COMx)").

## Install / load

1. In CHIRP, enable **Help → Developer Mode** and restart CHIRP.
   (The flag is tied to the CHIRP version, so re-enable it after a CHIRP update.)
2. **File → Load Module…**, accept the warning, and choose `opengd77_aes.py`.
   The title bar shows *"CHIRP Module Loaded"* (red) while it is active.
3. The radio now appears in **Radio → Download from radio** as
   **OpenGD77 / MD-UV380/390 (AES)**.

A module must be re-loaded each time you start CHIRP.

## AES key workflow

1. **Radio → Download from radio** (pick the COM port). This reads the codeplug,
   including the AES key store. The driver confirms it is talking to an
   MD-UV380/390 via the firmware's `RADIO_INFO` query.
2. Open **Settings → AES Keys**. You get:
   * **TX key id (0 = encrypted TX off)** — the key id used when transmitting.
   * **Key id 1…15** — each with an *enabled* checkbox and a **64 hex character**
     (32-byte) key field.
3. Edit keys / TX selector, then **Radio → Upload to radio**.

## Channels

Channels (memories 1–1024, analog and DMR) are read and written through the
normal CHIRP memory editor, including per-channel **power** (OpenGD77 levels:
Master / 50 mW … 10 W / Max). Each memory's *Extra* tab adds time-out timer,
VOX, squelch, all-scan skip, DMR colour code, timeslot, **Contact (TX talkgroup)**
and **RX group list** (name dropdowns), **per-channel AES encryption** (Inherit
global TX key / Key 1-15 / Off), and a per-channel DMR ID. Upload writes only the
flash sectors that actually changed and preserves OpenGD77-specific per-channel
fields CHIRP doesn't expose.

> Per-channel encryption and the per-channel DMR ID share one byte in the
> codeplug, so they're mutually exclusive — setting a DMR ID disables the
> per-channel key. (Per-channel encryption is a new, not-yet-fully-tested
> OpenGD77-AES firmware feature.)

> Digital/DTMF contacts, RX-group lists and the DMR-ID database are not written
> yet — see [STATUS.md](STATUS.md).

## Zones

Zones appear as **banks** in CHIRP's bank view. A channel can belong to several
zones; you can add/remove channels, rename a zone, and create new zones. The
Banks tab shows your in-use zones plus a few spare slots for new ones (reload
the tab for more spares once those are used). The driver auto-detects the
radio's 80- or 16-channels-per-zone format.

### Key byte order (important)

Keys are entered and displayed in the **radio / CPS byte order**, which is the
**reverse** of the byte order in an `aes256.dec` file. Enter the 64 hex
characters exactly as the radio/CPS shows them (MSB first). If you have a key
from an `aes256.dec`, reverse its 32 bytes before entering it here.

## Contacts & RX groups

*Settings → Contacts* lists the in-use digital contacts (plus a few spare slots
for adding new ones); each has a name, a TG/ID number and a call type
(Group/Private/All). *Settings → RX Groups* edits TG lists (name + member
contact indices). Contacts and RX groups also populate the per-channel dropdowns
described above.

*Settings → DTMF Contacts* manages DTMF contacts (name + a code of 0-9, A-D,
\* and #).

## DMR-ID database

The **DMR-ID database** (caller-ID lookup) can be imported from a
radioid.net-style CSV: in *Settings → Radio*, set **Import DMR-ID DB from CSV**
to the file path and click **Upload**. The driver builds the radio's DB format
(4-byte BCD id + plain text, sorted by id) and writes it to flash. Up to ~10,900
entries fit, so **pre-filter** large exports (e.g. by country/region); reboot
the radio afterwards to load it. The same panel shows the current entry count.
For very large databases, the OpenGD77 CPS downloader is still an option.

## General settings

*Settings → Radio* exposes the radio **callsign**, **DMR ID**, and the **boot
screen** (two text lines + Picture/Text type) — all read/write. More
general-settings fields and the DMR-ID database are on the way.

## How it works

The driver speaks the OpenGD77 CPS USB protocol (single-byte `R`/`X`/`C`
commands over CDC ACM at 115200). AES keys live in a standard OpenGD77
custom-data block (`type=6`, `AESK`, 584 bytes) in the SPI-flash custom-data
region at raw address `0x20000`. Writes use the firmware's proven
prepare-sector / send-data / commit sequence and preserve any sibling
custom-data blocks (themes, boot screen, …). See [FORMAT.md](FORMAT.md) for the
full, source-derived format.

## Development & tests

The driver is developed against a CHIRP source checkout but ships as a single
file. Host tests need no radio — a fake-radio fixture emulates the USB protocol
over an in-memory flash image.

```sh
# point CHIRP_SRC at a CHIRP checkout (defaults to a sibling ../chirp)
CHIRP_SRC=/path/to/chirp python run_tests.py      # no pytest needed
# or, if you have pytest:
CHIRP_SRC=/path/to/chirp pytest -q
```

## Legal

AES voice encryption is only legal on licensed **commercial / PMR** allocations.
It is **not** permitted on amateur (ham) bands in essentially every
jurisdiction. Use these keys only where you are licensed to do so.

## License

GPLv3, matching CHIRP. See the header in `opengd77_aes.py`.
