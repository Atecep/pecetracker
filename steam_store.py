import requests
from steam_reviews import get_review_summary
from version import USER_AGENT


APP_DETAILS_URL = (
    "https://store.steampowered.com/api/appdetails"
)

HEADERS = {
    "User-Agent": USER_AGENT
}

REGIONAL_FALLBACK_COUNTRY = "us"


class SteamStoreMetadataUnavailable(Exception):
    """Steam returned valid appdetails responses with success=false."""

    def __init__(self, appid):
        super().__init__(
            f"Steam Store metadata is unavailable for AppID {int(appid)}"
        )
        self.appid = int(appid)


def _request_store_details(appid, country_code=None):
    """Return the raw appdetails entry for one Store region."""
    params = {
        "appids": appid,
        "l": "english",
    }

    if country_code:
        params["cc"] = country_code

    response = requests.get(
        APP_DETAILS_URL,
        params=params,
        headers=HEADERS,
        timeout=15,
    )

    response.raise_for_status()

    payload = response.json()
    return payload.get(str(appid), {})


def get_store_details(appid):
    """
    Fetches information from the Steam Store page.

    If the default Store region returns success=false, retry once against
    the US storefront. This recovers general catalogue metadata for games
    whose Store page is unavailable in the user's current region.

    Only general game metadata is consumed by this app; regional pricing,
    currency, discounts and purchase availability are intentionally ignored.
    """

    app_data = _request_store_details(appid)

    if not app_data.get("success"):
        app_data = _request_store_details(
            appid,
            country_code=REGIONAL_FALLBACK_COUNTRY,
        )

    if not app_data.get("success"):
        # This is distinct from a timeout, 429, or other HTTP/network
        # failure. Metadata is considered unavailable only after both the
        # normal Store request and the single regional fallback fail.
        raise SteamStoreMetadataUnavailable(appid)

    data = app_data.get(
        "data",
        {}
    )

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

        "name": data.get(
            "name"
        ),

        "type": data.get(
            "type"
        ),

        "genres": genres,

        "categories": categories,

        "developers": data.get(
            "developers",
            []
        ),

        "publishers": data.get(
            "publishers",
            []
        ),

        "release_date": data.get(
            "release_date",
            {}
        ).get(
            "date"
        ),

        "is_free": data.get(
            "is_free",
            False
        ),
    }


def get_game_metadata(appid):
    """
    Combines Store Details + Reviews
    into a single result.
    """

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
        "reviews": reviews
    }
