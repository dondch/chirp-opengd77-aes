"""Host tests for the AES key store codec and the round-trip over a fake radio.

No hardware required.  Run with: pytest  (see conftest.py for path setup).
"""
import struct

import opengd77_aes as drv
from tests.fake_radio import FakeOpenGD77


# -- pure codec ----------------------------------------------------------
def test_payload_roundtrip_all_slots():
    store = drv.AesKeyStore(tx_key_id=3)
    for i in range(drv.AESK_NUM_SLOTS):
        store.slots[i] = drv.AesSlot(True, i, bytes([(i + 1)] * 32))
    payload = store.to_payload()
    assert len(payload) == drv.AESK_PAYLOAD_LEN == 584
    assert payload[:4] == b"AESK"
    again = drv.AesKeyStore.from_payload(payload)
    assert again.tx_key_id == 3
    for i in range(drv.AESK_NUM_SLOTS):
        assert again.slots[i].valid
        assert again.slots[i].key_id == i
        assert again.slots[i].key == bytes([(i + 1)] * 32)


def test_invalid_slot_serializes_blank():
    store = drv.AesKeyStore()
    store.slots[5] = drv.AesSlot(True, 5, bytes(range(32)))
    payload = store.to_payload()
    parsed = drv.AesKeyStore.from_payload(payload)
    assert parsed.slots[5].valid
    assert not parsed.slots[0].valid
    assert parsed.slots[0].key_hex == ""


def _fresh_region(size=drv.SECTOR_SIZE):
    return bytes(b"\xFF" * size)


def test_region_fresh_then_find():
    store = drv.AesKeyStore(tx_key_id=1)
    store.slots[1] = drv.AesSlot(True, 1, bytes([0xAB] * 32))
    region = drv.region_with_aes(_fresh_region(), store.to_payload())
    assert region[:8] == drv.CUSTOM_MAGIC
    off, payload = drv.find_aes_block(region)
    assert off == drv.CUSTOM_HDR_LEN
    parsed = drv.AesKeyStore.from_payload(payload)
    assert parsed.tx_key_id == 1
    assert parsed.slots[1].key == bytes([0xAB] * 32)


def test_region_preserves_sibling_blocks():
    # magic + a dummy THEME_DAY block (type 4, len 10), then free space.
    region = bytearray(b"\xFF" * drv.SECTOR_SIZE)
    region[0:8] = drv.CUSTOM_MAGIC
    struct.pack_into("<II", region, drv.CUSTOM_HDR_LEN, 4, 10)
    region[drv.CUSTOM_HDR_LEN + 8:drv.CUSTOM_HDR_LEN + 18] = bytes(range(10))

    store = drv.AesKeyStore()
    store.slots[0] = drv.AesSlot(True, 0, bytes([0x11] * 32))
    out = drv.region_with_aes(bytes(region), store.to_payload())

    # The theme block is untouched...
    dtype, dlen = struct.unpack_from("<II", out, drv.CUSTOM_HDR_LEN)
    assert (dtype, dlen) == (4, 10)
    assert out[drv.CUSTOM_HDR_LEN + 8:drv.CUSTOM_HDR_LEN + 18] == bytes(range(10))
    # ...and the AES block was appended after it.
    off, payload = drv.find_aes_block(out)
    assert off == drv.CUSTOM_HDR_LEN + 8 + 10
    assert drv.AesKeyStore.from_payload(payload).slots[0].key == bytes([0x11] * 32)


def test_region_updates_in_place():
    store = drv.AesKeyStore()
    store.slots[0] = drv.AesSlot(True, 0, bytes([0x01] * 32))
    region = drv.region_with_aes(_fresh_region(), store.to_payload())

    store2 = drv.AesKeyStore.from_payload(drv.find_aes_block(region)[1])
    store2.slots[2] = drv.AesSlot(True, 2, bytes([0x02] * 32))
    region2 = drv.region_with_aes(region, store2.to_payload())

    # Same length, block stays at the same offset.
    assert len(region2) == len(region)
    off1, _ = drv.find_aes_block(region)
    off2, payload = drv.find_aes_block(region2)
    assert off1 == off2
    parsed = drv.AesKeyStore.from_payload(payload)
    assert parsed.slots[0].key == bytes([0x01] * 32)
    assert parsed.slots[2].key == bytes([0x02] * 32)


