import unittest
from contextlib import ExitStack
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from iptv_multi_player import sports


class GameEnrichmentTests(unittest.TestCase):
    def setUp(self):
        sports.best_team_event_index.cache_clear()

    def channels(self, count=10):
        group = next(group for group, sport in sports.SPORT_BY_GROUP.items() if sport == "football")
        return [
            {"id": str(index), "name": f"Liverpool vs. Atletico Madrid (Channel {index})", "group": group}
            for index in range(count)
        ]

    def test_matching_and_metadata_are_shared_per_game_and_refreshed(self):
        channels = self.channels()
        channels[-1]["name"] = "LIVERPOOL  vs. Atletico Madrid (Spanish - ESPN)"
        channels.append({**channels[0], "id": "other", "name": "Barcelona vs. Feyenoord (TNT)"})
        with (
            patch.object(sports, "ensure_sports_cache_for", return_value=({}, {})),
            patch.object(sports, "sports_query_dates", return_value=[]),
            patch.object(sports, "cached_espn_events", return_value=[{}]) as espn,
            patch.object(sports, "cached_api_sports_events", return_value=[]) as api,
            patch.object(sports, "best_event_match", return_value=({}, 1.0)) as match,
            patch.object(sports, "game_metadata", return_value={"score": "1-0"}) as metadata,
        ):
            enriched, _ = sports.enrich_channels_with_games(channels)
            self.assertEqual(match.call_count, 2)
            self.assertEqual(metadata.call_count, 2)
            espn.assert_called_once()
            api.assert_called_once()
            self.assertEqual([item["id"] for item in enriched], [item["id"] for item in channels])
            self.assertTrue(all(item["game"]["score"] == "1-0" for item in enriched))
            self.assertIsNot(enriched[0]["game"], enriched[1]["game"])
            self.assertNotIn("game", channels[0])
            metadata.return_value = {"score": "2-0"}
            refreshed, _ = sports.enrich_channels_with_games(channels)
            self.assertTrue(all(item["game"]["score"] == "2-0" for item in refreshed))

    def test_unmatched_game_checks_both_providers_once(self):
        with (
            patch.object(sports, "ensure_sports_cache_for", return_value=({}, {})),
            patch.object(sports, "sports_query_dates", return_value=[]),
            patch.object(sports, "best_event_match", return_value=(None, 0.0)) as match,
        ):
            enriched, _ = sports.enrich_channels_with_games(self.channels())
            self.assertEqual(match.call_count, 2)
            self.assertTrue(all(not item["game"]["matched"] for item in enriched))

    def test_nested_channel_suffix_preserves_team_parentheses_and_shares_lookup(self):
        channels = self.channels(2)
        channels[0]["name"] = "Team (Women) vs. Rival (English - Sports (HD))"
        channels[1]["name"] = "Team (Women) vs. Rival (English - Other)"
        self.assertEqual(sports.parse_channel_matchup(channels[0])["title"], "Team (Women) vs. Rival")
        with (
            patch.object(sports, "ensure_sports_cache_for", return_value=({}, {})),
            patch.object(sports, "sports_query_dates", return_value=[]),
            patch.object(sports, "best_event_match", return_value=({}, 1.0)) as match,
            patch.object(sports, "game_metadata", return_value={"score": "1-0"}),
        ):
            enriched, _ = sports.enrich_channels_with_games(channels)
            match.assert_called_once()
            self.assertEqual(enriched[0]["game"], enriched[1]["game"])

    def test_title_parser_leaves_missing_empty_and_unbalanced_suffixes_intact(self):
        for name in ("Team vs. Rival", "Team vs. Rival ()", "Team vs. Rival (Sports HD))", "(Sports)"):
            with self.subTest(name=name):
                self.assertEqual(sports.channel_game_title(name), name)

    def test_provider_requests_do_not_scale_with_stream_count(self):
        for count in (1, 10):
            with (
                self.subTest(streams=count),
                patch.object(sports, "read_sports_cache", return_value={"sports": {}}),
                patch.object(sports, "write_sports_cache"),
                patch.object(sports, "api_sports_key", return_value="test"),
                patch.object(sports, "now_ts", return_value=2000000000),
                patch.object(sports, "sports_query_dates", return_value=["2026-09-09"]),
                patch.object(sports, "ESPN_CONFIG", {"football": {"endpoints": [("test", "https://example.invalid")]}}),
                patch.object(sports, "fetch_espn_events", return_value=[]) as espn,
                patch.object(sports, "fetch_sports_events", return_value=[]) as api,
            ):
                sports.ensure_sports_cache_for(self.channels(count))
                espn.assert_called_once()
                api.assert_called_once()


