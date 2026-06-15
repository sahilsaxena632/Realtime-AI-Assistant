# Real-time AI Assistant

An invisible background daemon for Pop!_OS that captures both sides of an
interview call, streams the audio to **Gemini Live** for end-to-end
transcription + AI answers, and displays everything on your **phone browser**
over WiFi. If Gemini Live rate-limits or fails, it automatically falls back to
**Deepgram Nova-2** transcription + **Groq / Claude / ChatGPT** answers.

Nothing visible on the PC — no window, no taskbar icon. The terminal prints a QR
code, then the process double-forks into the background and you can close it.

---

## Architecture

```
Primary path (Gemini Live):
  Mic + System Audio (PulseAudio mixed)
        -> Gemini Live API (audio in -> transcript + answer)
        -> Smart output parser ([INTERVIEWER]/[CANDIDATE]/[ANSWER])
        -> Flask + WebSocket -> Phone browser

Fallback path (Deepgram + AI):
  Mic + System Audio (PulseAudio mixed)
        -> Deepgram Nova-2 (streaming + diarization)
        -> Smart Chunker ([INTERVIEWER]/[YOU], complete-question trigger)
        -> Groq / Claude / ChatGPT (streamed answer)
        -> Flask + WebSocket -> Phone browser
```

**Fallback triggers automatically** on: Gemini 429 rate limit, WebSocket drop
after 3 reconnect attempts, missing/invalid Gemini key, or response latency
over 8 seconds.

**Switches back to Gemini** automatically: a background probe checks Gemini
every 60s while in fallback and restores it when healthy. You can also switch
manually from the phone UI.

---

## File structure

```
.
├── main.py            # entry point, CLI, manual double-fork daemonizer
├── daemon.py          # core orchestrator, manages all modules + switching
├── audio.py           # dual PulseAudio capture, 20ms mixed PCM16 stream
├── chunker.py         # smart text chunker + speaker labeling
├── calibration.py     # speaker voice calibration (pyannote)
├── server.py          # Flask + flask-sock phone server + embedded UI
├── config.py          # all constants (reads secrets from .env)
├── logutil.py         # thread-safe file logger
├── providers/
│   ├── __init__.py
│   ├── base.py        # abstract provider interface + safe event helpers
│   ├── gemini_live.py # Gemini Live end-to-end audio provider (primary)
│   └── deepgram_groq.py # Deepgram + AI fallback provider
├── .env.example
├── requirements.txt
├── setup.sh
├── start.sh           # auto-detects sources, launches daemon
├── stop.sh            # stops the daemon
└── README.md
```

---

## Quick start

```bash
# 1. Install (system + python deps, creates .venv and .env)
chmod +x setup.sh && ./setup.sh
source .venv/bin/activate

# 2. Fill API keys (Gemini free, Deepgram free, Groq free)
nano .env

# 3. Find your audio devices, add MIC_INDEX and SYS_INDEX to .env
python3 main.py --list-devices

# 4. Calibrate your voice (optional but recommended)
python3 main.py --calibrate

# 5. Launch - terminal prints a QR code then backgrounds itself
./start.sh

# 6. Scan the QR on your phone (same WiFi) - done
```

### Capturing audio + telling the two speakers apart

Mic and system audio are captured as **two separate streams** (via `pw-record`,
the native PipeWire recorder; falls back to `parec`), so speakers are labeled
**deterministically** — no voice-guessing:

- `MIC_SOURCE` -> **YOU** (your microphone)
- `SYS_SOURCE` -> **INTERVIEWER** (the `.monitor` of your output sink = the call
  audio coming out of your speakers)

Capturing the output *monitor* is **non-destructive**: you still hear the
meeting normally, nothing is routed back to your speakers, and there's no echo.

Run `python3 main.py --list-devices` — it prints your PulseAudio/PipeWire
sources and an auto-detected recommendation. Leave `MIC_SOURCE`/`SYS_SOURCE`
blank in `.env` to auto-detect your defaults at launch, or pin them explicitly.

Speaker labeling by path:

