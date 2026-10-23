from __future__ import annotations

from datetime import datetime, timezone
import platform
import re
import shlex
import shutil
import subprocess
from typing import Any

import requests

from version import USER_AGENT, GITHUB_URL


PROTONDB_BASE_URL = "https://www.protondb.com"
PROTONDB_TIMEOUT_SECONDS = 15
PROTONDB_MAX_REPORT_PAGES = 3
PROTONDB_REPORTS_PER_PAGE = 40

_TIER_ORDER = {
    "borked": 0,
    "bronze": 1,
    "silver": 2,
    "gold": 3,
    "platinum": 4,
}

_VENDOR_PATTERNS = {
    "AMD": (
        "amd",
        "radeon",
        "advanced micro devices",
        "ati ",
        "radv",
    ),
    "NVIDIA": (
        "nvidia",
        "geforce",
        "quadro",
    ),
    "Intel": (
        "intel",
        "iris",
        "arc ",
        "uhd graphics",
        "hd graphics",
    ),
}

_KNOWN_WRAPPERS = (
    "gamemoderun",
    "gamescope",
    "mangohud",
    "vkbasalt",
    "prime-run",
    "game-performance",
    "dlss-swapper",
)

_DIAGNOSTIC_TOKENS = (
    "PROTON_LOG",
    "WINEDEBUG",
    "DXVK_HUD",
    "VKD3D_DEBUG",
    "VKD3D_LOG",
    "MANGOHUD_CONFIG",
)

_SHELL_RISK_RE = re.compile(
    r"(?:\$\(|`|\n|\r|\b(?:sudo|su|rm|mkfs|dd|chmod|chown|curl|wget|eval)\b)",
    re.IGNORECASE,
)

_ASSIGNMENT_RE = re.compile(
    r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"[^\"\n]*\"|'[^'\n]*'|[^\s]+)"
)
_BACKTICK_RE = re.compile(r"`([^`\n]{1,500})`")


class ProtonDBError(RuntimeError):
    pass


def protondb_url(appid: int) -> str:
    return f"{PROTONDB_BASE_URL}/app/{int(appid)}"


