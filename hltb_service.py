from __future__ import annotations

from difflib import SequenceMatcher
import html
import re
import threading
import unicodedata

from cache_time import is_fresh
from hltb_transport import client, search_budget

from constants import (
    HLTB_CACHE_MAX_AGE_DAYS,
    HLTB_MATCH_AUTO_SIMILARITY,
    HLTB_MATCH_MIN_SIMILARITY,
    HLTB_MATCHER_VERSION,
    HLTB_NO_MATCH_CACHE_DAYS,
)
from database import (
    get_hltb_metadata as get_cached_hltb_metadata,
    save_hltb_metadata,
)


_fetch_locks_guard = threading.Lock()
_fetch_locks = {}

# Common year tags used by Steam to distinguish reboots/remasters with the
# same title, e.g. "GRID (2019)". We remove the tag for title comparison but
# keep the year as a separate safety signal.
_YEAR_SUFFIX_RE = re.compile(r"\s*[\(\[]((?:19|20)\d{2})[\)\]]\s*$")
_YEAR_ANY_RE = re.compile(r"\b((?:19|20)\d{2})\b")

# Edition labels that commonly exist in Steam product names even when HLTB
# only has one entry for the base game. These are stripped only for an
# alternate search/canonical comparison. Deliberately excludes labels such
# as "Remastered", which can represent a genuinely separate HLTB entry.
_EDITION_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?"
    r"(?:complete edition|game of the year edition|goty edition|goty|"
    r"deluxe edition|ultimate edition|definitive edition|premium edition|"
    r"anniversary edition|collector(?:'|’)?s edition|\(?classic\)?)"
    r"\s*$",
    re.IGNORECASE,
)


# Generic structural fallback for unknown Steam-only suffixes such as "HD".
# This replaces the need to keep adding every harmless suffix to a whitelist.
_STRUCTURAL_MAX_TRAILING_TOKENS = 3
_STRUCTURAL_MIN_BASE_TOKENS = 2
_STRUCTURAL_MIN_COVERAGE = 0.66

# These words often identify a genuinely distinct release. They can still
# match through the normal high-confidence path, but do not receive structural
# confirmation merely because the base title is an exact prefix.
_STRUCTURAL_SENSITIVE_TOKENS = {
    "remaster",
    "remastered",
    "remake",
}


def _get_fetch_lock(appid):
    with _fetch_locks_guard:
        return _fetch_locks.setdefault(int(appid), threading.Lock())


def _is_cache_fresh(metadata):
    state = metadata.get("state")

    # A manual HLTB ID is an explicit user override. It must not expire or be
    # invalidated by future matcher changes.
    if (
        state == "matched"
        and metadata.get("match_confidence") == "manual"
    ):
        return True

    if state == "matched" and has_sequel_conflict(metadata.get("searched_name"), metadata.get("matched_name")):
        return False

    # Matcher revisions are intended to improve unresolved/failed matches.
    # Do not invalidate an already successful match just because the matcher
    # version changed; otherwise every matcher tweak would make previously
    # resolved games hit HLTB again on the next modal open.
    #
    # Old no-match entries *are* invalidated by a matcher version change so
    # newly added normalisation/fallback rules get a chance to resolve them.
    if (
        state == "no_match"
        and metadata.get("matcher_version") != HLTB_MATCHER_VERSION
    ):
        return False

    fetched_at = metadata.get("fetched_at")

    if not fetched_at:
        return False

    max_age_days = (
        HLTB_NO_MATCH_CACHE_DAYS
        if state == "no_match"
        else HLTB_CACHE_MAX_AGE_DAYS
    )

    return is_fresh(fetched_at, max_age_days)



def is_hltb_cache_fresh(metadata):
    """Return whether one cached HLTB row is still valid for normal use."""
    return _is_cache_fresh(metadata)


def _normalise_hours(value):
    if value is None:
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def _extract_year(name):
    """Extract an explicit release year from a title, when present."""
    if not name:
        return None

    suffix_match = _YEAR_SUFFIX_RE.search(str(name))
    if suffix_match:
        return int(suffix_match.group(1))

    return None


