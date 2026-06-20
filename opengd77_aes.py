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
IMAGE_SIZE = IMG_RXGROUP + RXGROUP_REGION_LEN

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


def _write_region(pipe, raw_addr, new_data, old_data=None):
    """Write @new_data to raw SPI-flash @raw_addr, one 4 KB sector at a time.

    Only this region's bytes are sent; the firmware preserves the rest of each
    touched sector (it reads the sector, patches, erases, writes back).  This is
    how EEPROM-resident codeplug data is written on MD-UV380 (EEPROM == SPI
    flash at offset 0; the dedicated EEPROM write command is a no-op there).

    If @old_data is given, sectors whose region-bytes are unchanged are skipped.
    Each written sector is verified by read-back.  Returns the number of sectors
    written.
    """
    n = len(new_data)
    end = raw_addr + n
    sector = raw_addr // SECTOR_SIZE
    written = 0
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

    POWER_LEVELS = [
        chirp_common.PowerLevel("High", watts=10.0),
        chirp_common.PowerLevel("Low", watts=1.0),
    ]

    @classmethod
    def get_prompts(cls):
        rp = chirp_common.RadioPrompts()
        rp.experimental = (
            "This driver targets the OpenGD77-AES256 firmware.  Supported: "
            "AES-256 key management (Settings -> AES Keys), channel read/write "
            "(memories 1-1024, analog + DMR), zones (as banks), digital "
            "contacts and the radio callsign / DMR ID.  DTMF contacts, RX-group "
            "membership editing and the DMR-ID database are still in "
            "development.\n\n"
            "AES voice encryption is only legal on licensed commercial / PMR "
            "allocations -- NOT on amateur (ham) bands.")
        rp.pre_download = (
            "1. Connect the radio via USB.\n"
            "2. The radio should be on (normal mode).\n"
            "3. Click OK to download.")
        rp.pre_upload = (
            "Upload writes the AES key store and channels (only changed flash "
            "sectors are written).  Zones, contacts and other objects are not "
            "written back yet.")
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
        rf.valid_modes = ["FM", "NFM", "DMR"]
        rf.valid_tmodes = ["", "Tone", "TSQL", "DTCS"]
        rf.valid_power_levels = self.POWER_LEVELS
        rf.valid_duplexes = ["", "-", "+", "off"]
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
            img = self._mmap.get_packed()
            orig = getattr(self, "_orig", None)
            status = chirp_common.Status()
            status.msg = "Writing codeplug"
            status.max = len(_WRITE_SPANS)
            total = 0
            for i, (img_off, raw_addr, length) in enumerate(_WRITE_SPANS):
                new = img[img_off:img_off + length]
                old = orig[img_off:img_off + length] if orig else None
                total += _write_region(pipe, raw_addr, new, old)
                status.cur = i + 1
                self.status_fn(status)
            LOG.info("upload wrote %d sector(s)", total)
        finally:
            _close_cps(pipe)

    def process_mmap(self):
        # Channels are parsed on demand in get_memory(); precompute the contact
        # and RX-group name caches used for channel dropdowns.
        self._build_caches()

    # -- AES key store access on the image ------------------------------
    def _aes_region(self):
        return self._mmap.get_packed()[IMG_AES:IMG_AES + SECTOR_SIZE]

    def _read_aes_store(self):
        _, payload = find_aes_block(self._aes_region())
        return AesKeyStore.from_payload(payload)

    def _write_aes_store(self, store):
        region = region_with_aes(self._aes_region(), store.to_payload())
        self._mmap[IMG_AES] = region

    # -- channels (READ-ONLY in this build) -----------------------------
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
        data = self._mmap.get_packed()
        _, bm, bit = self._channel_offset(number)
        return bool((data[bm] >> bit) & 0x01)

    def get_raw_memory(self, number):
        off, _, _ = self._channel_offset(number)
        return util.hexprint(self._mmap.get_packed()[off:off + CH_SIZE])

    def get_memory(self, number):
        mem = chirp_common.Memory()
        mem.number = number
        if not self._channel_in_use(number):
            mem.empty = True
            return mem

        off, _, _ = self._channel_offset(number)
        raw = self._mmap.get_packed()[off:off + CH_SIZE]
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

        mem.power = self.POWER_LEVELS[0 if (flag4 & 0x80) else 1]
        if flag4 & 0x04:                       # RX_ONLY
            mem.duplex = "off"
        if (flag4 & 0x20) or (flag4 & 0x10):   # ZONE_SKIP / ALL_SKIP
            mem.skip = "S"

        self._add_dmr_extras(mem, raw)
        return mem

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
        """Build (options, current) for a 'idx: name' dropdown with a None entry,
        ensuring the current index is always representable."""
        options = ["None"] + ["%d: %s" % (i, n) for i, n in items]
        current = "None"
        if current_index:
            current = next(("%d: %s" % (i, n) for i, n in items
                            if i == current_index), None)
            if current is None:
                current = "%d: ?" % current_index
                options.append(current)
        return options, current

    def _add_dmr_extras(self, mem, raw):
        group = RadioSettingGroup("dmr", "DMR")
        optional_dmrid = bool(raw[38] & 0x80)
        group.append(RadioSetting(
            "cc", "Colour code",
            RadioSettingValueInteger(0, 15, raw[44] & 0x0F)))
        group.append(RadioSetting(
            "ts", "Timeslot",
            RadioSettingValueInteger(1, 2, 2 if (raw[49] & 0x40) else 1)))

        opts, cur = self._index_list(getattr(self, "_contacts", []),
                                     struct.unpack_from("<H", raw, 46)[0])
        group.append(RadioSetting("contact", "Contact (TX talkgroup)",
                                  RadioSettingValueList(opts, cur)))

        opts, cur = self._index_list(getattr(self, "_rxgroups", []), raw[43])
        group.append(RadioSetting("tg_list", "RX group list",
                                  RadioSettingValueList(opts, cur)))

        if not optional_dmrid:
            group.append(RadioSetting(
                "encrypt", "Privacy / AES key selector (encrypt byte)",
                RadioSettingValueInteger(0, 255, raw[41])))
        mem.extra = group

    def set_memory(self, mem):
        img = bytearray(self._mmap.get_packed())
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

        f4 = raw[51]
        if mem.power in self.POWER_LEVELS:
            high = self.POWER_LEVELS.index(mem.power) == 0
        else:
            high = True
        f4 = (f4 | 0x80) if high else (f4 & ~0x80)
        if mem.mode == "FM":
            f4 |= 0x02                           # 25 kHz
        else:
            f4 &= ~0x02                          # NFM / DMR -> 12.5 kHz
        if mem.duplex == "off":
            f4 |= 0x04                           # RX only
        else:
            f4 &= ~0x04
        if mem.skip == "S":
            f4 |= 0x20                           # zone skip
        else:
            f4 &= ~0x20
        raw[51] = f4 & 0xFF

        if mem.extra:
            ex = {s.get_name(): s.value for s in mem.extra}
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
            if "encrypt" in ex:
                raw[41] = int(ex["encrypt"]) & 0xFF

        img[off:off + CH_SIZE] = raw
        img[bm] |= (1 << bit)
        self._mmap = memmap.MemoryMapBytes(bytes(img))

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
        b = self._mmap.get_packed()[IMG_ZONE + ZONE_DETECT_OFF]
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
        img = self._mmap.get_packed()
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
        img = bytearray(self._mmap.get_packed())
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

    def _zone_add(self, i, ch):
        chans = self._zone_channel_list(i)
        if ch not in chans:
            chans.append(ch)
        self._zone_write(i, self._zone_label(self._mmap.get_packed(), i), chans)

    def _zone_remove(self, i, ch):
        chans = [c for c in self._zone_channel_list(i) if c != ch]
        name = self._zone_name(self._mmap.get_packed(), i)
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

    def _build_caches(self):
        img = self._mmap.get_packed()
        self._contacts = [(i, self._contact_name(img, i))
                          for i in range(1, CONTACTS_MAX + 1)
                          if self._contact_inuse(img, i)]
        self._rxgroups = [(i, self._rxgroup_name(img, i))
                          for i in range(1, RXGROUPS_MAX + 1)
                          if self._rxgroup_inuse(img, i)]

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
        gs = self._mmap.get_packed()[IMG_GENSET:IMG_GENSET + GENSET_LEN]
        radio = RadioSettingGroup("radio", "Radio")
        radio.append(RadioSetting(
            "callsign", "Callsign (radio name)",
            RadioSettingValueString(0, 8, _decode_name(gs[0:8]),
                                    autopad=False)))
        dmrid = _bcd2int(int.from_bytes(gs[8:12], "big"))
        if not (0 <= dmrid <= 16777215):        # unset / 0xFF -> show 0
            dmrid = 0
        radio.append(RadioSetting(
            "dmrid", "DMR ID",
            RadioSettingValueInteger(0, 16777215, dmrid)))
        return radio

    def _get_contacts_settings(self):
        img = self._mmap.get_packed()
        group = RadioSettingGroup("contacts", "Contacts")
        inuse = [i for i in range(1, CONTACTS_MAX + 1)
                 if self._contact_inuse(img, i)]
        spares = [i for i in range(1, CONTACTS_MAX + 1)
                  if not self._contact_inuse(img, i)][:8]
        for i in sorted(set(inuse) | set(spares)):
            name, num, ctype = self._contact_get(img, i)
            sub = RadioSettingGroup("contact_%d" % i, "Contact %d" % i)
            sub.append(RadioSetting(
                "contact_%d_name" % i, "Name",
                RadioSettingValueString(0, 16, name, autopad=False)))
            sub.append(RadioSetting(
                "contact_%d_num" % i, "Number (TG/ID)",
                RadioSettingValueInteger(0, 16777215, num)))
            sub.append(RadioSetting(
                "contact_%d_type" % i, "Type",
                RadioSettingValueList(CONTACT_TYPES, CONTACT_TYPES[ctype])))
            group.append(sub)
        return group

    def get_settings(self):
        radio = self._get_radio_settings()
        contacts = self._get_contacts_settings()
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

        aes.append(RadioSetting(
            "aes_txkeyid", "TX key id (0 = encrypted TX off)",
            RadioSettingValueInteger(0, 15, store.tx_key_id)))

        for i in range(AESK_NUM_SLOTS):
            slot = store.slots[i]
            aes.append(RadioSetting(
                "aes_valid_%d" % i, "Key id %d enabled" % i,
                RadioSettingValueBoolean(slot.valid)))
            aes.append(RadioSetting(
                "aes_key_%d" % i, "Key id %d (64 hex)" % i,
                RadioSettingValueString(0, 64, slot.key_hex,
                                        autopad=False, charset=HEX)))
        return RadioSettings(radio, contacts, aes)

    def set_settings(self, settings):
        flat = {}

        def walk(group):
            for el in group:
                if isinstance(el, RadioSetting):
                    flat[el.get_name()] = el
                else:
                    walk(el)
        walk(settings)

        img = bytearray(self._mmap.get_packed())

        # -- general (radio) settings --
        if "callsign" in flat:
            img[IMG_GENSET:IMG_GENSET + 8] = _encode_name(
                str(flat["callsign"].value), 8)
        if "dmrid" in flat:
            img[IMG_GENSET + 8:IMG_GENSET + 12] = \
                _int2bcd(int(flat["dmrid"].value)).to_bytes(4, "big")

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

        # -- AES key store --
        store = AesKeyStore.from_payload(
            find_aes_block(bytes(img[IMG_AES:IMG_AES + SECTOR_SIZE]))[1])
        if "aes_txkeyid" in flat:
            store.tx_key_id = int(flat["aes_txkeyid"].value)
        for i in range(AESK_NUM_SLOTS):
            vkey = "aes_valid_%d" % i
            kkey = "aes_key_%d" % i
            if vkey not in flat and kkey not in flat:
                continue
            valid = bool(flat[vkey].value) if vkey in flat \
                else store.slots[i].valid
            hexstr = str(flat[kkey].value).strip() if kkey in flat \
                else store.slots[i].key_hex
            if valid:
                if len(hexstr) != 64:
                    raise errors.InvalidValueError(
                        "AES key id %d must be exactly 64 hex characters "
                        "(got %d)" % (i, len(hexstr)))
                try:
                    key = bytes.fromhex(hexstr)
                except ValueError:
                    raise errors.InvalidValueError(
                        "AES key id %d is not valid hex" % i)
                store.slots[i] = AesSlot(True, i, key)
            else:
                store.slots[i] = AesSlot(False, i, b"")
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
    def get_num_mappings(self):
        return ZONES_MAX

    def get_mappings(self):
        img = self._radio._mmap.get_packed()
        return [OpenGD77Bank(self, i, self._radio._zone_label(img, i))
                for i in range(ZONES_MAX)]

    def add_memory_to_mapping(self, memory, mapping):
        self._radio._zone_add(mapping.get_index(), memory.number)

    def remove_memory_from_mapping(self, memory, mapping):
        self._radio._zone_remove(mapping.get_index(), memory.number)

    def get_mapping_memories(self, mapping):
        return [self._radio.get_memory(n)
                for n in self._radio._zone_channel_list(mapping.get_index())]

    def get_memory_mappings(self, memory):
        img = self._radio._mmap.get_packed()
        out = []
        for i in range(ZONES_MAX):
            if memory.number in self._radio._zone_channel_list(i):
                out.append(OpenGD77Bank(self, i, self._radio._zone_label(img, i)))
        return out
