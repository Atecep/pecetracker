$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "== Pecetracker: Windows build =="

function Assert-CommandSuccess {
    param(
        [string]$Message
    )

    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host $Message
        exit $LASTEXITCODE
    }
}

python -c "import PyInstaller" | Out-Null
Assert-CommandSuccess "PyInstaller is not installed in the active Python environment. Run: python -m pip install -r requirements-build.txt"

python -c "import streamlit, pandas, altair, requests, pyarrow, psutil" | Out-Null
Assert-CommandSuccess "One or more application dependencies are missing. Run: python -m pip install -r requirements-build.txt"

Write-Host ("Python:      " + (python --version))
Write-Host ("PyInstaller: " + (python -c "import PyInstaller; print(PyInstaller.__version__)"))
Write-Host ("Streamlit:   " + (python -c "import streamlit; print(streamlit.__version__)"))
Write-Host ""

python -m PyInstaller `
    --clean `
    --noconfirm `
    --workpath build-windows `
    --distpath dist-windows `
    Pecetracker.windows.spec

Assert-CommandSuccess "PyInstaller build failed."

$archiveName = python -c "from version import distribution_archive_name; print(distribution_archive_name())"
Assert-CommandSuccess "Could not determine the archive name."
Compress-Archive -Path "dist-windows\Pecetracker" -DestinationPath "dist-windows\$archiveName" -Force

Write-Host ""
Write-Host "Build complete."
Write-Host ""
Write-Host "Run it with:"
Write-Host "  .\dist-windows\Pecetracker\Pecetracker.exe"
Write-Host "Archive: dist-windows\$archiveName (local artifact only; nothing published)"
