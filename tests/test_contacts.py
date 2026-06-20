"""Host tests for digital contacts + RX groups (and the channel dropdowns)."""
import struct

import opengd77_aes as drv
from chirp import chirp_common
from chirp.settings import (RadioSettingGroup, RadioSetting,
                            RadioSettingValueInteger, RadioSettingValueList)
from tests.fake_radio import FakeOpenGD77


def _put_contact(fake, idx, name, num, ctype):
    o = drv.CONTACT_ADDR + (idx - 1) * drv.CONTACT_SIZE
    rec = bytearray(b"\xFF" * drv.CONTACT_SIZE)
    rec[0:16] = drv._encode_name(name, 16)
    rec[16:20] = drv._int2bcd(num).to_bytes(4, "big")
    rec[20] = ctype
    rec[21] = 0
    rec[22] = 0
    rec[23] = 0xFF
    fake.flash[o:o + drv.CONTACT_SIZE] = rec


def _put_rxgroup(fake, idx, name, contacts):
    fake.flash[drv.RXGROUP_LEN_ADDR + (idx - 1)] = len(contacts)
    o = drv.RXGROUP_ADDR + (idx - 1) * drv.RXGROUP_SIZE
    rec = bytearray(b"\x00" * drv.RXGROUP_SIZE)
    rec[0:16] = drv._encode_name(name, 16)
    for k, c in enumerate(contacts):
        struct.pack_into("<H", rec, 16 + k * 2, c)
    fake.flash[o:o + drv.RXGROUP_SIZE] = rec


def _zero_ch_bitmaps(fake):
    fake.flash[drv.EE_CH_BITMAP_ADDR:drv.EE_CH_BITMAP_ADDR + 16] = b"\x00" * 16
    for b in range(7):
        a = drv.FLASH_CH_BITMAP_ADDR + b * drv.FLASH_BANK_STRIDE
        fake.flash[a:a + 16] = b"\x00" * 16


def _flatten(settings):
    flat = {}

    def walk(g):
        for el in g:
            if isinstance(el, RadioSetting):
                flat[el.get_name()] = el
            else:
                walk(el)
    walk(settings)
    return flat


def test_contact_read():
    fake = FakeOpenGD77()
    _put_contact(fake, 5, "PARROT", 9990, drv.CONTACT_TYPE_PC)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    img = radio._mmap.get_packed()
    assert radio._contact_inuse(img, 5)
    assert radio._contact_get(img, 5) == ("PARROT", 9990, drv.CONTACT_TYPE_PC)
    assert (5, "PARROT") in radio._contacts


def test_contact_create_via_settings():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = _flatten(settings)
    i = min(int(k.split("_")[1]) for k in flat
            if k.startswith("contact_") and k.endswith("_name"))
    flat["contact_%d_name" % i].value = "TG TEST"
    flat["contact_%d_num" % i].value = 1234
    flat["contact_%d_type" % i].value = drv.CONTACT_TYPES[drv.CONTACT_TYPE_TG]
    radio.set_settings(settings)
    radio.sync_out()

    r2 = drv.OpenGD77AESRadio(fake)
    r2.sync_in()
    img = r2._mmap.get_packed()
    assert r2._contact_inuse(img, i)
    assert r2._contact_get(img, i) == ("TG TEST", 1234, drv.CONTACT_TYPE_TG)


def test_rxgroup_read():
    fake = FakeOpenGD77()
    _put_rxgroup(fake, 3, "WIDE", [1, 2, 3])
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    img = radio._mmap.get_packed()
    assert radio._rxgroup_inuse(img, 3)
    assert radio._rxgroup_name(img, 3) == "WIDE"
    assert (3, "WIDE") in radio._rxgroups


def test_channel_contact_and_tg_dropdown_roundtrip():
    fake = FakeOpenGD77()
    _zero_ch_bitmaps(fake)
    _put_contact(fake, 7, "REPEATER", 91, drv.CONTACT_TYPE_TG)
    _put_rxgroup(fake, 2, "LOCAL", [7])
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()

    mem = chirp_common.Memory()
    mem.number = 2
    mem.name = "DMRCH"
    mem.freq = 438000000
    mem.mode = "DMR"
    g = RadioSettingGroup("dmr", "DMR")
    g.append(RadioSetting("cc", "cc", RadioSettingValueInteger(0, 15, 1)))
    g.append(RadioSetting("ts", "ts", RadioSettingValueInteger(1, 2, 1)))
    g.append(RadioSetting("contact", "contact",
                          RadioSettingValueList(["None", "7: REPEATER"],
                                                "7: REPEATER")))
    g.append(RadioSetting("tg_list", "tg",
                          RadioSettingValueList(["None", "2: LOCAL"],
                                                "2: LOCAL")))
    mem.extra = g
    radio.set_memory(mem)
    radio.sync_out()

    r2 = drv.OpenGD77AESRadio(fake)
    r2.sync_in()
    m2 = r2.get_memory(2)
    ex = {s.get_name(): str(s.value) for s in m2.extra}
    assert ex["contact"] == "7: REPEATER"
    assert ex["tg_list"] == "2: LOCAL"
