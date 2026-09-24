# Real-time AI Assistant

An invisible background daemon for Pop!_OS that captures both sides of an
interview call, streams the audio for transcription + AI answers, and displays
everything on your **phone browser** over WiFi.

Nothing visible on the PC — no window, no taskbar icon. The terminal prints a
QR code, then the process double-forks into the background and you can close it.

---

## Architecture

```
Default path (Deepgram + AI):
  Mic + System Audio (two separate parec streams, PCM16 16 kHz)
        -> AGC (automatic gain boost for quiet monitor sources)
        -> Deepgram Nova-2 (streaming, stereo multichannel)
        -> SmartChunker (channel 0 = YOU, channel 1 = INTERVIEWER)
        -> Groq llama-3.3-70b  (primary answer AI)
              |-- on 429 -> NVIDIA NIM llama-3.1-70b (auto-fallback)
              |-- or Claude / ChatGPT (selectable from phone UI)
        -> Flask + WebSocket -> Phone browser

Secondary path (Gemini Live):
  System Audio only (interviewer side)
        -> Gemini Live API v1beta (audio in -> transcript + spoken answer)
        -> Output transcription captured as text
        -> Flask + WebSocket -> Phone browser
```

**Default provider is Deepgram** (`PRIMARY_PROVIDER=deepgram`). It is faster,
labels speakers per-channel with no guessing, and streams text answers from
Groq. Switch to Gemini Live manually from the phone UI or set
`PRIMARY_PROVIDER=gemini` in `.env`.

**Gemini auto-restore**: when `PRIMARY_PROVIDER=gemini`, a background probe
checks Gemini every 60 s while on Deepgram and switches back when healthy.
With `PRIMARY_PROVIDER=deepgram` it stays on Deepgram (you can still switch
manually from the phone UI).

---

## File structure

```
.
├── main.py            # entry point, CLI, manual double-fork daemonizer
├── daemon.py          # core orchestrator, manages all modules + switching
├── audio.py           # dual parec capture, 20 ms PCM16 stream + AGC
├── chunker.py         # smart text chunker + speaker labeling
├── calibration.py     # speaker voice calibration (pyannote)
├── context_store.py   # persistent candidate context (resume / JD / projects)
├── server.py          # Flask + flask-sock phone server + embedded UI
├── config.py          # all constants (reads secrets from .env)
├── logutil.py         # thread-safe file logger
├── providers/
│   ├── __init__.py
│   ├── base.py        # abstract provider interface + safe event helpers
│   ├── gemini_live.py # Gemini Live end-to-end audio provider
│   └── deepgram_groq.py # Deepgram + AI answer provider (default)
├── .env.example
├── requirements.txt
├── setup.sh           # installs system + python deps, creates .venv and .env
├── setup-audio.sh     # optional: mic-free loopback for interviewer audio
├── teardown-audio.sh  # removes the loopback created by setup-audio.sh
├── start.sh           # auto-detects sources, launches daemon
└── stop.sh            # stops the daemon
```

---

## Quick start

```bash
# 1. Install (system + python deps, creates .venv and .env)
chmod +x setup.sh && ./setup.sh
source .venv/bin/activate

# 2. Fill API keys
nano .env

# 3. List your audio devices and get recommended .env values
python3 main.py --list-devices

# 4. Verify both channels are picking up real audio (play something while this runs)
python3 main.py --check-audio

# 5. Calibrate your voice (optional but recommended)
python3 main.py --calibrate

# 6. Launch - terminal prints a QR code then backgrounds itself
./start.sh

# 7. Scan the QR on your phone (same WiFi) - done
```

### Loading your resume and job description

Pass context files at launch so the AI tailors answers to your background:

```bash
python3 main.py --start \
  --resume path/to/resume.txt \
  --jd path/to/job_description.txt \
  --projects path/to/projects.txt
```

Context is persisted to `~/.interview-assistant/context.json` and survives
restarts. You can also paste or edit it live from the phone UI's **gear button
(⚙)** without restarting the daemon.

### Capturing audio + telling the two speakers apart

Mic and system audio are captured as **two separate streams** via `parec`
(PulseAudio), so speakers are labeled **deterministically** — no voice-guessing:

