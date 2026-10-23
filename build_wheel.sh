#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON=.venv/bin/python
"$PYTHON" -m pip install -q build
"$PYTHON" -m build --wheel
ls -1 dist/*.whl