def _strip_query_noise(name):
    """Remove trademark/copyright noise before HLTB search and comparison."""
    if not name:
        return ""

    value = html.unescape(str(name))

    # Remove symbol forms BEFORE NFKC. NFKC converts "™" to the letters "TM",
    # which would otherwise leave titles such as "South ParkTM".
    value = value.replace("™", "").replace("®", "").replace("©", "")
    value = unicodedata.normalize("NFKC", value)

    # Text forms occasionally present in Steam product names.
    value = re.sub(
        r"(?i)(?:\(\s*tm\s*\)|\[\s*tm\s*\]|\(\s*r\s*\)|\[\s*r\s*\])",
        "",
        value,
    )

    # Clean whitespace introduced by removing the marks, but keep meaningful
    # punctuation such as ':' because HLTB searches often benefit from it.
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _strip_safe_edition_suffix(name):
    """Remove a Steam edition suffix that can safely fall back to a base title.

    This is used only as an alternate HLTB search/canonical comparison. The
    original Steam name is still retained and cached. "Remastered" is
    intentionally not stripped because remasters can have separate HLTB data.
    """
    if not name:
        return ""

    value = str(name).strip()
    previous = None
    while value and value != previous:
        previous = value
        value = _EDITION_SUFFIX_RE.sub("", value).strip()

    return value


def _normalise_title(name, strip_edition=False):
    """Return a conservative canonical title for matching."""
    if not name:
        return ""

    value = _strip_query_noise(name)
    value = _YEAR_SUFFIX_RE.sub("", value).strip()

    if strip_edition:
        value = _strip_safe_edition_suffix(value)

    value = value.casefold()

    # Treat punctuation as separators while preserving alphanumeric content.
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def _title_tokens(name):
    normalised = _normalise_title(name)
    return normalised.split() if normalised else []


def has_sequel_conflict(left, right):
    """Do not lose an instalment number when trying shortened search queries."""
    if not left or not right:
        return False
    roman = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
             "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11,
             "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16}
    def numbers(name):
        tokens = _normalise_title(name, strip_edition=True).split()
        return [int(token) if token.isdigit() else roman[token]
                for index, token in enumerate(tokens)
                if token.isdigit() or (index > 0 and token in roman)]
    return numbers(left) != numbers(right)


def _structural_title_match(left, right):
    """Confirm titles that differ only by a short trailing suffix."""
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)

    if not left_tokens or not right_tokens or left_tokens == right_tokens:
        return None

    if len(left_tokens) <= len(right_tokens):
        shorter = left_tokens
        longer = right_tokens
    else:
        shorter = right_tokens
        longer = left_tokens

    extra_count = len(longer) - len(shorter)

    if (
        len(shorter) < _STRUCTURAL_MIN_BASE_TOKENS
        or extra_count < 1
        or extra_count > _STRUCTURAL_MAX_TRAILING_TOKENS
    ):
        return None

    # The differing words must be a suffix, never words inserted in the middle.
    if longer[: len(shorter)] != shorter:
        return None

    coverage = len(shorter) / len(longer)
    if coverage < _STRUCTURAL_MIN_COVERAGE:
        return None

    extra_tokens = longer[len(shorter):]

    if any(token in _STRUCTURAL_SENSITIVE_TOKENS for token in extra_tokens):
        return None

    return {
        "coverage": coverage,
        "extra_tokens": extra_tokens,
    }


def _structural_fallback_queries(game_name, existing_queries):
    """Trim 1-3 trailing tokens, only after the normal search path failed."""
    clean = _strip_query_noise(game_name)
    clean = _YEAR_SUFFIX_RE.sub("", clean).strip()

    tokenised = re.sub(r"[^\w]+", " ", clean, flags=re.UNICODE)
    tokenised = re.sub(r"\s+", " ", tokenised).strip()
    tokens = tokenised.split() if tokenised else []

    queries = []

    for trim_count in range(1, _STRUCTURAL_MAX_TRAILING_TOKENS + 1):
        remaining = tokens[:-trim_count]

        if len(remaining) < _STRUCTURAL_MIN_BASE_TOKENS:
            break

        query = " ".join(remaining)

        if query and all(
            query.casefold() != existing.casefold()
            for existing in [*existing_queries, *queries]
        ):
            queries.append(query)

    return queries


def _search_queries(queries):
    candidates = {}

    for query in queries:
        for entry in _search_once(query):
            try:
                game_id = int(entry.game_id)
            except (TypeError, ValueError, AttributeError):
                continue

            wrapper_similarity = float(
                getattr(entry, "similarity", 0.0) or 0.0
            )

            existing = candidates.get(game_id)
            if existing is None or wrapper_similarity > existing[1]:
                candidates[game_id] = (entry, wrapper_similarity)

    return list(candidates.values())


def _entry_release_year(entry):
    """Read release year defensively across wrapper versions."""
    value = getattr(entry, "release_world", None)

    if value is None:
        value = getattr(entry, "releaseWorld", None)

    if value is None:
        raw = getattr(entry, "json_content", None)
        if isinstance(raw, dict):
            value = raw.get("release_world")

    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    return value if 1900 <= value <= 2100 else None


def _title_similarity(left, right):
    left = _normalise_title(left)
    right = _normalise_title(right)

    if not left or not right:
        return 0.0

    return SequenceMatcher(None, left, right).ratio()