- **Deepgram + Groq (default, `PRIMARY_PROVIDER=deepgram`)**: stereo
  *multichannel* — channel 0 = YOU, channel 1 = INTERVIEWER. 100% deterministic.
  Fast, text-based answers from Groq/Claude/ChatGPT. This is the recommended
  path.
- **Gemini Live (`PRIMARY_PROVIDER=gemini`)**: current Live models output
  **audio only**, so we request audio + input/output transcription and feed
  Gemini the interviewer's (system) audio. It transcribes the question and
  speaks an answer that we capture as text. Works, but slower than the text
  path and it doesn't transcribe your own mic. Endpoint is **v1beta**; model
  `gemini-3.1-flash-live-preview` (older `*-exp` / `*-live-001` are shut down).

You can switch providers live from the phone UI's provider toggle.

(Older `sounddevice` `MIC_INDEX`/`SYS_INDEX` still work as a fallback if neither
`pw-record` nor `parec` is available.)

---

## CLI

```bash
python3 main.py --start                 # start daemon in background
python3 main.py --stop                  # stop daemon
python3 main.py --restart               # restart daemon
python3 main.py --status                # running? pid + phone URL
python3 main.py --list-devices          # list audio devices
python3 main.py --calibrate             # voice calibration (foreground)
python3 main.py --login                 # first-time setup / regen key
python3 main.py --mic <i> --sys <i> --ai <groq|claude|chatgpt>
```

---

## Phone UI

Single dark fullscreen page (vanilla JS):

- **Status bar** — provider dot (purple = Gemini Live, orange = Deepgram, red =
  disconnected), pause, clear, fallback-AI selector (enabled only on fallback),
  manual provider toggle.
- **Interviewer question**, large **streaming AI answer** (Courier, 22px), and a
  dim footer with your speech + live interim transcript.
- Auto-reconnecting WebSocket (2s), `wakeLock` to keep the screen on, green
  border flash when an answer completes.
- State is kept server-side (last answer + last 10 chunks) and replayed on
  reconnect, so refreshing never shows a blank screen.

---

## Server API

REST (all require `?key=` secret): `GET /`, `POST /pause`, `POST /resume`,
`POST /clear`, `POST /switch-ai`, `POST /switch-provider`, `POST /stop`,
`GET /status`, `GET /devices`.

WebSocket `/ws` push events: `interim`, `chunk`, `ai_start`, `ai_chunk`,
`ai_done`, `provider`, `status`, `error`, `ping`.

---

## Notes & cost

All three free tiers together (Gemini Live + Deepgram + Groq) make this a fully
functional zero-cost system. Gemini Live handles the bulk of the work; Deepgram
+ Groq kick in automatically if anything goes wrong.

- Audio is sent as raw PCM16, 16 kHz mono, in 20 ms chunks (640 bytes).
- When paused, silence frames keep provider connections alive.
- The phone server key is auto-generated on first run and saved to `.env`.
- Logs: `~/.interview-assistant/daemon.log`. PID: `~/.interview-assistant/daemon.pid`.

This tool is intended for legitimate, permitted use (e.g. practice, accessibility,
note-taking). Make sure you have consent to record and that your use complies
with the rules of any interview or call you participate in.

## Troubleshooting

- **Nothing shows on the phone / "deepgram-sdk not installed"**: you must use
  Deepgram SDK **v3** (`deepgram-sdk>=3.7,<4`). The v7 rewrite removed
  `LiveOptions`/`LiveTranscriptionEvents`, so the import silently fails.
  Reinstall: `pip install "deepgram-sdk>=3.7,<4"`.
- **Gemini logs "model ... not found for API version" / reconnect loop**: the
  model or endpoint is outdated. Use the v1beta endpoint and a current Live
  model (e.g. `gemini-3.1-flash-live-preview`). List your account's models with
  `curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"`.
- **Transcription is blank but audio plays**: the captured `SYS_SOURCE` is not
  the device your audio actually plays through. Leave `SYS_SOURCE` blank to
  auto-detect the current default-output monitor, and check the level with
  `pw-record --target "$(pactl get-default-sink).monitor" --rate 16000 --channels 1 --format s16 --raw - | xxd | head`.
- **Logs**: `~/.interview-assistant/daemon.log`.
```
