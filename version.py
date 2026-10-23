"""Public identity and stable compatibility identifiers.

Changing the product name must never change the personal data/instance paths.
"""
APP_NAME = "Pecetracker"
APP_TAGLINE = "Your Steam Library, Tamed."
APP_DESCRIPTION = "A Steam library and backlog manager."
APP_VERSION = "1.5.0"
GITHUB_REPOSITORY = "Atecep/pecetracker"
GITHUB_URL = f"https://github.com/{GITHUB_REPOSITORY}"
GITHUB_RELEASES_URL = f"{GITHUB_URL}/releases"
GITHUB_API_BASE = "https://api.github.com/repos"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
REVIEW_SCORE_LABEL = f"{APP_NAME} Review Score"

# On-disk identities are deliberately unchanged, including for new installs.
LEGACY_DATA_DIR_NAME = "SteamLibraryTracker"
GLOBAL_RELEASE_TAG = "global-db"


def distribution_archive_name(system=None, machine=None):
    """Name local distribution artifacts without changing published assets."""
    import platform
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if system not in ("linux", "windows"):
        raise ValueError(f"Unsupported distribution platform: {system}")
    extension = "zip" if system == "windows" else "tar.xz"
    return f"{APP_NAME}-v{APP_VERSION}-{system}-{machine}.{extension}"
