"""MPRIS MediaPlayer2 service owned by the backend.

mpv-mpris only exists while mpv is alive, calls itself "mpv", and can expose
nothing but a title for a YouTube stream. This service owns the bus name for the
whole backend lifetime, so remote clients (KDE Connect, Flux, playerctl) see a
stable player with real artist/album/artwork metadata, and can control playback
even before a stream has been resolved.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
from typing import Any, Callable

from player import media_artist, media_title

LOGGER = logging.getLogger(__name__)

BUS_NAME = "org.mpris.MediaPlayer2.omarchy_ytmusic"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
ROOT_INTERFACE = "org.mpris.MediaPlayer2"
PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
NO_TRACK_PATH = f"{OBJECT_PATH}/TrackList/NoTrack"
ERROR_UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
ERROR_INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"
ERROR_READ_ONLY = "org.freedesktop.DBus.Error.PropertyReadOnly"
ERROR_NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"
ERROR_FAILED = "org.freedesktop.DBus.Error.Failed"

LOOP_STATUS = {"off": "None", "context": "Playlist", "track": "Track"}
REPEAT_MODE = {value: key for key, value in LOOP_STATUS.items()}


def playback_status(state: dict[str, Any]) -> str:
    if not isinstance(state.get("track"), dict) or not state["track"]:
        return "Stopped"
    return "Playing" if state.get("playing") else "Paused"


def loop_status(repeat: Any) -> str:
    return LOOP_STATUS.get(str(repeat or "off"), "None")


def repeat_mode(status: Any) -> str:
    return REPEAT_MODE.get(str(status or ""), "off")


def volume_ratio(state: dict[str, Any]) -> float:
    if state.get("muted"):
        return 0.0
    try:
        volume = int(state.get("volume") or 0)
    except (TypeError, ValueError):
        volume = 0
    return max(0, min(100, volume)) / 100.0


def track_object_path(video_id: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", str(video_id or "").strip())
    return f"{OBJECT_PATH}/Track/{safe or 'unknown'}"


def artist_names(track: dict[str, Any]) -> list[str]:
    names: list[str] = []
    artists = track.get("artists")
    if isinstance(artists, list):
        for entry in artists:
            if isinstance(entry, dict):
                name = str(entry.get("name") or "").strip()
                if name:
                    names.append(name)
    if not names:
        subtitle = str(track.get("subtitle") or "").strip()
        if subtitle:
            names = [part.strip() for part in subtitle.split(",") if part.strip()]
    return names[:12]


def album_name(track: dict[str, Any]) -> str:
    album = track.get("album")
    if isinstance(album, dict):
        return str(album.get("name") or "").strip()
    if isinstance(album, str):
        return album.strip()
    item = track.get("albumItem")
    if isinstance(item, dict):
        return str(item.get("name") or "").strip()
    return ""


def metadata(state: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """MPRIS Metadata as {name: (dbus signature, value)}."""
    track = state.get("track")
    if not isinstance(track, dict) or not track.get("videoId"):
        return {"mpris:trackid": ("o", NO_TRACK_PATH)}
    result: dict[str, tuple[str, Any]] = {
        "mpris:trackid": ("o", track_object_path(track.get("videoId"))),
        "xesam:title": ("s", media_title(track)),
    }
    try:
        duration = int(state.get("duration_ms") or track.get("durationMs") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        result["mpris:length"] = ("x", duration * 1000)
    art = str(track.get("imageUrl") or "").strip()
    if art.startswith(("http://", "https://")):
        result["mpris:artUrl"] = ("s", art)
    names = artist_names(track)
    artist = media_artist(track)
    if not names and artist:
        names = [artist]
    if names:
        result["xesam:artist"] = ("as", names)
        result["xesam:albumArtist"] = ("as", names[:1])
    album = album_name(track)
    if album:
        result["xesam:album"] = ("s", album)
    return result


def root_properties(identity: str, desktop_entry: str) -> dict[str, tuple[str, Any]]:
    return {
        "Identity": ("s", identity),
        "DesktopEntry": ("s", desktop_entry),
        "CanQuit": ("b", False),
        "CanRaise": ("b", False),
        "HasTrackList": ("b", False),
        "SupportedUriSchemes": ("as", []),
        "SupportedMimeTypes": ("as", []),
    }


def player_properties(state: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    track = state.get("track")
    has_track = isinstance(track, dict) and bool(track)
    return {
        "PlaybackStatus": ("s", playback_status(state)),
        "LoopStatus": ("s", loop_status(state.get("repeat"))),
        "Rate": ("d", 1.0),
        "Shuffle": ("b", bool(state.get("shuffle"))),
        "Metadata": ("a{sv}", metadata(state)),
        "Volume": ("d", volume_ratio(state)),
        "Position": ("x", max(0, int(state.get("position_ms") or 0)) * 1000),
        "MinimumRate": ("d", 0.01),
        "MaximumRate": ("d", 100.0),
        "CanGoNext": ("b", bool(state.get("queue"))),
        "CanGoPrevious": ("b", bool(state.get("queue"))),
        "CanPlay": ("b", has_track),
        "CanPause": ("b", has_track),
        "CanSeek": ("b", has_track),
        "CanControl": ("b", True),
    }


def _signature(props: dict[str, tuple[str, Any]]) -> str:
    stable = {key: value for key, value in props.items() if key != "Position"}
    return json.dumps(stable, sort_keys=True, default=str)


INTROSPECTION_XML = f"""<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{ROOT_INTERFACE}">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="{PLAYER_INTERFACE}">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg name="Offset" type="x" direction="in"/>
    </method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <method name="OpenUri">
      <arg name="Uri" type="s" direction="in"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{{sv}}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="Interface" type="s" direction="in"/>
      <arg name="Property" type="s" direction="in"/>
      <arg name="Value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="Interface" type="s" direction="in"/>
      <arg name="Properties" type="a{{sv}}" direction="out"/>
    </method>
    <method name="Set">
      <arg name="Interface" type="s" direction="in"/>
      <arg name="Property" type="s" direction="in"/>
      <arg name="Value" type="v" direction="in"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg name="Xml" type="s" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
  </interface>
