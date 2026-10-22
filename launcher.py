from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import secrets

import psutil
from app_logging import configure_logging, get_logger
from pathlib import Path

from streamlit import config as streamlit_config
from streamlit.web import bootstrap

from app_paths import DATA_DIR, ensure_data_dir
from version import APP_VERSION


HOST = "localhost"
FIRST_PORT = 8501
LAST_PORT = 8599

INSTANCE_STATE_PATH = DATA_DIR / "instance.json"
INSTANCE_LOCK_PATH = DATA_DIR / "instance.lock"
_instance_lock_file = None
_instance_token = None
_log = get_logger(__name__)


def get_bundle_dir() -> Path:
    """Return the directory containing bundled application resources."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)

    return Path(__file__).resolve().parent


def pid_is_running(pid: int) -> bool:
    return pid > 0 and psutil.pid_exists(pid)


def process_identity(pid):
    try:
        process = psutil.Process(pid)
        return {"created_at": process.create_time(), "executable": process.exe(),
                "command": process.cmdline()}
    except (psutil.Error, OSError):
        return None


def identity_matches(pid, expected):
    if not isinstance(expected, dict) or not expected.get("command"):
        return False
    actual = process_identity(pid)
    return actual is not None and actual == expected


def _lock_file(file):
    file.seek(0)
    if os.name == "nt":
        import msvcrt
        # Windows byte-range locks require an existing byte.
        if not file.read(1):
            file.write(b" "); file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_instance_lock():
    global _instance_lock_file
    if _instance_lock_file is not None:
        _instance_lock_file.close()
        _instance_lock_file = None
    # Never unlink the lock inode: another process may already have opened it.


def _instance_record(pid):
    return {"pid": pid, "version": APP_VERSION, "protocol": 2,
            "identity": process_identity(pid), "instance_token": _instance_token}


def find_free_port() -> int:
    """Return the first available local port in the configured range."""
    for port in range(FIRST_PORT, LAST_PORT + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue

    raise RuntimeError(
        f"No free port available between {FIRST_PORT} and {LAST_PORT}."
    )


def app_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def health_url(port: int) -> str:
    return f"{app_url(port)}/_stcore/health"


def server_is_healthy(port: int, timeout: float = 0.75) -> bool:
    """Return True if Streamlit is responding on the recorded port."""
    try:
        with urllib.request.urlopen(
            health_url(port),
            timeout=timeout,
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


def write_instance_state(pid: int, port: int) -> None:
    """Atomically persist the current instance PID and port."""
    ensure_data_dir()

    temporary_path = INSTANCE_STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({**_instance_record(pid), "port": port}, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(INSTANCE_STATE_PATH)


def remove_if_owned(path: Path, pid: int) -> None:
    """Remove a state/lock file only when it still belongs to this PID."""
    data = read_json(path)

    if path == INSTANCE_LOCK_PATH or not data or data.get("pid") != pid:
        return
    if data.get("instance_token") != _instance_token:
        return

    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_existing_instance() -> tuple[int, int, str | None] | None:
    """Return (pid, port, version) for a valid existing app instance."""
    state = read_json(INSTANCE_STATE_PATH)
    if not state:
        return None

    try:
        pid = int(state["pid"])
        port = int(state["port"])
    except (KeyError, TypeError, ValueError):
        return None

    if not pid_is_running(pid):
        return None

    if not (FIRST_PORT <= port <= LAST_PORT):
        return None

    version = state.get("version")
    if version is not None:
        version = str(version)

    return pid, port, version



def parse_version(version: str | None) -> tuple[int, ...] | None:
    """Parse simple dotted numeric application versions such as 1.3.0."""
    if not version:
        return None

    value = str(version).strip().lstrip("vV")
    parts = value.split(".")

    if not parts or not all(part.isdigit() for part in parts):
        return None

    return tuple(int(part) for part in parts)


def compare_versions(
    current_version: str,
    existing_version: str | None,
) -> int:
    """
    Compare the launcher version with the running instance.

    Returns:
        1  -> current launcher is newer
        0  -> same version
        -1 -> existing instance is newer

    Missing versions sort as older, but replacement still requires the current
    ownership protocol and a verified process identity.
    """
    if existing_version == current_version:
        return 0

    current = parse_version(current_version)
    existing = parse_version(existing_version)

    if existing is None:
        return 1

    if current is None:
        # Exact equality was handled above. For an unknown format, prefer the
        # launcher the user explicitly started rather than getting stuck on
        # another build forever.
        return 1

    width = max(len(current), len(existing))
    current += (0,) * (width - len(current))
    existing += (0,) * (width - len(existing))

    if current > existing:
        return 1

    if current < existing:
        return -1

    return 0


def terminate_process(pid: int, identity=None, force=False) -> None:
    """Signal only an authenticated process through a stable kernel handle.

    Legacy files lack identity and are deliberately never used to kill a PID.
    """
    if pid <= 0 or pid == os.getpid() or not identity_matches(pid, identity):
        raise RuntimeError("Cannot verify the existing instance. Close it manually before starting SLT.")
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000 | 0x0001, False, pid)
        if not handle:
            raise RuntimeError("The existing instance could not be opened safely.")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise RuntimeError("Cannot verify the existing process handle.")
            created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10_000_000 - 11644473600
            if abs(created - identity["created_at"]) > .001:
                raise RuntimeError("The existing process identity changed.")
            if not kernel.TerminateProcess(handle, 0):
                raise RuntimeError("The verified instance could not be stopped.")
        finally:
            kernel.CloseHandle(handle)
        return
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Safe process replacement is unavailable. Close the existing instance manually.")
    fd = os.pidfd_open(pid)
    try:
        # Check again AFTER opening the stable process handle, before signalling.
        if not identity_matches(pid, identity):
            raise RuntimeError("The existing process identity changed.")
        signal.pidfd_send_signal(fd, signal.SIGKILL if force else signal.SIGTERM)
    finally:
        os.close(fd)


def wait_for_process_exit(
    pid: int,
    timeout: float = 5.0,
) -> bool:
    """Wait briefly for a process to terminate."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return True

        time.sleep(0.1)

    return not pid_is_running(pid)


