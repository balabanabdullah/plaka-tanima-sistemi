"""
barrier_controller.py — ESP32 USB Seri Haberleşme ve Bariyer Kontrol Modülü

Bu modül, Python uygulamasının USB Serial port üzerinden ESP32 ile
haberleşmesini sağlar. Yetkili (allow) araç geçişlerinde ESP32'ye "OPEN"
komutu göndererek bariyer simülasyon LED'inin yanmasını tetikler.
"""

import time
import serial


class BarrierController:
    """
    ESP32 seri port bariyer kontrolcüsü sınıfı.
    """

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 115200,
        timeout: float = 1.0,
        dry_run: bool = False,
    ) -> None:
        """
        Sınıf başlatıcısı.

        Parametreler:
            port (str | None): COM portu veya seri cihaz adresi (örn. 'COM5', '/dev/ttyUSB0')
            baudrate (int): Seri haberleşme hızı (Varsayılan: 115200)
            timeout (float): Okuma zaman aşımı (saniye)
            dry_run (bool): True ise gerçek seri port açılmaz, test logları yazılır.
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.dry_run = dry_run
        self.ser: serial.Serial | None = None

    def connect(self) -> bool:
        """
        ESP32 cihazına seri port üzerinden bağlanır.
        dry_run=True ise simülasyon modunda çalışır.

        Döndürür:
            bool: Bağlantı başarılıysa True, aksi halde False.
        """
        if self.dry_run:
            print("[ESP32] Dry-run modu aktif. Seri port açılmayacak.")
            return True

        if not self.port:
            print("[ESP32] Seri port belirtilmedi. Cihaza bağlanılamadı.")
            return False

        try:
            print(f"[ESP32] {self.port} portuna {self.baudrate} baud hızıyla bağlanılıyor...")
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
            )
            # ESP32 seri bağlantı kurulduğunda otomatik resetlenebilir; 2 saniye bekle
            time.sleep(2.0)
            self.ser.reset_input_buffer()
            print(f"[ESP32] {self.port} portuna başarıyla bağlandı.")
            return True
        except serial.SerialException as e:
            print(f"[ESP32] Bağlantı kurulamadı: {e}")
            self.ser = None
            return False
        except Exception as e:
            print(f"[ESP32] Beklenmeyen seri port hatası: {e}")
            self.ser = None
            return False

    def is_connected(self) -> bool:
        """
        Seri portun bağlı ve açık olup olmadığını kontrol eder.

        Döndürür:
            bool: Bağlı ise True.
        """
        if self.dry_run:
            return True
        return self.ser is not None and self.ser.is_open

    def send_open(self) -> bool:
        """
        ESP32'ye bariyeri açma (OPEN) komutu gönderir.

        Döndürür:
            bool: Komut başarıyla gönderilip 'OK' yanıtı alındıysa veya dry_run ise True.
        """
        if self.dry_run:
            print("[DRY RUN] OPEN")
            return True

        if not self.is_connected():
            print("[ESP32] Komut gönderilemedi: Seri port bağlı değil.")
            return False

        try:
            # ESP32'ye komutu satır sonu ekleyerek gönder
            self.ser.write(b"OPEN\n")
            self.ser.flush()

            # ESP32'den yanıt bekle (OK)
            response = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if response == "OK":
                print("[ESP32] OPEN -> OK")
                return True
            else:
                print(f"[ESP32] Beklenmeyen yanıt alındı: '{response}'")
                return False
        except serial.SerialException as e:
            print(f"[ESP32] Komut gönderimi sırasında seri hata: {e}")
            return False
        except Exception as e:
            print(f"[ESP32] Komut gönderilirken hata oluştu: {e}")
            return False

    def disconnect(self) -> None:
        """
        Seri port bağlantısını güvenli şekilde kapatır.
        """
        if self.ser is not None and self.ser.is_open:
            try:
                self.ser.close()
                print("[ESP32] Seri port bağlantısı kapatıldı.")
            except Exception as e:
                print(f"[ESP32] Port kapatılırken sorun oluştu: {e}")
        self.ser = None
