import pandas as pd
import streamlit as st
import sqlite3

from app_logging import configure_logging, get_logger
from sqlite_support import is_busy

from constants import (
    STATUS_BACKLOG,
    STATUS_UNPLAYED,
)
from database import (
    get_all_game_data,
    init_database,
)
from global_bootstrap import start_global_bootstrap, get_bootstrap_result
from settings import DEFAULT_SETTINGS, SettingsError, load_settings
from metadata_background import (
    start_metadata_background_refresh,
    stop_metadata_background_refresh,
)
from hltb_background import (
    start_hltb_background_refresh,
    stop_hltb_background_refresh,
)
from steam_cache import (
    load_games,
    load_player_summary,
)
from ui.account import (
    render_onboarding,
    show_steam_account,
)
from ui.common import init_session_state
from ui.game_dialog import show_selected_game_dialog
from ui.library import render_library
from update_checker import check_for_updates
from version import APP_VERSION


# =========================================================
# PAGE CONFIGURATION
# =========================================================

st.set_page_config(
    page_title="Steam Library Tracker",
    page_icon="🎮",
    layout="wide"
)

st.markdown(
    """
    <style>
    [data-testid="stMainBlockContainer"] {
        padding-top: 2rem;
    }
    </style>
    """,
    unsafe_allow_html=True
)


@st.cache_data(
    ttl=6 * 60 * 60,
    show_spinner=False
)
def get_update_info():
    return check_for_updates()


# =========================================================
# DATABASE + SESSION STATE
# =========================================================

configure_logging()
try:
    init_database()
except (sqlite3.Error, RuntimeError) as error:
    get_logger(__name__).exception("Database initialization failed")
    st.error("The local database is busy. Please try again in a moment." if is_busy(error)
             else "The local database could not be opened or upgraded. Your data has been retained. See slt.log for details.")
    st.stop()
init_session_state()


# =========================================================
# STEAM CONFIGURATION
# =========================================================

try:
    settings = load_settings()
except SettingsError as error:
    st.warning(str(error))
    settings = dict(DEFAULT_SETTINGS)

steam_profile = settings["steam_profile"]
steam_id = settings["steam_id"]
steam_api_key = settings["steam_api_key"]

settings_configured = (
    bool(steam_id)
    and bool(steam_api_key)
)

player = None

if settings_configured:
    try:
        player = load_player_summary(
            steam_api_key,
            steam_id
        )
    except Exception:
        player = None


if st.session_state.pop(
    "library_just_refreshed",
    False
):
    st.toast(
        "✅ Steam library updated."
    )

initial_connection_name = st.session_state.pop(
    "initial_connection_success",
    None
)

if initial_connection_name:
    st.toast(
        f"✅ Steam account connected: {initial_connection_name}"
    )


# =========================================================
# HEADER
# =========================================================

header_avatar_url = (
    player.get("avatarfull")
    if player is not None
    else ""
)

st.markdown(
    f"""
    <style>
    .st-key-open_smart_pick_header {{
        display: flex;
        align-items: center;
    }}

    .st-key-open_smart_pick_header button {{
        height: 52px;
        font-weight: 700;
    }}

    .st-key-open_steam_account {{
        display: flex;
        justify-content: center;
        align-items: center;
    }}

    .st-key-open_steam_account button {{
        width: 52px !important;
        min-width: 52px !important;
        height: 52px !important;
        padding: 0 !important;
        border-radius: 8px !important;
        border: 2px solid rgba(128, 128, 128, 0.35) !important;
        background-image: url("{header_avatar_url}") !important;
        background-size: cover !important;
        background-position: center !important;
        background-repeat: no-repeat !important;
        color: transparent !important;
        font-size: 0 !important;
        overflow: hidden !important;
    }}

    .st-key-open_steam_account button:hover {{
        border-color: rgba(200, 200, 200, 0.7) !important;
        filter: brightness(1.06);
    }}
    </style>
    """,
    unsafe_allow_html=True
)

header_col1, header_col2 = st.columns(
    [8.6, 1.4],
    vertical_alignment="center",
    gap="small"
)

with header_col1:
    st.markdown(
        f"""
        <h1 style="margin: 0;">
            🎮 Steam Library Tracker
            <span style="font-size: 0.42em; font-weight: 400; opacity: 0.55; white-space: nowrap;">
                v{APP_VERSION}
            </span>
        </h1>
        """,
        unsafe_allow_html=True
    )

with header_col2:
    action_col, avatar_col = st.columns(
        [3.2, 1],
        vertical_alignment="center",
        gap="small"
    )

    with action_col:
        if settings_configured:
            if st.button(
                "**🎲 What should I play next?**",
                type="primary",
                width="stretch",
                key="open_smart_pick_header"
            ):
                st.session_state[
                    "open_smart_pick_dialog"
                ] = True

    with avatar_col:
        if settings_configured:
            if player is not None:
                if st.button(
                    "Steam account",
                    key="open_steam_account",
                    help="Steam account"
                ):
                    st.session_state.open_steam_account_dialog = True
            else:
                if st.button(
                    "⚙️",
                    key="open_steam_settings_recovery",
                    help="Steam account settings"
                ):
                    st.session_state.steam_menu_view = "settings"
                    st.session_state.open_steam_account_dialog = True
                    st.rerun()


