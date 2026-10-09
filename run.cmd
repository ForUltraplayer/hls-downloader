@rem @file run.cmd
@rem @author: rudals252
@rem @brief 설치된 Python으로 HLS 입력 UI 실행.
@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required. Install it and enable PATH.
    pause
    exit /b 1
)
python "%~dp0hls_downloader.py"
if errorlevel 1 pause
