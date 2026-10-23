# Cross-platform custom PyInstaller hook for Pecetracker.
#
# Keeps the PyArrow core used by Streamlit/Pandas for DataFrame serialisation
# while excluding optional Arrow feature families that this app does not use.

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


_EXCLUDED_MODULE_PREFIXES = (
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
)


def _excluded(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in _EXCLUDED_MODULE_PREFIXES
    )


hiddenimports = collect_submodules(
    "pyarrow",
    filter=lambda name: not _excluded(name),
    on_error="warn once",
)

excludedimports = list(_EXCLUDED_MODULE_PREFIXES)

# Do not ship pyarrow development headers/package data.
datas = []

# These markers work for both Unix .so files (libarrow_flight...) and
# Windows DLLs (arrow_flight..., parquet..., etc.).
_OPTIONAL_LIBRARY_MARKERS = (
    "arrow_flight",
    "arrow_dataset",
    "arrow_substrait",
    "arrow_cuda",
    "parquet",
    "gandiva",
)


def _keep_binary(item: tuple[str, str]) -> bool:
    source, _destination = item
    filename = Path(source).name.lower()
    return not any(marker in filename for marker in _OPTIONAL_LIBRARY_MARKERS)


binaries = [
    item
    for item in collect_dynamic_libs("pyarrow")
    if _keep_binary(item)
]
