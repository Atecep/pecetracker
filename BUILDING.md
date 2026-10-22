# Building Steam Library Tracker

This document covers running Steam Library Tracker from source and producing standalone Windows and Linux builds.

## Requirements

The current release build has been tested with:

- Python 3.14
- PyInstaller 6.22.3
- Streamlit 1.64.0

Python dependencies are pinned in:

```text
requirements-build.txt
```

The build environment is intentionally kept separate from the application source.

---

## Running from source

Install the dependencies in a virtual environment and run:

```text
python launcher.py
```

Do **not** run the launcher with:

```text
streamlit run launcher.py
```

The launcher starts Streamlit locally, manages the local port, opens the app in the default web browser and enforces single-instance behaviour.

---

# Linux

## 1. Create a build environment

From the project root:

```bash
python3 -m venv .venv-build
```

### Bash / Zsh

```bash
source .venv-build/bin/activate
```

### Fish

```fish
source .venv-build/bin/activate.fish
```

Confirm the active interpreter:

```bash
which python
python --version
```

## 2. Install dependencies

```bash
python -m pip install -U pip
python -m pip install -r requirements-build.txt
```


## 3. Run from source

```bash
python launcher.py
```

## 4. Build the standalone application

```bash
chmod +x build_linux.sh
./build_linux.sh
```

The result is created in:

```text
dist/SteamLibraryTracker/
```

Run it with:

```bash
./dist/SteamLibraryTracker/SteamLibraryTracker
```

## 5. Create the release archive

From the project root:

```bash
cd dist
tar -cJf SteamLibraryTracker-v1.2.0-linux-x86_64.tar.xz SteamLibraryTracker
```

## Testing a freshly rebuilt Linux bundle

Steam Library Tracker is single-instance.

If an older development or packaged instance is still running, launching a freshly rebuilt copy may reopen the existing process instead of starting the new build.

Before testing a new build from a clean state:

```bash
pkill -f SteamLibraryTracker
pkill -f streamlit
```

You can confirm the usual Streamlit ports are free with:

```bash
ss -ltnp | grep -E ':850[1-9]'
```

Close all SLT processes before removing stale instance state files. The current launcher holds an operating-system lock for its lifetime; deleting `instance.lock` while it is running breaks that ownership protocol.

```bash
rm -f ~/.local/share/SteamLibraryTracker/instance.json
rm -f ~/.local/share/SteamLibraryTracker/instance.lock
```

Do not remove `config.json` or `library.db` unless you intentionally want to reset the application's local data.

Older launchers without process-identity records must be closed manually before upgrading. The new launcher never terminates a process based only on a recorded PID.

---

# Windows

## 1. Create a build environment

From PowerShell in the project root:

```powershell
py -3.14 -m venv --without-scm-ignore-files .venv-build
```

The `--without-scm-ignore-files` option avoids creating Git ignore files inside the virtual environment.

## 2. Allow local scripts for the current PowerShell session

If PowerShell blocks activation or build scripts:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

This affects only the current PowerShell session.

## 3. Activate the environment

```powershell
.\.venv-build\Scripts\Activate.ps1
```

Confirm the interpreter:

```powershell
python --version
where.exe python
```

The first `python` path should point inside:

```text
.venv-build\Scripts\
```

## 4. Install dependencies

```powershell
python -m pip install -U pip
python -m pip install -r requirements-build.txt
```


Confirm PyInstaller is available:

```powershell
python -m PyInstaller --version
```

## 5. Run from source

```powershell
python launcher.py
```

## 6. Build the standalone application

```powershell
.\build_windows.ps1
```

The result is created in:

```text
dist-windows\SteamLibraryTracker\
```

Run it with:

```powershell
.\dist-windows\SteamLibraryTracker\SteamLibraryTracker.exe
```

The Windows build is configured with `console=False`, so the final executable should open without a terminal window.

## 7. Create the release archive

Compress:

```text
dist-windows\SteamLibraryTracker\
```

into:

```text
SteamLibraryTracker-v1.2.0-windows-x86_64.zip
```

---

# Build files

The standalone builds use these project files:

```text
SteamLibraryTracker.spec
SteamLibraryTracker.windows.spec
build_linux.sh
build_windows.ps1
requirements-build.txt
hooks/hook-pyarrow.py
```

The custom PyArrow hook reduces unnecessary bundled components while retaining the Arrow functionality required by Streamlit and Pandas.

---

# Application data

Packaged builds store user data outside the application directory.

## Linux

```text
~/.local/share/SteamLibraryTracker/
```

## Windows

```text
%LOCALAPPDATA%\SteamLibraryTracker\
```

This allows the application folder to be replaced during an update without overwriting user settings, notes or statuses.

---

# Versioning

The application version is defined in:

```text
version.py
```

For example:

```python
APP_NAME = "Steam Library Tracker"
APP_VERSION = "1.1.0"
GITHUB_REPOSITORY = "Atecep/steam-library-tracker"
```

Release tags should use the corresponding `v` prefix:

```text
v1.0.0
v1.0.1
v1.1.0
```

The update checker compares the installed application version with the latest published GitHub Release.

## Regression tests

Run the offline suite using the installed build environment:

```bash
python -B tests/run_tests.py
```

The runner redirects application data/configuration paths to temporary directories before importing SLT and blocks real HTTP. Tests cover SQLite migration/rollback and concurrent writers, launcher ownership, settings recovery, workers, caches, HLTB matching, personal edits and real Streamlit widget behavior. GitHub Actions runs the same suite on Linux and Windows; a workflow definition alone is not evidence of a completed Windows run.

An optional Linux rendered smoke uses an installed Chromium-family browser, a private temporary browser profile, temporary SQLite and simulated metadata:

```bash
python -B tests/linux_browser_smoke.py --browser /path/to/chromium --output /tmp/slt-browser-validation
```

It checks literal search, game details, the Linux ProtonDB dialog/back navigation and explicit note saving, and writes screenshots/logs to the output directory. It does not validate a frozen executable or live metadata services.
