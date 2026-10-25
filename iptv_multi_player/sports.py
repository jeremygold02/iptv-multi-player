from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse
from typing import Any

from .config import (
    ESPN_CONFIG,
    ESPN_REFRESH_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MATCHUP_RE,
    SPORTS_CONFIG,
    SPORT_BY_GROUP,
    SPORTS_DAILY_CALL_LIMIT,
    SPORTS_REFRESH_SECONDS,
    TEAM_ALIASES,
)
from .state import (
    api_sports_key,
    local_date,
    local_time_text,
    now_ts,
    read_sports_cache,
    sports_query_dates,
    write_sports_cache,
)

_request_lock = threading.Lock()
_request_slots: dict[str, float] = {}
_provider_backoff: dict[str, dict[str, int]] = {}


def retry_after_seconds(value: str, now: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, int(parsedate_to_datetime(value).timestamp()) - now)
        except (TypeError, ValueError, OverflowError):
            return 0


def provider_ready(url: str, now: int) -> bool:
    with _request_lock:
        return now >= _provider_backoff.get(urlparse(url).netloc, {}).get("retry_at", 0)


def fetch_sports_json(req: urllib.request.Request, spacing: float) -> dict[str, Any]:
    host = urlparse(req.full_url).netloc
    with _request_lock:
        if now_ts() < _provider_backoff.get(host, {}).get("retry_at", 0):
            raise RuntimeError("Score provider is cooling down after a rate-limit response.")
        now = time.monotonic()
        slot = max(now, _request_slots.get(host, 0))
        _request_slots[host] = slot + spacing
    if slot > now:
        time.sleep(slot - now)
    # A preceding request may have received a 429 while this worker waited.
    if not provider_ready(req.full_url, now_ts()):
        raise RuntimeError("Score provider is cooling down after a rate-limit response.")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in {429, 503}:
            now = now_ts()
            with _request_lock:
                failures = _provider_backoff.get(host, {}).get("failures", 0) + 1
                delay = max(min(3600, 300 * 2 ** min(failures - 1, 4)), retry_after_seconds((exc.headers or {}).get("Retry-After", ""), now))
                retry_at = max(now + delay, _provider_backoff.get(host, {}).get("retry_at", 0))
                _provider_backoff[host] = {"retry_at": retry_at, "failures": failures}
        raise
    with _request_lock:
        if now_ts() >= _provider_backoff.get(host, {}).get("retry_at", 0):
            _provider_backoff.pop(host, None)
    return payload


@lru_cache(maxsize=8192)
def normalize_team_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(fc|sc|cf|club|the)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return TEAM_ALIASES.get(text, text)


def team_match_score(left: str, right: str) -> float:
    left_norm = normalize_team_name(left)
    right_norm = normalize_team_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return 0.92
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def channel_game_title(name: str) -> str:
    name = name.strip()
    if name.endswith(")"):
        depth = 0
        for index in range(len(name) - 1, -1, -1):
            if name[index] == ")":
                depth += 1
            elif name[index] == "(":
                depth -= 1
                if depth == 0:
                    title = name[:index].strip()
                    label = name[index + 1:-1].strip()
                    return title if title and label else name
    return name


def parse_channel_matchup(channel: dict[str, Any]) -> dict[str, str] | None:
    sport = SPORT_BY_GROUP.get(channel.get("group", ""))
    if not sport:
        return None

    title = channel_game_title(str(channel.get("name", "")))
    if SPORTS_CONFIG[sport].get("event_kind") == "race":
        return {
            "sport": sport,
            "home": "",
            "away": "",
            "title": title,
        }

    if sport == "mma":
        match = MATCHUP_RE.search(title)
        return {
            "sport": sport,
            "home": match.group(1).split(":", 1)[-1].strip() if match else "",
            "away": match.group(2).strip() if match else "",
            "title": title,
        }

    match = MATCHUP_RE.match(title)
    if match is None:
        return None

    return {
        "sport": sport,
        "home": match.group(1).strip(),
        "away": match.group(2).strip(),
        "title": title,
    }


