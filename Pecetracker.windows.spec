# -*- mode: python ; coding: utf-8 -*-

from version import APP_NAME

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

streamlit_datas = collect_data_files("streamlit")
streamlit_hiddenimports = collect_submodules("streamlit")
streamlit_metadata = copy_metadata("streamlit")

altair_datas = collect_data_files("altair")
altair_hiddenimports = collect_submodules("altair")
altair_metadata = copy_metadata("altair")

project_hiddenimports = [
    "app_logging",
    "app_paths",
    "constants",
    "database",
    "global_bootstrap",
    "hltb_service",
    "hltb_background",
    "metadata_service",
    "metadata_background",
    "protondb_service",
    "settings",
    "smart_pick",
    "steam_api",
    "steam_cache",
    "steam_store",
    "steam_reviews",
    "update_checker",
    "version",
    "ui",
    "ui.account",
    "ui.common",
    "ui.game_dialog",
    "ui.library",
    "ui.smart_pick_dialog",
]

pyarrow_excludes = [
    "pyarrow.tests",
    "pyarrow.flight",
    "pyarrow._flight",
    "pyarrow.parquet",
    "pyarrow._parquet",
    "pyarrow._parquet_encryption",
    "pyarrow.dataset",
    "pyarrow._dataset",
    "pyarrow._dataset_orc",
    "pyarrow._dataset_parquet",
    "pyarrow.substrait",
    "pyarrow._substrait",
    "pyarrow.orc",
    "pyarrow._orc",
    "pyarrow.cuda",
    "pyarrow._cuda",
    "pyarrow.gandiva",
]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("app.py", "."),
        *streamlit_datas,
        *streamlit_metadata,
        *altair_datas,
        *altair_metadata,
    ],
    hiddenimports=[
        *project_hiddenimports,
        *streamlit_hiddenimports,
        *altair_hiddenimports,
    ],
    hookspath=["hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=pyarrow_excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
