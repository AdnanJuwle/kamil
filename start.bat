@echo off
REM start.bat -- bring Kamil up on Windows (single-model).
REM
REM One llama-server holding Huihui-Qwen3.8-27B-abliterated, which serves chat AND
REM vision. The old two-model setup (DeepSeek on :8080 + Gemma voice on :8084) is
REM gone; so is the separate vision model. See core/model.py.
REM
REM The llama-server flags are not written here -- core/model.py computes them from
REM the GGUF present and this machine's VRAM, so this file never goes stale.
setlocal
cd /d "%~dp0"

set "PY=%~dp0venv313\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [kamil] resolving model and llama-server...
for /f "delims=" %%B in ('"%PY%" -m core.model --bin') do set "LLAMA=%%B"
if not exist "%LLAMA%" (
  echo [kamil] llama-server not found at "%LLAMA%".
  echo [kamil] Huihui-Qwen3.8 needs a CURRENT llama.cpp ^(architecture "qwen35"^).
  echo [kamil] Set KAMIL_LLAMA_SERVER, or build a fresh llama.cpp.
  exit /b 1
)

REM Collect the computed flags onto one line.
set "ARGS="
for /f "delims=" %%A in ('"%PY%" -m core.model --serve-args') do call set "ARGS=%%ARGS%% %%A"
if not defined ARGS (
  echo [kamil] could not compute llama-server flags:
  "%PY%" -m core.model
  exit /b 1
)

for /f "delims=" %%L in ('"%PY%" -c "from core import model; print(model.label())"') do set "LABEL=%%L"
echo [kamil] model: %LABEL%

start "Kamil Model [%LABEL%]" cmd /k ""%LLAMA%"%ARGS%"

echo [kamil] waiting for the model to load (a 27B takes a while)...
timeout /t 90 /nobreak > nul

echo [kamil] starting backend on :3000
start "Kamil Backend" cmd /k ""%PY%" run.py"

echo.
echo [kamil] open http://localhost:3000
echo [kamil] room watch is OFF on Windows by default; set KAMIL_ROOM_WATCH=1 to enable.
endlocal
