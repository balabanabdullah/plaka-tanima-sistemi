@echo off
setlocal

echo ============================================================
echo   LICENSE PLATE SYSTEM - LOCAL STARTUP
echo ============================================================
echo.

cd /d "%~dp0"

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

if not exist "%~dp0cloud-sql-proxy.exe" (
    echo [ERROR] cloud-sql-proxy.exe not found in project root directory.
    echo Please place cloud-sql-proxy.exe in the project directory.
    pause
    exit /b 1
)

if "%CLOUD_DATABASE_URL%"=="" (
    echo [WARNING] CLOUD_DATABASE_URL environment variable is not set.
    echo Local OCR and Docker Web UI will run, but cloud sync will be inactive.
    echo.
)

echo [1/4] Starting Docker Web Service (docker compose up -d)...
docker compose up -d
if errorlevel 1 (
    echo [ERROR] Docker Compose failed to start.
    pause
    exit /b 1
)
echo [OK] Docker Web Service started.
echo.

echo [2/4] Starting Cloud SQL Auth Proxy in a new window...
start "Plaka - Cloud SQL Proxy" cmd /k ""%~dp0cloud-sql-proxy.exe" --port 5433 plaka-tanima-abdullah-2026:europe-west1:plaka-postgres"
echo [OK] Cloud SQL Auth Proxy window launched.
echo.

timeout /t 2 /nobreak >nul

echo [3/4] Starting Sync Manager in a new window...
start "Plaka - Sync Manager" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0src\sync_manager.py""
echo [OK] Sync Manager window launched.
echo.

timeout /t 1 /nobreak >nul

echo [4/4] Starting OCR Reader in a new window...
start "Plaka - OCR" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0src\ocr_reader.py" --camera 0 --direction entry --camera-name test_giris --barrier-dry-run"
echo [OK] OCR Reader window launched.
echo.

echo ============================================================
echo   ALL COMPONENTS LAUNCHED SUCCESSFULLY
echo ============================================================
echo  - Web UI  : http://localhost:8000
echo  - Proxy   : 127.0.0.1:5433
echo  - Run stop_system.bat to shut down all components.
echo ============================================================
echo.
pause
