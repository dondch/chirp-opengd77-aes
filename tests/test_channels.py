"""Host tests for channel read/write (encode/decode round-trip + upload).

No hardware: the fake radio backs the whole codeplug in an in-memory flash and
models the EEPROM region as flash at offset 0 (as the real firmware does).
"""
import opengd77_aes as drv
from chirp import chirp_common
from chirp.settings import (RadioSettingGroup, RadioSetting,
                            RadioSettingValueInteger)
from tests.fake_radio import FakeOpenGD77


def _dmr_extra(cc=1, ts=1, contact=0, tg_list=0):
    g = RadioSettingGroup("dmr", "DMR")
    g.append(RadioSetting("cc", "Colour code",
                          RadioSettingValueInteger(0, 15, cc)))
    g.append(RadioSetting("ts", "Timeslot",
                          RadioSettingValueInteger(1, 2, ts)))
    g.append(RadioSetting("contact", "Contact index",
                          RadioSettingValueInteger(0, 1024, contact)))
    g.append(RadioSetting("tg_list", "RX group (TG list) index",
                          RadioSettingValueInteger(0, 76, tg_list)))
    return g


def _extra_dict(mem):
    # ints, booleans, and "idx: name" dropdowns (or "None").
    d = {}
    for s in (mem.extra or []):
        v = str(s.value)
        if v in ("True", "False"):
            d[s.get_name()] = (v == "True")
        elif ":" in v:
            d[s.get_name()] = int(v.split(":")[0])
        elif v.startswith("None"):
            d[s.get_name()] = 0
        else:
            try:
                d[s.get_name()] = int(v)
            except ValueError:
                d[s.get_name()] = v
    return d


def _fresh_radio():
    fake = FakeOpenGD77()
    # Realistic empty codeplug: clear the channel in-use bitmaps (erased flash
    # is all 0xFF, which would otherwise mark every channel "in use").
    fake.flash[drv.EE_CH_BITMAP_ADDR:drv.EE_CH_BITMAP_ADDR + 16] = b"\x00" * 16
    for b in range(CH_BANKS_FLASH):
        a = drv.FLASH_CH_BITMAP_ADDR + b * drv.FLASH_BANK_STRIDE
        fake.flash[a:a + 16] = b"\x00" * 16
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    return fake, radio


CH_BANKS_FLASH = 7


def _reload(fake):
    r = drv.OpenGD77AESRadio(fake)
    r.sync_in()
    return r


def test_power_level_libredmr_roundtrip():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 8
    mem.name = "PWR"
    mem.freq = 145000000
    mem.mode = "FM"
    mem.power = radio.POWER_LEVELS[5]      # -> libreDMR_Power byte = 5
    radio.set_memory(mem)
    radio.sync_out()
    m2 = _reload(fake).get_memory(8)
    assert radio.POWER_LEVELS.index(m2.power) == 5


def test_channel_extras_roundtrip():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 9
    mem.name = "EX"
    mem.freq = 438000000
    mem.mode = "DMR"
    radio.set_memory(mem)
    radio.sync_out()

    r = _reload(fake)
    m = r.get_memory(9)
    ex = {s.get_name(): s for s in m.extra}
    ex["tot"].value = 60          # -> raw 4 (60/15)
    ex["vox"].value = True
    ex["squelch"].value = 5
    ex["all_skip"].value = True
    ex["dmrid"].value = 1234567
    ex["cc"].value = 7
    ex["ts"].value = 2
    r.set_memory(m)
    r.sync_out()

    e2 = _extra_dict(_reload(fake).get_memory(9))
    assert e2["tot"] == 60
    assert e2["vox"] is True
    assert e2["squelch"] == 5
    assert e2["all_skip"] is True
    assert e2["dmrid"] == 1234567
    assert e2["cc"] == 7
    assert e2["ts"] == 2


def _mk_dmr(radio, fake, number):
    mem = chirp_common.Memory()
    mem.number = number
    mem.name = "ENC"
    mem.freq = 438000000
    mem.mode = "DMR"
    radio.set_memory(mem)
    radio.sync_out()


def test_channel_encryption_roundtrip():
    fake, radio = _fresh_radio()
    _mk_dmr(radio, fake, 10)
    r = _reload(fake)
    m = r.get_memory(10)
    {s.get_name(): s for s in m.extra}["encrypt"].value = "Key 3"
    r.set_memory(m)
    r.sync_out()

    r2 = _reload(fake)
    m2 = r2.get_memory(10)
    e2 = {s.get_name(): str(s.value) for s in m2.extra}
    assert e2["encrypt"] == "Key 3"
    off, _, _ = r2._channel_offset(10)
    assert r2._img()[off + 41] == 3            # encrypt byte = slot 3


def test_channel_encryption_off():
    fake, radio = _fresh_radio()
    _mk_dmr(radio, fake, 11)
    r = _reload(fake)
    m = r.get_memory(11)
    {s.get_name(): s for s in m.extra}["encrypt"].value = "Off (no encryption)"
    r.set_memory(m)
    r.sync_out()

    r2 = _reload(fake)
    off, _, _ = r2._channel_offset(11)
    assert r2._img()[off + 41] == 0xFF
    e2 = {s.get_name(): str(s.value) for s in r2.get_memory(11).extra}
    assert e2["encrypt"].startswith("Off")


