"""Host tests for the Quick Keys editor (long-press number-key 0-9 shortcuts).

Quick keys live in the codeplug at 0x7524 (10 * uint16).  A value's bit15 picks
Contact (0) vs Menu (1); Contact values are a contact index (1..1024); menu
shortcuts are preserved verbatim; 0x0000/0x8000/0xFFFF are empty.
"""
import struct

import opengd77_aes as drv
from chirp.settings import RadioSetting
from tests.fake_radio import FakeOpenGD77


def _put_contact(fake, idx, name, num, ctype=0):
    o = drv.CONTACT_ADDR + (idx - 1) * drv.CONTACT_SIZE
    rec = bytearray(b"\xFF" * drv.CONTACT_SIZE)
    rec[0:16] = drv._encode_name(name, 16)
    rec[16:20] = drv._int2bcd(num).to_bytes(4, "big")
    rec[20] = ctype
    rec[21] = rec[22] = 0
    rec[23] = 0xFF
    fake.flash[o:o + drv.CONTACT_SIZE] = rec


def _put_quickkeys(fake, values):
    buf = b"".join(struct.pack("<H", v) for v in values)
    fake.flash[drv.QUICKKEYS_ADDR:drv.QUICKKEYS_ADDR + len(buf)] = buf


def _qk(fake, k):
    return struct.unpack_from("<H", bytes(fake.flash),
                              drv.QUICKKEYS_ADDR + k * 2)[0]


def _flatten(settings):
    flat = {}

    def walk(g):
        for el in g:
            if isinstance(el, RadioSetting):
                flat[el.get_name()] = el
            else:
                walk(el)
    for g in settings:
        walk(g)
    return flat


def test_quickkeys_decode():
    fake = FakeOpenGD77()
    _put_contact(fake, 6, "DCH", 9661)
    qk = [0] * drv.QUICKKEYS_COUNT
    qk[0] = 6            # Contact 6
    qk[1] = 0x8421       # Menu shortcut (bit15 set)
    qk[2] = 0x0000       # empty
    qk[3] = 0xFFFF       # erased -> empty
    qk[4] = 7            # Contact 7, not in use -> "7: ?"
    _put_quickkeys(fake, qk)

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    flat = _flatten(radio.get_settings())
    assert str(flat["quickkey_0"].value) == "6: DCH"
    assert str(flat["quickkey_1"].value) == "Menu shortcut 0x8421"
    assert str(flat["quickkey_2"].value) == "(empty)"
    assert str(flat["quickkey_3"].value) == "(empty)"
    assert str(flat["quickkey_4"].value) == "7: ?"


def test_quickkeys_set_contact_preserves_neighbours():
    fake = FakeOpenGD77()
    _put_contact(fake, 3, "TG3", 3)
    _put_quickkeys(fake, [0] * drv.QUICKKEYS_COUNT)
    # A marker in the VFO region (same flash sector) must survive the write.
    vfo_marker = bytes(range(drv.CH_SIZE))
    fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE] = vfo_marker

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    _flatten(settings)["quickkey_5"].value = "3: TG3"
    radio.set_settings(settings)
    radio.sync_out()

    assert _qk(fake, 5) == 3                      # contact index, bit15 = 0
    assert bytes(fake.flash[drv.VFO_ADDR:drv.VFO_ADDR + drv.CH_SIZE]) == vfo_marker


def test_quickkeys_clear():
    fake = FakeOpenGD77()
    _put_contact(fake, 4, "TG4", 4)
    qk = [0] * drv.QUICKKEYS_COUNT
    qk[2] = 4                                     # Contact 4 assigned to key 2
    _put_quickkeys(fake, qk)

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    _flatten(settings)["quickkey_2"].value = "(empty)"
    radio.set_settings(settings)
    radio.sync_out()
    assert _qk(fake, 2) == 0x0000


def test_quickkeys_menu_and_empty_preserved_byte_exact():
    # An untouched menu shortcut, a 0x0000 empty, and a 0xFFFF (erased) empty must
    # all round-trip byte-identically -- no spurious rewrite, no factory bytes lost.
    fake = FakeOpenGD77()
    qk = [0] * drv.QUICKKEYS_COUNT
    qk[0] = 0x8421       # menu shortcut
    qk[1] = 0x0000       # empty
    qk[2] = 0xFFFF       # erased empty
    _put_quickkeys(fake, qk)

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()          # no edits
    radio.set_settings(settings)
    radio.sync_out()
    assert _qk(fake, 0) == 0x8421
    assert _qk(fake, 1) == 0x0000
    assert _qk(fake, 2) == 0xFFFF