def test_bcd_helpers():
    # 146.520 MHz stored as BCD 0x14652000 (value/10 = Hz)
    assert drv._bcd2int(0x14652000) == 14652000
    assert drv._int2bcd(14652000) == 0x14652000
    assert drv._bcd2int(drv._int2bcd(43900125)) == 43900125


# -- end-to-end over the fake radio --------------------------------------
def _preload_aes(fake, store):
    region = drv.region_with_aes(bytes(b"\xFF" * drv.SECTOR_SIZE),
                                 store.to_payload())
    fake.flash[drv.CUSTOM_DATA_ADDR:drv.CUSTOM_DATA_ADDR + len(region)] = region


def test_sync_in_reads_keys():
    fake = FakeOpenGD77()
    pre = drv.AesKeyStore(tx_key_id=2)
    pre.slots[1] = drv.AesSlot(True, 1, bytes([0x42] * 32))
    _preload_aes(fake, pre)

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    store = radio._read_aes_store()
    assert store.tx_key_id == 2
    assert store.slots[1].key == bytes([0x42] * 32)


def test_settings_edit_then_upload_persists():
    fake = FakeOpenGD77()
    _preload_aes(fake, drv.AesKeyStore())

    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()

    settings = radio.get_settings()
    flat = {}
    for group in settings:
        for el in group:
            flat[el.get_name()] = el
    flat["aes_txkeyid"].value = 5
    flat["aes_valid_4"].value = True
    flat["aes_key_4"].value = "00112233445566778899aabbccddeeff" \
                              "00112233445566778899aabbccddeeff"
    radio.set_settings(settings)
    radio.sync_out()

    # Read the AES block straight from the fake radio's flash; the key is
    # addressed by key id 4 (not slot 4).
    region = bytes(fake.flash[drv.CUSTOM_DATA_ADDR:
                              drv.CUSTOM_DATA_ADDR + drv.SECTOR_SIZE])
    store = drv.AesKeyStore.from_payload(drv.find_aes_block(region)[1])
    assert store.tx_key_id == 5
    slot = store.key_for(4)
    assert slot is not None and slot.key_id == 4
    assert slot.key == bytes.fromhex(
        "00112233445566778899aabbccddeeff"
        "00112233445566778899aabbccddeeff")


def test_keys_listed_by_keyid_from_1():
    # Real radios store e.g. key id 1 in physical slot 0; it must show as
    # "Key id 1", and key id 0 must not be listed (selectors use 0 = off).
    fake = FakeOpenGD77()
    st = drv.AesKeyStore()
    st.slots[0] = drv.AesSlot(True, 1, bytes([0xAB] * 32))
    _preload_aes(fake, st)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    flat = {}
    for group in radio.get_settings():
        for el in group:
            flat[el.get_name()] = el
    assert "aes_valid_0" not in flat
    assert bool(flat["aes_valid_1"].value) is True
    assert str(flat["aes_key_1"].value) == "ab" * 32


def test_bad_radio_type_rejected():
    fake = FakeOpenGD77(radio_type=0)  # GD-77, not MD-UV380
    _preload_aes(fake, drv.AesKeyStore())
    radio = drv.OpenGD77AESRadio(fake)
    try:
        radio.sync_in()
    except Exception as e:
        assert "MD-UV380" in str(e) or "radioType" in str(e)
    else:
        assert False, "expected a RadioError for wrong radio type"


def _flatten(settings):
    flat = {}
    for group in settings:
        for el in group:
            flat[el.get_name()] = el
    return flat


def test_general_settings_roundtrip():
    fake = FakeOpenGD77()
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = _flatten(settings)
    flat["callsign"].value = "N0CALL"
    flat["dmrid"].value = 1234567
    radio.set_settings(settings)
    radio.sync_out()

    r2 = drv.OpenGD77AESRadio(fake)
    r2.sync_in()
    f2 = _flatten(r2.get_settings())
    assert str(f2["callsign"].value) == "N0CALL"
    assert int(f2["dmrid"].value) == 1234567


def test_invalid_hex_key_rejected():
    fake = FakeOpenGD77()
    _preload_aes(fake, drv.AesKeyStore())
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    settings = radio.get_settings()
    flat = {}
    for group in settings:
        for el in group:
            flat[el.get_name()] = el
    flat["aes_valid_1"].value = True
    flat["aes_key_1"].value = "abc"  # too short
    try:
        radio.set_settings(settings)
    except Exception as e:
        assert "64 hex" in str(e)
    else:
        assert False, "expected InvalidValueError for short key"