def parse_event_timestamp(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def espn_query_range(query_dates: list[str]) -> str:
    values = [date_value.replace("-", "") for date_value in query_dates]
    if not values:
        return local_date().replace("-", "")
    if len(values) == 1 or values[0] == values[-1]:
        return values[0]
    return f"{values[0]}-{values[-1]}"


def espn_competitor_name(competitor: dict[str, Any]) -> str:
    team = competitor.get("team") or competitor.get("athlete") or {}
    return str(
        team.get("displayName")
        or team.get("shortDisplayName")
        or team.get("name")
        or team.get("fullName")
        or ""
    )


def espn_score(competitor: dict[str, Any]) -> Any:
    score = competitor.get("score")
    if score in (None, ""):
        return None
    return score


def espn_live_code(sport: str, status: dict[str, Any]) -> str:
    status_type = status.get("type") or {}
    description = str(status_type.get("description") or "").lower()
    period = int(status.get("period") or 0)

    if "half" in description and "time" in description:
        return "HT"
    if sport == "football":
        if period == 1:
            return "1H"
        if period >= 2:
            return "2H"
        return "LIVE"
    if sport == "baseball":
        return f"IN{period}" if period else "LIVE"
    if sport in {"afl", "basketball", "nba", "nfl"}:
        if 1 <= period <= 4:
            return f"Q{period}"
        return "OT" if period else "LIVE"
    if sport == "hockey":
        if 1 <= period <= 3:
            return f"P{period}"
        return "OT" if period else "LIVE"
    return "LIVE"


def espn_status(event: dict[str, Any], competition: dict[str, Any], sport: str) -> dict[str, Any]:
    status = competition.get("status") or event.get("status") or {}
    status_type = status.get("type") or {}
    state = str(status_type.get("state") or "").lower()
    description = str(status_type.get("description") or "").strip()
    detail = str(status_type.get("shortDetail") or status_type.get("detail") or "").strip()

    if state == "pre":
        code = "SCHEDULED" if sport == "formula_1" else "NS"
    elif state == "post":
        code = "COMPLETED" if sport == "formula_1" else "FT"
    elif state == "in":
        code = espn_live_code(sport, status)
    else:
        code = str(status_type.get("id") or "").upper() or "UNKNOWN"

    return {
        "short": code,
        "long": description or detail or code,
        "timer": detail,
        "clock": detail,
        "elapsed": detail,
        "raw": status,
    }


def normalize_espn_event(
    sport: str,
    event: dict[str, Any],
    competition: dict[str, Any],
    endpoint_key: str,
) -> dict[str, Any] | None:
    competitors = competition.get("competitors") or []
    timestamp = parse_event_timestamp(competition.get("date") or event.get("date"))
    normalized: dict[str, Any] = {
        "_provider": "espn",
        "_sport": sport,
        "_endpoint": endpoint_key,
        "id": competition.get("id") or event.get("id"),
        "timestamp": timestamp,
        "status": espn_status(event, competition, sport),
    }

    if sport == "formula_1":
        normalized.update({
            "competition": {"name": event.get("name") or event.get("shortName") or "Formula 1"},
            "name": event.get("name") or event.get("shortName") or "Formula 1",
        })
        return normalized

    if sport == "mma":
        fighters = [competitor for competitor in competitors if espn_competitor_name(competitor)]
        first = fighters[0] if len(fighters) > 0 else {}
        second = fighters[1] if len(fighters) > 1 else {}
        normalized.update({
            "slug": event.get("name") or event.get("shortName") or "",
            "is_main": bool(competition.get("featured")),
            "fighters": {
                "first": {"name": espn_competitor_name(first)},
                "second": {"name": espn_competitor_name(second)},
            },
        })
        return normalized

    home = next((competitor for competitor in competitors if competitor.get("homeAway") == "home"), None)
    away = next((competitor for competitor in competitors if competitor.get("homeAway") == "away"), None)
    if home is None or away is None:
        ordered = sorted(competitors, key=lambda item: int(item.get("order") or 0))
        home = home or (ordered[0] if ordered else {})
        away = away or (ordered[1] if len(ordered) > 1 else {})

    home_name = espn_competitor_name(home or {})
    away_name = espn_competitor_name(away or {})
    if not home_name or not away_name:
        return None

    normalized.update({
        "teams": {
            "home": {"name": home_name},
            "away": {"name": away_name},
        },
        "scores": {
            "home": {"total": espn_score(home or {})},
            "away": {"total": espn_score(away or {})},
        },
    })
    return normalized


def fetch_espn_events(sport: str, endpoint_key: str, url: str, date_range: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    # Scoreboards reject date ranges; request today and tomorrow individually.
    for query_date in dict.fromkeys(date_range.split("-")):
        req_url = f"{url}?{urlencode({'dates': query_date, 'limit': 500})}"
        req = urllib.request.Request(
            req_url,
            headers={
                "User-Agent": "IPTV-Multi-Player/1.0",
                "Accept": "application/json",
            },
        )
        payload = fetch_sports_json(req, spacing=0.25)
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            for competition in event.get("competitions") or [{}]:
                if not isinstance(competition, dict):
                    continue
                normalized = normalize_espn_event(sport, event, competition, endpoint_key)
                if normalized is None:
                    continue
                identifier = str(normalized.get("id") or "")
                if identifier and identifier in seen_ids:
                    continue
                if identifier:
                    seen_ids.add(identifier)
                events.append(normalized)
    return events


def sports_calls_today(sport_cache: dict[str, Any]) -> int:
    return int(sport_cache.setdefault("calls", {}).get(local_date(), 0) or 0)


def increment_sports_calls(sport_cache: dict[str, Any]) -> None:
    calls = sport_cache.setdefault("calls", {})
    today = local_date()
    calls[today] = int(calls.get(today, 0) or 0) + 1
    for day in list(calls):
        if day != today:
            del calls[day]


def fetch_sports_events(sport: str, date_value: str, key: str) -> list[dict[str, Any]]:
    config = SPORTS_CONFIG[sport]
    url = f"{config['base_url']}?{urlencode({'date': date_value})}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "IPTV-Multi-Player/1.0",
            "Accept": "application/json",
            "x-apisports-key": key,
        },
    )
    payload = fetch_sports_json(req, spacing=1.0)

    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"API-SPORTS returned errors: {errors}")

    events = payload.get("response", [])
    if not isinstance(events, list):
        raise RuntimeError("API-SPORTS returned an unexpected response shape.")
    for event in events:
        if isinstance(event, dict):
            event["_sport"] = sport
    return events


