from __future__ import annotations

import requests
from steam_reviews import get_review_summary
from version import USER_AGENT


APP_DETAILS_URL = (
    "https://store.steampowered.com/api/appdetails"
)

HEADERS = {
    "User-Agent": USER_AGENT
}


def _find_app_entry(payload, appid):
    """Return the response entry for the requested Steam AppID."""
    appid = int(appid)

    direct = payload.get(str(appid))

    if isinstance(direct, dict):
        if direct.get("success") is False:
            return None
        data = direct.get("data")
        if direct.get("success") is True and isinstance(data, dict):
            try:
                if int(data.get("steam_appid")) == appid:
                    return direct
            except (TypeError, ValueError) as error:
                raise ValueError("Steam Store returned an invalid AppID") from error

    for entry in payload.values():
        if not isinstance(entry, dict):
            continue

        if entry.get("success") is not True:
            continue

        data = entry.get("data")
        if not isinstance(data, dict):
            continue

        try:
            returned_appid = int(data.get("steam_appid"))
        except (TypeError, ValueError):
            continue

        if returned_appid == appid:
            return entry

    raise ValueError("Steam Store response has no valid entry for the requested AppID")


def _request_store_entry(appid, cc=None):
    params = {
        "appids": int(appid),
        "l": "english",
    }

    if cc:
        params["cc"] = cc

    response = requests.get(
        APP_DETAILS_URL,
        params=params,
        headers=HEADERS,
        timeout=15,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("Steam Store returned invalid JSON") from error

    if not isinstance(payload, dict):
        raise ValueError("Steam Store returned an invalid details payload")

    return _find_app_entry(
        payload,
        appid,
    )


def get_store_details(appid):
    appid = int(appid)

    # First try the runner's normal/default Steam market.
    app_data = _request_store_entry(
        appid
    )

    # If the title is unavailable in the runner's market, retry using
    # the US store so the global database does not depend on runner region.
    if app_data is None:
        app_data = _request_store_entry(
            appid,
            cc="us",
        )

    if app_data is None:
        return None

    data = app_data["data"]
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("Steam Store returned a missing/invalid name")
    for field in ("genres", "categories"):
        values = data.get(field, [])
        if not isinstance(values, list) or any(
                not isinstance(value, dict) or (value.get("description") is not None
                                                and not isinstance(value["description"], str))
                for value in values):
            raise ValueError(f"Steam Store returned invalid {field}")
    for field in ("developers", "publishers"):
        values = data.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"Steam Store returned invalid {field}")
    release = data.get("release_date")
    if release is not None and (not isinstance(release, dict)
                               or (release.get("date") is not None and not isinstance(release["date"], str))):
        raise ValueError("Steam Store returned an invalid release_date")
    if "is_free" in data and not isinstance(data["is_free"], bool):
        raise ValueError("Steam Store returned an invalid is_free flag")

    genres = [
        genre.get("description")
        for genre in data.get("genres", [])
        if genre.get("description")
    ]

    categories = [
        category.get("description")
        for category in data.get("categories", [])
        if category.get("description")
    ]

    return {
        "appid": appid,
        "name": data.get("name"),
        "type": data.get("type"),
        "genres": genres,
        "categories": categories,
        "developers": data.get(
            "developers",
            [],
        ),
        "publishers": data.get(
            "publishers",
            [],
        ),
        "release_date": (
            data.get("release_date") or {}
        ).get("date"),
        "is_free": bool(
            data.get("is_free", False)
        ),
    }


def get_game_metadata(appid):
    store_details = get_store_details(
        appid
    )

    if store_details is None:
        return None

    reviews = get_review_summary(
        appid
    )

    return {
        **store_details,
        "reviews": reviews,
    }
