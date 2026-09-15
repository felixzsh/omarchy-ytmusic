#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen as fetch_url
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from server import (  # noqa: E402
    Backend,
    DEFAULT_ARTWORK_PORT,
    activated_socket,
    idle_should_exit,
    preferred_artwork_port,
)
from catalog import CatalogError  # noqa: E402


class IdleWatchTests(unittest.TestCase):
    def test_idle_exit_requires_minutes_and_silence(self):
        now = 1_000.0
        self.assertFalse(idle_should_exit(
            idle_minutes=15, playing=False, client_count=0,
            last_activity=now, now=now))
        self.assertTrue(idle_should_exit(
            idle_minutes=15, playing=False, client_count=0,
            last_activity=now - 15 * 60, now=now))

    def test_idle_exit_skips_playing_and_connected_clients(self):
        now = 1_000.0
        self.assertFalse(idle_should_exit(
            idle_minutes=15, playing=True, client_count=0,
            last_activity=now - 15 * 60, now=now))
        self.assertFalse(idle_should_exit(
            idle_minutes=15, playing=False, client_count=1,
            last_activity=now - 15 * 60, now=now))
        self.assertFalse(idle_should_exit(
            idle_minutes=0, playing=False, client_count=0,
            last_activity=now - 15 * 60, now=now))


class ActivatedSocketTests(unittest.TestCase):
    def test_returns_none_without_systemd_environment(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(activated_socket())

    def test_returns_none_without_listen_fds(self):
        env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "0"}
        with patch.dict("os.environ", env, clear=True):
            self.assertIsNone(activated_socket())

    def test_adopts_fd_when_systemd_activates(self):
        env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"}
        sentinel = object()
        with patch.dict("os.environ", env, clear=True), \
                patch("server.socket.fromfd", return_value=sentinel) as fromfd:
            self.assertIs(activated_socket(), sentinel)
            fromfd.assert_called_once_with(3, socket.AF_UNIX, socket.SOCK_STREAM)


class ArtworkProxyTests(unittest.TestCase):
    def test_localizes_remote_artwork_urls(self):
        backend = Backend()
        backend.artwork_base_url = "http://127.0.0.1:12345/artwork"
        original = "https://i.ytimg.com/vi/test/hqdefault.jpg"

        localized = backend._localize_images({"imageUrl": original})["imageUrl"]

        parsed = urlsplit(localized)
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(parse_qs(parsed.query)["url"], [original])

    def test_proxy_serves_cached_artwork_over_loopback(self):
        class Headers:
            def get_content_type(self):
                return "image/png"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _limit):
                return b"image-data"

        backend = Backend()
        with patch("server.preferred_artwork_port", return_value=0):
            backend._start_artwork_proxy()
        original = "https://i.ytimg.com/vi/test/hqdefault.jpg"
        try:
            with patch("server.urlopen", return_value=Response()):
                with fetch_url(backend._local_artwork_url(original)) as response:
                    self.assertEqual(response.read(), b"image-data")
        finally:
            backend._stop_artwork_proxy()

    def test_artwork_port_defaults_and_validates_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(preferred_artwork_port(), DEFAULT_ARTWORK_PORT)
        with patch.dict("os.environ",
                        {"OMARCHY_YTMUSIC_ARTWORK_PORT": "51234"}, clear=True):
            self.assertEqual(preferred_artwork_port(), 51234)
        with patch.dict("os.environ",
                        {"OMARCHY_YTMUSIC_ARTWORK_PORT": "nope"}, clear=True):
            self.assertEqual(preferred_artwork_port(), DEFAULT_ARTWORK_PORT)
        with patch.dict("os.environ",
                        {"OMARCHY_YTMUSIC_ARTWORK_PORT": "70000"}, clear=True):
            self.assertEqual(preferred_artwork_port(), DEFAULT_ARTWORK_PORT)

    def test_artwork_proxy_reuses_the_configured_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        with patch("server.preferred_artwork_port", return_value=port):
            first = Backend()
            first._start_artwork_proxy()
            base = first.artwork_base_url
            first._stop_artwork_proxy()

            second = Backend()
            second._start_artwork_proxy()
            try:
                self.assertIn(f":{port}/artwork", base)
                self.assertEqual(second.artwork_base_url, base)
            finally:
                second._stop_artwork_proxy()


class RequireCatalogTests(unittest.TestCase):
    def test_require_catalog_retries_init_on_demand(self):
        backend = Backend()
        backend.catalog = None
        sentinel = object()
        calls = []

        def fake_start():
            calls.append(1)
            backend.catalog = sentinel

        backend.start_catalog = fake_start
        self.assertIs(backend.require_catalog(), sentinel)
        self.assertEqual(calls, [1])

    def test_require_catalog_raises_when_retry_fails(self):
        backend = Backend()
        backend.catalog = None
        backend.start_catalog = lambda: None
        with self.assertRaises(CatalogError):
            backend.require_catalog()


if __name__ == "__main__":
    unittest.main()
