"""An in-memory fake of the OpenGD77-AES CPS USB protocol for host tests.

Implements just enough of the 'R'/'X'/'C' command set (and the RADIO_INFO area)
that the driver's sync_in()/sync_out() can run with no hardware.  Backed by a
sparse flash + EEPROM image so reads return what writes stored.
"""
import struct

AREA_FLASH = 1
AREA_EEPROM = 2
AREA_RADIO_INFO = 9
SECTOR = 4096


class FakeOpenGD77(object):
    def __init__(self, radio_type=6, flash_size=0x110000, eeprom_size=0x10000):
        self.flash = bytearray(b"\xFF" * flash_size)
        self.eeprom = bytearray(b"\xFF" * eeprom_size)
        self.radio_type = radio_type
        self._out = b""
        self._sector = -1
        self._sectorbuf = None
        self.commits = 0          # number of committed sector writes
        # pyserial-compatible attributes the driver sets
        self.baudrate = 115200
        self.timeout = 1.0

    # -- pyserial-like API ------------------------------------------
    def reset_input_buffer(self):
        self._out = b""

    def flush(self):
        pass

    def read(self, n):
        data, self._out = self._out[:n], self._out[n:]
        return data

    def write(self, data):
        data = bytes(data)
        cmd = data[0]
        if cmd == ord("C"):
            self._out += b"-"
        elif cmd == ord("R"):
            self._handle_read(data)
        elif cmd == ord("X"):
            self._handle_write(data)
        else:
            self._out += b"-"
        return len(data)

    # -- protocol ---------------------------------------------------
    def _handle_read(self, data):
        area = data[1]
        addr = int.from_bytes(data[2:6], "big")
        n = int.from_bytes(data[6:8], "big")
        if area == AREA_RADIO_INFO:
            info = struct.pack("<II16s16sIH", 3, self.radio_type,
                               b"fakegit", b"20260101000000", 0xEF40, 0)
            payload = info[:n] if n <= len(info) else info + b"\x00" * (n - len(info))
        elif area == AREA_FLASH:
            payload = bytes(self.flash[addr:addr + n])
        elif area == AREA_EEPROM:
            # On MD-UV380 the emulated EEPROM IS the SPI flash at offset 0
            # (EEPROM.c: EEPROM_Read -> SPI_Flash_read(addr + 0)).
            payload = bytes(self.flash[addr:addr + n])
        else:
            self._out += b"-"
            return
        self._out += bytes([ord("R"), 0, 0]) + payload

    def _handle_write(self, data):
        sub = data[1]
        if sub == 1:
            self._sector = int.from_bytes(data[2:5], "big")
            base = self._sector * SECTOR
            self._sectorbuf = bytearray(self.flash[base:base + SECTOR])
            self._out += bytes([ord("X"), sub])
        elif sub == 2:
            addr = int.from_bytes(data[2:6], "big")
            n = int.from_bytes(data[6:8], "big")
            chunk = data[8:8 + n]
            base = self._sector * SECTOR
            for i, b in enumerate(chunk):
                self._sectorbuf[addr - base + i] = b
            self._out += bytes([ord("X"), sub])
        elif sub == 3:
            base = self._sector * SECTOR
            self.flash[base:base + SECTOR] = self._sectorbuf
            self._sector = -1
            self._sectorbuf = None
            self.commits += 1
            self._out += bytes([ord("X"), sub])
        else:
            self._out += b"-"