def ensure_sports_cache_for(channels: list[dict[str, Any]], *, refresh: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    requested_sports = requested_sports_for_channels(channels)
    requested_espn_sports = {sport for sport in requested_sports if sport in ESPN_CONFIG}
    cache = read_sports_cache()
    key = api_sports_key()
    now = now_ts()
    with _request_lock:
        for host, saved in cache.get("provider_backoff", {}).items():
            if saved.get("retry_at", 0) > _provider_backoff.get(host, {}).get("retry_at", 0):
                _provider_backoff[host] = dict(saved)
    query_dates = sports_query_dates()
    query_date_set = set(query_dates)
    espn_range = espn_query_range(query_dates)
    changed = False
    meta = {
        "configured": bool(key) or bool(requested_espn_sports),
        "api_sports_configured": bool(key),
        "espn_configured": True,
        "refresh_seconds": ESPN_REFRESH_SECONDS if requested_espn_sports else SPORTS_REFRESH_SECONDS,
        "espn_refresh_seconds": ESPN_REFRESH_SECONDS,
        "api_sports_refresh_seconds": SPORTS_REFRESH_SECONDS,
        "daily_call_limit": SPORTS_DAILY_CALL_LIMIT,
        "sports": {},
    }

    # One bounded pool lets independent sports load together instead of in series.
    fetches = []
    for sport in sorted(requested_sports):
        espn_sport_cache = cache.setdefault("espn", {}).setdefault(sport, {"endpoints": {}, "last_error": ""})
        endpoint_caches = espn_sport_cache.setdefault("endpoints", {})
        for endpoint_key, url in ESPN_CONFIG.get(sport, {}).get("endpoints", ()):
            endpoint_cache = endpoint_caches.setdefault(endpoint_key, {
                "date_range": "", "last_fetch": 0, "events": [], "last_error": "",
            })
            last_fetch = int(endpoint_cache.get("last_fetch", 0) or 0)
            interval = ESPN_REFRESH_SECONDS
            last_attempt = max(last_fetch, int(endpoint_cache.get("last_attempt", 0) or 0))
            due = endpoint_cache.get("date_range") != espn_range or now - last_fetch >= interval
            if refresh and due and now - last_attempt >= interval and provider_ready(url, now):
                fetches.append((fetch_espn_events, (sport, endpoint_key, url, espn_range), endpoint_cache))

        sport_cache = cache["sports"].setdefault(sport, {"calls": {}, "dates": {}, "last_error": ""})
        calls_used = sports_calls_today(sport_cache)
        for date_value in query_dates:
            date_cache = sport_cache.setdefault("dates", {}).setdefault(date_value, {"last_fetch": 0, "events": []})
            last_fetch = int(date_cache.get("last_fetch", 0) or 0)
            last_attempt = max(last_fetch, int(date_cache.get("last_attempt", 0) or 0))
            stale = now - last_fetch >= SPORTS_REFRESH_SECONDS
            if refresh and key and stale and now - last_attempt >= SPORTS_REFRESH_SECONDS and provider_ready(SPORTS_CONFIG[sport]["base_url"], now):
                if calls_used < SPORTS_DAILY_CALL_LIMIT:
                    increment_sports_calls(sport_cache)
                    calls_used += 1
                    fetches.append((fetch_sports_events, (sport, date_value, key), date_cache))
                else:
                    sport_cache["last_error"] = "Daily API call limit reached for this sport."

    if fetches:
        with ThreadPoolExecutor(max_workers=min(8, len(fetches))) as executor:
            futures = {
                executor.submit(fetch, *args): (fetch, entry)
                for fetch, args, entry in fetches
            }
            for future in as_completed(futures):
                fetch, entry = futures[future]
                entry["last_attempt"] = now
                try:
                    entry["events"] = future.result()
                    entry["last_fetch"] = now
                    entry["last_error"] = ""
                    if fetch is fetch_espn_events:
                        entry["date_range"] = espn_range
                except Exception as exc:  # noqa: BLE001 - providers are nonfatal enrichment
                    entry["last_error"] = str(exc)
                changed = True
        with _request_lock:
            cache["provider_backoff"] = {host: dict(value) for host, value in _provider_backoff.items()}

    for sport in requested_sports:
        espn_event_count = 0
        espn_last_fetches: list[int] = []
        espn_current = False
        espn_stale = False
        espn_error = ""
        espn_config = ESPN_CONFIG.get(sport)
        if espn_config:
            espn_sport_cache = cache.setdefault("espn", {}).setdefault(sport, {"endpoints": {}, "last_error": ""})
            endpoint_caches = espn_sport_cache.setdefault("endpoints", {})
            expected_endpoint_keys = {endpoint_key for endpoint_key, _url in espn_config.get("endpoints", ())}
            espn_intervals = []
            for endpoint_key in expected_endpoint_keys:
                endpoint_cache = endpoint_caches.setdefault(endpoint_key, {
                    "date_range": "",
                    "last_fetch": 0,
                    "events": [],
                    "last_error": "",
                })
                espn_event_count += len(endpoint_cache.get("events", []))
                endpoint_last_fetch = int(endpoint_cache.get("last_fetch", 0) or 0)
                espn_last_fetches.append(endpoint_last_fetch)
                espn_current = espn_current or bool(endpoint_last_fetch and endpoint_cache.get("date_range") == espn_range)
                interval = ESPN_REFRESH_SECONDS
                espn_intervals.append(interval)
                espn_stale = espn_stale or endpoint_cache.get("date_range") != espn_range or now - endpoint_last_fetch >= interval
                if endpoint_cache.get("last_error"):
                    espn_error = str(endpoint_cache["last_error"])

            for endpoint_key in list(endpoint_caches):
                if endpoint_key not in expected_endpoint_keys:
                    del endpoint_caches[endpoint_key]
                    changed = True
            espn_sport_cache["last_error"] = espn_error

        sport_cache = cache["sports"].setdefault(sport, {"calls": {}, "dates": {}, "last_error": ""})
        calls_used = sports_calls_today(sport_cache)

        for date_key in list(sport_cache.get("dates", {})):
            if date_key not in query_date_set:
                del sport_cache["dates"][date_key]
                changed = True

        date_errors = [entry.get("last_error", "") for entry in sport_cache["dates"].values() if entry.get("last_error")]
        if date_errors:
            sport_cache["last_error"] = "; ".join(dict.fromkeys(date_errors))
        elif key and calls_used >= SPORTS_DAILY_CALL_LIMIT and any(
            now - int(entry.get("last_fetch", 0) or 0) >= SPORTS_REFRESH_SECONDS
            for entry in sport_cache["dates"].values()
        ):
            sport_cache["last_error"] = "Daily API call limit reached for this sport."
        else:
            sport_cache["last_error"] = ""

        date_entries = [sport_cache.get("dates", {}).get(date_value, {}) for date_value in query_dates]
        last_fetches = [int(item.get("last_fetch", 0) or 0) for item in date_entries]
        api_event_count = sum(len(item.get("events", [])) for item in date_entries)
        api_stale = any(now - last_fetch >= SPORTS_REFRESH_SECONDS for last_fetch in last_fetches)
        errors = [error for error in [espn_error, sport_cache.get("last_error", "")] if error]
        all_fetches = [last_fetch for last_fetch in [*espn_last_fetches, *last_fetches] if last_fetch]

        meta["sports"][sport] = {
            "calls_used": calls_used,
            "dates": query_dates,
            "last_fetch": min(all_fetches) if all_fetches else 0,
            "event_count": espn_event_count + api_event_count,
            "error": "; ".join(errors),
            "stale": espn_stale or (bool(key) and api_stale),
            "loading": not (espn_current or any(last_fetches)) and not errors and (bool(key) or bool(espn_config)),
            "providers": {
                "espn": {
                    "configured": sport in ESPN_CONFIG,
                    "date_range": espn_range,
                    "refresh_seconds": min(espn_intervals) if espn_config and espn_intervals else ESPN_REFRESH_SECONDS,
                    "last_fetch": min(espn_last_fetches) if espn_last_fetches else 0,
                    "event_count": espn_event_count,
                    "error": espn_error,
                    "stale": espn_stale,
                },
                "api_sports": {
                    "configured": bool(key),
                    "refresh_seconds": SPORTS_REFRESH_SECONDS,
                    "calls_used": calls_used,
                    "last_fetch": min(last_fetches) if last_fetches else 0,
                    "event_count": api_event_count,
                    "error": sport_cache.get("last_error", ""),
                    "stale": bool(key) and api_stale,
                },
            },
        }

    meta["loading"] = any(item["loading"] for item in meta["sports"].values())
    intervals = [
        provider["refresh_seconds"]
        for item in meta["sports"].values()
        for provider in item["providers"].values()
        if provider["configured"]
    ]
    meta["refresh_seconds"] = min(intervals) if intervals else SPORTS_REFRESH_SECONDS

    if changed and refresh:
        write_sports_cache(cache)

    return cache, meta


def event_team_names(event: dict[str, Any]) -> tuple[str, str]:
    if event.get("_sport") == "formula_1":
        return event_title(event, "formula_1"), ""

    if event.get("_sport") == "mma" or "fighters" in event:
        fighters = event.get("fighters") or {}
        first = fighters.get("first") or {}
        second = fighters.get("second") or {}
        return str(first.get("name") or ""), str(second.get("name") or "")

    teams = event.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or teams.get("visitors") or {}
    return str(home.get("name") or ""), str(away.get("name") or "")


def event_title(event: dict[str, Any], sport: str) -> str:
    if sport == "formula_1":
        competition = event.get("competition") or {}
        race_type = str(event.get("type") or "").strip()
        name = str(competition.get("name") or event.get("name") or "").strip()
        if name and race_type and race_type.lower() not in name.lower():
            return f"{name} {race_type}"
        return name or race_type or "Formula 1"
    if sport == "mma":
        return str(event.get("slug") or "").strip()
    home, away = event_team_names(event)
    return f"{home} vs {away}".strip()


def event_status(event: dict[str, Any], sport: str) -> dict[str, Any]:
    if event.get("_provider") == "espn":
        status = event.get("status") or {}
        return status if isinstance(status, dict) else {}
    if sport == "football":
        status = ((event.get("fixture") or {}).get("status") or {})
    elif sport == "mma":
        status = event.get("status") or {}
    else:
        status = event.get("status") or {}
    if isinstance(status, dict):
        return status
    if status in (None, ""):
        return {}
    return {"short": str(status), "long": str(status)}


def event_timestamp(event: dict[str, Any], sport: str) -> Any:
    if event.get("_provider") == "espn":
        return parse_event_timestamp(event.get("timestamp") or event.get("date"))
    if sport == "football":
        return parse_event_timestamp((event.get("fixture") or {}).get("timestamp"))
    if sport == "mma":
        return parse_event_timestamp(event.get("timestamp") or event.get("date"))
    if sport == "nba":
        return parse_event_timestamp((event.get("date") or {}).get("start"))
    return parse_event_timestamp(event.get("timestamp") or event.get("date"))


def event_id(event: dict[str, Any], sport: str) -> Any:
    if event.get("_provider") == "espn":
        return event.get("id")
    if sport == "football":
        return (event.get("fixture") or {}).get("id")
    if sport == "mma":
        return event.get("id")
    return event.get("id")


def event_score(event: dict[str, Any], sport: str) -> str:
    if sport in {"formula_1", "mma"}:
        return ""

    if sport == "football" and event.get("_provider") != "espn":
        goals = event.get("goals") or {}
        home = goals.get("home")
        away = goals.get("away")
    else:
        scores = event.get("scores") or {}
        home = score_value(scores.get("home"))
        away = score_value(scores.get("away") if "away" in scores else scores.get("visitors"))

    if home is None or away is None:
        return ""
    return f"{home}-{away}"


def score_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("total", "points", "score", "goals"):
            if value.get(key) is not None:
                return value.get(key)
        linescore = value.get("linescore")
        if isinstance(linescore, list):
            try:
                return sum(int(item) for item in linescore if str(item).strip())
            except (TypeError, ValueError):
                return None
        return None
    return value


def game_metadata(event: dict[str, Any], sport: str, confidence: float) -> dict[str, Any]:
    config = SPORTS_CONFIG[sport]
    status = event_status(event, sport)
    code = str(status.get("short") or "").upper()
    status_long = str(status.get("long") or config.get("status_labels", {}).get(code) or code or "Unknown")
    elapsed = status.get("elapsed")
    timer = status.get("timer")
    clock = status.get("clock")
    score = event_score(event, sport)
    raw_timestamp = event_timestamp(event, sport)
    start_time = local_time_text(raw_timestamp) if raw_timestamp else ""
    home, away = event_team_names(event)
    start_timestamp = parse_event_timestamp(raw_timestamp) if raw_timestamp else 0

    if code in config["live_codes"]:
        if sport == "mma":
            text = "Live" if code == "LIVE" else status_long
        elif sport == "formula_1":
            text = status_long
        else:
            detail = str(clock or timer or elapsed or code)
            suffix = f" {detail}'" if sport == "football" and str(detail).isdigit() else f" {detail}" if detail else ""
            text = f"Live{suffix}"
        kind = "live"
    elif code in config["scheduled_codes"]:
        text = f"Starts {start_time}" if start_time else "Scheduled"
        kind = "scheduled"
    elif code in config["finished_codes"]:
        text = "Final"
        kind = "final"
    elif code in config["inactive_codes"]:
        text = status_long
        kind = "inactive"
    else:
        text = status_long or "Unknown"
        kind = "unknown"

    display_score = "" if kind == "scheduled" and score in {"0-0", "0.0-0.0"} else score

    return {
        "kind": kind,
        "sport": sport,
        "text": text,
        "subtext": display_score or status_long,
        "start_time": start_time,
        "status_short": code,
        "status_long": status_long,
        "score": display_score,
        "start_timestamp": start_timestamp,
        "home": home,
        "away": away,
        "event_id": event_id(event, sport),
        "confidence": round(confidence, 3),
        "matched": True,
    }


def unmatched_game_metadata(channel: dict[str, Any], reason: str) -> dict[str, Any]:
    sport = SPORT_BY_GROUP.get(channel.get("group", ""))
    return {
        "kind": "unknown",
        "sport": sport,
        "text": "Unknown",
        "subtext": reason,
        "start_time": "",
        "status_short": "",
        "status_long": reason,
        "score": "",
        "start_timestamp": 0,
        "home": "",
        "away": "",
        "event_id": None,
        "confidence": 0,
        "matched": False,
    }


def stream_game_metadata() -> dict[str, Any]:
    return {
        "kind": "stream",
        "sport": "",
        "text": "Stream",
        "subtext": "No game lookup",
        "start_time": "",
        "status_short": "",
        "status_long": "No game lookup",
        "score": "",
        "start_timestamp": 0,
        "home": "",
        "away": "",
        "event_id": None,
        "confidence": 0,
        "matched": False,
    }


def loading_game_metadata(channel: dict[str, Any]) -> dict[str, Any]:
    sport = SPORT_BY_GROUP.get(channel.get("group", ""))
    return {
        "kind": "loading",
        "sport": sport,
        "text": "Loading",
        "subtext": "Fetching game data",
        "start_time": "",
        "status_short": "",
        "status_long": "Fetching game data",
        "score": "",
        "start_timestamp": 0,
        "home": "",
        "away": "",
        "event_id": None,
        "confidence": 0,
        "matched": False,
        "loading": True,
    }


def requested_sports_for_channels(channels: list[dict[str, Any]]) -> set[str]:
    return {
        sport
        for channel in channels
        for sport in [SPORT_BY_GROUP.get(channel.get("group", ""))]
        if sport
    }


def channels_with_pending_games(channels: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return enrich_channels_with_games(channels, refresh=False)


def status_code(event: dict[str, Any], sport: str) -> str:
    return str(event_status(event, sport).get("short") or "").upper()


def select_mma_card_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    config = SPORTS_CONFIG["mma"]

    def rank(event: dict[str, Any]) -> tuple[int, int, int]:
        code = status_code(event, "mma")
        timestamp = int(event.get("timestamp") or 0)
        main_rank = 0 if event.get("is_main") else 1
        if code in config["live_codes"]:
            return 0, main_rank, timestamp
        if code in config["scheduled_codes"]:
            return 1, timestamp, main_rank
        if code in config["inactive_codes"]:
            return 2, timestamp, main_rank
        if code in config["finished_codes"]:
            return 3, main_rank, timestamp
        return 4, timestamp, main_rank

    selected = min(events, key=rank)
    next_event = dict(selected)
    next_event["_sport"] = "mma"
    return next_event


def best_title_event_match(matchup: dict[str, str], events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    best_event = None
    best_score = 0.0
    for event in events:
        sport = matchup["sport"]
        title = event_title(event, sport)
        score = team_match_score(matchup.get("title", ""), title)
        if score > best_score:
            best_event = event
            best_score = score

    if best_score < 0.55:
        return None, best_score
    return best_event, best_score


def best_mma_event_match(matchup: dict[str, str], events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    candidates: dict[str, dict[str, Any]] = {}
    for event in events:
        slug = str(event.get("slug") or "").strip()
        first, second = event_team_names(event)
        title_score = team_match_score(matchup.get("title", ""), slug)
        fighter_score = 0.0
        if matchup.get("home") and matchup.get("away"):
            direct = (team_match_score(matchup["home"], first) + team_match_score(matchup["away"], second)) / 2
            swapped = (team_match_score(matchup["home"], second) + team_match_score(matchup["away"], first)) / 2
            fighter_score = max(direct, swapped)
        score = max(title_score, fighter_score)
        key = slug or str(event.get("id") or "")
        if not key:
            continue
        current = candidates.get(key)
        if current is None:
            candidates[key] = {"score": score, "events": [event]}
        else:
            current["score"] = max(current["score"], score)
            current["events"].append(event)

    if not candidates:
        return None, 0.0

    best = max(candidates.values(), key=lambda item: item["score"])
    if best["score"] < 0.78:
        return None, best["score"]
    return select_mma_card_event(best["events"]), best["score"]


@lru_cache(maxsize=256)
def best_team_event_index(home: str, away: str, candidates: tuple[tuple[str, str], ...]) -> tuple[int | None, float]:
    # Cache only the association; scores and status always come from the current event.
    best_index = None
    best_score = 0.0
    for index, (first, second) in enumerate(candidates):
        direct = (team_match_score(home, first) + team_match_score(away, second)) / 2
        swapped = (team_match_score(home, second) + team_match_score(away, first)) / 2
        score = max(direct, swapped)
        if score > best_score:
            best_index = index
            best_score = score
            if best_score == 1.0:
                break
    if best_score < 0.82:
        return None, best_score
    return best_index, best_score


def best_event_match(matchup: dict[str, str], events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    if matchup.get("sport") == "mma":
        return best_mma_event_match(matchup, events)
    if SPORTS_CONFIG[matchup["sport"]].get("event_kind") == "race":
        return best_title_event_match(matchup, events)

    candidates = tuple(event_team_names(event) for event in events)
    index, confidence = best_team_event_index(matchup["home"], matchup["away"], candidates)
    return (events[index] if index is not None else None), confidence


def cached_espn_events(cache: dict[str, Any], sport: str) -> list[dict[str, Any]]:
    sport_cache = cache.get("espn", {}).get(sport, {})
    endpoints = sport_cache.get("endpoints", {})
    date_range = espn_query_range(sports_query_dates())
    return [
        event
        for endpoint_cache in endpoints.values()
        if endpoint_cache.get("date_range") == date_range
        for event in endpoint_cache.get("events", [])
        if isinstance(event, dict)
    ]


def cached_api_sports_events(cache: dict[str, Any], sport: str, query_dates: list[str]) -> list[dict[str, Any]]:
    sport_cache = cache.get("sports", {}).get(sport, {})
    return [
        event
        for date_value in query_dates
        for event in sport_cache.get("dates", {}).get(date_value, {}).get("events", [])
        if isinstance(event, dict)
    ]


def enrich_channels_with_games(channels: list[dict[str, Any]], *, refresh: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache, meta = ensure_sports_cache_for(channels, refresh=refresh)
    query_dates = sports_query_dates()
    enriched = []
    games_by_matchup: dict[tuple[str, str], dict[str, Any]] = {}
    events_by_sport: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}

    for channel in channels:
        next_channel = dict(channel)
        matchup = parse_channel_matchup(channel)
        if matchup is None:
            next_channel["game"] = stream_game_metadata() if channel.get("group") not in SPORT_BY_GROUP else unmatched_game_metadata(channel, "Could not parse matchup")
            enriched.append(next_channel)
            continue

        sport = matchup["sport"]
        matchup_key = (sport, " ".join(matchup["title"].casefold().split()))
        if matchup_key in games_by_matchup:
            next_channel["game"] = dict(games_by_matchup[matchup_key])
            enriched.append(next_channel)
            continue

        if sport not in events_by_sport:
            events_by_sport[sport] = (
                cached_espn_events(cache, sport),
                cached_api_sports_events(cache, sport, query_dates),
            )
        espn_events, api_sports_events = events_by_sport[sport]
        event, confidence = best_event_match(matchup, espn_events)
        if event is None:
            event, confidence = best_event_match(matchup, api_sports_events)
        if event is None:
            reason = "No matching game found"
            if sport not in ESPN_CONFIG and not meta.get("api_sports_configured"):
                reason = "API key not configured"
            next_channel["game"] = (
                loading_game_metadata(channel)
                if not refresh and meta.get("sports", {}).get(sport, {}).get("loading")
                else unmatched_game_metadata(channel, reason)
            )
        else:
            next_channel["game"] = game_metadata(event, sport, confidence)
        games_by_matchup[matchup_key] = next_channel["game"]
        enriched.append(next_channel)

    return enriched, meta
