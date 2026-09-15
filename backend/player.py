"""mpv-backed local playback with an owned queue."""

from __future__ import annotations

import json
import logging
import os
import random
import select
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from catalog import track_item


LOGGER = logging.getLogger(__name__)


class PlayerError(RuntimeError):
    pass


def media_title(item: dict | None) -> str:
    source = item or {}
    title = str(source.get("name") or source.get("title") or "").strip()
    if title:
        return title[:200]
    return "YouTube Music"


def media_artist(item: dict | None) -> str:
    source = item or {}
    artist = str(source.get("subtitle") or "").strip()
    if artist:
        return artist[:200]
    artists = source.get("artists")
    if isinstance(artists, list):
        names = [str(entry.get("name") or "").strip()
                 for entry in artists if isinstance(entry, dict)]
        artist = ", ".join(name for name in names if name)
        if artist:
            return artist[:200]
    return ""


def mpris_title(item: dict | None = None) -> str:
    title = media_title(item)
    artist = media_artist(item)
    if artist and artist.lower() not in title.lower():
        return f"{artist} - {title}"[:220]
    return title


def loadfile_command(url: str, item: dict | None = None) -> list:
    options = {"force-media-title": mpris_title(item)}
    return ["loadfile", url, "replace", -1, options]


def looks_like_stream_title(text: str) -> bool:
    value = str(text or "")
    lower = value.lower()
    return (
        "googlevideo.com" in lower
        or "videoplayback" in lower
        or "mime=audio" in lower
        or value.startswith("webm&")
        or "&ns=" in value
        or "&sig=" in value
    )


def mpv_command_line(binary: str, ipc_path: Path, mpris: str = "") -> list[str]:
    command = [
        binary,
        "--no-config",
        "--idle=yes",
        "--no-video",
        "--vo=null",
        "--force-window=no",
        "--no-terminal",
        "--audio-display=no",
        "--osc=no",
        "--load-scripts=no",
        "--keep-open=no",
        "--ytdl=no",
        "--ao=pipewire,pulse",
        "--clipboard-backends-clr",
        "--no-input-default-bindings",
        "--volume=80",
        "--title=Omarchy YouTube Music",
        "--audio-client-name=omarchy-ytmusic",
        f"--input-ipc-server={ipc_path}",
        "--msg-level=cplayer=info,ao=info,ffmpeg=warn",
    ]
    if mpris:
        command.append(f"--script={mpris}")
    return command


def mpv_env(source: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(source if source is not None else os.environ)
    for key in (
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "SWAYSOCK",
        "WAYLAND_SOCKET",
    ):
        env.pop(key, None)
    return env


class Mpv:
    def __init__(self, ipc_path: Path):
        self.ipc_path = ipc_path
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        mpv = shutil.which("mpv")
        if not mpv:
            raise PlayerError("mpv is not installed")
        self.ipc_path.parent.mkdir(parents=True, exist_ok=True)
        if self.ipc_path.exists():
            try:
                self.ipc_path.unlink()
            except OSError:
                pass
        log_path = self.ipc_path.parent / "mpv.log"
        command = mpv_command_line(mpv, self.ipc_path, _mpris_script())
        stderr = log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=mpv_env(),
                start_new_session=True,
            )
        finally:
            stderr.close()
        self._wait_for_socket()
        self._connect()
        self.command(["observe_property", 1, "pause"])
        self.command(["observe_property", 2, "eof-reached"])
        self.command(["observe_property", 3, "idle-active"])
        self.command(["observe_property", 4, "time-pos"])
        self.command(["observe_property", 5, "duration"])
        self.command(["observe_property", 6, "volume"])
        self.command(["observe_property", 7, "media-title"])

    def _wait_for_socket(self, timeout: float = 4.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ipc_path.exists():
                return
            if self.process and self.process.poll() is not None:
                raise PlayerError("mpv exited before the control socket appeared")
            time.sleep(0.05)
        raise PlayerError("mpv control socket did not appear")

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(self.ipc_path))
        sock.setblocking(False)
        self.sock = sock

    def stop(self) -> None:
        if self.sock:
            try:
                self.command(["quit"])
            except Exception:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        if self.ipc_path.exists():
            try:
                self.ipc_path.unlink()
            except OSError:
                pass

    def command(self, args: list[Any]) -> int:
        if not self.sock:
            raise PlayerError("mpv is not connected")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = json.dumps({"command": args, "request_id": request_id}) + "\n"
            self.sock.sendall(payload.encode("utf-8"))
            return request_id

    def poll_events(self, timeout: float = 0.2) -> list[dict]:
        if not self.sock:
            return []
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return []
        chunks = []
        while True:
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                break
            if not data:
                break
            chunks.append(data)
        if not chunks:
            return []
        text = b"".join(chunks).decode("utf-8", errors="replace")
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                events.append(message)
        return events


