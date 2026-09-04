#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
python -m pip install -q -r requirements.txt
python -m pytest tests "$@"
