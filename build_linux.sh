#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "== Pecetracker: Linux build =="

if ! python -c "import PyInstaller" >/dev/null 2>&1; then
    echo
    echo "PyInstaller is not installed in the active Python environment."
    echo "Install it with:"
    echo "  python -m pip install -r requirements-build.txt"
    exit 1
fi

if ! python -c "import streamlit, pandas, altair, requests, pyarrow, psutil" >/dev/null 2>&1; then
    echo
    echo "One or more application dependencies are missing from the active environment."
    echo "Install them with:"
    echo "  python -m pip install -r requirements-build.txt"
    exit 1
fi

echo "Python:      $(python --version 2>&1)"
echo "PyInstaller: $(python -c 'import PyInstaller; print(PyInstaller.__version__)')"
echo "Streamlit:   $(python -c 'import streamlit; print(streamlit.__version__)')"
echo

python -m PyInstaller \
    --clean \
    --noconfirm \
    Pecetracker.spec

archive_name="$(python -c 'from version import distribution_archive_name; print(distribution_archive_name())')"
tar -cJf "dist/$archive_name" -C dist Pecetracker

echo
echo "Build complete."
echo
echo "Run it with:"
echo "  ./dist/Pecetracker/Pecetracker"
echo "Archive: dist/$archive_name (local artifact only; nothing published)"
