#!/bin/bash
# Stop the background daemon.
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python3 main.py --stop
