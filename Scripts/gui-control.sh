#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
project_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
runtime_root="${TMPDIR:-/tmp}/dagoberd-gui-${UID:-local}"
pid_file="$runtime_root/server.pid"
port_file="$runtime_root/server.port"
url_file="$runtime_root/server.url"
log_file="$runtime_root/server.log"
lock_dir="$runtime_root/control.lock"

mkdir -p "$runtime_root"
chmod 700 "$runtime_root"

read_pid() {
  local candidate
  [ -r "$pid_file" ] || return 1
  IFS= read -r candidate < "$pid_file"
  case "$candidate" in
    ''|*[!0-9]*) return 1 ;;
  esac
  printf '%s' "$candidate"
}

process_is_running() {
  local pid="$1"
  local state
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(ps -p "$pid" -o state= 2>/dev/null || true)"
  case "$state" in
    *Z*) return 1 ;;
  esac
  return 0
}

process_is_managed() {
  local pid="$1"
  local command
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$command" in
    *"dagoberd gui"*|*"dagoberd.cli gui"*) return 0 ;;
  esac
  return 1
}

cleanup_state() {
  local keep_log="${1:-false}"
  rm -f "$pid_file" "$port_file" "$url_file"
  if [ "$keep_log" != "true" ]; then
    rm -f "$log_file"
  fi
}

port_is_available() {
  local port="$1"
  (
    cd "$project_root"
    poetry run python -c \
      'import socket, sys; probe = socket.socket(); probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); probe.bind(("127.0.0.1", int(sys.argv[1]))); probe.close()' \
      "$port"
  ) >/dev/null 2>&1
}

adopt_legacy_process() {
  local port="$1"
  local pid
  local process_root
  command -v lsof >/dev/null 2>&1 || return 1
  pid="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  process_is_running "$pid" || return 1
  process_is_managed "$pid" || return 1
  process_root="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  [ "$process_root" = "$project_root" ] || return 1
  printf '%s\n' "$pid" > "$pid_file"
  printf '%s\n' "$port" > "$port_file"
}

acquire_lock() {
  local owner
  if mkdir "$lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" > "$lock_dir/pid"
    trap release_lock EXIT
    return
  fi
  owner=""
  if [ -r "$lock_dir/pid" ]; then
    IFS= read -r owner < "$lock_dir/pid"
  fi
  if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
    rm -f "$lock_dir/pid"
    rmdir "$lock_dir" 2>/dev/null || true
    mkdir "$lock_dir"
    printf '%s\n' "$$" > "$lock_dir/pid"
    trap release_lock EXIT
    return
  fi
  echo "Another process is already using GUI control." >&2
  exit 1
}

release_lock() {
  rm -f "$lock_dir/pid"
  rmdir "$lock_dir" 2>/dev/null || true
}

stop_managed() {
  local quiet="${1:-false}"
  local pid
  local port=""
  local attempt=0
  local port_attempt=0

  if ! pid="$(read_pid)"; then
    cleanup_state false
    if [ "$quiet" != "true" ]; then
      echo "Dagoberd GUI is not running."
    fi
    return 0
  fi
  if ! process_is_running "$pid"; then
    cleanup_state true
    if [ "$quiet" != "true" ]; then
      echo "Removed stale GUI status; no server process was active."
    fi
    return 0
  fi
  if ! process_is_managed "$pid"; then
    echo "PID $pid is not a managed Dagoberd GUI; leaving it unchanged." >&2
    return 1
  fi
  if [ -r "$port_file" ]; then
    IFS= read -r port < "$port_file"
  fi

  kill -TERM "$pid"
  while process_is_running "$pid" && [ "$attempt" -lt 100 ]; do
    sleep 0.1
    attempt=$((attempt + 1))
  done
  if process_is_running "$pid"; then
    kill -KILL "$pid"
    attempt=0
    while process_is_running "$pid" && [ "$attempt" -lt 30 ]; do
      sleep 0.1
      attempt=$((attempt + 1))
    done
  fi
  if process_is_running "$pid"; then
    echo "Could not stop GUI process $pid; retaining status files." >&2
    return 1
  fi

  cleanup_state false
  if [ -n "$port" ]; then
    while ! port_is_available "$port" && [ "$port_attempt" -lt 30 ]; do
      sleep 0.1
      port_attempt=$((port_attempt + 1))
    done
    if ! port_is_available "$port"; then
      echo "GUI stopped, but port $port is still in use." >&2
      return 1
    fi
  fi
  if [ "$quiet" != "true" ]; then
    echo "Dagoberd GUI stopped; port ${port:-unknown} is free."
  fi
}

