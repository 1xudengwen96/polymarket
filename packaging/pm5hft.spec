# -*- mode: python ; coding: utf-8 -*-
"""pm5hft 单 exe 打包（PyInstaller onefile）。

用法:
  pyinstaller packaging/pm5hft.spec --noconfirm --distpath dist --workpath build

产物: dist/pm5hft(.exe)
  pm5hft.exe               启动机器人（默认 paper）
  pm5hft.exe dashboard     启动监控面板

config/、artifacts/、.env 不打包进 exe，而是放在 exe 同目录（用户可改）。
"""
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    # 惰性导入（PyInstaller 静态分析看不到）
    collect_submodules("polymarket")
    + collect_submodules("psycopg")
    + [
        "psycopg",
        "psycopg.binary",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        "sqlalchemy.dialects.postgresql.asyncpg",
        "aiosqlite",
        "asyncpg",
        "aiohttp",
        "websockets",
        "yaml",
        "structlog",
        "pydantic_settings",
    ]
)

a = Analysis(
    ["entry.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 回测/研究专用重型依赖不打包进机器人 exe
    excludes=[
        "numpy", "pandas", "lightgbm", "sklearn", "matplotlib",
        "IPython", "notebook", "jupyter", "pytest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pm5hft",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 控制台程序：机器人/面板日志可见
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
