# OpenGD77-AES on-radio menu map

Reference for the OpenGD77-AES firmware menu tree on the **TYT MD-UV380/390 10W
Plus** (`PLATFORM_MDUV380` / `PLATFORM_VARIANT_UV380_PLUS_10W`, colour display +
GPS + soft-volume builds). Derived from the firmware sources
(`user_interface/menu*.c`, `menuSystem.c/.h`). Items listed are those compiled in
for this variant; a few are gated to other radios and noted where relevant.

This also documents how a **Quick Key "menu shortcut"** decodes: the 16-bit value
is `(1<<15) | (menuId<<10) | (entryId<<5) | functionId`, where *menuId* is the
`MENU_SCREENS` id below and *entryId* is the item's position (0-based) in that
menu's list. See FORMAT.md for the quick-key block layout.

## Navigation
- **Channel mode** and **VFO mode** are the two home screens (toggle between
  them). Long-pressing a number key on a home screen runs a **Quick Key**.
- **Menu key** → **Main Menu**.
- **Orange button** (top side key on the MD-UV380/390) on a home screen → that
  screen's **Quick Menu** (transient actions incl. scan types — not stored).
- **Long-press the front Up arrow** (without SK2) → **start scanning**.
- **SK1** held shows the channel-info overlay; **SK2** is the modifier key.

## Main Menu
`menuId` values are exact for this build (HAS_GPS + HAS_COLOURS, non-GD77S);
they shift on other variants because `MENU_GPS`/`MENU_THEME` etc. are
conditional. Quick keys can target `menuId` 0–31; on this build the addressable
menus run 0–25.

| Item | menuId | Description |
|---|---|---|
| Zone | 2 | Select the active zone |
| Contacts | 1 | Submenu: contact lists + new contact |
| Channel Details | 23 | Edit the current channel (fields below) |
| RSSI | 4 | Live signal-strength meter (viewer) |
| Firmware info | 24 | Version / git hash / build date (viewer) |
| Options | 6 | Settings submenus (below) |
| Last Heard | 5 | Recently received DMR stations (viewer) |
| Radio Infos | 3 | Battery / time / date / location / temperature / battery graph (viewer) |
| Satellite | 11 | Satellite pass prediction & tracking |
| GPS | 12 | GPS fix / satellites / mode (viewer) |

### Contacts (menuId 1)
| Item | Description |
|---|---|
| Contact list | Digital (DMR) contacts |
| DTMF contact list | DTMF code entries |
| New contact | Create a digital contact |

## Options
Top-level entries: General, Radio, Display, Sound, Language, Calibration, Theme,
APRS.

### General options (menuId 7)
| # | Item | Accepted values |
|---|---|---|
| 0 | Keypad timer (long) | Long-press threshold, seconds in 0.1 steps |
| 1 | Keypad timer (repeat) | Key auto-repeat rate, seconds in 0.1 steps |
| 2 | Auto-lock | Keypad auto-lock, minutes (0 = off) |
| 3 | Hotspot mode | Off / MMDVM / BlueDV |
| 4 | Temperature calibration | ±°C trim (0.5 steps) |
| 5 | Battery calibration | ±0.0x V trim |
| 6 | Eco level | 0–5 (power-save aggressiveness) |
| 7 | Safe power-on | On / Off (require long-press to power on) |
| 8 | Auto power off (APO) | Off, then 0.5–12 h (30-min steps) |
| 9 | APO with RF | On / Off (RF activity resets the APO timer) |
| 10 | Satellite (manual/auto) | Manual / Auto prediction |
| 11 | GPS | Off / On / NMEA / Log / Not detected |
| 12 | Channels read-only | On / Off (lock channel edits from the keypad) |

*(Not on this build: Trackball [MD2017], Poweroff-suspend [non-STM32F405].)*

### Radio options (menuId 8)
| # | Item | Accepted values |
|---|---|---|
| 0 | TX frequency limits | On / Off on the radio (CPS exposes None / Legacy default / From CPS) |
| 1 | TX inhibit | On / Off (block all transmit) |
| 2 | DMR monitor capture timeout | Seconds |
| 3 | Scan delay | Seconds to pause on a busy channel |
| 4 | Scan step time | Dwell per channel (ms) |
| 5 | Scan mode | Hold / Pause / Stop |
| 6 | Scan on boot | On / Off |
| 7 | Squelch default VHF | 0–100 % (5 % steps) |
| 8 | Squelch default 220 MHz | 0–100 % (5 % steps) |
| 9 | Squelch default UHF | 0–100 % (5 % steps) |
| 10 | PTT toggle | On / Off (tap to start/stop TX) |
| 11 | Private calls | Off / On / PTT / Auto |
| 12 | User power | Custom TX power value |
| 13 | DMR CRC | On (enforce) / Off (ignore) |

