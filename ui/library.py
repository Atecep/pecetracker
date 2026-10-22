import altair as alt
import math
import pandas as pd
import streamlit as st

from database import (
    get_all_game_metadata,
    get_all_hltb_metadata,
    get_metadata_unavailable_appids,
)
from metadata_background import request_metadata_recheck
from constants import (
    GAMES_PER_ROW,
    PAGE_SIZE,
    STATUSES,
    STATUS_BACKLOG,
    STATUS_FINISHED,
    STATUS_PLAYING,
    STATUS_UNPLAYED,
)
from ui.common import (
    get_game_image,
    open_game_dialog,
    save_gallery_status,
    shorten_game_title,
)
from ui.smart_pick_dialog import show_smart_pick


def _selected_statuses_from_chart_state(chart_state):
    """Extract selected statuses from an Altair/Streamlit event."""

    if not chart_state:
        return []

    try:
        selection = chart_state["selection"]
    except (KeyError, TypeError):
        try:
            selection = chart_state.selection
        except AttributeError:
            return []

    try:
        selected_points = selection[
            "library_status_selection"
        ]
    except (KeyError, TypeError):
        try:
            selected_points = (
                selection.library_status_selection
            )
        except AttributeError:
            return []

    selected_statuses = []

    for point in selected_points or []:

        if isinstance(point, dict):
            status = point.get("Filter")
        else:
            try:
                status = point["Filter"]
            except (KeyError, TypeError):
                status = None

        if (
            status in STATUSES
            and status not in selected_statuses
        ):
            selected_statuses.append(status)

    return selected_statuses

def _refresh_library_status_chart():
    """Force the chart to reflect the current status filter."""
    st.session_state.library_status_chart_version += 1


def _set_metadata_only_view(enabled):
    """Toggle the technical Steam-metadata-only library view."""
    st.session_state.library_metadata_only = bool(enabled)

    if enabled:
        st.session_state.library_hltb_metadata_only = False

    st.session_state.gallery_page = 1
    st.session_state.library_scroll_to_top = True


def _set_hltb_metadata_only_view(enabled):
    """Toggle the technical HLTB-metadata-only library view."""
    st.session_state.library_hltb_metadata_only = bool(enabled)

    if enabled:
        st.session_state.library_metadata_only = False

    st.session_state.gallery_page = 1
    st.session_state.library_scroll_to_top = True


def _render_metadata_view_controls(
    position,
    total_results,
    library_df,
    metadata_unavailable_appids,
):
    """Render controls for the metadata-only library view."""

    # Keep the top navigation deliberately minimal.
    if position == "top":
        st.button(
            "Show all games",
            type="tertiary",
            key="show_all_games_top",
            on_click=_set_metadata_only_view,
            args=(False,),
        )
        return

    # Keep the bottom metadata controls simple and left-aligned.
    # This avoids wide-page column layouts pushing actions toward the centre.
    st.caption(
        f"Showing {total_results} games without metadata"
    )

    if st.button(
        "Check for metadata updates",
        type="tertiary",
        key=f"recheck_metadata_{position}",
    ):
        queued = request_metadata_recheck(
            library_df.to_dict("records"),
            metadata_unavailable_appids,
        )

        if queued > 0:
            st.toast(
                f"Checking metadata for {queued} game"
                + ("s" if queued != 1 else "")
                + "."
            )
        else:
            st.toast(
                "Metadata recheck is already queued."
            )

    st.button(
        "Show all games",
        type="tertiary",
        key=f"show_all_games_{position}",
        on_click=_set_metadata_only_view,
        args=(False,),
    )


