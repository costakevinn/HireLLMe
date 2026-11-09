#!/usr/bin/env bash
# Runs one step in the container. Example: ./run.sh collect --dry-run  (= scripts/collect.py)
set -e
step="$1"; shift
[ -t 0 ] && tty="" || tty="-T"     # no terminal (e.g. GitHub Actions): no TTY
docker compose run --rm $tty app python -u "scripts/${step}.py" "$@"