"""Persisted playback session, so the player survives backend restarts.

The backend is socket-activated and may exit while paused. Keeping the queue,
current track, position, and volume on disk lets the next start show the last
track (paused) instead of an empty player.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SESSION_VERSION = 1
MAX_QUEUE = 500
MIN_VOLUME = 0
MAX_VOLUME = 100
REPEAT_MODES = ("off", "context", "track")


def default_state_path() -> Path:
    root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(root) / "omarchy-ytmusic" / "session.json"


def _int(value: Any, default: int = 0, minimum: int | None = None,
         maximum: int | None = None) -> int:
    if isinstance(value, bool):
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def clean_track(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    video_id = str(item.get("videoId") or "").strip()
    if not video_id:
        return None
    track = dict(item)
    track["videoId"] = video_id
    return track


def sanitize(state: Any) -> dict | None:
    """Return a JSON-safe session dict, or None when nothing is playable."""
    if not isinstance(state, dict):
        return None
    queue: list[dict] = []
    for entry in state.get("queue") or []:
        track = clean_track(entry)
        if track:
            queue.append(track)
        if len(queue) >= MAX_QUEUE:
            break
    if not queue:
        return None

    index = _int(state.get("index"), 0, 0, len(queue) - 1)
    duration = _int(state.get("duration_ms"), 0, 0)
    position = _int(state.get("position_ms"), 0, 0)
    if duration:
        position = min(position, duration)
    volume = _int(state.get("volume"), 80, MIN_VOLUME, MAX_VOLUME)
    muted = bool(state.get("muted")) and volume <= 0
    repeat = str(state.get("repeat") or "off")
    return {
        "version": SESSION_VERSION,
        "queue": queue,
        "index": index,
        "position_ms": position,
        "duration_ms": duration,
        "volume": volume,
        "muted": muted,
        "volume_before_mute": _int(
            state.get("volume_before_mute"), volume or 80, MIN_VOLUME, MAX_VOLUME),
        "shuffle": bool(state.get("shuffle")),
        "repeat": repeat if repeat in REPEAT_MODES else "off",
        "updated_at": _int(state.get("updated_at"), 0, 0),
    }


def load_session(path: Path | str | None) -> dict | None:
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    return sanitize(raw)


def save_session(path: Path | str | None, state: Any) -> bool:
    """Write the session atomically. Never raises for bad data or I/O."""
    if not path:
        return False
    clean = sanitize(state)
    if clean is None:
        return False
    try:
        payload = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    path = Path(path)
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".session-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = ""
    except OSError:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True
