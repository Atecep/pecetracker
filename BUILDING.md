# Building Pecetracker

This document covers running Pecetracker from source and producing standalone Windows and Linux builds.

**Your Steam Library, Tamed.** — A Steam library and backlog manager.
Repository: https://github.com/Atecep/pecetracker

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
dist/Pecetracker/
```

Run it with:

```bash
./dist/Pecetracker/Pecetracker
```

## 5. Local distribution archive

The build script also creates `dist/Pecetracker-v1.5.0-linux-x86_64.tar.xz`
(the architecture follows the build host). It does not publish anything.
To recreate it manually:

From the project root:

```bash
cd dist
tar -cJf Pecetracker-v1.5.0-linux-x86_64.tar.xz Pecetracker
```

## Testing a freshly rebuilt Linux bundle

Pecetracker is single-instance.

If an older development or packaged instance is still running, launching a freshly rebuilt copy may reopen the existing process instead of starting the new build.

Close the specific running app normally before testing the new build. For an
isolated smoke, set `HOME`, `XDG_DATA_HOME` and `XDG_CONFIG_HOME` to a temporary
directory before starting it. Do not use broad process-name termination commands.

You can confirm the usual Streamlit ports are free with:

```bash
ss -ltnp | grep -E ':850[1-9]'
```

The launcher holds an operating-system lock for its lifetime. Keep the legacy
`SteamLibraryTracker/instance.lock` and `instance.json` paths; removing a live lock
breaks ownership. Native locks are released on process exit; no manual cleanup
or personal-data migration is part of the rebranding.

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
dist-windows\Pecetracker\
```

Run it with:

```powershell
.\dist-windows\Pecetracker\Pecetracker.exe
```

The Windows build is configured with `console=False`, so the final executable should open without a terminal window.

## 7. Local distribution archive

The PowerShell script also creates the following archive (architecture follows
the build host), retaining the top-level `Pecetracker` directory:

Compress:

```text
dist-windows\Pecetracker\
```

into:

```text
Pecetracker-v1.5.0-windows-x86_64.zip
```

---

# Build files

The standalone builds use these project files:

```text
Pecetracker.spec
Pecetracker.windows.spec
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

These historical paths are a compatibility contract and remain the defaults for
new installations too. `library.db`, its schema, settings/backup, `slt.log` and its
rotations, instance filenames/protocol/ownership records, and bootstrap state keys
are unchanged. Versions sharing the same data home share the same instance lock.

---

# Versioning

The application version is defined in:

```text
version.py
```

For example:

```python
APP_NAME = "Pecetracker"
APP_VERSION = "1.5.0"
GITHUB_REPOSITORY = "Atecep/pecetracker"
```

Release tags should use the corresponding `v` prefix:

```text
v1.0.0
v1.0.1
v1.1.0
```

APP_VERSION remains 1.5.0 during rebranding validation. The future 1.6.0 release is
separate work. The scripts create local artifacts only; do not upload/rename old
releases or assets. Keep `global-db` and all `global_metadata.db*` filenames stable.

The update checker compares versions using the new repository. Old installed
builds continue through GitHub's repository-rename redirects, so the old repository
name must not be reused. A rebranding-only build at 1.5.0 is not advertised as an
upgrade to an installed 1.5.0 build; compare behaviour using an isolated data home
or close the existing instance normally.

## Regression tests

Run the offline suite using the installed build environment:

```bash
python -B tests/run_tests.py
```

The runner redirects application data/configuration paths to temporary directories before importing Pecetracker and blocks real HTTP. Tests cover SQLite migration/rollback and concurrent writers, launcher ownership, settings recovery, workers, caches, HLTB matching, personal edits and real Streamlit widget behavior. GitHub Actions runs the same suite on Linux and Windows; a workflow definition alone is not evidence of a completed Windows run.

An optional Linux rendered smoke uses an installed Chromium-family browser, a private temporary browser profile, temporary SQLite and simulated metadata:

```bash
python -B tests/linux_browser_smoke.py --browser /path/to/chromium --output /tmp/slt-browser-validation
```

It checks literal search, game details, the Linux ProtonDB dialog/back navigation and explicit note saving, and writes screenshots/logs to the output directory. It does not validate a frozen executable or live metadata services.

After building on Linux, a separate smoke exercises the actual frozen executable
and renders its onboarding page in the browser:

```bash
python -B tests/linux_bundle_smoke.py \
  --executable dist/Pecetracker/Pecetracker \
  --browser /path/to/chromium \
  --output /tmp/pecetracker-bundle-validation
```

It uses a temporary legacy data directory, blocks external metadata requests,
checks SQLite/config hashes and a manual HLTB ID, and verifies that a second
launch reopens the authenticated owner without replacing its native lock inode.
Linux build/runtime evidence does not validate a Windows binary or portability
to Linux distributions with an older glibc than the build host.
