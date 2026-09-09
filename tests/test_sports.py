import unittest
from unittest.mock import patch

from iptv_multi_player import sports


class GameEnrichmentTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
