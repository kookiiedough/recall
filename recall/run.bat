@echo off
REM One-command start for Windows. Creates .venv on first run, upgrades yt-dlp, then launches Recall.
cd /d "%~dp0"
if not exist .venv (
  echo [recall] creating virtualenv ^(first run only^)...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
  .venv\Scripts\pip install -r requirements.txt
)
REM TikTok changes often and yt-dlp fixes it within days: always run the newest yt-dlp (+ curl_cffi).
REM set RECALL_NO_UPDATE=1 to skip; set RECALL_YTDLP_NIGHTLY=1 for the nightly build.
if not defined RECALL_NO_UPDATE (
  echo [recall] updating yt-dlp...
  if defined RECALL_YTDLP_NIGHTLY (
    .venv\Scripts\python -m pip install -q --disable-pip-version-check -U --pre "yt-dlp[default,curl-cffi]" || echo [recall] could not update yt-dlp - continuing
  ) else (
    .venv\Scripts\python -m pip install -q --disable-pip-version-check -U "yt-dlp[default,curl-cffi]" || echo [recall] could not update yt-dlp - continuing
  )
)
for /f "delims=" %%v in ('.venv\Scripts\python -m yt_dlp --version 2^>nul') do echo [recall] yt-dlp %%v
where ffmpeg >nul 2>nul || (echo [recall] ffmpeg not found - install with: winget install Gyan.FFmpeg & exit /b 1)
.venv\Scripts\python -m recall %*
