#!/usr/bin/env bash
set -euo pipefail

action=${1:-}
source_root=${2:-}
if [[ -z $action || ( $# -gt 2 ) ]]; then
  echo "Usage: scripts/playback-runtime.sh check|start|stop|status|unit [plugin-dir]" >&2
  exit 2
fi

venv_python="$HOME/.local/share/omarchy-ytmusic/venv/bin/python"
lib_dir="$HOME/.local/lib/omarchy-ytmusic"
backend_script="$lib_dir/server.py"
resolver_dir="$HOME/.local/share/omarchy-ytmusic/resolver"
unit=omarchy-ytmusic.service

sync_backend() {
  [[ -n $source_root && -f $source_root/backend/server.py && -d $lib_dir ]] || return 1
  local updated=1
  local name
  for name in server.py protocol.py auth.py catalog.py player.py; do
    if [[ ! -f $lib_dir/$name ]] \
        || ! cmp -s -- "$source_root/backend/$name" "$lib_dir/$name"; then
      install -m 644 -- "$source_root/backend/$name" "$lib_dir/$name"
      updated=0
    fi
  done
  install -d -m 700 -- "$resolver_dir"
  if [[ ! -f "$resolver_dir/resolver.mjs" ]] \
      || ! cmp -s -- "$source_root/backend/resolver/resolver.mjs" "$resolver_dir/resolver.mjs"; then
    install -m 644 -- "$source_root/backend/resolver/resolver.mjs" "$resolver_dir/resolver.mjs"
    updated=0
  fi
  chmod 755 -- "$lib_dir/server.py"
  return "$updated"
}

unit_exists() {
  systemctl --user cat "$1" >/dev/null 2>&1
}

runtime_ready() {
  [[ -x $venv_python && -f $backend_script ]] && unit_exists "$unit" \
    && [[ -f "$resolver_dir/resolver.mjs" \
      && -f "$resolver_dir/node_modules/youtubei.js/package.json" ]] \
    && command -v node >/dev/null 2>&1 && command -v mpv >/dev/null 2>&1
}

case $action in
  check)
    runtime_ready
    ;;
  start)
    updated=1
    sync_backend && updated=0 || true
    runtime_ready || {
      echo "playback-runtime.sh: YouTube Music playback is not installed yet" >&2
      exit 1
    }
    if (( updated == 0 )) && systemctl --user is-active --quiet "$unit"; then
      systemctl --user restart "$unit"
    else
      systemctl --user start "$unit"
    fi
    ;;
  stop)
    systemctl --user stop "$unit" 2>/dev/null || true
    ;;
  status)
    runtime_ready || exit 1
    systemctl --user is-active "$unit"
    ;;
  unit)
    runtime_ready || exit 1
    printf '%s\n' "$unit"
    ;;
  *)
    echo "playback-runtime.sh: unknown action: $action" >&2
    exit 2
    ;;
esac
