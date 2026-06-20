# Status

Phased build toward full OpenGD77 CPS functionality + AES key management.

## Working now (v0.1)

* **Device detect / connect** — `RADIO_INFO` query, confirms `radioType == 6`
  (MD-UV380/390) before reading or writing.
* **AES-256 key management (the differentiator)** — read, edit and write the
  16-slot AES key store and the TX-key selector, via *Settings → AES Keys*.
  * Robust write: locates/creates the `AESK` block in the custom-data block
    chain in place, preserving sibling blocks; verifies by read-back.
  * Validation: per-slot enable, 64-hex-char enforcement, reversed-byte-order
    note in the UI.
* **Channels — read-only view.** Download shows all in-use channels (name,
  freq, duplex, FM/NFM/DMR mode, tones, power, skip; DMR colour code / timeslot
  / contact / TG-list / encrypt byte as extras). Fields are locked in the editor.
* **Host tests, no hardware** — fake-radio fixture + AES codec round-trip,
  sibling-block preservation, BCD helpers, end-to-end download/edit/upload.
  `python run_tests.py` → 10 passed.

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

## Deferred (next phases)

1. **Channel write.** Banks 1–7 (channels 129–1024) are plain flash writes and
   are straightforward. Bank 0 (channels 1–128) lives in EEPROM, and on
   MD-UV380/390 the CPS `EEPROM` write command is **compiled out**
   (`usb_com.c`: `#else ok = true;`) — it ACKs but writes nothing. The
   flash-backed EEPROM-emulation write path must be mapped first. **This is the
   key open item.**
2. **Zones** (176 B, 80 ch/zone, 68 max) — EEPROM-resident (see item 1).
3. **RX group lists** (80 B, 76 max) — flash.
4. **Digital contacts** (24 B, 1024 max) — flash.
5. **DTMF contacts** (32 B, 63 max) — EEPROM-resident.
6. **General settings** (radio name, DMR ID, …) — EEPROM-resident.
7. **DMR-ID database** (raw flash `0x30000`).
8. Frozen-build note: loading as a module works on the packaged Windows CHIRP;
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
