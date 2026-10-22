import streamlit as st
import sqlite3

from database import save_game_status
from app_logging import get_logger
from constants import STATUSES


def init_session_state():
    defaults = {
        "library_search": "",
        "status_filters": [],
        "library_status_chart_version": 0,
        "library_sort": "Hours",
        "library_order": "Descending",
        "gallery_page": 1,
        "gallery_filter_signature": None,
        "selected_game_appid": None,
        "open_game_dialog": False,
        "game_dialog_source": None,
        "smart_pick_context": None,
        "smart_pick_history": [],
        "open_smart_pick_dialog": False,
        "open_steam_account_dialog": False,
        "steam_menu_view": "account",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def get_game_image(appid):
    return (
        f"https://shared.fastly.steamstatic.com/"
        f"store_item_assets/steam/apps/{appid}/header.jpg"
    )

def save_gallery_status(appid, widget_key):
    if st.session_state.get(widget_key) not in STATUSES:
        return
    try:
        save_game_status(appid=appid, status=st.session_state[widget_key])
    except sqlite3.Error:
        get_logger(__name__).exception("Could not save gallery status for %s", appid)
        st.warning("The status could not be saved. Please try again.")

def shorten_game_title(title, max_length=42):
    title = str(title)

    if len(title) <= max_length:
        return title

    return title[:max_length - 1].rstrip() + "…"

def open_game_dialog(appid, source="library"):
    st.session_state.pop(f"detail_baseline_{appid}", None)
    st.session_state.selected_game_appid = int(appid)
    st.session_state.open_game_dialog = True
    st.session_state.game_dialog_source = source

def clear_selected_game():
    appid = st.session_state.get("selected_game_appid")
    st.session_state.pop(f"detail_baseline_{appid}", None)
    st.session_state.open_game_dialog = False
    st.session_state.selected_game_appid = None
    st.session_state.game_dialog_source = None
    st.session_state.smart_pick_context = None
    st.session_state.smart_pick_history = []
    st.session_state.pop(
        "smart_pick_reroll_message",
        None
    )

