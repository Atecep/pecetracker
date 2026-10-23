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
        data = direct.get("data") or {}

        if (
            direct.get("success")
            and int(data.get("steam_appid", appid)) == appid
        ):
            return direct

    for entry in payload.values():
        if not isinstance(entry, dict):
            continue

        if not entry.get("success"):
            continue

        data = entry.get("data") or {}

        try:
            returned_appid = int(data.get("steam_appid"))
        except (TypeError, ValueError):
            continue

        if returned_appid == appid:
            return entry

    return None


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

    payload = response.json()

    if not isinstance(payload, dict):
        return None

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

    data = app_data.get("data") or {}

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
