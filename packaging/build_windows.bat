@echo off
rem ============================================================
rem pm5hft Windows exe 构建脚本（必须在 Windows 上运行）
rem 用法（在项目根目录）:
rem   packaging\build_windows.bat
rem 产物: dist\pm5hft.exe
rem ============================================================
cd /d "%~dp0.."

echo [1/4] 创建虚拟环境...
python -m venv .venv-win
call .venv-win\Scripts\activate.bat

echo [2/4] 安装依赖 + PyInstaller...
python -m pip install --upgrade pip
pip install -e . pyinstaller

echo [3/4] 打包（onefile）...
pyinstaller packaging\pm5hft.spec --noconfirm --distpath dist --workpath build

echo [4/4] 组装交付目录 dist\...
xcopy /E /I /Y config dist\config
xcopy /E /I /Y artifacts dist\artifacts
copy /Y .env.example dist\ 2>nul
copy /Y README.md dist\ 2>nul

echo.
echo 完成！产物在 dist\pm5hft.exe（把 config、artifacts 和它放同一文件夹）
echo 运行: 双击 pm5hft.exe 启动机器人;  pm5hft.exe dashboard 启动监控面板
pause
