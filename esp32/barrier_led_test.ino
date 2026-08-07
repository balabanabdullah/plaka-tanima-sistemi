/*
 * ============================================================================
 * ESP32 Bariyer LED Simülasyonu (barrier_led_test.ino)
 * ============================================================================
 * 
 * Bu C++ / Arduino kodu ESP32 mikrodenetleyicisi üzerinde çalışır.
 * USB Serial üzerinden Python uygulamasından gelen "OPEN" komutunu dinler.
 * Komut alındığında LED pini HIGH yapılır, anında "OK" yanıtı döner ve
 * millis() tabanlı non-blocking timer ile LED 3 saniye sonra kapatılır.
 * 
 * HARDWARE / DEVRE BAĞLANTISI:
 * ----------------------------------------------------------------------------
 * ESP32 GPIO 18  -->  220 Ohm (veya 330 Ohm) Seri Direnç  -->  LED Anot (+)
 * GND            ----------------------------------------->  LED Katot (-)
 * 
 * DİKKAT: LED doğrudan GPIO ile GND arasına dirençsiz bağlanmamalıdır!
 * ============================================================================
 */

#include <Arduino.h>

// LED Pin ve Zamanlama Sabitleri
const int LED_PIN = 18;
const unsigned long LED_ON_DURATION_MS = 3000;

// Zamanlayıcı Değişkenleri (Non-blocking millis mantığı)
unsigned long ledTurnOffTime = 0;
bool isLedActive = false;

void setup() {
    // Seri haberleşmeyi 115200 baud hızında başlat
    Serial.begin(115200);

    // LED pinini çıkış olarak ayarla ve kapalı konuma getir
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
}

void loop() {
    // 1. Seri porttan veri geldi mi kontrol et
    if (Serial.available() > 0) {
        String command = Serial.readStringUntil('\n');
        command.trim();

        if (command == "OPEN") {
            // LED'i yak
            digitalWrite(LED_PIN, HIGH);
            isLedActive = true;
            ledTurnOffTime = millis() + LED_ON_DURATION_MS;

            // Python uygulamasına anında doğrulama yanıtı gönder
            Serial.println("OK");
        } else if (command.length() > 0) {
            // Bilinmeyen komutlar için hata döndür
            Serial.println("ERROR");
        }
    }

    // 2. Non-blocking LED kapatma zamanı kontrolü
    if (isLedActive && (millis() >= ledTurnOffTime)) {
        digitalWrite(LED_PIN, LOW);
        isLedActive = false;
    }
}