update_info = get_update_info()

if update_info is not None:
    st.info(
        "⬆️ "
        f"Steam Library Tracker {update_info.latest_version} is available. "
        f"[View release]({update_info.release_url})"
    )


# =========================================================
# STEAM ACCOUNT MENU
# =========================================================

if st.session_state.get(
    "open_steam_account_dialog",
    False
):
    show_steam_account(
        player=player or {},
        steam_profile=steam_profile,
        steam_id=steam_id,
        steam_api_key=steam_api_key
    )


# =========================================================
# INITIAL SETUP
# =========================================================

if not settings_configured:
    stop_metadata_background_refresh()
    stop_hltb_background_refresh()
    render_onboarding()
    st.stop()


# =========================================================
# LOAD LIBRARY
# =========================================================

def show_library_load_error(error):
    error_type = type(error).__name__

    if "Timeout" in error_type:
        st.error(
            "⏱️ Steam took too long to respond."
        )
        st.caption(
            "Try refreshing the library again in a moment."
        )

    elif "Connection" in error_type:
        st.error(
            "🌐 Could not contact Steam."
        )
        st.caption(
            "Check your internet connection and try again."
        )

    elif "HTTP" in error_type:
        st.error(
            "⚠️ Steam returned an error while processing the request."
        )
        st.caption(
            "Try again in a moment. If the problem persists, "
            "check your Steam account configuration."
        )

    else:
        st.error(
            "⚠️ Could not load the Steam library."
        )
        st.caption(
            "Try again. If the problem continues, check "
            "your Steam account configuration."
        )


try:
    games = load_games(
        steam_api_key,
        steam_id
    )
except Exception as error:
    stop_metadata_background_refresh()
    stop_hltb_background_refresh()
    show_library_load_error(error)

    if st.button(
        "⚙️ Change Steam settings",
        key="change_steam_settings_after_load_error"
    ):
        st.session_state.steam_menu_view = "settings"
        st.session_state.open_steam_account_dialog = True
        st.rerun()

    st.stop()

if not games:
    stop_metadata_background_refresh()
    stop_hltb_background_refresh()
    st.warning(
        "🎮 No games were found in this library."
    )
    st.info(
        "If you have games on this account, check your Steam privacy settings "
        "to make sure your game details "
        "are visible, then try refreshing the library."
    )

    retry_col, settings_col = st.columns(2)

    with retry_col:
        if st.button(
            "🔄 Try again",
            key="retry_empty_library",
            width="stretch"
        ):
            load_games.clear()
            st.rerun()

    with settings_col:
        if st.button(
            "⚙️ Change Steam settings",
            key="change_steam_settings_empty_library",
            width="stretch"
        ):
            st.session_state.steam_menu_view = "settings"
            st.session_state.open_steam_account_dialog = True
            st.rerun()

    st.stop()


# =========================================================
# BUILD LOCAL LIBRARY
# =========================================================

saved_data = get_all_game_data()
data = []

for game in games:
    appid = game.get("appid")
    playtime_minutes = game.get(
        "playtime_forever",
        0
    )
    playtime_hours = playtime_minutes / 60

    if playtime_minutes == 0:
        default_status = STATUS_UNPLAYED
    else:
        default_status = STATUS_BACKLOG

    game_saved_data = saved_data.get(
        appid,
        {}
    )

    status = game_saved_data.get(
        "status",
        default_status
    )

    notes = game_saved_data.get(
        "notes",
        ""
    )

    data.append(
        {
            "AppID": appid,
            "Game": game.get(
                "name",
                "Untitled"
            ),
            "Hours": round(
                playtime_hours,
                1
            ),
            "Status": status,
            "Notes": notes,
            "_saved_data": game_saved_data or None,
        }
    )

df = pd.DataFrame(data)

# Populate/refresh Steam Store metadata gently in the background.
# The worker uses only SQLite/network calls, never Streamlit UI calls, so
# normal interaction and Smart Pick remain responsive.
library_records = df.to_dict("records")

# Import missing catalogue rows asynchronously. Normal workers remain the
# fallback; the import never replaces existing local rows or manual IDs.
start_global_bootstrap(library_records)
bootstrap_result = get_bootstrap_result()
if bootstrap_result and bootstrap_result.get("attempted") and not st.session_state.get("bootstrap_notified"):
    st.session_state["bootstrap_notified"] = True
    if bootstrap_result.get("completed"):
        st.toast("✅ Global metadata imported.")
    else:
        st.toast("Global metadata is unavailable. Background refresh will continue.")

start_metadata_background_refresh(
    library_records
)

# Populate/refresh HowLongToBeat metadata independently from the Steam
# metadata worker. One cache being complete must never prevent the other
# worker from running.
start_hltb_background_refresh(
    library_records
)

show_selected_game_dialog(df)
render_library(df)
