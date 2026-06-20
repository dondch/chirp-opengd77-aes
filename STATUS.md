# Status

Phased build toward full OpenGD77 CPS functionality + AES key management.

## Working now (v0.2)

* **Device detect / connect** — `RADIO_INFO` query, confirms `radioType == 6`
  (MD-UV380/390) before reading or writing.
* **AES-256 key management (the differentiator)** — read, edit and write the
  16-slot AES key store and the TX-key selector, via *Settings → AES Keys*.
  * Robust write: locates/creates the `AESK` block in the custom-data block
    chain in place, preserving sibling blocks; verifies by read-back.
  * Validation: per-slot enable, 64-hex-char enforcement, reversed-byte-order
    note in the UI.
* **Channels — read & write** (memories 1–1024, analog + DMR). Name, freq,
  duplex/offset, FM/NFM/DMR mode, CTCSS/DCS tones, power, skip, plus DMR colour
  code / timeslot / contact / TG-list / encrypt byte (Extra tab). Upload writes
  only the flash sectors that changed and preserves OpenGD77-specific
  per-channel fields CHIRP doesn't expose. Covers both the EEPROM bank
  (channels 1–128) and the flash banks (129–1024) — the EEPROM region is just
  SPI flash at offset 0, written via the flash protocol.
* **General settings — callsign + DMR ID** (Settings → Radio), read/write.
  DMR ID is big-endian BCD; callsign is an 8-char padded string. (More
  general-settings fields — boot text, toggles — to follow.)
* **Zones → CHIRP banks** (read/write). A channel can belong to several zones
  (MTOBankModel). Add/remove channels, rename, create new zones (up to 68).
  Auto-detects the 80- vs 16-channel-per-zone format. Channels keep their order
  within a zone.
* **Digital contacts** (read/write) — Settings → Contacts lists in-use contacts
  plus spare slots; each has name, TG/ID number (big-endian BCD) and call type
  (Group/Private/All). A channel's **Contact** field is now a name **dropdown**.
* **RX-group lists** (read) — a channel's **RX group list** field is a name
  dropdown. (RX-group membership editing to follow.)
* **DTMF contacts** (read/write) — Settings → DTMF Contacts (name + code,
  digits 0-9 A-D * #).
* **Boot screen** (read/write) — boot text line 1 / line 2 and the boot screen
  type (Picture/Text), in Settings → Radio.
* **Host tests, no hardware** — fake-radio fixture + AES codec round-trip,
  sibling-block preservation, BCD helpers, channel encode/decode round-trips,
  diff-only sector writes, unmanaged-byte preservation, general-settings
  round-trip, zone create/membership/rename/multi-zone, contact read/create,
  RX-group read, channel contact/TG dropdowns, DTMF read/create, boot text.
  `python run_tests.py` → 29 passed.

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

## Write mechanism — solved

The earlier channel-write blocker is resolved. On MD-UV380/390 the "EEPROM"
region is simply **SPI flash at offset 0** (`EEPROM.c`:
`EEPROM_Write(addr) -> SPI_Flash_write(addr + 0)`). The dedicated EEPROM write
command is compiled out, but every codeplug region — EEPROM-resident or not —
is written with the flash `'X'` prepare/send/commit at its raw address.  So the
remaining objects below need only struct encode/decode + a settings UI; there
is no write-path blocker.

## Deferred (next phases)

1. **RX-group membership editing** (read + channel dropdown done; editing the
   group's contact list pending).
2. **General settings** — callsign, DMR ID and boot screen done; misc toggles
   (monitor/VOX/timers, the flag bytes at `0x00FA`-`0x00FD`) pending.
3. **DMR-ID database** (raw flash `0x30000`) — large callsign-lookup table.
4. Frozen-build note: loading as a module works on the packaged Windows CHIRP;
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
