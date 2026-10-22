#!/bin/sh
# Dedicated Tradovate browser for the bridge. Separate profile, loopback-only DevTools.
# Never reuses ports 9566 or 9567 (the Guardian and market browsers); 9556 belongs to the login guard.
# TVBRIDGE_PASSWORD_STORE (default and the units: gnome-libsecret) keeps Chromium's saved login encrypted by the
# desktop keyring. The login guard only clicks that saved login; nothing ever types or reads it.
#
# Multi-login (1.1.0): tvbridge-browser@<id>.service sets TVBRIDGE_LOGIN_ID and reads
# TVBRIDGE_DEVTOOLS_PORT/TVBRIDGE_PROFILE from logins/<id>/login.env. Two logins never share a profile:
#   - the profile carries a marker file .tvbridge-login with its login id; another login refuses it;
#   - a login other than A refuses an unmarked profile that already holds browser data;
#   - a profile whose Chromium lock is held by a running Chrome/Chromium for the same profile on a
#     different DevTools port is refused.
# With TVBRIDGE_LOGIN_ID set, the window gets a class and title containing the login id.
# Without TVBRIDGE_LOGIN_ID this is login A exactly as in 1.0.9.5.2: same checks, same argv, no marker.
set -eu
PORT="${TVBRIDGE_DEVTOOLS_PORT:-9555}"
PROFILE="${TVBRIDGE_PROFILE:-$HOME/.local/share/decrypt-os/tvbridge-browser/profile}"
URL="${TVBRIDGE_URL:-https://trader.tradovate.com/}"
LOGIN_ID="${TVBRIDGE_LOGIN_ID:-}"

refuse() { echo "refusing: $*" >&2; exit 1; }

# One argument per line for a running pid (Linux /proc; elsewhere ps, split before each " --").
proc_args() {
  if [ -r "/proc/$1/cmdline" ]; then
    tr '\0' '\n' < "/proc/$1/cmdline"
  else
    ps -o args= -p "$1" 2>/dev/null | awk '{ gsub(/ --/, "\n--"); print }'
  fi
}

if [ -z "$LOGIN_ID" ]; then
  # Login A (tvbridge-browser.service): unchanged from 1.0.9.5.2.
  case "$PORT" in 9566|9567) echo "refusing reserved port $PORT" >&2; exit 1;; esac
  mkdir -p "$PROFILE"
  chmod 700 "$(dirname "$PROFILE")"
else
  MARKER="$PROFILE/.tvbridge-login"
  case "$PORT" in 9556|9566|9567) refuse "reserved port $PORT";; esac
  case "$PORT" in ''|*[!0-9]*) refuse "TVBRIDGE_DEVTOOLS_PORT must be a number";; esac
  case "$LOGIN_ID" in [abcdefghijklmnopqrstuvwxyz]*) ;; *) refuse "TVBRIDGE_LOGIN_ID must start with a lowercase letter";; esac
  case "$LOGIN_ID" in *[!abcdefghijklmnopqrstuvwxyz0123456789-]*) refuse "TVBRIDGE_LOGIN_ID may hold only a-z, 0-9 and -";; esac
  [ "${#LOGIN_ID}" -le 16 ] || refuse "TVBRIDGE_LOGIN_ID longer than 16 characters"

  # Profile ownership.
  if [ -f "$MARKER" ]; then
    MARKED="$(head -n 1 "$MARKER" | tr -d '[:space:]')"
    [ "$MARKED" = "$LOGIN_ID" ] || refuse "profile $PROFILE belongs to login '$MARKED', not '$LOGIN_ID'"
  elif [ "$LOGIN_ID" != a ]; then
    [ "$PROFILE" != "$HOME/.local/share/decrypt-os/tvbridge-browser/profile" ] || refuse "login $LOGIN_ID cannot use login A's default profile"
    if [ -d "$PROFILE" ] && [ -n "$(ls -A "$PROFILE" 2>/dev/null)" ]; then
      refuse "profile $PROFILE has browser data but no .tvbridge-login marker; it may be another login's"
    fi
  fi

  # A live Chrome/Chromium holding this profile must be on this port. Stale locks, our own pid and
  # processes that are not a browser for this profile are ignored.
  LOCK="$PROFILE/SingletonLock"
  if [ -L "$LOCK" ]; then
    TARGET="$(readlink "$LOCK" || true)"
    PID="${TARGET##*-}"
    case "$PID" in ''|*[!0-9]*) PID="";; esac
    if [ -n "$PID" ] && [ "$PID" != "$$" ] && [ "${TARGET%-*}" = "$(hostname 2>/dev/null || uname -n)" ] \
        && kill -0 "$PID" 2>/dev/null; then
      ARGS="$(proc_args "$PID" || true)"
      EXE="$(printf '%s\n' "$ARGS" | head -n 1)"
      HELD_DIR="$(printf '%s\n' "$ARGS" | sed -n 's/^--user-data-dir=//p' | head -n 1)"
      HELD_PORT="$(printf '%s\n' "$ARGS" | sed -n 's/^--remote-debugging-port=//p' | head -n 1)"
      case "${EXE##*/}" in
        *[Cc]hrome*|*[Cc]hromium*)
          if [ "$HELD_DIR" = "$PROFILE" ] && [ -n "$HELD_PORT" ] && [ "$HELD_PORT" != "$PORT" ]; then
            refuse "profile $PROFILE is locked by running pid $PID on a different DevTools port ($HELD_PORT)"
          fi;;
      esac
    fi
  fi

  mkdir -p "$PROFILE"
  chmod 700 "$(dirname "$PROFILE")"
  if [ ! -f "$MARKER" ]; then
    (umask 077 && printf '%s\n' "$LOGIN_ID" > "$MARKER")
  fi
fi

if [ -n "${TVBRIDGE_BROWSER_BIN:-}" ]; then
  BIN="$TVBRIDGE_BROWSER_BIN"  # tests only; the units never set it
elif [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
else
  BIN="$(command -v chromium || command -v google-chrome)"
fi

if [ -n "$LOGIN_ID" ]; then
  # --class sets the X11 WM_CLASS (Chromium on Wayland may also use it for the app_id);
  # --window-name sets the window title.
  set -- "--class=tvbridge-login-$LOGIN_ID" "--window-name=Tradovate Bridge — Login $LOGIN_ID"
else
  set --
fi
# Unset means the desktop keyring (gnome-libsecret): Chromium must never fall back to its "basic" store,
# which encrypts saved logins with a fixed key. Set but empty is a config mistake and is refused.
[ -n "${TVBRIDGE_PASSWORD_STORE+set}" ] || TVBRIDGE_PASSWORD_STORE=gnome-libsecret
case "$TVBRIDGE_PASSWORD_STORE" in
  gnome-libsecret|kwallet5|kwallet6) set -- "$@" "--password-store=$TVBRIDGE_PASSWORD_STORE";;
  *) refuse "TVBRIDGE_PASSWORD_STORE must be gnome-libsecret, kwallet5 or kwallet6 (never basic or empty)";;
esac

exec "$BIN" \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port="$PORT" \
  --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  "$@" \
  "$URL"
