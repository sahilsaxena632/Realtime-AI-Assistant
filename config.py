"""Central configuration for the Real-time AI Interview Assistant.

Values here are defaults. Anything sensitive (API keys) is read from the
environment (loaded from .env by python-dotenv) so secrets never live in code.
"""

import os
from pathlib import Path

# Load .env that sits next to this file. dotenv is optional - if it is not yet
# installed we fall back to a tiny parser so the CLI still works.
_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_env(path: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except Exception:
        pass
    # Minimal fallback parser.
    try:
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


_load_env(_ENV_PATH)


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    return val if val is not None and val != "" else default


def _expand(path: str) -> str:
    return str(Path(os.path.expanduser(path)).resolve())


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 20  # 20ms audio chunks to providers
# 16000 * 0.02 * 2 bytes (int16) = 640 bytes per 20ms mono frame.
BYTES_PER_CHUNK = int(SAMPLE_RATE * (CHUNK_MS / 1000.0)) * 2 * CHANNELS
SAMPLES_PER_CHUNK = int(SAMPLE_RATE * (CHUNK_MS / 1000.0))

MIC_DEVICE_INDEX = None
SYS_DEVICE_INDEX = None

# Automatic gain control - boosts quiet/attenuated capture (e.g. monitor
# sources) so speech is loud enough to transcribe.
AUDIO_AGC = _env("AUDIO_AGC", "1") not in ("0", "false", "False", "no")
AUDIO_TARGET_PEAK = float(_env("AUDIO_TARGET_PEAK", "8000"))
AUDIO_MAX_GAIN = float(_env("AUDIO_MAX_GAIN", "40"))

# PulseAudio/PipeWire source names (preferred over indices). When set we capture
# mic and system audio as two SEPARATE streams via parec, which lets us label
# speakers deterministically: mic = YOU, system monitor = INTERVIEWER.
MIC_SOURCE = _env("MIC_SOURCE")
SYS_SOURCE = _env("SYS_SOURCE")

# Stereo (mic = channel 0 / left, system = channel 1 / right) for Deepgram
# multichannel transcription. 320 samples * 2ch * 2 bytes = 1280 bytes / 20ms.
BYTES_PER_CHUNK_STEREO = BYTES_PER_CHUNK * 2

# ---------------------------------------------------------------------------
# Gemini Live (primary)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = _env("GEMINI_API_KEY")
# Live API models output AUDIO; we request output/input transcription to get
# text. Endpoint is v1beta. gemini-2.0-flash-exp / *-live-001 are shut down.
GEMINI_MODEL = _env("GEMINI_MODEL", "models/gemini-3.1-flash-live-preview")
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_RECONNECT_TRIES = 3
GEMINI_TIMEOUT_SEC = 8

# Which provider to start on: "deepgram" (fast text path, default) or "gemini".
# Gemini Live models are audio-out only (we transcribe their speech), which is
# slower; Deepgram + Groq is text-based, faster, and labels speakers per-channel.
PRIMARY_PROVIDER = _env("PRIMARY_PROVIDER", "deepgram").lower()

# ---------------------------------------------------------------------------
# Deepgram (fallback transcription)
# ---------------------------------------------------------------------------
DEEPGRAM_API_KEY = _env("DEEPGRAM_API_KEY")
DEEPGRAM_MODEL = _env("DEEPGRAM_MODEL", "nova-2")

# ---------------------------------------------------------------------------
# Fallback AI answer providers
# ---------------------------------------------------------------------------
DEFAULT_FALLBACK_AI = _env("DEFAULT_AI", "groq")  # groq / claude / chatgpt
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
CLAUDE_API_KEY = _env("ANTHROPIC_API_KEY") or _env("CLAUDE_API_KEY")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-4-6")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-4o")

# ---------------------------------------------------------------------------
# Smart chunker
# ---------------------------------------------------------------------------
SILENCE_SPLIT_SEC = 1.5
MIN_CHUNK_WORDS = 3
MAX_HISTORY_CHUNKS = 6

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5050
SERVER_KEY = _env("SERVER_KEY")  # auto-generated on first run if empty

# ---------------------------------------------------------------------------
# Daemon / paths
# ---------------------------------------------------------------------------
_BASE_DIR = _expand("~/.interview-assistant")
LOG_FILE = os.path.join(_BASE_DIR, "daemon.log")
PID_FILE = os.path.join(_BASE_DIR, "daemon.pid")
CALIBRATION_FILE = os.path.join(_BASE_DIR, "calibration.json")
SCREENSHOT_DIR = _expand("~/Pictures/interview_shots")

# Watchdog
GEMINI_RESTORE_INTERVAL_SEC = 60  # how often to probe Gemini while in fallback

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
# The Live model only outputs audio; we capture its spoken answer as text via
# output transcription. It hears the interviewer's questions (system audio).
GEMINI_SYSTEM_PROMPT = """You are a silent real-time interview assistant. You hear an interviewer asking
a candidate questions. When the interviewer asks a question, immediately answer
it concisely so the candidate can use your answer. If the interviewer is not
asking a question (small talk, instructions), stay silent.

Answer style:
- Coding: state the approach in one line, then the key steps.
- System design: the main components and trade-offs.
- Behavioral: a short STAR-style structure.
- General CS: a direct 2-3 sentence answer.
Keep answers under 150 words. Speak plainly, no markdown symbols."""

FALLBACK_SYSTEM_PROMPT = """You are a silent real-time coding interview assistant.
You receive questions from the interviewer.
Coding: 1-line approach + clean commented code.
System design: bullet points, trade-offs.
Behavioral: STAR, 3-4 bullets.
General CS: 2-3 sentences.
Max 200 words. Plain text only. No markdown."""


def ensure_dirs() -> None:
    """Create runtime directories if they do not exist."""
    os.makedirs(_BASE_DIR, exist_ok=True)
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
