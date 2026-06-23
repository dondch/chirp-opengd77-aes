"""Host tests for the radio-wide settings (OpenGD77 nonVolatileSettings).

No hardware required.  Exercises decode, edit round-trip, byte-exact write-back
with neighbour/magic preservation, the bitfield toggle handling, and the
anti-clobber guard for an image without the settings magic.
"""
import struct
import warnings

import opengd77_aes as drv
from chirp import chirp_common
from tests.fake_radio import FakeOpenGD77


def _flatten(settings):
    flat = {}

    def walk(group):
        for el in group:
            if isinstance(el, drv.RadioSetting):
                flat[el.get_name()] = el
            else:
                walk(el)
    for grp in settings:
        walk(grp)
    return flat


def _make_blob(**by_off):
    """A valid 116-byte settings blob; keyword n<offset>=value sets a byte,
    or use bitopts=<u32> for the bitfieldOptions word."""
    blob = bytearray(b"\x00" * drv.SETTINGS_LEN)
    struct.pack_into("<I", blob, 0, drv.SETTINGS_MAGIC)
    bitopts = by_off.pop("bitopts", 0)
    struct.pack_into("<I", blob, drv.SETTINGS_BITOPTS_OFF, bitopts)
    for k, v in by_off.items():
        blob[int(k[1:])] = v & 0xFF       # keys look like "n75"
    return bytes(blob)


def _preload(fake, blob, neighbour=None):
    # Settings live at raw flash 0x604B; 0x6000.. is unrelated neighbour data.
    if neighbour is not None:
        fake.flash[0x6000:0x6000 + len(neighbour)] = neighbour
    fake.flash[drv.SETTINGS_ADDR:drv.SETTINGS_ADDR + len(blob)] = blob
    # A minimal valid AES region so get_settings() has a store to read.
    region = drv.region_with_aes(bytes(b"\xFF" * drv.SECTOR_SIZE),
                                 drv.AesKeyStore().to_payload())
    fake.flash[drv.CUSTOM_DATA_ADDR:drv.CUSTOM_DATA_ADDR + len(region)] = region


def test_settings_decode():
    fake = FakeOpenGD77()
    # backlightMode=2 (Manual), txPowerLevel=4, apo=3, roaming=2 (5 km),
    # displayContrast = -16 (0xF0), bit1 (PTT latch) + bit27 set.
    blob = _make_blob(n75=2, n70=4, n106=3, n110=2, n77=0xF0,
                      bitopts=(1 << 1) | (1 << 27))
    _preload(fake, blob)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    flat = _flatten(radio.get_settings())

    assert str(flat["set_backlightMode"].value) == "Manual"
    assert int(flat["set_txPowerLevel"].value) == 4
    assert int(flat["set_apo"].value) == 3
    assert str(flat["set_roaming"].value) == "5 km"
    assert int(flat["set_displayContrast"].value) == -16   # signed + widened
    assert bool(flat["set_bit_pttLatch"].value) is True
    assert bool(flat["set_bit_channelsReadOnly"].value) is True
    assert bool(flat["set_bit_inverseVideo"].value) is False


def test_settings_roundtrip_and_preserves_neighbour():
    fake = FakeOpenGD77()
    neighbour = bytes(range(0x4B))            # 0x6000..0x604A sentinel
    blob = _make_blob(n75=0, n70=1, n77=20, bitopts=(1 << 4) | (1 << 30))
    _preload(fake, blob, neighbour=neighbour)

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = _flatten(settings)
    flat["set_backlightMode"].value = "Squelch"        # list -> 1
    flat["set_txPowerLevel"].value = 7                 # int
    flat["set_displayContrast"].value = 30             # int
    flat["set_bit_pttLatch"].value = True              # set bit 1
    flat["set_bit_txInhibit"].value = True             # set bit 25
    radio.set_settings(settings)
    radio.sync_out()

    out = bytes(fake.flash[drv.SETTINGS_ADDR:drv.SETTINGS_ADDR + drv.SETTINGS_LEN])
    assert struct.unpack_from("<I", out, 0)[0] == drv.SETTINGS_MAGIC   # magic kept
    assert out[75] == 1                                # Squelch
    assert out[70] == 7
    assert out[77] == 30
    bits = struct.unpack_from("<I", out, drv.SETTINGS_BITOPTS_OFF)[0]
    assert bits & (1 << 1)                             # newly set
    assert bits & (1 << 25)                            # newly set
    assert bits & (1 << 4)                             # untouched bit preserved
    assert bits & (1 << 30)                            # bank id bits preserved
    # The 0x6000..0x604A neighbour data is untouched by the sector RMW.
    assert bytes(fake.flash[0x6000:0x6000 + 0x4B]) == neighbour