def analyse_protondb(appid: int, max_pages: int = PROTONDB_MAX_REPORT_PAGES) -> dict[str, Any]:
    """Fetch and analyse ProtonDB data for one Steam AppID.

    Nothing is persisted. The caller decides whether to keep the returned result
    in session memory. Community launch options are treated as untrusted text and
    are never executed or applied by this module.
    """
    appid = int(appid)
    max_pages = max(1, min(int(max_pages), PROTONDB_MAX_REPORT_PAGES))

    system_profile = detect_local_system_profile()
    summary = _fetch_summary(appid)

    result: dict[str, Any] = {
        "appid": appid,
        "url": protondb_url(appid),
        "summary": summary,
        "system": system_profile,
        "report_total": int(summary.get("total") or 0) if summary else 0,
        "reports_analysed": 0,
        "ranked_options": [],
        "detail_error": None,
        "reports_partial": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    if summary is None:
        return result

    try:
        reports, total, detail_error = _fetch_detailed_reports(
            appid,
            system_profile=system_profile,
            max_pages=max_pages,
        )
    except Exception as error:
        result["detail_error"] = str(error)
        return result

    result["report_total"] = max(int(total or 0), result["report_total"])
    result["reports_analysed"] = len(reports)
    result["detail_error"] = detail_error
    result["reports_partial"] = bool(detail_error and reports)

    analysis = _analyse_reports(reports, system_profile)
    result.update(analysis)
    return result


def detect_local_system_profile() -> dict[str, Any]:
    system_name = platform.system()
    profile: dict[str, Any] = {
        "platform": system_name,
        "is_linux": system_name.lower() == "linux",
        "distro": None,
        "kernel": platform.release() or None,
        "gpus": [],
        "gpu_vendors": [],
        "driver": None,
        "is_steam_deck": False,
    }

    if not profile["is_linux"]:
        return profile

    os_release = _read_os_release()
    profile["distro"] = (
        os_release.get("PRETTY_NAME")
        or os_release.get("NAME")
        or None
    )

    gpus: list[str] = []
    drivers: list[str] = []

    if shutil.which("lspci"):
        try:
            completed = subprocess.run(
                ["lspci", "-nnk"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            current_gpu = None
            for raw_line in completed.stdout.splitlines():
                line = raw_line.rstrip()
                lower = line.lower()
                if (
                    "vga compatible controller" in lower
                    or "3d controller" in lower
                    or "display controller" in lower
                ):
                    current_gpu = line.split(": ", 1)[-1].strip()
                    if current_gpu:
                        gpus.append(current_gpu)
                elif current_gpu and "kernel driver in use:" in lower:
                    driver = line.split(":", 1)[-1].strip()
                    if driver:
                        drivers.append(driver)
        except Exception:
            pass

    if shutil.which("nvidia-smi"):
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,driver_version",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            for line in completed.stdout.splitlines():
                parts = [part.strip() for part in line.split(",", 1)]
                if parts and parts[0] and parts[0] not in gpus:
                    gpus.append(parts[0])
                if len(parts) > 1 and parts[1]:
                    drivers.append(f"NVIDIA {parts[1]}")
        except Exception:
            pass

    vendors = []
    for gpu in gpus:
        vendor = _gpu_vendor(gpu)
        if vendor != "Unknown" and vendor not in vendors:
            vendors.append(vendor)

    profile["gpus"] = gpus
    profile["gpu_vendors"] = vendors
    profile["driver"] = ", ".join(dict.fromkeys(drivers)) or None

    product_name = _read_text_file("/sys/devices/virtual/dmi/id/product_name")
    combined = " ".join(
        value
        for value in (
            profile.get("distro"),
            product_name,
            os_release.get("VARIANT"),
        )
        if value
    ).lower()
    profile["is_steam_deck"] = any(
        marker in combined
        for marker in ("steam deck", "steamos", "jupiter", "galileo")
    )

    return profile


def _fetch_summary(appid: int) -> dict[str, Any] | None:
    url = f"{PROTONDB_BASE_URL}/api/v1/reports/summaries/{appid}.json"
    response = requests.get(
        url,
        headers=_request_headers(),
        timeout=PROTONDB_TIMEOUT_SECONDS,
    )

    if response.status_code == 404:
        return None

    try:
        response.raise_for_status()
        payload = response.json()
    except Exception as error:
        raise ProtonDBError("Could not load the ProtonDB summary.") from error

    tier = str(payload.get("tier") or "").lower()
    provisional = str(payload.get("provisionalTier") or "").lower()
    effective_tier = provisional if tier == "pending" and provisional else tier

    return {
        "tier": tier or None,
        "effective_tier": effective_tier or None,
        "provisional_tier": provisional or None,
        "trending_tier": _normalise_tier(payload.get("trendingTier")),
        "best_reported_tier": _normalise_tier(payload.get("bestReportedTier")),
        "confidence": payload.get("confidence"),
        "score": payload.get("score"),
        "total": payload.get("total"),
    }


def _fetch_detailed_reports(
    appid: int,
    system_profile: dict[str, Any],
    max_pages: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    counts_response = requests.get(
        f"{PROTONDB_BASE_URL}/data/counts.json",
        headers=_request_headers(),
        timeout=PROTONDB_TIMEOUT_SECONDS,
    )
    try:
        counts_response.raise_for_status()
        counts = counts_response.json()
        reports_token = int(counts["reports"])
        timestamp_token = int(counts["timestamp"])
    except Exception as error:
        raise ProtonDBError("Could not prepare ProtonDB community reports.") from error

    device = "steam-deck" if system_profile.get("is_steam_deck") else "pc"

    reports: list[dict[str, Any]] = []
    total = 0
    detail_error = None

    for page in range(1, max_pages + 1):
        report_hash = _protondb_report_hash(
            appid=appid,
            reports_token=reports_token,
            timestamp_token=timestamp_token,
            page=page,
        )
        url = (
            f"{PROTONDB_BASE_URL}/data/reports/{device}/app/"
            f"{report_hash}.json"
        )
        try:
            response = requests.get(
                url,
                headers=_request_headers(),
                timeout=PROTONDB_TIMEOUT_SECONDS,
            )
            if response.status_code == 404 and page == 1 and device != "all-devices":
                # Fall back once if there are no device-specific reports.
                device = "all-devices"
                url = (
                    f"{PROTONDB_BASE_URL}/data/reports/{device}/app/"
                    f"{report_hash}.json"
                )
                response = requests.get(
                    url,
                    headers=_request_headers(),
                    timeout=PROTONDB_TIMEOUT_SECONDS,
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid ProtonDB report page.")
            page_reports = payload.get("reports") or []
            if not isinstance(page_reports, list):
                raise ValueError("Invalid ProtonDB reports.")
            page_total = int(payload.get("total") or total or 0)
            per_page = int(payload.get("perPage") or PROTONDB_REPORTS_PER_PAGE)
        except Exception as error:
            if not reports:
                raise ProtonDBError(
                    "Detailed ProtonDB reports could not be loaded."
                ) from error
            detail_error = "Some ProtonDB community reports could not be loaded."
            break

        total = page_total
        reports.extend(
            report for report in page_reports if isinstance(report, dict)
        )

        if not page_reports or len(reports) >= total or len(page_reports) < per_page:
            break

    reports.sort(key=lambda report: _report_timestamp(report) or 0, reverse=True)
    return reports, total, detail_error


def _analyse_reports(
    reports: list[dict[str, Any]],
    system_profile: dict[str, Any],
) -> dict[str, Any]:
    """Rank recurring launch-option families, strongly favouring recent reports.

    Only positive/playable reports contribute to the ranking. Commands that are
    explicitly tied to another GPU vendor are omitted for a detected local GPU.
    """
    now = datetime.now(timezone.utc).timestamp()
    local_vendors = set(system_profile.get("gpu_vendors") or [])

    family_stats: dict[str, dict[str, Any]] = {}

    for report in reports:
        if not _report_is_positive(report):
            continue

        timestamp = _report_timestamp(report)
        age_days = _age_days(timestamp, now)
        report_vendor = _report_gpu_vendor(report)

        commands = []
        for command in _extract_launch_options(report):
            if not _is_safe_to_surface(command):
                continue
            normalised = _normalise_command(command)
            if normalised:
                commands.append(normalised)

        # Count each semantic family once per report. Slight command variants
        # remain available for display, but do not artificially inflate totals.
        report_families: dict[str, dict[str, Any]] = {}

        for command in commands:
            semantics = _command_semantics(command)
            category = semantics["category"]
            hardware_scope = semantics["hardware_scope"]

            if category == "diagnostic":
                continue

            if (
                hardware_scope
                and local_vendors
                and hardware_scope not in local_vendors
            ):
                continue

            family = report_families.setdefault(
                semantics["family_key"],
                {
                    "semantics": semantics,
                    "commands": set(),
                },
            )
            family["commands"].add(command)

        for family_key, family in report_families.items():
            semantics = family["semantics"]
            commands_in_report = sorted(family["commands"])

            stats = family_stats.setdefault(
                family_key,
                {
                    "family_key": family_key,
                    "title": semantics["title"],
                    "hardware_scope": semantics["hardware_scope"],
                    "count": 0,
                    "recent_count": 0,
                    "last_6_months": 0,
                    "score": 0.0,
                    "latest_timestamp": None,
                    "variants": {},
                },
            )

            # Recency is the dominant signal:
            #   <= 6 months: 4x
            #   <= 1 year:   3x
            #   <= 2 years:  1.5x
            #   older:       0.5x
            score = _ranking_recency_weight(age_days)

            # Very small hardware relevance nudge. This never turns a generic
            # command into a hardware-specific recommendation.
            if (
                report_vendor != "Unknown"
                and local_vendors
                and report_vendor in local_vendors
            ):
                score *= 1.08

            stats["count"] += 1
            if age_days <= 365:
                stats["recent_count"] += 1
            if age_days <= 183:
                stats["last_6_months"] += 1
            stats["score"] += score

            if timestamp and (
                stats["latest_timestamp"] is None
                or timestamp > stats["latest_timestamp"]
            ):
                stats["latest_timestamp"] = timestamp

            for command in commands_in_report:
                variant = stats["variants"].setdefault(
                    command,
                    {
                        "command": command,
                        "count": 0,
                        "recent_count": 0,
                        "last_6_months": 0,
                        "score": 0.0,
                        "latest_timestamp": None,
                    },
                )
                variant["count"] += 1
                if age_days <= 365:
                    variant["recent_count"] += 1
                if age_days <= 183:
                    variant["last_6_months"] += 1
                variant["score"] += score
                if timestamp and (
                    variant["latest_timestamp"] is None
                    or timestamp > variant["latest_timestamp"]
                ):
                    variant["latest_timestamp"] = timestamp

    ranked_options = []

    for stats in family_stats.values():
        variants = []
        for variant in stats["variants"].values():
            variants.append(
                {
                    "command": variant["command"],
                    "count": variant["count"],
                    "recent_count": variant["recent_count"],
                    "last_6_months": variant["last_6_months"],
                    "score": round(variant["score"], 3),
                    "latest_age_days": _age_days(
                        variant["latest_timestamp"],
                        now,
                    ),
                }
            )

        variants.sort(
            key=lambda item: (
                -item["score"],
                -item["last_6_months"],
                -item["recent_count"],
                -item["count"],
                item["latest_age_days"],
            )
        )

        ranked_options.append(
            {
                "family_key": stats["family_key"],
                "title": stats["title"],
                "hardware_scope": stats["hardware_scope"],
                "command": variants[0]["command"] if variants else "",
                "variants": variants,
                "count": stats["count"],
                "recent_count": stats["recent_count"],
                "last_6_months": stats["last_6_months"],
                "score": round(stats["score"], 3),
                "latest_age_days": _age_days(
                    stats["latest_timestamp"],
                    now,
                ),
            }
        )

    ranked_options.sort(
        key=lambda item: (
            -item["score"],
            -item["last_6_months"],
            -item["recent_count"],
            -item["count"],
            item["latest_age_days"],
        )
    )

    return {
        "ranked_options": ranked_options[:8],
    }


def _ranking_recency_weight(age_days: int) -> float:
    if age_days <= 183:
        return 4.0
    if age_days <= 365:
        return 3.0
    if age_days <= 730:
        return 1.5
    return 0.5


def _extract_launch_options(report: dict[str, Any]) -> list[str]:
    structured_candidates: list[str] = []
    free_text_candidates: list[str] = []

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, (*path, str(key).lower()))
            return
        if isinstance(value, list):
            for nested in value:
                walk(nested, path)
            return
        if not isinstance(value, str):
            return

        field_name = path[-1] if path else ""
        structured_hint = any(
            hint in field_name
            for hint in (
                "launch",
                "tinker",
                "command",
                "option",
                "parameter",
            )
        )

        destination = (
            structured_candidates if structured_hint else free_text_candidates
        )
        destination.extend(
            _command_candidates_from_text(value, structured=structured_hint)
        )

    walk(report)
    candidates = structured_candidates or free_text_candidates

    unique = []
    seen = set()
    for candidate in candidates:
        normalised = _normalise_command(candidate)
        if (
            normalised
            and normalised not in seen
            and _looks_like_launch_command(normalised, allow_bare_flags=True)
        ):
            seen.add(normalised)
            unique.append(normalised)

    return unique


def _command_candidates_from_text(
    text: str,
    structured: bool = False,
) -> list[str]:
    text = str(text).strip()
    if not text:
        return []

    backtick_candidates = [
        match.group(1).strip()
        for match in _BACKTICK_RE.finditer(text)
        if match.group(1).strip()
    ]
    candidates = [
        candidate
        for candidate in backtick_candidates
        if _command_fragment(candidate, allow_bare_flags=True) is not None
    ]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        line = re.sub(r"^(?:[-*•>]\s+)+", "", line)
        line, labelled = re.subn(
            r"^(?:launch options?|launch command|command|options?)\s*[:=-]\s*",
            "",
            line,
            flags=re.IGNORECASE,
        )
        line = line.strip()
        if not line:
            continue

        fragment = _command_fragment(line, allow_bare_flags=structured or bool(labelled))
        if fragment:
            candidates.append(fragment)

    return candidates


def _command_fragment(
    text: str,
    allow_bare_flags: bool = False,
) -> str | None:
    if not _looks_like_launch_command(text, allow_bare_flags):
        return None

    fragment = text.strip()
    if _normalise_command(fragment).lower() == "%command%":
        return None
    if not _is_command_shaped(fragment):
        return None

    return fragment


def _is_command_shaped(text: str) -> bool:
    try:
        tokens = shlex.split(text)
    except ValueError:
        return False

    if not tokens:
        return False

    first = tokens[0]
    if not (
        first.lower() == "%command%"
        or first.lower() in (*_KNOWN_WRAPPERS, "env")
        or first.startswith(("-", "+"))
        or _is_assignment_token(first)
    ):
        return False

    remaining_values = 0
    after_command = False
    assignments_allowed = True
    for token in tokens:
        lower = token.lower()
        if lower == "%command%":
            if after_command:
                return False
            after_command = True
            remaining_values = 0
        elif not after_command and lower in (*_KNOWN_WRAPPERS, "env"):
            # Only env accepts assignments as arguments to the executable.
            assignments_allowed = lower == "env"
            remaining_values = 0
        elif not after_command and _is_assignment_token(token):
            if not assignments_allowed:
                return False
        elif after_command and re.fullmatch(r"/[A-Za-z][A-Za-z0-9_.-]*:[^\s]+", token):
            # Slash options carry their value in the same token, including case.
            remaining_values = 0
        elif token.startswith(("-", "+")):
            # Bound positional values even in explicit fields/code: a field
            # name or backticks do not make trailing narrative a launch option.
            if "=" in token or lower in {"--", "-dx11", "-dx12", "--launcher-skip"}:
                remaining_values = 0
            elif lower in {"+set", "+seta", "+setu", "+sets", "--profiles"}:
                remaining_values = 2
            else:
                remaining_values = 1
        elif remaining_values:
            remaining_values -= 1
        else:
            return False

    return True


def _is_assignment_token(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _looks_like_launch_command(
    text: str,
    allow_bare_flags: bool = False,
) -> bool:
    value = str(text).strip()
    if not value or len(value) > 500:
        return False

    lower = value.lower()
    if allow_bare_flags and re.match(r"^[-+]{1,2}[A-Za-z]", value):
        return True
    if "%command%" in lower:
        return True
    if any(wrapper in lower for wrapper in _KNOWN_WRAPPERS):
        return True
    return bool(_ASSIGNMENT_RE.search(value))


def _normalise_command(command: str) -> str:
    value = str(command).strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in "`\"'"
    ):
        value = value[1:-1].strip()
    # Collapse only unquoted whitespace. Quoted values and escaped spaces are
    # part of the command, as is punctuation at the end of an argument.
    parts = []
    quote = None
    escaped = False
    pending_space = False
    for character in value:
        if not escaped and quote is None and character.isspace():
            pending_space = True
            continue
        if pending_space and parts:
            parts.append(" ")
        pending_space = False
        if escaped:
            parts.append(character)
            escaped = False
        elif character == "\\" and quote != "'":
            parts.append(character)
            escaped = True
        elif character in "\"'":
            parts.append(character)
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        else:
            parts.append(character)
    value = "".join(parts)

    if len(value) > 400:
        return ""
    return value


def _is_safe_to_surface(command: str) -> bool:
    """Reject report text that could obviously act as an arbitrary shell payload.

    The UI only displays commands, but ProtonDB reports are untrusted user content.
    Filtering shell control syntax prevents the app from presenting obviously
    dangerous snippets as if they were normal launch options.
    """
    if not command or _SHELL_RISK_RE.search(command):
        return False

    quote = None
    for character in command:
        if character in "\"'":
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        elif quote is None and character in ";|<>&":
            return False

    if quote is not None:
        return False

    lower = command.lower()
    if any(
        dangerous in lower
        for dangerous in (
            "/dev/sd",
            "/dev/nvme",
            "--no-preserve-root",
            ":(){",
        )
    ):
        return False

    return True


def _assignment_value(command: str, name: str) -> str | None:
    return dict(_assignment_items(command)).get(name)


def _assignment_items(command: str) -> list[tuple[str, str]]:
    items = []
    for token in shlex.split(command):
        if token == "env":
            continue
        if _is_assignment_token(token):
            items.append(tuple(token.split("=", 1)))
        else:
            # Once a wrapper/executable starts, VAR=value is an argument.
            break
    return items


def _family_key(base: str, command: str) -> str:
    # Last assignment wins; environment names and values are case-sensitive.
    assignments = sorted(dict(_assignment_items(command)).items())
    prefix = []
    arguments = []
    after_command = False
    environment_prefix = True
    for token in shlex.split(command):
        if token.lower() == "%command%":
            after_command = True
        elif after_command:
            arguments.append(token)
        elif environment_prefix and (token == "env" or _is_assignment_token(token)):
            continue
        else:
            # Keep assignments and env inside wrapper arguments in their scope.
            if not environment_prefix or token != "gamemoderun":
                prefix.append(token)
            environment_prefix = False
    signature = repr((assignments, prefix, arguments))
    # Shell expansions depend on their original quoting and escaping.
    if "$" in command or "\\" in command:
        signature += repr(command)
    return f"{base}:{signature}"


def _command_semantics(command: str) -> dict[str, Any]:
    upper = command.upper()
    lower = command.lower()

    hardware_scope = None
    if any(token in upper for token in ("RADV_", "AMD_", "R600_")):
        hardware_scope = "AMD"
    elif any(
        token in upper
        for token in (
            "__NV_",
            "__GL_",
            "NVIDIA_",
            "DXVK_NVAPI",
            "PROTON_ENABLE_NVAPI",
            "PROTON_FORCE_NVAPI",
            "PROTON_DISABLE_NVAPI",
            "PROTON_HIDE_NVIDIA_GPU",
        )
    ):
        hardware_scope = "NVIDIA"
    elif any(token in upper for token in ("ANV_", "INTEL_")):
        hardware_scope = "Intel"

    if any(token in upper for token in _DIAGNOSTIC_TOKENS):
        return {
            "family_key": _family_key("diagnostic", command),
            "title": "Diagnostic option",
            "category": "diagnostic",
            "hardware_scope": hardware_scope,
            "description": (
                "Diagnostic or monitoring setting. It can be useful while "
                "troubleshooting, but should not be treated as a compatibility "
                "recommendation."
            ),
        }

    steamdeck_value = _assignment_value(command, "SteamDeck")
    if steamdeck_value is not None:
        if steamdeck_value.strip("\"'") == "0":
            title = "Force non-Steam-Deck mode"
            description = (
                "Makes the game see a non-Steam-Deck environment. This can be "
                "useful on desktop Linux when a game incorrectly enables "
                "Deck-specific UI, presets or restricted graphics options."
            )
        else:
            title = "Force Steam Deck mode"
            description = (
                "Makes the game see a Steam-Deck-like environment. Some games "
                "use this identity to select a different UI, preset or platform "
                "behaviour."
            )
        return {
            "family_key": _family_key("platform:steamdeck", command),
            "title": title,
            "category": "platform_override",
            "hardware_scope": hardware_scope,
            "description": description,
        }

    if "VKD3D_FEATURE_LEVEL" in upper or "VKD3D_SHADER_MODEL" in upper:
        return {
            "family_key": _family_key(
                "vkd3d:d3d12-feature-override", command
            ),
            "title": "D3D12 feature-level override",
            "category": "compatibility",
            "hardware_scope": hardware_scope,
            "description": (
                "Overrides the Direct3D feature level and/or shader model "
                "advertised through VKD3D-Proton. Variants may also change DXR "
                "behaviour. This is a game-specific compatibility tweak rather "
                "than a universal setting."
            ),
        }

    if "VKD3D_CONFIG" in upper:
        return {
            "family_key": _family_key("vkd3d:config", command),
            "title": "VKD3D configuration override",
            "category": "compatibility",
            "hardware_scope": hardware_scope,
            "description": (
                "Changes VKD3D-Proton behaviour. Treat it as a game-specific "
                "DirectX 12 workaround rather than a universal setting."
            ),
        }

    semantic_rules = (
        (
            "PROTON_USE_WINED3D",
            "proton:wined3d",
            "WineD3D fallback",
            "compatibility",
            "Uses WineD3D/OpenGL instead of the normal Vulkan translation path. "
            "Usually a fallback for specific rendering problems, not a default choice.",
        ),
        (
            "PROTON_NO_ESYNC",
            "proton:no-esync",
            "Disable esync",
            "compatibility",
            "Disables esync. This can work around some synchronization or stability "
            "issues, but is normally unnecessary unless a game specifically benefits.",
        ),
        (
            "PROTON_NO_FSYNC",
            "proton:no-fsync",
            "Disable fsync",
            "compatibility",
            "Disables fsync. This can work around some synchronization or stability "
            "issues, but is normally unnecessary unless a game specifically benefits.",
        ),
        (
            "PROTON_ENABLE_NVAPI",
            "proton:nvapi",
            "Enable NVIDIA NVAPI",
            "compatibility",
            "Enables NVIDIA NVAPI support exposed through Proton.",
        ),
        (
            "PROTON_HIDE_NVIDIA_GPU",
            "proton:hide-nvidia",
            "Hide NVIDIA GPU",
            "compatibility",
            "Hides the NVIDIA GPU from the Windows game. This is an NVIDIA-specific "
            "compatibility workaround.",
        ),
        (
            "WINEDLLOVERRIDES",
            "wine:dll-overrides",
            "Wine DLL override",
            "compatibility",
            "Changes which Windows or Wine DLL implementation the game loads.",
        ),
        (
            "DRI_PRIME",
            "mesa:dri-prime",
            "Mesa GPU selection",
            "compatibility",
            "Selects which GPU Mesa should use. It is mainly useful on multi-GPU "
            "systems and laptops.",
        ),
    )

    for token, family_key, title, category, description in semantic_rules:
        if token in upper:
            return {
                "family_key": _family_key(family_key, command),
                "title": title,
                "category": category,
                "hardware_scope": hardware_scope,
                "description": description,
            }

    for wrapper, title, description in (
        (
            "gamemoderun",
            "GameMode",
            "Runs the game through GameMode. This is a performance helper rather "
            "than a compatibility fix.",
        ),
        (
            "gamescope",
            "gamescope",
            "Runs the game through gamescope. This may help presentation, scaling "
            "or compositor-related cases, but is not universally required.",
        ),
        (
            "mangohud",
            "MangoHud",
            "Enables MangoHud for monitoring. This is not normally a compatibility fix.",
        ),
        (
            "vkbasalt",
            "vkBasalt",
            "Enables vkBasalt post-processing. This is a visual helper rather than "
            "a compatibility fix.",
        ),
    ):
        if wrapper in lower:
            return {
                "family_key": _family_key(f"helper:{wrapper}", command),
                "title": title,
                "category": "performance",
                "hardware_scope": hardware_scope,
                "description": description,
            }

    if hardware_scope:
        return {
            "family_key": _family_key(f"{hardware_scope.lower()}:override", command),
            "title": f"{hardware_scope}-specific override",
            "category": "compatibility",
            "hardware_scope": hardware_scope,
            "description": _hardware_description(hardware_scope),
        }

    if "DXVK_" in upper:
        return {
            "family_key": _family_key("dxvk", command),
            "title": "DXVK override",
            "category": "compatibility",
            "hardware_scope": None,
            "description": (
                "Changes DXVK behaviour. Treat it as a game-specific workaround "
                "unless recent reports consistently show it is required."
            ),
        }

    if "VKD3D_" in upper:
        return {
            "family_key": _family_key("vkd3d", command),
            "title": "VKD3D-Proton override",
            "category": "compatibility",
            "hardware_scope": None,
            "description": (
                "Changes VKD3D-Proton behaviour. Treat it as a game-specific "
                "DirectX 12 workaround unless recent reports consistently show "
                "it is required."
            ),
        }

    if "PROTON_" in upper:
        return {
            "family_key": _family_key("proton", command),
            "title": "Proton override",
            "category": "compatibility",
            "hardware_scope": None,
            "description": (
                "Changes Proton behaviour. It is best treated as a game-specific "
                "workaround rather than a general launch option."
            ),
        }

    return {
        "family_key": _family_key("community", command),
        "title": "Community launch option",
        "category": "community",
        "hardware_scope": hardware_scope,
        "description": "Community launch option reported for this game.",
    }


def _hardware_description(hardware_scope: str) -> str:
    if hardware_scope == "AMD":
        return "AMD/RADV-specific environment option; mainly relevant to AMD Vulkan systems."
    if hardware_scope == "NVIDIA":
        return "NVIDIA-specific driver or Proton environment option."
    if hardware_scope == "Intel":
        return "Intel-specific graphics environment option."
    return "Hardware-specific community launch option."


def _report_is_positive(report: dict[str, Any]) -> bool:
    verdict = _nested_get(report, "responses", "verdict")
    if isinstance(verdict, str):
        value = verdict.strip().lower()
        if value in {"yes", "works", "playable", "good"}:
            return True
        if value in {"no", "borked", "broken"}:
            return False

    tier = (
        report.get("rating")
        or report.get("tier")
        or _nested_get(report, "responses", "rating")
    )
    if tier is not None:
        return _TIER_ORDER.get(str(tier).lower(), -1) >= _TIER_ORDER["silver"]

    return False


def _report_gpu_vendor(report: dict[str, Any]) -> str:
    candidates = (
        _nested_get(report, "device", "inferred", "steam", "gpu"),
        _nested_get(report, "device", "gpu"),
        report.get("gpu"),
    )
    for value in candidates:
        if value:
            vendor = _gpu_vendor(str(value))
            if vendor != "Unknown":
                return vendor
    return "Unknown"


def _gpu_vendor(text: str) -> str:
    lower = str(text).lower()
    for vendor, patterns in _VENDOR_PATTERNS.items():
        if any(pattern in lower for pattern in patterns):
            return vendor
    return "Unknown"


def _report_timestamp(report: dict[str, Any]) -> int | None:
    value = report.get("timestamp")
    if value is None:
        value = report.get("createdAt") or report.get("created_at")

    if isinstance(value, (int, float)):
        value = int(value)
        # Accept milliseconds as well as seconds.
        if value > 10_000_000_000:
            value //= 1000
        return value

    if isinstance(value, str) and value.strip():
        stripped = value.strip()
        if stripped.isdigit():
            return _report_timestamp({"timestamp": int(stripped)})
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return None

    return None


def _age_days(timestamp: int | None, now: float) -> int:
    if not timestamp:
        return 99999
    return max(0, int((now - timestamp) / 86400))


def _normalise_tier(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip().lower()
    return value or None


def _nested_get(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    path = "/etc/os-release"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key] = value.strip().strip('"')
    except OSError:
        pass
    return data


def _read_text_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            value = handle.read().strip()
            return value or None
    except OSError:
        return None


def _request_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            f"{USER_AGENT} (+{GITHUB_URL})"
        ),
        "Accept": "application/json",
    }


def _protondb_report_hash(
    appid: int,
    reports_token: int,
    timestamp_token: int,
    page: int,
) -> int:
    """Reproduce ProtonDB's current static-report URL hash in Python.

    ProtonDB's frontend computes the path from counts.json and the requested
    page. Keeping this function isolated makes it easy to update if their
    static-data layout changes.
    """

    def r(first: int, second: int, modulus: int) -> str:
        return f"{second}p{first * (second % modulus)}"

    material = (
        "p"
        + r(int(appid), int(reports_token), int(timestamp_token))
        + "*vRT"
        + r(int(page), int(appid), int(timestamp_token))
        + "undefined"
    )
    return _js_string_hash_abs(material + "m")


def _js_string_hash_abs(text: str) -> int:
    value = 0
    for character in text:
        value = ((value << 5) - value + ord(character)) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    return abs(value)
