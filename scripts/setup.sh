#!/usr/bin/env bash
set -euo pipefail

source_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh

Install the unprivileged YouTube Music playback backend: a user venv with
ytmusicapi, a local youtubei.js resolver, copies of both outside the plugin
tree, and socket-activated systemd user units. The socket is enabled at login;
the backend starts on the first connection and stops again when idle.
EOF
}

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  usage
  exit 0
fi

for command_name in python3 node npm mpv systemctl install; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "setup.sh: required command is missing: $command_name" >&2
    echo "Install playback dependencies with: omarchy pkg add mpv nodejs" >&2
    exit 1
  }
done

node_binary=$(command -v node)

config_root=${XDG_CONFIG_HOME:-"$HOME/.config"}
data_root=${XDG_DATA_HOME:-"$HOME/.local/share"}
lib_dir="$HOME/.local/lib/omarchy-ytmusic"
venv_dir="$data_root/omarchy-ytmusic/venv"
unit_dir="$config_root/systemd/user"
unit_file="$unit_dir/omarchy-ytmusic.service"
socket_file="$unit_dir/omarchy-ytmusic.socket"
auth_dir="$config_root/omarchy-ytmusic"
resolver_dir="$data_root/omarchy-ytmusic/resolver"

# Never compile or write inside the plugin directory. Omarchy hot-reloads on
# any write there and would restart the shell mid-setup.
install -d -m 700 -- "$lib_dir" "$auth_dir" "$unit_dir" \
  "$(dirname -- "$venv_dir")" "$resolver_dir"

install -m 644 -- \
  "$source_root/backend/server.py" \
  "$source_root/backend/protocol.py" \
  "$source_root/backend/auth.py" \
  "$source_root/backend/catalog.py" \
  "$source_root/backend/player.py" \
  "$lib_dir/"
chmod 755 -- "$lib_dir/server.py"

install -m 644 -- \
  "$source_root/backend/resolver/resolver.mjs" \
  "$source_root/backend/resolver/package.json" \
  "$resolver_dir/"

if [[ -f "$source_root/backend/resolver/package-lock.json" ]]; then
  install -m 644 -- "$source_root/backend/resolver/package-lock.json" "$resolver_dir/"
  npm ci --omit=dev --no-audit --no-fund --prefix "$resolver_dir"
else
  npm install --omit=dev --no-audit --no-fund --prefix "$resolver_dir"
fi

if [[ ! -x $venv_dir/bin/python ]]; then
  python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/pip" install --upgrade pip >/dev/null
"$venv_dir/bin/pip" install -r "$source_root/backend/requirements.txt"

# Point the unit at the installed copy. The plugin directory is only a source.
sed -e "s|ExecStart=.*|ExecStart=$venv_dir/bin/python $lib_dir/server.py|" \
  -e "s|Environment=OMARCHY_YTMUSIC_NODE=.*|Environment=OMARCHY_YTMUSIC_NODE=$node_binary|" \
  "$source_root/systemd/omarchy-ytmusic.service" > "$unit_file"
chmod 644 -- "$unit_file"

install -m 644 -- \
  "$source_root/systemd/omarchy-ytmusic.socket" "$socket_file"

systemctl --user daemon-reload

# Socket activation: enable the socket, never the service. The backend starts
# when the player connects and exits after the configured idle period.
systemctl --user disable --now omarchy-ytmusic.service >/dev/null 2>&1 || true
systemctl --user enable omarchy-ytmusic.socket >/dev/null

# Import an existing ytmusicbar session if this install has none yet.
if [[ ! -s $auth_dir/browser.json && -s $config_root/ytmusicbar/browser.json ]]; then
  install -m 600 -- "$config_root/ytmusicbar/browser.json" "$auth_dir/browser.json"
fi

"$venv_dir/bin/python" "$lib_dir/server.py" --self-test >/dev/null

echo "Installed YouTube Music playback to $lib_dir"
echo "The socket unit is $socket_file and is enabled at login; the backend"
echo "starts on first connection and stops again when idle."
