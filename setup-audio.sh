#!/usr/bin/env bash
#
# OPTIONAL. You usually do NOT need this - by default the assistant captures
# your default-output monitor (the interviewer) and your mic (you) separately,
# which works as long as you use headphones (so your mic doesn't pick up the
# interviewer through speakers).
#
# Use this only if you want a GUARANTEED clean interviewer channel - e.g. you
# use speakers and don't want your own voice transcribed at all. It creates a
# virtual "InterviewAudio" sink; route your meeting app's OUTPUT to it and the
# assistant captures only that (never your mic). A loopback plays it back to
# your real output so you still hear it.
#
set -euo pipefail

REAL_SINK="$(pactl get-default-sink)"
MIC="$(pactl get-default-source)"

echo "Real output (you hear here): ${REAL_SINK}"
echo "Microphone (YOU)           : ${MIC}"

# Remove any virtual sink / loopback we created previously (idempotent).
while read -r id _ args; do
  case "${args}" in
    *interview*) pactl unload-module "${id}" 2>/dev/null || true ;;
  esac
done < <(pactl list short modules)

pactl load-module module-null-sink \
  sink_name=interview \
  sink_properties=device.description=InterviewAudio >/dev/null

pactl load-module module-loopback \
  source=interview.monitor \
  sink="${REAL_SINK}" \
  latency_msec=60 >/dev/null

ENV_FILE="$(dirname "$0")/.env"
python3 - "$ENV_FILE" "$MIC" <<'PY'
import os, sys
path, mic = sys.argv[1], sys.argv[2]
vals = {"MIC_SOURCE": mic, "SYS_SOURCE": "interview.monitor"}
lines = open(path).read().splitlines() if os.path.exists(path) else []
seen = set()
for i, line in enumerate(lines):
    for k, v in vals.items():
        if line.strip().startswith(k + "="):
            lines[i] = f"{k}={v}"
            seen.add(k)
for k, v in vals.items():
    if k not in seen:
        lines.append(f"{k}={v}")
open(path, "w").write("\n".join(lines) + "\n")
print(f"Updated {path}: MIC_SOURCE={mic}  SYS_SOURCE=interview.monitor")
PY

cat <<EOF

Done. Virtual sink "InterviewAudio" is ready.

NEXT:
  1. Start your interview/meeting.
  2. Open 'pavucontrol' -> Playback tab and set that app's output to
     "InterviewAudio" (you still hear it via the loopback).
  3. (Re)start the assistant:  ./start.sh

Undo:  ./teardown-audio.sh
EOF