*(Not on this build: Force 10W [non-PLUS variants].)*

### Display options (menuId 9)
| # | Item | Accepted values |
|---|---|---|
| 0 | Display style | Font height 1 (normal) / 2 (double) |
| 1 | Brightness (day) | 0–100 % |
| 2 | Brightness (night) | 0–100 % |
| 3 | Brightness (off) | 0–100 % |
| 4 | Contrast | Numeric |
| 5 | Backlight mode | Auto / Squelch / Manual / Buttons / None |
| 6 | Backlight timeout | Seconds (No / n.a. in some modes) |
| 7 | Screen invert | Normal / Invert |
| 8 | Auto night | On / Off |
| 9 | Contact display order | CC>DB>TA / DB>CC>TA / TA>CC>DB / TA>DB>CC |
| 10 | Split contact | Single line / Two lines / Auto |
| 11 | Time in header | On / Off |
| 12 | Battery unit in header | Percent / Voltage |
| 13 | Extended infos | Off / Timeslot / Power / Both |
| 14 | Visual volume | On / Off |
| 15 | All LEDs | On / Off |
| 16 | Timezone value | UTC offset |
| 17 | Time UTC or local | UTC / Local |
| 18 | Show distance | On / Off (needs location) |
| 19 | DMR last talker on screen | Seconds (0 = off) |

### Sound options (menuId 10)
| # | Item | Accepted values |
|---|---|---|
| 0 | Timeout beep | Seconds before TX-timeout (×5 s; n.a. if TOT off) |
| 1 | Beep volume | dB (3 dB steps) |
| 2 | DMR beep | None / Start / Stop / Both (TX) |
| 3 | RX beep | None / Carrier / Talker / Both |
| 4 | RX talker-begin beep | End only / Both |
| 5 | Mic gain DMR | dB (3 dB steps, relative to default) |
| 6 | Mic gain FM | dB (3 dB steps, relative to default) |
| 7 | VOX threshold | 0 (off) – n |
| 8 | VOX tail | Seconds (0.x) |
| 9 | Audio prompt mode | Silent / Beep / No-key-beep / Voice 1 / Voice 2 / Voice 3 |
| 10 | DMR RX AGC | Off, then dB (3 dB steps) |

*(Not on this build: Speaker-click suppress [MD9600].)*

### Other Options entries
| Item | menuId | Description |
|---|---|---|
| Language | 18 | UI language picker |
| Calibration | 19 | RF / hardware calibration (advanced; avoid casually) |
| Theme | 20 | Colour theme editor (day / night palettes) |
| APRS | 25 | APRS beacon / config profiles |

The full contacts area also has its own screens: Contact list (menuId 13),
DTMF contact list (14), and the channel/VFO Quick Menus (UI_CHANNEL_QUICK_MENU
21, UI_VFO_QUICK_MENU 22).

## Channel Details (menuId 23)
Edits the channel (or VFO) currently in use. Fields marked *n.a.* are hidden for
the other mode (FM vs DMR).

| # | Field | Accepted values |
|---|---|---|
| 0 | Name | Text |
| 1 | RX frequency | MHz |
| 2 | TX frequency | MHz |
| 3 | Mode | FM / DMR |
| 4 | Use location | Yes / No (n.a. on VFO) |
| 5 | Latitude | Coordinate / n.a. |
| 6 | Longitude | Coordinate / n.a. |
| 7 | DMR ID | Per-channel ID / None / n.a. (FM) |
| 8 | Colour code | 0–15 / n.a. (FM) |
| 9 | Timeslot | 1 / 2 / n.a. (FM) |
| 10 | RX group | List name / None / n.a. (FM) |
| 11 | Contact (TX TG) | Contact name / None / n.a. (FM) |
| 12 | RX CSS | CTCSS Hz / DCS / None / n.a. (DMR) |
| 13 | TX CSS | CTCSS Hz / DCS / None / n.a. (DMR) |
| 14 | Bandwidth | 12.5 / 25 kHz / n.a. (DMR) |
| 15 | Frequency step | kHz |
| 16 | TOT (time-out timer) | Seconds (×15) / Off |
| 17 | RX only | Yes / No |
| 18 | Zone skip | Yes / No |
| 19 | All skip | Yes / No |
| 20 | VOX | On / Off |
| 21 | Power | From master / level + unit (50 mW … 10 W / +W-) |
| 22 | Squelch | From master / 0–100 % (5 % steps) |
| 23 | Beep | Yes / No (per-channel) |
| 24 | Eco | Yes / No (per-channel) |
| 25 | TA TX TS1 | Off / APRS / TA text / Both |
| 26 | TA TX TS2 | Off / APRS / TA text / Both |
| 27 | APRS config | Profile name / None |
| 28 | Force DMO | Yes / No / n.a. (FM) |

