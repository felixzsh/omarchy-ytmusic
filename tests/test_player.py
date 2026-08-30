#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from player import (  # noqa: E402
    loadfile_command,
    looks_like_stream_title,
    media_artist,
    media_title,
    mpris_title,
    mpv_command_line,
    mpv_env,
    StreamResolver,
    QueuePlayer,
)


class EventMpv:
    running = True

    def __init__(self, events, owner):
        self.events = list(events)
        self.owner = owner

    def poll_events(self, _timeout):
        if self.events:
            return [self.events.pop(0)]
        self.owner._stop.set()
        return []

    def command(self, _args):
        pass


class PlayerTests(unittest.TestCase):
    def test_stream_resolver_normalizes_quality(self):
        self.assertEqual(StreamResolver._normalize_quality(96), 96)
        self.assertEqual(StreamResolver._normalize_quality(128), 160)
        self.assertEqual(StreamResolver._normalize_quality(320), 320)

    def test_mpv_command_stays_headless(self):
        command = mpv_command_line("/usr/bin/mpv", Path("/tmp/mpv.sock"), "/lib/mpris.so")
        joined = " ".join(command)
        self.assertIn("--vo=null", command)
        self.assertIn("--no-config", command)
        self.assertIn("--load-scripts=no", command)
        self.assertIn("--keep-open=no", command)
        self.assertIn("--clipboard-backends-clr", command)
        self.assertIn("--audio-client-name=omarchy-ytmusic", command)
        self.assertIn("--script=/lib/mpris.so", command)
        self.assertNotIn("--really-quiet", command)
        self.assertIn("pipewire", joined)
        # Spaces in the Pulse/MPRIS client name make mpv-mpris hang on D-Bus.
        self.assertTrue(all(" " not in arg.split("=", 1)[-1]
                            for arg in command if arg.startswith("--audio-client-name=")))

    def test_media_title_prefers_track_name(self):
        self.assertEqual(media_title({"name": "Splashing Around"}), "Splashing Around")
        self.assertEqual(media_title({}), "YouTube Music")
        self.assertEqual(
            media_artist({"subtitle": "Baby Sleep Music", "artists": [{"name": "Other"}]}),
            "Baby Sleep Music",
        )
        self.assertEqual(media_artist({"artists": [{"name": "A"}, {"name": "B"}]}), "A, B")

    def test_loadfile_sets_force_media_title(self):
        command = loadfile_command(
            "https://example.test/stream",
            {"name": "Splashing Around", "subtitle": "Baby Sleep Music"},
        )
        self.assertEqual(command[0], "loadfile")
        self.assertEqual(command[2], "replace")
        self.assertEqual(
            command[4]["force-media-title"],
            "Baby Sleep Music - Splashing Around",
        )
        self.assertEqual(
            mpris_title({"name": "Splashing Around", "subtitle": "Baby Sleep Music"}),
            "Baby Sleep Music - Splashing Around",
        )

    def test_stream_titles_are_rejected(self):
        self.assertTrue(looks_like_stream_title("webm&ns=abc&rqh=1"))
        self.assertTrue(looks_like_stream_title("https://rr1---sn.googlevideo.com/videoplayback?x=1"))
        self.assertFalse(looks_like_stream_title("Skip to my lou"))

    def test_mpv_env_drops_wayland(self):
        env = mpv_env({
            "WAYLAND_DISPLAY": "wayland-1",
            "DISPLAY": ":0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "HOME": "/home/user",
        })
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertNotIn("DISPLAY", env)
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_stream_clock_waits_for_a_real_position(self):
        states = []
        player = QueuePlayer(
            Path("/tmp/omarchy-ytmusic-test"),
            on_change=lambda: states.append(player.playing),
        )
        player.queue = [{"videoId": "test"}]
        player.index = 0
        player._stream_start_ms = 130000
        player._resume_after_load = True
        player.mpv = EventMpv([
            {"event": "file-loaded"},
            {"event": "property-change", "name": "pause", "data": False},
            {"event": "playback-restart"},
            {"event": "property-change", "name": "time-pos", "data": 0.0},
            {"event": "property-change", "name": "time-pos", "data": 25.0},
        ], player)

        player._loop()

        self.assertTrue(player.playing)
        self.assertEqual(player.position_ms, 155000)
        self.assertEqual(states, [False, True])


if __name__ == "__main__":
    unittest.main()
