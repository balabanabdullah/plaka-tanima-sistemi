@echo off
setlocal

echo ============================================================
echo   LICENSE PLATE SYSTEM - LOCAL STARTUP (HTTPS SYNC)
echo ============================================================
echo.

cd /d "%~dp0"

if "%CAMERA_SOURCE%"=="" (
    set "CAMERA_SOURCE=0"
)

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found at .venv\Scripts\python.exe
    echo Please create the virtual environment before starting.
    pause
    exit /b 1
)

docker --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running or not installed.
    echo Please start Docker Desktop and try again.
    pause
    exit /b 1
)

if "%CLOUD_SYNC_API_URL%"=="" (
    echo [WARNING] CLOUD_SYNC_API_URL environment variable is not set.
    echo Local OCR and Docker Web UI will run, but cloud sync will be inactive.
    echo.
)

if "%SYNC_API_TOKEN%"=="" (
    echo [WARNING] SYNC_API_TOKEN environment variable is not set.
    echo Local OCR and Docker Web UI will run, but cloud sync authentication will fail.
    echo.
)

echo [1/3] Starting Docker Web Service (docker compose up -d)...
docker compose up -d
if errorlevel 1 (
    echo [ERROR] Docker Compose failed to start.
    pause
    exit /b 1
)
echo [OK] Docker Web Service started.
echo.

timeout /t 2 /nobreak >nul

echo [2/3] Starting Sync Manager in a new window...
start "Plaka - Sync Manager" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0src\sync_manager.py""
echo [OK] Sync Manager window launched.
echo.

timeout /t 1 /nobreak >nul

echo [3/3] Starting OCR Reader in a new window...
echo Camera source: %CAMERA_SOURCE%
start "Plaka - OCR" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0src\ocr_reader.py" --camera %CAMERA_SOURCE% --direction auto --camera-name test_giris --barrier-dry-run"
echo [OK] OCR Reader window launched.
echo.

echo ============================================================
echo   ALL COMPONENTS LAUNCHED SUCCESSFULLY
echo ============================================================
echo  - Web UI  : http://localhost:8000
echo  - Sync    : HTTPS API (%CLOUD_SYNC_API_URL%)
echo  - Run stop_system.bat to shut down all components.
echo ============================================================
echo.
pause
