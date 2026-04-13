@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo =============================================
echo    UZ NEWS BOT - Проверка настроек
echo =============================================
echo.
python setup.py
echo.
echo === Нажми любую клавишу для закрытия ===
pause >nul
