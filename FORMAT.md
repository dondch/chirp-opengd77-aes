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
| VFO A/B channels       | `0x7590`    | two 56-B channel structs (`0x7590 + 56*n`, `CHANNEL_VFO_A=0`); exposed as CHIRP special channels |
| Quick keys             | `0x7524`    | 10 × uint16 LE (long-press keys 0–9). bit15: 0=Contact (value = contact index 1–1024), 1=Menu (`menuId<<10\|entryId<<5\|functionId`). 0x0000/0x8000 = empty. Menu shortcuts preserved verbatim. Exposed in CHIRP (Settings → Quick Keys) |
| Radio-wide settings    | `0x604B`    | 116 B `nonVolatileSettings` blob (see below) |

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

### SATELLITE_TLE block (`dataType=3`, `dataLength=2520`)

25 × 100-byte packed records (`__attribute__((packed))`), then 20 bytes padding.
A record with `name[0]==0` terminates the radio's read. Per record:

```
+0   name              8 bytes (ASCII, not necessarily NUL-terminated)
+8   TLE_Line1         12 bytes (compressed)
+20  TLE_Line2         28 bytes (compressed)
+48  rxFreq1           u32 LE   voice downlink, 10-Hz units (Hz/10)
+52  txFreq1           u32 LE   voice uplink
+56  txCTCSS1          u16 LE   TX CTCSS, BCD of tone×10 (67.0 Hz -> 0x0670); 0 = none
+58  armCTCSS1         u16 LE   arming CTCSS (e.g. SO-50 74.4 -> 0x0744)
+60  rxFreq2 / txFreq2 2×u32 LE APRS/data pair
+68  rxFreq3 / txFreq3 2×u32 LE other pair
+76  AdditionalData    24 bytes (spare)
```

**TLE compression** (`satellite.c` `decompressTleData`): each byte holds two
4-bit nibbles indexing `"0123456789. +-*"`, so 12 B → 24 chars (line 1) and
28 B → 56 chars (line 2). The decompressed strings are fixed-field decimal
numbers parsed by `atof` at set offsets:

* line 1 (24 ch): `year(2) + epochDay(12) + firstDeriv(10)` ← std TLE L1
  cols `[18:20] [20:32] [33:43]`.
* line 2 (55 ch + pad): `inclination(8) + RAAN(8) + ecc(7,×1e-7) + argPerigee(8)
  + meanAnomaly(8) + meanMotion(11) + revNumber(5)` ← std TLE L2
  cols `[8:16] [17:25] [26:33] [34:42] [43:51] [52:63] [63:68]`.

The importer appends this block to the custom-data chain (or overwrites in place
if a same-size block exists), refusing if the `OpenGD77` magic is absent.

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

## Radio-wide settings (`nonVolatileSettings`, raw flash `0x604B`)

OpenGD77 keeps its operational settings in a flat `settingsStruct_t` blob, **not**
in the (firmware-ignored) TYT general-settings struct. `settingsStorage.c`:
`STORAGE_BASE_ADDRESS = 0x6000 + 0x4B = 0x604B`, written via `EEPROM_Write` (==
raw SPI flash at offset 0). It is one contiguous `sizeof(settingsStruct_t)` dump
— no wear-levelling — so it is read/written exactly like every other region. The
bytes `0x6000..0x604A` just before it hold unrelated "last used channel in zone"
data, preserved by the per-sector read-modify-write.

* **Size = 116 bytes.** `magicNumber:u32` @0 **must** equal `STORAGE_MAGIC_NUMBER
  = 0x4780`, or `settingsLoadSettings()` factory-resets every setting at boot. The
  driver refuses to write the blob unless that magic is present (anti-reset guard,
  like the AES anti-wipe guard), and preserves `magicNumber` + every unmanaged byte.
* Changes load into RAM only at boot, so they take effect after a **radio reboot**.
* **Enums are 1 byte** (firmware built `-fshort-enums`): a `roaming_t` field
  occupies a single byte. Offsets below are the exact as-built layout of the
  `PLATFORM_VARIANT_UV380_PLUS_10W` firmware, read from the build ELF's DWARF
  (`readelf --debug-dump=info`), not hand-derived.