## Scanning
Scanning is a **mode**, not a menu item. Its **settings** live in
**Options → Radio** (Scan delay, Scan step time, Scan mode = Hold/Pause/Stop,
Scan on boot).
- **Start:** long-press the front **Up** arrow (without SK2) on the Channel or
  VFO screen; or enable **Scan on boot**; or assign a Quick Key to the **Start
  scanning** function.
- **Channel mode** scans the active **zone** (channels not flagged Zone-skip or
  All-skip). **VFO mode** scans the tuned **frequency range**.
- **While scanning:** **Up/Down** reverse the direction; on a pause the current
  channel can be nuisance-deleted for the rest of the scan; the **Scan mode**
  setting decides whether it holds / pauses / stops on activity.
- **Stop:** press the **Red/Back** key.
- Extra scan types from the **Quick Menu**: **DMR CC scan** (auto-detect colour
  code), **Tone scan** (VFO — find the incoming CTCSS/DCS), **Dual watch**
  (VFO — alternate between VFO A and B).

## Quick Menus
Opened with the **Orange button** (top side key) on a home screen — transient
actions, nothing stored to the codeplug. Item # = quick-key `entryId`.

**Channel screen** (`UI_CHANNEL_QUICK_MENU`, menuId 21)
| # | Item | Description |
|---|---|---|
| 0 | Copy → VFO | Copy this channel into the VFO |
| 1 | Copy from VFO | Overwrite this channel from the VFO |
| 2 | Filter FM | FM RX filter (CTCSS/DCS / off) |
| 3 | Filter DMR | DMR RX filter level |
| 4 | DMR CC scan | Auto-detect the colour code |
| 5 | Filter DMR TS | Timeslot filter |
| 6 | Talkaround | TX on the RX frequency (bypass repeater offset) |
| 7 | Roaming | DMR roaming control |
| 8 | Audio mute | Mute the speaker |

**VFO screen** (`UI_VFO_QUICK_MENU`, menuId 22)
| # | Item | Description |
|---|---|---|
| 0 | VFO A/B | Switch VFO A ↔ B |
| 1 | TX↔RX swap | Swap the TX and RX frequencies |
| 2 | Both → RX | Set TX = RX (simplex) |
| 3 | Both → TX | Set RX = TX |
| 4 | Filter FM | FM RX filter |
| 5 | Filter DMR | DMR RX filter level |
| 6 | DMR CC scan | Auto-detect the colour code |
| 7 | Filter DMR TS | Timeslot filter |
| 8 | VFO → new channel | Save the VFO as a new memory |
| 9 | Tone scan | Find the incoming CTCSS/DCS tone |
| 10 | Dual watch | Alternate-monitor VFO A and B |
| 11 | Freq bind mode | How a typed frequency binds (RX / TX / both) |
| 12 | Audio mute | Mute the speaker |

## Quick-key direct functions
Besides a menu shortcut or a contact, a Quick Key can run a bare **function**
(`menuId` 0 + `functionId`): **Start scanning** (1), **Toggle torch** (2),
**Redraw** (3) — `enum QUICK_FUNCTIONS` in `menuSystem.h`.

## Operational screens (not user-navigable)
These `MENU_SCREENS` exist for the firmware's own flow and aren't reached by
browsing: TX screen, Lock screen, Private call, Message box, Hotspot mode
(MMDVM/BlueDV), CPS mode, Numerical entry, Splash, Power-off.

## Mapping to the CHIRP module
- **Options → General / Radio / Display / Sound** = the `nonVolatileSettings`
  fields in CHIRP's **Settings** tab (see FORMAT.md, `0x604B`).
- **Channel Details** = CHIRP's Memory editor + the per-channel "OpenGD77" Extra
  tab (encryption, TOT, VOX, CC/TS, contact, etc.).
- **Quick Keys** = CHIRP's **Settings → Quick Keys**; "Contact" quick keys are
  editable, "Menu shortcut" quick keys are decoded via the `menuId`/`entryId`
  tables above.
