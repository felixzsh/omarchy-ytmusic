#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import mpris  # noqa: E402
from mpris import MprisPlayer  # noqa: E402

try:  # The service module degrades to "unavailable" without jeepney.
    from jeepney import (  # noqa: E402
        Endianness, Header, HeaderFields, Message, MessageType,
    )
    HAVE_JEEPNEY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_JEEPNEY = False


STATE = {
    "playing": True,
    "position_ms": 42000,
    "duration_ms": 210000,
    "volume": 80,
    "muted": False,
    "shuffle": True,
    "repeat": "context",
    "queue": [{"videoId": "a"}, {"videoId": "b"}],
    "track": {
        "videoId": "abc_-123",
        "name": "Song",
        "subtitle": "Artist One, Artist Two",
        "artists": [{"name": "Artist One"}, {"name": "Artist Two"}],
        "album": {"name": "The Album"},
        "durationMs": 210000,
        "imageUrl": "http://127.0.0.1:47654/?url=cover",
    },
}


def method_call(interface, member, signature=None, body=()):
    fields = {
        HeaderFields.path: mpris.OBJECT_PATH,
        HeaderFields.interface: interface,
        HeaderFields.member: member,
    }
    if signature:
        fields[HeaderFields.signature] = signature
    header = Header(Endianness.little, MessageType.method_call, 0, 1, 0, 77, fields)
    return Message(header, body)


class MprisMappingTests(unittest.TestCase):
    def test_playback_status_tracks_the_queue(self):
        self.assertEqual(mpris.playback_status(STATE), "Playing")
        self.assertEqual(mpris.playback_status({**STATE, "playing": False}), "Paused")
        self.assertEqual(mpris.playback_status({"track": None}), "Stopped")

    def test_loop_status_mapping_round_trips(self):
        self.assertEqual(mpris.loop_status("context"), "Playlist")
        self.assertEqual(mpris.loop_status("track"), "Track")
        self.assertEqual(mpris.loop_status("weird"), "None")
        for mode in ("off", "context", "track"):
            self.assertEqual(mpris.repeat_mode(mpris.loop_status(mode)), mode)

    def test_volume_is_a_ratio_and_respects_mute(self):
        self.assertAlmostEqual(mpris.volume_ratio(STATE), 0.8)
        self.assertEqual(mpris.volume_ratio({**STATE, "muted": True}), 0.0)

    def test_metadata_carries_artist_album_artwork_and_length(self):
        meta = mpris.metadata(STATE)
        self.assertEqual(meta["xesam:title"], ("s", "Song"))
        self.assertEqual(meta["xesam:artist"], ("as", ["Artist One", "Artist Two"]))
        self.assertEqual(meta["xesam:album"], ("s", "The Album"))
        self.assertEqual(meta["mpris:length"], ("x", 210000000))
        self.assertEqual(meta["mpris:artUrl"], ("s", "http://127.0.0.1:47654/?url=cover"))
        self.assertEqual(meta["mpris:trackid"],
                         ("o", "/org/mpris/MediaPlayer2/Track/abc__123"))

    def test_metadata_without_a_track_is_the_no_track_marker(self):
        self.assertEqual(mpris.metadata({}), {"mpris:trackid": ("o", mpris.NO_TRACK_PATH)})

    def test_track_path_sanitises_unexpected_ids(self):
        self.assertEqual(mpris.track_object_path("a/b c"),
                         "/org/mpris/MediaPlayer2/Track/a_b_c")
        self.assertTrue(mpris.track_object_path("").endswith("/unknown"))

    def test_player_properties_report_capabilities(self):
        props = mpris.player_properties(STATE)
        self.assertEqual(props["PlaybackStatus"], ("s", "Playing"))
        self.assertEqual(props["Shuffle"], ("b", True))
        self.assertEqual(props["LoopStatus"], ("s", "Playlist"))
        self.assertEqual(props["CanSeek"], ("b", True))
        self.assertEqual(props["Position"], ("x", 42000000))
        empty = mpris.player_properties({"track": None, "queue": []})
        self.assertEqual(empty["PlaybackStatus"], ("s", "Stopped"))
        self.assertEqual(empty["CanPlay"], ("b", False))


