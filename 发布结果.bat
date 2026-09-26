@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist web\results.json (
  echo 还没有筛选结果，先双击 start.bat，在页面上点重新拉取。
  pause
  exit /b 1
)
if not exist docs mkdir docs
copy /Y web\results.json docs\results.json >nul
git add web\results.json docs\results.json
git diff --cached --quiet
if errorlevel 1 (
  git -c user.name=zhangkui180 -c user.email=58655214+zhangkui180@users.noreply.github.com commit -m "更新已发布的筛选结果"
  for /f %%i in ('"C:\Users\37090\AppData\Local\Programs\gh\bin\gh.exe" auth token') do set GH_TOKEN=%%i
  git -c http.extraheader="AUTHORIZATION: bearer %GH_TOKEN%" push origin HEAD
) else (
  echo 结果和上次发布的一样，没有新内容要发。
)
pause
