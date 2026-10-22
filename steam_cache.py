import streamlit as st
import sqlite3
from app_logging import get_logger
from database import save_library_appids

from steam_api import (
    get_owned_games,
    get_player_summary,
)


@st.cache_data(ttl=3600)
def load_games(steam_api_key, steam_id):
    games = get_owned_games(
        steam_api_key,
        steam_id
    )
    # A private/unavailable library can also produce an empty response. Keep
    # the last successful ownership snapshot rather than erasing it then.
    if games:
        try:
            save_library_appids(steam_id, games)
        except sqlite3.Error:
            get_logger(__name__).exception("Could not update ownership snapshot")
    return games


@st.cache_data(ttl=3600, show_spinner=False)
def load_player_summary(steam_api_key, steam_id):
    return get_player_summary(
        steam_api_key,
        steam_id
    )