@unittest.skipUnless(HAVE_JEEPNEY, "jeepney is required for MPRIS dispatch tests")
class MprisDispatchTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def record(name):
            def inner(*args):
                self.calls.append((name, args))
            return inner

        self.player = MprisPlayer(
            state_provider=lambda: STATE,
            controls={
                name: record(name)
                for name in ("play", "pause", "play_pause", "stop", "next",
                             "previous", "seek", "set_volume", "set_loop",
                             "set_shuffle")
            },
        )

    def test_get_and_get_all_answer_normalised_players(self):
        reply = self.player.handle(method_call(
            mpris.PROPERTIES_INTERFACE, "Get", "ss",
            ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")))
        self.assertEqual(reply.header.message_type, MessageType.method_return)
        self.assertEqual(reply.body, (("s", "Playing"),))

        reply = self.player.handle(method_call(
            mpris.PROPERTIES_INTERFACE, "GetAll", "s",
            ("org.mpris.MediaPlayer2",)))
        self.assertEqual(reply.body[0]["Identity"], ("s", "YouTube Music"))
        self.assertEqual(reply.body[0]["DesktopEntry"], ("s", "omarchy-ytmusic"))

    def test_unknown_property_is_an_error(self):
        reply = self.player.handle(method_call(
            mpris.PROPERTIES_INTERFACE, "Get", "ss",
            ("org.mpris.MediaPlayer2.Player", "Nope")))
        self.assertEqual(reply.header.message_type, MessageType.error)
        self.assertEqual(reply.header.fields[HeaderFields.error_name],
                         mpris.ERROR_INVALID_ARGS)

    def test_set_volume_loop_and_shuffle_use_the_controls(self):
        for name, signature, value in (
                ("Volume", "d", 0.55), ("LoopStatus", "s", "Playlist"),
                ("Shuffle", "b", True)):
            reply = self.player.handle(method_call(
                mpris.PROPERTIES_INTERFACE, "Set", "ssv",
                ("org.mpris.MediaPlayer2.Player", name, (signature, value))))
            self.assertEqual(reply.header.message_type, MessageType.method_return)
        self.assertEqual(self.calls, [
            ("set_volume", (55,)), ("set_loop", ("context",)),
            ("set_shuffle", (True,)),
        ])

    def test_read_only_property_cannot_be_set(self):
        reply = self.player.handle(method_call(
            mpris.PROPERTIES_INTERFACE, "Set", "ssv",
            ("org.mpris.MediaPlayer2.Player", "PlaybackStatus", ("s", "Playing"))))
        self.assertEqual(reply.header.fields[HeaderFields.error_name],
                         mpris.ERROR_READ_ONLY)

    def test_transport_methods_call_the_player(self):
        for member, expected in (("Play", "play"), ("Pause", "pause"),
                                 ("PlayPause", "play_pause"), ("Stop", "stop"),
                                 ("Next", "next"), ("Previous", "previous")):
            self.player.handle(method_call(mpris.PLAYER_INTERFACE, member))
            self.assertEqual(self.calls[-1][0], expected)

    def test_seek_converts_offsets_and_set_position_checks_the_track(self):
        self.player.handle(method_call(
            mpris.PLAYER_INTERFACE, "Seek", "x", (5000000,)))
        self.assertEqual(self.calls[-1], ("seek", (47000,)))

        self.player.handle(method_call(
            mpris.PLAYER_INTERFACE, "SetPosition", "ox",
            ("/org/mpris/MediaPlayer2/Track/other", 1000000)))
        self.assertEqual(len(self.calls), 1)

        self.player.handle(method_call(
            mpris.PLAYER_INTERFACE, "SetPosition", "ox",
            ("/org/mpris/MediaPlayer2/Track/abc__123", 3000000)))
        self.assertEqual(self.calls[-1], ("seek", (3000,)))

    def test_failing_control_becomes_a_dbus_error(self):
        self.player.controls["play"] = lambda: (_ for _ in ()).throw(
            RuntimeError("nothing queued"))
        reply = self.player.handle(method_call(mpris.PLAYER_INTERFACE, "Play"))
        self.assertEqual(reply.header.fields[HeaderFields.error_name], mpris.ERROR_FAILED)

    def test_introspection_and_ping(self):
        reply = self.player.handle(method_call(
            "org.freedesktop.DBus.Introspectable", "Introspect"))
        self.assertIn("org.mpris.MediaPlayer2.Player", reply.body[0])
        reply = self.player.handle(method_call("org.freedesktop.DBus.Peer", "Ping"))
        self.assertEqual(reply.header.message_type, MessageType.method_return)

    def test_position_only_changes_do_not_spam_properties_changed(self):
        self.player._bus_ready = True
        self.player.update(STATE)
        self.assertEqual(self.player._outbox.qsize(), 1)
        self.player.update({**STATE, "position_ms": 50000})
        self.assertEqual(self.player._outbox.qsize(), 1)
        self.player.update({**STATE, "playing": False})
        self.assertEqual(self.player._outbox.qsize(), 2)
        self.player._outbox.get_nowait()
        signal = self.player._outbox.get_nowait()
        self.assertEqual(signal.header.fields[HeaderFields.member], "PropertiesChanged")
        interface, changed, invalidated = signal.body
        self.assertEqual(interface, mpris.PLAYER_INTERFACE)
        self.assertEqual(changed["PlaybackStatus"], ("s", "Paused"))
        self.assertEqual(invalidated, [])


if __name__ == "__main__":
    unittest.main()
