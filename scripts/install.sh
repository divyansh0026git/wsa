#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=${PYTHON:-python3}
if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo 'Python 3.10 or newer is required.' >&2
  exit 1
fi
"$python" -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install -e "$root"
printf '\nWSA installed. Add this directory to your PATH, then open a new terminal:\n%s\n' "$root/.venv/bin"
printf 'Or run it directly: %s/.venv/bin/wsa\n' "$root"
