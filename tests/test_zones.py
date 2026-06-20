"""Host tests for zones (CHIRP banks). No hardware."""
import opengd77_aes as drv
from chirp import chirp_common
from tests.fake_radio import FakeOpenGD77

FLASH_BANKS = 7


def _fresh():
    fake = FakeOpenGD77()
    # Realistic empty codeplug.
    fake.flash[drv.EE_CH_BITMAP_ADDR:drv.EE_CH_BITMAP_ADDR + 16] = b"\x00" * 16
    for b in range(FLASH_BANKS):
        a = drv.FLASH_CH_BITMAP_ADDR + b * drv.FLASH_BANK_STRIDE
        fake.flash[a:a + 16] = b"\x00" * 16
    fake.flash[drv.ZONE_BITMAP_ADDR:drv.ZONE_BITMAP_ADDR + 32] = b"\x00" * 32
    fake.flash[0x806F] = 0x00      # force the 80-channels-per-zone format
    radio = drv.OpenGD77AESRadio(fake)
    radio.sync_in()
    return fake, radio


def _mk_channel(radio, number, name):
    mem = chirp_common.Memory()
    mem.number = number
    mem.name = name
    mem.freq = 145000000 + number * 1000
    mem.mode = "FM"
    radio.set_memory(mem)


def _reload(fake):
    r = drv.OpenGD77AESRadio(fake)
    r.sync_in()
    return r


def test_zone_create_members_name():
    fake, radio = _fresh()
    _mk_channel(radio, 2, "CH2")
    _mk_channel(radio, 3, "CH3")

    model = radio.get_bank_model()
    banks = model.get_mappings()
    assert len(banks) == drv.ZONES_MAX
    model.add_memory_to_mapping(radio.get_memory(2), banks[0])
    model.add_memory_to_mapping(radio.get_memory(3), banks[0])
    banks[0].set_name("My Zone")
    radio.sync_out()

    r2 = _reload(fake)
    m2 = r2.get_bank_model()
    b0 = m2.get_mappings()[0]
    assert b0.get_name() == "My Zone"
    members = [mem.number for mem in m2.get_mapping_memories(b0)]
    assert members == [2, 3]
    maps = [b.get_index() for b in m2.get_memory_mappings(r2.get_memory(2))]
    assert maps == [0]


def test_zone_multi_membership():
    fake, radio = _fresh()
    _mk_channel(radio, 5, "CH5")
    model = radio.get_bank_model()
    banks = model.get_mappings()
    model.add_memory_to_mapping(radio.get_memory(5), banks[0])
    model.add_memory_to_mapping(radio.get_memory(5), banks[1])
    radio.sync_out()

    r2 = _reload(fake)
    m2 = r2.get_bank_model()
    maps = sorted(b.get_index() for b in m2.get_memory_mappings(r2.get_memory(5)))
    assert maps == [0, 1]


def test_zone_remove_member():
    fake, radio = _fresh()
    _mk_channel(radio, 2, "CH2")
    _mk_channel(radio, 3, "CH3")
    model = radio.get_bank_model()
    banks = model.get_mappings()
    model.add_memory_to_mapping(radio.get_memory(2), banks[0])
    model.add_memory_to_mapping(radio.get_memory(3), banks[0])
    model.remove_memory_from_mapping(radio.get_memory(2), banks[0])
    radio.sync_out()

    r2 = _reload(fake)
    m2 = r2.get_bank_model()
    members = [mem.number for mem in m2.get_mapping_memories(m2.get_mappings()[0])]
    assert members == [3]


def test_zone_format_detected_80():
    fake, radio = _fresh()
    assert radio._channels_per_zone() == 80
