#!/usr/bin/env bash
set -euo pipefail
python /benchmark/inject.py "$1" "${2:-train}"
