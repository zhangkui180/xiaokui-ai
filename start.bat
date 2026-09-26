@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import pandas,numpy" 2>nul
if errorlevel 1 python -m pip install -r requirements.txt
echo.
echo 在浏览器打开 http://127.0.0.1:8771/
echo 关掉这个窗口就会停止工具。
echo.
python -u serve_tool.py
pause
