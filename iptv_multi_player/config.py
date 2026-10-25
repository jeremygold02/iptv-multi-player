from __future__ import annotations

import re
import sys
from pathlib import Path

def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_asset_dir() -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir)
    return Path(__file__).resolve().parent.parent


APP_DIR = get_app_dir()
ASSET_DIR = get_asset_dir()
DATA_DIR = APP_DIR / "data"
SOURCE_DIR = DATA_DIR / "sources"
LIBRARY_PATH = DATA_DIR / "library.json"
QUEUE_EXPORT_PATH = DATA_DIR / "iptv-multi-player-queue.m3u"
SPORTS_CACHE_PATH = DATA_DIR / "sports_cache.json"
ENV_PATH = APP_DIR / ".env"

MAX_QUEUE_ITEMS = 16
HTTP_TIMEOUT_SECONDS = 18
ESPN_REFRESH_SECONDS = 60
SPORTS_REFRESH_SECONDS = 15 * 60
SPORTS_DAILY_CALL_LIMIT = 100
DESKTOP_MODE = False
API_SPORTS_KEY_NAME = "API_SPORTS_KEY"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
MATCHUP_RE = re.compile(r"^(.+?)\s+(?:vs\.?|v|@)\s+(.+)$", re.IGNORECASE)

SPORTS_CONFIG = {
    "football": {
        "group": "Football",
        "groups": ("Football",),
        "base_url": "https://v3.football.api-sports.io/fixtures",
        "live_codes": {"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"},
        "scheduled_codes": {"TBD", "NS"},
        "finished_codes": {"FT", "AET", "PEN"},
        "inactive_codes": {"PST", "CANC", "ABD", "AWD", "WO"},
        "event_kind": "matchup",
    },
    "afl": {
        "group": "Australian Football",
        "groups": ("Australian Football", "AFL"),
        "base_url": "https://v1.afl.api-sports.io/games",
        "live_codes": {"Q1", "Q2", "Q3", "Q4", "QT", "ER", "HT"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT"},
        "inactive_codes": {"CANC", "PST"},
        "event_kind": "matchup",
    },
    "baseball": {
        "group": "Baseball",
        "groups": ("Baseball",),
        "base_url": "https://v1.baseball.api-sports.io/games",
        "live_codes": {
            "IN1", "IN2", "IN3", "IN4", "IN5", "IN6", "IN7", "IN8",
            "IN9", "IN10", "IN11", "IN12", "IN13", "IN14", "IN15",
        },
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT"},
        "inactive_codes": {"POST", "CANC", "INTR", "ABD"},
        "event_kind": "matchup",
    },
    "basketball": {
        "group": "Basketball",
        "groups": ("Basketball",),
        "base_url": "https://v1.basketball.api-sports.io/games",
        "live_codes": {"Q1", "Q2", "Q3", "Q4", "OT", "BT", "HT"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT", "AOT"},
        "inactive_codes": {"POST", "CANC", "SUSP", "AWD", "ABD"},
        "event_kind": "matchup",
    },
    "formula_1": {
        "group": "Motorsports",
        "groups": ("Formula 1", "Formula-1", "F1", "Motorsports"),
        "base_url": "https://v1.formula-1.api-sports.io/races",
        "live_codes": {"LIVE"},
        "scheduled_codes": {"SCHEDULED"},
        "finished_codes": {"COMPLETED"},
        "inactive_codes": {"CANCELLED", "POSTPONED"},
        "event_kind": "race",
    },
    "handball": {
        "group": "Handball",
        "groups": ("Handball",),
        "base_url": "https://v1.handball.api-sports.io/games",
        "live_codes": {"1H", "2H", "HT", "ET", "BT", "PT"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT", "AET", "AP"},
        "inactive_codes": {"AW", "POST", "CANC", "INTR", "ABD", "WO"},
        "event_kind": "matchup",
    },
    "hockey": {
        "group": "Ice Hockey",
        "groups": ("Ice Hockey", "Hockey"),
        "base_url": "https://v1.hockey.api-sports.io/games",
        "live_codes": {"P1", "P2", "P3", "OT", "PT", "BT"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT", "AOT", "AP"},
        "inactive_codes": {"AW", "POST", "CANC", "INTR", "ABD"},
        "event_kind": "matchup",
    },
    "mma": {
        "group": "Combat Sports",
        "groups": ("Combat Sports", "MMA"),
        "base_url": "https://v1.mma.api-sports.io/fights",
        "live_codes": {"IN", "PF", "LIVE", "EOR", "WO"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT"},
        "inactive_codes": {"CANC", "PST"},
        "event_kind": "fight",
    },
    "nba": {
        "group": "NBA",
        "groups": ("NBA",),
        "base_url": "https://v2.nba.api-sports.io/games",
        "live_codes": {"2"},
        "scheduled_codes": {"1"},
        "finished_codes": {"3"},
        "inactive_codes": {"4", "5", "6"},
        "event_kind": "matchup",
        "status_labels": {
            "1": "Not Started",
            "2": "Live",
            "3": "Finished",
            "4": "Postponed",
            "5": "Delayed",
            "6": "Canceled",
        },
    },
    "nfl": {
        "group": "American Football",
        "groups": ("American Football", "NFL", "NCAA Football"),
        "base_url": "https://v1.american-football.api-sports.io/games",
        "live_codes": {"Q1", "Q2", "Q3", "Q4", "OT", "HT"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT", "AOT"},
        "inactive_codes": {"CANC", "PST"},
        "event_kind": "matchup",
    },
    "rugby": {
        "group": "Rugby",
        "groups": ("Rugby",),
        "base_url": "https://v1.rugby.api-sports.io/games",
        "live_codes": {"1H", "2H", "HT", "ET", "BT", "PT"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT", "AET"},
        "inactive_codes": {"AW", "POST", "CANC", "INTR", "ABD"},
        "event_kind": "matchup",
    },
    "volleyball": {
        "group": "Volleyball",
        "groups": ("Volleyball",),
        "base_url": "https://v1.volleyball.api-sports.io/games",
        "live_codes": {"S1", "S2", "S3", "S4", "S5"},
        "scheduled_codes": {"NS"},
        "finished_codes": {"FT"},
        "inactive_codes": {"AW", "POST", "CANC", "INTR", "ABD"},
        "event_kind": "matchup",
    },
}

ESPN_CONFIG = {
    "football": {
        "endpoints": (
            ("fifa-friendly", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.friendly/scoreboard"),
            ("fifa-world", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"),
            ("fifa-worldq-uefa", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.worldq.uefa/scoreboard"),
            ("fifa-worldq-concacaf", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.worldq.concacaf/scoreboard"),
            ("fifa-worldq-conmebol", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.worldq.conmebol/scoreboard"),
            ("fifa-worldq-caf", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.worldq.caf/scoreboard"),
            ("fifa-worldq-afc", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.worldq.afc/scoreboard"),
            ("mls", "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"),
            ("premier-league", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
            ("la-liga", "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard"),
            ("serie-a", "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard"),
            ("bundesliga", "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard"),
            ("ligue-1", "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard"),
            ("uefa-champions", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"),
            ("uefa-europa", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.europa/scoreboard"),
            ("uefa-conference", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.europa.conf/scoreboard"),
        ),
    },
    "afl": {
        "endpoints": (
            ("afl", "https://site.api.espn.com/apis/site/v2/sports/australian-football/afl/scoreboard"),
        ),
    },
    "baseball": {
        "endpoints": (
            ("mlb", "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"),
        ),
    },
    "basketball": {
        "endpoints": (
            ("nba", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"),
            ("wnba", "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"),
        ),
    },
    "formula_1": {
        "endpoints": (
            ("f1", "https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard"),
        ),
    },
    "hockey": {
        "endpoints": (
            ("nhl", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"),
            ("mens-college", "https://site.api.espn.com/apis/site/v2/sports/hockey/mens-college-hockey/scoreboard"),
        ),
    },
    "mma": {
        "endpoints": (
            ("ufc", "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"),
        ),
    },
    "nba": {
        "endpoints": (
            ("nba", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"),
        ),
    },
    "nfl": {
        "endpoints": (
            ("nfl", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"),
            ("ufl", "https://site.api.espn.com/apis/site/v2/sports/football/ufl/scoreboard"),
            ("college-football", "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"),
        ),
    },
}

SPORT_BY_GROUP = {
    group: sport
    for sport, config in SPORTS_CONFIG.items()
    for group in config.get("groups", (config["group"],))
}
TEAM_ALIASES = {
    "bosnia herzegovina": "bosnia and herzegovina",
    "czechia": "czech republic",
    "south korea": "korea republic",
    "turkiye": "turkey",
    "united states": "usa",
}