| Off | Field | Type | Notes |
|-----|-------|------|-------|
| 0   | `magicNumber` | u32 | `0x4780`; preserved, never edited |
| 4   | `location` | 2×u32 | GPS fixed position (radio-managed) |
| 12  | `timezone` | u8 | bits 0-6 = UTC offset (64 = UTC, 15-min steps, UTC-12:00..+14:00), bit 7 = show-local-time flag. Exposed (offset + UTC/Local) |
| 13  | `beepOptions` | u8 | bitmask: 1=TXstart 2=TXstop 4=RXcarrier 8=RXtalker 16=talkerbegin |
| 14  | `vfoSweepSettings` | u16 | packed (not exposed) |
| 16  | `overrideTG` | u32 | runtime |
| 20  | `vfoScanLow[2]` / 28 `vfoScanHigh[2]` | u32 | VFO scan range (runtime) |
| 36  | `bitfieldOptions[1]` | u32 | boolean toggles; bits 30-31 = bank id (preserved) |
| 40  | `aprsBeaconingSettingsPart1[2]` | u32 | APRS (separate feature) |
| 48  | `gpsLogMemOffset` | u32 | runtime |
| 52  | `currentIndexInTRxGroupList[3]` / 58 `currentZone` | i16 | runtime |
| 60  | `userPower` / 62 `tsManualOverride` | u16 | runtime |
| 68  | `aprsBeaconingSettingsPart2` | u16 | APRS |
| 70  | `txPowerLevel` | u8 | default TX power (power-table index) |
| 71  | `txTimeoutBeepX5Secs` | u8 | ×5 s; 0 = off |
| 72  | `beepVolumeDivider` | u8 | |
| 73  | `micGainDMR` / 74 `micGainFM` | u8 | zero points 5 / 4 |
| 75  | `backlightMode` | u8 | Auto/Squelch/Manual/Buttons/None |
| 76  | `backLightTimeout` | u8 | seconds; 0 = never |
| 77  | `displayContrast` | i8 | |
| 78  | `displayBacklightPercentage[2]` | i8 | [0]=day [1]=night |
| 80  | `displayBacklightPercentageOff` | i8 | |
| 81  | `initialMenuNumber` | u8 | (not exposed) |
| 82  | `extendedInfosOnScreen` | u8 | Off/TS/Power/Both |
| 83  | `txFreqLimited` | u8 | band limits: None/Legacy/From-CPS |
| 84  | `scanModePause` | u8 | Hold/Pause/Stop |
| 85  | `scanDelay` / 88 `scanStepTime` | u8 | |
| 86  | `dmrRxAGC` | u8 | |
| 87  | `hotspotType` | u8 | Off/MMDVM/BlueDV |
| 89  | `currentVFONumber` | u8 | runtime |
| 90  | `dmrDestinationFilter` | u8 | None/TG/Contact/TGlist |
| 91  | `dmrCaptureTimeout` | u8 | seconds |
| 92  | `dmrCcTsFilter` | u8 | None/CC(1)/TS(2)/CC+TS(3) |
| 93  | `analogFilterLevel` | u8 | None/CTCSS-DCS |
| 94  | `privateCalls` | u8 | Off/On/PTT |
| 95  | `contactDisplayPriority` | u8 | CC/DB/TA orderings |
| 96  | `splitContact` | u8 | Single/Two-lines/Auto |
| 97  | `voxThreshold` | u8 | 0 = disabled |
| 98  | `voxTailUnits` | u8 | ×500 ms |
| 99  | `audioPromptMode` | u8 | Silent/Beep/NoKeyBeep/Voice1-3 |
| 100 | `temperatureCalibration` | i8 | factory cal (not exposed) |
| 101 | `batteryCalibration` | u8 | factory cal (not exposed) |
| 102 | `squelchDefaults[3]` | u8 | VHF / 220 / UHF |
| 105 | `ecoLevel` | u8 | 0..5 |
| 106 | `apo` | u8 | ×30 min; 0 = off |
| 107 | `keypadTimerLong` / 108 `keypadTimerRepeat` | u8 | |
| 109 | `autolockTimer` | u8 | minutes; 0 = off |
| 110 | `roaming` | u8 (enum) | Off/Manual/5/10/20 km |
| 111 | `gpsModeAndBaudsIndex` | u8 | low nibble = GPS mode (0=not-detected,1=Off,2=On,3=NMEA,4=Log), high nibble = baud index. Mode exposed; baud preserved |
| 112 | `lastTalkerOnScreenTimer` | u8 | seconds 0..30 |

`bitfieldOptions[0]` boolean toggles exposed (bit → meaning): 0 inverse video,
1 PTT latch, 3 battery voltage in header, 5 TX/RX freq lock, 6 all LEDs off,
7 scan on boot, 9 satellite auto, 12 ignore DMR CRC, 13 APO counts RF, 14 safe
power-on, 15 auto-night, 19 secondary language, 21 channel distance, 25 TX
inhibit, 27 channels read-only, 28 double-height UI. Bits 30-31 (storage bank id)
and every unexposed bit are preserved on write.
