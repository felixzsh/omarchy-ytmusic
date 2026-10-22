#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import session  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_keeps_only_playable_tracks_and_clamps_fields(self):
        clean = session.sanitize({
            "queue": [
                {"videoId": "a", "name": "A"},
                {"name": "no id"},
                {"videoId": "   "},
                {"videoId": "b", "name": "B"},
            ],
            "index": 99,
            "position_ms": 999000,
            "duration_ms": 120000,
            "volume": 250,
            "muted": True,
            "shuffle": True,
            "repeat": "sometimes",
        })
        self.assertEqual([track["videoId"] for track in clean["queue"]], ["a", "b"])
        self.assertEqual(clean["index"], 1)
        self.assertEqual(clean["position_ms"], 120000)
        self.assertEqual(clean["volume"], 100)
        self.assertFalse(clean["muted"])
        self.assertTrue(clean["shuffle"])
        self.assertEqual(clean["repeat"], "off")

    def test_rejects_sessions_without_a_track(self):
        self.assertIsNone(session.sanitize({}))
        self.assertIsNone(session.sanitize({"queue": [{"name": "no id"}]}))
        self.assertIsNone(session.sanitize("nope"))


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "session.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_creates_a_private_file(self):
        state = {
            "queue": [{"videoId": "a", "name": "A", "durationMs": 1000}],
            "index": 0,
            "position_ms": 500,
            "duration_ms": 1000,
            "volume": 40,
            "shuffle": True,
            "repeat": "track",
        }
        self.assertTrue(session.save_session(self.path, state))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        loaded = session.load_session(self.path)
        self.assertEqual(loaded["position_ms"], 500)
        self.assertEqual(loaded["repeat"], "track")
        self.assertEqual(loaded["queue"][0]["videoId"], "a")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["session.json"])

    def test_missing_and_corrupt_files_load_as_none(self):
        self.assertIsNone(session.load_session(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(session.load_session(self.path))
        self.assertIsNone(session.load_session(None))

    def test_refuses_to_persist_unserialisable_state(self):
        state = {"queue": [{"videoId": "a", "weird": object()}]}
        self.assertFalse(session.save_session(self.path, state))
        self.assertFalse(self.path.exists())

    def test_default_path_honours_xdg_state_home(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name}):
            path = session.default_state_path()
        self.assertEqual(path,
                         Path(self.tmp.name) / "omarchy-ytmusic" / "session.json")


class PlayerPersistenceTests(unittest.TestCase):
    def test_player_saves_and_restores_its_session(self):
        from player import QueuePlayer

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            player = QueuePlayer(Path(tmp) / "runtime", state_path=path)
            player.ensure_started = lambda: None
            player.queue = [{"videoId": "a", "name": "A", "durationMs": 200000}]
            player.index = 0
            player.position_ms = 61000
            player.duration_ms = 200000
            player.volume = 33
            player.repeat = "context"
            self.assertTrue(player.save_session())

            restored = QueuePlayer(Path(tmp) / "runtime", state_path=path)
            self.assertTrue(restored.restore_session(session.load_session(path)))
            self.assertEqual(restored.current["videoId"], "a")
            self.assertEqual(restored.position_ms, 61000)
            self.assertEqual(restored.volume, 33)
            self.assertEqual(restored.repeat, "context")
            self.assertFalse(restored.playing)

    def test_disabled_persistence_writes_nothing(self):
        from player import QueuePlayer

        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        self.assertFalse(player.save_session())
        self.assertEqual(player.snapshot_session()["queue"], [])


if __name__ == "__main__":
    unittest.main()
