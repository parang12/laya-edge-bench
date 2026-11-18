@echo off
setlocal
cd /d "%~dp0"
set "BENCH_PYTHON="

if defined LAYA_PYTHON (
  if not exist "%LAYA_PYTHON%" (
    echo LAYA_PYTHON does not point to an existing Python executable.
    pause
    exit /b 1
  )
  set "BENCH_PYTHON=%LAYA_PYTHON%"
)
if not defined BENCH_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "BENCH_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined BENCH_PYTHON if exist "%USERPROFILE%\Documents\ChatGPT\IOT\.laya-runtime\Scripts\python.exe" set "BENCH_PYTHON=%USERPROFILE%\Documents\ChatGPT\IOT\.laya-runtime\Scripts\python.exe"

if not defined BENCH_PYTHON (
  echo Python environment not found. See the project README for setup.
  pause
  exit /b 1
)
"%BENCH_PYTHON%" -B "%~dp0app.py"
set "BENCH_EXIT=%ERRORLEVEL%"
if not "%BENCH_EXIT%"=="0" pause
exit /b %BENCH_EXIT%