def stop_older_instance(pid: int, identity=None) -> None:
    """Stop an older app version before launching the current version."""
    terminate_process(pid, identity)

    if wait_for_process_exit(pid):
        return

    if os.name != "nt":
        try:
            terminate_process(pid, identity, force=True)
        except ProcessLookupError:
            return

        if wait_for_process_exit(pid, timeout=2.0):
            return

    raise RuntimeError(
        "An older Steam Library Tracker instance could not be closed. "
        "Close it manually and start the new version again."
    )


def remove_stale_instance_files(pid: int) -> None:
    """Remove old instance files after their owning process has stopped."""
    if pid_is_running(pid):
        return

    remove_if_owned(INSTANCE_STATE_PATH, pid)
    remove_if_owned(INSTANCE_LOCK_PATH, pid)


def should_replace_existing_version(
    existing_version: str | None,
) -> bool:
    """Return True when this launcher should replace the running instance."""
    return compare_versions(APP_VERSION, existing_version) > 0

def reopen_existing_instance(
    wait_seconds: float = 0.0,
) -> bool:
    """
    Reopen a compatible running Steam Library Tracker instance.

    If the running instance is older than this launcher, stop it so the new
    version can start. If the running instance is newer, keep and reopen it.
    """
    deadline = time.monotonic() + wait_seconds

    while True:
        existing = read_existing_instance()

        if existing is not None:
            pid, port, existing_version = existing
            state = read_json(INSTANCE_STATE_PATH) or {}
            if state.get("protocol") != 2:
                raise RuntimeError("A legacy SLT instance may be running. Close it manually before starting this version.")
            relation = compare_versions(
                APP_VERSION,
                existing_version,
            )

            if relation > 0:
                stop_older_instance(pid, state.get("identity"))
                remove_stale_instance_files(pid)
                return False

            if state.get("protocol") == 2 and not identity_matches(pid, state.get("identity")):
                return False
            if server_is_healthy(port):
                webbrowser.open(app_url(port))
                return True

        if time.monotonic() >= deadline:
            return False

        time.sleep(0.15)