def _render_hltb_metadata_view_controls(
    position,
    total_results,
):
    """Render controls for the HLTB-metadata-only library view."""

    if position == "top":
        st.button(
            "Show all games",
            type="tertiary",
            key="show_all_games_hltb_top",
            on_click=_set_hltb_metadata_only_view,
            args=(False,),
        )
        return

    st.caption(
        f"Showing {total_results} games without HLTB metadata"
    )

    st.button(
        "Show all games",
        type="tertiary",
        key=f"show_all_games_hltb_{position}",
        on_click=_set_hltb_metadata_only_view,
        args=(False,),
    )


def _render_gallery_pagination(
    total_results,
    current_page,
    total_pages,
    position,
    metadata_only=False,
    hltb_metadata_only=False,
    library_df=None,
    metadata_unavailable_appids=None,
):
    """Render compact pagination controls at the top or bottom."""

    results_col, prev_col, page_col, next_col = st.columns(
        [7.2, 0.55, 0.75, 0.55],
        vertical_alignment="top",
        gap="small"
    )

    with results_col:
        if metadata_only:
            _render_metadata_view_controls(
                position=position,
                total_results=total_results,
                library_df=library_df,
                metadata_unavailable_appids=metadata_unavailable_appids,
            )
        elif hltb_metadata_only:
            _render_hltb_metadata_view_controls(
                position=position,
                total_results=total_results,
            )
        else:
            st.markdown(
                f"**{total_results} games found**"
            )

    with prev_col:
        if st.button(
            "←",
            width="stretch",
            disabled=current_page <= 1,
            key=f"gallery_previous_page_{position}"
        ):
            st.session_state.gallery_page -= 1
            st.session_state.library_scroll_to_top = True
            st.rerun()

    with page_col:
        st.markdown(
            f"<div style='text-align:center; font-weight:600;'>"
            f"{current_page} / {total_pages}"
            f"</div>",
            unsafe_allow_html=True
        )

    with next_col:
        if st.button(
            "→",
            width="stretch",
            disabled=current_page >= total_pages,
            key=f"gallery_next_page_{position}"
        ):
            st.session_state.gallery_page += 1
            st.session_state.library_scroll_to_top = True
            st.rerun()


