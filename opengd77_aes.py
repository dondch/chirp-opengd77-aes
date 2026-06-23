# Copyright 2026 dondch <dondch@users.noreply.github.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""CHIRP driver for radios running the OpenGD77-AES256 firmware.

OpenGD77-AES256 is a private fork of OpenGD77 for the TYT MD-UV380/UV390 (10W
"Plus") that adds stock-interoperable DMRA AES-256 voice encryption.  The
headline feature of this driver is **AES-256 key management** -- reading,
editing and writing the radio's AES key store -- which no other cross-platform
GUI offers.  Codeplug channel viewing is also provided; channel editing and the
remaining codeplug objects (zones, contacts, RX groups, ...) are in progress.

This file is designed to be loaded into stock CHIRP via
*File -> Load Module...* (enable Help -> Developer Mode first).  It is fully
self-contained: drop it in and the radio appears in Radio -> Download.

The USB protocol and on-flash formats are ported from the OpenGD77-AES256
firmware (``usb_com.c``, ``codeplug.c``) and the firmware's ``aes_key_store.py``
reference tool.  See FORMAT.md for the derivation.  The radio enumerates as a
USB CDC ACM port, VID:PID 1FC9:0094, 115200 baud.
"""

import logging
import re
import struct
import time

from chirp import chirp_common, directory, errors, memmap, util
from chirp import bitwise  # noqa: F401  (used by future channel-edit phase)
from chirp.settings import (
    RadioSettings,
    RadioSettingGroup,
    RadioSetting,
    RadioSettingValueBoolean,
    RadioSettingValueInteger,
    RadioSettingValueList,
    RadioSettingValueString,
)

LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Protocol / format constants (verified against firmware -- see FORMAT.md)
# --------------------------------------------------------------------------
USB_VID = 0x1FC9
USB_PID = 0x0094
BAUD = 115200

# CPS_ACCESS_AREA
AREA_FLASH = 1
AREA_EEPROM = 2
AREA_RADIO_INFO = 9

RADIO_TYPE_MDUV380 = 6  # radioInfo.radioType for MD-UV380/390

FLASH_OFFSET = 0x20000          # FLASH_ADDRESS_OFFSET on MD-UV380/390
SECTOR_SIZE = 4096

# Custom-data region (raw flash) that holds the AES key block
CUSTOM_DATA_ADDR = FLASH_OFFSET                      # 0x20000
CUSTOM_MAGIC = b"OpenGD77"
CUSTOM_HDR_LEN = 12                                  # magic(8) + reserved(4)
CUSTOM_TYPE_AES_KEYS = 6
CUSTOM_TYPE_SATELLITE = 3                            # SATELLITE_TLE block
CUSTOM_TYPE_EMPTY = 0xFFFFFFFF

# AES key block
AESK_MAGIC = b"AESK"
AESK_VERSION = 1
AESK_NUM_SLOTS = 16
AESK_SLOT_SIZE = 36                                  # valid,keyId,rsvd2,key32
AESK_HDR_LEN = 8                                     # "AESK"+ver+tx+rsvd2
AESK_PAYLOAD_LEN = AESK_HDR_LEN + AESK_NUM_SLOTS * AESK_SLOT_SIZE  # 584
AESK_KEY_LEN = 32

# Channels
CH_SIZE = 56
CH_PER_BANK = 128
CH_BANKS = 8
CH_MAX = 1024
EE_CH_BITMAP_ADDR = 0x3780
EE_CH_DATA_ADDR = 0x3790
FLASH_CH_BITMAP_ADDR = FLASH_OFFSET + 0x7B1B0        # 0x9B1B0 (bank 1 bitmap)
FLASH_BANK_STRIDE = 16 + CH_PER_BANK * CH_SIZE       # 7184

# VFO A/B: two CodeplugChannel_t (56 B) structs at EEPROM 0x7590, indexed by
# Channel_t (CHANNEL_VFO_A = 0, CHANNEL_VFO_B = 1).  Surfaced as CHIRP special
# channels.  Their synthetic memory numbers sit just above the normal range.
VFO_ADDR = 0x7590
VFO_NAMES = ["VFO A", "VFO B"]
VFO_A_NUMBER = CH_MAX + 1

# Quick keys: 10 * uint16 (LE) at EEPROM 0x7524 -- the long-press number-key
# (0-9) shortcuts.  Each 16-bit value: bit15 = type (0 = Contact, 1 = Menu).
# A Contact quick key stores the contact index (1..1024); a Menu quick key packs
# (menuId<<10 | entryId<<5 | functionId) and is version-specific, so we preserve
# those verbatim rather than decode them.  0x0000/0x8000 = empty/cleared.
QUICKKEYS_ADDR = 0x7524
QUICKKEYS_COUNT = 10
QUICKKEYS_LEN = QUICKKEYS_COUNT * 2                  # 20 bytes


def _quickkey_is_empty(v):
    """True if @v should be shown as an empty quick-key slot (matches the
    firmware's notion plus the erased-flash value)."""
    return (v in (0x0000, 0x8000, 0xFFFF) or
            (not (v & 0x8000) and not (1 <= v <= CONTACTS_MAX)))

# General settings (EEPROM region == raw flash offset 0)
GENSET_ADDR = 0x00E0            # callsign[8] @0xE0, radioId(BCD BE)[4] @0xE8
GENSET_LEN = 40

# Zones (EEPROM region). In-use bitmap @0x8010 (32 B), zone list @0x8030.
# Each zone: name[16] + channels[cpz]:u16 (1-based, 0 = end). cpz is 80 for the
# OpenGD77 format (16 for the legacy format), detected from the byte at 0x806F.
ZONE_BITMAP_ADDR = 0x8010
ZONE_BITMAP_LEN = 32
ZONE_LIST_ADDR = 0x8030
ZONES_MAX = 68
ZONE_NAME_LEN = 16
ZONE_MAX_CH = 80
ZONE_STRIDE_MAX = ZONE_NAME_LEN + 2 * ZONE_MAX_CH      # 176 (80-ch format)
ZONE_DETECT_OFF = 0x806F - ZONE_BITMAP_ADDR            # 0x5F into the region
ZONE_REGION_LEN = (ZONE_LIST_ADDR - ZONE_BITMAP_ADDR) + ZONES_MAX * ZONE_STRIDE_MAX

# Digital contacts (flash). name[16] + tgNumber(BCD BE)[4] + callType + 2 misc.
# In use when name[0] != 0xFF.
CONTACT_ADDR = FLASH_OFFSET + 0x87620                   # 0xA7620
CONTACT_SIZE = 24
CONTACTS_MAX = 1024
CONTACTS_REGION_LEN = CONTACTS_MAX * CONTACT_SIZE       # 24576
CONTACT_TYPE_TG = 0
CONTACT_TYPE_PC = 1
CONTACT_TYPE_ALL = 2
CONTACT_TYPES = ["Group Call", "Private Call", "All Call"]

# RX group lists (flash). Length table (1 byte/group, >0 = in use) then the
# groups (name[16] + contacts[32]:u16, 1-based contact index, 0 = end).
RXGROUP_LEN_ADDR = FLASH_OFFSET + 0x8D620               # 0xAD620
RXGROUP_ADDR = FLASH_OFFSET + 0x8D6A0                   # 0xAD6A0
RXGROUP_SIZE = 80
RXGROUPS_MAX = 76
RXGROUP_REGION_LEN = (RXGROUP_ADDR - RXGROUP_LEN_ADDR) + RXGROUPS_MAX * RXGROUP_SIZE

# DTMF contacts (EEPROM region). name[16] + code[16] (digit per byte, 0xFF=end).
# In use when the first byte is not 0xFF/0x00.
DTMF_ADDR = 0x2F88
DTMF_SIZE = 32
DTMF_MAX = 63
DTMF_REGION_LEN = DTMF_MAX * DTMF_SIZE                  # 2016
DTMF_DIGITS = "0123456789ABCD*#"

# Boot screen (EEPROM region): two text lines + an intro-type byte.
BOOT_LINES_ADDR = 0x7540                                # line1[16] @0x7540, line2[16] @0x7550
BOOT_LINES_LEN = 32
BOOT_INTRO_ADDR = 0x7518                                # 0x01 = text, 0x00 = picture

# DMR-ID database (caller-ID lookup). Header at 0x50000: "Id" magic, byte[2]
# selects ID size (not 'N'/'n' -> 4-byte BCD ids + plain text), byte[3] =
# record length + 0x4A, count at bytes 8..11. Records are sorted ascending by
# id (the firmware binary-searches). This driver writes the simple 4-byte-BCD +
# plain-text format into area 1 only (0x50000..0x90000, before the codeplug).
DMRID_HEADER_ADDR = FLASH_OFFSET + 0x30000             # 0x50000
DMRID_HEADER_LEN = 12
DMRID_CONTACT_LEN = 24                                  # 4-byte id + 20-byte text
DMRID_AREA1_BYTES = 0x40000 - DMRID_HEADER_LEN
DMRID_MAX_ENTRIES = DMRID_AREA1_BYTES // DMRID_CONTACT_LEN

# Radio-wide settings (OpenGD77 nonVolatileSettings, settingsStruct_t).  Stored
# as a flat blob in the EEPROM region (== raw flash at offset 0) at
# STORAGE_BASE_ADDRESS = 0x6000 + 0x4B.  magicNumber (offset 0, u32) must stay
# SETTINGS_MAGIC or the firmware factory-resets every setting on the next boot.
# SETTINGS_LEN and all field offsets are the exact as-built layout of the
# UV380_PLUS_10W firmware, taken from the build's ELF DWARF (see FORMAT.md).
# 0x6000..0x604A holds unrelated "last used channel in zone" data, which the
# per-sector read-modify-write write path preserves.  Changes take effect on the
# next radio reboot (settings load into RAM at boot).
SETTINGS_ADDR = 0x604B
SETTINGS_LEN = 116
SETTINGS_MAGIC = 0x4780
SETTINGS_BITOPTS_OFF = 36       # bitfieldOptions[0] (u32); bits 30-31 = bank id
SETTINGS_GPSMODE_OFF = 111      # gpsModeAndBaudsIndex: low nibble = GPS mode,
#                                 high nibble = baud index (preserved on write)
SETTINGS_TZ_OFF = 12            # timezone byte: bits 0-6 = UTC offset (64 = UTC,
#                                 15-min steps), bit 7 = show-local-time flag

CSS_NONE = 0xFFFF

# --------------------------------------------------------------------------
# Internal CHIRP image layout.  Our .img is a concatenation of the radio
# regions we manage, at fixed offsets.  _RANGES maps each device region to its
# slice of the image.  (offset, area, device_addr, length)
# --------------------------------------------------------------------------
IMG_AES = 0
IMG_EE_BITMAP = IMG_AES + SECTOR_SIZE                # 0x1000
IMG_EE_CH = IMG_EE_BITMAP + 16                       # 0x1010
IMG_FLASH_CH = IMG_EE_CH + CH_PER_BANK * CH_SIZE     # 0x2C10
FLASH_CH_LEN = (CH_BANKS - 1) * FLASH_BANK_STRIDE    # 7 banks = 50288
IMG_GENSET = IMG_FLASH_CH + FLASH_CH_LEN             # 0xF080
IMG_ZONE = IMG_GENSET + GENSET_LEN
IMG_CONTACTS = IMG_ZONE + ZONE_REGION_LEN
IMG_RXGROUP = IMG_CONTACTS + CONTACTS_REGION_LEN
IMG_DTMF = IMG_RXGROUP + RXGROUP_REGION_LEN
IMG_BOOT = IMG_DTMF + DTMF_REGION_LEN
IMG_BOOTTYPE = IMG_BOOT + BOOT_LINES_LEN
IMG_DMRIDHDR = IMG_BOOTTYPE + 1
IMG_SETTINGS = IMG_DMRIDHDR + DMRID_HEADER_LEN
IMG_VFO = IMG_SETTINGS + SETTINGS_LEN
IMG_QUICKKEYS = IMG_VFO + len(VFO_NAMES) * CH_SIZE
IMAGE_SIZE = IMG_QUICKKEYS + QUICKKEYS_LEN

_RANGES = [
    # (image_offset, area, device_addr, length)
    (IMG_AES, AREA_FLASH, CUSTOM_DATA_ADDR, SECTOR_SIZE),
    (IMG_EE_BITMAP, AREA_EEPROM, EE_CH_BITMAP_ADDR, 16),
    (IMG_EE_CH, AREA_EEPROM, EE_CH_DATA_ADDR, CH_PER_BANK * CH_SIZE),
    (IMG_FLASH_CH, AREA_FLASH, FLASH_CH_BITMAP_ADDR, FLASH_CH_LEN),
    (IMG_GENSET, AREA_EEPROM, GENSET_ADDR, GENSET_LEN),
    (IMG_ZONE, AREA_EEPROM, ZONE_BITMAP_ADDR, ZONE_REGION_LEN),
    (IMG_CONTACTS, AREA_FLASH, CONTACT_ADDR, CONTACTS_REGION_LEN),
    (IMG_RXGROUP, AREA_FLASH, RXGROUP_LEN_ADDR, RXGROUP_REGION_LEN),
    (IMG_DTMF, AREA_EEPROM, DTMF_ADDR, DTMF_REGION_LEN),
    (IMG_BOOT, AREA_EEPROM, BOOT_LINES_ADDR, BOOT_LINES_LEN),
    (IMG_BOOTTYPE, AREA_EEPROM, BOOT_INTRO_ADDR, 1),
    (IMG_DMRIDHDR, AREA_FLASH, DMRID_HEADER_ADDR, DMRID_HEADER_LEN),
    (IMG_SETTINGS, AREA_EEPROM, SETTINGS_ADDR, SETTINGS_LEN),
    (IMG_VFO, AREA_EEPROM, VFO_ADDR, len(VFO_NAMES) * CH_SIZE),
    (IMG_QUICKKEYS, AREA_EEPROM, QUICKKEYS_ADDR, QUICKKEYS_LEN),
]

# Writable spans: (image_offset, raw_flash_addr, length).  All written through
# the flash 'X' protocol (EEPROM region included -- it is flash at offset 0).
# The EEPROM bank-0 bitmap (0x3780) + 128 channels (0x3790) are contiguous.
_WRITE_SPANS = [
    (IMG_AES, CUSTOM_DATA_ADDR, SECTOR_SIZE),
    (IMG_EE_BITMAP, EE_CH_BITMAP_ADDR, 16 + CH_PER_BANK * CH_SIZE),
    (IMG_FLASH_CH, FLASH_CH_BITMAP_ADDR, FLASH_CH_LEN),
    (IMG_GENSET, GENSET_ADDR, GENSET_LEN),
    (IMG_ZONE, ZONE_BITMAP_ADDR, ZONE_REGION_LEN),
    (IMG_CONTACTS, CONTACT_ADDR, CONTACTS_REGION_LEN),
    (IMG_RXGROUP, RXGROUP_LEN_ADDR, RXGROUP_REGION_LEN),
    (IMG_DTMF, DTMF_ADDR, DTMF_REGION_LEN),
    (IMG_BOOT, BOOT_LINES_ADDR, BOOT_LINES_LEN),
    (IMG_BOOTTYPE, BOOT_INTRO_ADDR, 1),
    (IMG_SETTINGS, SETTINGS_ADDR, SETTINGS_LEN),
    (IMG_VFO, VFO_ADDR, len(VFO_NAMES) * CH_SIZE),
    (IMG_QUICKKEYS, QUICKKEYS_ADDR, QUICKKEYS_LEN),
]

# --------------------------------------------------------------------------
# Radio-wide settings field model (settingsStruct_t).  Enum option lists are in
# the firmware's enum order (index == stored byte value).  See FORMAT.md.
# --------------------------------------------------------------------------
RS_BACKLIGHT = ["Auto", "Squelch", "Manual", "Buttons", "None"]
RS_HOTSPOT = ["Off", "MMDVM", "BlueDV"]
RS_CONTACT_PRIO = ["CC>DB>TA", "DB>CC>TA", "TA>CC>DB", "TA>DB>CC"]
RS_SCAN_MODE = ["Hold", "Pause", "Stop"]
RS_SPLIT_CONTACT = ["Single line", "Two lines", "Auto"]
RS_PRIVATE_CALLS = ["Off", "On", "PTT only"]
RS_BAND_LIMITS = ["None", "Legacy default", "From CPS"]
RS_INFO_SCREEN = ["Off", "Timeslot", "Power", "Both"]
RS_ROAMING = ["Off", "Manual", "5 km", "10 km", "20 km"]
RS_AUDIO_PROMPT = ["Silent", "Beep", "No key beep", "Voice 1", "Voice 2",
                   "Voice 3"]
RS_DMR_DEST_FILTER = ["None", "TG", "Contact", "TG list"]
RS_DMR_CCTS_FILTER = ["None", "CC", "TS", "CC+TS"]
RS_ANALOG_FILTER = ["None", "CTCSS/DCS"]
# GPS mode = low nibble of gpsModeAndBaudsIndex; values OFF=1, ON=2, NMEA=3,
# LOG=4 (0 = "not detected", a runtime state).  Index here = value - 1.
RS_GPS_MODES = ["Off", "On", "On + NMEA out", "On + logging"]


def _build_tz_offsets():
    """UTC-offset labels for the timezone byte's low 7 bits.  List index i maps
    to stored value 16 + i (64 = UTC, 15-min steps, UTC-12:00 .. UTC+14:00)."""
    out = []
    for tz in range(16, 121):
        total = (tz - 64) * 15                  # minutes from UTC
        if total == 0:
            out.append("UTC")
        else:
            out.append("UTC%s%d:%02d" % ("+" if total > 0 else "-",
                                         abs(total) // 60, abs(total) % 60))
    return out


RS_TZ_OFFSETS = _build_tz_offsets()
RS_TZ_DISPLAY = ["UTC", "Local"]

# Each entry: (subgroup, key, offset, kind, opt, label, doc)
#   kind "int":  opt = (lo, hi)         -- unsigned byte
#   kind "sint": opt = (lo, hi)         -- signed int8
#   kind "list": opt = options list     -- stored byte = index
# Displayed integer ranges widen to fit any out-of-range stored value, so an
# untouched control never clamps/corrupts the stored byte on write-back.
SETTINGS_FIELDS = [
    # Audio
    ("rs_audio", "set_beepOptions", 13, "int", (0, 63),
     "DMR beep events (bitmask)",
     "1=TX start, 2=TX stop, 4=RX carrier, 8=RX talker, 16=talker begin"),
    ("rs_audio", "set_beepVolumeDivider", 72, "int", (0, 10),
     "Beep volume divider", "Key/system beep volume; higher value = quieter."),
    ("rs_audio", "set_micGainDMR", 73, "int", (0, 15),
     "Mic gain, DMR", "Microphone gain for DMR transmit (default = 5)."),
    ("rs_audio", "set_micGainFM", 74, "int", (0, 31),
     "Mic gain, FM", "Microphone gain for FM transmit (default = 4)."),
    ("rs_audio", "set_audioPromptMode", 99, "list", RS_AUDIO_PROMPT,
     "Audio prompts",
     "Feedback for menus/keys: silent, a beep, or spoken voice prompts."),
    ("rs_audio", "set_voxThreshold", 97, "int", (0, 30),
     "VOX threshold", "Voice-activated TX sensitivity; 0 = VOX disabled."),
    ("rs_audio", "set_voxTailUnits", 98, "int", (0, 10),
     "VOX tail", "How long TX stays keyed after you stop talking (units of 500 ms)."),
    # Display
    ("rs_display", "set_backlightMode", 75, "list", RS_BACKLIGHT,
     "Backlight mode",
     "When the backlight is on: Auto (activity), Squelch, Manual, Buttons, None."),
    ("rs_display", "set_backLightTimeout", 76, "int", (0, 255),
     "Backlight timeout (s)",
     "Seconds the backlight stays on after activity; 0 = never time out."),
    ("rs_display", "set_blPercentDay", 78, "int", (0, 100),
     "Backlight brightness, day (%)", "Daytime backlight brightness."),
    ("rs_display", "set_blPercentNight", 79, "int", (0, 100),
     "Backlight brightness, night (%)", "Night-mode backlight brightness."),
    ("rs_display", "set_blPercentOff", 80, "sint", (0, 100),
     "Backlight brightness, off (%)", "Dimmed backlight level when 'off'."),
    ("rs_display", "set_displayContrast", 77, "sint", (0, 63),
     "Display contrast", "LCD contrast."),
    ("rs_display", "set_extendedInfosOnScreen", 82, "list", RS_INFO_SCREEN,
     "Extra info on screen",
     "Extra status on the home screen: timeslot, power, both, or off."),
    ("rs_display", "set_lastTalkerOnScreenTimer", 112, "int", (0, 30),
     "Last-talker on screen (s)",
     "Seconds to keep the last DMR talker shown; 0 = off."),
    # Scan
    ("rs_scan", "set_scanModePause", 84, "list", RS_SCAN_MODE,
     "Scan resume mode",
     "On a busy channel: Hold (stay), Pause (resume after delay), or Stop."),
    ("rs_scan", "set_scanDelay", 85, "int", (0, 30),
     "Scan delay (s)",
     "Seconds to wait on a busy channel before resuming (Pause mode)."),
    ("rs_scan", "set_scanStepTime", 88, "int", (0, 15),
     "Scan step time", "Dwell time on each channel while scanning."),
    # DMR
    ("rs_dmr", "set_dmrDestinationFilter", 90, "list", RS_DMR_DEST_FILTER,
     "DMR destination filter",
     "Which DMR calls open the squelch: none, matching TG, contact, or TG list."),
    ("rs_dmr", "set_dmrCcTsFilter", 92, "list", RS_DMR_CCTS_FILTER,
     "DMR CC/TS filter",
     "Require a matching colour code and/or timeslot to hear a DMR call."),
    ("rs_dmr", "set_dmrCaptureTimeout", 91, "int", (0, 30),
     "DMR capture (hang) time (s)",
     "Seconds the monitor holds a DMR call after it ends."),
    ("rs_dmr", "set_dmrRxAGC", 86, "int", (0, 10), "DMR RX AGC",
     "DMR receive audio automatic gain control."),
    ("rs_dmr", "set_analogFilterLevel", 93, "list", RS_ANALOG_FILTER,
     "Analog filter", "Analog RX squelch by CTCSS/DCS, or none."),
    ("rs_dmr", "set_privateCalls", 94, "list", RS_PRIVATE_CALLS,
     "Private calls", "Allow DMR private calls: off, on, or PTT-only."),
    ("rs_dmr", "set_contactDisplayPriority", 95, "list", RS_CONTACT_PRIO,
     "Contact display priority",
     "Order used to identify a caller (colour code / DB lookup / talker alias)."),
    ("rs_dmr", "set_splitContact", 96, "list", RS_SPLIT_CONTACT,
     "Split contact display",
     "Show long contact names on one line, two lines, or auto."),
    ("rs_dmr", "set_roaming", 110, "list", RS_ROAMING, "DMR roaming",
     "DMR roaming between repeaters in a roaming zone (off/manual/by distance)."),
    # Power & timers
    ("rs_power", "set_txPowerLevel", 70, "int", (0, 9),
     "Default TX power level",
     "Default transmit power (index into the radio's power table)."),
    ("rs_power", "set_txTimeoutBeep", 71, "int", (0, 255),
     "TX timeout warning beep",
     "Warning beep this many x5 s before the TX time-out; 0 = off."),
    ("rs_power", "set_apo", 106, "int", (0, 24),
     "Auto power off", "Switch off after idle (units of 30 min); 0 = off."),
    ("rs_power", "set_ecoLevel", 105, "int", (0, 5),
     "Power-saving (eco) level", "0 = off, 5 = most aggressive power saving."),
    ("rs_power", "set_keypadTimerLong", 107, "int", (0, 15),
     "Keypad long-press time", "How long a key must be held for a long press."),
    ("rs_power", "set_keypadTimerRepeat", 108, "int", (0, 15),
     "Keypad repeat time", "Auto-repeat rate when a key is held down."),
    ("rs_power", "set_autolockTimer", 109, "int", (0, 255),
     "Auto-lock (min)", "Auto-lock the keypad after this many minutes idle; 0 = off."),
    ("rs_power", "set_hotspotType", 87, "list", RS_HOTSPOT,
     "Hotspot mode", "USB hotspot mode when connected to a host (Off/MMDVM/BlueDV)."),
    ("rs_power", "set_txFreqLimited", 83, "list", RS_BAND_LIMITS,
     "Band (TX) limits",
     "Enforce TX band limits: none, legacy defaults, or the CPS limits."),
]

# Boolean toggles packed in bitfieldOptions[0] (u32 @ SETTINGS_BITOPTS_OFF).
# (key, bit, label, doc).  Only bits that exist on the UV380_PLUS_10W build.
SETTINGS_BITS = [
    ("set_bit_inverseVideo", 0, "Inverse video",
     "Inverted display (dark text on light background)."),
    ("set_bit_pttLatch", 1, "PTT latch",
     "Tap PTT to start/stop TX instead of holding it down."),
    ("set_bit_battVoltInHeader", 3, "Show battery voltage in header",
     "Show the battery voltage in the top status bar."),
    ("set_bit_txRxFreqLock", 5, "TX/RX frequency lock",
     "Lock the TX/RX frequencies so they can't be changed from the keypad."),
    ("set_bit_allLedsOff", 6, "Disable all LEDs",
     "Turn off the green/red status LEDs."),
    ("set_bit_scanOnBoot", 7, "Scan on boot",
     "Start scanning automatically at power-on."),
    ("set_bit_satelliteAuto", 9, "Satellite predictions auto",
     "Auto-select the next satellite pass instead of choosing manually."),
    ("set_bit_dmrCrcIgnored", 12, "Ignore DMR CRC",
     "Ignore DMR CRC errors (helps with some non-standard systems)."),
    ("set_bit_apoWithRf", 13, "Auto power off counts RF activity",
     "Let received RF activity reset the auto-power-off timer."),
    ("set_bit_safePowerOn", 14, "Safe power-on (long press)",
     "Require a long press of the power button to switch the radio on."),
    ("set_bit_autoNight", 15, "Automatic night mode",
     "Switch to the night colour theme automatically."),
    ("set_bit_secondaryLanguage", 19, "Use secondary language",
     "Use the firmware's secondary (alternate) UI language."),
    ("set_bit_channelDistance", 21, "Show channel distance",
     "Show the distance to a channel/contact (needs a location)."),
    ("set_bit_txInhibit", 25, "TX inhibit",
     "Block all transmit (receive-only)."),
    ("set_bit_channelsReadOnly", 27, "Channels read-only",
     "Prevent editing channels from the keypad."),
    ("set_bit_doubleHeightUI", 28, "Double-height font UI",
     "Use a larger double-height font for the user interface."),
]

HEX = "0123456789abcdefABCDEF"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _bcd2int(value):
    """Firmware bcd2int(): interpret the nibbles of @value as decimal digits."""
    result = 0
    mult = 1
    while value:
        result += (value & 0x0F) * mult
        mult *= 10
        value >>= 4
    return result


def _int2bcd(value):
    """Inverse of _bcd2int()."""
    result = 0
    shift = 0
    while value:
        result |= (value % 10) << shift
        value //= 10
        shift += 4
    return result


def _decode_name(raw):
    out = []
    for b in raw:
        if b in (0x00, 0xFF):
            break
        out.append(b)
    return bytes(out).decode("ascii", "replace").rstrip()


def _encode_name(name, length=16):
    b = name.encode("ascii", "ignore")[:length]
    return b + b"\xFF" * (length - len(b))


def _channel_optional_dmrid(raw):
    """Per-channel optional DMR ID (0 if disabled).  When flag1 bit7 is set,
    bytes 39..41 hold a 24-bit binary id (overlapping rxSignaling/arts/encrypt)."""
    if raw[38] & 0x80:
        return (raw[39] << 16) | (raw[40] << 8) | raw[41]
    return 0


# Per-channel AES encryption (channel `encrypt` byte, offset 41), per the
# OpenGD77-AES firmware: 0 = inherit the global TX key, 1..15 = AES key slot,
# 0xFF = force off.  Only valid when the optional-DMR-ID flag is clear (they
# share byte 41).
ENC_OPTIONS = (["Inherit (global TX key)", "Off (no encryption)"] +
               ["Key %d" % i for i in range(1, 16)])


def _enc_byte_to_label(b):
    if b == 0:
        return ENC_OPTIONS[0]
    if b == 0xFF:
        return ENC_OPTIONS[1]
    if 1 <= b <= 15:
        return "Key %d" % b
    return ENC_OPTIONS[0]


def _enc_label_to_byte(s):
    if s.startswith("Off"):
        return 0xFF
    if s.startswith("Key "):
        return int(s.split()[1])
    return 0                                  # Inherit / fallback


def _channel_set_optional_dmrid(raw, dmrid):
    if 0 < dmrid <= 0xFFFFFF:
        raw[38] |= 0x80
        raw[39] = (dmrid >> 16) & 0xFF
        raw[40] = (dmrid >> 8) & 0xFF
        raw[41] = dmrid & 0xFF
    elif raw[38] & 0x80:                  # was enabled, now turned off -> reset
        raw[38] = raw[38] & ~0x80 & 0xFF
        raw[39], raw[40], raw[41] = 0x00, 0x16, 0x00
    # else: already off -> leave rxSignaling/artsInterval/encrypt untouched


# --------------------------------------------------------------------------
# DMR-ID database importer (radioid.net-style CSV -> on-flash DB blob).
# Pure functions, no radio/CHIRP dependency.
# --------------------------------------------------------------------------
def parse_dmrid_csv(data):
    """Parse a radioid.net-style user CSV into [(id:int, text:str), ...].

    Accepts a header row (maps RADIO_ID/ID, CALLSIGN, FIRST_NAME/NAME columns)
    or headerless rows assumed to be id, callsign, name, ...  text becomes
    "CALLSIGN Firstname".
    """
    import csv
    import io

    rows = list(csv.reader(io.StringIO(data)))
    if not rows:
        return []

    id_i, call_i, name_i, start = 0, 1, 2, 0
    first = rows[0]
    if first and not first[0].strip().isdigit():
        hdr = [c.strip().lower() for c in first]

        def find(names, default):
            for n in names:
                if n in hdr:
                    return hdr.index(n)
            return default

        id_i = find(["radio_id", "id", "dmr_id"], 0)
        call_i = find(["callsign", "call"], 1)
        name_i = find(["first_name", "fname", "name"], None)
        start = 1

    out = []
    for row in rows[start:]:
        if len(row) <= id_i:
            continue
        idc = row[id_i].strip()
        if not idc.isdigit():
            continue
        call = row[call_i].strip() if (call_i is not None and
                                       len(row) > call_i) else ""
        nm = row[name_i].strip() if (name_i is not None and
                                     len(row) > name_i) else ""
        text = ("%s %s" % (call, nm)).strip() if nm else call
        out.append((int(idc), text or idc))
    return out


def build_dmrid_db(records, contact_len=DMRID_CONTACT_LEN):
    """Build the on-flash DMR-ID DB blob (4-byte BCD id + plain text).

    Records are sorted ascending by id and de-duplicated; the list is capped to
    what fits in area 1.  Returns (blob, num_entries, truncated)."""
    text_len = contact_len - 4
    seen = set()
    recs = []
    for rid, text in sorted(records, key=lambda r: r[0]):
        if rid in seen:
            continue
        seen.add(rid)
        recs.append((rid, text))

    truncated = len(recs) > DMRID_MAX_ENTRIES
    recs = recs[:DMRID_MAX_ENTRIES]

    body = bytearray()
    for rid, text in recs:
        body += struct.pack("<I", _int2bcd(rid))
        tb = text.encode("ascii", "ignore")[:text_len]
        body += tb + b"\x00" * (text_len - len(tb))

    header = bytearray(DMRID_HEADER_LEN)
    header[0:2] = b"Id"
    header[2] = 0x00                         # 4-byte BCD ids (not 'N'/'n')
    header[3] = (0x4A + contact_len) & 0xFF
    struct.pack_into("<I", header, 8, len(recs))
    return bytes(header) + bytes(body), len(recs), truncated


# --------------------------------------------------------------------------
# Satellite (TLE / keps) codec.  The radio stores up to 25 sats in the OpenGD77
# custom-data SATELLITE_TLE block (type 3): per sat a packed 100-byte record =
# name[8] + compressed TLE line1[12] + line2[28] + three RX/TX freq pairs
# (10-Hz units) + TX/arm CTCSS (BCD x10) + 24 spare.  Each TLE line is the
# firmware-expected fixed-field decimal string (24 / 56 chars) nibble-packed
# 2 chars/byte via the lookup "0123456789. +-*" (see satellite.c
# decompressTleData / satelliteTLE2Native).  All pure + host-tested.
# --------------------------------------------------------------------------
SAT_COUNT = 25
SAT_REC_LEN = 100
SAT_BLOCK_LEN = 2520
SAT_NAME_LEN = 8
SAT_COMPRESS_LUT = "0123456789. +-*"


def _sat_compress_line(s, nbytes):
    """Nibble-pack a decompressed field string (chars from SAT_COMPRESS_LUT) into
    @nbytes bytes, 2 chars/byte; right-pad with '0' to fill."""
    s = (s + "0" * (2 * nbytes))[:2 * nbytes]
    out = bytearray(nbytes)
    for i in range(nbytes):
        out[i] = (SAT_COMPRESS_LUT.index(s[2 * i]) << 4) | \
            SAT_COMPRESS_LUT.index(s[2 * i + 1])
    return bytes(out)


def _tle_line1_fields(l1):
    """24-char decompressed line 1: year(2)+epochDay(12)+firstDeriv(10), taken
    from the fixed columns of a standard TLE line 1."""
    return l1[18:20] + l1[20:32] + l1[33:43]


def _tle_line2_fields(l2):
    """55-char decompressed line 2: inclination(8)+RAAN(8)+ecc(7)+argPerigee(8)+
    meanAnomaly(8)+meanMotion(11)+revNumber(5) from a standard TLE line 2."""
    return (l2[8:16] + l2[17:25] + l2[26:33] + l2[34:42] +
            l2[43:51] + l2[52:63] + l2[63:68])


def _css_to_bcd(tone_hz):
    """CTCSS Hz (e.g. 67.0) -> firmware CSS value (BCD of tone*10); 0 = none."""
    return _int2bcd(int(round(tone_hz * 10))) if tone_hz else 0


def encode_satellite(name, line1, line2, freqs):
    """Encode one 100-byte satellite record.  @freqs: optional keys rx1/tx1
    (voice), ctcss1/arm1 (Hz), rx2/tx2 (APRS), rx3/tx3 (other); freqs in Hz are
    stored in 10-Hz units."""
    def f10(hz):
        return int(round((hz or 0) / 10.0))
    rec = bytearray(SAT_REC_LEN)
    nm = name.encode("ascii", "ignore")[:SAT_NAME_LEN]
    rec[0:len(nm)] = nm
    rec[8:20] = _sat_compress_line(_tle_line1_fields(line1), 12)
    rec[20:48] = _sat_compress_line(_tle_line2_fields(line2), 28)
    struct.pack_into("<II", rec, 48, f10(freqs.get("rx1")), f10(freqs.get("tx1")))
    struct.pack_into("<HH", rec, 56, _css_to_bcd(freqs.get("ctcss1")),
                     _css_to_bcd(freqs.get("arm1")))
    struct.pack_into("<II", rec, 60, f10(freqs.get("rx2")), f10(freqs.get("tx2")))
    struct.pack_into("<II", rec, 68, f10(freqs.get("rx3")), f10(freqs.get("tx3")))
    return bytes(rec)                            # rec[76:100] = spare (zero)


def build_satellite_block(sats):
    """Build the 2520-byte SATELLITE_TLE block from (name, l1, l2, freqs) tuples
    (max 25).  Unused slots stay zeroed; name[0]==0 terminates the radio's read."""
    block = bytearray(SAT_BLOCK_LEN)
    for i, (name, l1, l2, freqs) in enumerate(sats[:SAT_COUNT]):
        block[i * SAT_REC_LEN:(i + 1) * SAT_REC_LEN] = encode_satellite(
            name, l1, l2, freqs)
    return bytes(block)


# Built-in transponder frequencies for common FM amateur satellites (Hz).
# Matched against the TLE name via SAT_ALIASES (case-insensitive substring).
# ISS uplink/region and sat status vary -- edit/extend as needed.
SAT_FREQS = {
    "ISS": dict(rx1=437800000, tx1=145990000, ctcss1=67.0,
                rx2=145825000, tx2=145825000),  # FM cross-band repeater + APRS
    "SO-50": dict(rx1=436795000, tx1=145850000, ctcss1=67.0, arm1=74.4),
    "AO-91": dict(rx1=145960000, tx1=435250000, ctcss1=67.0),
    "AO-92": dict(rx1=145880000, tx1=435350000, ctcss1=67.0),
    "AO-27": dict(rx1=436795000, tx1=145850000),
    "PO-101": dict(rx1=145900000, tx1=437500000, ctcss1=141.3),
}
SAT_ALIASES = {
    "ISS": ["ISS", "ZARYA"],
    "SO-50": ["SO-50", "SAUDISAT"],
    "AO-91": ["AO-91", "RADFXSAT", "FOX-1B"],
    "AO-92": ["AO-92", "FOX-1D"],
    "AO-27": ["AO-27", "AO27", "EYESAT"],
    "PO-101": ["PO-101", "DIWATA"],
}


def parse_tle_file(text):
    """Parse a 2-line or 3-line TLE file -> list of (name, line1, line2)."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out = []
    i = 0
    while i < len(lines):
        if (lines[i].startswith("1 ") and i + 1 < len(lines) and
                lines[i + 1].startswith("2 ")):
            out.append(("SAT%d" % (len(out) + 1), lines[i], lines[i + 1]))
            i += 2
        elif (i + 2 < len(lines) and lines[i + 1].startswith("1 ") and
                lines[i + 2].startswith("2 ")):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def match_satellite_freqs(name):
    """Return a copy of the built-in freq dict for a TLE name, or None.  An alias
    must appear as a whole token (bounded by non-alphanumerics) so e.g.
    'SWISSCUBE' does not match the 'ISS' alias."""
    up = name.upper()
    for key, aliases in SAT_ALIASES.items():
        for a in aliases:
            if re.search(r"(?<![A-Z0-9])" + re.escape(a) + r"(?![A-Z0-9])", up):
                return dict(SAT_FREQS[key])
    return None


def build_satellite_block_from_tle(text, freqs_only=True):
    """Parse a TLE file, attach built-in freqs by name, and build the 2520-byte
    block.  If @freqs_only, only sats with a known freq entry are kept (so the
    radio gets usable, tunable sats).  Returns (block, names, dropped)."""
    parsed = parse_tle_file(text)
    sats = []
    for name, l1, l2 in parsed:
        fr = match_satellite_freqs(name)
        if fr is None:
            if freqs_only:
                continue
            fr = {}
        sats.append((name, l1, l2, fr))
        if len(sats) >= SAT_COUNT:
            break
    return build_satellite_block(sats), [s[0] for s in sats], len(parsed) - len(sats)


# --------------------------------------------------------------------------
# AES key store codec (pure functions on bytes; no radio / CHIRP dependency so
# they are trivially unit-testable -- see tests/).
# --------------------------------------------------------------------------
class AesSlot(object):
    __slots__ = ("valid", "key_id", "key")

    def __init__(self, valid=False, key_id=0, key=b""):
        self.valid = valid
        self.key_id = key_id
        self.key = key

    @property
    def key_hex(self):
        return self.key.hex() if self.valid else ""


class AesKeyStore(object):
    """Parsed AESK payload: a TX key id and 16 slots."""

    def __init__(self, tx_key_id=0, slots=None):
        self.tx_key_id = tx_key_id
        self.slots = slots or [AesSlot() for _ in range(AESK_NUM_SLOTS)]

    @classmethod
    def from_payload(cls, payload):
        if not payload or payload[:4] != AESK_MAGIC:
            return cls()
        tx_key_id = payload[5]
        slots = []
        for i in range(AESK_NUM_SLOTS):
            e = AESK_HDR_LEN + i * AESK_SLOT_SIZE
            valid = payload[e] == 1
            key_id = payload[e + 1]
            key = bytes(payload[e + 4:e + 4 + AESK_KEY_LEN])
            slots.append(AesSlot(valid, key_id, key))
        return cls(tx_key_id, slots)

    def to_payload(self):
        p = bytearray(AESK_PAYLOAD_LEN)
        p[0:4] = AESK_MAGIC
        p[4] = AESK_VERSION
        p[5] = self.tx_key_id & 0xFF
        for i, slot in enumerate(self.slots):
            e = AESK_HDR_LEN + i * AESK_SLOT_SIZE
            if slot.valid and len(slot.key) == AESK_KEY_LEN:
                p[e] = 1
                p[e + 1] = slot.key_id & 0xFF
                p[e + 4:e + 4 + AESK_KEY_LEN] = slot.key
        return bytes(p)

    # Key-id addressed access (the meaningful identifier; a key id may live in
    # any physical slot, matching how the firmware matches keys).
    def key_for(self, key_id):
        for s in self.slots:
            if s.valid and s.key_id == key_id:
                return s
        return None

    def set_key(self, key_id, key):
        free = None
        for i, s in enumerate(self.slots):
            if s.valid and s.key_id == key_id:
                self.slots[i] = AesSlot(True, key_id, key)
                return
            if free is None and not s.valid:
                free = i
        if free is None:
            raise errors.RadioError("No free AES key slot")
        self.slots[free] = AesSlot(True, key_id, key)

    def clear_key(self, key_id):
        for i, s in enumerate(self.slots):
            if s.valid and s.key_id == key_id:
                self.slots[i] = AesSlot(False, key_id, b"")


def find_aes_block(region):
    """Walk the custom-data block chain.  Return (header_offset, payload_bytes)
    for the AES_KEYS block, or (None, None)."""
    if region[:8] != CUSTOM_MAGIC:
        return None, None
    off = CUSTOM_HDR_LEN
    while off + 8 <= len(region):
        dtype, dlen = struct.unpack_from("<II", region, off)
        if dtype == CUSTOM_TYPE_EMPTY or dlen == 0 or dlen == 0xFFFFFFFF:
            return None, None
        if dtype == CUSTOM_TYPE_AES_KEYS:
            return off, bytes(region[off + 8:off + 8 + dlen])
        off += 8 + dlen
    return None, None


def find_chain_end(region):
    """Return the offset of the first free block header in the chain, or None
    if the region has no valid magic / is full."""
    if region[:8] != CUSTOM_MAGIC:
        return None
    off = CUSTOM_HDR_LEN
    while off + 8 <= len(region):
        dtype, dlen = struct.unpack_from("<II", region, off)
        if dtype == CUSTOM_TYPE_EMPTY or dlen == 0 or dlen == 0xFFFFFFFF:
            return off
        off += 8 + dlen
    return None


def region_with_aes(region, payload):
    """Return a copy of the custom-data @region with the AES_KEYS @payload
    written in place, preserving any sibling blocks.  Handles a fresh/erased
    region by laying down the magic + a single AES block."""
    region = bytearray(region)
    if region[:8] != CUSTOM_MAGIC:
        # Fresh region: magic + reserved + AES block (matches aes_key_store.py)
        out = bytearray(b"\xFF" * len(region))
        out[0:8] = CUSTOM_MAGIC
        struct.pack_into("<II", out, CUSTOM_HDR_LEN,
                         CUSTOM_TYPE_AES_KEYS, AESK_PAYLOAD_LEN)
        out[CUSTOM_HDR_LEN + 8:CUSTOM_HDR_LEN + 8 + len(payload)] = payload
        return bytes(out)

    hdr_off, _ = find_aes_block(region)
    if hdr_off is None:
        hdr_off = find_chain_end(region)
        if hdr_off is None or hdr_off + 8 + AESK_PAYLOAD_LEN > len(region):
            raise errors.RadioError(
                "No room in the custom-data region for an AES key block")
        struct.pack_into("<II", region, hdr_off,
                         CUSTOM_TYPE_AES_KEYS, AESK_PAYLOAD_LEN)
    region[hdr_off + 8:hdr_off + 8 + AESK_PAYLOAD_LEN] = payload
    return bytes(region)


# --------------------------------------------------------------------------
# USB CPS protocol (operates on a pyserial pipe, supplied by CHIRP)
# --------------------------------------------------------------------------
def _show_cps(pipe):
    pipe.reset_input_buffer()
    pipe.write(bytes([ord("C"), 0]))
    pipe.flush()
    time.sleep(0.1)
    pipe.read(64)


def _close_cps(pipe):
    pipe.write(bytes([ord("C"), 5]))
    pipe.flush()
    time.sleep(0.05)
    pipe.read(64)


def _read_area(pipe, area, addr, length):
    """CPS 'R' read of @length bytes from @addr in @area."""
    out = b""
    remaining = length
    while remaining > 0:
        n = min(remaining, 1024)
        req = bytes([ord("R"), area,
                     (addr >> 24) & 0xFF, (addr >> 16) & 0xFF,
                     (addr >> 8) & 0xFF, addr & 0xFF,
                     (n >> 8) & 0xFF, n & 0xFF])
        pipe.reset_input_buffer()
        pipe.write(req)
        pipe.flush()
        time.sleep(0.05)
        r = pipe.read(n + 3)
        if len(r) < 3 or r[0] != ord("R"):
            raise errors.RadioError(
                "Read failed @0x%X area %d (got %r)" % (addr, area, r[:8]))
        out += r[3:3 + n]
        addr += n
        remaining -= n
    return out


def _flash_prepare(pipe, sector):
    req = bytes([ord("X"), 1,
                 (sector >> 16) & 0xFF, (sector >> 8) & 0xFF, sector & 0xFF])
    pipe.reset_input_buffer()
    pipe.write(req)
    pipe.flush()
    time.sleep(0.2)
    r = pipe.read(8)
    if not r or r[0] == ord("-"):
        raise errors.RadioError("Flash prepare-sector 0x%X failed (%r)" % (
            sector, r))


def _flash_send(pipe, addr, data):
    off = 0
    while off < len(data):
        chunk = data[off:off + 1024]
        a = addr + off
        req = bytes([ord("X"), 2,
                     (a >> 24) & 0xFF, (a >> 16) & 0xFF,
                     (a >> 8) & 0xFF, a & 0xFF,
                     (len(chunk) >> 8) & 0xFF, len(chunk) & 0xFF]) + chunk
        pipe.reset_input_buffer()
        pipe.write(req)
        pipe.flush()
        time.sleep(0.05)
        r = pipe.read(8)
        if not r or r[0] == ord("-"):
            raise errors.RadioError("Flash send-data failed @0x%X (%r)" % (a, r))
        off += len(chunk)


def _flash_commit(pipe):
    pipe.reset_input_buffer()
    pipe.write(bytes([ord("X"), 3]))
    pipe.flush()
    time.sleep(0.5)
    r = pipe.read(8)
    if not r or r[0] == ord("-"):
        raise errors.RadioError("Flash commit failed (%r)" % r)


def _write_region(pipe, raw_addr, new_data, old_data=None, progress=None):
    """Write @new_data to raw SPI-flash @raw_addr, one 4 KB sector at a time.

    Only this region's bytes are sent; the firmware preserves the rest of each
    touched sector (it reads the sector, patches, erases, writes back).  This is
    how EEPROM-resident codeplug data is written on MD-UV380 (EEPROM == SPI
    flash at offset 0; the dedicated EEPROM write command is a no-op there).

    If @old_data is given, sectors whose region-bytes are unchanged are skipped.
    Each written sector is verified by read-back.  @progress(done, total) is
    called per sector.  Returns the number of sectors written.
    """
    n = len(new_data)
    end = raw_addr + n
    first = raw_addr // SECTOR_SIZE
    total = ((end - 1) // SECTOR_SIZE) - first + 1
    sector = first
    written = done = 0
    while sector * SECTOR_SIZE < end:
        s0 = sector * SECTOR_SIZE
        s1 = s0 + SECTOR_SIZE
        seg0 = max(raw_addr, s0)
        seg1 = min(end, s1)
        i0 = seg0 - raw_addr
        i1 = seg1 - raw_addr
        seg = bytes(new_data[i0:i1])
        if old_data is None or bytes(old_data[i0:i1]) != seg:
            _flash_prepare(pipe, sector)
            _flash_send(pipe, seg0, seg)
            _flash_commit(pipe)
            rb = _read_area(pipe, AREA_FLASH, seg0, len(seg))
            if rb != seg:
                raise errors.RadioError("Write verify failed @0x%X" % seg0)
            written += 1
        done += 1
        if progress:
            progress(done, total)
        sector += 1
    return written


def _read_radio_info(pipe):
    """Return dict from the RADIO_INFO area, or raise RadioError."""
    raw = _read_area(pipe, AREA_RADIO_INFO, 0, 46)
    if len(raw) < 46:
        raise errors.RadioError("Short RADIO_INFO response")
    (struct_ver, radio_type) = struct.unpack_from("<II", raw, 0)
    git = _decode_name(raw[8:24])
    built = _decode_name(raw[24:40])
    return {"struct_version": struct_ver, "radio_type": radio_type,
            "git": git, "built": built}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
@directory.register
class OpenGD77AESRadio(chirp_common.CloneModeRadio):
    """OpenGD77-AES256 (TYT MD-UV380/390 10W Plus)."""

    VENDOR = "OpenGD77"
    MODEL = "MD-UV380/390 (AES)"
    BAUD_RATE = BAUD
    NEEDS_COMPAT_SERIAL = False
    FORMATS = []
    _memsize = IMAGE_SIZE

    # OpenGD77 per-channel power (libreDMR_Power byte): 0 = Master (use the
    # radio's global power), else level = value - 1.  List index == the stored
    # byte value.  Watts match the MD-UV390 10W-Plus power table.
    POWER_LEVELS = [
        chirp_common.PowerLevel("Master", watts=0.0),
        chirp_common.PowerLevel("50mW", watts=0.05),
        chirp_common.PowerLevel("250mW", watts=0.25),
        chirp_common.PowerLevel("500mW", watts=0.5),
        chirp_common.PowerLevel("750mW", watts=0.75),
        chirp_common.PowerLevel("1W", watts=1.0),
        chirp_common.PowerLevel("2W", watts=2.0),
        chirp_common.PowerLevel("3W", watts=3.0),
        chirp_common.PowerLevel("5W", watts=5.0),
        chirp_common.PowerLevel("10W", watts=10.0),
        chirp_common.PowerLevel("Max", watts=11.0),
    ]

    @classmethod
    def get_prompts(cls):
        rp = chirp_common.RadioPrompts()
        rp.experimental = (
            "This driver targets the OpenGD77-AES256 firmware.  Supported: "
            "AES-256 key management (Settings -> AES Keys), channel read/write "
            "(memories 1-1024, analog + DMR), VFO A/B (as special channels), "
            "quick keys, zones (as banks), digital + DTMF "
            "contacts, RX groups, per-channel AES encryption, radio settings "
            "(callsign, DMR ID, boot screen), the radio-wide settings menu "
            "(Settings -> Radio settings), DMR-ID database import from a CSV and "
            "satellite (TLE) import for the Satellite menu (Settings -> Radio).\n\n"
            "AES voice encryption is only legal on licensed commercial / PMR "
            "allocations -- NOT on amateur (ham) bands.")
        rp.pre_download = (
            "1. Connect the radio via USB.\n"
            "2. The radio should be on (normal mode).\n"
            "3. Click OK to download.")
        rp.pre_upload = (
            "Upload writes the AES key store, channels, zones, contacts, RX "
            "groups, DTMF, the radio settings (callsign/DMR ID/boot screen and "
            "the radio-wide settings) and any pending DMR-ID database or "
            "satellite-TLE import.  Only changed flash sectors are written.  "
            "Reboot the radio afterwards for radio-wide settings to take effect.")
        return rp

    def get_features(self):
        rf = chirp_common.RadioFeatures()
        rf.has_settings = True
        rf.has_bank = True
        rf.has_bank_names = True
        rf.has_sub_devices = False
        rf.has_ctone = True
        rf.has_cross = False
        rf.has_tuning_step = False
        rf.has_dtcs = True
        rf.has_dtcs_polarity = True
        rf.has_rx_dtcs = False
        rf.has_name = True
        rf.valid_name_length = 16
        rf.memory_bounds = (1, CH_MAX)
        rf.valid_special_chans = list(VFO_NAMES)
        rf.valid_modes = ["FM", "NFM", "DMR"]
        rf.valid_tmodes = ["", "Tone", "TSQL", "DTCS"]
        rf.valid_power_levels = self.POWER_LEVELS
        rf.valid_duplexes = ["", "-", "+", "off"]
        # OpenGD77 step table (kHz). Must include 6.25 (PMR446) and 2.5, or
        # CHIRP rejects those frequencies as "step not supported".
        rf.valid_tuning_steps = [2.5, 5.0, 6.25, 10.0, 12.5, 25.0, 30.0, 50.0]
        rf.valid_bands = [(136000000, 174000000), (400000000, 480000000)]
        rf.valid_characters = chirp_common.CHARSET_ASCII
        rf.valid_skips = ["", "S"]
        rf.can_odd_split = True
        return rf

    # -- clone ----------------------------------------------------------
    def sync_in(self):
        try:
            self._do_download()
        except errors.RadioError:
            raise
        except Exception as e:
            LOG.exception("download failed")
            raise errors.RadioError("Failed to read from radio: %s" % e)

    def sync_out(self):
        try:
            self._do_upload()
        except errors.RadioError:
            raise
        except Exception as e:
            LOG.exception("upload failed")
            raise errors.RadioError("Failed to write to radio: %s" % e)

    def _do_download(self):
        pipe = self.pipe
        pipe.baudrate = self.BAUD_RATE
        pipe.timeout = 1.0
        _show_cps(pipe)
        info = _read_radio_info(pipe)
        LOG.info("RADIO_INFO: %s", info)
        if info["radio_type"] != RADIO_TYPE_MDUV380:
            _close_cps(pipe)
            raise errors.RadioError(
                "Connected radio radioType=%d, expected %d (MD-UV380/390). "
                "Is this an OpenGD77 MD-UV380/390?" % (
                    info["radio_type"], RADIO_TYPE_MDUV380))

        image = bytearray(b"\x00" * IMAGE_SIZE)
        try:
            status = chirp_common.Status()
            status.msg = "Reading codeplug"
            status.max = len(_RANGES)
            for i, (img_off, area, dev_addr, length) in enumerate(_RANGES):
                data = _read_area(pipe, area, dev_addr, length)
                image[img_off:img_off + length] = data
                status.cur = i + 1
                self.status_fn(status)
        finally:
            _close_cps(pipe)
        self._mmap = memmap.MemoryMapBytes(bytes(image))
        self._orig = bytes(image)        # for change-detection on upload
        self.process_mmap()

    def _do_upload(self):
        pipe = self.pipe
        pipe.baudrate = self.BAUD_RATE
        pipe.timeout = 1.0
        _show_cps(pipe)
        try:
            info = _read_radio_info(pipe)
            if info["radio_type"] != RADIO_TYPE_MDUV380:
                raise errors.RadioError(
                    "Connected radio is not an MD-UV380/390 (radioType=%d)" %
                    info["radio_type"])
            img = self._img()
            orig = getattr(self, "_orig", None)
            status = chirp_common.Status()
            status.msg = "Writing codeplug"
            status.max = len(_WRITE_SPANS)
            total = 0
            for i, (img_off, raw_addr, length) in enumerate(_WRITE_SPANS):
                new = img[img_off:img_off + length]
                old = orig[img_off:img_off + length] if orig else None
                # Safety: never overwrite the custom-data/AES sector with a
                # region that has no "OpenGD77" magic (e.g. an image that was
                # never downloaded) -- doing so would wipe the radio's AES keys.
                if raw_addr == CUSTOM_DATA_ADDR and new[:8] != CUSTOM_MAGIC:
                    LOG.warning("skipping AES sector write: image has no "
                                "custom-data magic (would erase keys)")
                    status.cur = i + 1
                    self.status_fn(status)
                    continue
                # Safety: likewise never write the radio-settings blob unless it
                # carries the settings magic, or the firmware would factory-reset
                # every setting on next boot.
                if (raw_addr == SETTINGS_ADDR and
                        struct.unpack_from("<I", bytes(new), 0)[0] != SETTINGS_MAGIC):
                    LOG.warning("skipping radio-settings write: no settings "
                                "magic (would trigger a factory reset)")
                    status.cur = i + 1
                    self.status_fn(status)
                    continue
                total += _write_region(pipe, raw_addr, new, old)
                status.cur = i + 1
                self.status_fn(status)
            LOG.info("upload wrote %d sector(s)", total)
            self._maybe_import_dmrid(pipe)
            self._maybe_import_satellite(pipe)
        finally:
            _close_cps(pipe)

    def _maybe_import_dmrid(self, pipe):
        path = getattr(self, "_dmrid_import_path", None)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        except OSError as e:
            raise errors.RadioError("Cannot read DMR-ID CSV %r: %s" % (path, e))
        records = parse_dmrid_csv(data)
        if not records:
            raise errors.RadioError("No usable rows in DMR-ID CSV %r" % path)
        blob, n, truncated = build_dmrid_db(records)
        status = chirp_common.Status()
        status.msg = "Writing DMR-ID database (%d entries)" % n

        def prog(done, tot):
            status.max = tot
            status.cur = done
            self.status_fn(status)

        _write_region(pipe, DMRID_HEADER_ADDR, blob, progress=prog)
        # Reflect the new header in the image so the status field updates.
        img = bytearray(self._img())
        img[IMG_DMRIDHDR:IMG_DMRIDHDR + DMRID_HEADER_LEN] = blob[:DMRID_HEADER_LEN]
        self._mmap = memmap.MemoryMapBytes(bytes(img))
        self._dmrid_import_path = None
        LOG.info("DMR-ID DB import: wrote %d entries%s", n,
                 " (CSV truncated to fit)" if truncated else "")

    # -- satellite (TLE) import -----------------------------------------
    def _find_custom_block(self, pipe, dtype):
        """Walk the OpenGD77 custom-data block chain (12-byte magic header, then
        {type:u32, len:u32}+payload blocks up to 0x10000).  Returns:
          ("nomagic", None)            -- region has no "OpenGD77" magic
          ("found", (hdr_addr, dlen))  -- a block of @dtype exists
          ("empty", hdr_addr)          -- first empty slot (append point)
          ("full", None)               -- no empty slot before the 64 KB limit
        Addresses are raw flash addresses."""
        base = CUSTOM_DATA_ADDR
        if _read_area(pipe, AREA_FLASH, base, 8) != CUSTOM_MAGIC:
            return ("nomagic", None)
        off = CUSTOM_HDR_LEN
        while off + 8 <= 0x10000:
            dtype_read, dlen = struct.unpack(
                "<II", _read_area(pipe, AREA_FLASH, base + off, 8))
            if dlen in (0, 0xFFFFFFFF):
                return ("empty", base + off)
            if dtype_read == dtype:
                return ("found", (base + off, dlen))
            off += 8 + dlen
        return ("full", None)

    def _write_satellite_block(self, pipe, block, progress=None):
        """Write @block (2520 B) as the SATELLITE_TLE custom-data block: overwrite
        an existing same-size block in place, else append at the first empty
        slot.  Refuses if the region lacks the OpenGD77 magic (anti-wipe)."""
        if len(block) != SAT_BLOCK_LEN:
            raise errors.RadioError("Satellite block must be %d bytes" %
                                    SAT_BLOCK_LEN)
        state, info = self._find_custom_block(pipe, CUSTOM_TYPE_SATELLITE)
        if state == "nomagic":
            raise errors.RadioError(
                "Custom-data region has no OpenGD77 magic; refusing to write "
                "satellites (download from the radio first).")
        if state == "full":
            raise errors.RadioError(
                "No room in the custom-data region for the satellite block.")
        if state == "found":
            hdr_addr, dlen = info
            if dlen != SAT_BLOCK_LEN:
                raise errors.RadioError(
                    "Existing satellite block is %d bytes (expected %d); refusing "
                    "to resize." % (dlen, SAT_BLOCK_LEN))
            _write_region(pipe, hdr_addr + 8, block, progress=progress)
        else:                                       # "empty" -> append
            hdr_addr = info
            if hdr_addr + 8 + SAT_BLOCK_LEN > CUSTOM_DATA_ADDR + 0x10000:
                raise errors.RadioError(
                    "No room in the custom-data region for the satellite block.")
            payload = struct.pack("<II", CUSTOM_TYPE_SATELLITE,
                                  SAT_BLOCK_LEN) + block
            _write_region(pipe, hdr_addr, payload, progress=progress)

    def _maybe_import_satellite(self, pipe):
        path = getattr(self, "_satellite_import_path", None)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
        except OSError as e:
            raise errors.RadioError("Cannot read TLE file %r: %s" % (path, e))
        block, names, dropped = build_satellite_block_from_tle(data)
        if not names:
            raise errors.RadioError(
                "No satellites with known frequencies found in %r (add their "
                "frequencies to SAT_FREQS)." % path)
        status = chirp_common.Status()
        status.msg = "Writing %d satellite(s)" % len(names)

        def prog(done, tot):
            status.max = tot
            status.cur = done
            self.status_fn(status)

        self._write_satellite_block(pipe, block, progress=prog)
        self._satellite_import_path = None
        LOG.info("satellite import: wrote %d sat(s)%s", len(names),
                 " (%d without freqs dropped)" % dropped if dropped else "")

    def process_mmap(self):
        # Channels are parsed on demand in get_memory(); precompute the contact
        # and RX-group name caches used for channel dropdowns.
        self._build_caches()

    def _img(self):
        # Cache the packed image. get_packed() rebuilds a bytes from a list on
        # every call, which is very expensive in hot loops (e.g. the bank
        # editor). The cache is invalidated automatically when _mmap is
        # replaced -- every edit creates a new MemoryMapBytes object.
        if getattr(self, "_packed_for", None) is not self._mmap:
            self._packed = self._mmap.get_packed()
            self._packed_for = self._mmap
        return self._packed

    # -- AES key store access on the image ------------------------------
    def _aes_region(self):
        return self._img()[IMG_AES:IMG_AES + SECTOR_SIZE]

    def _read_aes_store(self):
        _, payload = find_aes_block(self._aes_region())
        return AesKeyStore.from_payload(payload)

    def _write_aes_store(self, store):
        img = bytearray(self._img())
        img[IMG_AES:IMG_AES + SECTOR_SIZE] = region_with_aes(
            bytes(img[IMG_AES:IMG_AES + SECTOR_SIZE]), store.to_payload())
        self._mmap = memmap.MemoryMapBytes(bytes(img))

    # -- channels -------------------------------------------------------
    def _channel_offset(self, number):
        """Return (image_offset, bitmap_image_offset, bit_index) for channel
        @number (1-based)."""
        idx = number - 1
        if idx < CH_PER_BANK:
            data = IMG_EE_CH + idx * CH_SIZE
            bm = IMG_EE_BITMAP + idx // 8
            return data, bm, idx % 8
        fi = idx - CH_PER_BANK
        bank = fi // CH_PER_BANK            # 0..6 within flash
        within = fi % CH_PER_BANK
        base = IMG_FLASH_CH + bank * FLASH_BANK_STRIDE
        return base + 16 + within * CH_SIZE, base + within // 8, within % 8

    def _channel_in_use(self, number):
        data = self._img()
        _, bm, bit = self._channel_offset(number)
        return bool((data[bm] >> bit) & 0x01)

    def get_raw_memory(self, number):
        off, _, _ = self._channel_offset(number)
        return util.hexprint(self._img()[off:off + CH_SIZE])

    def get_memory(self, number):
        if isinstance(number, str) or number > CH_MAX:
            return self._get_vfo_memory(number)
        mem = chirp_common.Memory()
        mem.number = number
        if not self._channel_in_use(number):
            mem.empty = True
            return mem
        off, _, _ = self._channel_offset(number)
        self._decode_channel(mem, self._img()[off:off + CH_SIZE])
        return mem

    def _vfo_specials(self):
        """Map special-channel name -> synthetic memory number."""
        return {n: VFO_A_NUMBER + i for i, n in enumerate(VFO_NAMES)}

    def _get_vfo_memory(self, number):
        # VFO A/B are full CodeplugChannel_t structs at 0x7590 -- no bitmap, no
        # meaningful stored name, and they can't be deleted.
        if isinstance(number, str):
            name = number
            num = self._vfo_specials()[name]
        else:
            num = number
            name = VFO_NAMES[num - VFO_A_NUMBER]
        mem = chirp_common.Memory()
        mem.number = num
        mem.extd_number = name
        off = IMG_VFO + (num - VFO_A_NUMBER) * CH_SIZE
        raw = self._img()[off:off + CH_SIZE]
        if struct.unpack_from("<I", raw, 16)[0] in (0xFFFFFFFF, 0):
            mem.empty = True
        else:
            self._decode_channel(mem, raw)
        mem.name = ""                          # VFOs have no stored name
        mem.immutable = ["name", "skip"]
        return mem

    def _decode_channel(self, mem, raw):
        mem.name = _decode_name(raw[0:16])
        rx = _bcd2int(struct.unpack_from("<I", raw, 16)[0]) * 10
        tx = _bcd2int(struct.unpack_from("<I", raw, 20)[0]) * 10
        mem.freq = rx
        if tx == 0 or tx == rx:
            mem.duplex = ""
        elif tx < rx:
            mem.duplex = "-"
            mem.offset = rx - tx
        else:
            mem.duplex = "+"
            mem.offset = tx - rx

        ch_mode = raw[24]
        flag4 = raw[51]
        if ch_mode == 1:
            mem.mode = "DMR"
        else:
            mem.mode = "FM" if (flag4 & 0x02) else "NFM"

        rx_tone = struct.unpack_from("<H", raw, 32)[0]
        tx_tone = struct.unpack_from("<H", raw, 34)[0]
        self._decode_tones(mem, tx_tone, rx_tone)

        lp = raw[25]                           # libreDMR_Power (0 = Master)
        mem.power = self.POWER_LEVELS[lp if lp < len(self.POWER_LEVELS) else 0]
        if flag4 & 0x04:                       # RX_ONLY
            mem.duplex = "off"
        if flag4 & 0x20:                       # ZONE_SKIP
            mem.skip = "S"

        self._add_dmr_extras(mem, raw)

    @staticmethod
    def _decode_tones(mem, tx_tone, rx_tone):
        def decode(val):
            if val in (0, CSS_NONE):
                return None
            if val & 0x8000:                   # DCS
                return ("DTCS", _bcd2int(val & 0x0FFF),
                        "R" if (val & 0x4000) else "N")
            return ("Tone", _bcd2int(val) / 10.0, "N")

        rxd = decode(rx_tone)
        txd = decode(tx_tone)
        if txd and txd[0] == "Tone":
            mem.rtone = txd[1]
        if rxd and rxd[0] == "Tone":
            mem.ctone = rxd[1]
            mem.tmode = "TSQL" if txd else "Tone"
        elif txd and txd[0] == "Tone":
            mem.tmode = "Tone"
        if txd and txd[0] == "DTCS":
            mem.dtcs = txd[1]
            mem.tmode = "DTCS"
            mem.dtcs_polarity = txd[2] + (rxd[2] if rxd and rxd[0] == "DTCS"
                                          else "N")

    @staticmethod
    def _index_list(items, current_index):
        """Build (options, selected_position) for a 'idx: name' dropdown with a
        leading None entry, ensuring the current index is always representable.
        The returned position feeds RadioSettingValueList(current_index=...)."""
        options = ["None"] + ["%d: %s" % (i, n) for i, n in items]
        sel = 0
        if current_index:
            label = next(("%d: %s" % (i, n) for i, n in items
                          if i == current_index), None)
            if label is None:
                label = "%d: ?" % current_index
                options.append(label)
            sel = options.index(label)
        return options, sel

    def _add_dmr_extras(self, mem, raw):
        group = RadioSettingGroup("opengd77", "OpenGD77")
        flag4 = raw[51]
        tot = RadioSetting(
            "tot", "Time-out timer (s, 0=off)",
            RadioSettingValueInteger(0, 255 * 15, raw[27] * 15, 15))
        tot.set_doc("Maximum continuous transmit time before TX is cut "
                    "(15 s steps); 0 = no limit.")
        group.append(tot)
        vox = RadioSetting(
            "vox", "VOX", RadioSettingValueBoolean(bool(flag4 & 0x40)))
        vox.set_doc("Voice-activated transmit on this channel.")
        group.append(vox)
        sql = RadioSetting(
            "squelch", "Squelch (0=master, 1-21)",
            RadioSettingValueInteger(0, 21, raw[55] if raw[55] <= 21 else 0))
        sql.set_doc("Per-channel squelch level; 0 = use the radio's master "
                    "squelch, 1 (open) to 21 (tight).")
        group.append(sql)
        ask = RadioSetting(
            "all_skip", "Skip on all-scan (lone worker)",
            RadioSettingValueBoolean(bool(flag4 & 0x10)))
        ask.set_doc("Skip this channel during an all-channels scan.")
        group.append(ask)

        cc = RadioSetting(
            "cc", "DMR colour code",
            RadioSettingValueInteger(0, 15, raw[44] & 0x0F))
        cc.set_doc("DMR colour code (0-15); must match the repeater/contact.")
        group.append(cc)
        ts = RadioSetting(
            "ts", "DMR timeslot",
            RadioSettingValueInteger(1, 2, 2 if (raw[49] & 0x40) else 1))
        ts.set_doc("DMR timeslot (1 or 2) used for TX.")
        group.append(ts)

        opts, sel = self._index_list(getattr(self, "_contacts", []),
                                     struct.unpack_from("<H", raw, 46)[0])
        ct = RadioSetting("contact", "Contact (TX talkgroup)",
                          RadioSettingValueList(opts, current_index=sel))
        ct.set_doc("The digital contact / talkgroup this channel transmits to.")
        group.append(ct)

        opts, sel = self._index_list(getattr(self, "_rxgroups", []), raw[43])
        tg = RadioSetting("tg_list", "RX group list",
                          RadioSettingValueList(opts, current_index=sel))
        tg.set_doc("RX group list: the talkgroups this channel listens to.")
        group.append(tg)

        optional_dmrid = bool(raw[38] & 0x80)
        enc = _enc_byte_to_label(raw[41] if not optional_dmrid else 0)
        enc_idx = ENC_OPTIONS.index(enc) if enc in ENC_OPTIONS else 0
        es = RadioSetting("encrypt", "AES encryption",
                          RadioSettingValueList(ENC_OPTIONS, current_index=enc_idx))
        es.set_doc("Per-channel AES key (OpenGD77-AES firmware). 'Inherit' uses "
                   "the global TX key; 'Key N' forces AES key slot N; 'Off' "
                   "disables encryption. Ignored if a per-channel DMR ID is set "
                   "(they share the same byte).")
        group.append(es)

        did = RadioSetting(
            "dmrid", "Per-channel DMR ID (0=off)",
            RadioSettingValueInteger(0, 16777215,
                                     _channel_optional_dmrid(raw)))
        did.set_doc("Override your DMR ID on this channel; 0 = use the radio's "
                    "global DMR ID. Shares a byte with AES encryption (a non-zero "
                    "DMR ID wins).")
        group.append(did)
        mem.extra = group

    def set_memory(self, mem):
        if isinstance(mem.number, str) or mem.number > CH_MAX:
            return self._set_vfo_memory(mem)
        img = bytearray(self._img())
        off, bm, bit = self._channel_offset(mem.number)

        if mem.empty:
            img[bm] &= ~(1 << bit)
            img[off:off + CH_SIZE] = b"\xFF" * CH_SIZE
            self._mmap = memmap.MemoryMapBytes(bytes(img))
            return

        in_use = bool((img[bm] >> bit) & 0x01)
        # Modify the existing record in place (preserve OpenGD77-specific fields
        # CHIRP doesn't expose); start from a zeroed template for a new channel.
        raw = bytearray(img[off:off + CH_SIZE]) if in_use else bytearray(CH_SIZE)
        self._encode_channel(mem, raw)
        img[off:off + CH_SIZE] = raw
        img[bm] |= (1 << bit)
        self._mmap = memmap.MemoryMapBytes(bytes(img))

    def _set_vfo_memory(self, mem):
        num = (self._vfo_specials()[mem.number]
               if isinstance(mem.number, str) else mem.number)
        if mem.empty:
            return                              # VFOs cannot be deleted
        off = IMG_VFO + (num - VFO_A_NUMBER) * CH_SIZE
        img = bytearray(self._img())
        raw = bytearray(img[off:off + CH_SIZE])
        self._encode_channel(mem, raw, set_name=False)
        img[off:off + CH_SIZE] = raw
        self._mmap = memmap.MemoryMapBytes(bytes(img))

    def _encode_channel(self, mem, raw, set_name=True):
        if set_name:
            raw[0:16] = _encode_name(mem.name)

        struct.pack_into("<I", raw, 16, _int2bcd(mem.freq // 10))
        if mem.duplex == "-":
            tx = mem.freq - mem.offset
        elif mem.duplex == "+":
            tx = mem.freq + mem.offset
        else:                                    # "" or "off"
            tx = mem.freq
        struct.pack_into("<I", raw, 20, _int2bcd(tx // 10))

        raw[24] = 1 if mem.mode == "DMR" else 0

        tx_css, rx_css = self._encode_tones(mem)
        struct.pack_into("<H", raw, 32, rx_css)
        struct.pack_into("<H", raw, 34, tx_css)

        raw[25] = (self.POWER_LEVELS.index(mem.power)   # libreDMR_Power
                   if mem.power in self.POWER_LEVELS else 0)

        f4 = raw[51]
        if mem.mode == "FM":
            f4 |= 0x02                           # 25 kHz
        else:
            f4 &= ~0x02                          # NFM / DMR -> 12.5 kHz
        f4 = (f4 | 0x04) if mem.duplex == "off" else (f4 & ~0x04)  # RX only
        f4 = (f4 | 0x20) if mem.skip == "S" else (f4 & ~0x20)      # zone skip
        raw[51] = f4 & 0xFF

        if mem.extra:
            ex = {s.get_name(): s.value for s in mem.extra}
            if "tot" in ex:
                raw[27] = (int(ex["tot"]) // 15) & 0xFF
            if "vox" in ex:
                raw[51] = (raw[51] | 0x40) if bool(ex["vox"]) \
                    else raw[51] & ~0x40 & 0xFF
            if "squelch" in ex:
                raw[55] = int(ex["squelch"]) & 0xFF
            if "all_skip" in ex:
                raw[51] = (raw[51] | 0x10) if bool(ex["all_skip"]) \
                    else raw[51] & ~0x10 & 0xFF
            if "cc" in ex:
                raw[44] = (raw[44] & 0xF0) | (int(ex["cc"]) & 0x0F)
            if "ts" in ex:
                raw[49] = (raw[49] | 0x40) if int(ex["ts"]) == 2 \
                    else (raw[49] & ~0x40) & 0xFF
            if "contact" in ex:
                struct.pack_into("<H", raw, 46,
                                 self._parse_index(ex["contact"]) & 0xFFFF)
            if "tg_list" in ex:
                raw[43] = self._parse_index(ex["tg_list"]) & 0xFF
            # The per-channel DMR ID and the encryption byte share offset 41.
            # A DMR ID (>0) wins; otherwise apply the encryption selector.
            if "dmrid" in ex or "encrypt" in ex:
                dmrid = int(ex["dmrid"]) if "dmrid" in ex else 0
                if dmrid > 0:
                    _channel_set_optional_dmrid(raw, dmrid)
                else:
                    _channel_set_optional_dmrid(raw, 0)
                    if "encrypt" in ex:
                        raw[41] = _enc_label_to_byte(str(ex["encrypt"]))

    @staticmethod
    def _encode_tones(mem):
        def css_tone(freq):
            return _int2bcd(int(round(freq * 10)))

        def css_dtcs(code, pol):
            v = _int2bcd(code) | 0x8000
            if pol == "R":
                v |= 0x4000
            return v

        tx = rx = CSS_NONE
        if mem.tmode == "Tone":
            tx = css_tone(mem.rtone)
        elif mem.tmode == "TSQL":
            tx = rx = css_tone(mem.ctone)
        elif mem.tmode == "DTCS":
            pol = mem.dtcs_polarity or "NN"
            tx = css_dtcs(mem.dtcs, pol[0])
            rx = css_dtcs(mem.dtcs, pol[1])
        return tx, rx

    # -- zones (CHIRP banks) --------------------------------------------
    def get_bank_model(self):
        return OpenGD77BankModel(self, "Zones")

    def _channels_per_zone(self):
        b = self._img()[IMG_ZONE + ZONE_DETECT_OFF]
        return 80 if b <= 0x04 else 16

    def _zone_stride(self):
        return ZONE_NAME_LEN + 2 * self._channels_per_zone()

    def _zone_inuse(self, img, i):
        return bool((img[IMG_ZONE + i // 8] >> (i % 8)) & 0x01)

    def _zone_slot(self, i):
        return IMG_ZONE + ZONE_BITMAP_LEN + i * self._zone_stride()

    def _zone_name(self, img, i):
        o = self._zone_slot(i)
        return _decode_name(img[o:o + ZONE_NAME_LEN])

    def _zone_label(self, img, i):
        return self._zone_name(img, i) or ("Zone %d" % (i + 1))

    def _zone_channel_list(self, i):
        img = self._img()
        if not self._zone_inuse(img, i):
            return []
        cpz = self._channels_per_zone()
        base = self._zone_slot(i) + ZONE_NAME_LEN
        out = []
        for k in range(cpz):
            ch = struct.unpack_from("<H", img, base + k * 2)[0]
            if ch == 0:
                break
            out.append(ch)
        return out

    def _zone_write(self, i, name, channels):
        img = bytearray(self._img())
        cpz = self._channels_per_zone()
        o = self._zone_slot(i)
        img[o:o + ZONE_NAME_LEN] = _encode_name(name, ZONE_NAME_LEN)
        base = o + ZONE_NAME_LEN
        for k in range(cpz):
            ch = channels[k] if k < len(channels) else 0
            struct.pack_into("<H", img, base + k * 2, ch)
        bmbyte = IMG_ZONE + i // 8
        if channels or name:
            img[bmbyte] |= (1 << (i % 8))
        else:
            img[bmbyte] = img[bmbyte] & ~(1 << (i % 8)) & 0xFF
        self._mmap = memmap.MemoryMapBytes(bytes(img))
        self._build_caches()      # zone membership changed

    def _zone_add(self, i, ch):
        chans = self._zone_channel_list(i)
        if ch not in chans:
            chans.append(ch)
        self._zone_write(i, self._zone_label(self._img(), i), chans)

    def _zone_remove(self, i, ch):
        chans = [c for c in self._zone_channel_list(i) if c != ch]
        name = self._zone_name(self._img(), i)
        if not chans and not name:
            self._zone_write(i, "", [])
        else:
            self._zone_write(i, name or ("Zone %d" % (i + 1)), chans)

    def _zone_set_name(self, i, name):
        self._zone_write(i, name, self._zone_channel_list(i))

    # -- contacts & RX groups -------------------------------------------
    def _contact_off(self, i):
        return IMG_CONTACTS + (i - 1) * CONTACT_SIZE

    def _contact_inuse(self, img, i):
        return img[self._contact_off(i)] != 0xFF

    def _contact_name(self, img, i):
        o = self._contact_off(i)
        return _decode_name(img[o:o + 16])

    def _contact_get(self, img, i):
        o = self._contact_off(i)
        name = _decode_name(img[o:o + 16])
        num = _bcd2int(int.from_bytes(img[o + 16:o + 20], "big"))
        if not (0 <= num <= 16777215):
            num = 0
        ctype = img[o + 20] if img[o + 20] < len(CONTACT_TYPES) else 0
        return name, num, ctype

    def _rxgroup_off(self, i):
        return IMG_RXGROUP + (RXGROUP_ADDR - RXGROUP_LEN_ADDR) + (i - 1) * RXGROUP_SIZE

    def _rxgroup_inuse(self, img, i):
        n = img[IMG_RXGROUP + (i - 1)]      # length-table byte
        return 0 < n <= 32

    def _rxgroup_name(self, img, i):
        o = self._rxgroup_off(i)
        return _decode_name(img[o:o + 16])

    def _rxgroup_members(self, img, i):
        if not self._rxgroup_inuse(img, i):
            return []
        o = self._rxgroup_off(i) + 16
        out = []
        for k in range(32):
            c = struct.unpack_from("<H", img, o + k * 2)[0]
            if c == 0:
                break
            out.append(c)
        return out

    def _dtmf_off(self, i):
        return IMG_DTMF + (i - 1) * DTMF_SIZE

    def _dtmf_inuse(self, img, i):
        b = img[self._dtmf_off(i)]
        return b != 0xFF and b != 0x00

    def _dtmf_get(self, img, i):
        o = self._dtmf_off(i)
        name = _decode_name(img[o:o + 16])
        code = []
        for k in range(16):
            v = img[o + 16 + k]
            if v == 0xFF:
                break
            code.append(DTMF_DIGITS[v] if v < len(DTMF_DIGITS) else "?")
        return name, "".join(code)

    def _build_caches(self):
        img = self._img()
        self._contacts = [(i, self._contact_name(img, i))
                          for i in range(1, CONTACTS_MAX + 1)
                          if self._contact_inuse(img, i)]
        self._rxgroups = [(i, self._rxgroup_name(img, i))
                          for i in range(1, RXGROUPS_MAX + 1)
                          if self._rxgroup_inuse(img, i)]
        # Reverse map channel-number -> [zone slot indices] so that the bank
        # editor's get_memory_mappings() is O(1) instead of O(zones) per memory.
        self._zone_member_map = {}
        for i in range(ZONES_MAX):
            if self._zone_inuse(img, i):
                for ch in self._zone_channel_list(i):
                    self._zone_member_map.setdefault(ch, []).append(i)

    @staticmethod
    def _parse_index(value):
        s = str(value)
        if s.startswith("None"):
            return 0
        try:
            return int(s.split(":", 1)[0])
        except ValueError:
            return 0

    # -- settings: general + AES key store ------------------------------
    def _get_radio_settings(self):
        gs = self._img()[IMG_GENSET:IMG_GENSET + GENSET_LEN]
        radio = RadioSettingGroup("radio", "Radio")
        cs = RadioSetting(
            "callsign", "Callsign (radio name)",
            RadioSettingValueString(0, 8, _decode_name(gs[0:8]), autopad=False))
        cs.set_doc("Your callsign / radio name (up to 8 characters), shown on the "
                   "radio.")
        radio.append(cs)
        dmrid = _bcd2int(int.from_bytes(gs[8:12], "big"))
        if not (0 <= dmrid <= 16777215):        # unset / 0xFF -> show 0
            dmrid = 0
        di = RadioSetting(
            "dmrid", "DMR ID",
            RadioSettingValueInteger(0, 16777215, dmrid))
        di.set_doc("Your personal 7-digit DMR ID (from radioid.net). Used for all "
                   "DMR transmits unless a channel overrides it.")
        radio.append(di)

        img = self._img()
        b1 = RadioSetting(
            "boot_line1", "Boot text line 1",
            RadioSettingValueString(0, 16, _decode_name(img[IMG_BOOT:IMG_BOOT + 16]),
                                    autopad=False))
        b1.set_doc("First line of the power-on text greeting (when boot screen = "
                   "Text).")
        radio.append(b1)
        b2 = RadioSetting(
            "boot_line2", "Boot text line 2",
            RadioSettingValueString(0, 16,
                                    _decode_name(img[IMG_BOOT + 16:IMG_BOOT + 32]),
                                    autopad=False))
        b2.set_doc("Second line of the power-on text greeting.")
        radio.append(b2)
        bs = RadioSetting(
            "boot_screen", "Boot screen",
            RadioSettingValueList(["Picture", "Text"],
                                  current_index=1 if img[IMG_BOOTTYPE] == 1 else 0))
        bs.set_doc("Power-on screen: a stored Picture, or the two Text lines above.")
        radio.append(bs)

        hdr = img[IMG_DMRIDHDR:IMG_DMRIDHDR + DMRID_HEADER_LEN]
        if hdr[0:2] == b"Id":
            db = "%d entries" % int.from_bytes(hdr[8:12], "little")
        else:
            db = "not loaded (use the OpenGD77 CPS downloader)"
        dbset = RadioSetting("dmrid_db", "DMR-ID database (read-only)",
                             RadioSettingValueString(0, 50, db, autopad=False))
        dbset.set_doc("Informational. Use the import field below to write it.")
        radio.append(dbset)

        imp = RadioSetting(
            "dmrid_import",
            "Import DMR-ID DB from CSV (path; written on Upload)",
            RadioSettingValueString(0, 255, "", autopad=False))
        imp.set_doc("Path to a radioid.net-style CSV (id, callsign, name). On the "
                    "next Upload the DMR-ID database is built and written to the "
                    "radio (up to %d entries; pre-filter larger lists). Reboot "
                    "the radio afterwards to load it." % DMRID_MAX_ENTRIES)
        radio.append(imp)

        sat = RadioSetting(
            "sat_import",
            "Import satellites from TLE file (path; written on Upload)",
            RadioSettingValueString(0, 255, "", autopad=False))
        sat.set_doc("Path to a 2- or 3-line TLE file (e.g. CelesTrak amateur.txt). "
                    "On the next Upload, satellites whose names match the built-in "
                    "FM-transponder table (ISS, SO-50, AO-91/92/27, PO-101, ...) "
                    "are written to the radio's Satellite menu (up to 25). Needs a "
                    "GPS fix or set location to predict passes.")
        radio.append(sat)
        return radio

    def _get_rxgroup_settings(self):
        img = self._img()
        group = RadioSettingGroup("rxgroups", "RX Groups")
        inuse = [i for i in range(1, RXGROUPS_MAX + 1)
                 if self._rxgroup_inuse(img, i)]
        spares = [i for i in range(1, RXGROUPS_MAX + 1)
                  if not self._rxgroup_inuse(img, i)][:4]
        for i in sorted(set(inuse) | set(spares)):
            name = self._rxgroup_name(img, i) if self._rxgroup_inuse(img, i) else ""
            members = ",".join(str(c) for c in self._rxgroup_members(img, i))
            sub = RadioSettingGroup("rxgroup_%d" % i, "RX Group %d" % i)
            gn = RadioSetting(
                "rxgroup_%d_name" % i, "Name",
                RadioSettingValueString(0, 16, name, autopad=False))
            gn.set_doc("RX group name (blank = delete this group). A channel's "
                       "'RX group list' points here to choose which talkgroups it "
                       "listens to.")
            sub.append(gn)
            gm = RadioSetting(
                "rxgroup_%d_members" % i, "Contact indices (comma-separated)",
                RadioSettingValueString(0, 200, members, autopad=False,
                                        charset="0123456789, "))
            gm.set_doc("Contacts in this group, by their Contact number "
                       "(1-based), comma-separated, e.g. 1,4,5.")
            sub.append(gm)
            group.append(sub)
        return group

    def _get_dtmf_settings(self):
        img = self._img()
        group = RadioSettingGroup("dtmf", "DTMF Contacts")
        inuse = [i for i in range(1, DTMF_MAX + 1) if self._dtmf_inuse(img, i)]
        spares = [i for i in range(1, DTMF_MAX + 1)
                  if not self._dtmf_inuse(img, i)][:4]
        for i in sorted(set(inuse) | set(spares)):
            name, code = self._dtmf_get(img, i)
            sub = RadioSettingGroup("dtmf_%d" % i, "DTMF %d" % i)
            dn = RadioSetting(
                "dtmf_%d_name" % i, "Name",
                RadioSettingValueString(0, 16, name, autopad=False))
            dn.set_doc("DTMF contact name (blank = delete this entry).")
            sub.append(dn)
            dc = RadioSetting(
                "dtmf_%d_code" % i, "Code (0-9 A-D * #)",
                RadioSettingValueString(0, 16, code, autopad=False,
                                        charset=DTMF_DIGITS))
            dc.set_doc("DTMF digit sequence to send (digits 0-9, A-D, * and #).")
            sub.append(dc)
            group.append(sub)
        return group

    def _get_contacts_settings(self):
        img = self._img()
        group = RadioSettingGroup("contacts", "Contacts")
        inuse = [i for i in range(1, CONTACTS_MAX + 1)
                 if self._contact_inuse(img, i)]
        spares = [i for i in range(1, CONTACTS_MAX + 1)
                  if not self._contact_inuse(img, i)][:8]
        for i in sorted(set(inuse) | set(spares)):
            name, num, ctype = self._contact_get(img, i)
            sub = RadioSettingGroup("contact_%d" % i, "Contact %d" % i)
            cn = RadioSetting(
                "contact_%d_name" % i, "Name",
                RadioSettingValueString(0, 16, name, autopad=False))
            cn.set_doc("Digital contact name (blank = delete this contact).")
            sub.append(cn)
            cnum = RadioSetting(
                "contact_%d_num" % i, "Number (TG/ID)",
                RadioSettingValueInteger(0, 16777215, num))
            cnum.set_doc("Talkgroup number (Group Call) or DMR ID (Private Call).")
            sub.append(cnum)
            ctp = RadioSetting(
                "contact_%d_type" % i, "Type",
                RadioSettingValueList(
                    CONTACT_TYPES,
                    current_index=ctype if ctype < len(CONTACT_TYPES) else 0))
            ctp.set_doc("Group Call (talkgroup), Private Call (individual), or "
                        "All Call.")
            sub.append(ctp)
            group.append(sub)
        return group

    def _settings_blob(self):
        return bytes(self._img()[IMG_SETTINGS:IMG_SETTINGS + SETTINGS_LEN])

    def _settings_valid(self, blob=None):
        if blob is None:
            blob = self._settings_blob()
        return (len(blob) >= SETTINGS_LEN and
                struct.unpack_from("<I", blob, 0)[0] == SETTINGS_MAGIC)

    def _get_radiowide_settings(self):
        top = RadioSettingGroup("rsettings", "Radio settings")
        blob = self._settings_blob()
        if not self._settings_valid(blob):
            note = RadioSetting(
                "rsettings_note", "Radio settings",
                RadioSettingValueString(
                    0, 80, "Not available (no settings block on this image).",
                    autopad=False))
            note.set_doc("The OpenGD77 settings block was not found / not "
                         "initialised; nothing to edit here.")
            top.append(note)
            return top

        subs = []
        sub_by_key = {}
        for key, label in (("rs_audio", "Audio"), ("rs_display", "Display"),
                           ("rs_scan", "Scan"), ("rs_dmr", "DMR"),
                           ("rs_power", "Power & timers"),
                           ("rs_opts", "Options")):
            g = RadioSettingGroup(key, label)
            subs.append(g)
            sub_by_key[key] = g

        for grp, key, off, kind, opt, label, doc in SETTINGS_FIELDS:
            if kind == "list":
                idx = blob[off] if blob[off] < len(opt) else 0
                rs = RadioSetting(key, label,
                                  RadioSettingValueList(opt, current_index=idx))
            else:
                val = blob[off]
                if kind == "sint" and val >= 128:
                    val -= 256
                lo, hi = opt
                rs = RadioSetting(key, label, RadioSettingValueInteger(
                    min(lo, val), max(hi, val), val))
            if doc:
                rs.set_doc(doc)
            sub_by_key[grp].append(rs)

        bits = struct.unpack_from("<I", blob, SETTINGS_BITOPTS_OFF)[0]
        for key, bit, label, doc in SETTINGS_BITS:
            rs = RadioSetting(key, label,
                              RadioSettingValueBoolean(bool(bits & (1 << bit))))
            if doc:
                rs.set_doc(doc)
            sub_by_key["rs_opts"].append(rs)

        # GPS mode = low nibble of gpsModeAndBaudsIndex (0 = "not detected", a
        # runtime state -> shown as Off). High nibble (baud) preserved on write.
        gmode = blob[SETTINGS_GPSMODE_OFF] & 0x0F
        gps_rs = RadioSetting(
            "set_gpsMode", "GPS mode",
            RadioSettingValueList(
                RS_GPS_MODES,
                current_index=(gmode - 1) if 1 <= gmode <= len(RS_GPS_MODES) else 0))
        gps_rs.set_doc("GPS receiver mode (Off / On / On+NMEA output / "
                       "On+logging). Takes effect after a reboot; the GPS baud "
                       "rate is preserved.")
        sub_by_key["rs_power"].append(gps_rs)

        # Time zone = low 7 bits of the timezone byte (64 = UTC, 15-min steps);
        # bit 7 = show-local-time flag.  Both bits' neighbours are preserved.
        tzval = blob[SETTINGS_TZ_OFF] & 0x7F
        tzidx = tzval - 16
        if not 0 <= tzidx < len(RS_TZ_OFFSETS):
            tzidx = 64 - 16                     # default to UTC if out of range
        tz_rs = RadioSetting("set_timezone", "Time zone",
                             RadioSettingValueList(RS_TZ_OFFSETS,
                                                   current_index=tzidx))
        tz_rs.set_doc("UTC offset for the clock, in 15-minute steps "
                      "(UTC-12:00 .. UTC+14:00).")
        sub_by_key["rs_display"].append(tz_rs)
        tl_rs = RadioSetting(
            "set_timeLocal", "Time display",
            RadioSettingValueList(
                RS_TZ_DISPLAY,
                current_index=1 if (blob[SETTINGS_TZ_OFF] & 0x80) else 0))
        tl_rs.set_doc("Show the clock in UTC or in Local time (= UTC + the time "
                      "zone above).")
        sub_by_key["rs_display"].append(tl_rs)

        for g in subs:
            top.append(g)
        return top

    def _get_quickkeys_settings(self):
        img = self._img()
        contacts = getattr(self, "_contacts", [])
        group = RadioSettingGroup("quickkeys", "Quick Keys")
        for k in range(QUICKKEYS_COUNT):
            val = struct.unpack_from("<H", img, IMG_QUICKKEYS + k * 2)[0]
            options = ["(empty)"] + ["%d: %s" % (i, n) for i, n in contacts]
            current = "(empty)"
            if not _quickkey_is_empty(val):
                if not (val & 0x8000):              # Contact quick key
                    name = next((n for i, n in contacts if i == val), None)
                    current = "%d: %s" % (val, name or "?")
                    if current not in options:
                        options.append(current)     # contact not in use -> "idx: ?"
                else:                               # Menu shortcut -- preserved
                    current = "Menu shortcut 0x%04X" % val
                    options.append(current)
            rs = RadioSetting(
                "quickkey_%d" % k, "Long-press key %d" % k,
                RadioSettingValueList(options, current_index=options.index(current)))
            rs.set_doc(
                "Long-press number key %d on the home screen to dial the chosen "
                "contact (sets the TX talkgroup).  'Menu shortcut' entries were "
                "assigned on the radio and are preserved; pick a contact or "
                "(empty) to change this key." % k)
            group.append(rs)
        return group

    def get_settings(self):
        radio = self._get_radio_settings()
        rwsettings = self._get_radiowide_settings()
        quickkeys = self._get_quickkeys_settings()
        contacts = self._get_contacts_settings()
        rxgroups = self._get_rxgroup_settings()
        dtmf = self._get_dtmf_settings()
        store = self._read_aes_store()
        aes = RadioSettingGroup("aes", "AES Keys")

        note = RadioSetting(
            "aes_note", "Byte order",
            RadioSettingValueString(
                0, 80,
                "Enter keys in radio/CPS order (reverse of aes256.dec).",
                autopad=False))
        note.set_doc("Informational; keys are 64 hex chars (32 bytes), "
                     "MSB-first as shown on the radio.")
        aes.append(note)

        tx = RadioSetting(
            "aes_txkeyid", "TX key id (0 = encrypted TX off)",
            RadioSettingValueInteger(0, 15, store.tx_key_id))
        tx.set_doc("Which AES key id (1-15) channels set to 'Inherit' transmit "
                   "with; 0 = transmit unencrypted.")
        aes.append(tx)

        # Keys are addressed by key id 1..15 (key id 0 = "off"/"inherit" in the
        # TX and per-channel selectors), matching the channel dropdowns. Each key
        # id is matched to its storage slot by the slot's key_id, not its
        # position, so a radio that stores key id 1 in slot 0 still shows here as
        # "Key id 1".
        for k in range(1, AESK_NUM_SLOTS):
            slot = store.key_for(k)
            ven = RadioSetting(
                "aes_valid_%d" % k, "Key id %d enabled" % k,
                RadioSettingValueBoolean(slot is not None))
            ven.set_doc("Enable AES key id %d. Disable to clear/delete the key." % k)
            aes.append(ven)
            kv = RadioSetting(
                "aes_key_%d" % k, "Key id %d (64 hex)" % k,
                RadioSettingValueString(0, 64, slot.key_hex if slot else "",
                                        autopad=False, charset=HEX))
            kv.set_doc("AES-256 key id %d: 64 hex chars (32 bytes), in radio/CPS "
                       "byte order (reverse of aes256.dec)." % k)
            aes.append(kv)
        return RadioSettings(radio, rwsettings, quickkeys, contacts, rxgroups,
                             dtmf, aes)

    def set_settings(self, settings):
        flat = {}

        def walk(group):
            for el in group:
                if isinstance(el, RadioSetting):
                    flat[el.get_name()] = el
                else:
                    walk(el)
        walk(settings)

        img = bytearray(self._img())

        # -- general (radio) settings --
        if "callsign" in flat:
            img[IMG_GENSET:IMG_GENSET + 8] = _encode_name(
                str(flat["callsign"].value), 8)
        if "dmrid" in flat:
            img[IMG_GENSET + 8:IMG_GENSET + 12] = \
                _int2bcd(int(flat["dmrid"].value)).to_bytes(4, "big")
        if "boot_line1" in flat:
            img[IMG_BOOT:IMG_BOOT + 16] = _encode_name(
                str(flat["boot_line1"].value), 16)
        if "boot_line2" in flat:
            img[IMG_BOOT + 16:IMG_BOOT + 32] = _encode_name(
                str(flat["boot_line2"].value), 16)
        if "boot_screen" in flat:
            img[IMG_BOOTTYPE] = 1 if str(flat["boot_screen"].value) == "Text" else 0
        if "dmrid_import" in flat:
            p = str(flat["dmrid_import"].value).strip()
            if p:
                self._dmrid_import_path = p
        if "sat_import" in flat:
            p = str(flat["sat_import"].value).strip()
            if p:
                self._satellite_import_path = p

        # -- radio-wide settings (nonVolatileSettings) --
        # Patch only the bytes of fields the user changed; magicNumber and every
        # unmanaged byte (including the 0x6000-0x604A neighbour) are preserved,
        # so the firmware keeps its settings and never factory-resets.
        sbase = IMG_SETTINGS
        if self._settings_valid(bytes(img[sbase:sbase + SETTINGS_LEN])):
            for grp, key, off, kind, opt, label, doc in SETTINGS_FIELDS:
                if key not in flat:
                    continue
                v = flat[key].value
                if kind == "list":
                    s = str(v)
                    if s in opt:
                        img[sbase + off] = opt.index(s)
                else:
                    img[sbase + off] = int(v) & 0xFF
            if any(key in flat for key, _, _, _ in SETTINGS_BITS):
                bits = struct.unpack_from("<I", bytes(img),
                                          sbase + SETTINGS_BITOPTS_OFF)[0]
                for key, bit, label, doc in SETTINGS_BITS:
                    if key not in flat:
                        continue
                    if bool(flat[key].value):
                        bits |= (1 << bit)
                    else:
                        bits &= ~(1 << bit)
                struct.pack_into("<I", img, sbase + SETTINGS_BITOPTS_OFF,
                                 bits & 0xFFFFFFFF)
            if "set_gpsMode" in flat:
                # Patch only the low (mode) nibble; keep the high (baud) nibble.
                s = str(flat["set_gpsMode"].value)
                mode = (RS_GPS_MODES.index(s) + 1) if s in RS_GPS_MODES else 1
                o = sbase + SETTINGS_GPSMODE_OFF
                img[o] = (img[o] & 0xF0) | (mode & 0x0F)
            if "set_timezone" in flat:
                s = str(flat["set_timezone"].value)
                if s in RS_TZ_OFFSETS:
                    tzv = 16 + RS_TZ_OFFSETS.index(s)   # 7-bit offset value
                    o = sbase + SETTINGS_TZ_OFF
                    img[o] = (img[o] & 0x80) | (tzv & 0x7F)
            if "set_timeLocal" in flat:
                o = sbase + SETTINGS_TZ_OFF
                if str(flat["set_timeLocal"].value) == "Local":
                    img[o] |= 0x80
                else:
                    img[o] &= ~0x80 & 0xFF

        # -- DTMF contacts --
        for i in range(1, DTMF_MAX + 1):
            nk = "dtmf_%d_name" % i
            if nk not in flat:
                continue
            o = IMG_DTMF + (i - 1) * DTMF_SIZE
            name = str(flat[nk].value).strip()
            if name:
                img[o:o + 16] = _encode_name(name, 16)
                code = str(flat["dtmf_%d_code" % i].value).strip().upper()
                cb = bytearray(b"\xFF" * 16)
                for k, ch in enumerate(code[:16]):
                    if ch in DTMF_DIGITS:
                        cb[k] = DTMF_DIGITS.index(ch)
                img[o + 16:o + 32] = bytes(cb)
            else:
                img[o:o + DTMF_SIZE] = b"\xFF" * DTMF_SIZE

        # -- contacts --
        for i in range(1, CONTACTS_MAX + 1):
            nk = "contact_%d_name" % i
            if nk not in flat:
                continue
            o = IMG_CONTACTS + (i - 1) * CONTACT_SIZE
            name = str(flat[nk].value).strip()
            if name:
                was = img[o] != 0xFF
                img[o:o + 16] = _encode_name(name, 16)
                num = int(flat["contact_%d_num" % i].value)
                img[o + 16:o + 20] = _int2bcd(num).to_bytes(4, "big")
                tstr = str(flat["contact_%d_type" % i].value)
                img[o + 20] = (CONTACT_TYPES.index(tstr)
                               if tstr in CONTACT_TYPES else 0)
                if not was:
                    img[o + 21] = 0          # callRxTone
                    img[o + 22] = 0          # ringStyle
                    img[o + 23] = 0xFF       # reserve1 (no TS override)
            else:
                img[o:o + CONTACT_SIZE] = b"\xFF" * CONTACT_SIZE

        # -- RX groups --
        for i in range(1, RXGROUPS_MAX + 1):
            nk = "rxgroup_%d_name" % i
            if nk not in flat:
                continue
            lenoff = IMG_RXGROUP + (i - 1)
            o = self._rxgroup_off(i)
            name = str(flat[nk].value).strip()
            mstr = str(flat["rxgroup_%d_members" % i].value).replace(" ", "")
            members = [int(x) for x in mstr.split(",")
                       if x.isdigit() and 0 < int(x) <= CONTACTS_MAX][:32]
            if name and members:
                img[lenoff] = len(members)
                img[o:o + 16] = _encode_name(name, 16)
                for k in range(32):
                    struct.pack_into("<H", img, o + 16 + k * 2,
                                     members[k] if k < len(members) else 0)
            elif 0 < img[lenoff] <= 32:          # was in use -> delete
                img[lenoff] = 0
                img[o:o + 16] = b"\xFF" * 16
                for k in range(32):
                    struct.pack_into("<H", img, o + 16 + k * 2, 0)

        # -- quick keys --
        # Each "quickkey_N" is a dropdown: (empty) / "idx: contact" / a preserved
        # "Menu shortcut 0xXXXX".  Leaving an already-empty slot empty keeps its
        # exact bytes (no spurious sector write); clearing a set slot writes 0.
        for k in range(QUICKKEYS_COUNT):
            qk = "quickkey_%d" % k
            if qk not in flat:
                continue
            off = IMG_QUICKKEYS + k * 2
            orig = struct.unpack_from("<H", bytes(img), off)[0]
            s = str(flat[qk].value)
            if s.startswith("Menu shortcut 0x"):
                val = int(s[len("Menu shortcut 0x"):][:4], 16)
            elif s == "(empty)":
                val = orig if _quickkey_is_empty(orig) else 0x0000
            else:
                val = self._parse_index(s) & 0x7FFF      # contact index (bit15=0)
            struct.pack_into("<H", img, off, val & 0xFFFF)

        # -- AES key store --
        store = AesKeyStore.from_payload(
            find_aes_block(bytes(img[IMG_AES:IMG_AES + SECTOR_SIZE]))[1])
        if "aes_txkeyid" in flat:
            store.tx_key_id = int(flat["aes_txkeyid"].value)
        for k in range(1, AESK_NUM_SLOTS):
            vkey = "aes_valid_%d" % k
            kkey = "aes_key_%d" % k
            if vkey not in flat and kkey not in flat:
                continue
            cur = store.key_for(k)
            valid = bool(flat[vkey].value) if vkey in flat else (cur is not None)
            hexstr = str(flat[kkey].value).strip() if kkey in flat \
                else (cur.key_hex if cur else "")
            if valid:
                if len(hexstr) != 64:
                    raise errors.InvalidValueError(
                        "AES key id %d must be exactly 64 hex characters "
                        "(got %d)" % (k, len(hexstr)))
                try:
                    key = bytes.fromhex(hexstr)
                except ValueError:
                    raise errors.InvalidValueError(
                        "AES key id %d is not valid hex" % k)
                store.set_key(k, key)
            else:
                store.clear_key(k)
        # Drop unusable/corruption slots: a stored key with key id 0 ("off"
        # sentinel) or out of 1..15 can never be selected and only appears from
        # older buggy writes. Clearing it keeps the store clean.
        for i, s in enumerate(store.slots):
            if s.valid and not (1 <= s.key_id <= 15):
                store.slots[i] = AesSlot(False, 0, b"")
        region = region_with_aes(
            bytes(img[IMG_AES:IMG_AES + SECTOR_SIZE]), store.to_payload())
        img[IMG_AES:IMG_AES + SECTOR_SIZE] = region

        self._mmap = memmap.MemoryMapBytes(bytes(img))
        self._build_caches()      # contact/RX-group edits affect channel dropdowns


# --------------------------------------------------------------------------
# Zones surface as CHIRP banks (a channel may be in several zones).
# --------------------------------------------------------------------------
class OpenGD77Bank(chirp_common.NamedBank):
    def set_name(self, name):
        chirp_common.NamedBank.set_name(self, name)
        self._model._radio._zone_set_name(self.get_index(), name)


class OpenGD77BankModel(chirp_common.MTOBankModel):
    # Showing all 68 zone slots makes the bank grid huge and slow to build; show
    # the in-use zones plus a few empty slots for creating new zones (reload the
    # tab for more spares once these are used).
    SPARE_ZONES = 8

    def _shown_indices(self):
        radio = self._radio
        img = radio._img()
        inuse = [i for i in range(ZONES_MAX) if radio._zone_inuse(img, i)]
        spares = [i for i in range(ZONES_MAX)
                  if not radio._zone_inuse(img, i)][:self.SPARE_ZONES]
        return sorted(set(inuse) | set(spares))

    def get_num_mappings(self):
        return ZONES_MAX

    def get_mappings(self):
        radio = self._radio
        img = radio._img()
        return [OpenGD77Bank(self, i, radio._zone_label(img, i))
                for i in self._shown_indices()]

    def add_memory_to_mapping(self, memory, mapping):
        self._radio._zone_add(mapping.get_index(), memory.number)

    def remove_memory_from_mapping(self, memory, mapping):
        self._radio._zone_remove(mapping.get_index(), memory.number)

    def get_mapping_memories(self, mapping):
        return [self._radio.get_memory(n)
                for n in self._radio._zone_channel_list(mapping.get_index())]

    def get_memory_mappings(self, memory):
        radio = self._radio
        img = radio._img()
        zones = getattr(radio, "_zone_member_map", {}).get(memory.number, [])
        return [OpenGD77Bank(self, i, radio._zone_label(img, i)) for i in zones]
