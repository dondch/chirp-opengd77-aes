# Status

Phased build toward full OpenGD77 CPS functionality + AES key management.

## Working now (v0.2)

* **Device detect / connect** — `RADIO_INFO` query, confirms `radioType == 6`
  (MD-UV380/390) before reading or writing.
* **AES-256 key management (the differentiator)** — read, edit and write the
  AES key store and the TX-key selector, via *Settings → AES Keys*.
  * Keys are listed/addressed by **key id 1..15** (matching the channel and
    TX selectors; key id 0 = off/inherit), independent of the physical storage
    slot — so a radio that stores key id 1 in slot 0 shows as "Key id 1".
  * Robust write: locates/creates the `AESK` block in the custom-data block
    chain in place, preserving sibling blocks; verifies by read-back.
  * Validation: per-key enable, 64-hex-char enforcement, key-entry note in
    the UI (enter keys exactly as shown in TYT CPS).
* **Channels — read & write** (memories 1–1024, analog + DMR). Name, freq,
  duplex/offset, FM/NFM/DMR mode, CTCSS/DCS tones, per-channel **power**
  (OpenGD77 `libreDMR_Power` levels: Master/50mW…10W/Max), zone-skip; Extra tab
  adds time-out timer, VOX, squelch, all-scan skip, DMR colour code / timeslot /
  contact (name dropdown) / RX-group (name dropdown) / **per-channel AES
  encryption** (Inherit global TX key / Key 1-15 / Off) / per-channel DMR ID.
  Upload writes only the flash sectors that changed and preserves
  OpenGD77-specific bytes CHIRP doesn't expose. Covers the EEPROM bank
  (channels 1–128) and the flash banks (129–1024) — the EEPROM region is just
  SPI flash at offset 0, written via the flash protocol.
