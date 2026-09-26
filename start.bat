@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import pandas,numpy" 2>nul
if errorlevel 1 python -m pip install -r requirements.txt
echo.
echo 电脑地址 http://127.0.0.1:8771/
echo 手机地址 https://zhangkui180.github.io/xiaokui-ai/
echo 在电脑上点重新拉取。要更新手机页面，拉完后双击 发布结果.bat。
echo 关掉这个窗口就会停止工具。
echo.
python -u serve_tool.py
pause