</node>
"""


def _import_jeepney():
    try:
        from jeepney import (  # noqa: F401
            DBusAddress, DBusNameFlags, HeaderFields, MessageType, new_error,
            new_method_return, new_signal,
        )
        from jeepney.bus_messages import message_bus  # noqa: F401
        from jeepney.io.blocking import Proxy, open_dbus_connection  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the installed venv
        return None
    return {
        "DBusAddress": DBusAddress,
        "DBusNameFlags": DBusNameFlags,
        "HeaderFields": HeaderFields,
        "MessageType": MessageType,
        "new_error": new_error,
        "new_method_return": new_method_return,
        "new_signal": new_signal,
        "message_bus": message_bus,
        "Proxy": Proxy,
        "open_dbus_connection": open_dbus_connection,
    }


class MprisPlayer:
    """Serve org.mpris.MediaPlayer2 over the session bus in a daemon thread."""

    def __init__(
        self,
        *,
        state_provider: Callable[[], dict[str, Any]],
        controls: dict[str, Callable[..., Any]],
        identity: str = "YouTube Music",
        desktop_entry: str = "omarchy-ytmusic",
        bus_name: str = BUS_NAME,
    ):
        self.state_provider = state_provider
        self.controls = controls
        self.identity = identity
        self.desktop_entry = desktop_entry
        self.bus_name = bus_name
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._outbox: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._signature: str | None = None
        self._jeepney = _import_jeepney()
        self._bus_ready = False

    @property
    def available(self) -> bool:
        return self._jeepney is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.available:
            LOGGER.warning("python jeepney is missing; MPRIS control is disabled")
            return False
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mpris", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None

    def update(self, state: dict[str, Any] | None = None) -> None:
        """Queue a PropertiesChanged signal when something actually changed."""
        if not self._bus_ready or not self._jeepney:
            return
        if state is None:
            try:
                state = self.state_provider()
            except Exception:
                return
        try:
            props = player_properties(state)
        except Exception:
            return
        signature = _signature(props)
        with self._lock:
            if signature == self._signature:
                return
            self._signature = signature
        changed = {key: value for key, value in props.items() if key != "Position"}
        self._outbox.put(
            self._jeepney["new_signal"](
                self._address(PROPERTIES_INTERFACE),
                "PropertiesChanged",
                "sa{sv}as",
                (PLAYER_INTERFACE, changed, []),
            )
        )

    def _emit_seeked(self, position_ms: int) -> None:
        if not self._bus_ready or not self._jeepney:
            return
        self._outbox.put(
            self._jeepney["new_signal"](
                self._address(PLAYER_INTERFACE),
                "Seeked",
                "x",
                (max(0, int(position_ms)) * 1000,),
            )
        )

    def _address(self, interface: str):
        return self._jeepney["DBusAddress"](
            OBJECT_PATH, bus_name=self.bus_name, interface=interface)

    def _run(self) -> None:
        jeepney = self._jeepney
        if not jeepney:
            return
        conn = None
        try:
            conn = jeepney["open_dbus_connection"](bus="SESSION")
            bus = jeepney["Proxy"](jeepney["message_bus"], conn)
            reply = bus.RequestName(
                self.bus_name, jeepney["DBusNameFlags"].do_not_queue)
            if not reply or reply[0] not in (1, 4):
                LOGGER.warning(
                    "another process owns %s; MPRIS control is disabled", self.bus_name)
                return
            self._bus_ready = True
            self.update()
            while not self._stop.is_set():
                self._flush(conn)
                try:
                    message = conn.receive(timeout=0.1)
                except TimeoutError:
                    continue
                if message.header.message_type is jeepney["MessageType"].method_call:
                    response = self.handle(message)
                    if response is not None:
                        conn.send(response)
        except Exception as exc:  # pragma: no cover - needs a live session bus
            LOGGER.warning("MPRIS service stopped: %s", exc)
        finally:
            self._bus_ready = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _flush(self, conn) -> None:
        while True:
            try:
                message = self._outbox.get_nowait()
            except queue.Empty:
                return
            try:
                conn.send(message)
            except Exception:
                return

    def handle(self, message):
        """Dispatch one method call and return the reply message."""
        jeepney = self._jeepney
        fields = message.header.fields
        interface = fields.get(jeepney["HeaderFields"].interface)
        member = fields.get(jeepney["HeaderFields"].member)
        try:
            state = self.state_provider()
        except Exception:
            state = {}
        if interface == PROPERTIES_INTERFACE:
            return self._handle_properties(message, member, state)
        if interface == ROOT_INTERFACE:
            return self._handle_root(message, member)
        if interface == PLAYER_INTERFACE:
            return self._handle_player(message, member, state)
        if interface == "org.freedesktop.DBus.Introspectable" and member == "Introspect":
            return jeepney["new_method_return"](message, "s", (INTROSPECTION_XML,))
        if interface == "org.freedesktop.DBus.Peer" and member == "Ping":
            return jeepney["new_method_return"](message)
        return self._error(message, ERROR_UNKNOWN_METHOD, f"Unknown method {member}")

    def _properties_for(self, interface: str, state: dict[str, Any]):
        if interface == ROOT_INTERFACE:
            return root_properties(self.identity, self.desktop_entry)
        if interface == PLAYER_INTERFACE:
            return player_properties(state)
        return None

    def _handle_properties(self, message, member, state):
        jeepney = self._jeepney
        body = message.body
        if member == "Get" and len(body) >= 2:
            interface, name = str(body[0]), str(body[1])
            props = self._properties_for(interface, state)
            if props is None:
                return self._error(
                    message, ERROR_INVALID_ARGS, f"Unknown interface {interface}")
            if name not in props:
                return self._error(
                    message, ERROR_INVALID_ARGS, f"Unknown property {name}")
            return jeepney["new_method_return"](message, "v", (props[name],))
        if member == "GetAll" and len(body) >= 1:
            interface = str(body[0])
            props = self._properties_for(interface, state)
            if props is None:
                return self._error(
                    message, ERROR_INVALID_ARGS, f"Unknown interface {interface}")
            return jeepney["new_method_return"](message, "a{sv}", (props,))
        if member == "Set" and len(body) >= 3:
            interface, name, variant = str(body[0]), str(body[1]), body[2]
            return self._set_property(message, interface, name, variant, state)
        return self._error(message, ERROR_UNKNOWN_METHOD, f"Unknown method {member}")

    def _set_property(self, message, interface, name, variant, state):
        jeepney = self._jeepney
        if interface != PLAYER_INTERFACE:
            return self._error(message, ERROR_READ_ONLY, "That property is read-only")
        signature, value = variant if isinstance(variant, (tuple, list)) else ("", variant)
        try:
            if name == "Volume" and signature == "d":
                self.controls["set_volume"](int(round(float(value) * 100)))
            elif name == "LoopStatus" and signature == "s":
                self.controls["set_loop"](repeat_mode(value))
            elif name == "Shuffle" and signature == "b":
                self.controls["set_shuffle"](bool(value))
            elif name == "Rate" and signature == "d":
                pass
            else:
                return self._error(
                    message, ERROR_READ_ONLY, f"Property {name} is read-only")
        except Exception as exc:
            return self._error(message, ERROR_FAILED, str(exc))
        self.update()
        return jeepney["new_method_return"](message)

    def _handle_root(self, message, member):
        jeepney = self._jeepney
        if member == "Raise":
            return jeepney["new_method_return"](message)
        return self._error(
            message, ERROR_NOT_SUPPORTED, f"{member} is not supported")

    def _handle_player(self, message, member, state):
        jeepney = self._jeepney
        calls = {
            "Play": lambda: self.controls["play"](),
            "Pause": lambda: self.controls["pause"](),
            "PlayPause": lambda: self.controls["play_pause"](),
            "Stop": lambda: self.controls["stop"](),
            "Next": lambda: self.controls["next"](),
            "Previous": lambda: self.controls["previous"](),
        }
        if member in calls:
            try:
                calls[member]()
            except Exception as exc:
                return self._error(message, ERROR_FAILED, str(exc))
            self.update()
            return jeepney["new_method_return"](message)
        if member == "Seek" and len(message.body) >= 1:
            try:
                offset_ms = int(message.body[0]) // 1000
            except (TypeError, ValueError):
                return self._error(message, ERROR_INVALID_ARGS, "Invalid offset")
            position = max(0, int(state.get("position_ms") or 0) + offset_ms)
            try:
                self.controls["seek"](position)
            except Exception as exc:
                return self._error(message, ERROR_FAILED, str(exc))
            self._emit_seeked(position)
            return jeepney["new_method_return"](message)
        if member == "SetPosition" and len(message.body) >= 2:
            trackid, position_us = str(message.body[0]), int(message.body[1]
                                                              if message.body[1] else 0)
            track = state.get("track") or {}
            current = track_object_path(track.get("videoId")) if track else NO_TRACK_PATH
            if trackid and trackid != current:
                return jeepney["new_method_return"](message)
            position = max(0, position_us // 1000)
            try:
                self.controls["seek"](position)
            except Exception as exc:
                return self._error(message, ERROR_FAILED, str(exc))
            self._emit_seeked(position)
            return jeepney["new_method_return"](message)
        if member == "OpenUri":
            return self._error(
                message, ERROR_NOT_SUPPORTED, "OpenUri is not supported")
        return self._error(message, ERROR_UNKNOWN_METHOD, f"Unknown method {member}")

    def _error(self, message, name: str, description: str):
        jeepney = self._jeepney
        return jeepney["new_error"](message, name, "s", (description,))