def test_encryption_and_dmrid_mutually_exclusive():
    fake, radio = _fresh_radio()
    _mk_dmr(radio, fake, 12)
    r = _reload(fake)
    m = r.get_memory(12)
    ex = {s.get_name(): s for s in m.extra}
    ex["encrypt"].value = "Key 5"
    ex["dmrid"].value = 1234567          # DMR ID wins (shares byte 41)
    r.set_memory(m)
    r.sync_out()

    e2 = {s.get_name(): str(s.value) for s in _reload(fake).get_memory(12).extra}
    assert int(e2["dmrid"]) == 1234567
    assert e2["encrypt"].startswith("Inherit")   # encryption not applied


def test_tuning_steps_include_6p25():
    fake, radio = _fresh_radio()
    steps = radio.get_features().valid_tuning_steps
    # PMR446 (e.g. 446.006250) needs 6.25 kHz; 2.5 is also used by OpenGD77.
    assert 6.25 in steps
    assert 2.5 in steps


def test_pmr446_channel_roundtrip():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 7
    mem.name = "PMR07"
    mem.freq = 446006250          # requires the 6.25 kHz step
    mem.mode = "NFM"
    radio.set_memory(mem)
    radio.sync_out()
    m2 = _reload(fake).get_memory(7)
    assert m2.freq == 446006250


def test_eeprom_channel_roundtrip():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 2                       # EEPROM bank-0 channel
    mem.name = "EE CH 2"
    mem.freq = 146520000
    mem.mode = "FM"
    mem.duplex = ""
    mem.power = radio.POWER_LEVELS[1]    # Low
    radio.set_memory(mem)
    radio.sync_out()

    m2 = _reload(fake).get_memory(2)
    assert not m2.empty
    assert m2.name == "EE CH 2"
    assert m2.freq == 146520000
    assert m2.mode == "FM"
    assert radio.POWER_LEVELS.index(m2.power) == 1


def test_flash_channel_roundtrip_dmr():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 200                     # flash-bank channel
    mem.name = "FLASH200"
    mem.freq = 446006250
    mem.mode = "DMR"
    mem.duplex = ""
    mem.power = radio.POWER_LEVELS[0]    # High
    mem.extra = _dmr_extra(cc=5, ts=2, contact=7, tg_list=3)
    radio.set_memory(mem)
    radio.sync_out()

    m2 = _reload(fake).get_memory(200)
    assert m2.name == "FLASH200"
    assert m2.freq == 446006250
    assert m2.mode == "DMR"
    ex = _extra_dict(m2)
    assert ex["cc"] == 5
    assert ex["ts"] == 2
    assert ex["contact"] == 7
    assert ex["tg_list"] == 3


def test_analog_tone_and_split():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 3
    mem.name = "RPT"
    mem.freq = 145500000
    mem.duplex = "-"
    mem.offset = 600000
    mem.mode = "NFM"
    mem.tmode = "Tone"
    mem.rtone = 88.5
    mem.skip = "S"
    radio.set_memory(mem)
    radio.sync_out()

    m2 = _reload(fake).get_memory(3)
    assert m2.freq == 145500000
    assert m2.duplex == "-"
    assert m2.offset == 600000
    assert m2.mode == "NFM"
    assert m2.tmode == "Tone"
    assert m2.rtone == 88.5
    assert m2.skip == "S"


def test_rx_only_and_tsql():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 4
    mem.name = "RXONLY"
    mem.freq = 433000000
    mem.mode = "FM"
    mem.duplex = "off"
    mem.tmode = "TSQL"
    mem.ctone = 100.0
    radio.set_memory(mem)
    radio.sync_out()

    m2 = _reload(fake).get_memory(4)
    assert m2.duplex == "off"
    assert m2.tmode == "TSQL"
    assert m2.ctone == 100.0


def test_delete_channel():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 5
    mem.name = "TODEL"
    mem.freq = 145000000
    mem.mode = "FM"
    radio.set_memory(mem)
    radio.sync_out()
    assert not _reload(fake).get_memory(5).empty

    r = _reload(fake)
    delmem = chirp_common.Memory()
    delmem.number = 5
    delmem.empty = True
    r.set_memory(delmem)
    r.sync_out()
    assert _reload(fake).get_memory(5).empty


def test_upload_writes_only_changed_sectors():
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 2
    mem.name = "ONE"
    mem.freq = 146000000
    mem.mode = "FM"
    radio.set_memory(mem)
    fake.commits = 0
    radio.sync_out()
    # Only the single EEPROM sector containing channel 2 should be written;
    # the AES sector and the flash channel banks are unchanged.
    assert fake.commits == 1


def test_preserves_unmanaged_bytes():
    # A byte CHIRP doesn't manage (e.g. offset 37, _UNUSED_1) must survive an
    # edit of an existing channel.
    fake, radio = _fresh_radio()
    mem = chirp_common.Memory()
    mem.number = 6
    mem.name = "KEEP"
    mem.freq = 145000000
    mem.mode = "FM"
    radio.set_memory(mem)
    radio.sync_out()

    # Poke a marker into the on-radio channel record at offset 37.
    off, _, _ = drv.OpenGD77AESRadio(fake)._channel_offset(6)
    fake.flash[drv.EE_CH_DATA_ADDR + (6 - 1) * drv.CH_SIZE + 37] = 0xC3

    r = _reload(fake)
    m = r.get_memory(6)
    m.name = "KEEP2"
    r.set_memory(m)
    r.sync_out()

    marker = fake.flash[drv.EE_CH_DATA_ADDR + (6 - 1) * drv.CH_SIZE + 37]
    assert marker == 0xC3
