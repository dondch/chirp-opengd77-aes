"""Host tests for DTMF contacts and boot-screen settings."""
import opengd77_aes as drv
from chirp.settings import RadioSetting
from tests.fake_radio import FakeOpenGD77


def _put_dtmf(fake, idx, name, code):
    o = drv.DTMF_ADDR + (idx - 1) * drv.DTMF_SIZE
    rec = bytearray(b"\xFF" * drv.DTMF_SIZE)
    rec[0:16] = drv._encode_name(name, 16)
    for k, ch in enumerate(code):
        rec[16 + k] = drv.DTMF_DIGITS.index(ch)
    fake.flash[o:o + drv.DTMF_SIZE] = rec


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


def test_dtmf_read():
    fake = FakeOpenGD77()
    _put_dtmf(fake, 2, "GATE", "1234*5")
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    img = radio._mmap.get_packed()
    assert radio._dtmf_inuse(img, 2)
    assert radio._dtmf_get(img, 2) == ("GATE", "1234*5")


def test_dtmf_create_via_settings():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = _flatten(settings)
    i = min(int(k.split("_")[1]) for k in flat
            if k.startswith("dtmf_") and k.endswith("_name"))
    flat["dtmf_%d_name" % i].value = "DOOR"
    flat["dtmf_%d_code" % i].value = "12AB#"
    radio.set_settings(settings)
    radio.sync_out()

    r2 = drv.OpenGD77AESRadio(fake)
    r2.sync_in()
    assert r2._dtmf_get(r2._mmap.get_packed(), i) == ("DOOR", "12AB#")


def test_boot_text_roundtrip():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = _flatten(settings)
    flat["boot_line1"].value = "HELLO"
    flat["boot_line2"].value = "WORLD"
    flat["boot_screen"].value = "Text"
    radio.set_settings(settings)
    radio.sync_out()

    r2 = drv.OpenGD77AESRadio(fake)
    r2.sync_in()
    f2 = _flatten(r2.get_settings())
    assert str(f2["boot_line1"].value) == "HELLO"
    assert str(f2["boot_line2"].value) == "WORLD"
    assert str(f2["boot_screen"].value) == "Text"
