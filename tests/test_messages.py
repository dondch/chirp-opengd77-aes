"""Host tests for the encrypted-SMS messaging config (MSGC custom-data block).

No hardware required.  The MSGC layout is shared byte-for-byte with the firmware
(functions/dmr_sms.c); these lock the codec + the round-trip over a fake radio.
"""
import struct

import opengd77_aes as drv
from tests.fake_radio import FakeOpenGD77


# -- pure codec ----------------------------------------------------------
def test_msgconfig_payload_roundtrip():
    cfg = drv.MsgConfig(default_dst=9661, default_group=True,
                        presets=["QSL 73", "On my way", "", "ack"])
    payload = cfg.to_payload()
    assert len(payload) == drv.MSGC_PAYLOAD_LEN == 492
    assert payload[:4] == b"MSGC"
    again = drv.MsgConfig.from_payload(payload)
    assert again.default_dst == 9661
    assert again.default_group is True
    assert again.presets[0] == "QSL 73"
    assert again.presets[1] == "On my way"
    assert again.presets[2] == ""
    assert again.presets[3] == "ack"


def test_msgconfig_private_and_truncation():
    long_text = "x" * 100
    cfg = drv.MsgConfig(default_dst=12341, default_group=False,
                        presets=[long_text])
    again = drv.MsgConfig.from_payload(cfg.to_payload())
    assert again.default_group is False
    assert again.default_dst == 12341
    assert again.presets[0] == "x" * drv.MSGC_PRESET_LEN   # clamped to 48


def test_msgconfig_blank_payload():
    cfg = drv.MsgConfig.from_payload(b"")
    assert cfg.default_dst == 0
    assert all(p == "" for p in cfg.presets)
    assert cfg.max_len == 0            # unset -> firmware default (144)


def test_msgconfig_max_len_roundtrip():
    # maxLen lives at header byte 7 (firmware dmrSmsCfg_t.maxLen); 0 = default.
    cfg = drv.MsgConfig(default_dst=9661, default_group=True,
                        presets=["hi"], max_len=100)
    payload = cfg.to_payload()
    assert payload[7] == 100
    again = drv.MsgConfig.from_payload(payload)
    assert again.max_len == 100
    assert again.default_dst == 9661 and again.presets[0] == "hi"
    assert drv.MsgConfig().to_payload()[7] == 0   # default writes 0


# -- block chain: config coexists with the AES block ---------------------
def test_config_block_preserves_aes_sibling():
    store = drv.AesKeyStore(tx_key_id=1)
    store.slots[1] = drv.AesSlot(True, 1, bytes([0xAB] * 32))
    region = drv.region_with_aes(bytes(b"\xFF" * drv.SECTOR_SIZE),
                                 store.to_payload())

    cfg = drv.MsgConfig(default_dst=4007, default_group=True,
                        presets=["hello"])
    region = drv.region_with_block(region, drv.CUSTOM_TYPE_MSG_CONFIG,
                                   cfg.to_payload())

    # AES block intact...
    aes = drv.AesKeyStore.from_payload(drv.find_aes_block(region)[1])
    assert aes.tx_key_id == 1
    assert aes.key_for(1).key == bytes([0xAB] * 32)
    # ...and the config block readable.
    _, payload = drv.find_block(region, drv.CUSTOM_TYPE_MSG_CONFIG)
    assert drv.MsgConfig.from_payload(payload).presets[0] == "hello"


# -- end-to-end over the fake radio --------------------------------------
def _flatten(settings):
    flat = {}
    for group in settings:
        for el in group:
            flat[el.get_name()] = el
    return flat


def _preload_aes(fake):
    region = drv.region_with_aes(bytes(b"\xFF" * drv.SECTOR_SIZE),
                                 drv.AesKeyStore().to_payload())
    fake.flash[drv.CUSTOM_DATA_ADDR:drv.CUSTOM_DATA_ADDR + len(region)] = region


def test_messages_settings_edit_then_upload_persists():
    fake = FakeOpenGD77()
    _preload_aes(fake)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()

    settings = radio.get_settings()
    flat = _flatten(settings)
    assert "msg_default_dst" in flat          # Messages tab present
    flat["msg_default_dst"].value = 91234
    flat["msg_default_group"].value = "Private"
    flat["msg_max_len"].value = 64
    flat["msg_preset_0"].value = "QRX 5 min"
    flat["msg_preset_1"].value = "73 de op"
    radio.set_settings(settings)
    radio.sync_out()

    # config block landed in the radio's flash, alongside the AES block
    region = bytes(fake.flash[drv.CUSTOM_DATA_ADDR:
                              drv.CUSTOM_DATA_ADDR + drv.SECTOR_SIZE])
    _, payload = drv.find_block(region, drv.CUSTOM_TYPE_MSG_CONFIG)
    cfg = drv.MsgConfig.from_payload(payload)
    assert cfg.default_dst == 91234
    assert cfg.default_group is False
    assert cfg.max_len == 64
    assert cfg.presets[0] == "QRX 5 min"
    assert cfg.presets[1] == "73 de op"
    # AES block still there
    assert drv.find_aes_block(region)[1] is not None

    # and it reads back through get_settings
    r2 = drv.OpenGD77AESRadio(fake)
    r2.sync_in()
    f2 = _flatten(r2.get_settings())
    assert int(f2["msg_default_dst"].value) == 91234
    assert int(f2["msg_max_len"].value) == 64
    assert str(f2["msg_preset_0"].value) == "QRX 5 min"


def test_messages_untouched_leaves_no_config_block():
    # Opening other tabs and uploading must NOT create a spurious MSGC block.
    fake = FakeOpenGD77()
    _preload_aes(fake)
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()

    settings = radio.get_settings()
    flat = _flatten(settings)
    flat["aes_txkeyid"].value = 2          # only touch AES
    radio.set_settings(settings)
    radio.sync_out()

    region = bytes(fake.flash[drv.CUSTOM_DATA_ADDR:
                              drv.CUSTOM_DATA_ADDR + drv.SECTOR_SIZE])
    off, _ = drv.find_block(region, drv.CUSTOM_TYPE_MSG_CONFIG)
    assert off is None                     # no config block written
