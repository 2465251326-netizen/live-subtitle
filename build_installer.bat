@echo off
chcp 65001 >nul
echo ==================================================
echo   LiveSubtitle - EXE 安装程序一键构建
echo   输出: dist\installer\LiveSubtitle-Setup-版本号.exe（版本号见 setup.iss）
echo ==================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或 3.11 并勾选 Add to PATH
    pause
    exit /b 1
)

echo [1/5] 创建构建虚拟环境 build_env ...
if not exist build_env (
    python -m venv build_env
)
call build_env\Scripts\activate.bat

echo [2/5] 安装依赖（首次约 5-10 分钟）...
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
)
pip install pyinstaller -q

echo [3/5] PyInstaller 打包 ...
pyinstaller livesubtitle.spec --noconfirm
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败
    pause
    exit /b 1
)

echo [4/5] 查找 Inno Setup 编译器 ...
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe

if "%ISCC%"=="" (
    echo   未安装 Inno Setup，尝试通过 winget 自动安装...
    winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe
)

if "%ISCC%"=="" (
    echo [提示] 未找到 Inno Setup，无法生成安装程序。
    echo   请手动安装: winget install JRSoftware.InnoSetup
    echo   或访问 https://jrsoftware.org/isdl.php 下载
    echo   然后重新运行本脚本。
    echo.
    echo   绿色版已可用: dist\LiveSubtitle\LiveSubtitle.exe
    pause
    exit /b 0
)

echo [5/5] 生成安装程序 ...
"%ISCC%" setup.iss
if errorlevel 1 (
    echo [错误] Inno Setup 编译失败
    pause
    exit /b 1
)

echo.
echo ==================================================
echo   完成！安装程序: dist\installer\LiveSubtitle-Setup-版本号.exe（版本号见 setup.iss）
echo   绿色版程序:     dist\LiveSubtitle\LiveSubtitle.exe
echo ==================================================
pause