- `MIC_SOURCE` → **YOU** (your microphone)
- `SYS_SOURCE` → **INTERVIEWER** (the `.monitor` of your output sink = the call
  audio coming out of your speakers)

Capturing the output *monitor* is **non-destructive**: you still hear the
meeting normally, nothing is routed back to your speakers, and there's no echo.

Run `python3 main.py --list-devices` — it prints your PulseAudio/PipeWire
sources and an auto-detected recommendation. Leave `MIC_SOURCE`/`SYS_SOURCE`
blank in `.env` to auto-detect your defaults at launch, or pin them explicitly.

**Automatic Gain Control (AGC)** is enabled by default. Monitor/loopback sources
are often attenuated; AGC quietly boosts them toward a usable level without
amplifying silence. Disable with `AUDIO_AGC=0` in `.env` if you don't need it.

Speaker labeling by path:

- **Deepgram (default, `PRIMARY_PROVIDER=deepgram`)**: stereo *multichannel* —
  channel 0 = YOU, channel 1 = INTERVIEWER. 100% deterministic. Fast,
  text-based answers from Groq (with automatic NVIDIA NIM failover on 429).
- **Gemini Live (`PRIMARY_PROVIDER=gemini`)**: interviewer (system) audio only.
  Gemini transcribes the question and speaks an answer captured as text. Works,
  but slower and doesn't transcribe your own mic. Endpoint is **v1beta**; model
  `gemini-3.1-flash-live-preview` (older `*-exp` / `*-live-001` are shut down).

You can switch providers live from the phone UI's provider toggle (⇄).

(Older `sounddevice` `MIC_INDEX`/`SYS_INDEX` still work as a fallback if
`parec` is not available.)

---

## CLI

```bash
python3 main.py --start                 # start daemon in background
python3 main.py --stop                  # stop daemon
python3 main.py --restart               # restart daemon
python3 main.py --status                # running? pid + phone URL
python3 main.py --list-devices          # list audio devices + recommended .env
python3 main.py --check-audio           # capture 6 s and verify both channels
python3 main.py --calibrate             # voice calibration (foreground)
python3 main.py --login                 # first-time setup / regen key

# Options combinable with --start / --restart:
#   --mic <index>           sounddevice mic index (fallback)
#   --sys <index>           sounddevice system index (fallback)
#   --ai <groq|claude|chatgpt|nvidia>   preferred fallback AI
#   --resume <file>         path to resume .txt/.md
#   --jd <file>             path to job description file
#   --projects <file>       path to key projects file
```

---

## Phone UI

Single dark fullscreen page (vanilla JS):

- **Status bar** — provider dot (purple = Gemini Live, orange = Deepgram, red =
  disconnected/paused), pause (⏸), clear (⌫), fallback-AI selector
  (Groq / NVIDIA / Claude / ChatGPT), provider toggle (⇄), context panel (⚙).
- **Interviewer question**, large **streaming AI answer** (Courier, 22 px), and a
  dim footer with your speech + live interim transcript.
- **Context panel (⚙)** — paste your resume, key projects, and job description
  directly from the phone; saved instantly to the daemon without a restart.
- Auto-reconnecting WebSocket (2 s), `wakeLock` to keep the screen on, green
  border flash when an answer completes.
- State is kept server-side (last answer + last 10 chunks) and replayed on
  reconnect, so refreshing never shows a blank screen.

---

## Server API

REST (all require `?key=` secret):

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Phone UI HTML |
| `POST` | `/pause` | Pause transcription |
| `POST` | `/resume` | Resume transcription |
| `POST` | `/clear` | Clear the answer display |
| `POST` | `/switch-ai` | `{"ai": "groq\|nvidia\|claude\|chatgpt"}` |
| `POST` | `/switch-provider` | `{"provider": "gemini\|fallback"}` |
| `POST` | `/stop` | Stop the daemon |
| `GET` | `/status` | Provider, paused state, uptime, fallback AI |
| `GET` | `/devices` | List audio input devices |
| `GET` | `/context` | Get current candidate context |
| `POST` | `/context` | `{"resume": "...", "jd": "...", "projects": "..."}` |

