#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from player import (  # noqa: E402
    loadfile_command,
    looks_like_stream_title,
    media_artist,
    media_title,
    mpris_title,
    mpv_command_line,
    mpv_env,
    node_sandbox_flags,
    StreamResolver,
    QueuePlayer,
    PlayerError,
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

    def test_resolver_runs_inside_nodes_permission_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolver = StreamResolver(
                runtime_dir=root / "run",
                resolver_script=root / "resolver" / "resolver.mjs",
            )
            resolver.cache_dir = root / "cache"
            with patch("player.node_sandbox_flags", return_value=["--permission"]):
                command = resolver._node_command("/usr/bin/node")
        self.assertEqual(command[0], "/usr/bin/node")
        self.assertIn("--permission", command)
        # The resolver reads its own tree and writes only its cache and scratch.
        self.assertIn(f"--allow-fs-read={root / 'resolver'}", command)
        self.assertIn(f"--allow-fs-read={root / 'cache'}", command)
        self.assertIn(f"--allow-fs-read={root / 'run' / 'tmp'}", command)
        self.assertIn(f"--allow-fs-write={root / 'cache'}", command)
        self.assertIn(f"--allow-fs-write={root / 'run' / 'tmp'}", command)
        self.assertNotIn(f"--allow-fs-write={root / 'resolver'}", command)
        self.assertNotIn("--allow-child-process", command)
        self.assertNotIn("--allow-worker", command)

    def test_resolver_still_starts_when_node_lacks_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = StreamResolver(
                runtime_dir=Path(tmp) / "run",
                resolver_script=Path(tmp) / "resolver.mjs",
            )
            with patch("player.node_sandbox_flags", return_value=[]):
                command = resolver._node_command("/usr/bin/node")
        self.assertEqual(command[0], "/usr/bin/node")
        self.assertFalse(any(item.startswith("--allow-fs") for item in command))

    def test_resolver_sandbox_blocks_the_owners_filesystem(self):
        node = shutil.which("node")
        if not node or not node_sandbox_flags(node):
            self.skipTest("this node has no permission model")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "resolver" / "probe.mjs"
            script.parent.mkdir()
            script.write_text(
                "import fs from 'node:fs';\n"
                "import { execFileSync } from 'node:child_process';\n"
                "const attempt = (fn) => {\n"
                "  try { fn(); return 'allowed'; } catch (e) { return e.code; }\n"
                "};\n"
                "console.log(JSON.stringify({\n"
                "  home: attempt(() => fs.readFileSync(process.env.HOME + '/.ssh/known_hosts')),\n"
                "  scratch: attempt(() => fs.writeFileSync(process.env.TMPDIR + '/s', 'x')),\n"
                "  spawn: attempt(() => execFileSync('true')),\n"
                "}));\n"
            )
            resolver = StreamResolver(
                runtime_dir=root / "run",
                resolver_script=script,
            )
            resolver.cache_dir = root / "cache"
            resolver.cache_dir.mkdir()
            scratch = resolver._scratch_dir()
            scratch.mkdir(parents=True)
            environment = dict(os.environ, TMPDIR=str(scratch))
            result = subprocess.run(
                resolver._node_command(node),
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["home"], "ERR_ACCESS_DENIED")
        self.assertEqual(report["scratch"], "allowed")
        self.assertEqual(report["spawn"], "ERR_ACCESS_DENIED")

    def test_mpv_command_stays_headless(self):
        command = mpv_command_line("/usr/bin/mpv", Path("/tmp/mpv.sock"))
        joined = " ".join(command)
        self.assertIn("--vo=null", command)
        self.assertIn("--no-config", command)
        self.assertIn("--load-scripts=no", command)
        self.assertIn("--keep-open=no", command)
        self.assertIn("--clipboard-backends-clr", command)
        self.assertIn("--audio-client-name=omarchy-ytmusic", command)
        # MPRIS is owned by the backend, so mpv must not publish a second player.
        self.assertFalse(any(arg.startswith("--script=") for arg in command))
        self.assertNotIn("--really-quiet", command)
        self.assertIn("pipewire", joined)
        self.assertTrue(all(" " not in arg.split("=", 1)[-1]
                            for arg in command if arg.startswith("--audio-client-name=")))

    def test_restored_session_plays_from_the_saved_position(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.ensure_started = lambda: None
        restored = player.restore_session({
            "queue": [{"videoId": "a", "name": "A"}, {"videoId": "b", "name": "B"}],
            "index": 1,
            "position_ms": 42000,
            "duration_ms": 200000,
            "volume": 65,
            "shuffle": True,
            "repeat": "context",
        })
        self.assertTrue(restored)
        self.assertEqual(player.current["videoId"], "b")
        self.assertEqual(player.position_ms, 42000)
        self.assertEqual(player.volume, 65)
        self.assertTrue(player.shuffle)
        self.assertEqual(player.repeat, "context")
        self.assertFalse(player.playing)
        calls = []
        player._play_current = lambda **kwargs: calls.append(kwargs)
        player.play()
        self.assertEqual(calls, [{"start": True, "start_ms": 42000}])

    def test_seek_before_play_only_moves_the_saved_position(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.queue = [{"videoId": "a", "durationMs": 200000}]
        player.index = 0
        player.mpv = type("IdleMpv", (), {"running": False})()
        player.seek(90000)
        self.assertEqual(player.position_ms, 90000)
        player.seek(999000)
        self.assertEqual(player.position_ms, 200000)
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

    def test_next_skips_track_after_retries(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.queue = [
            {"videoId": "first"},
            {"videoId": "broken"},
            {"videoId": "third"},
        ]
        player.index = 0
        calls = []

        def fake_play(start=True, expose_error=True):
            calls.append(player.current["videoId"])
            if player.current["videoId"] == "broken":
                raise PlayerError("PMD:Undefined")
            player.playing = True

        player._play_current = fake_play
        with patch("player.time.sleep"):
            player.next()

        self.assertEqual(player.index, 2)
        self.assertTrue(player.playing)
        self.assertEqual(calls, ["broken", "broken", "broken", "third"])
        self.assertEqual(player.error, "")

    def test_next_reports_only_when_queue_is_exhausted(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.queue = [{"videoId": "first"}, {"videoId": "broken"}]
        player.index = 0

        def fake_play(start=True, expose_error=True):
            raise PlayerError("PMD:Undefined")

        player._play_current = fake_play
        with patch("player.time.sleep"):
            player.next()

        self.assertFalse(player.playing)
        self.assertEqual(player.error, "PMD:Undefined")

    def test_shuffle_consumes_a_persistent_random_order(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.ensure_started = lambda: None

        def fake_play(start=True, expose_error=True):
            player.playing = True

        player._play_current = fake_play
        player.load([{"videoId": video_id} for video_id in "ABCD"])

        with patch("player.random.shuffle", side_effect=lambda values: values.reverse()) as shuffle:
            player.set_shuffle(True)
            player.set_shuffle(True)
            self.assertEqual(player._shuffle_pending, [3, 2, 1])
            player.next()
            self.assertEqual(player.current["videoId"], "D")
            player.next()
            self.assertEqual(player.current["videoId"], "C")
            player.next()
            self.assertEqual(player.current["videoId"], "B")
            self.assertEqual(shuffle.call_count, 1)

    def test_stream_error_retries_then_skips(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.queue = [{"videoId": "broken"}, {"videoId": "second"}]
        player.index = 0
        player.playing = True
        calls = []

        def fake_play(start=True, expose_error=True):
            calls.append(player.current["videoId"])
            if player.current["videoId"] == "broken":
                raise PlayerError("SABR request failed: 403")
            player.playing = True

        player._play_current = fake_play
        player.mpv = EventMpv([{"event": "end-file", "reason": "error"}], player)

        with patch("player.time.sleep"):
            player._loop()

        self.assertEqual(player.index, 1)
        self.assertTrue(player.playing)
        self.assertEqual(calls, ["broken", "broken", "broken", "second"])

    def test_eof_advances_even_when_play_flag_is_stale(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.queue = [{"videoId": "first"}, {"videoId": "second"}]
        player.index = 0
        player.playing = False

        def fake_play(start=True, expose_error=True):
            player.playing = True

        player._play_current = fake_play
        player.mpv = EventMpv([{"event": "end-file", "reason": "eof"}], player)

        player._loop()

        self.assertEqual(player.index, 1)
        self.assertTrue(player.playing)

    def test_duplicate_end_events_advance_once(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.queue = [
            {"videoId": "first"},
            {"videoId": "second"},
            {"videoId": "third"},
        ]
        player.index = 0
        player.playing = True

        def fake_play(start=True, expose_error=True):
            player.playing = True

        player._play_current = fake_play
        player.mpv = EventMpv([
            {"event": "end-file", "reason": "eof"},
            {"event": "property-change", "name": "eof-reached", "data": True},
        ], player)

        player._loop()

        self.assertEqual(player.index, 1)

    def test_shuffle_previous_uses_history_and_next_restores_forward_track(self):
        player = QueuePlayer(Path("/tmp/omarchy-ytmusic-test"))
        player.ensure_started = lambda: None

        def fake_play(start=True, expose_error=True):
            player.playing = True

        player._play_current = fake_play
        player.load([{"videoId": video_id} for video_id in "ABCD"])

        with patch("player.random.shuffle", side_effect=lambda values: values.reverse()):
            player.set_shuffle(True)
            player.next()
            player.next()
            self.assertEqual(player.current["videoId"], "C")

            player.previous()
            self.assertEqual(player.current["videoId"], "D")
            player.next()
            self.assertEqual(player.current["videoId"], "C")
            player.previous()
            self.assertEqual(player.current["videoId"], "D")


if __name__ == "__main__":
    unittest.main()
