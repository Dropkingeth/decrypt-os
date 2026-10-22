#!/bin/bash
# Start or stop the tvbridge-admin SANDBOX server (build/testing only; not a service).
#   tools/run_sandbox.sh start|stop|status
set -u
cd "$(dirname "$0")/.." || exit 1
export TMPDIR=$HOME/.cache PYTHONDONTWRITEBYTECODE=1
PIDF=sandbox/server.pid
running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }
case "${1:-status}" in
  start) running && { echo "already running (pid $(cat $PIDF))"; exit 0; }
         setsid python3 app/server.py --profile sandbox/profile.json > sandbox/server.log 2>&1 < /dev/null &
         echo $! > "$PIDF"; sleep 1; cat sandbox/server.log ;;
  stop)  running && kill "$(cat $PIDF)"; rm -f "$PIDF"; echo stopped ;;
  *)     running && echo "running (pid $(cat $PIDF))" || echo "not running" ;;
esac
