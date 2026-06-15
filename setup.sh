#!/bin/bash
# Setup script for the Real-time AI Interview Assistant (Pop!_OS / Ubuntu).
set -e

echo "=== Real-time AI Interview Assistant - setup ==="

# 1. System packages needed for audio capture + qr.
if command -v apt >/dev/null 2>&1; then
  echo "[1/4] Installing system packages (PortAudio, PulseAudio utils)..."
  sudo apt update
  sudo apt install -y python3-venv python3-pip portaudio19-dev libportaudio2 \
                      pulseaudio-utils pipewire-bin
fi

# 2. Python virtual environment.
echo "[2/4] Creating virtual environment (.venv)..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

# 3. Python dependencies.
echo "[3/4] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Environment file.
echo "[4/4] Preparing .env..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from template. Edit it and add your API keys:"
  echo "    nano .env"
else
  echo ".env already exists - leaving it untouched."
fi

chmod +x start.sh stop.sh 2>/dev/null || true

echo ""
echo "Setup complete. Next steps:"
echo "  1. source .venv/bin/activate"
echo "  2. nano .env                       # add API keys"
echo "  3. python3 main.py --list-devices  # note MIC_INDEX / SYS_INDEX"
echo "  4. python3 main.py --calibrate     # optional voice calibration"
echo "  5. ./start.sh                      # launch; scan QR; close terminal"
