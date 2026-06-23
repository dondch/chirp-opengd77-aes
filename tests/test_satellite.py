"""Host tests for the satellite TLE/keps encoder.

Proves the encoder by decoding its output exactly the way the firmware does
(decompressTleData: 2 nibbles/byte via "0123456789. +-*", then atof on
fixed-offset fields) and checking real orbital values round-trip.
"""
import struct

import opengd77_aes as drv
from tests.fake_radio import FakeOpenGD77

# A real-format ISS TLE (line-2 columns are the orbit-critical fields).
L1 = "1 25544U 98067A   24016.49000000  .00016717  00000+0  30000-3 0  9993"
L2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49514345123456"

LUT = "0123456789. +-*"


def _decompress(b):
    return "".join(LUT[x >> 4] + LUT[x & 0x0F] for x in b)


def _elem(s, off, ln):
    return float(s[off:off + ln])


def test_encode_satellite_roundtrips_orbital_values():
    rec = drv.encode_satellite("ISS", L1, L2, {})
    assert len(rec) == drv.SAT_REC_LEN == 100
    assert rec[0:3] == b"ISS"

    d1 = _decompress(rec[8:20])               # 24 chars
    d2 = _decompress(rec[20:48])              # 56 chars
    # Line 1: year + epoch day.
    assert _elem(d1, 0, 2) == 24
    assert abs(_elem(d1, 2, 12) - 16.49) < 1e-6
    # Line 2: the firmware's field offsets -> known ISS values.
    assert abs(_elem(d2, 0, 8) - 51.6416) < 1e-4          # inclination
    assert abs(_elem(d2, 8, 8) - 247.4627) < 1e-4         # RAAN
    assert abs(_elem(d2, 16, 7) * 1.0e-7 - 0.0006703) < 1e-9   # eccentricity
    assert abs(_elem(d2, 23, 8) - 130.5360) < 1e-4        # arg perigee
    assert abs(_elem(d2, 31, 8) - 325.0288) < 1e-4        # mean anomaly
    assert abs(_elem(d2, 39, 11) - 15.49514345) < 1e-7    # mean motion


def test_encode_satellite_freqs_and_ctcss():
    rec = drv.encode_satellite("SO-50", L1, L2,
                               dict(rx1=436795000, tx1=145850000,
                                    ctcss1=67.0, arm1=74.4))
    rx1, tx1 = struct.unpack_from("<II", rec, 48)
    txcss, armcss = struct.unpack_from("<HH", rec, 56)
    assert rx1 == 43679500              # 10-Hz units (436.795 MHz)
    assert tx1 == 14585000             # 145.850 MHz
    assert txcss == drv._int2bcd(670)  # 67.0 Hz -> BCD 0x0670
    assert armcss == drv._int2bcd(744)  # 74.4 Hz arming tone


def test_build_block_size_and_termination():
    sats = [("ISS", L1, L2, {}), ("SO-50", L1, L2, dict(rx1=436795000))]
    block = drv.build_satellite_block(sats)
    assert len(block) == drv.SAT_BLOCK_LEN == 2520
    assert block[0:3] == b"ISS"
    assert block[100:105] == b"SO-50"
    assert block[200] == 0             # 3rd slot empty -> terminates the read


def test_parse_tle_and_freq_match():
    text = ("ISS (ZARYA)\n" + L1 + "\n" + L2 + "\n"
            "FOOSAT 99\n"
            "1 99999U 24001A   24016.00000000  .00000000  00000+0  00000+0 0  9990\n"
            "2 99999  98.0000 100.0000 0001000  90.0000 270.0000 14.20000000123456\n")
    parsed = drv.parse_tle_file(text)
    assert len(parsed) == 2
    assert parsed[0][0] == "ISS (ZARYA)"
    assert drv.match_satellite_freqs("ISS (ZARYA)")["rx1"] == 437800000
    assert drv.match_satellite_freqs("FOOSAT 99") is None
    # Whole-token match: 'SWISSCUBE' must NOT match the 'ISS' alias.
    assert drv.match_satellite_freqs("SWISSCUBE") is None

    block, names, dropped = drv.build_satellite_block_from_tle(text)
    assert names == ["ISS (ZARYA)"]    # only the known-freq sat kept
    assert dropped == 1
    assert len(block) == 2520


TLE_TEXT = "ISS (ZARYA)\n" + L1 + "\n" + L2 + "\n"


def _preload_custom_with_aes(fake, key_byte=0xAB):
    store = drv.AesKeyStore(tx_key_id=1)
    store.slots[0] = drv.AesSlot(True, 1, bytes([key_byte] * 32))
    region = drv.region_with_aes(bytes(b"\xFF" * drv.SECTOR_SIZE),
                                 store.to_payload())
    fake.flash[drv.CUSTOM_DATA_ADDR:drv.CUSTOM_DATA_ADDR + len(region)] = region


def _chain_blocks(fake):
    """Walk the fake's custom-data chain -> list of (type, payload)."""
    base = drv.CUSTOM_DATA_ADDR
    out = []
    if bytes(fake.flash[base:base + 8]) != drv.CUSTOM_MAGIC:
        return out
    off = drv.CUSTOM_HDR_LEN
    while off + 8 <= 0x10000:
        t, length = struct.unpack_from("<II", bytes(fake.flash), base + off)
        if length in (0, 0xFFFFFFFF):
            break
        out.append((t, bytes(fake.flash[base + off + 8:base + off + 8 + length])))
        off += 8 + length
    return out


def test_satellite_write_appends_and_preserves_aes():
    fake = FakeOpenGD77()
    _preload_custom_with_aes(fake)
    radio = drv.OpenGD77AESRadio(fake)
    block, names, _ = drv.build_satellite_block_from_tle(TLE_TEXT)
    radio._write_satellite_block(fake, block)

    blocks = dict(_chain_blocks(fake))
    # AES (type 6) intact, satellite (type 3) appended.
    aes = drv.AesKeyStore.from_payload(blocks[drv.CUSTOM_TYPE_AES_KEYS])
    assert aes.key_for(1).key == bytes([0xAB] * 32)
    assert len(blocks[drv.CUSTOM_TYPE_SATELLITE]) == drv.SAT_BLOCK_LEN
    assert blocks[drv.CUSTOM_TYPE_SATELLITE][0:3] == b"ISS"


def test_satellite_write_overwrites_in_place():
    fake = FakeOpenGD77()
    _preload_custom_with_aes(fake)
    radio = drv.OpenGD77AESRadio(fake)
    radio._write_satellite_block(fake, drv.build_satellite_block_from_tle(TLE_TEXT)[0])
    radio._write_satellite_block(
        fake, drv.build_satellite_block([("SO-50", L1, L2, dict(rx1=436795000))]))

    chain = _chain_blocks(fake)
    sat_blocks = [p for t, p in chain if t == drv.CUSTOM_TYPE_SATELLITE]
    assert len(sat_blocks) == 1                 # overwritten, not appended twice
    assert sat_blocks[0][0:5] == b"SO-50"
    # AES still present and first.
    assert chain[0][0] == drv.CUSTOM_TYPE_AES_KEYS


def test_satellite_write_refuses_without_magic():
    fake = FakeOpenGD77()                        # flash all 0xFF -> no magic
    radio = drv.OpenGD77AESRadio(fake)
    block = drv.build_satellite_block_from_tle(TLE_TEXT)[0]
    try:
        radio._write_satellite_block(fake, block)
    except Exception as e:
        assert "magic" in str(e).lower()
    else:
        assert False, "expected refusal to write without the OpenGD77 magic"