class EspnQueryTests(unittest.TestCase):
    def event(self, identifier):
        return {
            "id": identifier, "date": "2026-10-25T20:00:00Z",
            "competitions": [{
                "id": identifier,
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Liverpool"}, "score": "1"},
                    {"homeAway": "away", "team": {"displayName": "Atletico Madrid"}, "score": "0"},
                ],
            }],
        }

    def test_scoreboard_requests_individual_days_and_deduplicates_games(self):
        with patch.object(sports, "fetch_sports_json", side_effect=[
            {"events": [self.event("one")]},
            {"events": [self.event("one"), self.event("two")]},
        ]) as fetch:
            events = sports.fetch_espn_events("football", "test", "https://example.invalid/scoreboard", "20261025-20261026")
        dates = [parse_qs(urlparse(call.args[0].full_url).query)["dates"] for call in fetch.call_args_list]
        self.assertEqual(dates, [["20261025"], ["20261026"]])
        self.assertEqual([event["id"] for event in events], ["one", "two"])
        self.assertEqual(sports.event_score(events[0], "football"), "1-0")

    def test_single_day_makes_one_scoreboard_request(self):
        with patch.object(sports, "fetch_sports_json", return_value={"events": []}) as fetch:
            self.assertEqual(sports.fetch_espn_events("football", "test", "https://example.invalid/scoreboard", "20261025"), [])
        fetch.assert_called_once()


class SportsCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 2000000000
        self.cache = {"sports": {}}
        self.channels = [{"id": "1", "name": "Liverpool vs. Atletico Madrid (Sports)", "group": "Football"}]
        self.dates = ["2033-05-18"]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(sports._provider_backoff, {}, clear=True))
        self.stack.enter_context(patch.dict(sports._request_slots, {}, clear=True))
        for name, value in (
            ("read_sports_cache", self.cache), ("write_sports_cache", None),
            ("api_sports_key", "test"), ("now_ts", self.now),
            ("sports_query_dates", self.dates),
            ("fetch_espn_events", []), ("fetch_sports_events", []),
        ):
            setattr(self, name, self.stack.enter_context(patch.object(sports, name, return_value=value)))
        self.stack.enter_context(patch.object(sports, "ESPN_CONFIG", {
            sport: {"endpoints": [(sport, "https://example.invalid/" + sport)]}
            for sport in ("football", "hockey")
        }))
        sports.best_team_event_index.cache_clear()

    def event(self, name="Liverpool", code="2H", score=1):
        return {
            "_provider": "espn", "_sport": "football", "id": "game",
            "teams": {"home": {"name": name}, "away": {"name": "Atletico Madrid"}},
            "scores": {"home": score, "away": 0},
            "status": {"short": code}, "timestamp": self.now + 120,
        }

    def test_cached_startup_shows_scores_without_network_or_writes(self):
        self.fetch_espn_events.return_value = [self.event()]
        sports.ensure_sports_cache_for(self.channels)
        self.fetch_espn_events.reset_mock()
        self.fetch_sports_events.reset_mock()
        self.write_sports_cache.reset_mock()
        self.now_ts.return_value = self.now + 3600
        channels, meta = sports.channels_with_pending_games(self.channels)
        self.assertEqual(channels[0]["game"]["kind"], "live")
        self.assertEqual(channels[0]["game"]["score"], "1-0")
        self.assertTrue(meta["sports"]["football"]["stale"])
        self.fetch_espn_events.assert_not_called()
        self.fetch_sports_events.assert_not_called()
        self.write_sports_cache.assert_not_called()

    def test_empty_startup_marks_games_loading_without_network(self):
        channels, meta = sports.channels_with_pending_games(self.channels)
        self.assertEqual(channels[0]["game"]["kind"], "loading")
        self.assertTrue(meta["loading"])
        self.fetch_espn_events.assert_not_called()
        self.fetch_sports_events.assert_not_called()
        self.write_sports_cache.assert_not_called()

    def test_startup_does_not_show_scores_from_a_previous_date_range(self):
        self.api_sports_key.return_value = ""
        self.fetch_espn_events.return_value = [self.event(code="FT")]
        sports.ensure_sports_cache_for(self.channels)
        self.dates[0] = "2033-05-19"
        channels, meta = sports.channels_with_pending_games(self.channels)
        self.assertEqual(channels[0]["game"]["kind"], "loading")
        self.assertEqual(channels[0]["game"]["score"], "")
        self.assertTrue(meta["sports"]["football"]["stale"])
        self.fetch_espn_events.assert_called_once()

    def test_match_association_is_reused_but_scores_and_status_are_current(self):
        matchup = sports.parse_channel_matchup(self.channels[0])
        original = self.event()
        updated = self.event(code="FT", score=2)
        with patch.object(sports, "team_match_score", wraps=sports.team_match_score) as compare:
            self.assertIs(sports.best_event_match(matchup, [original])[0], original)
            comparisons = compare.call_count
            event, confidence = sports.best_event_match(matchup, [updated])
            self.assertEqual(compare.call_count, comparisons)
            metadata = sports.game_metadata(event, "football", confidence)
            self.assertEqual(metadata["score"], "2-0")
            self.assertEqual(metadata["kind"], "final")

    def test_changed_event_roster_invalidates_cached_association(self):
        matchup = sports.parse_channel_matchup(self.channels[0])
        rival = self.event(name="Completely Different Team")
        original = self.event()
        self.assertIsNone(sports.best_event_match(matchup, [rival])[0])
        self.assertIs(sports.best_event_match(matchup, [rival, original])[0], original)
        self.assertIs(sports.best_event_match(matchup, [original, rival])[0], original)
        self.assertIsNone(sports.best_event_match(matchup, [])[0])

    def test_independent_sports_and_providers_fetch_concurrently(self):
        self.channels.append({"id": "2", "name": "Team vs. Rival", "group": "Ice Hockey"})
        barrier = threading.Barrier(4)

        def fetch(*args):
            barrier.wait(timeout=5)
            return []

        self.fetch_espn_events.side_effect = fetch
        self.fetch_sports_events.side_effect = fetch
        _, meta = sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(self.fetch_espn_events.call_count, 2)
        self.assertEqual(self.fetch_sports_events.call_count, 2)
        self.assertTrue(all(not item["error"] for item in meta["sports"].values()))

    def test_idle_and_live_feeds_refresh_once_a_minute(self):
        self.api_sports_key.return_value = ""
        _, meta = sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(meta["refresh_seconds"], 60)
        self.now_ts.return_value = self.now + 31
        sports.ensure_sports_cache_for(self.channels)
        self.fetch_espn_events.assert_called_once()
        self.now_ts.return_value = self.now + 61
        self.fetch_espn_events.return_value = [self.event()]
        _, meta = sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(meta["refresh_seconds"], 60)
        self.now_ts.return_value += 61
        sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(self.fetch_espn_events.call_count, 3)

    def test_minute_updates_do_not_increase_api_sports_usage(self):
        sports.ensure_sports_cache_for(self.channels)
        self.now_ts.return_value += 61
        sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(self.fetch_espn_events.call_count, 2)
        self.fetch_sports_events.assert_called_once()

    def test_failures_preserve_previous_scores_and_back_off(self):
        self.fetch_espn_events.return_value = [self.event()]
        sports.ensure_sports_cache_for(self.channels)
        self.now_ts.return_value = self.now + 1000
        self.fetch_espn_events.side_effect = TimeoutError("test timeout")
        self.fetch_sports_events.side_effect = TimeoutError("test timeout")
        cache, meta = sports.ensure_sports_cache_for(self.channels)
        previous = deepcopy(cache)
        self.assertEqual(cache["espn"]["football"]["endpoints"]["football"]["events"], [self.event()])
        self.assertIn("test timeout", meta["sports"]["football"]["error"])
        sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(self.fetch_espn_events.call_count, 2)
        self.assertEqual(self.fetch_sports_events.call_count, 2)
        self.assertEqual(cache, previous)
        self.now_ts.return_value += 61
        sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(self.fetch_espn_events.call_count, 3)
        self.assertEqual(self.fetch_sports_events.call_count, 2)

    def test_parallel_fetches_respect_daily_api_budget(self):
        self.dates.append("2033-05-19")
        self.cache["sports"]["football"] = {
            "calls": {sports.local_date(): sports.SPORTS_DAILY_CALL_LIMIT - 1}, "dates": {},
        }
        _, meta = sports.ensure_sports_cache_for(self.channels)
        self.fetch_sports_events.assert_called_once()
        self.assertEqual(meta["sports"]["football"]["calls_used"], sports.SPORTS_DAILY_CALL_LIMIT)
        self.assertIn("Daily API call limit", meta["sports"]["football"]["error"])

    def test_failed_idle_feed_waits_a_minute_before_retrying(self):
        self.api_sports_key.return_value = ""
        self.fetch_espn_events.side_effect = TimeoutError("test timeout")
        _, meta = sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(meta["refresh_seconds"], 60)
        self.now_ts.return_value += 31
        sports.ensure_sports_cache_for(self.channels)
        self.fetch_espn_events.assert_called_once()
        self.now_ts.return_value = self.now + 61
        sports.ensure_sports_cache_for(self.channels)
        self.assertEqual(self.fetch_espn_events.call_count, 2)

    def test_retry_after_accepts_seconds_dates_and_invalid_values(self):
        self.assertEqual(sports.retry_after_seconds("120", self.now), 120)
        self.assertEqual(sports.retry_after_seconds(formatdate(self.now + 120, usegmt=True), self.now), 120)
        for value in ("", "invalid", "-1", None):
            with self.subTest(value=value):
                self.assertEqual(sports.retry_after_seconds(value, self.now), 0)

    def test_rate_limit_cools_down_all_endpoints_and_survives_restart(self):
        req = urllib.request.Request("https://example.invalid/football")
        error = urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "600"}, None)
        with patch.object(sports.urllib.request, "urlopen", side_effect=error) as urlopen:
            with self.assertRaises(urllib.error.HTTPError):
                sports.fetch_sports_json(req, spacing=0.25)
            with self.assertRaisesRegex(RuntimeError, "cooling down"):
                sports.fetch_sports_json(urllib.request.Request("https://example.invalid/hockey"), spacing=0.25)
            urlopen.assert_called_once()
        self.cache["provider_backoff"] = deepcopy(sports._provider_backoff)
        sports._provider_backoff.clear()
        self.now_ts.return_value += 61
        sports.ensure_sports_cache_for(self.channels)
        self.fetch_espn_events.assert_not_called()
        self.fetch_sports_events.assert_called_once()
        self.now_ts.return_value = self.now + 601
        sports.ensure_sports_cache_for(self.channels)
        self.fetch_espn_events.assert_called_once()

    def test_rate_limit_backoff_grows_without_retry_after(self):
        req = urllib.request.Request("https://example.invalid/football")
        error = urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        with patch.object(sports.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                sports.fetch_sports_json(req, spacing=0)
            self.assertEqual(sports._provider_backoff["example.invalid"]["retry_at"], self.now + 300)
            self.now_ts.return_value += 301
            with self.assertRaises(urllib.error.HTTPError):
                sports.fetch_sports_json(req, spacing=0)
            self.assertEqual(sports._provider_backoff["example.invalid"]["retry_at"], self.now_ts.return_value + 600)

    def test_requests_are_spaced_without_busy_waiting(self):
        req = urllib.request.Request("https://example.invalid/football")
        with (
            patch.object(sports.time, "monotonic", return_value=100.0),
            patch.object(sports.time, "sleep") as sleep,
            patch.object(sports.urllib.request, "urlopen") as urlopen,
        ):
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            sports.fetch_sports_json(req, spacing=0.25)
            sports.fetch_sports_json(req, spacing=0.25)
            sleep.assert_called_once_with(0.25)

    def test_concurrent_rate_limits_cannot_shorten_a_server_cooldown(self):
        barrier = threading.Barrier(2)
        release_second = threading.Event()

        def fail(req, **kwargs):
            barrier.wait(timeout=5)
            if req.full_url.endswith("second"):
                release_second.wait(timeout=5)
                delay = "60"
            else:
                delay = "7200"
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": delay}, None)

        with patch.object(sports.urllib.request, "urlopen", side_effect=fail), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(sports.fetch_sports_json, urllib.request.Request("https://example.invalid/first"), 0)
            second = executor.submit(sports.fetch_sports_json, urllib.request.Request("https://example.invalid/second"), 0)
            try:
                with self.assertRaises(urllib.error.HTTPError):
                    first.result(timeout=5)
            finally:
                release_second.set()
            with self.assertRaises(urllib.error.HTTPError):
                second.result(timeout=5)
        self.assertEqual(sports._provider_backoff["example.invalid"]["retry_at"], self.now + 7200)


if __name__ == "__main__":
    unittest.main()