def _search_once(query):
    results = client(0.4).search(
        query,
        similarity_case_sensitive=False,
    )

    if results is None:
        # The wrapper documents None as a possible request/error result.
        # Do not turn a temporary HLTB failure into a long-lived no-match.
        raise RuntimeError("HowLongToBeat search failed")

    return results


def _collect_candidates(game_name):
    """Search the normal title variants first."""
    queries = [game_name]

    search_variant = _strip_query_noise(game_name)
    search_variant = _YEAR_SUFFIX_RE.sub("", search_variant).strip()

    if search_variant and search_variant.casefold() != game_name.casefold():
        queries.append(search_variant)

    punctuation_variant = re.sub(
        r"[^\w]+",
        " ",
        search_variant,
        flags=re.UNICODE,
    )
    punctuation_variant = re.sub(r"\s+", " ", punctuation_variant).strip()
    if punctuation_variant and all(
        punctuation_variant.casefold() != query.casefold()
        for query in queries
    ):
        queries.append(punctuation_variant)

    # Keep the existing known-edition shortcut because it can remove a
    # multi-word suffix in one request. Generic structural fallback below
    # handles suffixes we have never seen before.
    edition_variant = _strip_safe_edition_suffix(search_variant)
    if edition_variant and all(
        edition_variant.casefold() != query.casefold()
        for query in queries
    ):
        queries.append(edition_variant)

    edition_punctuation_variant = re.sub(
        r"[^\w]+",
        " ",
        edition_variant,
        flags=re.UNICODE,
    )
    edition_punctuation_variant = re.sub(
        r"\s+",
        " ",
        edition_punctuation_variant,
    ).strip()
    if edition_punctuation_variant and all(
        edition_punctuation_variant.casefold() != query.casefold()
        for query in queries
    ):
        queries.append(edition_punctuation_variant)

    return _search_queries(queries), queries


def _evaluate_candidate(game_name, entry, wrapper_similarity):
    if has_sequel_conflict(game_name, getattr(entry, "game_name", "")):
        return None
    steam_year = _extract_year(game_name)
    hltb_year = _entry_release_year(entry)

    steam_normalised = _normalise_title(game_name)
    hltb_normalised = _normalise_title(getattr(entry, "game_name", ""))
    steam_base_normalised = _normalise_title(game_name, strip_edition=True)
    hltb_base_normalised = _normalise_title(
        getattr(entry, "game_name", ""),
        strip_edition=True,
    )

    normalised_similarity = _title_similarity(game_name, getattr(entry, "game_name", ""))
    base_similarity = SequenceMatcher(
        None,
        steam_base_normalised,
        hltb_base_normalised,
    ).ratio() if steam_base_normalised and hltb_base_normalised else 0.0

    similarity = max(wrapper_similarity, normalised_similarity, base_similarity)
    exact_normalised_name = bool(steam_normalised) and steam_normalised == hltb_normalised
    exact_base_name = (
        bool(steam_base_normalised)
        and steam_base_normalised == hltb_base_normalised
    )
    structural_match = _structural_title_match(
        game_name,
        getattr(entry, "game_name", ""),
    )

    year_match = (
        steam_year is not None
        and hltb_year is not None
        and steam_year == hltb_year
    )
    year_conflict = (
        steam_year is not None
        and hltb_year is not None
        and steam_year != hltb_year
    )

    # An explicit year in the Steam title is a disambiguation signal. A known
    # conflicting HLTB year is therefore a hard rejection even for a near-
    # identical title (important for reboots that reuse the same name).
    if year_conflict:
        return None

    confidence = None

    if similarity >= HLTB_MATCH_AUTO_SIMILARITY:
        if steam_year is None or year_match:
            # An exact base-title match is also valid when the only difference
            # is a safe edition suffix such as "Complete Edition".
            if exact_normalised_name or exact_base_name or wrapper_similarity >= HLTB_MATCH_AUTO_SIMILARITY:
                confidence = "high"
        elif (exact_normalised_name or exact_base_name) and hltb_year is None:
            # The title is exact after safe canonicalisation, but HLTB did not
            # provide a year to validate it. Keep it out of the fully automatic
            # tier; it can still pass as a confirmed fuzzy match.
            confidence = "confirmed"

    elif similarity >= HLTB_MATCH_MIN_SIMILARITY:
        # Mid-confidence results require an independent title signal.
        # Structural confirmation means the shorter title is an exact prefix
        # and the only difference is a short trailing suffix.
        supporting_name_signal = (
            exact_normalised_name
            or exact_base_name
            or structural_match is not None
        )

        if supporting_name_signal and (steam_year is None or year_match):
            confidence = "confirmed"

    if confidence is None:
        return None

    return {
        "entry": entry,
        "similarity": similarity,
        "match_confidence": confidence,
        "year_match": year_match,
    }


