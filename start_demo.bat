@echo off
REM ============================================================
REM  一键启动 Demo —— 跨境内容合规引擎（复赛作品）
REM  双击本文件即可在 Windows 上启动 Streamlit 演示服务
REM ============================================================
REM 说明：本脚本只负责启动本地 Web 服务，不涉及任何 API 密钥的明文使用
REM      （密钥由程序从上层目录 .env 自动读取，不在此出现）。

setlocal
REM 工作目录：本文件所在目录
cd /d "%~dp0"

REM Python 解释器（项目指定版本）
set PY=C:\Python314\python.exe

REM 检查 python 是否存在
if not exist "%PY%" (
    echo [错误] 未找到 %PY%
    echo 请确认已安装 Python 3.14，或修改本脚本中的 PY 路径。
    pause
    exit /b 1
)

REM 检查依赖
"%PY%" -c "import streamlit" 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 streamlit，正在尝试安装依赖（需联网）...
    "%PY%" -m pip install -r requirements.txt
)

echo.
echo ============================================================
echo   正在启动「跨境内容合规引擎」演示服务 ...
echo   请稍候，浏览器将提示访问：
echo        http://localhost:8501
echo   若未自动打开，请手动复制上面的地址到浏览器。
echo   按 Ctrl+C 可停止服务。
echo ============================================================
echo.

REM 启动 Streamlit（headless 后台服务模式，自动弹出浏览器）
"%PY%" -m streamlit run app.py --server.headless false --browser.gatherUsageStats false

if errorlevel 1 (
    echo.
    echo [错误] 服务启动失败，请查看上方报错信息。
    echo 常见原因：端口 8501 被占用，或 .env 中缺少 HACKATHON_API_KEY。
    pause
)
endlocal
