# Technical notes

The shell layout, plugin kinds, and much of the player UI started from
[Omarchy Spotify](https://github.com/stappmus/Omarchy-Spotify). Catalog access
and local playback are YouTube Music specific.

## Architecture

Omarchy YouTube Music runs as a plugin inside Omarchy's existing `omarchy-shell`
Quickshell process. It provides a shared service, a bar widget, and a
lazy-loaded panel. There is no embedded website or browser engine.

Catalog data and account operations use the unofficial
[`ytmusicapi`](https://github.com/sigma67/ytmusicapi) client. Local audio is
**mpv**, with stream URLs from a persistent Node helper using
[`youtubei.js`](https://github.com/LuanRT/YouTube.js) and
[`googlevideo`](https://github.com/LuanRT/googlevideo). The helper uses an
authenticated browser-like InnerTube session, generates a content-bound Web
PO token per video, and requests audio through YouTube's SABR/UMP protocol.
`SabrStreamingAdapter` builds the protobuf requests, `SabrUmpProcessor` handles
protocol directives, and the helper extracts media clusters from UMP responses.
It exposes those clusters as one local chunked HTTP response; it does not make
direct `googlevideo` range requests. mpv is
launched headless (`--vo=null`, no Wayland/X display) and uses the D-Bus-safe
client name `omarchy-ytmusic`. Each track still sets `force-media-title` for log
and window titles, but `mpv-mpris` is deliberately not loaded: the backend owns
MPRIS itself (see below). The playback path does not invoke or require
`yt-dlp`.
The plugin
talks to a private Unix socket at `$XDG_RUNTIME_DIR/omarchy-ytmusic/backend.sock`
using versioned newline-delimited JSON.
Artwork URLs are rewritten to a loopback HTTP proxy; the backend fetches and
keeps a bounded in-memory cache of remote thumbnails so Quickshell does not
open HTTPS image requests directly. The proxy prefers a stable loopback port
(`OMARCHY_YTMUSIC_ARTWORK_PORT`, default 47654, falling back to an ephemeral
one) so cached image URLs survive a backend restart instead of reloading with
"Connection refused".

A track that finishes or whose stream dies always moves the queue on. A dead
stream retries the current song and then skips, matching the recovery used for
queue transitions, instead of stopping playback until the backend restarts.

The backend is a Python process activated on demand by a systemd user socket
(`omarchy-ytmusic.socket`). The Node resolver is a child of that backend, so a
new process is not created for every track. Connecting to the socket starts the
backend. It is resident by default (the **Keep player available** setting) so
MPRIS clients and phones can always find it, like MPD; enabling idle shutdown
lets it exit after the configured idle period, after which the socket starts it
again on the next connection. While a surface is open, the plugin watches that
state: if the runtime is missing it re-runs `scripts/setup.sh`, and if a
connected backend never reports `ready` it runs `scripts/playback-runtime.sh
restart` to replace the wedged process. Setup and restart attempts are bounded
so a genuinely broken install reports an error instead of looping.

## MPRIS and session state

`backend/mpris.py` implements `org.mpris.MediaPlayer2.omarchy_ytmusic` over the
session bus (jeepney, one daemon thread in the backend). Owning the interface
in the backend instead of mpv means:

- `Identity` is "YouTube Music" and the player exists even when no stream is
  loaded, so remote clients do not lose it between tracks or after a pause.
- Metadata carries `xesam:title`, `xesam:artist`, `xesam:album`,
  `mpris:length`, and `mpris:artUrl` (the loopback artwork proxy); mpv streams
  carry no tags, so `mpv-mpris` could only ever expose a title.
- `PlaybackStatus`, `Position`, `Volume`, `Shuffle`, and `LoopStatus` follow
  the backend state, and `Play`/`Pause`/`PlayPause`/`Next`/`Previous`/`Seek`/
  `SetPosition` drive the same queue as the panel.
- `Seek`/`SetPosition` re-resolve the stream with `start_ms`, because the local
  SABR response is not seekable. Seeking on a paused, restored session only
  moves the saved position; the stream starts there on the next play.

`backend/session.py` persists queue, index, position, volume, shuffle, and
repeat to `$XDG_STATE_HOME/omarchy-ytmusic/session.json` (atomic writes, mode
`600`). The player writes it on discrete changes, every five seconds while
playing, and on shutdown. On start the backend restores it without resolving a
stream or starting mpv, so the bar and MPRIS show the last track as paused
instead of "Nothing playing".

Omarchy hot-reloads plugins on any write inside their directory, so the venv
and installed backend live outside the plugin tree:

- `$HOME/.local/share/omarchy-ytmusic/venv`
- `$HOME/.local/lib/omarchy-ytmusic/`
- `$HOME/.local/share/omarchy-ytmusic/resolver/`
- `$HOME/.config/omarchy-ytmusic/browser.json`

Resolver diagnostics are written to
`$XDG_RUNTIME_DIR/omarchy-ytmusic/resolver.log`; cookies, PO tokens, and stream
URLs are redacted. The local SABR response is intentionally non-seekable:
Python relaunches a new resolver stream with `start_ms` when the user seeks.
The player determines per stream whether `mpv` reports relative or absolute
timestamps, so seek offsets are not hardcoded to a particular track.

## Protocol

Requests:

```json
{"v":1,"id":7,"command":"pause"}
```

Successful responses keep that id. Failures set `ok` to false with a stable
error code. The server pushes `state_changed` on connection and whenever
playback state changes.

Commands include `hello`, `setup_auth`, `import_browser`, `logout`, `play`, `pause`, `toggle`,
`next`, `previous`, `seek`, `set_volume`, `set_shuffle`, `set_repeat`, `load`,
`add_to_queue`, `search`, `browse`, `get_playlist`, `get_album`, `get_artist`,
`like`, `create_playlist`, `add_to_playlist`, and `sleep`.

## Authentication

The usual sign-in path copies the YouTube Music session already in Chromium
(or Chrome/Brave) on this computer: decrypt the browser cookie database with
the libsecret OSCrypt key, then write `ytmusicapi` headers with
`ytmusicapi.setup()`. Pasting request headers is still supported as a
fallback. The cookie header is passed in memory to the local resolver when it
starts; no Netscape cookie file is generated.

## Local development

```bash
./scripts/install-local.sh
./scripts/test.sh
```

Complete removal:

```bash
./scripts/remove-runtime.sh --purge
omarchy plugin remove felixzsh.ytmusic --yes
```