def render_library_overview(
    total_games,
    playing_games,
    backlog_games,
    unplayed_games,
    finished_games
):
    """Display and control the filter through the segmented bar."""

    status_data = [
        {
            "Status": "Playing",
            "Filter": STATUS_PLAYING,
            "Games": playing_games,
            "Icon": "🟣",
        },
        {
            "Status": "Backlog",
            "Filter": STATUS_BACKLOG,
            "Games": backlog_games,
            "Icon": "🔵",
        },
        {
            "Status": "Never played",
            "Filter": STATUS_UNPLAYED,
            "Games": unplayed_games,
            "Icon": "🟠",
        },
        {
            "Status": "Finished",
            "Filter": STATUS_FINISHED,
            "Games": finished_games,
            "Icon": "🟢",
        },
    ]

    for item in status_data:
        item["Percentage"] = (
            item["Games"] / total_games * 100
            if total_games > 0
            else 0
        )
        item["Group"] = "Library"

    selected_statuses = list(
        st.session_state.get(
            "status_filters",
            []
        )
    )

    # The Altair selection is used only to detect the clicked segment.
    # The actual filter state lives in st.session_state["status_filters"].
    #
    # This avoids a Streamlit/Altair issue when only one status is selected:
    # the chart selection may look active while the widget's internal
    # selection remains empty, so clicking the only segment to deselect it
    # may not trigger a change.
    if "library_status_chart_version" not in st.session_state:
        st.session_state.library_status_chart_version = 0

    chart_version = st.session_state.library_status_chart_version

    chart_key = (
        "library_status_chart_"
        + str(chart_version)
    )

    status_selection = alt.selection_point(
        name="library_status_selection",
        fields=["Filter"],
        toggle=False,
        clear=False
    )

    for item in status_data:
        item["Selected"] = (
            not selected_statuses
            or item["Filter"] in selected_statuses
        )

    chart_data = pd.DataFrame(status_data)

    header_col, stats_col, spacer_col = st.columns(
        [2.5, 5.0, 2.5],
        vertical_alignment="center",
        gap="small",
    )

    with header_col:
        st.markdown(
            f"**🎮 {total_games} games in your library**"
        )

    with stats_col:
        st.markdown(
            '<div style="display:flex; align-items:center; justify-content:center; '
            'gap:28px; flex-wrap:nowrap; color:#9ca3af; font-size:0.875rem; '
            'white-space:nowrap;">'
            + "".join(
                f"<span>{item['Icon']} {item['Status']}: "
                f"{item['Games']} · {item['Percentage']:.1f}%</span>"
                for item in status_data
            )
            + "</div>",
            unsafe_allow_html=True,
        )

    chart = (
        alt.Chart(chart_data)
        .mark_bar(
            cornerRadius=5
        )
        .encode(
            x=alt.X(
                "Games:Q",
                stack="zero",
                axis=None,
                title=None,
                scale=alt.Scale(
                    domain=[0, total_games],
                    nice=False
                )
            ),
            y=alt.Y(
                "Group:N",
                axis=None,
                title=None
            ),
            color=alt.Color(
                "Status:N",
                scale=alt.Scale(
                    domain=[
                        "Playing",
                        "Backlog",
                        "Never played",
                        "Finished",
                    ],
                    range=[
                        "#A855F7",
                        "#2D8CFF",
                        "#FF9800",
                        "#7CB342",
                    ]
                ),
                legend=None
            ),
            opacity=alt.condition(
                "datum.Selected",
                alt.value(1.0),
                alt.value(0.28)
            ),
            tooltip=[
                alt.Tooltip(
                    "Status:N",
                    title="Status"
                ),
                alt.Tooltip(
                    "Games:Q",
                    title="Games",
                    format=",.0f"
                ),
                alt.Tooltip(
                    "Percentage:Q",
                    title="Percentage",
                    format=".1f"
                ),
            ]
        )
        .add_params(
            status_selection
        )
        .properties(
            height=38
        )
    )

    def sync_status_filter_from_chart():
        chart_state = st.session_state.get(
            chart_key
        )

        clicked_statuses = (
            _selected_statuses_from_chart_state(
                chart_state
            )
        )

        if not clicked_statuses:
            return

        clicked_status = clicked_statuses[-1]

        current_statuses = list(
            st.session_state.get(
                "status_filters",
                []
            )
        )

        if clicked_status in current_statuses:
            current_statuses.remove(
                clicked_status
            )
        else:
            current_statuses.append(
                clicked_status
            )

        st.session_state.status_filters = (
            current_statuses
        )

        # New chart instance: the internal selection resets to empty.
        # This makes each click behave like a true toggle.
        st.session_state.library_status_chart_version += 1

    st.altair_chart(
        chart,
        width="stretch",
        key=chart_key,
        on_select=sync_status_filter_from_chart,
        selection_mode="library_status_selection"
    )


