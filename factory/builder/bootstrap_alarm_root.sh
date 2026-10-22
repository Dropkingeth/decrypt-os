#!/usr/bin/env bash
# Verify/extract only into a new disposable builder directory. No downloads.
set -euo pipefail
umask 022
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec nice -n 19 ionice -c3 python3 -B "$here/bootstrap_alarm_root.py" "$@"
