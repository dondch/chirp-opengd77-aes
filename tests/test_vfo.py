"""Host tests for VFO A/B exposed as CHIRP special channels.

No hardware required.  VFO A/B are two CodeplugChannel_t structs at 0x7590; they
reuse the normal channel codec but have no bitmap, no editable name, and cannot
be deleted.
"""
import struct

import opengd77_aes as drv
from chirp import chirp_common
from tests.fake_radio import FakeOpenGD77


def _make_channel_raw(radio, freq, mode="NFM", name=None):
    m = chirp_common.Memory()
    m.number = 1
    m.freq = freq
    m.mode = mode
    m.duplex = ""
    raw = bytearray(drv.CH_SIZE)
    radio._encode_channel(m, raw)
    if name is not None:
        raw[0:16] = drv._encode_name(name)
    return bytes(raw)


def test_vfo_listed_as_specials():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    rf = radio.get_features()
    assert rf.valid_special_chans == ["VFO A", "VFO B"]


def test_vfo_empty_when_uninitialised():
    fake = FakeOpenGD77()                       # 0xFF flash -> VFO blank
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    a = radio.get_memory("VFO A")
    assert a.empty
    assert a.extd_number == "VFO A"
    assert "name" in a.immutable               # name not editable for a VFO


def test_vfo_decode_roundtrip_and_name_preserved():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    # Preload VFO A with a known channel that also carries a stored name.
    fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE] = _make_channel_raw(
        radio, 446006250, "NFM", name="OLDVFO")
    radio.sync_in()

    a = radio.get_memory("VFO A")
    assert not a.empty
    assert a.freq == 446006250
    assert a.mode == "NFM"
    assert a.name == ""                         # stored name suppressed in the UI

    a.freq = 145500000
    a.mode = "FM"
    radio.set_memory(a)
    radio.sync_out()

    raw = bytes(fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE])
    assert drv._bcd2int(struct.unpack_from("<I", raw, 16)[0]) * 10 == 145500000
    assert raw[24] == 0                         # analog
    assert raw[51] & 0x02                       # FM (25 kHz) bandwidth bit
    # set_name=False: the stored name bytes are left untouched.
    assert drv._decode_name(raw[0:16]) == "OLDVFO"


def test_vfo_b_writes_its_own_slot():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE] = _make_channel_raw(
        radio, 446006250, "NFM", name="KEEPA")
    radio.sync_in()

    b = radio.get_memory("VFO B")
    assert b.empty
    b.empty = False
    b.freq = 433450000
    b.mode = "FM"
    b.duplex = ""
    radio.set_memory(b)
    radio.sync_out()

    vfo_a = bytes(fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE])
    vfo_b = bytes(fake.flash[drv.VFO_ADDR + drv.CH_SIZE:
                             drv.VFO_ADDR + 2 * drv.CH_SIZE])
    # VFO A untouched, VFO B got the new frequency in the correct slot.
    assert drv._decode_name(vfo_a[0:16]) == "KEEPA"
    assert drv._bcd2int(struct.unpack_from("<I", vfo_b, 16)[0]) * 10 == 433450000


def test_vfo_not_deletable():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE] = _make_channel_raw(
        radio, 446006250, "NFM", name="KEEP")
    radio.sync_in()
    a = radio.get_memory("VFO A")
    a.empty = True                              # editor tries to clear it
    radio.set_memory(a)
    radio.sync_out()
    raw = bytes(fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE])
    assert drv._decode_name(raw[0:16]) == "KEEP"   # unchanged, not wiped