def _mpris_script() -> str:
    candidates = [
        "/usr/lib/mpv-mpris/mpris.so",
        "/usr/lib/mpv/scripts/mpris.so",
        "/usr/lib64/mpv-mpris/mpris.so",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


class StreamResolver:
    """Resolve YouTube audio in one persistent youtubei.js helper process."""

    def __init__(
        self,
        runtime_dir: Path | None = None,
        kbps: int = 320,
        resolver_script: Path | None = None,
    ):
        self.runtime_dir = runtime_dir or Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/omarchy-ytmusic-{os.getuid()}"
        )
        self.kbps = self._normalize_quality(kbps)
        self.resolver_script = resolver_script or self._default_script()
        self.cache_dir = self._default_cache_dir()
        self._cache: dict[str, tuple[float, str]] = {}
        self._cookie_header = ""
        self._configured_cookie: str | None = None
        self._process: subprocess.Popen | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._position_lock = threading.Lock()
        self._pending_position: tuple[str, int] | None = None
        self._position_thread: threading.Thread | None = None

    @staticmethod
    def _normalize_quality(kbps: int) -> int:
        value = int(kbps or 320)
        return 96 if value <= 96 else (160 if value <= 160 else 320)

    @staticmethod
    def _default_cache_dir() -> Path:
        root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
        return root / "omarchy-ytmusic" / "youtubei"

    @staticmethod
    def _default_script() -> Path:
        source = Path(__file__).resolve().parent / "resolver" / "resolver.mjs"
        data_root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        installed = data_root / "omarchy-ytmusic" / "resolver" / "resolver.mjs"
        return installed if installed.is_file() else source

    def set_quality(self, kbps: int) -> None:
        value = self._normalize_quality(kbps)
        with self._lock:
            if self.kbps != value:
                self.kbps = value
                self._cache.clear()

    def set_cookie_header(self, header: str) -> None:
        value = str(header or "").strip()
        with self._lock:
            if self._cookie_header != value:
                self._cookie_header = value
                self._configured_cookie = None
                self._cache.clear()

    def resolve(self, video_id: str, start_ms: int = 0) -> str:
        video_id = str(video_id or "").strip()
        if not video_id:
            raise PlayerError("Missing video id")
        start_ms = max(0, int(start_ms or 0))
        if self._shutdown.is_set():
            raise PlayerError("The stream resolver is shutting down")
        with self._lock:
            now = time.time()
            if not start_ms:
                cached = self._cache.get(video_id)
                if cached and cached[0] > now:
                    return cached[1]
            result = self._request_locked("resolve", {
                "video_id": video_id,
                "quality_kbps": self.kbps,
                "start_ms": start_ms,
            })
            url = str(result.get("url") or "")
            if not (url.startswith("https://") or url.startswith("http://127.0.0.1:")):
                raise PlayerError("Resolver returned an invalid audio URL")
            expires = float(result.get("expires_in_seconds") or 4 * 60 * 60)
            if not start_ms:
                self._cache[video_id] = (now + max(60.0, expires - 60.0), url)
            return url

    def prefetch(self, video_id: str) -> None:
        def worker() -> None:
            try:
                self.resolve(video_id)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def warmup(self) -> None:
        """Start the resolver and initialize its InnerTube client in the background."""
        with self._lock:
            if self._shutdown.is_set():
                return
            try:
                self._request_locked("ping", {})
            except Exception:
                self._stop_helper_locked()

    def update_position(self, video_id: str, position_ms: int) -> None:
        video_id = str(video_id or "").strip()
        if not video_id or self._shutdown.is_set():
            return
        with self._lock:
            if not self._process or self._process.poll() is not None:
                return
        with self._position_lock:
            self._pending_position = (video_id, max(0, int(position_ms or 0)))
            if self._position_thread and self._position_thread.is_alive():
                return
            self._position_thread = threading.Thread(
                target=self._flush_positions,
                daemon=True,
            )
            self._position_thread.start()

    def _flush_positions(self) -> None:
        while not self._shutdown.is_set():
            with self._position_lock:
                pending = self._pending_position
                self._pending_position = None
            if pending is None:
                with self._position_lock:
                    self._position_thread = None
                return
            try:
                with self._lock:
                    if self._process and self._process.poll() is None:
                        self._request_locked("position", {
                            "video_id": pending[0],
                            "position_ms": pending[1],
                        })
            except Exception:
                return

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._position_lock:
            self._pending_position = None
        process = self._process
        self._process = None
        self._configured_cookie = None
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except OSError:
                pass

    def _node_binary(self) -> str:
        configured = os.environ.get("OMARCHY_YTMUSIC_NODE", "").strip()
        if configured and os.path.isfile(configured):
            return configured
        return shutil.which("node") or ""

    def _start_helper_locked(self) -> None:
        if self._process and self._process.poll() is None:
            return
        if self._process:
            self._stop_helper_locked()
        if self._shutdown.is_set():
            raise PlayerError("The stream resolver is shutting down")
        node = self._node_binary()
        if not node:
            raise PlayerError("node is not installed; run the playback setup")
        if not self.resolver_script.is_file():
            raise PlayerError("The YouTube Music stream resolver is not installed")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.cache_dir.chmod(0o700)
        except OSError:
            pass
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.runtime_dir / "resolver.log"
        log_handle = log_path.open("ab")
        try:
            self._process = subprocess.Popen(
                [node, str(self.resolver_script), "--cache-dir", str(self.cache_dir)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_handle,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(self.resolver_script.parent),
                start_new_session=True,
            )
        finally:
            log_handle.close()

    def _stop_helper_locked(self) -> None:
        process = self._process
        self._process = None
        self._configured_cookie = None
        if not process:
            return
        for stream in (process.stdin, process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except OSError:
                    pass

    def _request_locked(self, command: str, fields: dict[str, Any]) -> dict[str, Any]:
        self._start_helper_locked()
        if self._configured_cookie != self._cookie_header:
            self._call_locked("configure", {"cookie": self._cookie_header})
            self._configured_cookie = self._cookie_header
        return self._call_locked(command, fields)

    def _call_locked(self, command: str, fields: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if not process or not process.stdin or not process.stdout:
            raise PlayerError("The stream resolver is not running")
        request_id = self._next_id
        self._next_id += 1
        payload = {"id": request_id, "command": command, **fields}
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 45.0)
            if not ready:
                raise PlayerError("The stream resolver timed out")
            line = process.stdout.readline()
        except PlayerError:
            self._stop_helper_locked()
            raise
        except (OSError, ValueError) as exc:
            self._stop_helper_locked()
            raise PlayerError("The stream resolver stopped") from exc
        if not line:
            raise PlayerError("The stream resolver stopped")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlayerError("The stream resolver returned invalid data") from exc
        if response.get("id") != request_id:
            raise PlayerError("The stream resolver response did not match the request")
        if response.get("ok") is not True:
            raise PlayerError(str(response.get("error") or "Could not resolve audio stream"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise PlayerError("The stream resolver returned an invalid result")
        return result


class QueuePlayer:
    def __init__(
        self,
        runtime_dir: Path,
        on_change: Callable[[], None] | None = None,
        catalog_radio: Callable[[str], list[dict]] | None = None,
    ):
        self.mpv = Mpv(runtime_dir / "mpv.sock")
        self.resolver = StreamResolver(runtime_dir=runtime_dir)
        self.on_change = on_change or (lambda: None)
        self.catalog_radio = catalog_radio
        self.queue: list[dict] = []
        self.index = -1
        self.shuffle = False
        self._play_history: list[int] = []
        self._history_position = -1
        self._shuffle_pending: list[int] = []
        self.repeat = "off"
        self.playing = False
        self.volume = 80
        self.muted = False
        self.volume_before_mute = 80
        self.position_ms = 0
        self.duration_ms = 0
        self._stream_start_ms = 0
        self._stream_started = False
        self._stream_position_seen = False
        self._stream_position_mode: str | None = None
        self._resume_after_load = False
        self.error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_eof = False
        # True between a track ending/failing and the replacement file loading.
        # Swallows the duplicate eof/end-file events mpv emits for the old file
        # so one end cannot advance the queue twice.
        self._ending = False
        self._generation = 0
        self._sleep_deadline = 0.0
        self._sleep_after = ""
        self._display_title = ""
        self.last_activity = time.time()

    @property
    def current(self) -> dict | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def snapshot_track(self) -> dict | None:
        item = self.current
        return dict(item) if item else None

    def ensure_started(self) -> None:
        if not self.mpv.running:
            self.mpv.start()
            self._stop.clear()
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
            self.mpv.command(["set_property", "volume", self.volume])

    def shutdown(self) -> None:
        self._stop.set()
        self.mpv.stop()
        self.resolver.shutdown()
        self.playing = False

    def load(self, items: list[dict], index: int = 0, play: bool = True) -> None:
        tracks = [item for item in items if isinstance(item, dict) and item.get("videoId")]
        if not tracks:
            raise PlayerError("Nothing playable in that selection")
        self.queue = tracks
        self.index = max(0, min(int(index or 0), len(tracks) - 1))
        self._play_history = [self.index]
        self._history_position = 0
        self._shuffle_pending = []
        if self.shuffle:
            self._prepare_shuffle_cycle()
        self.note_activity()
        self.ensure_started()
        self._play_current(start=play)

    def add_to_queue(self, item: dict) -> None:
        track = item if item.get("type") == "track" else track_item(item)
        if not track or not track.get("videoId"):
            raise PlayerError("That item cannot be queued")
        self.queue.append(track)
        if self.shuffle:
            self._shuffle_pending.insert(
                random.randrange(len(self._shuffle_pending) + 1),
                len(self.queue) - 1,
            )
        self.note_activity()
        if self.current and self.current.get("videoId"):
            nxt = self._upcoming_video_id()
            if nxt:
                self.resolver.prefetch(nxt)
        self.on_change()

    def play(self) -> None:
        if not self.current:
            raise PlayerError("Nothing is queued")
        self.ensure_started()
        self.mpv.command(["set_property", "pause", False])
        self._resume_after_load = True
        self.playing = self._stream_started and self._stream_position_seen
        self.note_activity()
        self.on_change()

    def pause(self) -> None:
        if not self.mpv.running:
            return
        self.mpv.command(["set_property", "pause", True])
        self._resume_after_load = False
        self.playing = False
        self.note_activity()
        self.on_change()

    def toggle(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        if self.mpv.running:
            try:
                self.mpv.command(["stop"])
            except Exception:
                pass
        self._resume_after_load = False
        self._stream_started = False
        self._stream_position_seen = False
        self.playing = False
        self.position_ms = 0
        self.note_activity()
        self.on_change()

    def next(self) -> None:
        self.note_activity()
        if self._advance():
            if not self._play_next_with_recovery():
                self.playing = False
                self.on_change()
        else:
            self.playing = False
            self.on_change()

    def previous(self) -> None:
        self.note_activity()
        if self.position_ms > 3000 and self.current:
            self.seek(0)
            return
        if self.shuffle and self._history_position > 0:
            self._history_position -= 1
            self.index = self._play_history[self._history_position]
            self._play_current(start=True)
            return
        if self.shuffle:
            if self.repeat == "context" and self.queue:
                self.index = len(self.queue) - 1
                self._shuffle_pending = [
                    index for index in self._shuffle_pending if index != self.index
                ]
                self._remember_index(self.index)
                self._play_current(start=True)
            else:
                self.seek(0)
            return
        if self.index > 0:
            self.index -= 1
            self._remember_index(self.index)
            self._play_current(start=True)
        elif self.repeat == "context" and self.queue:
            self.index = len(self.queue) - 1
            self._remember_index(self.index)
            self._play_current(start=True)
        else:
            self.seek(0)

    def seek(self, position_ms: int) -> None:
        if not self.mpv.running:
            return
        seconds = max(0, int(position_ms or 0)) / 1000.0
        item = self.current
        resume = self.playing
        if item:
            url = self.resolver.resolve(str(item.get("videoId") or ""), int(seconds * 1000))
            self._stream_start_ms = int(seconds * 1000)
            self._stream_started = False
            self._stream_position_seen = False
            self._stream_position_mode = None
            self._resume_after_load = resume
            self.mpv.command(loadfile_command(url, item))
            self.mpv.command(["set_property", "pause", not resume])
            self.playing = False
        self.position_ms = int(seconds * 1000)
        self.note_activity()
        self.on_change()

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        self.volume = volume
        self.muted = volume <= 0
        if volume > 0:
            self.volume_before_mute = volume
        if self.mpv.running:
            self.mpv.command(["set_property", "volume", volume])
        self.note_activity()
        self.on_change()

    def set_shuffle(self, value: bool) -> None:
        enabled = bool(value)
        if enabled and not self.shuffle and self.current:
            self._shuffle_pending = []
            self.shuffle = True
            self._prepare_shuffle_cycle()
        else:
            self.shuffle = enabled
            if not enabled:
                self._shuffle_pending = []
        self.note_activity()
        self.on_change()

    def set_repeat(self, mode: str) -> None:
        if mode not in ("off", "context", "track"):
            mode = "off"
        self.repeat = mode
        self.note_activity()
        self.on_change()

    def cycle_repeat(self) -> str:
        nxt = {"off": "context", "context": "track", "track": "off"}[self.repeat]
        self.set_repeat(nxt)
        return nxt

    def set_sleep(self, minutes: float = 0, after: str = "") -> None:
        if after in ("track", "context"):
            self._sleep_after = after
            self._sleep_deadline = 0.0
        elif minutes > 0:
            self._sleep_deadline = time.time() + minutes * 60
            self._sleep_after = ""
        else:
            self._sleep_deadline = 0.0
            self._sleep_after = ""
        self.note_activity()
        self.on_change()

    def sleep_active(self) -> bool:
        return self._sleep_deadline > 0 or bool(self._sleep_after)

    def sleep_remaining_seconds(self) -> int:
        if self._sleep_deadline <= 0:
            return 0
        return max(0, int(self._sleep_deadline - time.time()))

    def note_activity(self) -> None:
        self.last_activity = time.time()

    def _remember_index(self, index: int) -> None:
        if (self._play_history and self._history_position >= 0
                and self._play_history[self._history_position] == index):
            self.index = index
            return
        if self._history_position + 1 < len(self._play_history):
            self._play_history = self._play_history[:self._history_position + 1]
        self._play_history.append(index)
        self._history_position = len(self._play_history) - 1
        self.index = index

    def _prepare_shuffle_cycle(self, excluded: set[int] | None = None) -> None:
        blocked = set(self._play_history)
        blocked.update(excluded or set())
        candidates = [
            index for index in range(len(self.queue))
            if index != self.index and index not in blocked
        ]
        if not candidates:
            candidates = [
                index for index in range(len(self.queue))
                if index != self.index and index not in (excluded or set())
            ]
        random.shuffle(candidates)
        self._shuffle_pending = candidates

    def _upcoming_index(self) -> int | None:
        if self.shuffle:
            if self._history_position + 1 < len(self._play_history):
                return self._play_history[self._history_position + 1]
            if not self._shuffle_pending and self.repeat == "context":
                self._prepare_shuffle_cycle()
            return self._shuffle_pending[0] if self._shuffle_pending else None
        nxt = self.index + 1
        return nxt if 0 <= nxt < len(self.queue) else None

    def _upcoming_video_id(self) -> str:
        nxt = self._upcoming_index()
        if nxt is not None:
            return str(self.queue[nxt].get("videoId") or "")
        return ""

    def _publish_title(self, item: dict | None = None) -> None:
        title = mpris_title(item if item is not None else self.current)
        self._display_title = title
        if not self.mpv.running:
            return
        try:
            self.mpv.command(["set_property", "force-media-title", title])
        except Exception:
            pass

    def _play_current(self, start: bool = True, expose_error: bool = True) -> None:
        item = self.current
        if not item:
            raise PlayerError("Nothing is queued")
        video_id = str(item.get("videoId") or "")
        self.error = ""
        self._stream_start_ms = 0
        self._stream_started = False
        self._stream_position_seen = False
        self._stream_position_mode = None
        self._resume_after_load = start
        self.ensure_started()
        self._publish_title(item)
        try:
            url = self.resolver.resolve(video_id)
            self._publish_title(item)
            self.mpv.command(loadfile_command(url, item))
            self.mpv.command(["set_property", "pause", not start])
            # Wait for mpv's playback-restart event before advancing the UI
            # clock; loadfile may spend time buffering the first segment.
            self.playing = False
            self.position_ms = 0
            self.duration_ms = int(item.get("durationMs") or 0)
        except Exception as exc:
            if expose_error:
                self.error = str(exc)
            self.playing = False
            if expose_error:
                self.on_change()
            raise PlayerError(str(exc)) from exc
        nxt = self._upcoming_video_id()
        if nxt:
            self.resolver.prefetch(nxt)
        elif self.catalog_radio and (
            (self.shuffle and not self._shuffle_pending)
            or (not self.shuffle and len(self.queue) - self.index <= 2)
        ):
            self._fill_radio(video_id)
        self._generation += 1
        self.on_change()

    def _fill_radio(self, video_id: str) -> None:
        def worker() -> None:
            try:
                related = self.catalog_radio(video_id) if self.catalog_radio else []
            except Exception:
                related = []
            if not related:
                return
            with self._lock:
                seen = {str(item.get("videoId") or "") for item in self.queue}
                added = 0
                for item in related:
                    vid = str(item.get("videoId") or "")
                    if not vid or vid in seen:
                        continue
                    self.queue.append(item)
                    if self.shuffle:
                        self._shuffle_pending.insert(
                            random.randrange(len(self._shuffle_pending) + 1),
                            len(self.queue) - 1,
                        )
                    seen.add(vid)
                    added += 1
                    if added >= 24:
                        break
                nxt = self._upcoming_video_id()
            if nxt:
                self.resolver.prefetch(nxt)
            if added:
                self.on_change()
        threading.Thread(target=worker, daemon=True).start()

    def _play_current_with_retries(self, attempts: int = 3) -> PlayerError | None:
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                self._play_current(start=True, expose_error=False)
                return None
            except PlayerError as exc:
                last_error = exc
                LOGGER.warning(
                    "track playback failed video_id=%s attempt=%d/%d error=%s",
                    (self.current or {}).get("videoId", ""),
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(0.25 * (2 ** (attempt - 1)))
        return last_error

    def _next_index_after_failure(self, failed: set[int]) -> int | None:
        if self.shuffle:
            while self._shuffle_pending:
                index = self._shuffle_pending.pop(0)
                if index not in failed and index != self.index:
                    return index
            if self.repeat == "context":
                self._prepare_shuffle_cycle(failed)
                while self._shuffle_pending:
                    index = self._shuffle_pending.pop(0)
                    if index not in failed and index != self.index:
                        return index
            return None
        for index in range(self.index + 1, len(self.queue)):
            if index not in failed:
                return index
        if self.repeat == "context":
            for index in range(len(self.queue)):
                if index not in failed:
                    return index
        return None

    def _play_next_with_recovery(self) -> bool:
        failed: set[int] = set()
        last_error = None
        while len(failed) < len(self.queue):
            if self.index in failed:
                self.error = str(last_error or "Playback failed")
                return False
            failed.add(self.index)
            last_error = self._play_current_with_retries()
            if last_error is None:
                return True
            next_index = self._next_index_after_failure(failed)
            if next_index is None:
                self.error = str(last_error)
                return False
            LOGGER.warning(
                "skipping unplayable track video_id=%s",
                (self.current or {}).get("videoId", ""),
            )
            self._remember_index(next_index)
        self.error = str(last_error or "Playback failed")
        return False

    def _advance(self) -> bool:
        if self._sleep_after == "track":
            self._sleep_after = ""
            return False
        if self.repeat == "track" and self.current:
            return True
        if self.shuffle:
            if self._history_position + 1 < len(self._play_history):
                self._history_position += 1
                self.index = self._play_history[self._history_position]
                return True
            if not self._shuffle_pending:
                if self.repeat == "context":
                    self._prepare_shuffle_cycle()
                elif len(self.queue) == 1:
                    return False
            if self._shuffle_pending:
                self._remember_index(self._shuffle_pending.pop(0))
                return True
            return False
        if self.index + 1 < len(self.queue):
            self._remember_index(self.index + 1)
            return True
        if self.repeat == "context" and self.queue:
            if self._sleep_after == "context":
                self._sleep_after = ""
                return False
            self._remember_index(0)
            return True
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._sleep_deadline and time.time() >= self._sleep_deadline:
                self._sleep_deadline = 0.0
                try:
                    self.pause()
                except Exception:
                    pass
                continue
            try:
                events = self.mpv.poll_events(0.25)
            except Exception:
                time.sleep(0.2)
                continue
            changed = False
            eof = False
            failed = False
            for event in events:
                name = event.get("event")
                if name == "property-change":
                    prop = event.get("name")
                    value = event.get("data")
                    if prop == "pause":
                        if value is True:
                            self.playing = False
                            changed = True
                        elif (
                            value is False
                            and self._stream_started
                            and self._stream_position_seen
                        ):
                            self.playing = True
                            changed = True
                    elif prop == "time-pos" and isinstance(value, (int, float)):
                        if not self._stream_started:
                            continue
                        raw_ms = int(max(0, value) * 1000)
                        if raw_ms == 0:
                            continue
                        if self._stream_position_mode is None:
                            if self._stream_start_ms > 2000 and raw_ms < 1000:
                                continue
                            if self._stream_start_ms <= 2000:
                                self._stream_position_mode = "relative"
                            elif raw_ms >= self._stream_start_ms - 1000:
                                self._stream_position_mode = "absolute"
                            else:
                                self._stream_position_mode = "relative"
                        self.position_ms = (
                            raw_ms
                            if self._stream_position_mode == "absolute"
                            else self._stream_start_ms + raw_ms
                        )
                        if not self._stream_position_seen:
                            self._stream_position_seen = True
                            if self._resume_after_load:
                                self.playing = True
                                changed = True
                        item = self.current
                        if item:
                            self.resolver.update_position(
                                str(item.get("videoId") or ""),
                                self.position_ms,
                            )
                    elif prop == "duration" and isinstance(value, (int, float)) and value > 0:
                        item = self.current
                        self.duration_ms = int(item.get("durationMs") or 0) if item else 0
                        if not self.duration_ms:
                            self.duration_ms = self._stream_start_ms + int(value * 1000)
                        changed = True
                    elif prop == "volume" and isinstance(value, (int, float)):
                        self.volume = int(max(0, min(100, value)))
                    elif prop == "eof-reached" and value is True and not self._ending:
                        eof = True
                    elif prop == "media-title":
                        shown = str(value or "")
                        if self._display_title and (
                            looks_like_stream_title(shown) or shown != self._display_title
                        ):
                            self._publish_title()
                elif name == "file-loaded":
                    self._stream_started = False
                    self._stream_position_seen = False
                    self._stream_position_mode = None
                    self._ending = False
                    self._publish_title()
                elif name == "playback-restart":
                    self._publish_title()
                    self._stream_started = True
                    self._stream_position_mode = None
                    self.playing = self._stream_position_seen
                    self.error = ""
                    self._ending = False
                    changed = True
                elif name == "end-file" and not self._ending:
                    reason = str(event.get("reason") or "")
                    if reason in ("eof", "0"):
                        eof = True
                    elif reason == "error":
                        failed = True
            if eof or failed:
                # A finished or failed track must always move the queue on. A
                # stream error used to stop playback for good, and a missed
                # eof used to strand the player on the last song.
                self._ending = True
                if failed:
                    # Retry the current song, then skip unplayable tracks,
                    # exactly like a queue transition.
                    if not self._play_next_with_recovery():
                        self.playing = False
                        changed = True
                elif self._advance():
                    if not self._play_next_with_recovery():
                        self.playing = False
                        changed = True
                else:
                    if self.duration_ms > 0:
                        self.position_ms = self.duration_ms
                    self.playing = False
                    changed = True
            if changed:
                self.on_change()