* **VFO A/B** (read/write) — surfaced as CHIRP **special channels** (the memory
  editor's "VFO A"/"VFO B" rows). They are two `CodeplugChannel_t` structs at
  EEPROM `0x7590` and reuse the full channel codec (freq, mode, tones, power,
  DMR extras); they have no editable name and can't be deleted.
* **Quick Keys** (read/write) — Settings → Quick Keys. The OpenGD77 long-press
  number-key (0–9) shortcuts (codeplug `0x7524`, 10 × uint16). Each key is a
  dropdown: (empty) or a contact (dials that TG). Menu shortcuts assigned on the
  radio are shown as `Menu shortcut 0xXXXX` and preserved byte-exact (the menu
  function-ID tree isn't decoded). Shares flash sector 7 with boot text/VFO;
  the per-sector RMW keeps all of it intact.
* **Satellite (TLE) import** — Settings → Radio, set a TLE-file path (CelesTrak
  `amateur.txt` etc.); on Upload the matching FM birds (built-in freq table: ISS,
  SO-50, AO-91/92/27, PO-101) are encoded into the OpenGD77 `SATELLITE_TLE`
  custom-data block (type 3, 25 × 100 B) and written into the 64 KB custom-data
  block chain — appended, or overwritten in place. Anti-wipe guard (needs the
  `OpenGD77` magic); preserves the AES + other blocks. Lights up the radio's
  Satellite menu (needs a GPS fix / location for pass prediction).
* **General settings — callsign + DMR ID** (Settings → Radio), read/write.
  DMR ID is big-endian BCD; callsign is an 8-char padded string.
* **Radio-wide settings** (Settings → Radio settings), read/write — the OpenGD77
  `nonVolatileSettings` blob (116 B at raw flash `0x604B`), grouped into Audio /
  Display / Scan / DMR / Power & timers / Options sub-tabs (~50 controls):
  TX power, TX-timeout beep, backlight mode/timeout/brightness, contrast, squelch
  defaults, beep/mic gain, VOX, scan resume/delay/step, DMR filters/AGC,
  private-calls, contact-display, APO, eco, keypad/autolock timers, roaming,
  hotspot type, band limits, GPS mode (Off/On/NMEA/Log — low nibble of
  `gpsModeAndBaudsIndex`, baud nibble preserved), time zone (UTC offset in 15-min
  steps + UTC/Local flag, packed in the `timezone` byte), plus boolean toggles (PTT latch, inverse video,
  TX/RX freq lock, LEDs off, scan-on-boot, TX inhibit, channels read-only,
  auto-night, …). Patches only the bytes that changed; preserves `magicNumber`
  (anti factory-reset guard) and all unmanaged bytes. **Takes effect after a
  radio reboot** (settings load to RAM at boot). Excludes factory calibration
  and radio-managed runtime state (current zone/VFO, GPS position).
* **Zones → CHIRP banks** (read/write). A channel can belong to several zones
  (MTOBankModel). Add/remove channels, rename, create new zones (up to 68). The
  Banks tab shows in-use zones plus a few spare slots (not all 68) and uses a
  cached image + channel→zone reverse map, so it loads instantly. Auto-detects
  the 80- vs 16-channel-per-zone format; channel order within a zone is kept.
* **Digital contacts** (read/write) — Settings → Contacts lists in-use contacts
  plus spare slots; each has name, TG/ID number (big-endian BCD) and call type
  (Group/Private/All). A channel's **Contact** field is now a name **dropdown**.
* **RX-group lists** (read/write) — Settings → RX Groups (name + member contact
  indices); a channel's **RX group list** field is a name dropdown.
* **DTMF contacts** (read/write) — Settings → DTMF Contacts (name + code,
  digits 0-9 A-D * #).
* **Boot screen** (read/write) — boot text line 1 / line 2 and the boot screen
  type (Picture/Text), in Settings → Radio.
* **DMR-ID database (caller-ID lookup)** — import from a radioid.net-style CSV:
  Settings → Radio, set the CSV path, then Upload. Builds the firmware's
  4-byte-BCD + plain-text DB (sorted by id), writes it to flash area 1
  (`0x50000`), and shows the entry count. Up to ~10.9k entries (pre-filter
  larger lists); reboot the radio to load it.
* **Host tests, no hardware** — fake-radio fixture + AES codec round-trip,
  sibling-block preservation, BCD helpers, channel encode/decode round-trips,
  diff-only sector writes, unmanaged-byte preservation, general-settings
  round-trip, zone create/membership/rename/multi-zone, contact read/create,
  RX-group read/edit, channel contact/TG dropdowns, DTMF read/create, boot text,
  DMR-ID DB status, bank-count limit + membership cache, tuning steps, DMR-ID
  CSV import (parse/build/upload), per-channel power + extras round-trip,
  per-channel encryption (round-trip, off, DMR-ID exclusion), radio-wide settings
  (decode incl. signed/enum/bitfield, edit round-trip, neighbour + magic + bank-bit
  preservation, no-clobber without magic), VFO A/B special channels (listed,
  empty-when-blank, decode + edit round-trip, name preserved, per-slot write,
  not-deletable), and a guard that every settings/extras dropdown is built with
  `current_index` (no `RadioSettingValueList` FutureWarning), Quick Keys
  (decode contact/menu/empty, set contact, clear, menu + erased-empty preserved
  byte-exact, neighbour preserved), GPS mode round-trip (baud nibble preserved),
  satellite TLE encode (real ISS TLE round-trips to correct orbital elements,
  freqs/CTCSS) + chain-write (append preserving AES, overwrite in place, refuse
  without magic), time-zone round-trip (offset + local-flag bit preserved), and a
  guard that every settings/extras item has an explanation tooltip (`set_doc`).
  `python run_tests.py` → 74 passed.

## On-hardware test result (2026-06-20, COM4)

Verified against a real MD-UV390 10W Plus running OpenGD77-AES256
(git `c543c86`, built 20260620143151):

* **Detect:** `RADIO_INFO` returns `radioType=6` — confirmed. ✔
* **AES read:** custom-data region read OK (magic `OpenGD77`, AES block at +12);
  existing store read back correctly (`tx_key_id=1`, KEY1 in slot 0). ✔
* **AES write (low-level + driver path):** wrote a test key into a free slot,
  read-back byte-exact, test key persisted to flash, existing KEY1 untouched,
  sibling region preserved — via both the raw protocol and the
  `set_settings`→`sync_out` GUI path. ✔
* **Channel read:** full `sync_in` OK; decoded the in-use channel
  (`PMR01`, 446.00625 MHz, DMR). ✔
* **Restore:** original AES sector written back and verified byte-exact; radio
  left exactly as found (only KEY1 present). ✔

Still requires a human (RF/functional, can't be automated here): reboot the
radio and confirm KEY1 still decrypts a stock encrypted call after a real
key-edit upload. The byte-exact restore shows KEY1 is unmodified by the
round-trip.

**Channel write (2026-06-21, COM4):** created a test channel in a free slot
(`ZZTEST`, 145.500 MHz, −0.6 MHz shift, CTCSS 88.5, FM), uploaded (only the one
changed EEPROM sector written), read back byte-exact, existing PMR01 untouched;
the EEPROM channel sectors were then restored byte-exact. ✔

**General settings + zones (2026-06-21, COM4):** callsign (`GD77`) and DMR ID
decoded correctly. Zone format detected as 80-ch; the radio's existing zone
(`Zone1` → ch 1) read correctly; created `ZZTESTZONE` → ch 1 in a free slot,
read back, then restored the zone sectors byte-exact. ✔

**Contacts + RX groups (2026-06-21, COM4):** read the radio's 6 contacts
(e.g. `Parrot 9990`/Private, `OpenGD77 TG`/98977, `DCH_Group`/9661) and 2 RX
groups (`Brandmeister`, `DMR MARC`) — names, BCD numbers and call types all
correct; channel 1's Contact dropdown correctly resolved to `6: DCH_Group`.
(Read-only check; contact write uses the same proven flash path + host tests.) ✔

**DTMF + boot (2026-06-21, COM4):** boot text read as `Radioddity` / `GD-77`,
screen type `Text`; 0 DTMF contacts on the radio (none programmed). DTMF/boot
write use the same proven flash path + host tests. ✔

**RX-group edit + DMR-ID status (2026-06-21, COM4):** read the radio's RX groups
(`Brandmeister` → [1,4,5,3], `DMR MARC` → [1]); created `ZZRXGRP` → [6] in a
free slot, read back, restored the RX-group sectors byte-exact. DMR-ID DB status
read as "not loaded" (the radio has no DB downloaded). ✔

**DMR-ID DB import (2026-06-21, COM4):** imported a 3-entry CSV, wrote the DB to
`0x50000`, read it back (`Id` magic, count 3), firmware-style lookups resolved
`2342001 → G4ABC John` and `3101234 → W1AW Hiram`, then restored the DB sector
byte-exact. ✔

**Per-channel power + extras (2026-06-21, COM4):** PMR01 read its real power
(`1W`, via `libreDMR_Power` — the previous flag-based mapping was wrong); a test
channel round-tripped power `5W`, TOT `120 s`, VOX on, squelch `3`, CC `5`,
TS `2`; EEPROM channel sectors restored byte-exact. ✔

**Per-channel encryption (2026-06-21, COM4):** wrote a channel with
`encrypt = Key 3`; our read and the firmware's own `0x83` subcommand both
reported `(3, 0)`. (PMR01 already uses `Key 1`.) Sectors restored byte-exact. ✔

## Write mechanism — solved

The earlier channel-write blocker is resolved. On MD-UV380/390 the "EEPROM"
region is simply **SPI flash at offset 0** (`EEPROM.c`:
`EEPROM_Write(addr) -> SPI_Flash_write(addr + 0)`). The dedicated EEPROM write
command is compiled out, but every codeplug region — EEPROM-resident or not —
is written with the flash `'X'` prepare/send/commit at its raw address.  So the
remaining objects below need only struct encode/decode + a settings UI; there
is no write-path blocker.

## Deferred (next phases)

1. **Larger DMR-ID databases** — area 1 (~10.9k entries) is supported via CSV
   import. Going beyond needs the 3-byte-id + 6-bit-compressed format and area 2
   (`0xD8000`); for very large DBs use the OpenGD77 CPS downloader.
2. Frozen-build note: loading as a module works on the packaged Windows CHIRP;
   only a *from-source frozen rebuild* would also need the module added to
   `chirp/drivers/__init__.py:__all__` (not required for Load Module).

## On-hardware test checklist (user runs; needs the radio)

AES key management — the priority — should be verified end-to-end:

1. Load the module (Help → Developer Mode → restart → File → Load Module…).
2. **Radio → Download from radio**; confirm it connects and reports the radio.
3. **Settings → AES Keys**: confirm existing keys/TX selector read back correctly
   (compare against `aes_key_store.py --show`).
4. Edit a key in an unused slot, set the TX key id, **Upload to radio**.
   Confirm the "read-back verify" succeeds (no error dialog).
5. Reboot the radio. Download again; confirm the edited key/TX selector persisted.
6. Confirm a previously-working key (e.g. KEY1) still decrypts a stock encrypted
   call after the round-trip (no regression to the existing store).
7. Sanity: confirm sibling custom-data (boot screen / theme, if present) is
   intact after the AES write.

Radio-wide settings (do on a free/known setting; back up the sector first):

1. Download; **Settings → Radio settings** — confirm values match the radio's
   own menus (e.g. backlight mode/timeout, squelch, scan, VOX, hotspot type).
2. Change one obvious setting (e.g. backlight timeout, or a toggle like
   "Inverse video"), **Upload**, then **reboot the radio** and confirm it took
   effect on the radio and that other settings/menus are unchanged.
3. Confirm the radio did **not** factory-reset its settings (magic preserved).
   If `magicNumber` were ever wrong the radio resets all settings at boot — the
   write is guarded against this, but verify once on hardware.
4. Back up sector 6 (raw `0x6000`, 4 KB) before testing and restore byte-exact
   afterwards, per the usual procedure.
