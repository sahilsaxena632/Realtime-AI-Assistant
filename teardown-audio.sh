#!/usr/bin/env bash
#
# Removes the optional virtual "InterviewAudio" sink + loopback created by
# setup-audio.sh and blanks the pinned sources in .env (back to auto-detect).
#
set -euo pipefail

while read -r id _ args; do
  case "${args}" in
    *interview*) pactl unload-module "${id}" 2>/dev/null || true ;;
  esac
done < <(pactl list short modules)

ENV_FILE="$(dirname "$0")/.env"
if [ -f "${ENV_FILE}" ]; then
  python3 - "$ENV_FILE" <<'PY'
import sys
path = sys.argv[1]
lines = open(path).read().splitlines()
for i, line in enumerate(lines):
    if line.strip().startswith("MIC_SOURCE="):
        lines[i] = "MIC_SOURCE="
    elif line.strip().startswith("SYS_SOURCE="):
        lines[i] = "SYS_SOURCE="
open(path, "w").write("\n".join(lines) + "\n")
print(f"Reset MIC_SOURCE / SYS_SOURCE in {path} (auto-detect).")
PY
fi

echo "Virtual sink removed."
