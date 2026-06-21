"""Host tests for the DMR-ID database importer (CSV -> on-flash blob)."""
import os
import struct
import tempfile

import opengd77_aes as drv
from chirp.settings import RadioSetting
from tests.fake_radio import FakeOpenGD77

CSV = ("RADIO_ID,CALLSIGN,FIRST_NAME,CITY\n"
       "2342001,G4ABC,John,London\n"
       "3101234,W1AW,Hiram,Newington\n"
       "1000,ZZ,Test,X\n")


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


def _db_count(blob):
    assert blob[0:2] == b"Id"
    return int.from_bytes(blob[8:12], "little")


def _db_lookup(blob, target):
    # Mirror the firmware's 4-byte path: LE id == int2bcd(target), plain text.
    contact_len = blob[3] - 0x4A
    text_len = contact_len - 4
    count = int.from_bytes(blob[8:12], "little")
    tgt = drv._int2bcd(target)
    for k in range(count):
        o = drv.DMRID_HEADER_LEN + k * contact_len
        if struct.unpack_from("<I", blob, o)[0] == tgt:
            return blob[o + 4:o + 4 + text_len].split(b"\x00")[0].decode("ascii")
    return None


def test_parse_csv_with_header():
    recs = dict(drv.parse_dmrid_csv(CSV))
    assert recs[2342001] == "G4ABC John"
    assert recs[3101234] == "W1AW Hiram"
    assert recs[1000] == "ZZ Test"


def test_parse_csv_headerless():
    recs = dict(drv.parse_dmrid_csv("2342001,G4ABC,John\n1000,ZZ,Test\n"))
    assert recs[2342001] == "G4ABC John"


def test_build_db_sorted_and_lookup():
    blob, n, trunc = drv.build_dmrid_db(drv.parse_dmrid_csv(CSV))
    assert n == 3 and not trunc
    assert blob[0:2] == b"Id"
    assert (blob[3] - 0x4A) == drv.DMRID_CONTACT_LEN
    # records sorted ascending by id -> first is the smallest (1000)
    first = struct.unpack_from("<I", blob, drv.DMRID_HEADER_LEN)[0]
    assert first == drv._int2bcd(1000)
    assert _db_lookup(blob, 2342001) == "G4ABC John"
    assert _db_lookup(blob, 3101234) == "W1AW Hiram"


def test_build_db_truncates():
    recs = [(i + 1, "C%d" % i) for i in range(drv.DMRID_MAX_ENTRIES + 5)]
    blob, n, trunc = drv.build_dmrid_db(recs)
    assert trunc and n == drv.DMRID_MAX_ENTRIES


def test_import_via_upload():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()

    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(CSV)
        settings = radio.get_settings()
        flat = _flatten(settings)
        flat["dmrid_import"].value = path
        radio.set_settings(settings)
        radio.sync_out()
    finally:
        os.remove(path)

    end = drv.DMRID_HEADER_ADDR + 12 + 3 * drv.DMRID_CONTACT_LEN
    blob = bytes(fake.flash[drv.DMRID_HEADER_ADDR:end])
    assert _db_count(blob) == 3
    assert _db_lookup(blob, 2342001) == "G4ABC John"
    # status field reflects the new count without a re-download
    f2 = _flatten(radio.get_settings())
    assert "3 entries" in str(f2["dmrid_db"].value)