def _accepted_candidates(game_name, candidates):
    accepted = []

    for entry, wrapper_similarity in candidates:
        evaluation = _evaluate_candidate(
            game_name,
            entry,
            wrapper_similarity,
        )
        if evaluation is not None:
            accepted.append(evaluation)

    return accepted


def _pick_best_candidate(accepted):
    if not accepted:
        return None

    best = max(
        accepted,
        key=lambda item: (
            item["similarity"],
            1 if item["year_match"] else 0,
        ),
    )
    entry = best["entry"]

    return {
        "state": "matched",
        "hltb_id": int(entry.game_id),
        "matched_name": entry.game_name,
        "similarity": float(best["similarity"]),
        "match_confidence": best["match_confidence"],
        "matcher_version": HLTB_MATCHER_VERSION,
        "main_story": _normalise_hours(entry.main_story),
        "main_extra": _normalise_hours(entry.main_extra),
        "completionist": _normalise_hours(entry.completionist),
        "all_styles": _normalise_hours(entry.all_styles),
        "web_link": entry.game_web_link,
    }


def _search_hltb(game_name):
    candidates, primary_queries = _collect_candidates(game_name)

    result = _pick_best_candidate(
        _accepted_candidates(game_name, candidates)
    )
    if result is not None:
        return result

    # Generic fallback is deliberately lazy: only failed normal matches pay
    # for these extra requests. Try the least destructive trim first and stop
    # as soon as a valid structural match is found.
    for query in _structural_fallback_queries(
        game_name,
        primary_queries,
    ):
        fallback_candidates = _search_queries([query])
        result = _pick_best_candidate(
            _accepted_candidates(game_name, fallback_candidates)
        )

        if result is not None:
            return result

    return None



def set_manual_hltb_metadata(appid, game_name, hltb_id):
    """Bind one Steam AppID to an explicit HowLongToBeat game ID.

    Manual matches are stored with match_confidence='manual', which makes
    them permanent until the user explicitly changes/removes the override.
    """
    appid = int(appid)
    game_name = str(game_name or "").strip()

    try:
        hltb_id = int(hltb_id)
    except (TypeError, ValueError) as error:
        raise ValueError("HLTB ID must be a number.") from error

    if hltb_id <= 0:
        raise ValueError("HLTB ID must be greater than zero.")

    with _get_fetch_lock(appid):
        with search_budget():
            entry = client(0.0).search_from_id(hltb_id)

        if entry is None:
            raise RuntimeError(
                "HowLongToBeat could not load that ID."
            )

        save_hltb_metadata(
            appid=appid,
            searched_name=game_name,
            state="matched",
            hltb_id=int(entry.game_id),
            matched_name=entry.game_name,
            similarity=None,
            match_confidence="manual",
            matcher_version=HLTB_MATCHER_VERSION,
            main_story=_normalise_hours(entry.main_story),
            main_extra=_normalise_hours(entry.main_extra),
            completionist=_normalise_hours(entry.completionist),
            all_styles=_normalise_hours(entry.all_styles),
            web_link=entry.game_web_link,
        )

        return get_cached_hltb_metadata(appid)


def get_hltb_metadata(appid, game_name, force_refresh=False, allow_fetch=True):
    """Return cached HLTB data, searching by Steam game name when needed."""
    appid = int(appid)
    game_name = str(game_name or "").strip()

    if not game_name:
        return None

    cached = get_cached_hltb_metadata(appid)

    if cached and cached.get("state") == "matched" and cached.get("match_confidence") == "manual":
        return cached
    if not allow_fetch:
        if cached and cached.get("state") == "matched" and cached.get("match_confidence") != "manual" and has_sequel_conflict(
            cached.get("searched_name"), cached.get("matched_name")
        ):
            return None
        return cached

    if (
        cached is not None
        and not force_refresh
        and _is_cache_fresh(cached)
    ):
        return cached

    with _get_fetch_lock(appid):
        cached = get_cached_hltb_metadata(appid)
        if cached and cached.get("state") == "matched" and cached.get("match_confidence") == "manual":
            return cached

        if (
            cached is not None
            and not force_refresh
            and _is_cache_fresh(cached)
        ):
            return cached

        with search_budget():
            result = _search_hltb(game_name)

        if result is None:
            save_hltb_metadata(
                appid=appid,
                searched_name=game_name,
                state="no_match",
                matcher_version=HLTB_MATCHER_VERSION,
            )
        else:
            save_hltb_metadata(
                appid=appid,
                searched_name=game_name,
                **result,
            )

        return get_cached_hltb_metadata(appid)
