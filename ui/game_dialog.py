import math
import platform
import sqlite3
from app_logging import get_logger

import streamlit as st

from constants import (
    METADATA_STATE_METADATA_UNAVAILABLE,
    STATUSES,
)
from database import (
    GameDataConflict,
    get_metadata_fetch_status,
    save_game_data,
)
from metadata_service import get_game_metadata
from hltb_service import get_hltb_metadata, set_manual_hltb_metadata
from protondb_service import analyse_protondb, protondb_url
from smart_pick import smart_pick
from ui.common import (
    clear_selected_game,
    get_game_image,
)


def _compact_metric(label, value):
    st.markdown(
        f"""
        <div class="compact-metric">
            <div class="compact-metric-label">{label}</div>
            <div class="compact-metric-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )



def _proton_tier_label(value):
    if not value:
        return "Unknown"
    return str(value).replace("_", " ").title()


def _proton_tier_badge(value):
    tier = str(value or "unknown").strip().lower()

    styles = {
        "platinum": {
            "bg": "rgba(59, 130, 246, 0.18)",
            "border": "rgba(96, 165, 250, 0.45)",
            "text": "#93c5fd",
            "label": "Platinum",
        },
        "gold": {
            "bg": "rgba(234, 179, 8, 0.18)",
            "border": "rgba(250, 204, 21, 0.45)",
            "text": "#fde047",
            "label": "Gold",
        },
        "silver": {
            "bg": "rgba(148, 163, 184, 0.18)",
            "border": "rgba(203, 213, 225, 0.40)",
            "text": "#e2e8f0",
            "label": "Silver",
        },
        "bronze": {
            "bg": "rgba(180, 83, 9, 0.18)",
            "border": "rgba(217, 119, 6, 0.45)",
            "text": "#fdba74",
            "label": "Bronze",
        },
        "borked": {
            "bg": "rgba(220, 38, 38, 0.18)",
            "border": "rgba(248, 113, 113, 0.45)",
            "text": "#fca5a5",
            "label": "Borked",
        },
        "pending": {
            "bg": "rgba(107, 114, 128, 0.18)",
            "border": "rgba(156, 163, 175, 0.40)",
            "text": "#d1d5db",
            "label": "Pending",
        },
        "unknown": {
            "bg": "rgba(107, 114, 128, 0.18)",
            "border": "rgba(156, 163, 175, 0.40)",
            "text": "#d1d5db",
            "label": "Unknown",
        },
    }

    style = styles.get(tier, styles["unknown"])

    return f"""
    <div style="
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.42rem 0.85rem;
        border-radius: 999px;
        border: 1px solid {style['border']};
        background: {style['bg']};
        color: {style['text']};
        font-weight: 700;
        font-size: 1rem;
        line-height: 1;
        margin: 0.1rem 0 0.35rem 0;
    ">
        <span>🐧</span>
        <span>{style['label']}</span>
    </div>
    """


def _format_age_days(days):
    if days is None or days >= 99999:
        return "unknown age"
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        months = max(1, round(days / 30.4))
        return f"{months} months ago"
    years = days / 365.25
    return f"{years:.1f} years ago"


def _open_protondb_dialog():
    appid = st.session_state.get("selected_game_appid")
    if appid is not None:
        st.session_state["protondb_game_draft"] = {
            "appid": int(appid),
            "fields": {
                key: st.session_state[key]
                for key in (f"detail_notes_{appid}", f"detail_status_{appid}")
                if key in st.session_state
            },
        }
    st.session_state["open_protondb_dialog"] = True


def _close_protondb_dialog():
    st.session_state["open_protondb_dialog"] = False


def _dismiss_protondb_dialog():
    _close_protondb_dialog()
    st.session_state.pop("protondb_game_draft", None)
    clear_selected_game()


def _invalidate_protondb_analysis(appid):
    cache = st.session_state.setdefault(
        "protondb_analysis_cache",
        {},
    )
    cache.pop(int(appid), None)


def _render_ranked_protondb_option(item, rank):
    st.markdown(f"**{rank}.**")
    st.code(item.get("command", ""), language=None)

    evidence = [
        f"{int(item.get('recent_count') or 0)} recent",
        f"{int(item.get('count') or 0)} total",
        f"latest {_format_age_days(item.get('latest_age_days'))}",
    ]
    st.caption(" · ".join(evidence))


def _render_protondb_analysis(analysis):
    summary = analysis.get("summary")

    if summary is None:
        st.caption(
            "No ProtonDB community reports were found for this Steam AppID."
        )
        return

    effective_tier = summary.get("effective_tier")
    st.markdown(
        _proton_tier_badge(effective_tier),
        unsafe_allow_html=True,
    )

    detail_error = analysis.get("detail_error")
    reports_analysed = int(analysis.get("reports_analysed") or 0)

    if detail_error:
        st.warning(
            "Partial analysis: some community reports could not be loaded."
            if analysis.get("reports_partial")
            else "Detailed community reports could not be analysed."
        )
        if not analysis.get("reports_partial"):
            return

    if reports_analysed <= 0:
        st.caption("No detailed reports were available for analysis.")
        return

    ranked_options = analysis.get("ranked_options") or []

    st.markdown("**Top community launch options**")

    if not ranked_options:
        st.caption(
            "No launch options were found in the analysed successful reports."
        )
    else:
        primary = ranked_options[:3]
        extra = ranked_options[3:]

        for index, item in enumerate(primary, start=1):
            _render_ranked_protondb_option(item, index)

        if extra:
            with st.expander(f"Show {len(extra)} more"):
                for index, item in enumerate(extra, start=4):
                    _render_ranked_protondb_option(item, index)


def _render_protondb_methodology(analysis):
    summary = analysis.get("summary") or {}
    confidence = _proton_tier_label(summary.get("confidence"))
    report_total = analysis.get("report_total") or summary.get("total") or 0
    reports_analysed = int(analysis.get("reports_analysed") or 0)

    st.markdown(f"**ProtonDB confidence:** {confidence}")
    st.markdown(f"**Total ProtonDB reports:** {int(report_total):,}")
    st.markdown(
        f"SLT analysed **{reports_analysed} community reports** from ProtonDB."
    )
    st.caption(
        "Newer successful reports receive more weight in the ranking."
    )
    st.caption(
        "GPU-specific options for a different detected vendor are omitted."
    )
    st.caption(
        "Community launch options are untrusted user reports. "
        "SLT only displays them; it never applies or executes them."
    )


@st.dialog(
    "ProtonDB analysis",
    width="medium",
    on_dismiss=_dismiss_protondb_dialog,
)
def show_protondb_analysis_dialog(appid, game_name):
    st.subheader(game_name)

    if st.button(
        "← Back",
        key=f"protondb_back_{appid}",
    ):
        _close_protondb_dialog()
        st.rerun()

    cache = st.session_state.setdefault(
        "protondb_analysis_cache",
        {},
    )
    analysis = cache.get(int(appid))

    if analysis is None:
        try:
            with st.spinner("Analysing ProtonDB reports..."):
                analysis = analyse_protondb(appid)
        except Exception:
            st.error("ProtonDB could not be reached at the moment.")
        else:
            cache[int(appid)] = analysis

    if analysis is not None:
        _render_protondb_analysis(analysis)

        st.divider()

        with st.expander("How this ranking works"):
            _render_protondb_methodology(analysis)

    refresh_col, protondb_col = st.columns(2)

    with refresh_col:
        st.button(
            "Refresh",
            key=f"protondb_refresh_{appid}",
            on_click=_invalidate_protondb_analysis,
            args=(appid,),
            width="stretch",
        )

    with protondb_col:
        st.link_button(
            "Open ProtonDB",
            protondb_url(appid),
            width="stretch",
        )


def _slt_review_score(positive_percentage, total_reviews):
    """Return the 95% Wilson lower bound used by SLT Review Score."""
    if (
        positive_percentage is None
        or total_reviews is None
        or total_reviews <= 0
    ):
        return None

    p_hat = max(0.0, min(1.0, float(positive_percentage) / 100.0))
    n = float(total_reviews)
    z = 1.96
    z_squared = z * z

    numerator = (
        p_hat
        + z_squared / (2.0 * n)
        - z * math.sqrt(
            (p_hat * (1.0 - p_hat) / n)
            + z_squared / (4.0 * n * n)
        )
    )
    denominator = 1.0 + z_squared / n

    return (numerator / denominator) * 100.0


@st.dialog(
    "Game details",
    width="medium",
    on_dismiss=clear_selected_game
)
def show_game_details(game, df):

    appid = int(game["AppID"])
    game_name = game["Game"]
    hours = float(game["Hours"])
    current_status = game["Status"]
    current_notes = game["Notes"]
    baseline_key = f"detail_baseline_{appid}"
    if baseline_key not in st.session_state:
        st.session_state[baseline_key] = game.get(
            "_saved_data", {"status": current_status, "notes": current_notes}
        )

    st.markdown(
        """
        <style>
        [data-testid="stDialog"] .compact-metric {
            padding: 0.05rem 0 0.15rem 0;
        }

        [data-testid="stDialog"] .compact-metric-label {
            font-size: 0.76rem;
            line-height: 1.15;
            opacity: 0.88;
            margin-bottom: 0.18rem;
        }

        [data-testid="stDialog"] .compact-metric-value {
            font-size: 1.55rem;
            line-height: 1.08;
            font-weight: 400;
            letter-spacing: -0.02em;
        }

        [data-testid="stDialog"] .compact-section-title {
            font-size: 0.91rem;
            font-weight: 600;
            margin: 0.2rem 0 0.35rem 0;
        }

        [data-testid="stDialog"] .compact-caption {
            font-size: 0.74rem;
            opacity: 0.68;
            margin: -0.05rem 0 0.15rem 0;
        }

        [data-testid="stDialog"] .review-secondary-row {
            font-size: 0.74rem;
            opacity: 0.68;
            margin: -0.05rem 0 0.15rem 0;
        }

        [data-testid="stDialog"] .section-gap {
            height: 0.45rem;
        }

        [data-testid="stDialog"] .secondary-info {
            font-size: 0.76rem;
            opacity: 0.76;
            margin: 0.1rem 0 0.1rem 0;
        }

        [data-testid="stDialog"] [class*="st-key-hltb_edit_button_"] {
            margin-top: 0.05rem;
            width: fit-content;
        }

        [data-testid="stDialog"] [class*="st-key-hltb_edit_button_"] button {
            min-height: 0 !important;
            height: auto !important;
            padding: 0 !important;
            border: 0 !important;
            background: transparent !important;
            box-shadow: none !important;
            opacity: 0.68;
        }

        [data-testid="stDialog"] [class*="st-key-hltb_edit_button_"] button p {
            font-size: 0.76rem !important;
            line-height: 1.15 !important;
            font-weight: 400 !important;
        }

        [data-testid="stDialog"] [class*="st-key-hltb_edit_button_"] button:hover {
            opacity: 1;
        }

        [data-testid="stDialog"] .technical-caption {
            font-size: 0.69rem;
            opacity: 0.48;
            margin-top: 0.1rem;
        }

        [data-testid="stDialog"] hr {
            margin-top: 0.6rem;
            margin-bottom: 0.6rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # =====================================================
    # STEAM METADATA
    # =====================================================

    metadata_load_failed = False

    try:
        metadata = get_game_metadata(appid, allow_fetch=False)
    except Exception:
        metadata = None
        metadata_load_failed = True

    metadata_fetch_status = get_metadata_fetch_status(appid)
    metadata_unavailable = (
        metadata_fetch_status is not None
        and metadata_fetch_status.get("state")
        == METADATA_STATE_METADATA_UNAVAILABLE
    )

    genres = []
    categories = []
    developers = []
    publishers = []

    review_description = None
    positive_percentage = None
    total_reviews = None

    if metadata:

        genres = metadata.get(
            "genres",
            []
        )

        categories = metadata.get(
            "categories",
            []
        )

        developers = metadata.get(
            "developers",
            []
        )

        publishers = metadata.get(
            "publishers",
            []
        )

        review_description = metadata.get(
            "review_description"
        )

        positive_percentage = metadata.get(
            "positive_percentage"
        )

        total_reviews = metadata.get(
            "total_reviews"
        )

    relevant_categories = [
        category
        for category in categories
        if any(
            keyword in category.lower()
            for keyword in [
                "single-player",
                "multi-player",
                "co-op",
                "pvp",
            ]
        )
    ]

    # =====================================================
    # HOWLONGTOBEAT
    # =====================================================

    hltb = None
    hltb_load_failed = False

    try:
        hltb = get_hltb_metadata(appid, game_name, allow_fetch=False)
    except Exception:
        # HLTB is an optional third-party source. A temporary failure must
        # never prevent the rest of the game modal from opening.
        hltb_load_failed = True

    hltb_matched = (
        hltb is not None
        and hltb.get("state") == "matched"
    )

    # =====================================================
    # BANNER
    # =====================================================

    image_url = get_game_image(appid)

    st.markdown(
        f"""
        <div style="
            width: 100%;
            height: 230px;
            overflow: hidden;
            border-radius: 8px;
            margin-bottom: 8px;
        ">
            <img
                src="{image_url}"
                style="
                    width: 100%;
                    height: 100%;
                    object-fit: cover;
                    display: block;
                "
            >
        </div>
        """,
        unsafe_allow_html=True
    )

    # =====================================================
    # TITLE + STATUS
    # =====================================================

    title_col, status_col = st.columns(
        [4, 1.3],
        vertical_alignment="center"
    )

    with title_col:

        st.subheader(
            game_name
        )

    with status_col:

        st.markdown(
            f"**{current_status}**"
        )

    # =====================================================
    # METRICS
    # =====================================================

    metric_col1, metric_col2, metric_col3 = st.columns(3)

    with metric_col1:
        _compact_metric(
            "⏱️ Playtime",
            f"{hours:.1f} h",
        )

    with metric_col2:
        _compact_metric(
            "👍 Positive (%)",
            (
                f"{positive_percentage:.1f}%"
                if positive_percentage is not None
                else "—"
            ),
        )

    with metric_col3:
        _compact_metric(
            "💬 Reviews",
            (
                f"{int(total_reviews):,}"
                if total_reviews is not None
                else "—"
            ),
        )

    slt_score = _slt_review_score(positive_percentage, total_reviews)

    if review_description or slt_score is not None:
        review_secondary_parts = []
        if review_description:
            review_secondary_parts.append(f"Steam: {review_description}")
        if slt_score is not None:
            review_secondary_parts.append(f"SLT Score: {slt_score:.1f}%")

        st.markdown(
            f'<div class="review-secondary-row">{" · ".join(review_secondary_parts)}</div>',
            unsafe_allow_html=True,
        )

    if hltb_matched:
        st.markdown(
            '<div class="section-gap"></div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="compact-section-title">⏳ HowLongToBeat</div>',
            unsafe_allow_html=True,
        )

        edit_state_key = f"hltb_edit_open_{appid}"

        if edit_state_key not in st.session_state:
            st.session_state[edit_state_key] = False

        hltb_col1, hltb_col2, hltb_col3 = st.columns(3)

        with hltb_col1:
            _compact_metric(
                "Main Story",
                (
                    f"{hltb['main_story']:.1f} h"
                    if hltb.get("main_story") is not None
                    else "—"
                ),
            )

        with hltb_col2:
            _compact_metric(
                "Main + Extras",
                (
                    f"{hltb['main_extra']:.1f} h"
                    if hltb.get("main_extra") is not None
                    else "—"
                ),
            )

        with hltb_col3:
            _compact_metric(
                "Completionist",
                (
                    f"{hltb['completionist']:.1f} h"
                    if hltb.get("completionist") is not None
                    else "—"
                ),
            )

        if st.button(
            "Edit",
            type="tertiary",
            key=f"hltb_edit_button_{appid}",
        ):
            st.session_state[edit_state_key] = not st.session_state[
                edit_state_key
            ]

        if st.session_state[edit_state_key]:
            if hltb.get("matched_name"):
                st.caption(
                    f"Current HLTB match: {hltb['matched_name']}"
                )

            with st.form(
                f"hltb_edit_match_{appid}",
                border=False,
            ):
                manual_id_col, save_col, cancel_col = st.columns(
                    [3, 1, 1],
                    vertical_alignment="bottom",
                )

                with manual_id_col:
                    manual_hltb_id = st.text_input(
                        "HLTB ID",
                        value=(
                            str(hltb.get("hltb_id"))
                            if hltb.get("hltb_id") is not None
                            else ""
                        ),
                        key=f"hltb_edit_id_{appid}",
                    )

                with save_col:
                    save_manual_hltb_id = st.form_submit_button(
                        "Save",
                        use_container_width=True,
                    )

                with cancel_col:
                    cancel_hltb_edit = st.form_submit_button(
                        "Cancel",
                        use_container_width=True,
                    )

                if cancel_hltb_edit:
                    st.session_state[edit_state_key] = False
                    st.rerun()

                if save_manual_hltb_id:
                    try:
                        manual_hltb_id = manual_hltb_id.strip()

                        if not manual_hltb_id:
                            raise ValueError("Enter an HLTB ID.")

                        set_manual_hltb_metadata(
                            appid=appid,
                            game_name=game_name,
                            hltb_id=manual_hltb_id,
                        )

                    except ValueError as error:
                        st.error(str(error))

                    except RuntimeError:
                        st.error(
                            "Could not load that HLTB ID. "
                            "Check the ID and try again."
                        )

                    except Exception:
                        st.error(
                            "HowLongToBeat could not be reached at the moment."
                        )

                    else:
                        st.session_state[edit_state_key] = False
                        st.rerun()

    elif hltb is not None and hltb.get("state") == "no_match":
        st.caption(
            "⏳ HowLongToBeat: no reliable name match found."
        )

        with st.form(
            f"hltb_manual_match_{appid}",
            border=False,
        ):
            manual_id_col, manual_button_col = st.columns(
                [3, 1],
                vertical_alignment="bottom",
            )

            with manual_id_col:
                manual_hltb_id = st.text_input(
                    "HLTB ID",
                    placeholder="e.g. 3126",
                    key=f"hltb_manual_id_{appid}",
                )

            with manual_button_col:
                use_manual_hltb_id = st.form_submit_button(
                    "Use HLTB ID",
                    use_container_width=True,
                )

            if use_manual_hltb_id:
                try:
                    manual_hltb_id = manual_hltb_id.strip()

                    if not manual_hltb_id:
                        raise ValueError("Enter an HLTB ID.")

                    set_manual_hltb_metadata(
                        appid=appid,
                        game_name=game_name,
                        hltb_id=manual_hltb_id,
                    )

                except ValueError as error:
                    st.error(str(error))

                except RuntimeError:
                    st.error(
                        "Could not load that HLTB ID. "
                        "Check the ID and try again."
                    )

                except Exception:
                    st.error(
                        "HowLongToBeat could not be reached at the moment."
                    )

                else:
                    st.rerun()

    elif hltb_load_failed:
        st.caption(
            "⚠️ HowLongToBeat data could not be loaded at the moment."
        )

    st.markdown(
        '<div class="section-gap"></div>',
        unsafe_allow_html=True,
    )

    if platform.system() == "Linux" and st.button(
        "🐧 Analyse ProtonDB",
        width="stretch",
        key=f"protondb_open_{appid}",
        help=(
            "Open an on-demand ProtonDB analysis for this game. "
            "Nothing is fetched until you click."
        ),
    ):
        _open_protondb_dialog()
        st.rerun()

    if metadata_unavailable:

        st.caption(
            "⚠️ Metadata unavailable. Steam is not currently returning "
            "Store metadata for this game. This can happen with removed "
            "titles, special editions, unavailable Store pages, or other "
            "Steam catalogue cases. The app will retry automatically."
        )

    elif metadata_load_failed:

        st.caption(
            "⚠️ Could not load all Steam metadata "
            "at the moment."
        )

    # =====================================================
    # GENRES + MODES
    # =====================================================

    metadata_col1, metadata_col2 = st.columns(2)

    with metadata_col1:

        st.markdown(
            '<div class="compact-section-title">🎭 Genres</div>',
            unsafe_allow_html=True,
        )

        if genres:

            st.write(
                " · ".join(genres)
            )

        else:

            st.caption(
                "No information"
            )

    with metadata_col2:

        st.markdown(
            '<div class="compact-section-title">🎮 Modes</div>',
            unsafe_allow_html=True,
        )

        if relevant_categories:

            st.write(
                " · ".join(
                    relevant_categories
                )
            )

        else:

            st.caption(
                "No information"
            )

    # =====================================================
    # DEVELOPER / PUBLISHER
    # =====================================================

    secondary_info = []

    if developers:

        secondary_info.append(
            "Developed by "
            + ", ".join(developers)
        )

    if publishers:

        secondary_info.append(
            "Published by "
            + ", ".join(publishers)
        )

    if secondary_info:

        st.markdown(
            f'<div class="secondary-info">{" · ".join(secondary_info)}</div>',
            unsafe_allow_html=True,
        )

    # =====================================================
    # PERSONAL DATA
    # =====================================================

    st.divider()

    status = st.selectbox(
        "Status",
        STATUSES,
        index=(
            None
            if f"detail_status_{appid}" in st.session_state
            else STATUSES.index(current_status)
            if current_status in STATUSES
            else 0
        ),
        key=f"detail_status_{appid}"
    )

    notes = st.text_area(
        "Notes",
        value=None if f"detail_notes_{appid}" in st.session_state else current_notes,
        height=100,
        key=f"detail_notes_{appid}",
        placeholder=(
            "Thoughts, where I left off, "
            "what I want to do next..."
        )
    )

    # =====================================================
    # SMART PICK REROLL
    # =====================================================

    came_from_smart_pick = (
        st.session_state.get(
            "game_dialog_source"
        )
        == "smart_pick"
    )

    smart_pick_context = (
        st.session_state.get(
            "smart_pick_context"
        )
    )

    if (
        came_from_smart_pick
        and smart_pick_context
    ):

        if st.button(
            "🎲 Another suggestion",
            width="stretch",
            key=f"smart_pick_reroll_{appid}"
        ):

            previous_appids = set(
                st.session_state.get(
                    "smart_pick_history",
                    []
                )
            )

            candidate_games = [
                game_record
                for game_record in df.to_dict(
                    "records"
                )
                if int(
                    game_record["AppID"]
                )
                not in previous_appids
            ]

            with st.spinner(
                "Looking for another suggestion..."
            ):

                reroll_result = smart_pick(
                    games=candidate_games,
                    statuses=smart_pick_context[
                        "statuses"
                    ],
                    genre=smart_pick_context[
                        "genre"
                    ],
                    category=smart_pick_context[
                        "category"
                    ],
                    min_positive_percentage=(
                        smart_pick_context[
                            "min_positive_percentage"
                        ]
                    ),
                    hltb_duration_type=(
                        smart_pick_context.get(
                            "hltb_duration_type"
                        )
                    ),
                    min_hltb_hours=(
                        smart_pick_context.get(
                            "min_hltb_hours"
                        )
                    ),
                    max_hltb_hours=(
                        smart_pick_context.get(
                            "max_hltb_hours"
                        )
                    ),
                )

            reroll_game = reroll_result.get(
                "game"
            )

            if reroll_game is None:

                st.session_state[
                    "smart_pick_reroll_message"
                ] = (
                    "Could not find another suggestion "
                    "with the same criteria."
                )

            else:

                reroll_appid = int(
                    reroll_game["AppID"]
                )

                history = list(
                    st.session_state.get(
                        "smart_pick_history",
                        []
                    )
                )

                history.append(
                    reroll_appid
                )

                st.session_state[
                    "smart_pick_history"
                ] = history

                st.session_state[
                    "selected_game_appid"
                ] = reroll_appid

                st.session_state[
                    "game_dialog_source"
                ] = "smart_pick"

                st.session_state.pop(
                    "smart_pick_reroll_message",
                    None
                )

                st.rerun()

        reroll_message = st.session_state.pop(
            "smart_pick_reroll_message",
            None
        )

        if reroll_message:

            st.info(
                reroll_message
            )

    # =====================================================
    # ACTIONS
    # =====================================================

    action_col1, action_col2, action_col3 = st.columns(3)

    with action_col1:

        if st.button(
            "💾 Save",
            type="primary",
            width="stretch",
            key=f"detail_save_{appid}"
        ):

            try:
                save_game_data(
                    appid=appid, status=status, notes=notes,
                    expected=st.session_state[baseline_key],
                )
            except GameDataConflict:
                st.warning("Another window changed this game. Your draft is still here. Close and reopen the game to load the latest saved data before saving again.")
                return
            except sqlite3.Error:
                get_logger(__name__).exception("Could not save personal data for %s", appid)
                st.warning("Your changes could not be saved. Your draft is still here; please try again.")
                return
            st.session_state[baseline_key] = {"status": status, "notes": notes}

            gallery_status_key = (
                f"gallery_status_{appid}"
            )

            if gallery_status_key in st.session_state:

                st.session_state[
                    gallery_status_key
                ] = status

            # Keep the game dialog open after saving.
            # The full rerun reloads the row from SQLite, while
            # selected_game_appid/open_game_dialog remain in session state.
            st.rerun()

    with action_col2:

        st.link_button(
            "▶️ Play / Install",
            f"steam://launch/{appid}/dialog",
            width="stretch",
            help=(
                "Open this game in the Steam client. "
                "Steam will offer installation if needed."
            )
        )

    with action_col3:

        st.link_button(
            "🛒 Open in Steam",
            (
                "https://store.steampowered.com/"
                f"app/{appid}/"
            ),
            width="stretch"
        )

    st.markdown(
        f'<div class="technical-caption">Steam AppID: {appid}</div>',
        unsafe_allow_html=True,
    )


def show_selected_game_dialog(df):
    selected_appid = st.session_state.get(
        "selected_game_appid"
    )

    if (
        selected_appid is None
        or not st.session_state.get(
            "open_game_dialog",
            False
        )
    ):
        st.session_state["open_protondb_dialog"] = False
        st.session_state.pop("protondb_game_draft", None)
        return

    selected_rows = df[
        df["AppID"] == int(selected_appid)
    ]

    if selected_rows.empty:
        st.session_state["open_protondb_dialog"] = False
        st.session_state.pop("protondb_game_draft", None)
        clear_selected_game()
        return

    selected_game_data = (
        selected_rows
        .iloc[0]
        .to_dict()
    )

    if st.session_state.get(
        "open_protondb_dialog",
        False,
    ):
        show_protondb_analysis_dialog(
            int(selected_appid),
            selected_game_data["Game"],
        )
        return

    draft = st.session_state.pop("protondb_game_draft", None)
    if draft and draft["appid"] == int(selected_appid):
        # Widget state is removed while the details dialog is not rendered.
        # Restore the snapshot before creating its widgets; Save remains explicit.
        st.session_state.update(draft["fields"])

    show_game_details(
        selected_game_data,
        df
    )
