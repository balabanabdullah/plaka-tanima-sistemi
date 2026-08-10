@echo off
setlocal

echo ============================================================
echo   LICENSE PLATE SYSTEM - SHUTDOWN
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/2] Stopping Docker Web Service (docker compose down)...
docker compose down
echo [OK] Docker Web Service stopped.
echo.

echo [2/2] Closing OCR and Sync Manager windows...
taskkill /FI "WINDOWTITLE eq Plaka - OCR*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Plaka - Sync Manager*" /F >nul 2>&1
echo [OK] Project Python windows closed.
echo.

echo ============================================================
echo   SHUTDOWN COMPLETE
echo ============================================================
echo.
pause
