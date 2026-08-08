@echo off
setlocal

echo ============================================================
echo   LICENSE PLATE SYSTEM - SHUTDOWN
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/3] Stopping Docker Web Service (docker compose down)...
docker compose down
echo [OK] Docker Web Service stopped.
echo.

echo [2/3] Closing OCR and Sync Manager windows...
taskkill /FI "WINDOWTITLE eq Plaka - OCR*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Plaka - Sync Manager*" /F >nul 2>&1
echo [OK] Project Python windows closed.
echo.

echo [3/3] Stopping Cloud SQL Auth Proxy...
taskkill /FI "WINDOWTITLE eq Plaka - Cloud SQL Proxy*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cloud-sql-proxy.exe" /F >nul 2>&1
echo [OK] Cloud SQL Auth Proxy stopped.
echo.

echo ============================================================
echo   SHUTDOWN COMPLETE
echo ============================================================
echo.
pause
