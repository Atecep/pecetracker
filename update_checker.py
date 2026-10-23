from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from version import APP_NAME, APP_VERSION, GITHUB_REPOSITORY, GITHUB_API_BASE


_VERSION_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)$"
)


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str


def _parse_version(version: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.fullmatch(
        version.strip()
    )

    if match is None:
        return None

    return tuple(
        int(part)
        for part in match.groups()
    )


def check_for_updates(
    repository: str = GITHUB_REPOSITORY,
    current_version: str = APP_VERSION,
    timeout: float = 3.0,
) -> UpdateInfo | None:
    """Return update information when a newer GitHub release exists.

    Any network/API/parsing failure is treated as "no update information" so
    an unavailable update service never prevents the application from opening.
    """

    repository = repository.strip().strip("/")

    if not repository or "/" not in repository:
        return None

    current = _parse_version(current_version)

    if current is None:
        return None

    api_url = (
        f"{GITHUB_API_BASE}/{repository}/releases/latest"
    )

    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": f"{APP_NAME.replace(' ', '-')}/{current_version}",
        },
    )

    try:
        with urlopen(
            request,
            timeout=timeout,
        ) as response:
            payload = json.load(response)

    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
    ):
        return None

    tag_name = str(
        payload.get("tag_name", "")
    ).strip()

    release_url = str(
        payload.get("html_url", "")
    ).strip()

    latest = _parse_version(tag_name)

    if (
        latest is None
        or latest <= current
        or not release_url
    ):
        return None

    latest_version = tag_name.removeprefix("v")

    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
    )
