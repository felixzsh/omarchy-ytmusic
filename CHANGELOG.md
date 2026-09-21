# Changelog

## Unreleased

- Start a Mix from a playlist, album, or artist, from its detail page or the
  right-click menu, alongside the existing per-song radio.
- Keep playing when a stream dies mid-track: a failed stream now retries the
  song and then skips, instead of stopping until the backend is restarted.
- Advance on end-of-file even when the play flag is momentarily stale, and
  ignore the duplicate end events mpv emits for the file just replaced, so a
  finished track cannot stall the queue or skip the next song.
- Serve artwork from a stable loopback port so images are not reloaded with
  "Connection refused" every time an idle backend restarts.
- Activate the playback backend through a systemd socket so opening the player
  starts it and idle shutdown leaves the socket ready for the next connection.
- Drop the QML start/stop orchestration; the client connection owns the backend
  lifecycle and no longer keeps it awake in the background.
- Self-heal while a surface is open: retry the playback install when the runtime
  is missing and restart the backend when it never becomes ready, instead of
  staying on "not ready" until a manual service restart.
- Surface playback install failures in the player instead of swallowing them.
- Start the socket immediately during setup and document enabling the socket
  rather than the socket-activated service.

## 1.2.0

- Normalize relative and absolute `mpv` timestamps per stream for reliable seeks.
- Start SABR seeks with a segment preroll so irregular segment boundaries do not
  skip the requested position.
- Recover the Quickshell backend socket after a service restart without leaving
  Home in a not-ready state.
- Prewarm the persistent resolver while the plugin backend is active.
- Keep the playback clock still until the first stream position is stable.
- Serve remote artwork through a bounded local proxy to avoid Qt/OpenSSL image
  loader crashes in Quickshell.
- Document that local playback no longer requires `yt-dlp`.

- Replace per-track stream processes with a persistent youtubei.js stream resolver.
- Keep ytmusicapi as the catalog, library, playlist, and authentication client.
- Cache the YouTube player and resolved stream URLs until their expiry.
- Remove the generated Netscape cookie file and the old external resolver requirement.

## 1.1.1

- Keep the playback socket alive while the player is open, and reconnect when it drops.
- Open the backend socket before catalog setup so the player can connect immediately.
- Do not idle-stop playback while a player window is connected.
- Restart a stopped backend instead of leaving Home empty.

## 1.1.0

- Recreate the backend socket after a dropped connection so the player can recover.
- Sign in by copying the YouTube Music session already in Chromium on this computer.
- Keep pasted request headers as a fallback.
- Keep the local mpv process off the Wayland session so tracks actually start.
- Use a D-Bus-safe mpv client name so MPRIS cannot freeze playback.
- Publish the song title to MPRIS instead of the googlevideo stream URL.
- Refresh that MPRIS title when the next track starts, not after the stream URL loads.
- Keep the bar slot as the YouTube Music logo only.

## 1.0.0

- First release: Omarchy bar widget, mini-player, and full player for YouTube Music.
- Started from [Omarchy Spotify](https://github.com/stappmus/Omarchy-Spotify).
- Local playback through a plugin-owned mpv backend and yt-dlp, not Chromium.
- Library, search, playlists, queue, likes, radio, sleep timer, and Omasing lyrics.
