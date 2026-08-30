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
client name `omarchy-ytmusic` so MPRIS cannot stall the player. Each track sets
`force-media-title` so MPRIS clients show the song name, not the stream URL.
The playback path does not invoke or require `yt-dlp`.
The plugin
talks to a private Unix socket at `$XDG_RUNTIME_DIR/omarchy-ytmusic/backend.sock`
using versioned newline-delimited JSON.
Artwork URLs are rewritten to a loopback HTTP proxy; the backend fetches and
keeps a bounded in-memory cache of remote thumbnails so Quickshell does not
open HTTPS image requests directly.

The backend is a Python process supervised by a static systemd user unit that
is never enabled at login. The Node resolver is a child of that backend, so a
new process is not created for every track. The plugin starts the unit when a
UI is visible or you press play, and the backend exits after the configured
idle period.

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
