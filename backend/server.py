#!/usr/bin/env python3
"""Owner-only Unix socket backend for Omarchy YouTube Music."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import auth
import catalog
import protocol
from catalog import Catalog, CatalogError
from player import PlayerError, QueuePlayer
from protocol import (
    BACKEND_VERSION,
    ERROR_AUTH,
    ERROR_CATALOG,
    ERROR_INVALID_REQUEST,
    ERROR_PLAYBACK,
    ERROR_UNKNOWN_COMMAND,
    ERROR_UNSUPPORTED_VERSION,
    PROTOCOL_VERSION,
    dumps,
    event,
    parse_line,
    redact,
    response,
)


def runtime_dir() -> Path:
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/omarchy-ytmusic-{os.getuid()}")
    path = root / "omarchy-ytmusic"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def socket_path() -> Path:
    return runtime_dir() / "backend.sock"


def idle_should_exit(*, idle_minutes: int, playing: bool, client_count: int,
                     last_activity: float, now: float) -> bool:
    if idle_minutes <= 0 or playing or client_count > 0:
        return False
    return (now - last_activity) >= idle_minutes * 60


class Backend:
    def __init__(self, auth_path: Path | None = None):
        self.auth_path = auth.default_auth_path() if auth_path is None else auth_path
        self.catalog: Catalog | None = None
        self.signed_in = False
        self.account_name = ""
        self.lifecycle = "starting"
        self.error = ""
        self.idle_minutes = 15
        self.quality_kbps = 320
        self.generation = 0
        self.artwork_base_url = ""
        self._artwork_httpd: ThreadingHTTPServer | None = None
        self._artwork_thread: threading.Thread | None = None
        self._artwork_cache: dict[str, tuple[str, bytes]] = {}
        self._artwork_lock = threading.Lock()
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._stop = threading.Event()
        self.player = QueuePlayer(
            runtime_dir(),
            on_change=self._on_player_change,
            catalog_radio=self._radio_tracks,
        )
        self._last_broadcast = 0.0

    def start_catalog(self) -> None:
        from ytmusicapi import YTMusic

        path = auth.resolve_auth_path(str(self.auth_path) if self.auth_path else None)
        self.auth_path = path
        cookie_header = ""
        try:
            if auth.auth_available(path):
                headers = auth.load_headers(path)
                cookie_header = auth.cookie_header(headers)
                self.catalog = Catalog(YTMusic(str(path)))
                self.signed_in = True
                try:
                    info = self.catalog.account()
                    self.account_name = info.get("name") or ""
                except Exception:
                    self.account_name = ""
            else:
                self.catalog = Catalog(YTMusic())
                self.signed_in = False
                self.account_name = ""
            self.player.resolver.set_cookie_header(cookie_header)
            self.lifecycle = "ready"
            self.error = ""
        except Exception as exc:
            try:
                self.catalog = Catalog(YTMusic())
                self.signed_in = False
                self.player.resolver.set_cookie_header("")
                self.lifecycle = "ready"
                self.error = redact(str(exc))
            except Exception as inner:
                self.catalog = None
                self.player.resolver.set_cookie_header("")
                self.lifecycle = "error"
                self.error = redact(str(inner))

    def state(self) -> dict[str, Any]:
        track = self.player.snapshot_track()
        return self._localize_images({
            "lifecycle": self.lifecycle,
            "backend_version": BACKEND_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "signed_in": self.signed_in,
            "account_name": self.account_name,
            "playing": self.player.playing,
            "shuffle": self.player.shuffle,
            "repeat": self.player.repeat,
            "volume": self.player.volume,
            "muted": self.player.muted,
            "position_ms": self.player.position_ms,
            "duration_ms": self.player.duration_ms,
            "track": track,
            "queue": list(self.player.queue),
            "queue_index": self.player.index,
            "sleep_active": self.player.sleep_active(),
            "sleep_remaining": self.player.sleep_remaining_seconds(),
            "idle_minutes": self.idle_minutes,
            "quality_kbps": self.quality_kbps,
            "generation": self.generation,
            "error": self.error or self.player.error,
            "artwork_base_url": self.artwork_base_url,
        })

    def _local_artwork_url(self, value: Any) -> str:
        url = str(value or "").strip()
        if not url or not self.artwork_base_url:
            return url
        if url.startswith(self.artwork_base_url + "?"):
            return url
        parsed = urlsplit(url)
        if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" \
                and parsed.path == "/artwork":
            url = parse_qs(parsed.query).get("url", [""])[0].strip()
            if not url:
                return ""
            parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return f"{self.artwork_base_url}?url={quote(url, safe='')}"

    def _localize_images(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._localize_images(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self._local_artwork_url(item) if key == "imageUrl"
                else self._localize_images(item)
                for key, item in value.items()
            }
        return value

    def _fetch_artwork(self, url: str) -> tuple[str, bytes] | None:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        with self._artwork_lock:
            cached = self._artwork_cache.get(url)
            if cached:
                return cached
        try:
            request = Request(url, headers={
                "Accept": "image/*",
                "User-Agent": "omarchy-ytmusic-artwork/1.0",
            })
            with urlopen(request, timeout=10) as response:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    return None
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    return None
        except Exception:
            return None
        result = (content_type, data)
        with self._artwork_lock:
            self._artwork_cache[url] = result
            while len(self._artwork_cache) > 128:
                self._artwork_cache.pop(next(iter(self._artwork_cache)))
        return result

    def _start_artwork_proxy(self) -> None:
        if self._artwork_httpd:
            return
        backend = self

        class ArtworkHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlsplit(self.path)
                url = parse_qs(parsed.query).get("url", [""])[0]
                artwork = backend._fetch_artwork(url) if parsed.path == "/artwork" else None
                if not artwork:
                    self.send_error(404)
                    return
                content_type, data = artwork
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _format, *_args):
                pass

        self._artwork_httpd = ThreadingHTTPServer(("127.0.0.1", 0), ArtworkHandler)
        self._artwork_httpd.daemon_threads = True
        port = self._artwork_httpd.server_address[1]
        self.artwork_base_url = f"http://127.0.0.1:{port}/artwork"
        self._artwork_thread = threading.Thread(
            target=self._artwork_httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
        )
        self._artwork_thread.start()

    def _stop_artwork_proxy(self) -> None:
        httpd = self._artwork_httpd
        self._artwork_httpd = None
        self.artwork_base_url = ""
        if not httpd:
            return
        httpd.shutdown()
        httpd.server_close()

    def broadcast(self) -> None:
        self.generation += 1
        payload = dumps(event("state_changed", self.state())) + "\n"
        data = payload.encode("utf-8")
        with self._clients_lock:
            living = []
            for client in self._clients:
                try:
                    client.sendall(data)
                    living.append(client)
                except OSError:
                    try:
                        client.close()
                    except OSError:
                        pass
            self._clients[:] = living
        self._last_broadcast = time.time()

    def _on_player_change(self) -> None:
        now = time.time()
        # Position-only ticks are already sent on a 1s timer. Coalesce bursts.
        if now - self._last_broadcast < 0.05:
            return
        self.broadcast()

    def _radio_tracks(self, video_id: str) -> list[dict]:
        if not self.catalog:
            return []
        with self._catalog_lock:
            return self.catalog.watch_playlist(video_id)

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        version = message.get("v", PROTOCOL_VERSION)
        request_id = message.get("id")
        if version != PROTOCOL_VERSION:
            return response(request_id, False, code=ERROR_UNSUPPORTED_VERSION,
                            message="This plugin backend speaks protocol 1")
        command = str(message.get("command") or "").strip()
        try:
            result = self.dispatch(command, message)
            return response(request_id, True, result)
        except AuthError as exc:
            return response(request_id, False, code=ERROR_AUTH, message=str(exc))
        except CatalogError as exc:
            return response(request_id, False, code=ERROR_CATALOG, message=str(exc))
        except PlayerError as exc:
            return response(request_id, False, code=ERROR_PLAYBACK, message=str(exc))
        except ValueError as exc:
            return response(request_id, False, code=ERROR_INVALID_REQUEST, message=str(exc))
        except Exception as exc:
            traceback.print_exc()
            return response(request_id, False, code=ERROR_INVALID_REQUEST,
                            message=redact(str(exc)))

    def dispatch(self, command: str, message: dict[str, Any]) -> dict[str, Any]:
        self.player.note_activity()
        if command in ("hello", "ping", "get_state"):
            return self.state()
        if command == "setup_auth":
            return self.setup_auth(str(message.get("headers_raw") or ""))
        if command == "import_browser":
            return self.import_browser()
        if command == "logout":
            return self.logout()
        if command == "set_idle_minutes":
            self.idle_minutes = max(0, min(1440, int(message.get("minutes") or 0)))
            return self.state()
        if command == "set_quality":
            self.quality_kbps = 96 if int(message.get("kbps") or 320) <= 96 else (
                160 if int(message.get("kbps") or 320) <= 160 else 320)
            self.player.resolver.set_quality(self.quality_kbps)
            return self.state()
        if command == "play":
            self.player.play()
            return self.state()
        if command == "pause":
            self.player.pause()
            return self.state()
        if command == "toggle":
            self.player.toggle()
            return self.state()
        if command == "stop":
            self.player.stop()
            return self.state()
        if command == "next":
            self.player.next()
            return self.state()
        if command == "previous":
            self.player.previous()
            return self.state()
        if command == "seek":
            self.player.seek(int(message.get("position_ms") or 0))
            return self.state()
        if command == "set_volume":
            volume = message.get("volume")
            if volume is None:
                volume = message.get("value")
            self.player.set_volume(int(volume if volume is not None else 80))
            return self.state()
        if command == "set_shuffle":
            self.player.set_shuffle(bool(message.get("shuffle")))
            return self.state()
        if command == "set_repeat":
            self.player.set_repeat(str(message.get("mode") or "off"))
            return self.state()
        if command == "load":
            return self.load(message)
        if command == "add_to_queue":
            item = message.get("item") or {}
            if not isinstance(item, dict):
                raise ValueError("item is required")
            self.player.add_to_queue(item)
            return {"queue": list(self.player.queue)}
        if command == "search":
            return self.require_catalog().search(
                str(message.get("query") or ""),
                str(message.get("filter") or ""),
                int(message.get("limit") or 24),
            )
        if command == "browse":
            return self.browse(str(message.get("view") or "home"))
        if command == "get_playlist":
            return self.require_catalog().playlist(str(message.get("id") or ""))
        if command == "get_album":
            return self.require_catalog().album(str(message.get("id") or ""))
        if command == "get_artist":
            return self.require_catalog().artist(str(message.get("id") or ""))
        if command == "get_queue":
            return {"items": list(self.player.queue), "index": self.player.index}
        if command == "like":
            return self.like(str(message.get("video_id") or ""), bool(message.get("liked")))
        if command == "create_playlist":
            item = self.require_catalog().create_playlist(str(message.get("name") or ""))
            return {"playlist": item}
        if command == "add_to_playlist":
            self.require_catalog().add_to_playlist(
                str(message.get("playlist_id") or ""),
                str(message.get("video_id") or ""),
            )
            return {}
        if command == "sleep":
            self.player.set_sleep(
                minutes=float(message.get("minutes") or 0),
                after=str(message.get("after") or ""),
            )
            return self.state()
        if command == "cancel_sleep":
            self.player.set_sleep(0, "")
            return self.state()
        if not command:
            raise ValueError("missing command")
        raise ValueError(f"Unknown command: {command}")

    def require_catalog(self) -> Catalog:
        if not self.catalog:
            raise CatalogError("YouTube Music is unavailable")
        return self.catalog

    def setup_auth(self, headers_raw: str) -> dict[str, Any]:
        if not headers_raw.strip():
            raise AuthError("Paste the request headers from music.youtube.com")
        try:
            path = auth.save_headers(headers_raw, auth.default_auth_path())
        except Exception as exc:
            raise AuthError(redact(str(exc))) from exc
        return self._activate_auth(path)

    def import_browser(self) -> dict[str, Any]:
        try:
            path = auth.import_from_browser(auth.default_auth_path())
        except auth.BrowserAuthError as exc:
            raise AuthError(str(exc)) from exc
        except Exception as exc:
            raise AuthError(redact(str(exc))) from exc
        return self._activate_auth(path)

    def _activate_auth(self, path: Path) -> dict[str, Any]:
        self.auth_path = path
        self.start_catalog()
        if not self.signed_in:
            raise AuthError(self.error or "Those headers were not accepted")
        self.broadcast()
        return self.state()

    def logout(self) -> dict[str, Any]:
        try:
            self.player.stop()
        except Exception:
            pass
        auth.clear_auth(self.auth_path)
        self.start_catalog()
        self.broadcast()
        return self.state()

    def browse(self, view: str) -> dict[str, Any]:
        cat = self.require_catalog()
        with self._catalog_lock:
            if view == "home":
                return {"home": cat.home(), "signed_in": self.signed_in}
            if view == "history":
                return {"items": cat.history()}
            if view == "liked":
                return {"items": cat.liked()}
            if view == "playlists":
                return {"items": cat.playlists()}
            if view == "library_songs":
                return {"items": cat.library_songs()}
            if view == "library_albums":
                return {"items": cat.library_albums()}
            if view == "library_artists":
                return {"items": cat.library_artists()}
            if view == "library":
                return {
                    "songs": cat.library_songs(),
                    "albums": cat.library_albums(),
                    "artists": cat.library_artists(),
                    "playlists": cat.playlists(),
                    "liked": cat.liked(),
                    "history": cat.history(),
                }
        raise ValueError(f"Unknown view: {view}")

    def like(self, video_id: str, liked: bool) -> dict[str, Any]:
        if not video_id:
            raise ValueError("video_id is required")
        self.require_catalog().rate_song(video_id, liked)
        current = self.player.current
        if current and current.get("videoId") == video_id:
            current["liked"] = liked
        self.broadcast()
        return {"liked": liked}

    def load(self, message: dict[str, Any]) -> dict[str, Any]:
        items = message.get("items")
        index = int(message.get("index") or message.get("offset_index") or 0)
        radio = message.get("radio") is True
        video_id = str(message.get("video_id") or "")
        playlist_id = str(message.get("playlist_id") or "")
        album_id = str(message.get("album_id") or "")
        artist_id = str(message.get("artist_id") or "")
        play = message.get("play", True) is not False

        resolved: list[dict] = []
        if isinstance(items, list) and items:
            resolved = [item for item in items if isinstance(item, dict) and item.get("videoId")]
        elif playlist_id:
            resolved = self.require_catalog().playlist(playlist_id).get("tracks") or []
        elif album_id:
            resolved = self.require_catalog().album(album_id).get("tracks") or []
        elif artist_id:
            detail = self.require_catalog().artist(artist_id)
            resolved = detail.get("tracks") or []
            if not resolved and detail.get("radioId"):
                resolved = self.require_catalog().playlist(str(detail["radioId"])).get("tracks") or []
        elif video_id:
            seed = {
                "kind": "item",
                "type": "track",
                "id": video_id,
                "uri": f"ytm:track:{video_id}",
                "name": str(message.get("name") or "YouTube Music"),
                "subtitle": str(message.get("subtitle") or ""),
                "videoId": video_id,
                "externalUrl": f"https://music.youtube.com/watch?v={video_id}",
            }
            if radio or True:
                related = self._radio_tracks(video_id)
                resolved = related or [seed]
                if resolved and resolved[0].get("videoId") != video_id:
                    resolved = [seed] + [item for item in resolved if item.get("videoId") != video_id]
            else:
                resolved = [seed]
            index = 0

        if not resolved:
            raise PlayerError("Nothing playable was found")
        if video_id:
            for i, item in enumerate(resolved):
                if item.get("videoId") == video_id:
                    index = i
                    break
        self.player.load(resolved, index=index, play=play)
        return self.state()

    def serve(self, path: Path) -> None:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        self._start_artwork_proxy()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(8)
            server.settimeout(0.5)
        except Exception:
            self._stop_artwork_proxy()
            server.close()
            raise
        print(f"omarchy-ytmusic-backend listening on {path}", file=sys.stderr)
        idle_thread = threading.Thread(target=self._idle_watch, daemon=True)
        idle_thread.start()
        position_thread = threading.Thread(target=self._position_watch, daemon=True)
        position_thread.start()
        catalog_thread = threading.Thread(target=self._start_catalog_locked, daemon=True)
        catalog_thread.start()
        try:
            while not self._stop.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(target=self._client_loop, args=(client,), daemon=True)
                thread.start()
        finally:
            self._stop.set()
            self.player.shutdown()
            self._stop_artwork_proxy()
            try:
                server.close()
            except OSError:
                pass
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    def _start_catalog_locked(self) -> None:
        self.start_catalog()
        threading.Thread(target=self.player.resolver.warmup, daemon=True).start()
        self.broadcast()

    def _client_loop(self, client: socket.socket) -> None:
        client.settimeout(1.0)
        self.player.note_activity()
        with self._clients_lock:
            self._clients.append(client)
        try:
            client.sendall((dumps(event("state_changed", self.state())) + "\n").encode("utf-8"))
        except OSError:
            self._drop(client)
            return
        buffer = ""
        while not self._stop.is_set():
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                message = parse_line(line)
                if not message:
                    continue
                reply = self._localize_images(self.handle(message))
                try:
                    client.sendall((dumps(reply) + "\n").encode("utf-8"))
                except OSError:
                    self._drop(client)
                    return
        self._drop(client)

    def _drop(self, client: socket.socket) -> None:
        with self._clients_lock:
            self._clients = [item for item in self._clients if item is not client]
        try:
            client.close()
        except OSError:
            pass

    def _idle_watch(self) -> None:
        while not self._stop.is_set():
            time.sleep(15)
            with self._clients_lock:
                clients = len(self._clients)
            if idle_should_exit(
                idle_minutes=self.idle_minutes,
                playing=self.player.playing,
                client_count=clients,
                last_activity=self.player.last_activity,
                now=time.time(),
            ):
                print("omarchy-ytmusic-backend idle shutdown", file=sys.stderr)
                self._stop.set()
                return

    def _position_watch(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            if self.player.playing:
                self.broadcast()


class AuthError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Omarchy YouTube Music backend")
    parser.add_argument("--auth", default="", help="Path to ytmusicapi browser.json")
    parser.add_argument("--socket", default="", help="Unix socket path")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        from catalog import duration_ms, track_item
        sample = {
            "title": "Test Song",
            "videoId": "dQw4w9wgkcQ",
            "artists": [{"name": "Artist", "id": "UC123"}],
            "duration": "3:45",
            "thumbnails": [{"url": "https://example.com/a.jpg", "width": 226}],
        }
        item = track_item(sample)
        assert item and item["durationMs"] == 225000
        assert duration_ms({"duration": "1:02:03"}) == 3723000
        print("ok")
        return 0

    auth_path = Path(os.path.expanduser(args.auth)) if args.auth else None
    backend = Backend(auth_path)

    def handle_stop(signum, frame):
        backend._stop.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    path = Path(args.socket) if args.socket else socket_path()
    backend.serve(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