WebSocket `/ws` push events: `interim`, `chunk`, `ai_start`, `ai_chunk`,
`ai_done`, `ai_provider`, `provider`, `status`, `error`, `clear`,
`context_updated`, `ping`.

---

## API keys

| Service | Used for | Free tier |
|---------|----------|-----------|
| Deepgram | Transcription (default path) | Yes |
| Groq | Answer AI — primary | Yes |
| NVIDIA NIM | Answer AI — auto-fallback when Groq 429s | Yes (needs one-time "Try API" click on build.nvidia.com) |
| Gemini | End-to-end Live path (optional) | Yes |
| Anthropic | Claude answer AI (optional) | Paid |
| OpenAI | ChatGPT answer AI (optional) | Paid |

At minimum you need **Deepgram + Groq** for a fully working zero-cost setup.
NVIDIA NIM is optional but recommended as a free Groq failover.

---

## Notes & cost

- Audio is sent as raw PCM16, 16 kHz, in 20 ms chunks (640 bytes mono /
  1280 bytes stereo).
- When paused, silence frames keep provider connections alive.
- The phone server key is auto-generated on first run and saved to `.env`.
- Candidate context (resume / JD / projects) is stored in
  `~/.interview-assistant/context.json` and injected into every AI prompt.
  Resume and projects are capped at 4000 chars each; JD at 3000 chars.
- Logs: `~/.interview-assistant/daemon.log`.
- PID: `~/.interview-assistant/daemon.pid`.

This tool is intended for legitimate, permitted use (e.g. practice,
accessibility, note-taking). Make sure you have consent to record and that your
use complies with the rules of any interview or call you participate in.

---

## Troubleshooting

- **Nothing shows on the phone / "deepgram-sdk not installed"**: you must use
  Deepgram SDK **v3** (`deepgram-sdk>=3.7,<4`). The v7 rewrite removed
  `LiveOptions`/`LiveTranscriptionEvents`. Reinstall:
  `pip install "deepgram-sdk>=3.7,<4"`.

- **Gemini logs "model ... not found for API version" / reconnect loop**: the
  model or endpoint is outdated. Use the v1beta endpoint and a current Live
  model (e.g. `gemini-3.1-flash-live-preview`). List your account's models
  with `curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"`.

- **It labels YOUR voice as the INTERVIEWER / both speakers look identical**:
  this was caused by `pw-record`. `pw-record --target <name>` matches PipeWire
  *node* names, so a PulseAudio source like `xxx.monitor` silently falls back
  to the default mic, making both channels capture your microphone. The app now
  prefers `parec`, which addresses monitor sources correctly. Verify the two
  streams differ with `python3 main.py --check-audio` (INTERVIEWER should
  reflect the call audio, not your mic).

- **You still hear yourself transcribed as YOU while on speakers**: that's your
  mic picking up the interviewer through the speakers (acoustic bleed). It's
  labeled YOU and never answered. Use **headphones** to eliminate it, or run
  `./setup-audio.sh` for a guaranteed mic-free interviewer channel.

- **Transcription is blank but audio plays**: the captured `SYS_SOURCE` is not
  the device your audio actually plays through. Leave `SYS_SOURCE` blank to
  auto-detect the current default-output monitor, and check levels with
  `python3 main.py --check-audio`.

- **Groq answers stop coming**: Groq free tier has per-minute token limits. The
  app automatically retries against NVIDIA NIM when Groq returns 429. Set
  `NVIDIA_API_KEY` in `.env` to enable this failover. You can also switch to
  Claude or ChatGPT from the phone UI's AI selector.

- **NVIDIA NIM returns 403**: some models require a one-time "Try API"
  registration click on build.nvidia.com before the key activates for that model.

- **Auto-switches to Gemini even though you chose Deepgram**: the daemon only
  auto-restores Gemini when `PRIMARY_PROVIDER=gemini`. With
  `PRIMARY_PROVIDER=deepgram` it stays on Deepgram; you can still switch
  manually from the phone UI.

- **Context not appearing in answers**: confirm context was saved either via
  `--resume/--jd/--projects` at start, or via the phone UI's gear (⚙) panel.
  Check `~/.interview-assistant/context.json` to verify it was written.

- **Logs**: `~/.interview-assistant/daemon.log`.
