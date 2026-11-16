#!/bin/sh
# POSIX entrypoint; does not change system settings or download dependencies.
kit_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
interactive=no
if [ "$#" -eq 0 ]; then interactive=yes; fi
python_bin=${CODEX_SWARM_PYTHON:-python3}
if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1; then
  printf '%s\n' 'Python 3.11+ was not found. Install it from https://www.python.org/downloads/macos/'
  printf '%s\n' 'Or set CODEX_SWARM_PYTHON to an existing Python executable. No download was requested.'
  status=2
else
  "$python_bin" -B -X utf8 "$kit_dir/setup.py" "$@"
  status=$?
fi
if [ "$interactive" = yes ]; then
  printf '%s' 'Press Return to close: '
  read -r ignored
fi
exit "$status"
