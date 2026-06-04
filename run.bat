@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 빌드 없이 Python 으로 바로 실행 (테스트용)
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
%PY% -m pip install pystray pillow >nul 2>nul
where pythonw >nul 2>nul && (start "" pythonw auto_restart.pyw) || (start "" %PY% auto_restart.pyw)