start_gui() {
  local port=8765
  local no_browser=false
  local argument
  local next_is_port=false
  local pid
  local url=""
  local attempt=0
  local -a launch_args=()

  for argument in "$@"; do
    if [ "$next_is_port" = "true" ]; then
      port="$argument"
      next_is_port=false
      launch_args+=("$argument")
      continue
    fi
    case "$argument" in
      --port)
        next_is_port=true
        launch_args+=("$argument")
        ;;
      --port=*)
        port="${argument#--port=}"
        launch_args+=("$argument")
        ;;
      --no-browser)
        no_browser=true
        ;;
      *) launch_args+=("$argument") ;;
    esac
  done
  if [ "$next_is_port" = "true" ]; then
    echo "--port requires a port number." >&2
    return 2
  fi
  case "$port" in
    ''|*[!0-9]*) echo "Invalid GUI port: $port" >&2; return 2 ;;
  esac
  if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo "GUI port must be between 1 and 65535." >&2
    return 2
  fi

  if pid="$(read_pid)" && process_is_running "$pid"; then
    if ! process_is_managed "$pid"; then
      echo "Stored PID $pid does not belong to Dagoberd; refusing to start." >&2
      return 1
    fi
    echo "Restarting the existing Dagoberd GUI gracefully."
    stop_managed true
  else
    cleanup_state false
  fi
  if ! port_is_available "$port"; then
    if adopt_legacy_process "$port"; then
      echo "Adopting and gracefully stopping an older Dagoberd GUI from this checkout."
      stop_managed true
    else
      echo "Port $port belongs to an unmanaged process; leaving it unchanged." >&2
      return 1
    fi
  fi

  : > "$log_file"
  pid="$(
    cd "$project_root"
    poetry run python -m dagoberd.gui.daemon "$log_file" gui \
      ${launch_args[@]+"${launch_args[@]}"} --no-browser
  )"
  case "$pid" in
    ''|*[!0-9]*)
      echo "The background GUI process returned an invalid PID: $pid" >&2
      cleanup_state true
      return 1
      ;;
  esac
  printf '%s\n' "$pid" > "$pid_file"
  printf '%s\n' "$port" > "$port_file"

  while [ "$attempt" -lt 150 ]; do
    if ! process_is_running "$pid"; then
      echo "Could not start Dagoberd GUI:" >&2
      sed -n '1,20p' "$log_file" >&2
      cleanup_state true
      return 1
    fi
    url="$(sed -n 's#^\(http://[^[:space:]]*\)$#\1#p' "$log_file" | head -n 1)"
    if [ -n "$url" ]; then
      break
    fi
    sleep 0.1
    attempt=$((attempt + 1))
  done
  if [ -z "$url" ]; then
    echo "Dagoberd GUI did not return its address within 15 seconds." >&2
    stop_managed true
    return 1
  fi
  printf '%s\n' "$url" > "$url_file"

  echo "$url"
  echo "The GUI is running in the background (PID $pid, port $port)."
  echo "Stop: ./Scripts/stop-gui.sh"
  if [ "$no_browser" != "true" ]; then
    if command -v open >/dev/null 2>&1; then
      open "$url" >/dev/null 2>&1 &
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$url" >/dev/null 2>&1 &
    else
      echo "No browser launcher found; open the printed URL manually."
    fi
  fi
}

stop_gui() {
  local port=8765
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --port)
        shift
        [ "$#" -gt 0 ] || { echo "--port requires a port number." >&2; return 2; }
        port="$1"
        ;;
      --port=*) port="${1#--port=}" ;;
      *) echo "Unknown stop option: $1" >&2; return 2 ;;
    esac
    shift
  done
  case "$port" in
    ''|*[!0-9]*) echo "Invalid GUI port: $port" >&2; return 2 ;;
  esac

  if read_pid >/dev/null; then
    stop_managed false
    return
  fi
  if port_is_available "$port"; then
    cleanup_state false
    echo "Dagoberd GUI is not running."
    return
  fi
  if ! adopt_legacy_process "$port"; then
    echo "Port $port cannot be identified as this Dagoberd checkout; leaving the process unchanged." >&2
    return 1
  fi
  echo "Detected an older Dagoberd GUI from this checkout."
  stop_managed false
}

show_status() {
  local pid
  local port="unbekannt"
  if ! pid="$(read_pid)" || ! process_is_running "$pid"; then
    cleanup_state true
    echo "Dagoberd GUI is not running."
    return 1
  fi
  if ! process_is_managed "$pid"; then
    echo "Stored PID $pid does not belong to a managed Dagoberd GUI." >&2
    return 1
  fi
  if [ -r "$port_file" ]; then
    IFS= read -r port < "$port_file"
  fi
  echo "Dagoberd GUI is running (PID $pid, port $port)."
}

action="${1:-}"
if [ -n "$action" ]; then
  shift
fi
acquire_lock
case "$action" in
  start) start_gui "$@" ;;
  stop) stop_gui "$@" ;;
  restart)
    stop_managed true
    start_gui "$@"
    ;;
  status) show_status ;;
  *)
    echo "Usage: $0 {start|stop|restart|status} [GUI options]" >&2
    exit 2
    ;;
esac
