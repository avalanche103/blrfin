@echo off
setlocal

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found: "%PYTHON_EXE%"
    echo Create or restore .venv first.
    exit /b 1
)

set "MODE=%~1"
if "%MODE%"=="" set "MODE=start"

if /I "%MODE%"=="check" goto :check
if /I "%MODE%"=="migrate" goto :migrate
if /I "%MODE%"=="run" goto :run
if /I "%MODE%"=="start" goto :start
if /I "%MODE%"=="help" goto :help

echo [ERROR] Unknown mode: %MODE%
goto :help

:check
echo [INFO] Running Django system checks...
"%PYTHON_EXE%" manage.py check
exit /b %ERRORLEVEL%

:migrate
echo [INFO] Applying migrations...
"%PYTHON_EXE%" manage.py migrate
exit /b %ERRORLEVEL%

:run
echo [INFO] Starting local server at http://127.0.0.1:8000/
"%PYTHON_EXE%" manage.py runserver
exit /b %ERRORLEVEL%

:start
call "%~f0" migrate
if errorlevel 1 exit /b %ERRORLEVEL%
call "%~f0" run
exit /b %ERRORLEVEL%

:help
echo Usage:
echo   run_local.bat           ^<migrate + runserver^>
echo   run_local.bat check     ^<manage.py check^>
echo   run_local.bat migrate   ^<manage.py migrate^>
echo   run_local.bat run       ^<manage.py runserver^>
echo   run_local.bat help
exit /b 1