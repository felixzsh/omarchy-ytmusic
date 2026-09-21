#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from catalog import (  # noqa: E402
    Catalog,
    CatalogError,
    context_item,
    duration_ms,
    map_items,
    thumbnail_url,
    track_item,
)


class FakeYTMusic:
    def __init__(self, tracks=None, error=None):
        self.tracks = tracks or []
        self.error = error
        self.calls = []

    def get_watch_playlist(self, playlistId=None, limit=25, radio=False, shuffle=False):
        self.calls.append(playlistId)
        if self.error:
            raise self.error
        return {"tracks": self.tracks}


class CatalogTests(unittest.TestCase):
    def test_duration_parses_clock_and_seconds(self):
        self.assertEqual(duration_ms({"duration": "3:45"}), 225000)
        self.assertEqual(duration_ms({"duration": "1:02:03"}), 3723000)
        self.assertEqual(duration_ms({"duration_seconds": 90}), 90000)
        self.assertEqual(duration_ms({}), 0)

    def test_track_item_normalizes_song(self):
        item = track_item({
            "title": "Under the Bridge",
            "videoId": "GLvqBAudoEg",
            "artists": [{"name": "Red Hot Chili Peppers", "id": "UC123"}],
            "album": {"name": "Blood Sugar Sex Magik", "id": "MPREb_album"},
            "duration": "4:24",
            "thumbnails": [{"url": "https://img/small.jpg", "width": 60},
                           {"url": "https://img/large.jpg", "width": 544}],
            "likeStatus": "LIKE",
        })
        self.assertIsNotNone(item)
        self.assertEqual(item["type"], "track")
        self.assertEqual(item["kind"], "item")
        self.assertEqual(item["uri"], "ytm:track:GLvqBAudoEg")
        self.assertEqual(item["subtitle"], "Red Hot Chili Peppers")
        self.assertEqual(item["album"], "Blood Sugar Sex Magik")
        self.assertTrue(item["liked"])
        self.assertEqual(item["imageUrl"], "https://img/large.jpg")
        self.assertEqual(item["durationMs"], 264000)
        self.assertEqual(item["albumItem"]["type"], "album")

    def test_context_item_playlist(self):
        item = context_item({
            "title": "Liked Music",
            "playlistId": "LM",
            "count": 12,
        }, "playlist")
        self.assertEqual(item["type"], "playlist")
        self.assertEqual(item["kind"], "context")
        self.assertIn("12 songs", item["subtitle"])

    def test_map_items_skips_junk(self):
        rows = map_items([
            None,
            {"title": "Nope"},
            {"title": "Song", "videoId": "abcdefghijk"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["videoId"], "abcdefghijk")

    def test_thumbnail_prefers_wide_image(self):
        url = thumbnail_url({
            "thumbnails": [
                {"url": "a", "width": 60},
                {"url": "b", "width": 226},
            ]
        })
        self.assertEqual(url, "b")

    def test_playlist_mix_seeds_real_playlists(self):
        yt = FakeYTMusic(tracks=[{"title": "Song", "videoId": "abcdefghijk"}])
        tracks = Catalog(yt).playlist_mix("PL_123")
        self.assertEqual(yt.calls, ["RDAMPLPL_123"])
        self.assertEqual(tracks[0]["videoId"], "abcdefghijk")

    def test_playlist_mix_seeds_albums_too(self):
        yt = FakeYTMusic(tracks=[])
        Catalog(yt).playlist_mix("OLAK5uy_album")
        self.assertEqual(yt.calls, ["RDAMPLOLAK5uy_album"])

    def test_playlist_mix_keeps_existing_mix_ids(self):
        yt = FakeYTMusic(tracks=[])
        Catalog(yt).playlist_mix("RDEMartist")
        self.assertEqual(yt.calls, ["RDEMartist"])

    def test_playlist_mix_skips_empty_ids(self):
        yt = FakeYTMusic(tracks=[])
        self.assertEqual(Catalog(yt).playlist_mix(""), [])
        self.assertEqual(yt.calls, [])

    def test_playlist_mix_raises_catalog_error(self):
        yt = FakeYTMusic(error=RuntimeError("boom"))
        with self.assertRaises(CatalogError):
            Catalog(yt).playlist_mix("PL_123")


if __name__ == "__main__":
    unittest.main()
