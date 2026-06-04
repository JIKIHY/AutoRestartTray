@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   자동 재시작 트레이 - 포터블 EXE 빌드
echo ============================================
echo.

where py >nul 2>nul && (set "PY=py") || (set "PY=python")

echo [1/3] 필요 패키지 설치...
%PY% -m pip install --upgrade pip
%PY% -m pip install pyinstaller pystray pillow
if errorlevel 1 (
    echo.
    echo [오류] 패키지 설치 실패. Python 설치 여부를 확인하세요.
    pause
    exit /b 1
)

echo.
echo [2/3] 아이콘 생성...
%PY% make_ico.py

echo.
echo [3/3] 단일 EXE 빌드 (PyInstaller)...
if exist app.ico (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name AutoRestartTray --icon app.ico auto_restart.pyw
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name AutoRestartTray auto_restart.pyw
)

echo.
if exist "dist\AutoRestartTray.exe" (
    echo ============================================
    echo   완료!  dist\AutoRestartTray.exe
    echo   이 EXE 하나만 원하는 폴더로 복사해 쓰면 됩니다.
    echo ============================================
) else (
    echo [오류] 빌드 실패. 위 로그를 확인하세요.
)
echo.
pause
