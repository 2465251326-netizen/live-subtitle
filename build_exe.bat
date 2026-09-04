@echo off
chcp 65001 >nul
echo ============================================
echo   LiveSubtitle - Windows 一键打包
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或 3.11 并勾选 Add to PATH
    pause
    exit /b 1
)

echo [1/4] 创建构建虚拟环境 build_env ...
if not exist build_env (
    python -m venv build_env
)
call build_env\Scripts\activate.bat

echo [2/4] 安装依赖（首次约 5-10 分钟）...
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
)
pip install pyinstaller -q

echo [3/4] PyInstaller 打包 ...
pyinstaller livesubtitle.spec --noconfirm
if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo [4/4] 完成！
echo   输出目录: dist\LiveSubtitle\
echo   主程序:   dist\LiveSubtitle\LiveSubtitle.exe
echo   首次运行请先在界面里选择识别模型，会自动下载
echo.
echo   可选：如需离线翻译引擎（Argos），另执行
echo     build_env\Scripts\pip install -r requirements-offline.txt
echo   然后重新打包
pause