def test_settings_unchanged_field_not_corrupted():
    # An untouched control whose stored value is outside the nominal UI range
    # must be written back byte-identical (no clamping).
    fake = FakeOpenGD77()
    # ecoLevel nominal 0..5, stored 99; displayContrast stored -16 (0xF0 signed).
    blob = _make_blob(n105=99, n77=0xF0)
    _preload(fake, blob)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    flat = _flatten(radio.get_settings())
    assert int(flat["set_ecoLevel"].value) == 99
    assert int(flat["set_displayContrast"].value) == -16     # signed decode
    radio.set_settings(radio.get_settings())
    radio.sync_out()
    out = bytes(fake.flash[drv.SETTINGS_ADDR:drv.SETTINGS_ADDR + drv.SETTINGS_LEN])
    assert out[105] == 99
    assert out[77] == 0xF0                                   # signed write-back identity


def test_settings_unavailable_without_magic():
    fake = FakeOpenGD77()                       # default flash is 0xFF -> no magic
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    flat = _flatten(radio.get_settings())
    assert "rsettings_note" in flat             # shows the unavailable note
    assert "set_backlightMode" not in flat


def test_settings_not_written_without_magic():
    # Upload must skip the settings sector when the image lacks the magic,
    # so it never triggers a firmware factory-reset.
    fake = FakeOpenGD77()
    before = bytes(fake.flash[drv.SETTINGS_ADDR:drv.SETTINGS_ADDR + drv.SETTINGS_LEN])
    assert before == b"\xFF" * drv.SETTINGS_LEN
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    radio._orig = None                          # force the unconditional write path
    radio.sync_out()
    after = bytes(fake.flash[drv.SETTINGS_ADDR:drv.SETTINGS_ADDR + drv.SETTINGS_LEN])
    assert after == before                      # untouched


def test_no_dropdown_future_warnings():
    # Every RadioSettingValueList must be built with current_index, not a value
    # string, or CHIRP emits a FutureWarning (and may break in a future release).
    # Build every dropdown surface: radio-wide settings, contact type, boot
    # screen, and the per-channel extras (contact / tg_list / encrypt) for a
    # normal channel and a VFO.
    fake = FakeOpenGD77()
    _preload(fake, _make_blob())
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()

    m = chirp_common.Memory()
    m.number = 1
    m.freq = 446006250
    m.mode = "DMR"
    m.duplex = ""
    radio.set_memory(m)                         # channel 1 in use -> extras built
    v = radio.get_memory("VFO A")
    v.empty = False
    v.freq = 145500000
    v.mode = "FM"
    v.duplex = ""
    radio.set_memory(v)                         # VFO A in use -> extras built

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        radio.get_settings()
        radio.get_memory(1)
        radio.get_memory("VFO A")
        radio.get_memory("VFO B")

    offenders = [str(w.message) for w in caught
                 if issubclass(w.category, FutureWarning)
                 and "current_index" in str(w.message)]
    assert not offenders, offenders


def test_every_setting_has_a_tooltip():
    # set_doc() stores an instance __doc__; without it the attr isn't in the
    # instance dict (it would fall back to the class docstring). Every leaf
    # RadioSetting we expose must have an explicit, non-empty tooltip.
    from chirp.settings import RadioSetting
    fake = FakeOpenGD77()
    _preload(fake, _make_blob())
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    m = chirp_common.Memory()
    m.number = 1
    m.freq = 446006250
    m.mode = "DMR"
    m.duplex = ""
    radio.set_memory(m)                          # channel 1 in use -> Extra tab

    missing = []

    def check(el):
        if isinstance(el, RadioSetting):
            doc = vars(el).get("__doc__")
            if not (isinstance(doc, str) and doc.strip()):
                missing.append(el.get_name())

    for group in radio.get_settings():
        for el in group.walk():
            check(el)
    for el in radio.get_memory(1).extra:
        check(el)
    assert not missing, "settings without a tooltip: %s" % missing


def test_gps_mode_roundtrip_preserves_baud():
    # gpsModeAndBaudsIndex @111: low nibble = mode, high nibble = baud index.
    fake = FakeOpenGD77()
    _preload(fake, _make_blob(n111=0x23))        # baud index 2, mode 3 (NMEA)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = _flatten(settings)
    assert str(flat["set_gpsMode"].value) == "On + NMEA out"

    flat["set_gpsMode"].value = "On + logging"   # mode 4
    radio.set_settings(settings)
    radio.sync_out()
    assert fake.flash[drv.SETTINGS_ADDR + 111] == 0x24   # baud nibble kept, mode=4


def test_timezone_roundtrip_preserves_local_flag():
    # timezone byte @12: bits 0-6 = offset (64=UTC, 15-min steps), bit 7 = local.
    fake = FakeOpenGD77()
    _preload(fake, _make_blob(n12=(0x80 | 68)))  # local flag set, UTC+1:00 (68)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    flat = _flatten(radio.get_settings())
    assert str(flat["set_timezone"].value) == "UTC+1:00"
    assert str(flat["set_timeLocal"].value) == "Local"

    settings = radio.get_settings()
    _flatten(settings)["set_timezone"].value = "UTC+5:30"   # 64 + 22 = 86
    radio.set_settings(settings)
    radio.sync_out()
    # offset updated to 86, local-flag (0x80) preserved.
    assert fake.flash[drv.SETTINGS_ADDR + 12] == (0x80 | 86)
