"""
tests/test_barrier_controller.py — BarrierController Birim Testleri

pyserial gerektirmeyen, unittest.mock ile seri port haberleşmesini
simüle eden test paketi.
"""

import unittest
from unittest.mock import MagicMock, patch
import serial

import sys
from pathlib import Path

# src klasörünü sys.path'e ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from barrier_controller import BarrierController


class TestBarrierController(unittest.TestCase):
    """
    BarrierController sınıfı için birim testler.
    """

    def test_1_dry_run_connect(self):
        """
        TEST 1: dry_run=True iken connect() True dönmeli ve gerçek port açmamalı.
        """
        bc = BarrierController(dry_run=True)
        result = bc.connect()
        self.assertTrue(result)
        self.assertTrue(bc.is_connected())
        self.assertIsNone(bc.ser)

    def test_2_dry_run_send_open(self):
        """
        TEST 2: dry_run=True iken send_open() True dönmeli.
        """
        bc = BarrierController(dry_run=True)
        bc.connect()
        result = bc.send_open()
        self.assertTrue(result)

    @patch("serial.Serial")
    def test_3_mock_serial_connection(self, mock_serial_cls):
        """
        TEST 3: dry_run=False ve geçerli port ile mock serial bağlantısı kurulmalı.
        """
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_serial_cls.return_value = mock_ser

        bc = BarrierController(port="COM5", baudrate=115200, dry_run=False)
        result = bc.connect()

        self.assertTrue(result)
        self.assertTrue(bc.is_connected())
        mock_serial_cls.assert_called_once_with(port="COM5", baudrate=115200, timeout=1.0)

    @patch("serial.Serial")
    def test_4_send_open_writes_command(self, mock_serial_cls):
        """
        TEST 4: send_open() çağrıldığında seri porta b'OPEN\n' yazılmalı.
        """
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.readline.return_value = b"OK\n"
        mock_serial_cls.return_value = mock_ser

        bc = BarrierController(port="COM5", dry_run=False)
        bc.connect()
        bc.send_open()

        mock_ser.write.assert_called_once_with(b"OPEN\n")
        mock_ser.flush.assert_called_once()

    @patch("serial.Serial")
    def test_5_send_open_returns_true_on_ok(self, mock_serial_cls):
        """
        TEST 5: ESP32 mock yanıtı b'OK\n' ise send_open() True dönmeli.
        """
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_ser.readline.return_value = b"OK\r\n"
        mock_serial_cls.return_value = mock_ser

        bc = BarrierController(port="COM5", dry_run=False)
        bc.connect()
        result = bc.send_open()

        self.assertTrue(result)

    @patch("serial.Serial")
    def test_6_serial_exception_handled_gracefully(self, mock_serial_cls):
        """
        TEST 6: SerialException oluştuğunda program çökmemeli, False dönmeli.
        """
        mock_serial_cls.side_effect = serial.SerialException("Port bulunamadı")

        bc = BarrierController(port="INVALID_PORT", dry_run=False)
        connect_result = bc.connect()

        # Connect exception fırlatmadan False dönmeli
        self.assertFalse(connect_result)
        self.assertFalse(bc.is_connected())

        # send_open denendiğinde de exception fırlatmadan False dönmeli
        send_result = bc.send_open()
        self.assertFalse(send_result)


if __name__ == "__main__":
    unittest.main()
