# OpenGD77 (MD-UV380/390) codeplug & AES key format

Derived from the OpenGD77-AES256 firmware (`MDUV380_firmware/application/source/functions/codeplug.c`,
`.../include/functions/codeplug.h`, `.../source/usb/usb_com.c`) and the `tools/aes_key_store.py`
reference tool. All addresses verified against firmware source — **not guessed**.

This document is the authoritative reference for `opengd77_aes.py`.

## USB transport

* CDC ACM serial, **VID:PID `1FC9:0094`**, 115200 baud. Commands are single ASCII bytes; the
  reply echoes the command byte.
* `CPS_ACCESS_AREA`: `FLASH=1`, `EEPROM=2`, `MCU_ROM=5`, `DISPLAY_BUFFER=6`, `WAV_BUFFER=7`,
  `RADIO_INFO=9`, `FLASH_SECURITY_REGISTERS=10`.
* **Read** `R`: `['R', area, addr(4, BE), len(2, BE)]` → `['R', ?, ?, <data...>]` (data starts at
  reply byte 3). Max ~1024 bytes/read.
* **Flash write** `X` (read-modify-write of a 4 KB sector; untouched bytes in the sector are
  preserved by the firmware):
  1. prepare sector: `['X', 1, sector(3, BE)]`  (sector = addr // 4096)
  2. send data:      `['X', 2, addr(4, BE), len(2, BE), <data>]`  (repeat; <=~1024/chunk)
  3. commit:         `['X', 3]`  (erase sector + write back the buffer)

  An error reply begins with `'-'` (0x2D).
* **EEPROM write** `['X', 4, addr(4,BE), len(2,BE), <data>]`: **compiled out on MD-UV380/390**
  (firmware `#else ok = true;` — it ACKs but writes nothing). EEPROM-resident codeplug data on
  this platform is written through a flash-backed emulation path (TODO: map for channel-write phase).
* `RADIO_INFO` (read, area 9) → packed struct:
  `structVersion:u32 (=3)`, `radioType:u32`, `gitRevision[16]`, `buildDateTime[16]`,
  `flashId:u32`, `features:u16`. **`radioType == 6` ⇒ MD-UV380/390.** Used to confirm the radio.
* CPS subcommands via `C`: `0x80` set+persist AES key `[keyId, 32B]`; `0x81` select TX key
  `[keyId]` (0 = enc TX off); `0x86` load key to RAM only. `'C',0` shows the CPS screen; `'C',5` closes.

## Flash address mapping

`FLASH_ADDRESS_OFFSET = 0x20000` (128 KiB) on MD-UV380/390. **Raw SPI-flash address =
`0x20000 + codeplug-relative address`.** The `R`/`X` commands take *raw* addresses.

| Region                | Codeplug-rel | Raw flash addr | Notes |
|-----------------------|--------------|----------------|-------|
| Calibration           | —            | `0x10000`      | local copy, special-cased |
| Custom-data region    | `0x00000`    | `0x20000`      | magic + blocks (AES keys live here) |
| DMR-ID database       | `0x10000`    | `0x30000`      | |
| Channels (flash banks)| `0x7B1B0`    | `0x9B1B0`      | banks 1..7 (channels 129..1024) |
| Contacts              | `0x87620`    | `0xA7620`      | 1024 × 24 B |
| RX group lists        | `0x8D620`    | `0xAD620`      | 76 lists |

EEPROM-resident (read via area 2; addresses are EEPROM-relative):

| Region                 | EEPROM addr | Notes |
|------------------------|-------------|-------|
| General settings       | `0x00E0`    | 40 B; radioName, DMR ID, etc. |
| Channel bank-0 bitmap  | `0x3780`    | 16 B (128 bits) |
| Channels bank 0        | `0x3790`    | channels 1..128, 56 B each |
| DTMF contacts          | `0x02F88`   | 63 × 32 B |
| Zones (in-use bitmap)  | `0x8010`    | |
| Zones list             | `0x8030`    | 68 zones, 176 B each (OpenGD77 format, 80 ch/zone) |
| VFO A/B channels       | `0x7590`    | two 56-B channel structs |
| Quick keys             | `0x7524`    | protected by firmware on write |

## Custom-data region (raw `0x20000`)

```
+0   "OpenGD77"            8-byte magic
+8   reserved             4 bytes (0xFF...)
+12  block[0]             {dataType:u32 LE, dataLength:u32 LE} then dataLength payload bytes
     block[1]             ...
     ...                  type 0xFFFFFFFF / dataLength 0 marks end
```
`CodeplugCustomDataType_t`: `IMAGE=1, BEEP=2, SATELLITE_TLE=3, THEME_DAY=4, THEME_NIGHT=5,
AES_KEYS=6, EMPTY=0xFFFFFFFF`. Block size is fixed once allocated (firmware refuses to resize).

### AES_KEYS block (`dataType=6`, `dataLength=584`)

```
+0   "AESK"               4-byte magic
+4   version              u8 (=1)
+5   txKeyId              u8  (0 = encrypted TX off; else the keyId to transmit with)
+6   reserved             2 bytes
+8   slot[0..15]          16 × 36 bytes:
        +0  valid         u8 (1 = slot populated)
        +1  keyId         u8 (logical DMRA key id, 0..15)
        +2  reserved      2 bytes
        +4  key           32 bytes (AES-256)
```
Total payload = 8 + 16×36 = **584**. Region image written for a fresh store =
`magic(12) + {6,584} header(8) + payload(584)` = 604 bytes at `0x20000`.

**Key byte order:** the radio/CPS key bytes are the **reverse** of the `aes256.dec` byte order.
Keys are entered/displayed in the radio (CPS) order; the UI documents this.

## Channel record (56 bytes, `CODEPLUG_CHANNEL_DATA_STRUCT_SIZE`)

1024 channels in 8 banks of 128. Bank 0 (1..128) in EEPROM; banks 1..7 (129..1024) in flash.
Each bank is preceded by a 16-byte in-use bitmap (`bit (i%128)` of bank `i//128`, LSB-first per byte).
Flash bank stride = `16 + 128*56 = 7184`; bank `b` (1..7) bitmap at `0x9B1B0 + (b-1)*7184`, data +16.

| off | type      | field            | encoding |
|-----|-----------|------------------|----------|
| 0   | char[16]  | name             | 0xFF-padded |
| 16  | u32       | rxFreq           | BCD, value = freq_Hz / 10 (e.g. 0x14652000 = 146.520 MHz) |
| 20  | u32       | txFreq           | BCD, /10 |
| 24  | u8        | chMode           | 0 = analog/FM, 1 = digital/DMR |
| 25  | u8        | libreDMR_Power    | per-channel power (0 = use global) |
| 26  | u8        | locationLat0     | |
| 27  | u8        | tot              | time-out timer |
| 28-29 | u8×2    | locationLat1/2   | |
| 30-31 | u8×2    | locationLon0/1   | |
| 32  | u16       | rxTone           | CSS: CTCSS=BCD (×10 Hz); DCS uses bit15/bit14 flags; 0xFFFF = none |
| 34  | u16       | txTone           | CSS, as rxTone |
| 36  | u8        | locationLon2     | |
| 37  | u8        | _UNUSED_1        | |
| 38  | u8        | LibreDMR_flag1   | bit7 OPTIONAL_DMRID, bit6 NO_BEEP, bit5 NO_ECO, bit4 OOB, bit3 USE_LOCATION, bit2 FORCE_DMO, bit0 ROAMING |
| 39  | u8        | rxSignaling      | repurposed: optional-DMRID byte[2] when flag1 bit7 set |
| 40  | u8        | artsInterval     | repurposed: optional-DMRID byte[1] when flag1 bit7 set |
| 41  | u8        | encrypt          | privacy / key selector; repurposed: optional-DMRID byte[0] when flag1 bit7 set |
| 42  | u8        | _UNUSED_2        | |
| 43  | u8        | rxGroupList      | TG-list index (1-based; 0 = none) |
| 44  | u8        | txColor          | DMR colour code |
| 45  | u8        | aprsConfigIndex  | |
| 46  | u16       | contact          | digital contact index (1-based) |
| 48  | u8        | flag1            | low nibble = TA-TX control |
| 49  | u8        | flag2            | bit6 TIMESLOT_TWO (DMR TS) |
| 50  | u8        | flag3            | bit6 STE (0xC0), bit5 NON_STE |
| 51  | u8        | flag4            | bit7 POWER, bit6 VOX, bit5 ZONE_SKIP, bit4 ALL_SKIP, bit2 RX_ONLY, bit1 BW_25K, bit0 SQUELCH |
| 52  | u16       | VFOoffsetFreq    | |
| 54  | u8        | VFOflag5         | upper nibble = step index |
| 55  | u8        | sql              | squelch (0..21) |

Optional per-channel DMR ID: when `flag1(LibreDMR_flag1) bit7` set,
`dmrID = (rxSignaling<<16)|(artsInterval<<8)|encrypt`.

## Other structures (sizes; layout to be detailed in their build phases)

* Zone: in-use bitmap (32 B) at EEPROM `0x8010`; list at `0x8030`, stride
  `16 + 2*cpz` per zone (`cpz` = 80 OpenGD77 / 16 legacy, detected from the byte
  at `0x806F` ≤ 0x04 ⇒ 80). Each zone = `name[16]` + `channels[cpz]:u16`
  (1-based channel index, 0 = end). Max 68 zones + a virtual "All channels".
* RX group list: 80 B — `name[16]` + `contacts[32]:u16`. Max 76.
* Digital contact: 24 B — `name[16]` + `tgNumber:u32` + `callType:u8` + `callRxTone:u8` +
  `ringStyle:u8` + `reserve1:u8 (TS override)`. Max 1024.
* DTMF contact: 32 B — `name[16]` + `code[16]`. Max 63.
* General settings: 40 B — `radioName[8]` (callsign) + `radioId:u32` (DMR ID) + flags…
