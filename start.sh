#!/bin/bash
# Launch the daemon. Prints the phone URL + QR, then backgrounds itself.
#
# Capture strategy (deterministic speaker labeling):
#   * MIC_SOURCE  -> YOU         (your microphone)
#   * SYS_SOURCE  -> INTERVIEWER (monitor of your output sink = call audio)
# Captured separately via pw-record (PipeWire). Capturing the output *monitor*
# is non-destructive: you still hear the meeting normally, there is no loopback
# to your speakers, and no echo.
set -a
[ -f .env ] && source .env
set +a

# Activate venv if present.
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Auto-detect sources if not pinned in .env.
if [ -z "${MIC_SOURCE}" ]; then
  MIC_SOURCE="$(pactl get-default-source 2>/dev/null)"
  export MIC_SOURCE
fi
if [ -z "${SYS_SOURCE}" ]; then
  DEFAULT_SINK="$(pactl get-default-sink 2>/dev/null)"
  [ -n "${DEFAULT_SINK}" ] && export SYS_SOURCE="${DEFAULT_SINK}.monitor"
fi

echo "Mic source (YOU)         : ${MIC_SOURCE:-<auto/sounddevice>}"
echo "System source (INTERVIEWER): ${SYS_SOURCE:-<none>}"

ARGS="--start"
[ -n "${MIC_INDEX}" ] && ARGS="${ARGS} --mic ${MIC_INDEX}"
[ -n "${SYS_INDEX}" ] && ARGS="${ARGS} --sys ${SYS_INDEX}"
ARGS="${ARGS} --ai ${DEFAULT_AI:-groq}"

# shellcheck disable=SC2086
python3 main.py ${ARGS}

echo "Daemon started. You can close this terminal."
