#!/usr/bin/env bash
# qa_triage: find the known problems among a teuthology run's failures; see
# README.md. Run it from anywhere, or from a link to it on your PATH:
#
#   qa_triage.sh https://pulpito.ceph.com/<run>/
#
# The first time, it makes its own Python environment, with the packages that
# pyproject.toml lists, in the cache: $QA_TRIAGE_CACHE/venv, by default
# ~/.cache/qa_triage/venv. After that it only checks that the list has not
# changed. QA_TRIAGE_PYTHON names the Python 3.9 or later to make it with
# (default: python3).

set -euo pipefail

# This directory, where the code is, even when run through a link.
source=${BASH_SOURCE[0]}
while [ -L "$source" ]; do
    dir=$(cd "$(dirname "$source")" && pwd)
    source=$(readlink "$source")
    [[ $source == /* ]] || source=$dir/$source
done
here=$(cd "$(dirname "$source")" && pwd)

venv=${QA_TRIAGE_CACHE:-$HOME/.cache/qa_triage}/venv
python=${QA_TRIAGE_PYTHON:-python3}
wanted=$(sed -n 's/^dependencies = \[\(.*\)\]$/\1/p' "$here/pyproject.toml" | tr -d '" ' |
         tr ',' ' ')
read -ra packages <<< "$wanted"

if [ "$(cat "$venv/.packages" 2> /dev/null)" != "$wanted" ]; then
    if [ ! -x "$venv/bin/python" ]; then
        if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
            echo "qa_triage: needs Python 3.9 or later, and $python is not;" \
                 "QA_TRIAGE_PYTHON names another" >&2
            exit 2
        fi
        echo "qa_triage: making its Python environment in $venv" >&2
        "$python" -m venv "$venv"
    fi
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check "${packages[@]}"
    echo "$wanted" > "$venv/.packages"
fi

# The code in this directory, not a copy: so that each checkout runs its own.
exec "$venv/bin/python" "$here" "$@"