def read_lock_state() -> tuple[int, str | None] | None:
    """Return (pid, version) from the launcher lock file."""
    lock = read_json(INSTANCE_LOCK_PATH)
    if not lock:
        return None

    try:
        pid = int(lock["pid"])
    except (KeyError, TypeError, ValueError):
        return None

    version = lock.get("version")
    if version is not None:
        version = str(version)

    return pid, version


def acquire_instance_lock() -> int:
    """Hold an OS lock for the entire server lifetime, including JSON writes."""
    global _instance_lock_file, _instance_token
    ensure_data_dir()
    own_pid = os.getpid()
    while True:
        file = os.fdopen(os.open(INSTANCE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600), "r+b")
        try:
            _lock_file(file)
        except OSError:
            file.close()
            if reopen_existing_instance(wait_seconds=5.0):
                return 0
            # A verified older process may have just exited. Retry the OS lock.
            existing = read_json(INSTANCE_STATE_PATH) or {}
            if pid_is_running(int(existing.get("pid") or 0)):
                raise RuntimeError("SLT is already starting. Please wait and try again.")
            # Do not spin forever on a lock owned by an unknown process.
            file = os.fdopen(os.open(INSTANCE_LOCK_PATH, os.O_RDWR), "r+b")
            try:
                _lock_file(file)
            except OSError:
                file.close()
                raise RuntimeError("Another launcher owns the instance lock. Close it before starting SLT.")
        try:
            previous = read_json(INSTANCE_LOCK_PATH) or {}
            pid = int(previous.get("pid") or 0)
            # An old launcher did not hold an OS lock. Do not start alongside it
            # or infer ownership from its PID. Legacy instances need manual exit.
            if pid != own_pid and pid_is_running(pid) and previous.get("protocol") != 2:
                raise RuntimeError("A legacy SLT instance may be running. Close it manually before starting this version.")
            _instance_token = secrets.token_hex(16)
            file.seek(0); file.truncate()
            file.write(json.dumps(_instance_record(own_pid)).encode("utf-8")); file.flush()
            _instance_lock_file = file
            return own_pid
        except BaseException:
            file.close()
            raise


def main() -> None:
    configure_logging()
    ensure_data_dir()

    # Fast path: an existing healthy instance is already running.
    if reopen_existing_instance():
        return

    own_pid = acquire_instance_lock()
    if own_pid == 0:
        return

    try:
        app_path = get_bundle_dir() / "app.py"

        if not app_path.exists():
            remove_if_owned(INSTANCE_LOCK_PATH, own_pid)
            raise FileNotFoundError(
                f"Could not find bundled app.py at: {app_path}"
            )

        port = find_free_port()
        write_instance_state(own_pid, port)

        flag_options = {
            "global_developmentMode": False,
            "server_address": HOST,
            "server_port": port,
            "server_headless": False,
            "server_fileWatcherType": "none",
            "browser_serverAddress": HOST,
            "browser_serverPort": port,
            "browser_gatherUsageStats": False,
            "client_toolbarMode": "viewer",
            "logger_hideWelcomeMessage": True,
        }

        streamlit_config._main_script_path = str(app_path)
        bootstrap.load_config_options(flag_options)

        bootstrap.run(
            str(app_path),
            False,
            [],
            flag_options,
        )
    finally:
        remove_if_owned(INSTANCE_STATE_PATH, own_pid)
        release_instance_lock()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log.exception("Launcher failed")
        raise