def render_library(df):
    metadata_view_param = st.query_params.get("metadata_view")

    if metadata_view_param == "steam":
        st.session_state.library_metadata_only = True
        st.session_state.library_hltb_metadata_only = False
        st.session_state.gallery_page = 1
        st.session_state.library_scroll_to_top = True
        del st.query_params["metadata_view"]
        st.rerun()

    if metadata_view_param == "hltb":
        st.session_state.library_metadata_only = False
        st.session_state.library_hltb_metadata_only = True
        st.session_state.gallery_page = 1
        st.session_state.library_scroll_to_top = True
        del st.query_params["metadata_view"]
        st.rerun()

    # Stable anchor used when switching between the normal library and the
    # metadata-unavailable view. It lives in the parent Streamlit document,
    # so the tiny component below can reliably scroll to it after a rerun.
    st.markdown(
        "<div id='library-top-anchor' style='height:0; margin:0; padding:0;'></div>",
        unsafe_allow_html=True
    )

    if st.session_state.pop("library_scroll_to_top", False):
        # st.iframe accepts trusted raw HTML and allows JavaScript to access
        # the same-origin Streamlit parent document. This replaces the
        # deprecated st.components.v1.html helper.
        st.iframe(
            """
            <script>
            const parentDocument = window.parent.document;

            const scrollToLibraryTop = () => {
                const anchor = parentDocument.getElementById('library-top-anchor');

                if (anchor) {
                    anchor.scrollIntoView({
                        behavior: 'smooth',
                        block: 'start'
                    });
                    return true;
                }

                return false;
            };

            if (!scrollToLibraryTop()) {
                setTimeout(scrollToLibraryTop, 80);
                setTimeout(scrollToLibraryTop, 220);
            }
            </script>
            """,
            height=1
        )

    total_games = len(df)

    status_counts = df["Status"].value_counts()

    finished_games = int(
        status_counts.get(
            STATUS_FINISHED,
            0
        )
    )

    playing_games = int(
        status_counts.get(
            STATUS_PLAYING,
            0
        )
    )

    backlog_games = int(
        status_counts.get(
            STATUS_BACKLOG,
            0
        )
    )

    unplayed_games = int(
        status_counts.get(
            STATUS_UNPLAYED,
            0
        )
    )

    # =====================================================
    # LIBRARY OVERVIEW
    # =====================================================

    render_library_overview(
        total_games=total_games,
        playing_games=playing_games,
        backlog_games=backlog_games,
        unplayed_games=unplayed_games,
        finished_games=finished_games
    )

    if st.session_state.pop(
        "open_smart_pick_dialog",
        False
    ):
        show_smart_pick(df)

    # =====================================================
    # LIBRARY TOOLBAR
    # =====================================================

    metadata_unavailable_appids = set(
        get_metadata_unavailable_appids()
    )

    metadata_unavailable_count = int(
        df["AppID"].isin(
            metadata_unavailable_appids
        ).sum()
    )

    if "library_metadata_only" not in st.session_state:
        st.session_state.library_metadata_only = False

    if (
        st.session_state.library_metadata_only
        and metadata_unavailable_count == 0
    ):
        st.session_state.library_metadata_only = False

    metadata_only = bool(
        st.session_state.library_metadata_only
    )

    hltb_metadata = get_all_hltb_metadata()
    hltb_matched_appids = {
        int(appid)
        for appid, metadata in hltb_metadata.items()
        if metadata.get("state") == "matched"
    }

    hltb_metadata_unavailable_appids = {
        int(appid)
        for appid in df["AppID"].tolist()
        if int(appid) not in hltb_matched_appids
    }

    hltb_metadata_unavailable_count = int(
        df["AppID"].isin(
            hltb_metadata_unavailable_appids
        ).sum()
    )

    if "library_hltb_metadata_only" not in st.session_state:
        st.session_state.library_hltb_metadata_only = False

    if (
        st.session_state.library_hltb_metadata_only
        and hltb_metadata_unavailable_count == 0
    ):
        st.session_state.library_hltb_metadata_only = False

    hltb_metadata_only = bool(
        st.session_state.library_hltb_metadata_only
    )

    search_col, status_col, sort_col, order_col = st.columns(
        [2.4, 1.6, 1, 1]
    )

    # -----------------------------------------------------
    # Search
    # -----------------------------------------------------

    with search_col:

        search_term = st.text_input(
            "🔎 Search",
            placeholder=(
                "e.g. Borderlands, Resident Evil, Mass Effect..."
            ),
            key="library_search"
        )

    # -----------------------------------------------------
    # Status
    # -----------------------------------------------------

    with status_col:

        status_filters = st.multiselect(
            "Status",
            STATUSES,
            key="status_filters",
            placeholder="All statuses",
            on_change=_refresh_library_status_chart
        )

    # -----------------------------------------------------
    # Contextual default sorting
    # -----------------------------------------------------

    status_signature = tuple(status_filters)
    previous_status_signature = st.session_state.get(
        "library_sort_status_signature"
    )

    only_never_played = (
        len(status_filters) == 1
        and status_filters[0] == STATUS_UNPLAYED
    )

    previously_only_never_played = (
        previous_status_signature == (STATUS_UNPLAYED,)
    )

    if previous_status_signature != status_signature:
        if only_never_played:
            # Entering the Never played-only view gets a useful alphabetical
            # default. The user can still change it afterwards.
            st.session_state.library_sort_v2 = "Name"
            st.session_state.library_order = "Ascending"
            st.session_state.library_never_played_auto_sort = True

        elif (
            previously_only_never_played
            and st.session_state.get(
                "library_never_played_auto_sort",
                False,
            )
        ):
            # Restore the normal library default only if the user did not
            # override the automatic Never played ordering.
            st.session_state.library_sort_v2 = "Hours Played"
            st.session_state.library_order = "Descending"
            st.session_state.library_never_played_auto_sort = False

        st.session_state.library_sort_status_signature = status_signature

    # -----------------------------------------------------
    # Sort by
    # -----------------------------------------------------

    with sort_col:

        sort_options = [
            "Name",
            "Hours Played",
            "HLTB Main Story",
            "HLTB Main + Extras",
            "HLTB Completionist",
            "SLT Review Score",
        ]

        if "library_sort_v2" not in st.session_state:
            st.session_state.library_sort_v2 = "Hours Played"

        sort_by = st.selectbox(
            "Sort by",
            sort_options,
            key="library_sort_v2"
        )

    # -----------------------------------------------------
    # Order
    # -----------------------------------------------------

    with order_col:

        order = st.selectbox(
            "Order",
            [
                "Descending",
                "Ascending",
            ],
            key="library_order"
        )

    # If the user changes away from the automatic Never played default,
    # do not overwrite that choice when leaving the view.
    if only_never_played and (
        sort_by != "Name"
        or order != "Ascending"
    ):
        st.session_state.library_never_played_auto_sort = False

    # =====================================================
    # SEARCH + FILTERS
    # =====================================================

    filtered_df = df.copy()

    # -----------------------------------------------------
    # Search
    # -----------------------------------------------------

    if search_term:

        filtered_df = filtered_df[
            filtered_df["Game"]
            .str.contains(
                search_term,
                case=False,
                na=False,
                regex=False,
            )
        ]

    # -----------------------------------------------------
    # Status
    # -----------------------------------------------------

    if status_filters:

        filtered_df = filtered_df[
            filtered_df["Status"].isin(
                status_filters
            )
        ]

    # -----------------------------------------------------
    # Metadata unavailable
    # -----------------------------------------------------

    if metadata_only:
        filtered_df = filtered_df[
            filtered_df["AppID"].isin(
                metadata_unavailable_appids
            )
        ]

    if hltb_metadata_only:
        filtered_df = filtered_df[
            filtered_df["AppID"].isin(
                hltb_metadata_unavailable_appids
            )
        ]

    # =====================================================
    # SORTING
    # =====================================================

    ascending = (
        order == "Ascending"
    )

    # -----------------------------------------------------
    # Hours played
    # -----------------------------------------------------

    if sort_by == "Hours Played":

        # Finished games always stay at the end
        filtered_df["Finished_sort"] = (
            filtered_df["Status"]
            == STATUS_FINISHED
        )

        filtered_df = (
            filtered_df
            .sort_values(
                by=[
                    "Finished_sort",
                    "Hours"
                ],
                ascending=[
                    True,
                    ascending
                ]
            )
            .drop(
                columns=["Finished_sort"]
            )
        )

    # -----------------------------------------------------
    # HowLongToBeat duration
    # -----------------------------------------------------

    elif sort_by in {
        "HLTB Main Story",
        "HLTB Main + Extras",
        "HLTB Completionist",
    }:
        duration_fallbacks = {
            "HLTB Main Story": (
                "main_story",
                "main_extra",
                "completionist",
            ),
            "HLTB Main + Extras": (
                "main_extra",
                "completionist",
                "main_story",
            ),
            "HLTB Completionist": (
                "completionist",
                "main_extra",
                "main_story",
            ),
        }[sort_by]

        # HLTB sorting only reads the local SQLite cache. It never triggers
        # requests to HowLongToBeat. If the selected duration is unavailable,
        # use the closest available HLTB duration as a sorting fallback. The
        # modal still shows only the real values returned by HLTB.
        hltb_metadata = get_all_hltb_metadata()

        duration_by_appid = {}
        for appid, metadata in hltb_metadata.items():
            if metadata.get("state") != "matched":
                continue

            for field in duration_fallbacks:
                value = metadata.get(field)
                if value is not None:
                    duration_by_appid[appid] = value
                    break

        filtered_df["_HLTB_duration"] = pd.to_numeric(
            filtered_df["AppID"].map(duration_by_appid),
            errors="coerce",
        )
        filtered_df["_HLTB_missing"] = (
            filtered_df["_HLTB_duration"].isna()
        )
        filtered_df["_Game_sort"] = (
            filtered_df["Game"].str.lower()
        )

        filtered_df = (
            filtered_df
            .sort_values(
                by=[
                    "_HLTB_missing",
                    "_HLTB_duration",
                    "_Game_sort",
                ],
                ascending=[
                    True,
                    ascending,
                    True,
                ],
                na_position="last",
            )
            .drop(
                columns=[
                    "_HLTB_duration",
                    "_HLTB_missing",
                    "_Game_sort",
                ]
            )
        )

    # -----------------------------------------------------
    # Name
    # -----------------------------------------------------

    elif sort_by == "Name":

        filtered_df = (
            filtered_df
            .sort_values(
                by="Game",
                ascending=ascending,
                key=lambda column:
                    column.str.lower()
            )
        )

    # -----------------------------------------------------
    # SLT Review Score
    # -----------------------------------------------------

    elif sort_by == "SLT Review Score":
        # Reviews already live in the local SQLite metadata cache. Reading
        # them here never triggers a Steam request. Ranking uses the lower
        # bound of a 95% Wilson score interval so that tiny samples do not
        # outrank very highly rated games with substantial review counts.
        metadata_by_appid = get_all_game_metadata()

        positive_percentage = {
            appid: metadata.get("positive_percentage")
            for appid, metadata in metadata_by_appid.items()
        }

        total_reviews = {
            appid: metadata.get("total_reviews")
            for appid, metadata in metadata_by_appid.items()
        }

        filtered_df["_Review_percentage"] = (
            pd.to_numeric(
                filtered_df["AppID"].map(positive_percentage),
                errors="coerce",
            )
        )
        filtered_df["_Total_reviews"] = (
            pd.to_numeric(
                filtered_df["AppID"].map(total_reviews),
                errors="coerce",
            )
        )

        def _wilson_lower_bound(row):
            percentage = row["_Review_percentage"]
            total = row["_Total_reviews"]

            if pd.isna(percentage) or pd.isna(total) or total <= 0:
                return float("nan")

            p_hat = max(0.0, min(1.0, float(percentage) / 100.0))
            n = float(total)
            z = 1.96  # 95% confidence interval
            z_squared = z * z

            numerator = (
                p_hat
                + z_squared / (2.0 * n)
                - z
                * math.sqrt(
                    (p_hat * (1.0 - p_hat) / n)
                    + z_squared / (4.0 * n * n)
                )
            )
            denominator = 1.0 + z_squared / n
            return numerator / denominator

        filtered_df["_Wilson_score"] = filtered_df.apply(
            _wilson_lower_bound,
            axis=1,
        )
        filtered_df["_Reviews_missing"] = (
            filtered_df["_Wilson_score"].isna()
        )
        filtered_df["_Game_sort"] = (
            filtered_df["Game"].str.lower()
        )

        filtered_df = (
            filtered_df
            .sort_values(
                by=[
                    "_Reviews_missing",
                    "_Wilson_score",
                    "_Total_reviews",
                    "_Game_sort",
                ],
                ascending=[
                    True,
                    ascending,
                    False,
                    True,
                ],
                na_position="last",
            )
            .drop(
                columns=[
                    "_Review_percentage",
                    "_Total_reviews",
                    "_Wilson_score",
                    "_Reviews_missing",
                    "_Game_sort",
                ]
            )
        )

    # =====================================================
    # GALLERY
    # =====================================================

    # -------------------------------------------------
    # Pagination
    # -------------------------------------------------

    # If search/filter/sorting changes:
    # return to page 1
    filter_signature = (
        search_term,
        tuple(status_filters),
        metadata_only,
        hltb_metadata_only,
        sort_by,
        order
    )

    previous_filter_signature = (
        st.session_state.get(
            "gallery_filter_signature"
        )
    )

    if previous_filter_signature is None:

        st.session_state[
            "gallery_filter_signature"
        ] = filter_signature

    elif previous_filter_signature != filter_signature:

        st.session_state.gallery_page = 1

        st.session_state[
            "gallery_filter_signature"
        ] = filter_signature

    total_results = len(
        filtered_df
    )

    if total_results == 0:

        if metadata_only:
            _render_metadata_view_controls(
                position="empty",
                total_results=0,
                library_df=df,
                metadata_unavailable_appids=metadata_unavailable_appids,
            )

            st.info(
                "🎮 No games without Steam metadata match the current filters."
            )

        elif hltb_metadata_only:
            _render_hltb_metadata_view_controls(
                position="empty",
                total_results=0,
            )

            st.info(
                "🎮 No games without HLTB metadata match the current filters."
            )

        else:
            st.markdown(
                "**0 games found**"
            )

            if search_term and status_filters:
                st.info(
                    "🔎 No games match your search and the selected "
                    "statuses. Try changing the filters."
                )

            elif search_term:
                st.info(
                    "🔎 No games match your search. "
                    "Try another name."
                )

            elif status_filters:
                st.info(
                    "🎮 There are no games with the selected statuses."
                )

            else:
                st.info(
                    "🎮 There are no games to show."
                )

        st.stop()

    total_pages = max(
        1,
        (
            total_results
            + PAGE_SIZE
            - 1
        ) // PAGE_SIZE
    )

    if (
        st.session_state.gallery_page
        > total_pages
    ):
        st.session_state.gallery_page = (
            total_pages
        )

    current_page = (
        st.session_state.gallery_page
    )

    # -------------------------------------------------
    # Results + compact navigation (top)
    # -------------------------------------------------

    _render_gallery_pagination(
        total_results=total_results,
        current_page=current_page,
        total_pages=total_pages,
        position="top",
        metadata_only=metadata_only,
        hltb_metadata_only=hltb_metadata_only,
        library_df=df,
        metadata_unavailable_appids=metadata_unavailable_appids,
    )

    # -------------------------------------------------
    # Games on this page
    # -------------------------------------------------

    start_index = (
        current_page - 1
    ) * PAGE_SIZE

    end_index = (
        start_index
        + PAGE_SIZE
    )

    gallery_df = filtered_df.iloc[
        start_index:end_index
    ]

    # -------------------------------------------------
    # Smooth clickable covers
    # -------------------------------------------------

    game_button_css = []

    for _, css_game in gallery_df.iterrows():

        css_appid = int(
            css_game["AppID"]
        )

        css_image_url = get_game_image(
            css_appid
        )

        game_button_css.append(
            f"""
            .st-key-open_game_{css_appid} button {{
                width: 100% !important;
                min-height: 0 !important;
                height: auto !important;
                aspect-ratio: 460 / 215 !important;
                padding: 0 !important;
                border: 0 !important;
                border-radius: 8px !important;
                background-image: url("{css_image_url}") !important;
                background-size: cover !important;
                background-position: center !important;
                background-repeat: no-repeat !important;
                color: transparent !important;
                font-size: 0 !important;
                box-shadow: none !important;
                overflow: hidden !important;
            }}

            .st-key-open_game_{css_appid} button p {{
                font-size: 0 !important;
                color: transparent !important;
            }}

            .st-key-open_game_{css_appid} button:hover {{
                filter: brightness(1.06);
            }}
            """
        )

    st.markdown(
        "<style>"
        + "".join(game_button_css)
        + "</style>",
        unsafe_allow_html=True
    )

    # -------------------------------------------------
    # Gallery
    # -------------------------------------------------

    for start in range(
        0,
        len(gallery_df),
        GAMES_PER_ROW
    ):

        row_games = gallery_df.iloc[
            start:
            start + GAMES_PER_ROW
        ]

        columns = st.columns(
            GAMES_PER_ROW
        )

        for column, (_, game) in zip(
            columns,
            row_games.iterrows()
        ):

            with column:

                appid = int(
                    game["AppID"]
                )

                # ---------------------------------
                # Smooth clickable banner
                # ---------------------------------

                st.button(
                    f"Open {game['Game']}",
                    key=f"open_game_{appid}",
                    width="stretch",
                    on_click=open_game_dialog,
                    args=(appid,)
                )

                # ---------------------------------
                # Name
                # ---------------------------------

                display_name = shorten_game_title(
                    game["Game"]
                )

                st.markdown(
                    f"**{display_name}**"
                )

                # ---------------------------------
                # Hours
                # ---------------------------------

                st.caption(
                    f"⏱️ {game['Hours']:.1f} h"
                )

                # ---------------------------------
                # Quick status
                # ---------------------------------

                current_status = (
                    game["Status"]
                )

                if current_status in STATUSES:

                    current_index = (
                        STATUSES.index(
                            current_status
                        )
                    )

                else:

                    current_index = 0

                status_key = (
                    f"gallery_status_{appid}_{current_index}"
                )

                st.selectbox(
                    "Status",
                    STATUSES,
                    index=current_index,
                    key=status_key,
                    label_visibility="collapsed",
                    on_change=save_gallery_status,
                    args=(
                        appid,
                        status_key
                    )
                )

    # -------------------------------------------------
    # Compact navigation (bottom)
    # -------------------------------------------------

    _render_gallery_pagination(
        total_results=total_results,
        current_page=current_page,
        total_pages=total_pages,
        position="bottom",
        metadata_only=metadata_only,
        hltb_metadata_only=hltb_metadata_only,
        library_df=df,
        metadata_unavailable_appids=metadata_unavailable_appids,
    )

    # -------------------------------------------------
    # Discreet metadata-unavailable view
    # -------------------------------------------------

    if (
        not metadata_only
        and not hltb_metadata_only
        and (
            metadata_unavailable_count > 0
            or hltb_metadata_unavailable_count > 0
        )
    ):
        metadata_links = []

        if metadata_unavailable_count > 0:
            metadata_links.append(
                (
                    "[Games Without Steam Metadata "
                    f"({metadata_unavailable_count})]"
                    "(?metadata_view=steam)"
                )
            )

        if hltb_metadata_unavailable_count > 0:
            metadata_links.append(
                (
                    "[Games Without HLTB Metadata "
                    f"({hltb_metadata_unavailable_count})]"
                    "(?metadata_view=hltb)"
                )
            )

        st.markdown(" · ".join(metadata_links))
