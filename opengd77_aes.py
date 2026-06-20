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
IMAGE_SIZE = IMG_FLASH_CH + FLASH_CH_LEN             # 61568

_RANGES = [
    # (image_offset, area, device_addr, length)
    (IMG_AES, AREA_FLASH, CUSTOM_DATA_ADDR, SECTOR_SIZE),
    (IMG_EE_BITMAP, AREA_EEPROM, EE_CH_BITMAP_ADDR, 16),
    (IMG_EE_CH, AREA_EEPROM, EE_CH_DATA_ADDR, CH_PER_BANK * CH_SIZE),
    (IMG_FLASH_CH, AREA_FLASH, FLASH_CH_BITMAP_ADDR, FLASH_CH_LEN),
]

# Writable spans: (image_offset, raw_flash_addr, length).  All written through
# the flash 'X' protocol (EEPROM region included -- it is flash at offset 0).
# The EEPROM bank-0 bitmap (0x3780) + 128 channels (0x3790) are contiguous.
_WRITE_SPANS = [
    (IMG_AES, CUSTOM_DATA_ADDR, SECTOR_SIZE),
    (IMG_EE_BITMAP, EE_CH_BITMAP_ADDR, 16 + CH_PER_BANK * CH_SIZE),
    (IMG_FLASH_CH, FLASH_CH_BITMAP_ADDR, FLASH_CH_LEN),
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
            "AES-256 key management (Settings -> AES Keys) and channel "
            "read/write (memories 1-1024, analog + DMR).  Zones, contacts, RX "
            "groups and other codeplug objects are still in development.\n\n"
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
        rf.has_bank = False
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
        # Channels are parsed on demand in get_memory(); nothing to precompute.
        pass

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
    def _add_dmr_extras(mem, raw):
        group = RadioSettingGroup("dmr", "DMR")
        flag1 = raw[38]
        optional_dmrid = bool(flag1 & 0x80)
        cc = raw[44] & 0x0F
        ts = 2 if (raw[49] & 0x40) else 1
        contact = struct.unpack_from("<H", raw, 46)[0]
        if contact > 1024:                  # 0xFFFF / unset -> show "none"
            contact = 0
        tg_list = raw[43]
        if tg_list > 76:                    # 0xFF / unset -> show "none"
            tg_list = 0
        group.append(RadioSetting(
            "cc", "Colour code",
            RadioSettingValueInteger(0, 15, cc)))
        group.append(RadioSetting(
            "ts", "Timeslot",
            RadioSettingValueInteger(1, 2, ts)))
        group.append(RadioSetting(
            "contact", "Contact index",
            RadioSettingValueInteger(0, 1024, contact)))
        group.append(RadioSetting(
            "tg_list", "RX group (TG list) index",
            RadioSettingValueInteger(0, 76, tg_list)))
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
                struct.pack_into("<H", raw, 46, int(ex["contact"]) & 0xFFFF)
            if "tg_list" in ex:
                raw[43] = int(ex["tg_list"]) & 0xFF
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

    # -- settings: AES key store ----------------------------------------
    def get_settings(self):
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
        return RadioSettings(aes)

    def set_settings(self, settings):
        store = self._read_aes_store()
        flat = {}

        def walk(group):
            for el in group:
                if isinstance(el, RadioSetting):
                    flat[el.get_name()] = el
                else:
                    walk(el)
        walk(settings)

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

        self._write_aes_store(store)
