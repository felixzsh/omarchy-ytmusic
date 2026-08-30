#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen as fetch_url
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from server import Backend, idle_should_exit  # noqa: E402


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
        backend._start_artwork_proxy()
        original = "https://i.ytimg.com/vi/test/hqdefault.jpg"
        try:
            with patch("server.urlopen", return_value=Response()):
                with fetch_url(backend._local_artwork_url(original)) as response:
                    self.assertEqual(response.read(), b"image-data")
        finally:
            backend._stop_artwork_proxy()


if __name__ == "__main__":
    unittest.main()
