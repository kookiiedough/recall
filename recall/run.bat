@echo off
REM One-command start for Windows. Creates .venv on first run, then launches Recall.
cd /d "%~dp0"
if not exist .venv (
  echo [recall] creating virtualenv ^(first run only^)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
  .venv\Scripts\pip install -r requirements.txt
)
where ffmpeg >nul 2>nul || (echo [recall] ffmpeg not found - install with: winget install Gyan.FFmpeg & exit /b 1)
.venv\Scripts\python -m recall %*
